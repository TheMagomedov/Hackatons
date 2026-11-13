"""The real manual workflow needs Telegram ownership, never a configured AI model."""

from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from app.ai.provider import UnconfiguredAI
from app.config import Settings
from app.core.service import BackendService
from app.media.runtime import UnconfiguredImages
from app.miniapp.server import create_app
from app.shared.errors import ProviderUnavailable
from app.storage import FileStore, Repository
from tests.core.test_service import FakeNotifier, FakePublisher
from tests.test_miniapp import OWNER, TOKEN, signed_data


def image_bytes(format="PNG", size=(24, 24)):
    stream = BytesIO()
    Image.new("RGB", size, "#f69f55").save(stream, format=format)
    return stream.getvalue()


@pytest.fixture
async def manual(tmp_path):
    settings = Settings(mode="live", data_dir=tmp_path, owner_id=OWNER, telegram_bot_token=TOKEN,
                        ai_factory="app.ai.provider:build_provider", image_factory="app.media.runtime:build_provider")
    repo = Repository(tmp_path / "backend.sqlite3")
    repo.initialize()
    repo.bind_runtime("live", OWNER)
    now = [datetime(2026, 11, 13, 10, tzinfo=UTC)]
    publisher = FakePublisher()
    service = BackendService(repo, FileStore(tmp_path / "assets"), settings, UnconfiguredAI(),
                             UnconfiguredImages(), publisher, FakeNotifier(), clock=lambda: now[0])
    try:
        async with TestClient(TestServer(create_app(service, token=TOKEN))) as client:
            client.session.headers.update({"X-Telegram-Init-Data": signed_data(now[0])})
            business = service.create_business(OWNER, "Пекарня на углу")
            yield SimpleNamespace(client=client, service=service, repo=repo, now=now, business=business,
                                  prefix=f"/mini/api/business/{business.id}", publisher=publisher)
    finally:
        repo.close()


async def setup_profile(manual):
    response = await manual.client.post(manual.prefix + "/profile/manual", json={
        "revision": 0, "description": "Печём хлеб каждое утро", "products": ["Багеты", "Круассаны"],
        "audience": "Соседи", "tone": "Тёплый, конкретный", "constraints": ["Не обещать скидки"],
    })
    assert response.status == 200
    business = await response.json()
    assert not business["profile_confirmed"]
    assert (await manual.client.post(manual.prefix + "/profile/confirm", json={
        "revision": business["profile_version"],
    })).ok
    assert (await manual.client.post(manual.prefix + "/channel", json={"value": "-100bakery"})).ok
    return business


async def upload(manual, format="PNG", content=None, business_id=None):
    data = FormData()
    data.add_field("file", image_bytes(format) if content is None else content,
                   filename="photo.jpg" if format == "JPEG" else "picture.png", content_type="application/octet-stream")
    prefix = f"/mini/api/business/{business_id}" if business_id else manual.prefix
    return await manual.client.post(prefix + "/image", data=data)


async def draft(manual, asset_id, **changes):
    payload = {"text": "Свежий хлеб уже на полке", "image_asset_id": asset_id, "request_id": "manual-1", **changes}
    return await manual.client.post(manual.prefix + "/draft", json=payload)


@pytest.mark.parametrize("format", ["PNG", "JPEG"])
async def test_manual_post_complete_cycle_without_ai_and_persistent_deduplication(manual, format):
    business = await setup_profile(manual)
    image = await upload(manual, format)
    assert image.status == 200
    asset = await image.json()
    assert asset["is_demo"] is False
    assert asset["mime_type"] == ("image/png" if format == "PNG" else "image/jpeg")
    when = (manual.now[0] + timedelta(minutes=3)).isoformat()
    changes = {"scheduled_at": when, "source_ids": business["profile"]["facts"][0]["source_ids"]}
    response = await draft(manual, asset["id"], **changes)
    assert response.status == 200
    post = await response.json()
    assert post["status"] == "review" and post["revision"] == 1
    assert post["content_origin"] == "manual" and post["created_by"] == OWNER
    assert post["used_source_ids"] == changes["source_ids"] and post["job_id"] is None
    assert post["approved_revision"] is None and not post["is_demo"]
    assert manual.service.ai_usage(OWNER)["used_today"] == 0
    assert manual.service.list_jobs(OWNER, manual.business.id) == []
    assert await manual.service.publish_due_once() is False
    assert manual.publisher.calls == []

    # Reopening the database keeps the request receipt; another server cannot create a duplicate post.
    reopened = Repository(manual.repo.db_path)
    manual.service.repo = reopened
    try:
        repeated = await (await draft(manual, asset["id"], **changes)).json()
        assert repeated["id"] == post["id"]
        assert len(manual.service.list_posts(OWNER, manual.business.id)) == 1
        assert (await draft(manual, asset["id"], text="Другой текст", **changes)).status == 409
    finally:
        manual.service.repo = manual.repo
        reopened.close()
    notifications = manual.repo.pending_notifications(20, manual.now[0])
    assert len([n for n in notifications if n.post_id == post["id"]]) == 1
    approve = await manual.client.post(f"/mini/api/post/{post['id']}/approve", json={"revision": post["revision"]})
    assert (await approve.json())["status"] == "approved"
    manual.now[0] += timedelta(minutes=4)
    assert await manual.service.publish_due_once() is True
    assert await manual.service.publish_due_once() is False
    assert len(manual.publisher.calls) == 1
    assert manual.service.ai_usage(OWNER)["used_today"] == 0


