import asyncio
import json

import httpx
import pytest

from app.ai.client import AISettings, ChatJSONClient
from app.shared.errors import InvalidInput, ProviderUnavailable
from app.shared.models import TaskIntent


@pytest.fixture(autouse=True)
def isolate_provider_environment(monkeypatch):
    for name in (
        "TEXT_API_KEY", "TEXT_MODEL", "TEXT_API_BASE", "TEXT_TIMEOUT_SECONDS", "TEXT_MAX_RETRIES",
        "AI_API_KEY", "AI_MODEL", "AI_BASE_URL", "AI_TIMEOUT_SECONDS", "AI_MAX_RETRIES",
        "TEXT_REASONING_EFFORT", "AI_REASONING_EFFORT",
        "TEXT_WIRE_API", "AI_WIRE_API",
    ):
        monkeypatch.delenv(name, raising=False)


def response(content, *, finish="stop"):
    return httpx.Response(200, json={
        "choices": [{"finish_reason": finish, "message": {"content": content}}],
    })


def client(handler, **kwargs):
    return ChatJSONClient(
        AISettings(api_key="test-secret", model="test-model", **kwargs),
        transport=httpx.MockTransport(handler),
    )


async def test_real_wire_shape_and_secret_only_in_header():
    def handler(request):
        payload = json.loads(request.content)
        assert request.url == "https://api.openai.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert payload["model"] == "test-model"
        assert payload["response_format"] == {"type": "json_object"}
        assert "test-secret" not in request.content.decode()
        assert "JSON" in payload["messages"][0]["content"]
        return response('{"action":"report"}')

    instance = client(handler)
    try:
        result = await instance.complete(TaskIntent, "Распознай запрос", {"text": "Отчёт"})
        assert result.action == "report"
    finally:
        await instance.close()
    assert instance._http.is_closed


async def test_selected_reasoning_effort_reaches_provider_without_changing_model(monkeypatch):
    for key, value in {
        "TEXT_API_KEY": "private-test-key", "TEXT_MODEL": "gpt-6.1-sol",
        "TEXT_API_BASE": "https://agentrouter.org/v1", "TEXT_REASONING_EFFORT": "low",
        "AI_REASONING_EFFORT": "high",
    }.items():
        monkeypatch.setenv(key, value)

    def handler(request):
        payload = json.loads(request.content)
        assert request.url == "https://agentrouter.org/v1/chat/completions"
        assert payload["model"] == "gpt-6.1-sol"
        assert payload["reasoning_effort"] == "low"
        return response('{"action":"report"}')

    instance = ChatJSONClient(AISettings.from_env(), transport=httpx.MockTransport(handler))
    try:
        assert (await instance.complete(TaskIntent, "Intent", {})).action == "report"
    finally:
        await instance.close()


async def test_external_access_denial_is_not_retried_or_replaced():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"type": "unauthorized_client_error"})

    instance = client(handler, reasoning_effort="low")
    try:
        with pytest.raises(ProviderUnavailable, match="отклонил доступ"):
            await instance.complete(TaskIntent, "Intent", {})
    finally:
        await instance.close()
    assert len(calls) == 1


def responses_body(text='{"action":"report"}', **updates):
    return {"status": "completed", "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": text}]},
    ], **updates}


async def test_responses_wire_uses_low_effort_redacts_input_and_disables_storage():
    def handler(request):
        payload = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert payload["reasoning"] == {"effort": "low"}
        assert payload["store"] is False
        assert payload["text"] == {"format": {"type": "json_object"}}
        assert "private@example.com" not in request.content.decode()
        assert "[EMAIL]" in request.content.decode()
        assert "JSON" in payload["instructions"]
        assert "messages" not in payload and "tools" not in payload
        return httpx.Response(200, json=responses_body())

    instance = client(handler, wire_api="responses", reasoning_effort="low")
    try:
        assert (await instance.complete(TaskIntent, "Intent", {"email": "private@example.com"})).action == "report"
    finally:
        await instance.close()


