"""User-facing controls against real SQLite, with gated external calls."""

import asyncio
import hashlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.core.service import BackendService
from app.shared.errors import Conflict, Forbidden, InvalidInput, ProviderUnavailable
from app.shared.models import ContentPlan, PlanItem, PostStatus, TextDraft
from app.storage import FileStore, Repository
from tests.core.test_service import OWNER, Clock, FakeAI, FakeImages, FakeNotifier, FakePublisher, Gate


@pytest.fixture
async def setup(tmp_path):
    clock = Clock()
    repository = Repository(tmp_path / "backend.sqlite3")
    repository.initialize()
    ai, images, publisher, notifier = FakeAI(clock), FakeImages(), FakePublisher(), FakeNotifier()
    settings = Settings(owner_id=OWNER, data_dir=tmp_path)
    service = BackendService(
        repository, FileStore(tmp_path / "assets"), settings, ai, images, publisher, notifier, clock=clock
    )
    business = service.create_business(OWNER, "Bakery")
    service.save_profile(OWNER, business.id, 0, ai.profile)
    service.confirm_profile(OWNER, business.id, 1)
    business = await service.connect_channel(OWNER, business.id, "-100bakery")
    yield SimpleNamespace(
        service=service,
        repo=repository,
        ai=ai,
        images=images,
        publisher=publisher,
        notifier=notifier,
        clock=clock,
        business=business,
    )
    repository.close()


async def prepare(fixture):
    job = fixture.service.request_preparation(OWNER, fixture.business.id, "One post", "prepare")
    assert await fixture.service.prepare_next_job()
    assert fixture.repo.get_job(job.id).status == "done"
    return fixture.service.get_job_posts(OWNER, job.id)[0]


async def test_generation_and_revision_keep_provenance_and_profile_review(setup):
    s = setup.service
    source = s.add_text_source(OWNER, setup.business.id, "Verified opening hours")
    business = s.get_business(OWNER, setup.business.id)
    s.confirm_profile(OWNER, business.id, business.profile_version)
    setup.ai.write_post = AsyncMock(
        return_value=TextDraft(
            text="Open today.", image_brief="Bakery", used_source_ids=[source.id], warnings=["Check hours"]
        )
    )
    post = await prepare(setup)
    assert post.used_source_ids == [source.id]
    assert post.warnings == ["Check hours"]
    assert post.profile_version == business.profile_version

    changed = s.save_profile(
        OWNER,
        business.id,
        business.profile_version,
        business.profile.model_copy(update={"description": "Changed hours"}),
    )
    s.confirm_profile(OWNER, business.id, changed.profile_version)
    scheduled = s.edit_post(OWNER, post.id, post.revision, scheduled_at=setup.clock() + timedelta(minutes=5))
    assert scheduled.profile_version == post.profile_version
    with pytest.raises(Conflict):
        s.approve_post(OWNER, post.id, scheduled.revision)
    reviewed = s.refresh_post_context(OWNER, post.id, scheduled.revision)
    assert reviewed.status == PostStatus.REVIEW
    assert reviewed.approved_revision is None
    assert reviewed.warnings == ["Check hours"]
    assert reviewed.used_source_ids == [source.id]
    assert reviewed.profile_version == changed.profile_version
    s.approve_post(OWNER, post.id, reviewed.revision)


async def test_ai_text_revision_replaces_sources_and_warnings_and_replay_is_free(setup):
    post = await prepare(setup)
    setup.ai.revise_text = AsyncMock(
        return_value=TextDraft(text="Updated.", image_brief="Bakery", warnings=["Confirm availability"])
    )
    updated = await setup.service.revise_text(
        OWNER, post.id, post.revision, "Shorter", request_id="telegram_update:2"
    )
    repeated = await setup.service.revise_text(
        OWNER, post.id, post.revision, "Shorter", request_id="telegram_update:2"
    )
    assert repeated == updated
    assert updated.warnings == ["Confirm availability"]
    assert setup.ai.revise_text.await_count == 1
    assert setup.service.ai_usage(OWNER)["used_today"] == 4
    # Restarted UI might route the same event differently; it must still not pay twice.
    with pytest.raises(Conflict):
        await setup.service.interpret_request(
            OWNER, setup.business.id, "Shorter", request_id="telegram_update:2"
        )


async def test_analyze_replay_does_not_increment_profile_again(setup):
    source = setup.service.add_text_source(OWNER, setup.business.id, "Bread")
    setup.ai.build_profile = AsyncMock(return_value=setup.ai.profile)
    analyzed = await setup.service.analyze_business(OWNER, source.business_id, request_id="event:1")
    again = await setup.service.analyze_business(OWNER, source.business_id, request_id="event:1")
    assert again.profile_version == analyzed.profile_version
    assert setup.ai.build_profile.await_count == 1


