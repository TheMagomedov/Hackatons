"""Mini App boundaries against SQLite and signed Telegram sessions; no live network."""

from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from pypdf import PdfWriter

from app.config import Settings
from app.core.runtime import build_runtime
from app.miniapp.server import JSON_LIMIT, create_app
from app.shared.models import TaskIntent
from tests.test_miniapp import OWNER, TOKEN, signed_data


@pytest.fixture
async def mini(tmp_path):
    runtime = await build_runtime(Settings(owner_id=OWNER, data_dir=tmp_path))
    service = runtime.service
    try:
        async with TestClient(TestServer(create_app(service, token=TOKEN))) as client:
            client.session.headers.update({"X-Telegram-Init-Data": signed_data(service.now())})
            business = service.create_business(OWNER, "Булочная")
            service.add_text_source(OWNER, business.id, "Свежий хлеб каждый день с 8 утра")
            business = await service.analyze_business(OWNER, business.id)
            service.confirm_profile(OWNER, business.id, business.profile_version)
            business = await service.connect_channel(OWNER, business.id, "@bakery")
            yield SimpleNamespace(client=client, service=service, runtime=runtime, business=business,
                                  prefix=f"/mini/api/business/{business.id}")
    finally:
        await runtime.close()


async def prepared_post(mini):
    job = mini.service.request_preparation(OWNER, mini.business.id, "Пост про хлеб", "fixture")
    assert await mini.service.prepare_next_job()
    return mini.service.get_job_posts(OWNER, job.id)[0]


async def test_schedule_runs_without_dialogue_but_never_approves_material(mini):
    now = mini.service.now()
    mini.service.clock = lambda: now
    response = await mini.client.post(mini.prefix + "/automation", json={
        "enabled": True, "interval_hours": 24, "request": "Один полезный пост про хлеб",
        "first_run_at": (now + timedelta(minutes=1)).isoformat(),
    })
    assert response.status == 200
    assert (await response.json())["automation_enabled"] is True
    mini.service.clock = lambda: now + timedelta(minutes=2)
    assert mini.service.schedule_automations() == 1
    assert mini.service.schedule_automations() == 0
    assert await mini.service.prepare_next_job()
    posts = mini.service.list_posts(OWNER, mini.business.id)
    assert posts and all(p.status == "review" and p.approved_revision is None for p in posts)
    assert await mini.service.publish_due_once() is False
    assert mini.service.publisher.sent == []
    control = await (await mini.client.get(mini.prefix + "/control")).json()
    assert control["jobs"][0]["status"] == "done"
    assert control["business"]["next_generation_at"] > now.isoformat()
    assert control["usage"]["period_timezone"] == "UTC"


async def test_pause_stops_approved_post_until_owner_resumes(mini):
    post = await prepared_post(mini)
    now = mini.service.now()
    mini.service.clock = lambda: now
    post = mini.service.edit_post(OWNER, post.id, post.revision, scheduled_at=now + timedelta(minutes=1))
    mini.service.approve_post(OWNER, post.id, post.revision)
    assert (await mini.client.post(mini.prefix + "/pause", json={"paused": True})).ok
    mini.service.clock = lambda: now + timedelta(minutes=2)
    assert await mini.service.publish_due_once() is False
    assert not mini.service.publisher.sent
    assert (await mini.client.post(mini.prefix + "/pause", json={"paused": False})).ok
    assert await mini.service.publish_due_once() is True
    assert len(mini.service.publisher.sent) == 1
    assert (await mini.client.post(mini.prefix + "/pause", json={"paused": "false"})).status == 400


async def test_job_cancel_is_persistent_and_does_not_spend_on_ai(mini):
    created = await (await mini.client.post(mini.prefix + "/prepare", json={
        "request": "Один пост", "request_id": "cancel-me",
    })).json()
    response = await mini.client.post(f"/mini/api/job/{created['id']}/cancel", json={})
    assert (await response.json())["status"] == "cancelled"
    assert await mini.service.prepare_next_job() is False
    jobs = await (await mini.client.get(mini.prefix + "/jobs")).json()
    assert jobs["items"][0]["status"] == "cancelled"
    assert mini.service.list_posts(OWNER, mini.business.id) == []


