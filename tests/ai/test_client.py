import asyncio
import json

import httpx
import pytest

from app.ai.client import AISettings, ChatJSONClient
from app.shared.errors import InvalidInput, ProviderUnavailable
from app.shared.models import TaskIntent


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