async def test_daily_owner_limit_covers_intents_analysis_and_edits(setup):
    s = setup.service
    s.settings = s.settings.model_copy(update={"max_ai_operations_per_day": 4})
    post = await prepare(setup)  # plan, text, image
    setup.ai.interpret_request = AsyncMock(return_value=setup.ai.intent)
    first = await s.interpret_request(OWNER, setup.business.id, "Next", request_id="event:intent")
    assert await s.interpret_request(OWNER, setup.business.id, "Next", request_id="event:intent") == first
    with pytest.raises(Conflict, match="лимит"):
        await s.revise_image(OWNER, post.id, post.revision, "Different", request_id="event:edit")
    with pytest.raises(Conflict, match="лимит"):
        await s.analyze_business(OWNER, setup.business.id, request_id="event:analyze")
    second = s.create_business(OWNER, "Other")
    with pytest.raises(Conflict, match="лимит"):
        await s.interpret_request(OWNER, second.id, "Hello", request_id="event:other")
    assert s.get_post(OWNER, post.id).revision == post.revision
    assert s.ai_usage(OWNER)["used_today"] == 4
    setup.clock.advance(days=1)
    await s.interpret_request(OWNER, second.id, "Hello", request_id="event:tomorrow")
    assert s.ai_usage(OWNER)["used_today"] == 1


async def test_concurrent_duplicate_and_cancelled_operation_never_repeat_provider(setup):
    s = setup.service
    s.settings = s.settings.model_copy(update={"max_concurrent_ai_operations": 1})
    gate = Gate()

    async def slow(*args):
        await gate.wait()
        return setup.ai.intent

    setup.ai.interpret_request = AsyncMock(side_effect=slow)
    task = asyncio.create_task(s.interpret_request(OWNER, setup.business.id, "slow", request_id="event:slow"))
    await asyncio.wait_for(gate.entered.wait(), 1)
    queued = s.request_preparation(OWNER, setup.business.id, "Queued behind AI", "busy-queue")
    assert not await s.prepare_next_job()
    assert s.get_job(OWNER, queued.id).status == "pending"
    for key in ("event:slow", "event:parallel"):
        with pytest.raises(Conflict):
            await s.interpret_request(OWNER, setup.business.id, "slow", request_id=key)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(Conflict):
        await s.interpret_request(OWNER, setup.business.id, "slow", request_id="event:slow")
    assert setup.ai.interpret_request.await_count == 1
    usage = s.ai_usage(OWNER)
    assert usage["running"] == 0 and usage["failed_today"] == 1


async def test_failed_operation_is_safe_visible_and_not_automatically_retried(setup):
    s = setup.service
    marker = "secret-provider-payload"
    setup.ai.interpret_request = AsyncMock(side_effect=RuntimeError(marker))
    with pytest.raises(ProviderUnavailable) as error:
        await s.interpret_request(OWNER, setup.business.id, marker, request_id="event:failed")
    assert marker not in str(error.value)
    rows = s.list_ai_operations(OWNER, setup.business.id)
    assert len(rows) == 1 and rows[0]["error_code"] == "provider_unavailable"
    assert marker not in str(rows)
    with pytest.raises(Conflict):
        await s.interpret_request(OWNER, setup.business.id, marker, request_id="event:failed")
    assert setup.ai.interpret_request.await_count == 1


async def test_pending_cancel_and_running_job_progress(setup):
    s = setup.service
    cancelled = s.request_preparation(OWNER, setup.business.id, "Cancel", "cancel")
    assert s.cancel_job(OWNER, cancelled.id).status == "cancelled"
    assert s.cancel_job(OWNER, cancelled.id).status == "cancelled"
    assert not await s.prepare_next_job()
    assert s.ai_usage(OWNER)["used_today"] == 0
    job = s.request_preparation(OWNER, setup.business.id, "Run", "run")
    gate = setup.images.gate = Gate()
    task = asyncio.create_task(s.prepare_next_job())
    await asyncio.wait_for(gate.entered.wait(), 1)
    progress = s.get_job(OWNER, job.id)
    assert (progress.stage, progress.processed, progress.total) == ("image", 2, 3)
    assert progress.updated_at is not None
    with pytest.raises(Conflict):
        s.cancel_job(OWNER, job.id)
    gate.release.set()
    assert await task
    done = s.get_job(OWNER, job.id)
    assert done.status == "done" and done.processed == done.total == 3


async def test_five_posts_progress_and_quota_count_every_provider_stage(setup):
    s = setup.service
    s.settings = s.settings.model_copy(update={"max_posts_per_job": 5})
    setup.ai.make_plan = AsyncMock(
        return_value=ContentPlan(
            items=[
                PlanItem(topic=f"Post {i}", goal="Tell", brief="Bread", image_brief="Bread") for i in range(5)
            ]
        )
    )
    job = s.request_preparation(OWNER, setup.business.id, "Five posts", "five")
    assert await s.prepare_next_job()
    result = s.get_job(OWNER, job.id)
    assert result.status == "done" and result.total == result.processed == 11
    assert len(s.get_job_posts(OWNER, job.id)) == 5
    assert s.ai_usage(OWNER)["used_today"] == 11