@pytest.mark.parametrize("kind", ["text", "image"])
async def test_ai_revision_invalidates_approval_and_duplicate_request_is_not_paid_again(mini, kind):
    post = await prepared_post(mini)
    post = mini.service.edit_post(OWNER, post.id, post.revision,
                                 scheduled_at=mini.service.now() + timedelta(hours=1))
    mini.service.approve_post(OWNER, post.id, post.revision)
    prefix = f"/mini/api/post/{post.id}"
    payload = {"revision": post.revision, "feedback": "Больше утреннего света", "request_id": f"revise-{kind}"}
    response = await mini.client.post(prefix + f"/revise-{kind}", json=payload)
    assert response.status == 200
    changed = await response.json()
    assert changed["status"] == "review" and changed["approved_revision"] is None
    assert changed["revision"] > post.revision
    used = mini.service.ai_usage(OWNER)["used_today"]
    repeated = await (await mini.client.post(prefix + f"/revise-{kind}", json=payload)).json()
    assert repeated["revision"] == changed["revision"]
    assert mini.service.ai_usage(OWNER)["used_today"] == used
    assert (await mini.client.post(prefix + "/approve", json={"revision": post.revision})).status == 409
    assert mini.service.publisher.sent == []


async def test_profile_change_requires_explicit_review_in_mini_app(mini):
    post = await prepared_post(mini)
    business = mini.service.get_business(OWNER, mini.business.id)
    updated = mini.service.save_profile(OWNER, business.id, business.profile_version,
                                       business.profile.model_copy(update={"tone": "Деловой"}))
    mini.service.confirm_profile(OWNER, business.id, updated.profile_version)
    post = mini.service.edit_post(OWNER, post.id, post.revision,
                                 scheduled_at=mini.service.now() + timedelta(hours=1))
    prefix = f"/mini/api/post/{post.id}"
    assert (await mini.client.post(prefix + "/approve", json={"revision": post.revision})).status == 409
    response = await mini.client.post(prefix + "/refresh-context", json={"revision": post.revision})
    reviewed = await response.json()
    assert reviewed["profile_version"] == updated.profile_version
    assert reviewed["status"] == "review"
    assert reviewed["approved_revision"] is None


async def test_company_boundaries_are_checked_before_read_or_mutation(mini):
    foreign = mini.service.repo.create_business(OWNER + 1, "Чужая компания", "Europe/Moscow", mini.service.now())
    foreign_url = f"/mini/api/business/{foreign.id}"
    for path in ("/control", "/jobs", "/history", "/posts", "/report"):
        response = await mini.client.get(foreign_url + path)
        assert response.status == 403, path
    for path, payload in (("/pause", {"paused": True}),
                          ("/automation", {"enabled": False}),
                          ("/prepare", {"request": "Пост", "request_id": "foreign"}),
                          ("/chat", {"text": "Что вы знаете?", "request_id": "foreign-chat"})):
        assert (await mini.client.post(foreign_url + path, json=payload)).status == 403
    assert mini.service.repo.get_business(foreign.id).publications_paused is False
    assert mini.service.repo.list_jobs(foreign.id) == []


async def test_chat_has_persisted_company_history_and_deduplicates_preparation(mini):
    mini.service.ai.interpret_request = AsyncMock(return_value=TaskIntent(
        action="prepare", request="Подготовь один пост про хлеб с указанием открытия в 8 утра",
    ))
    payload = {"text": "Сделай пост об этом", "request_id": "contextual-chat"}
    first = await mini.client.post(mini.prefix + "/chat", json=payload)
    assert first.status == 200
    result = await first.json()
    assert result["job"]["request"] == result["intent"]["request"]
    again = await (await mini.client.post(mini.prefix + "/chat", json=payload)).json()
    assert again["job"]["id"] == result["job"]["id"]
    assert mini.service.ai.interpret_request.await_count == 1
    history = await (await mini.client.get(mini.prefix + "/history")).json()
    assert len(history["items"]) == 1
    assert history["items"][0]["user_text"] == payload["text"]
    second = mini.service.create_business(OWNER, "Другая компания")
    history2 = await (await mini.client.get(f"/mini/api/business/{second.id}/history")).json()
    assert history2["items"] == []
    assert not mini.service.publisher.sent


