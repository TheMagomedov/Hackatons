"""Real SQLite boundaries; every delivery target is an in-memory fake."""

import asyncio
import io
import threading
from unittest.mock import patch

import pytest
from PIL import Image

from app.config import Settings
from app.core.runtime import build_runtime
from app.safety import NoopSafety, UnavailableSafety
from app.shared.errors import Conflict, SafetyBlocked, SafetyUnavailable
from app.shared.models import ChannelInfo, PostStatus
from tests.core.test_product_controls import prepare
from tests.core.test_product_controls import setup as setup
from tests.core.test_service import OWNER, Clock, FakeAI, FakeImages, FakeNotifier, FakePublisher
from tests.safety.test_wrappers import RecordingSafety


@pytest.fixture
def policy(setup):
    policy = RecordingSafety()
    setup.service.safety = policy
    return policy


def valid_png():
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    return buffer.getvalue()


async def approved_due(setup):
    post = await prepare(setup)
    setup.service.approve_post(OWNER, post.id, post.revision)
    setup.clock.advance(minutes=2)
    return post


async def test_manual_upload_blocked_before_any_public_or_private_asset(setup, policy):
    policy.image_error = SafetyBlocked()
    before = set(setup.service.files.root.iterdir())
    with pytest.raises(SafetyBlocked):
        setup.service.upload_post_image(OWNER, setup.business.id, "picture.png", valid_png())
    assert set(setup.service.files.root.iterdir()) == before
    assert not setup.service.list_posts(OWNER, setup.business.id)


async def test_manual_draft_and_edit_block_before_database_change(setup, policy):
    asset = setup.service.upload_post_image(OWNER, setup.business.id, "picture.png", valid_png())
    policy.text_error = SafetyBlocked()
    with pytest.raises(SafetyBlocked):
        setup.service.create_manual_draft(OWNER, setup.business.id, "caption", asset.id, "request-1")
    assert not setup.service.list_posts(OWNER, setup.business.id)
    policy.text_error = None
    post = await prepare(setup)
    approved = setup.service.approve_post(OWNER, post.id, post.revision)
    policy.text_error = SafetyUnavailable()
    with pytest.raises(SafetyUnavailable):
        setup.service.edit_post(OWNER, post.id, post.revision, text="edited caption")
    assert setup.service.get_post(OWNER, post.id) == approved
    policy.text_error = None
    edited = setup.service.edit_post(OWNER, post.id, post.revision, text="edited caption")
    assert edited.status == PostStatus.REVIEW and edited.approved_revision is None
    assert edited.revision == post.revision + 1
    with pytest.raises(Conflict):
        setup.service.approve_post(OWNER, post.id, post.revision)


@pytest.mark.parametrize("field,error", [("text_error", SafetyBlocked), ("image_error", SafetyBlocked),
                                        ("text_error", SafetyUnavailable), ("image_error", SafetyUnavailable)])
async def test_final_check_failure_never_sends_or_becomes_delivery_unknown(setup, policy, field, error):
    post = await approved_due(setup)
    setattr(policy, field, error())
    assert await setup.service.publish_due_once()
    assert not setup.publisher.calls
    failed = setup.service.get_post(OWNER, post.id)
    assert failed.status == PostStatus.FAILED
    assert not await setup.service.publish_due_once()  # No automatic retry after moderation failure.


async def test_final_checks_review_exact_saved_bytes_and_caption(setup, policy):
    post = await approved_due(setup)
    assert await setup.service.publish_due_once()
    assert policy.texts == [[post.text]]
    assert policy.images == [(setup.images.content, "image/png")]
    assert len(setup.publisher.calls) == 1
    assert setup.service.get_post(OWNER, post.id).status == PostStatus.PUBLISHED


class GatedSafety(RecordingSafety):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def check_text(self, texts):
        self.entered.set()
        if not self.release.wait(5):
            raise SafetyUnavailable()
        super().check_text(texts)


@pytest.mark.parametrize("change", ["pause", "profile", "channel"])
async def test_pause_or_context_change_during_check_prevents_delivery(setup, change):
    post = await approved_due(setup)
    policy = GatedSafety()
    setup.service.safety = policy
    publishing = asyncio.create_task(setup.service.publish_due_once())
    try:
        assert await asyncio.to_thread(policy.entered.wait, 2)
        if change == "pause":
            setup.service.set_publications_paused(OWNER, setup.business.id, True)
        elif change == "profile":
            setup.service.save_profile(OWNER, setup.business.id, setup.business.profile_version, setup.ai.profile)
        else:
            # Simulate context changed by another worker while the external check waits.
            setup.repo.set_channel(setup.business.id, ChannelInfo(id="-100new", title="New"), setup.clock())
    finally:
        policy.release.set()
    assert await publishing
    assert not setup.publisher.calls
    assert setup.service.get_post(OWNER, post.id).status == PostStatus.FAILED


async def test_cancel_during_moderation_is_known_unsent(setup):
    post = await approved_due(setup)
    policy = GatedSafety()
    setup.service.safety = policy
    publishing = asyncio.create_task(setup.service.publish_due_once())
    try:
        assert await asyncio.to_thread(policy.entered.wait, 2)
        publishing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await publishing
    finally:
        policy.release.set()
    assert not setup.publisher.calls
    assert setup.service.get_post(OWNER, post.id).status == PostStatus.FAILED


async def test_live_missing_env_refuses_content_while_demo_is_explicitly_offline(tmp_path, monkeypatch):
    monkeypatch.delenv("MODERATION_API_BASE", raising=False)
    monkeypatch.delenv("MODERATION_API_KEY", raising=False)
    settings = Settings(mode="live", owner_id=OWNER, data_dir=tmp_path / "live",
                        telegram_bot_token="test-only-token", ai_factory="app.fake:build",
                        image_factory="app.fake:build")
    provider, images = FakeAI(Clock()), FakeImages()
    with patch("app.core.runtime._provider", side_effect=[provider, images, None]), \
            patch("app.core.runtime.TelegramPublisher", return_value=FakePublisher()), \
            patch("app.core.runtime.TelegramNotifier", return_value=FakeNotifier()):
        runtime = await build_runtime(settings)
    try:
        assert isinstance(runtime.service.safety, UnavailableSafety)
        with pytest.raises(SafetyUnavailable):
            await runtime.service.ai.interpret_request("hello", {})
        business = runtime.service.create_business(OWNER, "Business")
        with pytest.raises(SafetyUnavailable):
            runtime.service.upload_post_image(OWNER, business.id, "image.png", valid_png())
        assert not list(runtime.service.files.root.iterdir())
    finally:
        await runtime.close()
    demo = await build_runtime(Settings(owner_id=OWNER, data_dir=tmp_path / "demo"))
    try:
        assert isinstance(demo.service.safety, NoopSafety)
    finally:
        await demo.close()