async def test_paused_publications_wait_without_changing_approval_or_paying(setup):
    s = setup.service
    post = await prepare(setup)
    approved = s.approve_post(OWNER, post.id, post.revision)
    s.set_publications_paused(OWNER, setup.business.id, True)
    setup.clock.advance(minutes=2)
    assert not await s.publish_due_once()
    assert s.get_post(OWNER, post.id) == approved
    assert not setup.publisher.calls
    s.set_publications_paused(OWNER, setup.business.id, False)
    assert await s.publish_due_once()
    assert len(setup.publisher.calls) == 1
    assert s.ai_usage(OWNER)["used_today"] == 3


async def test_resume_after_lateness_needs_new_approval(setup):
    s = setup.service
    post = await prepare(setup)
    s.approve_post(OWNER, post.id, post.revision)
    s.set_publications_paused(OWNER, setup.business.id, True)
    setup.clock.advance(days=1)
    s.set_publications_paused(OWNER, setup.business.id, False)
    assert await s.publish_due_once()
    assert s.get_post(OWNER, post.id).status == PostStatus.FAILED
    assert not setup.publisher.calls


async def test_manual_notification_retry_does_not_repeat_business_operation(setup):
    s = setup.service
    post = await prepare(setup)
    notification = setup.repo.pending_notifications(20, now=setup.clock())[0]
    for _ in range(5):
        setup.repo.record_notification_failure(notification.id, setup.clock())
    failed = s.failed_notifications(OWNER, setup.business.id)
    assert [item["id"] for item in failed] == [notification.id]
    assert s.get_failed_notification(OWNER, notification.id)["business_id"] == setup.business.id
    s.retry_notification(OWNER, setup.business.id, notification.id)
    assert await s.dispatch_notifications() == 1
    assert s.get_post(OWNER, post.id) == post
    assert s.ai_usage(OWNER)["used_today"] == 3
    assert not setup.publisher.calls


async def test_controls_require_owner_authorization(setup):
    s = setup.service
    job = s.request_preparation(OWNER, setup.business.id, "Queued", "auth")
    operations = [
        lambda: s.cancel_job(2, job.id),
        lambda: s.list_jobs(2, setup.business.id),
        lambda: s.set_publications_paused(2, setup.business.id, True),
        lambda: s.ai_usage(2),
        lambda: s.list_ai_operations(2),
        lambda: s.failed_notifications(2, setup.business.id),
        lambda: s.get_failed_notification(2, "missing"),
        lambda: s.retry_notification(2, setup.business.id, "missing"),
        lambda: s.accept_telegram_update(2, "telegram:123:update:1", "a" * 64),
    ]
    for operation in operations:
        with pytest.raises(Forbidden):
            operation()


async def test_inbound_receipt_is_durable_before_any_paid_action(setup):
    s = setup.service
    digest = hashlib.sha256(b"source text event").hexdigest()
    event_key = "telegram:123:update:1"
    assert s.accept_telegram_update(OWNER, event_key, digest)
    reopened = BackendService(
        Repository(setup.repo.db_path),
        s.files,
        s.settings,
        s.ai,
        s.images,
        s.publisher,
        s.notifier,
        clock=s.clock,
    )
    assert not reopened.accept_telegram_update(OWNER, event_key, digest)
    assert reopened.ai_usage(OWNER)["used_today"] == 0
    with pytest.raises(Conflict):
        reopened.accept_telegram_update(OWNER, event_key, "0" * 64)
    # Another bot's update number is a different event, even for the same owner.
    assert reopened.accept_telegram_update(OWNER, "telegram:456:update:1", digest)


async def test_source_limits_before_loading_pdf_and_before_new_upload(setup):
    s = setup.service
    content = b"%PDF-" + b"x" * 600
    s.add_pdf_source(OWNER, setup.business.id, "first.pdf", content)
    s.add_pdf_source(OWNER, setup.business.id, "second.pdf", content)
    s.settings = s.settings.model_copy(update={"max_source_bytes_total": 1024})
    with patch.object(s.files, "read") as read:
        with pytest.raises(InvalidInput, match="объём"):
            await s.analyze_business(OWNER, setup.business.id, request_id="too-big")
        read.assert_not_called()
    with patch.object(s.files, "write") as write:
        with pytest.raises(InvalidInput):
            s.add_pdf_source(OWNER, setup.business.id, "third.pdf", content)
        write.assert_not_called()
    s.settings = s.settings.model_copy(
        update={"max_sources_per_business": 2, "max_source_bytes_total": 10000}
    )
    with pytest.raises(InvalidInput, match="источников"):
        s.add_url_source(OWNER, setup.business.id, "https://example.org")
