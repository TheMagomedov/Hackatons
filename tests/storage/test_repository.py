import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.shared.errors import Conflict, Forbidden, InvalidInput
from app.shared.models import (
    Asset,
    BusinessProfile,
    ChannelInfo,
    ContentPlan,
    MetricSnapshot,
    Notification,
    PlanItem,
    PostStatus,
    PublicationResult,
    Source,
)
from app.storage.repository import Repository

NOW = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "nested" / "app.sqlite"
        self.repo = Repository(self.path)
        self.repo.initialize()
        self.business = self.repo.create_business(17, "Кофейня", "UTC", NOW)
        self.repo.set_channel(self.business.id, ChannelInfo(id="-1001", title="Канал"), NOW)
        self.asset = Asset(
            id=str(uuid4()),
            business_id=self.business.id,
            filename="image.png",
            mime_type="image/png",
            size=3,
            sha256="a" * 64,
            created_at=NOW,
        )
        self.repo.add_asset(self.asset)

    def tearDown(self):
        self.temp.cleanup()

    def values(self, **updates):
        return {
            "text": "Наши завтраки",
            "image_asset_id": self.asset.id,
            "channel_id": "-1001",
            "scheduled_at": NOW + timedelta(hours=1),
            **updates,
        }

    def post(self, **updates):
        return self.repo.create_posts(self.business.id, [self.values(**updates)], NOW)[0]

    def confirmed_profile(self):
        business = self.repo.set_profile(
            self.business.id, BusinessProfile(name="Кофейня", description="Завтраки"), NOW
        )
        return self.repo.confirm_profile(business.id, business.profile_version, NOW)

    def approved(self):
        post = self.post()
        return self.repo.approve_post(post.id, post.revision, 17, NOW)

    def result(self):
        return PublicationResult(channel_id="-1001", message_id=4, message_url="https://t.me/example/4")

    def test_wal_configuration_and_repeat_initialization(self):
        self.repo.initialize()
        with self.repo._reader() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_future_schema_is_rejected_without_downgrade(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA user_version = 3")
        with self.assertRaises(Conflict):
            self.repo.initialize()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_profile_version_cas_and_confirmation(self):
        one = self.confirmed_profile()
        two = self.repo.set_profile(
            one.id,
            BusinessProfile(name="Новое", description="Описание"),
            NOW,
            expected_version=one.profile_version,
        )
        self.assertEqual(two.profile_version, 2)
        self.assertFalse(two.profile_confirmed)
        with self.assertRaises(Conflict):
            self.repo.confirm_profile(two.id, one.profile_version, NOW)
        with self.assertRaises(Conflict):
            self.repo.set_profile(two.id, two.profile, NOW, expected_version=one.profile_version)

    def test_sources_and_assets_are_scoped_and_idempotent(self):
        source = Source(
            id=str(uuid4()),
            business_id=self.business.id,
            kind="pdf",
            name="menu.pdf",
            asset_id=self.asset.id,
            created_at=NOW,
        )
        self.repo.add_source(source)
        self.repo.add_source(source)
        self.assertEqual(self.repo.get_source(source.id), source)
        self.assertEqual(len(self.repo.list_sources(self.business.id)), 1)
        other = self.repo.create_business(18, "Другая", "UTC", NOW)
        with self.assertRaises(InvalidInput):
            self.repo.add_source(source.model_copy(update={"id": str(uuid4()), "business_id": other.id}))

    def test_new_source_invalidates_existing_profile_and_inflight_generation(self):
        before = self.confirmed_profile()
        self.repo.set_automation(before.id, True, "Подготовь пост", 24, NOW, NOW)
        source = Source(
            id=str(uuid4()),
            business_id=before.id,
            kind="text",
            name="Обновлённое меню",
            text="Теперь завтраки доступны весь день",
            created_at=NOW,
        )
        self.repo.add_source(source)
        after = self.repo.get_business(before.id)
        self.assertEqual(after.profile, before.profile)
        self.assertEqual(after.profile_version, before.profile_version + 1)
        self.assertFalse(after.profile_confirmed)
        self.assertEqual(self.repo.schedule_automations(NOW), 0)
        with self.assertRaises(Conflict):
            self.repo.confirm_profile(before.id, before.profile_version, NOW)
        with self.assertRaises(Conflict):
            self.repo.create_posts(
                before.id, [self.values()], NOW, expected_profile_version=before.profile_version
            )
        self.assertEqual(self.repo.list_posts(before.id), [])
        # Delivery retry of the same source must not invalidate a newly approved
        # profile or increment its version again.
        self.repo.confirm_profile(after.id, after.profile_version, NOW)
        self.repo.add_source(source)
        repeated = self.repo.get_business(before.id)
        self.assertEqual(repeated.profile_version, after.profile_version)
        self.assertTrue(repeated.profile_confirmed)
        invalidations = [
            event
            for event in self.repo.audit_events("business", before.id)
            if event["action"] == "profile_invalidated_new_source"
        ]
        self.assertEqual(len(invalidations), 1)
        self.assertEqual(invalidations[0]["details"]["source_id"], source.id)

    def test_initial_sources_do_not_increment_missing_profile_version(self):
        source = Source(
            id=str(uuid4()),
            business_id=self.business.id,
            kind="text",
            name="Описание",
            text="Кофейня с завтраками",
            created_at=NOW,
        )
        self.repo.add_source(source)
        business = self.repo.get_business(self.business.id)
        self.assertIsNone(business.profile)
        self.assertEqual(business.profile_version, 0)
        self.assertFalse(business.profile_confirmed)

    def test_approval_requires_owner_and_future_time(self):
        post = self.post()
        with self.assertRaises(Forbidden):
            self.repo.approve_post(post.id, 1, 18, NOW)
        with self.assertRaises(InvalidInput):
            self.repo.approve_post(post.id, 1, 17, post.scheduled_at)
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.REVIEW)

    def test_stale_buttons_and_text_edit_invalidate_approval(self):
        post = self.approved()
        self.assertEqual(self.repo.approve_post(post.id, 1, 17, NOW), post)
        changed = self.repo.revise_post(post.id, 1, {"text": "Новая редакция"}, NOW)
        self.assertEqual(changed.revision, 2)
        self.assertEqual(changed.status, PostStatus.REVIEW)
        self.assertIsNone(changed.approved_revision)
        self.assertIsNone(changed.approved_by)
        with self.assertRaises(Conflict):
            self.repo.approve_post(post.id, 1, 17, NOW)
        with self.assertRaises(Conflict):
            self.repo.revise_post(post.id, 1, {"text": "Устаревшая редакция"}, NOW)
        self.assertIsNone(self.repo.claim_due_post(NOW + timedelta(hours=2)))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM post_revisions WHERE post_id=?", (post.id,)).fetchone()[0],
                2,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE post_revisions SET data='{}' WHERE post_id=?", (post.id,))

    def test_image_and_schedule_edit_invalidate_approval(self):
        post = self.approved()
        other_asset = self.asset.model_copy(update={"id": str(uuid4())})
        self.repo.add_asset(other_asset)
        changed = self.repo.revise_post(
            post.id, 1, {"image_asset_id": other_asset.id, "scheduled_at": NOW + timedelta(hours=3)}, NOW
        )
        self.assertEqual(changed.image_asset_id, other_asset.id)
        self.assertEqual(changed.status, PostStatus.REVIEW)
        self.assertIsNone(changed.approved_revision)

    def test_channel_switch_back_does_not_restore_approval(self):
        post = self.approved()
        self.repo.set_channel(self.business.id, ChannelInfo(id="-1002", title="Другой"), NOW)
        self.repo.set_channel(self.business.id, ChannelInfo(id="-1001", title="Первый"), NOW)
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.REVIEW)
        self.assertIsNone(self.repo.claim_due_post(NOW + timedelta(hours=2)))

    def test_only_one_concurrent_worker_claims_a_post(self):
        post = self.approved()
        due = NOW + timedelta(hours=2)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.repo.claim_due_post(due), range(4)))
        claimed = [result for result in results if result is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].id, post.id)
        self.assertEqual(claimed[0].status, PostStatus.PUBLISHING)
        with self.assertRaises(Conflict):
            self.repo.revise_post(post.id, 1, {"text": "Слишком поздно"}, due)
        done = self.repo.finish_publication(post.id, 1, self.result(), due)
        self.assertEqual(done.status, PostStatus.PUBLISHED)
        self.assertEqual(done.message_id, 4)
        self.assertIsNone(self.repo.claim_due_post(due))
        with self.assertRaises(Conflict):
            self.repo.reject_post(post.id, 1, due)

    def test_publication_finish_requires_correct_revision_and_channel(self):
        post = self.approved()
        due = NOW + timedelta(hours=2)
        self.repo.claim_due_post(due)
        with self.assertRaises(Conflict):
            self.repo.finish_publication(post.id, 2, self.result(), due)
        with self.assertRaises(Conflict):
            self.repo.finish_publication(post.id, 1, PublicationResult(channel_id="-999", message_id=1), due)
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.PUBLISHING)

    def test_recovery_marks_ambiguous_send_and_never_retries(self):
        post = self.approved()
        job = self.repo.enqueue_job(self.business.id, "Подготовь", "manual-1", NOW)
        self.repo.claim_job(NOW)
        due = NOW + timedelta(hours=2)
        self.repo.claim_due_post(due)
        restarted = Repository(self.path)
        restarted.initialize()
        self.assertEqual(restarted.recover_interrupted(due), {"posts": 1, "jobs": 1, "edits": 0})
        self.assertEqual(restarted.recover_interrupted(due), {"posts": 0, "jobs": 0, "edits": 0})
        self.assertEqual(restarted.get_job(job.id).status, "failed")
        self.assertEqual(restarted.get_post(post.id).status, PostStatus.DELIVERY_UNKNOWN)
        self.assertIsNone(restarted.claim_due_post(due))
        self.assertIsNone(restarted.claim_job(due))
        with self.assertRaises(Conflict):
            restarted.approve_post(post.id, 1, 17, due)
        with self.assertRaises(Conflict):
            restarted.revise_post(post.id, 1, {"text": "Повтор"}, due)
        failed = restarted.resolve_unknown(post.id, 1, None, due)
        self.assertEqual(failed.status, PostStatus.FAILED)
        self.assertIsNone(failed.approved_revision)
        self.assertIsNone(restarted.claim_due_post(due))
        retry = restarted.revise_post(post.id, 1, {"scheduled_at": due + timedelta(hours=1)}, due)
        self.assertEqual(retry.revision, 2)
        self.assertEqual(retry.status, PostStatus.REVIEW)

    def test_manual_resolution_preserves_delivery_audit(self):
        post = self.approved()
        due = NOW + timedelta(hours=2)
        self.repo.claim_due_post(due)
        self.repo.fail_publication(post.id, 1, "timeout", True, due)
        done = self.repo.resolve_unknown(post.id, 1, self.result(), due)
        self.assertEqual(done.status, PostStatus.PUBLISHED)
        actions = [event["action"] for event in self.repo.audit_events("post", post.id)]
        self.assertIn("delivery_unknown", actions)
        self.assertIn("delivery_manually_confirmed", actions)

    def test_batch_creation_rolls_back_and_job_completion_is_atomic(self):
        job = self.repo.enqueue_job(self.business.id, "Три поста", "request-1", NOW)
        self.repo.claim_job(NOW)
        with self.assertRaises(InvalidInput):
            self.repo.create_posts(self.business.id, [self.values(), self.values(text="")], NOW, job.id)
        self.assertEqual(self.repo.list_posts(self.business.id), [])
        self.assertEqual(self.repo.get_job(job.id).status, "running")
        posts = self.repo.create_posts(
            self.business.id, [self.values(), self.values(text="Второй")], NOW, job.id
        )
        self.assertEqual(len(posts), 2)
        self.assertEqual(self.repo.get_job(job.id).status, "done")
        with self.assertRaises(Conflict):
            self.repo.create_posts(self.business.id, [self.values()], NOW, job.id)
        self.assertEqual(len(self.repo.list_posts(self.business.id)), 2)

    def test_generated_result_cannot_overwrite_new_profile_or_channel(self):
        current = self.confirmed_profile()
        with self.assertRaises(Conflict):
            self.repo.create_posts(
                self.business.id, [self.values()], NOW, expected_profile_version=current.profile_version - 1
            )
        with self.assertRaises(Conflict):
            self.repo.create_posts(self.business.id, [self.values()], NOW, expected_channel_id="-1002")
        self.assertEqual(self.repo.list_posts(self.business.id), [])

    def test_plan_remains_available_after_generation_failure(self):
        job = self.repo.enqueue_job(self.business.id, "Подготовь", "request-plan", NOW)
        self.repo.claim_job(NOW)
        plan = ContentPlan(
            items=[PlanItem(topic="Завтраки", goal="Рассказать", brief="О меню", image_brief="Чашка кофе")]
        )
        self.repo.save_plan(self.business.id, job.id, plan, NOW)
        self.repo.save_plan(self.business.id, job.id, plan, NOW)
        self.repo.fail_job(job.id, "Изображение недоступно", NOW)
        self.assertEqual(self.repo.get_plan_for_job(job.id), plan)
        self.assertEqual(self.repo.list_posts(self.business.id), [])

    def test_job_dedup_and_limits_are_atomic(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            jobs = list(
                executor.map(
                    lambda _: self.repo.enqueue_job(
                        self.business.id, "Пост", "same", NOW, max_pending=1, max_per_day=1
                    ),
                    range(4),
                )
            )
        self.assertEqual(len({job.id for job in jobs}), 1)
        with self.assertRaises(Conflict):
            self.repo.enqueue_job(self.business.id, "Другой", "another", NOW, max_pending=1)
        self.repo.fail_job(jobs[0].id, "Отмена", NOW)
        with self.assertRaises(Conflict):
            self.repo.enqueue_job(self.business.id, "Другой", "another", NOW, max_per_day=1)
        next_day = self.repo.enqueue_job(
            self.business.id, "Другой", "tomorrow", NOW + timedelta(days=1), max_per_day=1
        )
        self.assertNotEqual(jobs[0].id, next_day.id)
        self.assertEqual(len(self.repo.list_jobs(self.business.id)), 2)

    def test_automation_skips_missed_windows_and_respects_capacity(self):
        self.confirmed_profile()
        self.repo.set_automation(self.business.id, True, "Подготовь", 24, NOW, NOW)
        blocker = self.repo.enqueue_job(self.business.id, "Ручное", "blocker", NOW)
        much_later = NOW + timedelta(days=3, hours=1)
        self.assertEqual(self.repo.schedule_automations(much_later, max_pending=1), 0)
        self.assertEqual(self.repo.get_business(self.business.id).next_generation_at, NOW)
        self.repo.fail_job(blocker.id, "Отмена", much_later)
        self.assertEqual(self.repo.schedule_automations(much_later, max_pending=1), 1)
        self.assertEqual(self.repo.schedule_automations(much_later, max_pending=1), 0)
        self.assertEqual(self.repo.get_business(self.business.id).next_generation_at, NOW + timedelta(days=4))
        self.assertEqual(len(self.repo.list_jobs(self.business.id)), 2)

    def test_unconfirmed_profile_stops_automation(self):
        self.confirmed_profile()
        self.repo.set_automation(self.business.id, True, "Подготовь", 24, NOW, NOW)
        self.repo.set_profile(self.business.id, BusinessProfile(name="Новое", description="Изменено"), NOW)
        self.assertEqual(self.repo.schedule_automations(NOW), 0)

    def test_durable_notifications_back_off_and_stop_after_five_attempts(self):
        note = Notification(
            id=str(uuid4()),
            business_id=self.business.id,
            owner_id=17,
            kind="ready",
            text="Готово",
            created_at=NOW,
        )
        self.repo.add_notification(note)
        self.repo.add_notification(note)
        current = NOW
        for attempt in range(5):
            self.assertEqual(self.repo.pending_notifications(10, current), [note])
            self.repo.record_notification_failure(note.id, current)
            self.assertEqual(self.repo.pending_notifications(10, current), [])
            current += timedelta(seconds=30 * 2**attempt)
        restarted = Repository(self.path)
        self.assertEqual(restarted.pending_notifications(10, current + timedelta(days=1)), [])
        self.assertEqual(self.repo.audit_events("notification", note.id)[-1]["action"], "exhausted")

    def test_notification_acknowledgement_is_idempotent(self):
        note = Notification(
            id=str(uuid4()),
            business_id=self.business.id,
            owner_id=17,
            kind="ready",
            text="Готово",
            created_at=NOW,
        )
        self.repo.add_notification(note)
        self.repo.acknowledge_notification(note.id, NOW)
        self.repo.acknowledge_notification(note.id, NOW)
        self.assertEqual(self.repo.pending_notifications(10, NOW), [])

    def test_metrics_are_event_idempotent_and_report_has_baseline(self):
        post = self.post()
        values = [
            ("old", NOW - timedelta(days=2), 10),
            ("baseline", NOW - timedelta(hours=1), 12),
            ("period", NOW + timedelta(hours=1), 14),
            ("future", NOW + timedelta(days=2), 20),
        ]
        for event, observed, value in values:
            metric = MetricSnapshot(
                business_id=self.business.id,
                name="subscribers",
                value=value,
                observed_at=observed,
                source="telegram",
                event_id=event,
            )
            self.assertTrue(self.repo.record_metric(metric))
            self.assertFalse(self.repo.record_metric(metric))
        reactions = MetricSnapshot(
            business_id=self.business.id,
            name="reactions",
            value=3,
            observed_at=NOW + timedelta(hours=1),
            source="telegram",
            post_id=post.id,
            event_id="r1",
        )
        self.repo.record_metric(reactions)
        with self.assertRaises(Conflict):
            self.repo.record_metric(reactions.model_copy(update={"value": 4}))
        report = self.repo.report_input(self.business.id, NOW, NOW + timedelta(days=1))
        self.assertEqual({metric.event_id for metric in report.metrics}, {"baseline", "period", "r1"})
        self.assertEqual(report.posts, [post])
        with self.assertRaises(InvalidInput):
            self.repo.report_input(self.business.id, NOW, NOW - timedelta(seconds=1))

    def test_naive_dates_are_rejected(self):
        with self.assertRaises(InvalidInput):
            self.repo.create_business(19, "Пример", "UTC", datetime(2026, 11, 9))
        with self.assertRaises(InvalidInput):
            self.post(scheduled_at=datetime(2026, 11, 10))


if __name__ == "__main__":
    unittest.main()