async def test_manual_draft_requires_explicit_profile_confirmation_and_channel(manual):
    asset = await (await upload(manual)).json()
    assert (await draft(manual, asset["id"])).status == 409
    profile = await (await manual.client.post(manual.prefix + "/profile/manual", json={
        "revision": 0, "description": "Хлеб каждый день",
    })).json()
    assert (await draft(manual, asset["id"])).status == 409
    await manual.client.post(manual.prefix + "/profile/confirm", json={"revision": profile["profile_version"]})
    assert (await draft(manual, asset["id"])).status == 409
    await manual.client.post(manual.prefix + "/channel", json={"value": "-100bakery"})
    assert (await draft(manual, asset["id"])).status == 200


async def test_manual_profile_has_owner_evidence_and_stale_edits_leave_no_source(manual):
    profile = await setup_profile(manual)
    sources = manual.service.list_sources(OWNER, manual.business.id)
    assert len(sources) == 1 and sources[0].kind == "text"
    assert "Печём хлеб" in sources[0].text
    assert {sid for fact in profile["profile"]["facts"] for sid in fact["source_ids"]} == {sources[0].id}
    response = await manual.client.post(manual.prefix + "/profile/manual", json={"revision": 0, "description": "Stale"})
    assert response.status == 409
    assert len(manual.service.list_sources(OWNER, manual.business.id)) == 1
    response = await manual.client.post(manual.prefix + "/profile/manual", json={
        "revision": profile["profile_version"], "description": "Новые сведения владельца",
    })
    assert response.status == 200
    assert (await response.json())["profile_confirmed"] is False
    assert manual.service.list_sources(OWNER, manual.business.id)[0].text == sources[0].text


async def test_manual_profile_write_is_atomic_on_failure(manual):
    audit = manual.repo._audit

    def fail(conn, entity_type, entity_id, action, at, **details):
        if action == "manual_profile_saved":
            raise RuntimeError("Interrupted before commit")
        return audit(conn, entity_type, entity_id, action, at, **details)

    with patch.object(manual.repo, "_audit", side_effect=fail):
        with pytest.raises(RuntimeError):
            manual.service.save_manual_profile(OWNER, manual.business.id, 0, description="Факты владельца")
    assert manual.service.list_sources(OWNER, manual.business.id) == []
    assert manual.service.get_business(OWNER, manual.business.id).profile is None


async def test_manual_image_replacement_revokes_approval_and_cannot_mix_companies(manual):
    await setup_profile(manual)
    one = await (await upload(manual)).json()
    post = await (await draft(manual, one["id"], scheduled_at=(manual.now[0] + timedelta(hours=1)).isoformat())).json()
    prefix = f"/mini/api/post/{post['id']}"
    assert (await manual.client.post(prefix + "/approve", json={"revision": post["revision"]})).ok
    two = await (await upload(manual, "JPEG")).json()
    changed = await (await manual.client.post(prefix + "/edit", json={
        "revision": post["revision"], "image_asset_id": two["id"], "text": "Новый текст владельца",
    })).json()
    assert changed["status"] == "review" and changed["approved_revision"] is None
    assert changed["content_origin"] == "manual" and changed["created_by"] == OWNER
    assert (await manual.client.post(prefix + "/approve", json={"revision": post["revision"]})).status == 409
    other = manual.service.create_business(OWNER, "Другой бизнес")
    foreign_image = await (await upload(manual, business_id=other.id)).json()
    assert (await draft(manual, foreign_image["id"], request_id="foreign-image")).status == 400
    foreign_source = manual.service.add_text_source(OWNER, other.id, "Чужой секретный факт")
    assert (await draft(manual, two["id"], request_id="foreign-source", source_ids=[foreign_source.id])).status == 400
    assert len(manual.service.list_posts(OWNER, manual.business.id)) == 1