@pytest.mark.parametrize("body", [
    responses_body(status="incomplete"), responses_body(error={"message": "private error"}),
    responses_body(output=[]), responses_body(output=[{"type": "function_call", "name": "publish"}]),
    responses_body(output=[{"type": "message", "role": "assistant", "status": "in_progress", "content": []}]),
    responses_body(output=[None]), None,
])
async def test_responses_rejects_incomplete_empty_or_tool_output(body):
    instance = client(lambda _: httpx.Response(200, json=body), wire_api="responses", max_retries=0)
    try:
        with pytest.raises(ProviderUnavailable):
            await instance.complete(TaskIntent, "Intent", {})
    finally:
        await instance.close()


async def test_responses_refusal_has_no_repair_retry():
    requests = []

    def handler(request):
        requests.append(request)
        body = responses_body()
        body["output"][1]["content"] = [{"type": "refusal", "refusal": "No"}]
        return httpx.Response(200, json=body)

    instance = client(handler, wire_api="responses")
    try:
        with pytest.raises(ProviderUnavailable, match="отказался"):
            await instance.complete(TaskIntent, "Intent", {})
    finally:
        await instance.close()
    assert len(requests) == 1


async def test_invalid_output_repaired_once_without_reflecting_raw_output():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return response("untrusted-output-secret")
        return response('{"action":"prepare"}')

    instance = client(handler)
    try:
        assert (await instance.complete(TaskIntent, "Intent", {})).action == "prepare"
    finally:
        await instance.close()
    assert len(requests) == 2
    assert "untrusted-output-secret" not in json.dumps(requests)


@pytest.mark.parametrize("envelope", [None, [], {}, {"choices": [None]}, {
    "choices": [{"message": None, "finish_reason": "stop"}],
}])
async def test_malformed_envelopes_produce_safe_error(envelope):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=json.dumps(envelope))

    instance = client(handler)
    try:
        with pytest.raises(ProviderUnavailable, match="формату"):
            await instance.complete(TaskIntent, "Intent", {})
    finally:
        await instance.close()
    assert len(calls) == 2


@pytest.mark.parametrize("status", [401, 403, 400, 302])
async def test_access_failure_never_retries_or_exposes_response(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="test-secret private-provider-body")

    instance = client(handler)
    try:
        with pytest.raises(ProviderUnavailable) as exc:
            await instance.complete(TaskIntent, "Intent", {})
        assert "test-secret" not in str(exc.value)
        assert "private-provider-body" not in str(exc.value)
    finally:
        await instance.close()
    assert len(calls) == 1


async def test_retry_budget_shared_by_http_failure_and_format_repair():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else response("invalid")

    instance = client(handler)
    try:
        with pytest.raises(ProviderUnavailable):
            await instance.complete(TaskIntent, "Intent", {})
    finally:
        await instance.close()
    assert len(calls) == 2


async def test_total_timeout_includes_mock_transport_wait():
    async def handler(request):
        await asyncio.sleep(10)
        return response('{"action":"report"}')

    instance = client(handler, timeout_seconds=0.02)
    try:
        with pytest.raises(ProviderUnavailable, match="вовремя"):
            await instance.complete(TaskIntent, "Intent", {})
    finally:
        await instance.close()


async def test_cancelled_error_is_not_swallowed():
    async def handler(request):
        raise asyncio.CancelledError

    instance = client(handler)
    try:
        with pytest.raises(asyncio.CancelledError):
            await instance.complete(TaskIntent, "Intent", {})
    finally:
        await instance.close()


async def test_oversize_provider_response_is_rejected():
    instance = client(lambda request: httpx.Response(200, text="x" * 1025), max_response_bytes=1024)
    try:
        with pytest.raises(ProviderUnavailable, match="размер"):
            await instance.complete(TaskIntent, "Intent", {})
    finally:
        await instance.close()


