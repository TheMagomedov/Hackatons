"""Data-preserving migrations, edit fencing, and transactional outbox regressions."""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app.shared.errors import Conflict, InvalidInput
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
from app.storage import Repository
from app.storage.repository import SCHEMA_VERSION

NOW = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "backend.sqlite3"
        self.repo = Repository(self.path)
        self.repo.initialize()
        self.business = self.repo.create_business(17, "Private business name", "UTC", NOW)
        self.repo.set_channel(self.business.id, ChannelInfo(id="-1001", title="Channel"), NOW)
        self.asset = Asset(
            id=str(uuid4()),
            business_id=self.business.id,
            filename="private-image.png",
            mime_type="image/png",
            size=3,
            sha256="a" * 64,
            created_at=NOW,
        )
        self.repo.add_asset(self.asset)

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def values(self, **updates):
        return {
            "text": "Private post content",
            "image_asset_id": self.asset.id,
            "channel_id": "-1001",
            "scheduled_at": NOW + timedelta(minutes=1),
            **updates,
        }

    def post(self, **updates):
        return self.repo.create_posts(self.business.id, [self.values(**updates)], NOW)[0]

    def note(self, *, post_id=None, job_id=None, **updates):
        values = dict(
            id=str(uuid4()),
            business_id=self.business.id,
            owner_id=17,
            kind="test",
            text="Private notification",
            created_at=NOW,
            post_id=post_id,
            job_id=job_id,
        )
        values.update(updates)
        return Notification(**values)

    def approved(self):
        post = self.post()
        return self.repo.approve_post(post.id, post.revision, 17, NOW)

    def confirmed(self, business=None):
        business = business or self.business
        current = self.repo.set_profile(
            business.id, BusinessProfile(name="Profile", description="Facts"), NOW
        )
        return self.repo.confirm_profile(business.id, current.profile_version, NOW)

    def make_legacy_v1(self):
        # Runtime-binding is v2's sole schema addition. Remove it from a populated
        # compatible fixture to recreate the exact v1 table shape and contents.
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("DROP TABLE ai_operations")
            conn.execute("DROP TABLE inbound_events")
            conn.execute("DROP TABLE runtime_binding")
            conn.execute("PRAGMA user_version = 1")
            conn.commit()

    def rows_snapshot(self):
        with closing(sqlite3.connect(self.path)) as conn:
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('runtime_binding','ai_operations','inbound_events') ORDER BY name"
                )
            ]
            return {name: conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in names}

    def test_populated_v1_migration_preserves_every_existing_row_and_repeats(self):
        job = self.repo.enqueue_job(self.business.id, "A request", "one", NOW)
        self.repo.claim_job(NOW)
        plan = ContentPlan(items=[PlanItem(topic="Topic", goal="Goal", brief="Brief", image_brief="Image")])
        self.repo.save_plan(self.business.id, job.id, plan, NOW)
        posts = self.repo.create_posts(
            self.business.id, [self.values()], NOW, job.id, notification=self.note(job_id=job.id)
        )
        self.repo.add_source(
            Source(
                id=str(uuid4()),
                business_id=self.business.id,
                kind="text",
                name="Source",
                text="Private source content",
                created_at=NOW,
            )
        )
        self.repo.record_metric(
            MetricSnapshot(
                business_id=self.business.id,
                name="subscribers",
                value=7,
                observed_at=NOW,
                source="test",
                event_id="first",
            )
        )
        self.make_legacy_v1()
        before = self.rows_snapshot()
        self.repo.initialize()
        self.repo.initialize()
        self.assertEqual(self.rows_snapshot(), before)
        self.assertIsNone(self.repo.runtime_binding())
        self.assertEqual(self.repo.diagnostics()["schema_version"], SCHEMA_VERSION)
        self.assertEqual(self.repo.get_post(posts[0].id), posts[0])
        self.assertEqual(self.repo.get_plan_for_job(job.id), plan)

    def test_migration_failure_rolls_back_schema_and_version_and_can_retry(self):
        self.post()
        self.make_legacy_v1()
        before = self.rows_snapshot()
        original = self.repo._migrate_v1_to_v2

        def fail_after_ddl(conn):
            original(conn)
            raise RuntimeError("simulated migration interruption")

        with patch.object(self.repo, "_migrate_v1_to_v2", side_effect=fail_after_ddl):
            with self.assertRaises(RuntimeError):
                self.repo.initialize()
        self.assertEqual(self.rows_snapshot(), before)
        self.assertEqual(self.repo.diagnostics()["schema_version"], 1)
        with self.repo._reader() as conn:
            self.assertIsNone(
                conn.execute("SELECT name FROM sqlite_master WHERE name='runtime_binding'").fetchone()
            )
        self.repo.initialize()
        self.assertEqual(self.repo.diagnostics()["schema_version"], SCHEMA_VERSION)

    def test_empty_runtime_binding_is_durable_and_cannot_switch_owner_or_mode(self):
        path = Path(self.temp.name) / "empty.sqlite3"
        empty = Repository(path)
        empty.initialize()
        expected = {"mode": "live", "owner_id": 17}
        self.assertEqual(empty.bind_runtime("live", 17), expected)
        self.assertEqual(Repository(path).runtime_binding(), expected)
        self.assertEqual(empty.bind_runtime("live", 17), expected)
        for mode, owner in (("demo", 17), ("live", 18)):
            with self.assertRaises(Conflict):
                empty.bind_runtime(mode, owner, adopt_legacy=True)
        self.assertEqual(empty.runtime_binding(), expected)

    def test_populated_unbound_database_requires_explicit_adoption(self):
        with self.assertRaises(Conflict):
            self.repo.bind_runtime("live", 17)
        self.assertIsNone(self.repo.runtime_binding())
        with self.assertRaises(Conflict):
            self.repo.bind_runtime("live", 18, adopt_legacy=True)
        self.assertIsNone(self.repo.runtime_binding())
        self.assertEqual(
            self.repo.bind_runtime("live", 17, adopt_legacy=True), {"mode": "live", "owner_id": 17}
        )

    def test_demo_legacy_adoption_checks_posts_not_source_asset_flags(self):
        self.assertFalse(self.asset.is_demo)
        demo = self.post(is_demo=True)
        self.repo.bind_runtime("demo", 17, adopt_legacy=True)
        self.assertTrue(self.repo.get_post(demo.id).is_demo)
        self.assertFalse(self.repo.get_asset(self.asset.id).is_demo)

    def test_demo_refuses_real_posts_and_live_preserves_all_flags(self):
        real = self.post()
        demo = self.post(is_demo=True)
        with self.assertRaises(Conflict):
            self.repo.bind_runtime("demo", 17, adopt_legacy=True)
        self.assertIsNone(self.repo.runtime_binding())
        self.repo.bind_runtime("live", 17, adopt_legacy=True)
        self.assertFalse(self.repo.get_post(real.id).is_demo)
        self.assertTrue(self.repo.get_post(demo.id).is_demo)

    def test_binding_rejects_invalid_and_mixed_business_owners(self):
        with self.assertRaises(InvalidInput):
            self.repo.bind_runtime("live", 17, adopt_legacy="false")
        for mode, owner in (("unknown", 17), ("live", True), ("live", 0)):
            with self.assertRaises(InvalidInput):
                self.repo.bind_runtime(mode, owner, adopt_legacy=True)
        self.repo.create_business(99, "Another owner", "UTC", NOW)
        with self.assertRaises(Conflict):
            self.repo.bind_runtime("live", 17, adopt_legacy=True)
        self.assertIsNone(self.repo.runtime_binding())

    def test_edit_start_revokes_approval_and_fences_every_old_callback(self):
        old = self.approved()
        editing = self.repo.begin_edit(old.id, old.revision, NOW)
        self.assertEqual(editing.revision, old.revision + 1)
        self.assertEqual(editing.status, PostStatus.EDITING)
        self.assertEqual(editing.text, old.text)
        self.assertIsNone(editing.approved_revision)
        self.assertIsNone(self.repo.claim_due_post(NOW + timedelta(minutes=2)))
        for action in (
            lambda: self.repo.approve_post(old.id, old.revision, 17, NOW),
            lambda: self.repo.approve_post(old.id, editing.revision, 17, NOW),
            lambda: self.repo.reject_post(old.id, editing.revision, NOW),
            lambda: self.repo.revise_post(old.id, editing.revision, {"text": "Unexpected"}, NOW),
            lambda: self.repo.begin_edit(old.id, editing.revision, NOW),
        ):
            with self.assertRaises(Conflict):
                action()
        ready = self.repo.complete_edit(old.id, editing.revision, {"text": "Edited content"}, NOW)
        self.assertEqual(ready.revision, editing.revision + 1)
        self.assertEqual(ready.status, PostStatus.REVIEW)
        self.assertIsNone(ready.approved_revision)
        with self.assertRaises(Conflict):
            self.repo.complete_edit(old.id, editing.revision, {"text": "Late result"}, NOW)
        with self.repo._reader() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM post_revisions WHERE post_id=?", (old.id,)).fetchone()[0],
                3,
            )

    def test_only_one_concurrent_edit_can_begin(self):
        post = self.approved()

        def begin(_):
            try:
                return self.repo.begin_edit(post.id, post.revision, NOW)
            except Conflict:
                return None

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(begin, range(4)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_edit_completion_profile_and_channel_cas_are_atomic(self):
        profile = self.confirmed()
        post = self.post()
        editing = self.repo.begin_edit(post.id, post.revision, NOW)
        for kwargs in (
            {"expected_profile_version": profile.profile_version - 1},
            {"expected_channel_id": "-1002"},
        ):
            with self.assertRaises(Conflict):
                self.repo.complete_edit(post.id, editing.revision, {"text": "Edited"}, NOW, **kwargs)
        self.assertEqual(self.repo.get_post(post.id), editing)
        ready = self.repo.complete_edit(
            post.id,
            editing.revision,
            {"text": "Edited"},
            NOW,
            expected_profile_version=profile.profile_version,
            expected_channel_id="-1001",
        )
        self.assertEqual(ready.status, PostStatus.REVIEW)

    def test_revision_bool_is_not_an_integer_callback(self):
        post = self.post()
        with self.assertRaises(InvalidInput):
            self.repo.begin_edit(post.id, True, NOW)
        with self.assertRaises(InvalidInput):
            self.repo.approve_post(post.id, True, 17, NOW)

    def test_create_posts_and_done_job_roll_back_if_outbox_is_invalid(self):
        job = self.repo.enqueue_job(self.business.id, "Prepare", "prepare", NOW)
        self.repo.claim_job(NOW)
        bad = self.note(job_id=job.id, owner_id=99)
        with self.assertRaises(InvalidInput):
            self.repo.create_posts(self.business.id, [self.values()], NOW, job.id, notification=bad)
        self.assertEqual(self.repo.get_job(job.id).status, "running")
        self.assertEqual(self.repo.list_posts(self.business.id), [])
        good = self.note(job_id=job.id)
        self.repo.create_posts(self.business.id, [self.values()], NOW, job.id, notification=good)
        reopened = Repository(self.path)
        self.assertEqual(reopened.get_job(job.id).status, "done")
        self.assertEqual(reopened.pending_notifications(10, NOW), [good])

    def test_publication_and_failure_outcomes_are_atomic_with_outbox(self):
        for unknown in (None, False, True):
            with self.subTest(unknown=unknown):
                post = self.approved()
                due = NOW + timedelta(minutes=2)
                self.repo.claim_due_post(due)
                bad = self.note(post_id=post.id, owner_id=99)

                def action(note):
                    if unknown is None:
                        return self.repo.finish_publication(
                            post.id,
                            post.revision,
                            PublicationResult(channel_id=post.channel_id, message_id=55),
                            due,
                            notification=note,
                        )
                    return self.repo.fail_publication(
                        post.id, post.revision, "Failure", unknown, due, notification=note
                    )

                with self.assertRaises(InvalidInput):
                    action(bad)
                self.assertEqual(self.repo.get_post(post.id).status, PostStatus.PUBLISHING)
                good = self.note(post_id=post.id)
                action(good)
                reopened = Repository(self.path)
                self.assertIn(good, reopened.pending_notifications(20, due))
                expected = (
                    PostStatus.PUBLISHED
                    if unknown is None
                    else (PostStatus.DELIVERY_UNKNOWN if unknown else PostStatus.FAILED)
                )
                self.assertEqual(reopened.get_post(post.id).status, expected)

    def test_edit_and_job_failure_outbox_roll_back_on_invalid_recipient(self):
        post = self.post()
        editing = self.repo.begin_edit(post.id, post.revision, NOW)
        bad = self.note(post_id=post.id, owner_id=99)
        with self.assertRaises(InvalidInput):
            self.repo.fail_edit(post.id, editing.revision, "Failed", NOW, notification=bad)
        self.assertEqual(self.repo.get_post(post.id).status, PostStatus.EDITING)
        good = self.note(post_id=post.id)
        self.repo.fail_edit(post.id, editing.revision, "Failed", NOW, notification=good)
        self.assertIsNone(self.repo.get_post(post.id).approved_revision)
        with self.assertRaises(Conflict):
            self.repo.fail_edit(post.id, editing.revision, "Duplicate", NOW)
        job = self.repo.enqueue_job(self.business.id, "Prepare", "failure", NOW)
        with self.assertRaises(InvalidInput):
            self.repo.fail_job(job.id, "Failed", NOW, notification=self.note(job_id=job.id, owner_id=99))
        self.assertEqual(self.repo.get_job(job.id).status, "pending")
        job_note = self.note(job_id=job.id)
        self.repo.fail_job(job.id, "Failed", NOW, notification=job_note)
        self.assertEqual(set(n.id for n in self.repo.pending_notifications(10, NOW)), {good.id, job_note.id})

    def test_recovery_commits_each_state_with_one_deterministic_notification(self):
        sending = self.approved()
        self.repo.claim_due_post(NOW + timedelta(minutes=2))
        editing_post = self.post()
        editing = self.repo.begin_edit(editing_post.id, editing_post.revision, NOW)
        job = self.repo.enqueue_job(self.business.id, "Prepare", "recovery", NOW)
        self.repo.claim_job(NOW)
        due = NOW + timedelta(minutes=3)
        with patch.object(self.repo, "_add_notification", side_effect=RuntimeError("outbox unavailable")):
            with self.assertRaises(RuntimeError):
                self.repo.recover_interrupted(due)
        self.assertEqual(self.repo.get_post(sending.id).status, PostStatus.PUBLISHING)
        self.assertEqual(self.repo.get_post(editing.id).status, PostStatus.EDITING)
        self.assertEqual(self.repo.get_job(job.id).status, "running")
        self.assertEqual(self.repo.pending_notifications(10, due), [])
        self.assertEqual(self.repo.recover_interrupted(due), {"posts": 1, "jobs": 1, "edits": 1})
        notes = self.repo.pending_notifications(10, due)
        self.assertEqual(len(notes), 3)
        self.assertEqual(
            {note.kind for note in notes}, {"delivery_unknown", "edit_failed", "preparation_failed"}
        )
        reopened = Repository(self.path)
        self.assertEqual(
            reopened.recover_interrupted(due + timedelta(minutes=1)), {"posts": 0, "jobs": 0, "edits": 0}
        )
        self.assertEqual(reopened.pending_notifications(10, due), notes)
        self.assertEqual(reopened.get_post(editing.id).status, PostStatus.FAILED)
        self.assertIsNone(reopened.get_post(editing.id).approved_revision)

    def test_automation_payload_change_skips_used_slot_without_blocking_other_business(self):
        self.confirmed()
        self.repo.set_automation(self.business.id, True, "Original request", 24, NOW, NOW)
        self.assertEqual(self.repo.schedule_automations(NOW), 1)
        self.repo.set_automation(self.business.id, True, "Changed request", 24, NOW, NOW)
        other = self.repo.create_business(17, "Other", "UTC", NOW)
        self.repo.set_channel(other.id, ChannelInfo(id="-1002", title="Other"), NOW)
        self.confirmed(other)
        self.repo.set_automation(other.id, True, "Other request", 24, NOW, NOW)
        self.assertEqual(self.repo.schedule_automations(NOW, max_pending=1), 1)
        jobs = self.repo.list_jobs(self.business.id)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].request, "Original request")
        self.assertEqual(len(self.repo.list_jobs(other.id)), 1)
        self.assertGreater(self.repo.get_business(self.business.id).next_generation_at, NOW)
        self.assertIn(
            "automation_slot_skipped",
            [e["action"] for e in self.repo.audit_events("business", self.business.id)],
        )
        with self.assertRaises(Conflict):
            self.repo.enqueue_job(self.business.id, "Changed request", jobs[0].dedup_key, NOW)
        self.assertEqual(self.repo.find_job(self.business.id, jobs[0].dedup_key), jobs[0])
        self.assertIsNone(self.repo.find_job(self.business.id, "missing"))

    def test_read_only_legacy_diagnostics_exposes_counts_not_private_content(self):
        self.post()
        self.repo.enqueue_job(self.business.id, "Private request", "pending", NOW)
        pending, exhausted, delivered = self.note(), self.note(), self.note()
        for note in (pending, exhausted, delivered):
            self.repo.add_notification(note)
        for _ in range(5):
            self.repo.record_notification_failure(exhausted.id, NOW)
        self.repo.acknowledge_notification(delivered.id, NOW)
        self.make_legacy_v1()
        before = self.rows_snapshot()
        diagnostics = self.repo.diagnostics()
        self.assertEqual(
            {key: diagnostics[key] for key in ("schema_version", "binding", "counts")},
            {
                "schema_version": 1,
                "binding": None,
                "counts": {
                    "businesses": 1,
                    "posts": {"review": 1},
                    "jobs": {"pending": 1},
                    "notifications": {"pending": 1, "exhausted": 1, "delivered": 1},
                },
            },
        )
        self.assertEqual(self.rows_snapshot(), before)
        self.assertIsNone(self.repo.runtime_binding())
        serialized = json.dumps(diagnostics)
        for forbidden in (
            "Private",
            self.business.name,
            self.asset.filename,
            self.asset.sha256,
            str(self.path),
        ):
            self.assertNotIn(forbidden, serialized)

    def test_diagnostics_cannot_create_a_missing_database(self):
        path = Path(self.temp.name) / "missing.sqlite3"
        repository = Repository(path)
        with self.assertRaises(sqlite3.OperationalError):
            repository.diagnostics()
        self.assertFalse(path.exists())
        with self.assertRaises(sqlite3.OperationalError):
            repository.runtime_binding()
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
