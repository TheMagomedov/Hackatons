import asyncio
import json
import sqlite3

import pytest
from pydantic import BaseModel

from app.ai.local import LocalAISettings
from app.ai.resilience import InferenceStore, LocalInferenceRouter, inference_scope
from app.shared.errors import Forbidden, ProviderUnavailable, SafetyBlocked


class Answer(BaseModel):
    text: str


class Client:
    def __init__(self, model="primary:local", outcome=None):
        self.settings = LocalAISettings(model=model, max_retries=0)
        self.calls = 0
        self.outcome = outcome or Answer(text="An answer")
        self.closed = False

    async def complete(self, schema, instruction, data, validate=None):
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        result = schema.model_validate(self.outcome)
        if validate is not None and not validate(result):
            raise ProviderUnavailable("Output rejected")
        return result

    async def close(self):
        self.closed = True


async def test_external_cache_keeps_provider_effort_and_company_separate(tmp_path):
    from app.ai.client import AISettings
    from app.ai.resilience import InferenceRouter

    client = Client()
    client.settings = AISettings(api_key="private-test-key", model="gpt-6.1-sol",
                                 base_url="https://agentrouter.org/v1", reasoning_effort="low")
    store = InferenceStore(tmp_path / "cache.sqlite3")
    router = InferenceRouter([client], store)
    with scope():
        await call(router)
        await call(router)
        assert client.calls == 1
        client.settings = client.settings.model_copy(update={"reasoning_effort": "medium"})
        await call(router)
        assert client.calls == 2
    with scope("another-company"):
        await call(router)
    assert client.calls == 3
    with store.connection() as db:
        records = str([tuple(row) for row in db.execute("SELECT * FROM inference_cache")])
        records += str([tuple(row) for row in db.execute("SELECT * FROM inference_traces")])
    assert "private-test-key" not in records
    await router.close()
    assert client.closed


def scope(business="company-a", version=1):
    return inference_scope(business, "operation-a", "interpret", version)


async def call(router, data=None, **kwargs):
    return await router.complete(Answer, "Answer using the evidence", data or {"question": "Opening hours"}, **kwargs)


async def test_cache_survives_restart_expires_and_never_crosses_company_or_profile(tmp_path):
    now = [1000]
    path = tmp_path / "cache.sqlite3"
    store = InferenceStore(path, ttl_seconds=60, clock=lambda: now[0])
    client = Client()
    router = LocalInferenceRouter([client], store)
    with scope():
        first = await call(router)
        assert await call(router) == first
    assert client.calls == 1
    reopened = InferenceStore(path, ttl_seconds=60, clock=lambda: now[0])
    second_client = Client()
    reopened_router = LocalInferenceRouter([second_client], reopened)
    with scope():
        assert await call(reopened_router) == first
    assert second_client.calls == 0
    with scope("company-b"):
        await call(reopened_router)
    with scope(version=2):
        await call(reopened_router)
    assert second_client.calls == 2
    now[0] += 61
    with scope():
        await call(reopened_router)
    assert second_client.calls == 3


async def test_cache_keys_include_model_settings_source_data_and_system_policy(tmp_path, monkeypatch):
    store = InferenceStore(tmp_path / "cache.sqlite3")
    client = Client()
    router = LocalInferenceRouter([client], store)
    with scope():
        await call(router)
        await call(router)
        await call(router, {"question": "Opening hours", "document_version": "changed"})
        client.settings = client.settings.model_copy(update={"model": "different:local"})
        await call(router)
        client.settings = client.settings.model_copy(update={"num_ctx": 4096})
        await call(router)
        monkeypatch.setattr("app.ai.client.SYSTEM", "Updated policy")
        await call(router)
    assert client.calls == 5


async def test_cache_hit_revalidates_schema_and_current_business_rules(tmp_path):
    store = InferenceStore(tmp_path / "cache.sqlite3")
    client = Client(outcome=Answer(text="Old result"))
    router = LocalInferenceRouter([client], store)
    with scope():
        await call(router)
        client.outcome = Answer(text="New acceptable result")
        answer = await call(router, validate=lambda value: value.text.startswith("New"))
        assert answer.text == "New acceptable result"
        with store.connection() as db:
            db.execute("UPDATE inference_cache SET result=?", (json.dumps({"unrecognized": "stale schema"}),))
        await call(router)
        with store.connection() as db:
            db.execute("UPDATE inference_cache SET result='not JSON'")
        await call(router)
    assert client.calls == 4


