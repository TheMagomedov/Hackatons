"""Real SQLite and files; only external providers are deterministic fakes."""

from __future__ import annotations

import asyncio
import base64
import sqlite3
import tempfile
import traceback
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app.config import Settings
from app.core.service import BackendService
from app.shared.errors import Conflict, DeliveryRejected, Forbidden, InvalidInput, ProviderUnavailable
from app.shared.models import (
    BusinessProfile,
    ChannelInfo,
    ContentPlan,
    Fact,
    GeneratedImage,
    PlanItem,
    PostStatus,
    PublicationResult,
    Source,
    TaskIntent,
    TextDraft,
)
from app.storage import FileStore, Repository

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1EAAAAASUVORK5CYII="
)
OWNER = 1001


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 9, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


class Gate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self) -> None:
        self.entered.set()
        await self.release.wait()


class FakeAI:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.profile = BusinessProfile(name="Bakery", description="Bread and coffee")
        self.gates: dict[str, Gate] = {}
        self.profile_inputs = []
        self.used_source_ids: list[str] = []
        self.suggested_delta = timedelta(minutes=1)
        self.intent: object = TaskIntent(action="prepare", request="One post")

    async def _gate(self, key: str) -> None:
        if key in self.gates:
            await self.gates[key].wait()

    async def build_profile(self, sources):
        self.profile_inputs = sources
        await self._gate("profile")
        return self.profile

    async def interpret_request(self, text, context):
        return self.intent

    async def make_plan(self, profile, request):
        await self._gate("plan")
        return ContentPlan(
            items=[
                PlanItem(
                    topic="Bread",
                    goal="Tell customers",
                    brief="Fresh bread",
                    image_brief="A loaf on a plate",
                    suggested_at=self.clock() + self.suggested_delta,
                )
            ]
        )

    async def write_post(self, profile, item):
        await self._gate("write")
        return TextDraft(
            text="Fresh bread today.", image_brief=item.image_brief, used_source_ids=self.used_source_ids
        )

    async def revise_text(self, profile, current_text, feedback):
        await self._gate("revision")
        return TextDraft(text="AI revised text.", image_brief="A loaf on a plate")


class FakeImages:
    def __init__(self) -> None:
        self.content = PNG
        self.is_demo = False
        self.gate: Gate | None = None

    async def generate(self, profile, text, feedback=None):
        if self.gate:
            await self.gate.wait()
        return GeneratedImage(
            content=self.content, mime_type="image/png", provider="test", is_demo=self.is_demo
        )


class FakePublisher:
    def __init__(self) -> None:
        self.calls = []
        self.gate: Gate | None = None
        self.error: Exception | None = None

    async def verify_channel(self, channel_id, owner_id):
        return ChannelInfo(id=channel_id, title="Test channel")

    async def publish(self, post, image, mime_type):
        self.calls.append((post, image, mime_type))
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        return PublicationResult(channel_id=post.channel_id, message_id=len(self.calls))

    async def subscriber_count(self, channel_id):
        return 42

    async def close(self):
        pass


class FakeNotifier:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, notification):
        self.sent.append(notification)


class BackendServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="marketer-core-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = Repository(self.root / "state.sqlite3")
        self.repo.initialize()
        self.files = FileStore(self.root / "assets")
        self.clock = Clock()
        self.ai = FakeAI(self.clock)
        self.images = FakeImages()
        self.publisher = FakePublisher()
        self.notifier = FakeNotifier()
        self.settings = Settings(owner_id=OWNER, data_dir=self.root, provider_timeout_seconds=2)
        self.service = self.new_service()
        self.business = await self.ready_business("Bakery")
        self.request_counter = 0

    def new_service(self, repository: Repository | None = None) -> BackendService:
        return BackendService(
            repository or self.repo,
            self.files,
            self.settings,
            self.ai,
            self.images,
            self.publisher,
            self.notifier,
            clock=self.clock,
        )

    async def ready_business(self, name):
        business = self.service.create_business(OWNER, name)
        business = self.service.save_profile(OWNER, business.id, 0, self.ai.profile)
        self.service.confirm_profile(OWNER, business.id, business.profile_version)
        return await self.service.connect_channel(OWNER, business.id, f"-100{name}")

    async def prepare_post(self):
        self.request_counter += 1
        job = self.service.request_preparation(
            OWNER, self.business.id, "Prepare a post", f"request-{self.request_counter}"
        )
        self.assertTrue(await self.service.prepare_next_job())
        self.assertEqual(self.repo.get_job(job.id).status, "done")
        return self.service.list_posts(OWNER, self.business.id)[-1]

    async def due_post(self):
        post = await self.prepare_post()
        self.service.approve_post(OWNER, post.id, post.revision)
        self.clock.advance(minutes=2)
        return self.repo.get_post(post.id)

    async def test_complete_cycle_requires_explicit_approval_and_publishes_once(self):
        post = await self.prepare_post()
        self.assertEqual(post.status, PostStatus.REVIEW)
        self.assertIsNone(post.approved_revision)
        self.assertFalse(await self.service.publish_due_once())
        self.service.approve_post(OWNER, post.id, post.revision)
        self.assertFalse(await self.service.publish_due_once())
        self.clock.advance(minutes=2)
        self.assertTrue(await self.service.publish_due_once())
        self.assertFalse(await self.service.publish_due_once())
        saved = self.repo.get_post(post.id)
        self.assertEqual(saved.status, PostStatus.PUBLISHED)
        self.assertEqual(saved.message_id, 1)
        self.assertEqual(len(self.publisher.calls), 1)
        self.assertEqual(self.publisher.calls[0][1], PNG)

    async def test_unauthorized_actor_cannot_read_or_mutate_business_content(self):
        post = await self.prepare_post()
        source = self.service.add_pdf_source(OWNER, self.business.id, "brief.pdf", b"%PDF-1.7 test")
        operations = (
            lambda: self.service.list_businesses(999),
            lambda: self.service.get_business(999, self.business.id),
            lambda: self.service.get_post(999, post.id),
            lambda: self.service.read_asset(999, source.asset_id),
            lambda: self.service.add_text_source(999, self.business.id, "secret"),
            lambda: self.service.request_preparation(999, self.business.id, "task", "id"),
            lambda: self.service.edit_post(999, post.id, post.revision, text="changed"),
            lambda: self.service.approve_post(999, post.id, post.revision),
            lambda: self.service.reject_post(999, post.id, post.revision),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(Forbidden):
                    operation()
        with self.assertRaises(Forbidden):
            await self.service.revise_text(999, post.id, post.revision, "change")
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.REVIEW)

    async def test_owner_cannot_access_a_business_owned_by_another_actor(self):
        foreign = self.repo.create_business(999, "Foreign", "UTC", self.clock())
        with self.assertRaises(Forbidden):
            self.service.get_business(OWNER, foreign.id)
        self.assertNotIn(foreign.id, [b.id for b in self.service.list_businesses(OWNER)])

    async def test_request_dedup_survives_restart_and_limits_do_not_reject_duplicate(self):
        self.settings = self.settings.model_copy(update={"max_generation_jobs_per_day": 1})
        self.service = self.new_service()
        first = self.service.request_preparation(OWNER, self.business.id, "task", "update-1")
        restarted_repo = Repository(self.root / "state.sqlite3")
        restarted_repo.initialize()
        restarted = self.new_service(restarted_repo)
        self.assertEqual(
            restarted.request_preparation(OWNER, self.business.id, "task", "update-1").id, first.id
        )
        with self.assertRaises(Conflict):
            restarted.request_preparation(OWNER, self.business.id, "different task", "update-1")
        with self.assertRaises(Conflict):
            restarted.request_preparation(OWNER, self.business.id, "task", "update-2")
        self.clock.advance(days=1)
        second = restarted.request_preparation(OWNER, self.business.id, "task", "update-2")
        self.assertNotEqual(first.id, second.id)

    async def test_pending_limit_counts_running_generation(self):
        self.settings = self.settings.model_copy(update={"max_pending_jobs": 1})
        self.service = self.new_service()
        job = self.service.request_preparation(OWNER, self.business.id, "task", "one")
        self.repo.claim_job(self.clock())
        self.assertEqual(self.repo.get_job(job.id).status, "running")
        with self.assertRaises(Conflict):
            self.service.request_preparation(OWNER, self.business.id, "task", "two")

    async def test_daily_limit_resets_at_business_local_midnight(self):
        self.settings = self.settings.model_copy(update={"max_generation_jobs_per_day": 1})
        self.service = self.new_service()
        # Europe/Moscow midnight is 21:00 UTC, not the UTC calendar boundary.
        self.clock.value = datetime(2026, 11, 9, 20, 59, tzinfo=UTC)
        self.service.request_preparation(OWNER, self.business.id, "task", "before-midnight")
        with self.assertRaises(Conflict):
            self.service.request_preparation(OWNER, self.business.id, "task", "same-day")
        self.clock.advance(minutes=1)
        after = self.service.request_preparation(OWNER, self.business.id, "task", "after-midnight")
        self.assertEqual(after.status, "pending")

    async def test_stale_profile_analysis_does_not_overwrite_manual_correction(self):
        self.service.add_text_source(OWNER, self.business.id, "Business description")
        current_version = self.repo.get_business(self.business.id).profile_version
        gate = self.ai.gates["profile"] = Gate()
        task = asyncio.create_task(self.service.analyze_business(OWNER, self.business.id))
        await asyncio.wait_for(gate.entered.wait(), 1)
        corrected = BusinessProfile(name="Corrected", description="Owner correction")
        saved = self.service.save_profile(OWNER, self.business.id, current_version, corrected)
        gate.release.set()
        with self.assertRaises(Conflict):
            await task
        self.assertEqual(self.repo.get_business(self.business.id).profile, corrected)
        self.assertEqual(self.repo.get_business(self.business.id).profile_version, saved.profile_version)

    async def test_new_source_during_first_profile_analysis_rejects_stale_result(self):
        business = self.service.create_business(OWNER, "Initial profile")
        self.service.add_text_source(OWNER, business.id, "First description")
        gate = self.ai.gates["profile"] = Gate()
        task = asyncio.create_task(self.service.analyze_business(OWNER, business.id))
        await asyncio.wait_for(gate.entered.wait(), 1)
        self.service.add_text_source(OWNER, business.id, "New information added during analysis")
        gate.release.set()
        with self.assertRaises(Conflict):
            await task
        current = self.repo.get_business(business.id)
        self.assertIsNone(current.profile)
        self.assertFalse(current.profile_confirmed)
        self.assertEqual(len(self.repo.list_sources(business.id)), 2)

    async def test_profile_change_during_generation_fails_job_without_posts(self):
        job = self.service.request_preparation(OWNER, self.business.id, "task", "profile-race")
        gate = self.ai.gates["write"] = Gate()
        task = asyncio.create_task(self.service.prepare_next_job())
        await asyncio.wait_for(gate.entered.wait(), 1)
        self.service.save_profile(
            OWNER,
            self.business.id,
            self.business.profile_version,
            BusinessProfile(name="Changed", description="New facts"),
        )
        gate.release.set()
        await task
        self.assertEqual(self.repo.get_job(job.id).status, "failed")
        self.assertEqual(self.repo.list_posts(self.business.id), [])

    async def test_stale_ai_text_revision_cannot_overwrite_manual_edit(self):
        post = await self.prepare_post()
        gate = self.ai.gates["revision"] = Gate()
        task = asyncio.create_task(self.service.revise_text(OWNER, post.id, post.revision, "shorten"))
        await asyncio.wait_for(gate.entered.wait(), 1)
        manual = self.service.edit_post(OWNER, post.id, post.revision, text="Owner's final text.")
        gate.release.set()
        with self.assertRaises(Conflict):
            await task
        self.assertEqual(self.repo.get_post(post.id).text, manual.text)
        self.assertEqual(self.repo.get_post(post.id).revision, manual.revision)

    async def test_stale_generated_image_cannot_replace_new_revision(self):
        post = await self.prepare_post()
        gate = self.images.gate = Gate()
        task = asyncio.create_task(self.service.revise_image(OWNER, post.id, post.revision, "warmer"))
        await asyncio.wait_for(gate.entered.wait(), 1)
        manual = self.service.edit_post(OWNER, post.id, post.revision, text="Final owner text.")
        gate.release.set()
        with self.assertRaises(Conflict):
            await task
        saved = self.repo.get_post(post.id)
        self.assertEqual(saved.revision, manual.revision)
        self.assertEqual(saved.image_asset_id, post.image_asset_id)

    async def test_channel_change_during_generation_discards_old_destination(self):
        job = self.service.request_preparation(OWNER, self.business.id, "task", "channel-race")
        gate = self.ai.gates["write"] = Gate()
        task = asyncio.create_task(self.service.prepare_next_job())
        await asyncio.wait_for(gate.entered.wait(), 1)
        await self.service.connect_channel(OWNER, self.business.id, "-100NEW")
        gate.release.set()
        await task
        self.assertEqual(self.repo.get_job(job.id).status, "failed")
        self.assertEqual(self.repo.list_posts(self.business.id), [])

    async def test_delayed_channel_verification_rechecks_current_approved_posts(self):
        original_channel = self.business.channel_id
        gate = Gate()

        async def delayed_verify(channel_id, owner_id):
            if channel_id == original_channel:
                await gate.wait()
            return ChannelInfo(id=channel_id, title="Verified channel")

        with patch.object(self.publisher, "verify_channel", side_effect=delayed_verify):
            delayed = asyncio.create_task(
                self.service.connect_channel(OWNER, self.business.id, original_channel)
            )
            await asyncio.wait_for(gate.entered.wait(), 1)
            await self.service.connect_channel(OWNER, self.business.id, "-100NEW")
            post = await self.prepare_post()
            self.service.approve_post(OWNER, post.id, post.revision)
            gate.release.set()
            with self.assertRaises(Conflict):
                await delayed
        self.assertEqual(self.repo.get_business(self.business.id).channel_id, "-100NEW")
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.APPROVED)

    async def test_profile_change_during_text_revision_rejects_stale_result(self):
        post = await self.prepare_post()
        gate = self.ai.gates["revision"] = Gate()
        task = asyncio.create_task(self.service.revise_text(OWNER, post.id, post.revision, "shorten"))
        await asyncio.wait_for(gate.entered.wait(), 1)
        self.service.save_profile(
            OWNER,
            self.business.id,
            self.business.profile_version,
            BusinessProfile(name="Changed", description="New facts"),
        )
        gate.release.set()
        with self.assertRaises(Conflict):
            await task
        self.assertEqual(self.repo.get_post(post.id).text, post.text)

    async def test_edit_invalidates_approval_and_old_callback(self):
        post = await self.prepare_post()
        self.service.approve_post(OWNER, post.id, post.revision)
        edited = self.service.edit_post(OWNER, post.id, post.revision, text="Updated text.")
        self.assertEqual(edited.status, PostStatus.REVIEW)
        self.assertIsNone(edited.approved_revision)
        with self.assertRaises(Conflict):
            self.service.approve_post(OWNER, post.id, post.revision)
        self.clock.advance(minutes=2)
        self.assertFalse(await self.service.publish_due_once())
        self.assertEqual(len(self.publisher.calls), 0)

    async def test_delayed_approval_requires_new_future_time(self):
        post = await self.prepare_post()
        self.clock.advance(minutes=2)
        with self.assertRaises(InvalidInput):
            self.service.approve_post(OWNER, post.id, post.revision)
        with self.assertRaises(InvalidInput):
            self.service.edit_post(OWNER, post.id, post.revision, scheduled_at=self.clock())
        edited = self.service.edit_post(
            OWNER, post.id, post.revision, scheduled_at=self.clock() + timedelta(minutes=1)
        )
        approved = self.service.approve_post(OWNER, post.id, edited.revision)
        self.assertEqual(approved.status, PostStatus.APPROVED)

    async def test_overdue_approved_post_requires_new_time_and_approval(self):
        post = await self.due_post()
        self.clock.advance(minutes=16)
        self.assertTrue(await self.service.publish_due_once())
        self.assertEqual(self.publisher.calls, [])
        missed = self.repo.get_post(post.id)
        self.assertEqual(missed.status, PostStatus.FAILED)
        with self.assertRaises(Conflict):
            self.service.approve_post(OWNER, post.id, missed.revision)
        self.assertTrue(
            any(n.kind == "missed_time" for n in self.repo.pending_notifications(100, self.clock()))
        )
        rescheduled = self.service.edit_post(
            OWNER, post.id, missed.revision, scheduled_at=self.clock() + timedelta(minutes=1)
        )
        self.assertEqual(rescheduled.status, PostStatus.REVIEW)
        self.assertIsNone(rescheduled.approved_revision)
        self.service.approve_post(OWNER, post.id, rescheduled.revision)
        self.clock.advance(minutes=1)
        self.assertTrue(await self.service.publish_due_once())
        self.assertEqual(len(self.publisher.calls), 1)
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.PUBLISHED)

    async def test_short_restart_downtime_preserves_valid_approval(self):
        post = await self.due_post()
        restarted_repo = Repository(self.root / "state.sqlite3")
        restarted_repo.initialize()
        restarted = self.new_service(restarted_repo)
        self.assertEqual(restarted.recover_interrupted()["posts"], 0)
        self.assertTrue(await restarted.publish_due_once())
        self.assertEqual(restarted_repo.get_post(post.id).status, PostStatus.PUBLISHED)
        self.assertFalse(await restarted.publish_due_once())
        self.assertEqual(len(self.publisher.calls), 1)

    async def test_ai_past_suggestion_remains_unscheduled_and_unapproved(self):
        self.ai.suggested_delta = timedelta(minutes=-1)
        post = await self.prepare_post()
        self.assertIsNone(post.scheduled_at)
        self.assertEqual(post.status, PostStatus.REVIEW)
        with self.assertRaises(InvalidInput):
            self.service.approve_post(OWNER, post.id, post.revision)

    async def test_automation_creates_one_job_after_downtime_without_approval(self):
        self.service.configure_automation(
            OWNER, self.business.id, True, first_run_at=self.clock(), interval_hours=24
        )
        self.clock.advance(days=3)
        self.assertEqual(self.service.schedule_automations(), 1)
        self.assertEqual(self.service.schedule_automations(), 0)
        self.assertEqual(len(self.repo.list_jobs(self.business.id)), 1)
        await self.service.prepare_next_job()
        post = self.repo.list_posts(self.business.id)[0]
        self.assertEqual(post.status, PostStatus.REVIEW)
        self.clock.advance(minutes=2)
        self.assertFalse(await self.service.publish_due_once())

    async def test_generation_cancellation_is_durable_failure_without_retry(self):
        job = self.service.request_preparation(OWNER, self.business.id, "task", "cancel-job")
        gate = self.ai.gates["plan"] = Gate()
        task = asyncio.create_task(self.service.prepare_next_job())
        await asyncio.wait_for(gate.entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.repo.get_job(job.id).status, "failed")
        self.assertFalse(await self.service.prepare_next_job())
        self.assertEqual(self.repo.list_posts(self.business.id), [])

    async def test_publication_cancellation_is_unknown_without_automatic_retry(self):
        post = await self.due_post()
        gate = self.publisher.gate = Gate()
        task = asyncio.create_task(self.service.publish_due_once())
        await asyncio.wait_for(gate.entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.DELIVERY_UNKNOWN)
        self.assertFalse(await self.service.publish_due_once())
        self.assertEqual(len(self.publisher.calls), 1)

    async def test_timeout_after_send_is_unknown_and_explicit_rejection_is_failed(self):
        for error, expected in (
            (TimeoutError("network"), PostStatus.DELIVERY_UNKNOWN),
            (DeliveryRejected("rejected"), PostStatus.FAILED),
        ):
            with self.subTest(expected=expected):
                post = await self.due_post()
                self.publisher.error = error
                await self.service.publish_due_once()
                self.assertEqual(self.repo.get_post(post.id).status, expected)
                self.assertFalse(await self.service.publish_due_once())

    async def test_db_failure_before_completion_marks_delivery_unknown(self):
        post = await self.due_post()
        with patch.object(self.repo, "finish_publication", side_effect=sqlite3.OperationalError("disk")):
            await self.service.publish_due_once()
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.DELIVERY_UNKNOWN)
        self.assertFalse(await self.service.publish_due_once())
        self.assertEqual(len(self.publisher.calls), 1)

    async def test_post_commit_error_cannot_demote_published_post_or_send_twice(self):
        post = await self.due_post()
        original = self.repo.finish_publication

        def committed_then_raised(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("response lost after commit")

        with patch.object(self.repo, "finish_publication", side_effect=committed_then_raised):
            # A service may report the persistence fault; the durable successful
            # state must win over attempted failure handling in either case.
            try:
                await self.service.publish_due_once()
            except (Conflict, sqlite3.OperationalError):
                pass
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.PUBLISHED)
        self.assertFalse(await self.service.publish_due_once())
        self.assertEqual(len(self.publisher.calls), 1)

    async def test_persistence_unavailable_after_send_recovers_as_unknown(self):
        post = await self.due_post()
        with (
            patch.object(self.repo, "finish_publication", side_effect=sqlite3.OperationalError("disk")),
            patch.object(self.repo, "fail_publication", side_effect=sqlite3.OperationalError("disk")),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                await self.service.publish_due_once()
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.PUBLISHING)
        recovered = self.service.recover_interrupted()
        self.assertEqual(recovered["posts"], 1)
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.DELIVERY_UNKNOWN)
        self.assertFalse(await self.service.publish_due_once())

    async def test_zero_message_id_cannot_confirm_non_delivery(self):
        post = await self.due_post()
        self.publisher.error = TimeoutError("network")
        await self.service.publish_due_once()
        with self.assertRaises(InvalidInput):
            self.service.resolve_unknown_delivery(OWNER, post.id, post.revision, message_id=0)
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.DELIVERY_UNKNOWN)

    async def test_live_generation_rejects_invalid_image_signature(self):
        self.settings = Settings(
            mode="live",
            owner_id=OWNER,
            data_dir=self.root,
            telegram_bot_token="123:TEST_ONLY",
            ai_factory="test:ai",
            image_factory="test:image",
        )
        self.service = self.new_service()
        self.images.content = b"not a PNG despite MIME type"
        job = self.service.request_preparation(OWNER, self.business.id, "task", "invalid-image")
        await self.service.prepare_next_job()
        self.assertEqual(self.repo.get_job(job.id).status, "failed")
        self.assertEqual(self.repo.list_posts(self.business.id), [])
        self.assertEqual(len(self.publisher.calls), 0)

    async def test_demo_material_cannot_be_approved_in_live_mode(self):
        post = await self.prepare_post()
        self.settings = Settings(
            mode="live",
            owner_id=OWNER,
            data_dir=self.root,
            telegram_bot_token="123:TEST_ONLY",
            ai_factory="test:ai",
            image_factory="test:image",
        )
        self.service = self.new_service()
        with self.assertRaises(InvalidInput):
            self.service.approve_post(OWNER, post.id, post.revision)
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.REVIEW)

    async def test_live_image_edit_preserves_demo_origin_of_existing_post(self):
        post = await self.prepare_post()
        self.assertTrue(post.is_demo)
        self.settings = Settings(
            mode="live",
            owner_id=OWNER,
            data_dir=self.root,
            telegram_bot_token="123:TEST_ONLY",
            ai_factory="test:ai",
            image_factory="test:image",
        )
        self.service = self.new_service()
        edited = await self.service.revise_image(OWNER, post.id, post.revision, "Use a real image")
        self.assertNotEqual(edited.image_asset_id, post.image_asset_id)
        self.assertFalse(self.repo.get_asset(edited.image_asset_id).is_demo)
        self.assertTrue(edited.is_demo)
        with self.assertRaises(InvalidInput):
            self.service.approve_post(OWNER, edited.id, edited.revision)
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.REVIEW)

    async def test_pdf_input_contains_only_its_business_sources_and_bytes(self):
        own = self.service.add_pdf_source(OWNER, self.business.id, "brief.pdf", b"%PDF-1.7 own")
        second = await self.ready_business("Second")
        foreign = self.service.add_pdf_source(OWNER, second.id, "private.pdf", b"%PDF-1.7 foreign")
        await self.service.analyze_business(OWNER, self.business.id)
        self.assertEqual(len(self.ai.profile_inputs), 1)
        self.assertEqual(self.ai.profile_inputs[0].source.id, own.id)
        self.assertEqual(self.ai.profile_inputs[0].content, b"%PDF-1.7 own")
        with self.assertRaises(InvalidInput):
            self.repo.add_source(
                Source(
                    id=str(uuid4()),
                    business_id=self.business.id,
                    kind="pdf",
                    name="forged",
                    asset_id=foreign.asset_id,
                    created_at=self.clock(),
                )
            )

    async def test_foreign_business_image_cannot_be_attached_to_post(self):
        post = await self.prepare_post()
        second = await self.ready_business("Second")
        image = self.files.write(second.id, "foreign.png", PNG, "image/png", self.clock())
        self.repo.add_asset(image)
        with self.assertRaises(InvalidInput):
            self.service.edit_post(OWNER, post.id, post.revision, image_asset_id=image.id)
        self.assertEqual(self.repo.get_post(post.id).image_asset_id, post.image_asset_id)

    async def test_unsupported_urls_are_rejected_without_creating_source(self):
        invalid = (
            "file:///etc/passwd",
            "ftp://example.com/file",
            "http://localhost/",
            "https://127.0.0.1/",
            "http://[::1]/",
            "http://10.0.0.1/",
            "https://user:password@example.com/",
            "https://example.com:8443/",
            "https://service.internal/",
            "https:///missing-host",
        )
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(InvalidInput):
                    self.service.add_url_source(OWNER, self.business.id, url)
        self.assertEqual(self.repo.list_sources(self.business.id), [])
        source = self.service.add_url_source(OWNER, self.business.id, "https://example.com/about")
        self.assertEqual(source.url, "https://example.com/about")

    async def test_ai_unknown_source_cannot_enter_profile_or_post(self):
        self.service.add_text_source(OWNER, self.business.id, "Known text")
        self.ai.profile = BusinessProfile(
            name="Invalid",
            description="Unknown citation",
            facts=[Fact(text="Claim", source_ids=["foreign-source"])],
        )
        with self.assertRaises(InvalidInput):
            await self.service.analyze_business(OWNER, self.business.id)
        current = self.repo.get_business(self.business.id)
        self.service.confirm_profile(OWNER, self.business.id, current.profile_version)
        self.ai.used_source_ids = ["foreign-source"]
        job = self.service.request_preparation(OWNER, self.business.id, "task", "source-check")
        await self.service.prepare_next_job()
        self.assertEqual(self.repo.get_job(job.id).status, "failed")
        self.assertEqual(self.repo.list_posts(self.business.id), [])

    async def test_ai_revision_rejects_unknown_source_and_preserves_original(self):
        post = await self.prepare_post()

        async def bad_revision(*args, **kwargs):
            return TextDraft(
                text="Claim based on a foreign source.",
                image_brief="Bread",
                used_source_ids=["foreign-source"],
            )

        with patch.object(self.ai, "revise_text", side_effect=bad_revision):
            with self.assertRaises(InvalidInput):
                await self.service.revise_text(OWNER, post.id, post.revision, "add a fact")
        self.assertEqual(self.repo.get_post(post.id).revision, post.revision)
        self.assertEqual(self.repo.get_post(post.id).text, post.text)

    async def test_ai_cannot_choose_an_unselected_post_or_approve_it(self):
        post = await self.prepare_post()
        self.ai.intent = TaskIntent(action="revise_text", post_id=post.id, request="change")
        with self.assertRaises(InvalidInput):
            await self.service.interpret_request(OWNER, self.business.id, "change some post")
        self.ai.intent = {"action": "approve", "post_id": post.id}
        with self.assertRaises(ProviderUnavailable):
            await self.service.interpret_request(OWNER, self.business.id, "approve", post_id=post.id)
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.REVIEW)

    async def test_malformed_ai_result_exposes_no_raw_payload_or_validation_context(self):
        post = await self.prepare_post()
        self.service.add_text_source(OWNER, self.business.id, "Known business description")
        current = self.repo.get_business(self.business.id)
        self.service.confirm_profile(OWNER, self.business.id, current.profile_version)
        secret = "private-provider-content-" + uuid4().hex
        scenarios = (
            (
                "build_profile",
                {"name": "Bakery", "description": {"private": secret}},
                lambda: self.service.analyze_business(OWNER, self.business.id),
            ),
            (
                "interpret_request",
                {"action": secret},
                lambda: self.service.interpret_request(OWNER, self.business.id, "prepare"),
            ),
            (
                "revise_text",
                {"text": [secret], "image_brief": "Bread"},
                lambda: self.service.revise_text(OWNER, post.id, post.revision, "shorten"),
            ),
        )
        for method, invalid_response, operation in scenarios:
            with self.subTest(method=method):
                with patch.object(self.ai, method, return_value=invalid_response):
                    try:
                        await operation()
                    except ProviderUnavailable as error:
                        self.assertNotIn(secret, str(error))
                        self.assertNotIn(secret, "".join(traceback.format_exception(error)))
                        self.assertTrue(error.__suppress_context__)
                    else:
                        self.fail("Malformed AI result was accepted")
        self.assertEqual(self.repo.get_post(post.id).revision, post.revision)

    async def test_malformed_image_result_fails_job_without_raw_payload_in_logs_or_storage(self):
        secret = "private-image-response-" + uuid4().hex
        job = self.service.request_preparation(OWNER, self.business.id, "task", "bad-image-model")
        invalid_image = {"content": PNG, "mime_type": secret, "provider": "fake"}
        with patch.object(self.images, "generate", return_value=invalid_image):
            with self.assertLogs("app.core.service", level="WARNING") as captured:
                await self.service.prepare_next_job()
        failed = self.repo.get_job(job.id)
        self.assertEqual(failed.status, "failed")
        self.assertNotIn(secret, failed.error)
        self.assertNotIn(secret, "\n".join(captured.output))
        self.assertEqual(self.repo.list_posts(self.business.id), [])


if __name__ == "__main__":
    unittest.main()