async def test_pdf_upload_is_owned_bounded_and_not_publicly_readable(mini):
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(stream)
    data = FormData()
    data.add_field("file", stream.getvalue(), filename="brief.pdf", content_type="application/pdf")
    response = await mini.client.post(mini.prefix + "/pdf", data=data)
    assert response.status == 200
    source = await response.json()
    assert source["business_id"] == mini.business.id and source["kind"] == "pdf"
    # The image route must never disclose private PDFs.
    assert (await mini.client.get(f"/mini/api/asset/{source['asset_id']}")).status == 404
    invalid = FormData()
    invalid.add_field("file", b"not PDF", filename="bad.pdf", content_type="application/pdf")
    assert (await mini.client.post(mini.prefix + "/pdf", data=invalid)).status == 400
    assert (await mini.client.post(mini.prefix + "/pdf", json={})).status == 400


async def test_bad_and_oversized_json_cannot_reach_domain_service(mini):
    response = await mini.client.post(mini.prefix + "/pause", data="not JSON", headers={"Content-Type": "application/json"})
    assert response.status == 400
    response = await mini.client.post(mini.prefix + "/pause", data='{"paused":true}', headers={"Content-Type": "text/plain"})
    assert response.status == 400
    response = await mini.client.post(mini.prefix + "/pause", data=b" " * (JSON_LIMIT + 1),
                                      headers={"Content-Type": "application/json"})
    assert response.status == 413
    assert response.headers["Cache-Control"] == "no-store"
    assert mini.service.get_business(OWNER, mini.business.id).publications_paused is False


async def test_pdf_limit_and_extra_parts_are_rejected_before_storing_source(mini):
    initial_count = len(mini.service.list_sources(OWNER, mini.business.id))
    mini.service.settings = mini.service.settings.model_copy(update={"max_upload_bytes": 256})
    oversized = FormData()
    oversized.add_field("file", b"%PDF-" + b"x" * 300, filename="large.pdf", content_type="application/pdf")
    response = await mini.client.post(mini.prefix + "/pdf", data=oversized)
    assert response.status == 413
    multiple = FormData()
    multiple.add_field("file", b"%PDF-1.7", filename="first.pdf", content_type="application/pdf")
    multiple.add_field("file", b"%PDF-1.7", filename="second.pdf", content_type="application/pdf")
    assert (await mini.client.post(mini.prefix + "/pdf", data=multiple)).status == 400
    assert len(mini.service.list_sources(OWNER, mini.business.id)) == initial_count


async def test_cross_company_post_cannot_be_selected_as_conversation_context(mini):
    post = await prepared_post(mini)
    other = mini.service.create_business(OWNER, "Другой бренд")
    interpreter = AsyncMock(return_value=TaskIntent(action="prepare", request="Пост"))
    mini.service.ai.interpret_request = interpreter
    response = await mini.client.post(f"/mini/api/business/{other.id}/chat", json={
        "text": "Что было в предыдущем посте?", "request_id": "cross-brand", "post_id": post.id,
    })
    assert response.status == 403
    interpreter.assert_not_awaited()


async def test_public_health_has_no_business_data_and_preview_still_has_no_api():
    async with TestClient(TestServer(create_app())) as client:
        response = await client.get("/mini/health")
        assert await response.json() == {"status": "ok", "mode": "preview", "runtime_mode": "preview"}
        assert (await client.get("/mini/api/business/anything/control")).status == 404