async def test_cache_hit_cannot_bypass_current_guard_or_security_validator(tmp_path, monkeypatch):
    store = InferenceStore(tmp_path / "cache.sqlite3")
    client = Client()
    router = LocalInferenceRouter([client], store)
    with scope():
        await call(router)

        def denied(_data):
            raise SafetyBlocked()

        with monkeypatch.context() as patch:
            patch.setattr("app.safety.input_guard.guard_payload", denied)
            with pytest.raises(SafetyBlocked):
                await call(router)
        with pytest.raises(SafetyBlocked):
            await call(router, validate=denied)
    assert client.calls == 1


@pytest.mark.parametrize("operation", ["get", "put", "trace"])
async def test_derived_database_failure_does_not_lose_successful_model_response(tmp_path, monkeypatch, operation):
    store = InferenceStore(tmp_path / "cache.sqlite3")
    client = Client()
    router = LocalInferenceRouter([client], store)

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("database locked; private detail")

    monkeypatch.setattr(store, operation, fail)
    with scope():
        assert (await call(router)).text == "An answer"
    assert client.calls == 1


async def test_corrupt_derived_database_is_a_cache_miss_not_a_failed_inference(tmp_path):
    path = tmp_path / "cache.sqlite3"
    store = InferenceStore(path)
    path.write_bytes(b"broken derived database")
    client = Client()
    with scope():
        assert (await call(LocalInferenceRouter([client], store))).text == "An answer"


@pytest.mark.parametrize("error", [SafetyBlocked(), Forbidden("Access revoked"), asyncio.CancelledError()])
async def test_fallback_never_retries_safety_access_or_cancellation(tmp_path, error):
    store = InferenceStore(tmp_path / "cache.sqlite3")
    primary, fallback = Client(outcome=error), Client("fallback:local")
    with scope():
        with pytest.raises(type(error)):
            await call(LocalInferenceRouter([primary, fallback], store))
    assert primary.calls == 1 and fallback.calls == 0


async def test_explicit_fallback_follows_unavailability_once_and_traces_no_prompt(tmp_path):
    store = InferenceStore(tmp_path / "cache.sqlite3")
    primary = Client(outcome=ProviderUnavailable("Local server unavailable"))
    fallback = Client("fallback:local", outcome=Answer(text="Recovered"))
    router = LocalInferenceRouter([primary, fallback], store)
    private_question = "Contract with customer@example.org: when is delivery?"
    with scope():
        assert (await call(router, {"question": private_question})).text == "Recovered"
        assert (await call(router, {"question": private_question})).text == "Recovered"
    assert primary.calls == 1 and fallback.calls == 1
    traces = store.recent("company-a")
    assert {t["event"] for t in traces} == {"model_failed", "fallback", "model_ok", "cache_hit"}
    assert private_question not in json.dumps(traces) and "customer@example.org" not in json.dumps(traces)
    assert store.recent("company-b") == []
    await router.close()
    assert primary.closed and fallback.closed


async def test_no_context_and_disabled_cache_never_reuse_results(tmp_path):
    client = Client()
    router = LocalInferenceRouter([client], InferenceStore(tmp_path / "disabled.sqlite3", ttl_seconds=0))
    await call(router)
    await call(router)
    with scope():
        await call(router)
        await call(router)
    assert client.calls == 4
    assert len(router.store.recent("company-a")) == 2


def test_retention_bounds_cache_and_trace_counts_across_companies(tmp_path):
    clock = [1]
    store = InferenceStore(tmp_path / "cache.sqlite3", max_entries=2, max_traces=3, clock=lambda: clock[0])
    for index in range(8):
        clock[0] += 1
        business = "company-a" if index % 2 else "company-b"
        store.put(str(index), business, {"text": "Answer"}, "primary:local")
        store.trace({"business_id": business, "operation_id": str(index), "kind": "interpret"}, "completed")
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM inference_cache").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM inference_traces").fetchone()[0] == 3
        assert {r[0] for r in db.execute("SELECT cache_key FROM inference_cache")} == {"6", "7"}


def test_retention_and_fallback_model_configuration_are_bounded(tmp_path):
    for kwargs in [{"max_entries": -1}, {"max_traces": 0}, {"ttl_seconds": True}, {"ttl_seconds": 999999}]:
        with pytest.raises(ValueError):
            InferenceStore(tmp_path / "cache.sqlite3", **kwargs)
    with pytest.raises(ValueError):
        LocalAISettings(model="example-cloud")
    with pytest.raises(ValueError):
        LocalAISettings(base_url="https://external.invalid")
    with pytest.raises(ValueError):
        LocalInferenceRouter([Client(), Client(), Client()], None)
