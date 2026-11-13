import asyncio
import json

import httpx
import pytest

from app.ai.local import LocalAISettings, OllamaJSONClient, build_provider, output_schema
from app.ai.provider import ModelAI
from app.config import Settings
from app.shared.errors import InvalidInput, ProviderUnavailable
from app.shared.models import BusinessProfile, Fact, PlanItem, TaskIntent

LOCAL_METADATA = {"details": {"format": "gguf"}, "model_info": {"general.architecture": "qwen3"}}


@pytest.fixture(autouse=True)
def isolate_local_config(monkeypatch):
    for name in (
        "LOCAL_TEXT_MODEL", "LOCAL_TEXT_API_BASE", "LOCAL_TEXT_TIMEOUT_SECONDS", "LOCAL_TEXT_MAX_RETRIES",
        "LOCAL_TEXT_NUM_CTX", "LOCAL_TEXT_NUM_THREADS", "LOCAL_TEXT_MAX_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)


def response(content='{"action":"report"}', **overrides):
    return httpx.Response(200, json={
        "model": "qwen3:8b", "done": True, "done_reason": "stop",
        "message": {"role": "assistant", "content": content}, **overrides,
    })


def client(handler, *, metadata=LOCAL_METADATA, cloud_status=None, **config):
    requests = []

    async def transport(request):
        requests.append(request)
        if request.url.path == "/api/status":
            return httpx.Response(200, json={"cloud": {"disabled": True}} if cloud_status is None else cloud_status)
        if request.url.path == "/api/show":
            return httpx.Response(200, json=metadata)
        result = handler(request)
        return await result if asyncio.iscoroutine(result) else result

    return OllamaJSONClient(LocalAISettings(**config), transport=httpx.MockTransport(transport)), requests


async def test_native_schema_cpu_request_and_private_data_only_after_local_check(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "unused-cloud-key")
    monkeypatch.setenv("TEXT_API_BASE", "https://cloud.invalid/v1")
    secret = "Кофейня с секретным новым меню"
    instance, calls = client(lambda request: response())
    try:
        assert (await instance.complete(TaskIntent, "Распознай", {"text": secret})).action == "report"
    finally:
        await instance.close()
    assert [str(request.url) for request in calls] == [
        "http://127.0.0.1:11434/api/status", "http://127.0.0.1:11434/api/show", "http://127.0.0.1:11434/api/chat",
    ]
    assert calls[0].method == "GET" and secret not in calls[1].content.decode()
    assert all("authorization" not in request.headers for request in calls)
    payload = json.loads(calls[2].content)
    assert payload["model"] == "qwen3:4b-instruct" and payload["format"] == output_schema(TaskIntent)
    assert payload["think"] is False and payload["stream"] is False
    assert payload["truncate"] is False and payload["shift"] is False
    assert payload["options"]["num_gpu"] == 0 and payload["options"]["num_predict"] == 3000
    assert {key: payload["options"][key] for key in ("temperature", "top_p", "top_k", "min_p")} == {
        "temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0,
    }
    assert secret in payload["messages"][1]["content"]
    assert secret not in payload["messages"][0]["content"]
    assert "unused-cloud-key" not in calls[2].content.decode()
    assert instance._http.is_closed


async def test_environment_proxies_are_never_consulted(monkeypatch):
    def forbidden_proxies():
        pytest.fail("Local Ollama client must not consult environment proxies")

    monkeypatch.setattr(httpx._client, "get_environment_proxies", forbidden_proxies)
    instance = OllamaJSONClient(LocalAISettings())
    await instance.close()


@pytest.mark.parametrize("url", [
    "https://api.example", "http://localhost:11434", "http://127.0.0.2:11434",
    "http://[::1]:11434", "http://127.0.0.1.evil.test:11434", "http://2130706433:11434",
    "http://user:secret@127.0.0.1:11434", "http://127.0.0.1:11434/api",
    "http://127.0.0.1:11434?token=secret", "http://127.0.0.1:11434#fragment",
    "http://127.0.0.1:11434\n", "http://127.0.0.1", "http://127.0.0.1:0",
])
def test_nonlocal_or_ambiguous_addresses_rejected(url):
    with pytest.raises(ValueError):
        LocalAISettings(base_url=url)


@pytest.mark.parametrize("model", ["qwen3-cloud", "gpt-oss:120b-cloud", "https://cloud/model", "a/b", ""])
def test_cloud_or_registry_model_names_rejected(model):
    with pytest.raises(ValueError):
        LocalAISettings(model=model)


def test_profile_decoder_bounds_do_not_change_or_relax_application_schema():
    original = BusinessProfile.model_json_schema()
    bounded = output_schema(BusinessProfile)
    assert BusinessProfile.model_json_schema() == original
    assert set(original["required"]) <= set(bounded["required"]) == set(original["properties"])
    assert bounded["additionalProperties"] is False
    assert bounded["properties"]["tone"]["maxLength"] == 180
    assert bounded["properties"]["facts"]["maxItems"] == 20
    assert bounded["$defs"]["Fact"]["properties"]["source_ids"] == original["$defs"]["Fact"]["properties"]["source_ids"]
    assert bounded["$defs"]["Fact"]["properties"]["text"]["maxLength"] <= original["$defs"]["Fact"]["properties"]["text"]["maxLength"]
    assert bounded["properties"]["description"]["maxLength"] <= original["properties"]["description"]["maxLength"]
    assert bounded["$defs"]["Fact"]["required"] == ["text", "source_ids"]
    intent = output_schema(TaskIntent)
    assert set(intent["required"]) == set(TaskIntent.model_json_schema()["properties"])
    assert intent["properties"]["action"] == TaskIntent.model_json_schema()["properties"]["action"]


@pytest.mark.parametrize("metadata", [
    {}, None, [], {**LOCAL_METADATA, "remote_host": "https://ollama.com"},
    {**LOCAL_METADATA, "remote_model": "cloud-alias"}, {**LOCAL_METADATA, "details": None},
])
async def test_unconfirmed_or_remote_model_stops_before_user_text_sent(metadata):
    instance, calls = client(lambda request: pytest.fail("Must not send /chat"), metadata=metadata)
    try:
        with pytest.raises(ProviderUnavailable, match="не подтвердил локальную"):
            await instance.complete(TaskIntent, "Intent", {"text": "private input"})
    finally:
        await instance.close()
    assert len(calls) == 2 and all("private input" not in request.content.decode() for request in calls)


@pytest.mark.parametrize("status", [{}, [], {"cloud": None}, {"cloud": {"disabled": False}},
                                    {"cloud": {"disabled": "true"}}, {"cloud": {"disabled": 1}}])
async def test_cloud_enabled_or_unknown_server_stops_before_model_or_user_input(status):
    instance, calls = client(lambda request: pytest.fail("Must not generate"), cloud_status=status)
    try:
        with pytest.raises(ProviderUnavailable, match="OLLAMA_NO_CLOUD=1"):
            await instance.complete(TaskIntent, "Intent", {"text": "private input"})
    finally:
        await instance.close()
    assert len(calls) == 1 and calls[0].url.path == "/api/status" and not calls[0].content


@pytest.mark.parametrize("status", [302, 307, 401, 403, 404, 400])
async def test_redirect_or_permanent_error_no_retry_no_body_leak(status):
    instance, calls = client(lambda request: httpx.Response(
        status, text="private raw response", headers={"Location": "https://cloud.invalid/api/chat"},
    ))
    try:
        with pytest.raises(ProviderUnavailable) as error:
            await instance.complete(TaskIntent, "Intent", {})
        assert "private raw response" not in str(error.value)
    finally:
        await instance.close()
    assert len(calls) == 3


async def test_shared_bounded_retry_and_invalid_json_repair():
    attempts = []

    def handler(request):
        attempts.append(json.loads(request.content))
        return httpx.Response(503) if len(attempts) == 1 else response("private invalid output")

    instance, calls = client(handler)
    try:
        with pytest.raises(ProviderUnavailable):
            await instance.complete(TaskIntent, "Intent", {})
    finally:
        await instance.close()
    assert len(attempts) == 2 and len(calls) == 6
    assert "private invalid output" not in json.dumps(attempts)


async def test_schema_and_application_constraints_repaired_without_reflecting_output():
    outputs = [response('{"action":"answer","answer":"untrusted output"}'), response()]
    instance, calls = client(lambda request: outputs.pop(0))
    try:
        result = await instance.complete(TaskIntent, "Intent", {}, lambda intent: intent.action == "report")
        assert result.action == "report"
    finally:
        await instance.close()
    assert len(calls) == 6
    assert all("untrusted output" not in request.content.decode() for request in calls)
    assert "Предыдущий результат" in json.loads(calls[-1].content)["messages"][0]["content"]


@pytest.mark.parametrize("overrides", [
    {"done": False}, {"done": 1}, {"done_reason": "length"},
    {"message": None}, {"message": {"role": "assistant", "content": {"action": "report"}}},
    {"message": {"role": "assistant", "content": '{"action":"report"}', "tool_calls": [{}]}},
])
async def test_incomplete_or_unexpected_envelope_rejected(overrides):
    instance, calls = client(lambda request: response(**overrides), max_retries=0)
    try:
        with pytest.raises(ProviderUnavailable, match="формату"):
            await instance.complete(TaskIntent, "Intent", {})
    finally:
        await instance.close()
    assert len(calls) == 3


@pytest.mark.parametrize("marker", ["remote_host", "remote_model"])
async def test_remote_execution_marker_stops_without_retry(marker):
    instance, calls = client(lambda request: response(**{marker: "private-cloud-marker"}), max_retries=2)
    try:
        with pytest.raises(ProviderUnavailable, match="облачное исполнение") as error:
            await instance.complete(TaskIntent, "Intent", {})
        assert "private-cloud-marker" not in str(error.value)
    finally:
        await instance.close()
    assert len(calls) == 3


async def test_request_and_response_sizes_bounded_before_model_use():
    instance, calls = client(lambda request: httpx.Response(200, content=b"x" * 1025), max_response_bytes=1024)
    try:
        with pytest.raises(InvalidInput, match="Слишком много"):
            await instance.complete(TaskIntent, "Intent", {"text": "x" * 160_001})
        assert not calls
        with pytest.raises(ProviderUnavailable, match="размер"):
            await instance.complete(TaskIntent, "Intent", {})
    finally:
        await instance.close()
    assert len(calls) == 3


async def test_total_timeout_and_external_cancellation_do_not_retry():
    async def slow(request):
        await asyncio.sleep(10)

    instance, calls = client(slow, timeout_seconds=0.02)
    try:
        with pytest.raises(ProviderUnavailable, match="вовремя"):
            await instance.complete(TaskIntent, "Intent", {})
    finally:
        await instance.close()
    assert len(calls) == 3

    async def cancelled(request):
        raise asyncio.CancelledError

    instance, calls = client(cancelled)
    try:
        with pytest.raises(asyncio.CancelledError):
            await instance.complete(TaskIntent, "Intent", {})
    finally:
        await instance.close()
    assert len(calls) == 3


async def test_connection_error_has_no_cloud_fallback_or_raw_exception():
    def failure(request):
        raise httpx.ConnectError("private exception", request=request)

    instance, calls = client(failure)
    try:
        with pytest.raises(ProviderUnavailable) as error:
            await instance.complete(TaskIntent, "Intent", {})
        assert "private exception" not in str(error.value)
    finally:
        await instance.close()
    assert len(calls) == 3


async def test_modelai_still_enforces_source_ids_and_factual_constraints():
    instance, calls = client(lambda request: response(json.dumps({
        "text": "Кофе за 999 рублей", "image_brief": "Кофе", "used_source_ids": ["source-1"],
    }, ensure_ascii=False)), max_retries=0)
    provider = ModelAI(instance)
    profile = BusinessProfile(
        name="Кофейня", description="Кофе", facts=[Fact(text="Готовим кофе", source_ids=["source-1"])],
    )
    try:
        with pytest.raises(ProviderUnavailable, match="ограничениям"):
            await provider.write_post(profile, PlanItem(topic="Кофе", goal="Меню", brief="Кофе", image_brief="Кофе"))
    finally:
        await provider.close()
    assert len(calls) == 3
    payload = json.loads(calls[-1].content)
    assert "не выдумывай" in payload["messages"][0]["content"]
    assert "1–2 конкретных предложения" in payload["messages"][0]["content"]


async def test_factory_honors_local_settings_and_global_timeout_without_network(monkeypatch):
    monkeypatch.setenv("LOCAL_TEXT_MODEL", "qwen3:4b")
    monkeypatch.setenv("LOCAL_TEXT_TIMEOUT_SECONDS", "500")
    monkeypatch.setenv("LOCAL_TEXT_NUM_THREADS", "8")
    provider = build_provider(Settings(provider_timeout_seconds=240, max_posts_per_job=2))
    try:
        assert isinstance(provider, ModelAI) and isinstance(provider.client, OllamaJSONClient)
        assert provider.client.settings.model == "qwen3:4b"
        assert provider.client.settings.timeout_seconds == 240
        assert provider.client.settings.num_threads == 8 and provider.max_posts == 2
    finally:
        await provider.close()


def test_invalid_environment_safe_error_without_config_value(monkeypatch):
    monkeypatch.setenv("LOCAL_TEXT_API_BASE", "https://private-token@cloud.invalid")
    with pytest.raises(InvalidInput) as error:
        LocalAISettings.from_env()
    assert "private-token" not in str(error.value)