async def test_bad_images_and_huge_dimensions_never_reach_storage(manual):
    initial = len(list(manual.service.files.root.glob("*.blob")))
    for content in (b"not an image", image_bytes()[:-20], image_bytes(size=(4097, 16)),
                    image_bytes(size=(1000, 1)), b"<svg>fake</svg>"):
        response = await upload(manual, content=content)
        assert response.status == 400
    frames = BytesIO()
    first = Image.new("RGB", (24, 24), "red")
    first.save(frames, format="PNG", save_all=True, append_images=[Image.new("RGB", (24, 24), "blue")], duration=100)
    assert (await upload(manual, content=frames.getvalue())).status == 400
    assert len(list(manual.service.files.root.glob("*.blob"))) == initial


async def test_manual_routes_deny_wrong_signed_owner_and_bound_upload_size(manual):
    headers = {"X-Telegram-Init-Data": signed_data(manual.now[0], actor=OWNER + 1)}
    assert (await manual.client.post(manual.prefix + "/profile/manual", json={"revision": 0, "description": "x"},
                                     headers=headers)).status == 403
    assert (await manual.client.post(manual.prefix + "/image", data=b"bad", headers=headers)).status == 403
    assert (await manual.client.post(manual.prefix + "/draft", json={}, headers=headers)).status == 403
    manual.service.settings = manual.service.settings.model_copy(update={"max_upload_bytes": 64})
    assert (await upload(manual)).status == 413


async def test_capabilities_are_explicitly_unverified_and_manual_flow_does_not_call_ai(manual):
    response = await manual.client.get("/mini/api/state")
    state = await response.json()
    assert state["runtime_mode"] == "live"
    assert state["capabilities"]["text_generation"] == "unconfigured"
    assert state["capabilities"]["image_generation"] == "unconfigured"
    assert state["capabilities"]["external_access_checked"] is False
    with pytest.raises(ProviderUnavailable):
        await manual.service.ai.build_profile([])
    await setup_profile(manual)
    asset = await (await upload(manual)).json()
    assert (await draft(manual, asset["id"])).ok
    assert manual.service.ai_usage(OWNER)["used_today"] == 0


async def test_calendar_respects_business_timezone_and_search_filters_real_posts(manual):
    await setup_profile(manual)
    asset = await (await upload(manual)).json()
    await draft(manual, asset["id"], scheduled_at="2026-11-30T22:30:00+00:00")  # Dec 1 in Moscow.
    await draft(manual, asset["id"], request_id="unscheduled", text="Круассаны с утра")
    november = await (await manual.client.get(manual.prefix + "/calendar?month=2026-11")).json()
    december = await (await manual.client.get(manual.prefix + "/calendar?month=2026-12")).json()
    assert november["items"] == [] and november["unscheduled_count"] == 1
    assert december["timezone"] == "Europe/Moscow" and len(december["items"]) == 1
    assert (await manual.client.get(manual.prefix + "/calendar?month=2026-13")).status == 400
    search = await (await manual.client.get(manual.prefix + "/posts", params={"search": "КРУАССАНЫ"})).json()
    assert len(search["items"]) == 1 and search["items"][0]["text"] == "Круассаны с утра"


async def test_manual_draft_rejects_past_time_and_tampered_stored_image(manual):
    await setup_profile(manual)
    asset = await (await upload(manual)).json()
    assert (await draft(manual, asset["id"], scheduled_at=manual.now[0].isoformat())).status == 400
    assert manual.service.list_posts(OWNER, manual.business.id) == []
    path = manual.service.files.root / f"{asset['id']}.blob"
    content = bytearray(path.read_bytes())
    content[-1] ^= 1
    path.write_bytes(content)
    assert (await draft(manual, asset["id"])).status == 409
    assert manual.service.list_posts(OWNER, manual.business.id) == []


async def test_demo_image_cannot_enter_live_manual_creation_or_replacement(manual):
    await setup_profile(manual)
    asset = await (await upload(manual)).json()
    post = await (await draft(manual, asset["id"])).json()
    demonstration = manual.service.files.write(manual.business.id, "demo.png", image_bytes(), "image/png",
                                              manual.now[0], is_demo=True)
    manual.repo.add_asset(demonstration)
    assert (await draft(manual, demonstration.id, request_id="demo-file")).status == 400
    assert (await manual.client.post(f"/mini/api/post/{post['id']}/edit", json={
        "revision": post["revision"], "image_asset_id": demonstration.id,
    })).status == 400
    current = manual.service.get_post(OWNER, post["id"])
    assert current.revision == post["revision"] and current.image_asset_id == asset["id"]
