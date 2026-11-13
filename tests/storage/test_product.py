"""Durable quotas, provenance and user-controlled work survive races/restarts."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.shared.errors import Conflict, Forbidden, InvalidInput, NotFound
from app.shared.models import Asset, BusinessProfile, ChannelInfo, Notification, PostStatus, Source
from app.storage import Repository
from app.storage.repository import SCHEMA_VERSION

NOW = datetime(2026, 11, 9, 9, tzinfo=UTC)


@pytest.fixture
def data(tmp_path):
    repo = Repository(tmp_path / "backend.sqlite3")
    repo.initialize()
    business = repo.create_business(17, "Private business", "UTC", NOW)
    repo.bind_runtime("live", 17, adopt_legacy=True)
    repo.set_channel(business.id, ChannelInfo(id="-1001", title="Channel"), NOW)
    source = Source(
        id=str(uuid4()),
        business_id=business.id,
        kind="text",
        name="Private source",
        text="Coffee",
        created_at=NOW,
    )
    repo.add_source(source)
    business = repo.set_profile(business.id, BusinessProfile(name="Cafe", description="Coffee"), NOW)
    business = repo.confirm_profile(business.id, business.profile_version, NOW)
    asset = Asset(
        id=str(uuid4()),
        business_id=business.id,
        filename="private.png",
        mime_type="image/png",
        size=3,
        sha256="a" * 64,
        created_at=NOW,
    )
    repo.add_asset(asset)
    values = {
        "text": "Private post",
        "channel_id": "-1001",
        "image_asset_id": asset.id,
        "scheduled_at": NOW + timedelta(minutes=1),
        "used_source_ids": [source.id],
        "warnings": ["Confirm business hours"],
    }
    yield repo, business, source, values
    repo.close()


def begin(
    repo, business, *, key="request:one", kind="interpret", digest="a" * 64, now=NOW, daily=5, parallel=2
):
    return repo.begin_ai_operation(
        business.owner_id, business.id, kind, key, digest, now, daily_limit=daily, max_parallel=parallel
    )


def legacy_v2(repo, post_id=None):
    with closing(sqlite3.connect(repo.db_path)) as conn:
        conn.execute("DROP TABLE conversation_turns")
        conn.execute("DROP TABLE ai_operations")
        conn.execute("DROP TABLE inbound_events")
        conn.execute("PRAGMA user_version=2")
        if post_id:
            # Emulate the actual v2 JSON without new provenance fields, including
            # immutable snapshots. This fixture runs before migration begins.
            trigger = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='immutable_revision_update'"
            ).fetchone()[0]
            conn.execute("DROP TRIGGER immutable_revision_update")
            for table in ("posts", "post_revisions"):
                key = "id" if table == "posts" else "post_id"
                for rowid, raw in conn.execute(
                    f"SELECT rowid,data FROM {table} WHERE {key}=?", (post_id,)
                ).fetchall():
                    value = json.loads(raw)
                    for name in ("profile_version", "used_source_ids", "warnings"):
                        value.pop(name)
                    conn.execute(f"UPDATE {table} SET data=? WHERE rowid=?", (json.dumps(value), rowid))
            conn.execute(trigger)
        conn.commit()


def raw_snapshot(repo):
    with closing(sqlite3.connect(repo.db_path)) as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('ai_operations','inbound_events','conversation_turns')"
            )
        ]
        return {name: conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in names}


def test_v2_migration_preserves_rows_and_requires_legacy_content_review(data):
    repo, business, _, values = data
    post = repo.create_posts(business.id, [values], NOW)[0]
    repo.approve_post(post.id, post.revision, 17, NOW)
    legacy_v2(repo, post.id)
    before = raw_snapshot(repo)
    repo.initialize()
    repo.initialize()
    assert raw_snapshot(repo) == before
    assert repo.diagnostics()["schema_version"] == SCHEMA_VERSION
    assert repo.get_post(post.id).profile_version is None
    assert repo.claim_due_post(NOW + timedelta(minutes=1)) is None
    current = repo.get_post(post.id)
    assert current.status == PostStatus.REVIEW
    with pytest.raises(Conflict):
        repo.approve_post(current.id, current.revision, 17, NOW)
    reviewed = repo.revise_post(
        current.id, current.revision, {"profile_version": business.profile_version}, NOW
    )
    assert reviewed.revision == current.revision + 1
    assert repo.approve_post(reviewed.id, reviewed.revision, 17, NOW).status == PostStatus.APPROVED


def test_v2_failed_migration_rolls_back_ddl_and_version(data):
    repo, business, _, values = data
    repo.create_posts(business.id, [values], NOW)
    legacy_v2(repo)
    before = raw_snapshot(repo)
    original = repo._migrate_v2_to_v3

    def fail(conn):
        original(conn)
        raise RuntimeError("Simulated interruption")

    with patch.object(repo, "_migrate_v2_to_v3", side_effect=fail), pytest.raises(RuntimeError):
        repo.initialize()
    assert raw_snapshot(repo) == before
    assert repo.diagnostics()["schema_version"] == 2
    with repo._reader() as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='ai_operations'").fetchone() is None
    repo.initialize()
    assert repo.diagnostics()["schema_version"] == SCHEMA_VERSION


def test_provenance_survives_revisions_and_unknown_sources_roll_back(data):
    repo, business, source, values = data
    post = repo.create_posts(business.id, [values], NOW)[0]
    assert post.profile_version == business.profile_version
    assert post.used_source_ids == [source.id]
    changed = repo.revise_post(post.id, 1, {"scheduled_at": NOW + timedelta(hours=1)}, NOW)
    assert changed.warnings == post.warnings
    assert changed.used_source_ids == post.used_source_ids
    with pytest.raises(InvalidInput):
        repo.revise_post(post.id, changed.revision, {"used_source_ids": ["unknown"]}, NOW)
    assert repo.get_post(post.id) == changed
    with repo._reader() as conn:
        snapshot = json.loads(
            conn.execute(
                "SELECT data FROM post_revisions WHERE post_id=? AND revision=1", (post.id,)
            ).fetchone()[0]
        )
    assert snapshot["warnings"] == post.warnings


def test_another_business_source_cannot_be_attached_to_post(data):
    repo, business, _, values = data
    other = repo.create_business(17, "Other", "UTC", NOW)
    source = Source(
        id=str(uuid4()), business_id=other.id, kind="text", name="Other", text="Other", created_at=NOW
    )
    repo.add_source(source)
    with pytest.raises(InvalidInput):
        repo.create_posts(business.id, [{**values, "used_source_ids": [source.id]}], NOW)
    assert repo.list_posts(business.id) == []


def test_profile_change_invalidates_approval_without_silently_refreshing_text(data):
    repo, business, _, values = data
    post = repo.create_posts(business.id, [values], NOW)[0]
    repo.approve_post(post.id, post.revision, 17, NOW)
    changed = repo.set_profile(business.id, BusinessProfile(name="Cafe", description="New facts"), NOW)
    repo.confirm_profile(business.id, changed.profile_version, NOW)
    assert repo.claim_due_post(NOW + timedelta(minutes=1)) is None
    stale = repo.get_post(post.id)
    assert stale.status == PostStatus.REVIEW and stale.approved_revision is None
    moved = repo.revise_post(post.id, stale.revision, {"scheduled_at": NOW + timedelta(hours=1)}, NOW)
    assert moved.profile_version == post.profile_version
    with pytest.raises(Conflict):
        repo.approve_post(post.id, moved.revision, 17, NOW)


def test_publication_pause_stops_new_claims_and_preserves_approved_content(data):
    repo, business, _, values = data
    post = repo.create_posts(business.id, [values], NOW)[0]
    approved = repo.approve_post(post.id, post.revision, 17, NOW)
    repo.set_publications_paused(business.id, True, NOW)
    assert Repository(repo.db_path).get_business(business.id).publications_paused
    assert repo.claim_due_post(NOW + timedelta(hours=1)) is None
    assert repo.get_post(post.id) == approved
    with pytest.raises(InvalidInput):
        repo.set_publications_paused(business.id, "false", NOW)
    repo.set_publications_paused(business.id, False, NOW)
    claimed = repo.claim_due_post(NOW + timedelta(hours=1))
    assert claimed.id == post.id
    assert claimed.scheduled_at == approved.scheduled_at  # service still checks lateness
    repo.set_publications_paused(business.id, True, NOW)
    assert repo.get_post(post.id).status == PostStatus.PUBLISHING


def test_cancel_pending_and_claim_are_mutually_exclusive(data):
    repo, business, _, _ = data
    job = repo.enqueue_job(business.id, "Private request", "job", NOW)

    def cancel():
        try:
            return repo.cancel_job(job.id, NOW)
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cancel)
        second = pool.submit(repo.claim_job, NOW)
        cancelled, claimed = first.result(), second.result()
    assert (cancelled is None) != (claimed is None)
    stored = repo.get_job(job.id)
    if stored.status == "cancelled":
        assert repo.cancel_job(job.id, NOW) == stored
        assert repo.claim_job(NOW) is None
    else:
        with pytest.raises(Conflict):
            repo.cancel_job(job.id, NOW)
    assert repo.ai_usage(17, NOW)["used_today"] == 0


def test_progress_persists_and_terminal_jobs_cannot_be_updated(data):
    repo, business, _, _ = data
    job = repo.enqueue_job(business.id, "Request", "job", NOW)
    with pytest.raises(Conflict):
        repo.set_job_progress(job.id, "text", 0, 2, NOW)
    repo.claim_job(NOW)
    updated = repo.set_job_progress(job.id, "image", 1, 2, NOW + timedelta(seconds=5))
    assert Repository(repo.db_path).get_job(job.id) == updated
    assert (updated.stage, updated.processed, updated.total) == ("image", 1, 2)
    with pytest.raises(InvalidInput):
        repo.set_job_progress(job.id, "image", 3, 2, NOW)
    assert repo.set_job_progress(job.id, "saving", 11, 11, NOW).processed == 11
    with pytest.raises(InvalidInput):
        repo.set_job_progress(job.id, "saving", 12, 12, NOW)
    failed = repo.fail_job(job.id, "Safe failure", NOW)
    assert failed.stage == "failed"
    with pytest.raises(Conflict):
        repo.set_job_progress(job.id, "text", 1, 2, NOW)


def test_operation_cache_is_durable_and_replay_does_not_consume_quota(data):
    repo, business, _, _ = data
    operation = begin(repo, business, daily=1)
    assert operation["created"]
    repo.finish_ai_operation(operation["id"], {"answer": "private answer"}, NOW)
    replay = begin(Repository(repo.db_path), business, daily=1)
    assert not replay["created"] and replay["result"] == {"answer": "private answer"}
    assert repo.ai_usage(17, NOW)["used_today"] == 1
    with pytest.raises(Conflict):
        begin(repo, business, key="new", daily=1)
    with pytest.raises(Conflict):
        begin(repo, business, kind="revise_text")
    with pytest.raises(Conflict):
        begin(repo, business, digest="b" * 64)
    with pytest.raises(Conflict):
        repo.fail_ai_operation(operation["id"], "provider_failed", NOW)


def test_operation_key_cannot_be_reused_for_another_business(data):
    repo, business, _, _ = data
    begin(repo, business)
    other = repo.create_business(17, "Other", "UTC", NOW)
    with pytest.raises(Conflict):
        begin(repo, other)
    with pytest.raises(Forbidden):
        repo.begin_ai_operation(
            18, business.id, "interpret", "x", "a" * 64, NOW, daily_limit=3, max_parallel=1
        )


def test_atomic_owner_quota_and_parallel_limits_across_connections(data):
    repo, business, _, _ = data

    def reserve(i):
        try:
            return begin(Repository(repo.db_path), business, key=f"parallel:{i}", daily=2, parallel=2)
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert sum(result is not None for result in results) == 2
    assert repo.ai_usage(17, NOW)["running"] == 2
    assert repo.ai_usage(17, NOW)["used_today"] == 2


def test_failed_and_interrupted_operations_keep_quota_until_next_utc_day(data):
    repo, business, _, _ = data
    first = begin(repo, business, key="first", parallel=1)
    with pytest.raises(Conflict):
        begin(repo, business, key="second", parallel=1)
    repo.fail_ai_operation(first["id"], "provider_failed", NOW)
    second = begin(repo, business, key="second", daily=2)
    repo.recover_interrupted(NOW + timedelta(seconds=2))
    replay = begin(repo, business, key="second", daily=2)
    assert not replay["created"] and replay["error_code"] == "interrupted"
    assert replay["id"] == second["id"]
    assert repo.ai_usage(17, NOW)["failed_today"] == 2
    with pytest.raises(Conflict):
        begin(repo, business, key="third", daily=2)
    assert begin(repo, business, key="nextday", now=NOW + timedelta(days=1), daily=2)["created"]


def test_notification_retry_only_requeues_notification_and_is_audited_once(data):
    repo, business, _, values = data
    post = repo.create_posts(business.id, [values], NOW)[0]
    job = repo.enqueue_job(business.id, "Request", "job", NOW)
    notification = Notification(
        id=str(uuid4()),
        business_id=business.id,
        owner_id=17,
        kind="test",
        text="Private notice",
        post_id=post.id,
        job_id=job.id,
        created_at=NOW,
    )
    repo.add_notification(notification)
    for _ in range(5):
        repo.record_notification_failure(notification.id, NOW)
    metadata = repo.get_failed_notification(notification.id)
    assert metadata["attempts"] == 5 and "text" not in metadata
    assert repo.list_failed_notifications(business.id) == [metadata]
    assert repo.retry_notification(notification.id, business.id, NOW) == notification
    assert repo.retry_notification(notification.id, business.id, NOW) == notification
    assert repo.get_post(post.id) == post and repo.get_job(job.id) == job
    assert repo.pending_notifications(20, NOW) == [notification]
    assert not repo.list_failed_notifications(business.id)
    with pytest.raises(NotFound):
        repo.get_failed_notification(notification.id)
    other = repo.create_business(17, "Other", "UTC", NOW)
    with pytest.raises(Forbidden):
        repo.retry_notification(notification.id, other.id, NOW)
    repo.acknowledge_notification(notification.id, NOW)
    with pytest.raises(Conflict):
        repo.retry_notification(notification.id, business.id, NOW)
    with repo._reader() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit WHERE entity_id=? AND action='manual_retry'", (notification.id,)
            ).fetchone()[0]
            == 1
        )


def test_diagnostics_never_exposes_operation_payload_or_results(data):
    repo, business, _, _ = data
    operation = begin(repo, business, key="private-secret-request-id")
    repo.finish_ai_operation(operation["id"], {"secret": "private-secret-provider-output"}, NOW)
    second = begin(repo, business, key="second")
    repo.fail_ai_operation(second["id"], "provider_failed", NOW)
    diagnostics = repo.diagnostics()
    encoded = json.dumps(diagnostics)
    assert "private-secret" not in encoded and business.name not in encoded
    assert "a" * 64 not in encoded
    details = diagnostics["details"]["ai_operations"]
    assert {entry["id"] for entry in details} == {operation["id"], second["id"]}
    assert "provider_failed" in encoded
    assert repo.list_ai_operations(17, business.id)[0]["id"] in {operation["id"], second["id"]}


def test_inbound_receipt_survives_restart_and_cannot_change_payload(data):
    repo, _, _, _ = data
    assert repo.claim_inbound_event(17, "telegram:123:456", "a" * 64, NOW)
    reopened = Repository(repo.db_path)
    assert not reopened.claim_inbound_event(17, "telegram:123:456", "a" * 64, NOW)
    with pytest.raises(Conflict):
        reopened.claim_inbound_event(17, "telegram:123:456", "b" * 64, NOW)
    assert reopened.claim_inbound_event(17, "telegram:999:456", "a" * 64, NOW)
    assert repo.ai_usage(17, NOW)["used_today"] == 0
    assert "telegram:123:456" not in json.dumps(repo.diagnostics())


def test_inbound_claim_is_atomic_across_workers(data):
    repo, _, _, _ = data

    def claim(_):
        return Repository(repo.db_path).claim_inbound_event(17, "telegram:123:789", "a" * 64, NOW)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(8)))
    assert results.count(True) == 1
    assert results.count(False) == 7
    assert repo.ai_usage(17, NOW)["used_today"] == 0


@pytest.mark.parametrize(
    "owner,key,digest",
    [(True, "event", "a" * 64), (17, "", "a" * 64), (17, "x" * 201, "a" * 64), (17, "event", "raw-payload")],
)
def test_inbound_receipt_validates_metadata_without_persisting_payload(data, owner, key, digest):
    repo, _, _, _ = data
    with pytest.raises(InvalidInput):
        repo.claim_inbound_event(owner, key, digest, NOW)