async def test_truncated_model_result_not_accepted_even_if_json_valid():
    instance = client(lambda request: response('{"action":"report"}', finish="length"))
    try:
        with pytest.raises(ProviderUnavailable):
            await instance.complete(TaskIntent, "Intent", {})
    finally:
        await instance.close()


def test_environment_validation_has_no_secret_in_error(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "test-secret")
    monkeypatch.setenv("AI_MODEL", "test")
    monkeypatch.setenv("AI_MAX_RETRIES", "99")
    with pytest.raises(InvalidInput) as exc:
        AISettings.from_env()
    assert "test-secret" not in str(exc.value)


@pytest.mark.parametrize("url", ["http://api.example/v1", "https://key@api.example/v1", "https://api.example/?key=x"])
def test_insecure_or_credential_bearing_base_url_rejected(url):
    with pytest.raises(ValueError):
        AISettings(api_key="secret", model="model", base_url=url)


def test_canonical_text_configuration_wins_when_both_families_complete(monkeypatch):
    for key, value in {
        "TEXT_API_KEY": "text-key", "TEXT_MODEL": "text-model", "TEXT_API_BASE": "https://text.example/v1",
        "TEXT_TIMEOUT_SECONDS": "9", "TEXT_MAX_RETRIES": "0",
        "AI_API_KEY": "legacy-key", "AI_MODEL": "legacy-model", "AI_BASE_URL": "https://legacy.example/v1",
        "AI_TIMEOUT_SECONDS": "20", "AI_MAX_RETRIES": "2",
    }.items():
        monkeypatch.setenv(key, value)
    config = AISettings.from_env()
    assert config.api_key.get_secret_value() == "text-key"
    assert config.model == "text-model"
    assert config.base_url == "https://text.example/v1"
    assert config.timeout_seconds == 9 and config.max_retries == 0


def test_legacy_key_ignores_text_example_defaults(monkeypatch):
    for key, value in {
        "TEXT_API_KEY": "", "TEXT_MODEL": "example-model", "TEXT_API_BASE": "https://text.example/v1",
        "AI_API_KEY": "legacy-key", "AI_MODEL": "legacy-model",
    }.items():
        monkeypatch.setenv(key, value)
    config = AISettings.from_env()
    assert config.api_key.get_secret_value() == "legacy-key"
    assert config.model == "legacy-model"
    assert config.base_url == "https://api.openai.com/v1"


@pytest.mark.parametrize("values", [
    {"AI_API_KEY": "legacy-key", "TEXT_MODEL": "text-model", "TEXT_API_BASE": "https://text.example/v1"},
    {"TEXT_API_KEY": "text-key", "TEXT_MODEL": "", "AI_MODEL": "legacy-model", "AI_BASE_URL": "https://legacy.example/v1"},
    {"TEXT_API_KEY": "text-key", "TEXT_MODEL": "", "AI_API_KEY": "legacy-key", "AI_MODEL": "legacy-model"},
])
def test_partial_families_never_supply_each_others_model_or_url(monkeypatch, values):
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert AISettings.from_env_if_configured() is None
    with pytest.raises(InvalidInput):
        AISettings.from_env()


@pytest.mark.parametrize("key,value", [
    ("TEXT_API_BASE", "http://insecure.example/v1"), ("TEXT_TIMEOUT_SECONDS", "bad"),
    ("TEXT_MAX_RETRIES", "99"), ("AI_BASE_URL", "http://insecure.example/v1"),
    ("TEXT_REASONING_EFFORT", "unsupported"),
])
def test_invalid_optional_configuration_fails_even_when_unconfigured(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(InvalidInput):
        AISettings.from_env_if_configured()


def test_text_family_preserves_main_model_default_without_using_legacy_values(monkeypatch):
    monkeypatch.setenv("TEXT_API_KEY", "text-key")
    monkeypatch.setenv("AI_MODEL", "other-model")
    monkeypatch.setenv("AI_BASE_URL", "https://legacy.example/v1")
    config = AISettings.from_env()
    assert config.model == "gpt-4.1-mini"
    assert config.base_url == "https://api.openai.com/v1"
