import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from app.core.demo import run_demo
from app.core.instance_lock import InstanceLock
from app.ops.backup import backup_data, restore_data, verify_backup
from app.shared.errors import Conflict, InvalidInput
from app.shared.models import PostStatus
from app.storage import FileStore, Repository
from app.storage.repository import SCHEMA_VERSION
from tests.ops.test_cli_ops import command


@pytest.fixture
def data(tmp_path):
    source = tmp_path / "live data"
    asyncio.run(run_demo(source))
    (source / ".env").write_text("PRIVATE_SECRET=never-copy-this", encoding="utf-8")
    repo = Repository(source / "backend.sqlite3")
    business = repo.list_businesses()[0]
    repo.enqueue_job(business.id, "Pending request before backup", "backup-pending", datetime.now(UTC))
    return source, repo, business


def test_backup_restore_preserves_content_and_stops_autonomous_replay(data, tmp_path):
    source, repo, business = data
    original_posts = repo.list_posts(business.id)
    before = repo.diagnostics()
    target = tmp_path / "backup one"
    result = backup_data(source, target)
    assert result["external_requests"] == 0
    assert not (target / ".env").exists()
    assert verify_backup(target)["valid"]
    assert repo.diagnostics() == before
    restored = tmp_path / "restored"
    restore_result = restore_data(target, restored)
    assert restore_result["publications_paused"]
    assert restore_result["automation_disabled"]
    assert restore_result["pending_jobs_cancelled"] == 1
    reopened = Repository(restored / "backend.sqlite3")
    assert reopened.runtime_binding() == repo.runtime_binding()
    assert reopened.list_posts(business.id) == original_posts
    assert reopened.get_business(business.id).publications_paused
    assert not reopened.get_business(business.id).automation_enabled
    assert all(job.status != "pending" for job in reopened.list_jobs(business.id))
    store = FileStore(restored / "assets")
    for post in original_posts:
        assert store.read(reopened.get_asset(post.image_asset_id))
    assert reopened.diagnostics()["schema_version"] == SCHEMA_VERSION
    assert verify_backup(target)["valid"]  # Restore did not modify its source.


def test_backup_refuses_running_process_and_never_overwrites(data, tmp_path):
    source, _, _ = data
    target = tmp_path / "snapshot"
    with InstanceLock(source / "backend.lock"), pytest.raises(Conflict):
        backup_data(source, target)
    assert not target.exists()
    target.mkdir()
    marker = target / "keep"
    marker.write_text("important")
    with pytest.raises(Conflict):
        backup_data(source, target)
    assert marker.read_text() == "important"
    with pytest.raises(InvalidInput):
        backup_data(source, source / "nested")


def test_restore_clears_approval_that_may_have_been_used_after_snapshot(data, tmp_path):
    source, repo, business = data
    now = datetime.now(UTC)
    draft = next(post for post in repo.list_posts(business.id) if post.status == PostStatus.REVIEW)
    draft = repo.revise_post(draft.id, draft.revision, {"scheduled_at": now + timedelta(minutes=2)}, now)
    approved = repo.approve_post(draft.id, draft.revision, business.owner_id, now)
    snapshot, destination = tmp_path / "snapshot", tmp_path / "restored"
    backup_data(source, snapshot)
    result = restore_data(snapshot, destination)
    restored = Repository(destination / "backend.sqlite3")
    current = restored.get_post(approved.id)
    assert result["approvals_cleared"] == 1
    assert current.status == PostStatus.REVIEW
    assert current.revision == approved.revision + 1
    assert current.approved_revision is None
    assert current.text == approved.text and current.image_asset_id == approved.image_asset_id
    restored.set_publications_paused(business.id, False, now)
    assert restored.claim_due_post(now + timedelta(minutes=3)) is None
    assert repo.get_post(approved.id) == approved


@pytest.mark.parametrize("corruption", ["database", "asset", "missing", "traversal", "journal"])
def test_restore_rejects_tampering_without_partial_destination(data, tmp_path, corruption):
    source, _, _ = data
    snapshot = tmp_path / "snapshot"
    backup_data(source, snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text())
    asset = next(name for name in manifest["files"] if name.startswith("assets/"))
    if corruption == "database":
        (snapshot / "backend.sqlite3").write_bytes(b"broken")
    elif corruption == "asset":
        (snapshot / asset).write_bytes(b"tampered")
    elif corruption == "missing":
        (snapshot / asset).unlink()
    elif corruption == "journal":
        (snapshot / "backend.sqlite3-wal").write_bytes(b"unverified-journal")
    else:
        manifest["files"]["../outside"] = "0" * 64
        (snapshot / "manifest.json").write_text(json.dumps(manifest))
    target = tmp_path / "restored"
    with pytest.raises((Conflict, InvalidInput)):
        restore_data(snapshot, target)
    assert not target.exists()
    assert list(tmp_path.glob(".marketer-snapshot-*")) == []


def test_legacy_snapshot_restores_with_migration_without_changing_backup(data, tmp_path):
    source, _, business = data
    with closing(sqlite3.connect(source / "backend.sqlite3")) as conn:
        conn.execute("DROP TABLE ai_operations")
        conn.execute("DROP TABLE inbound_events")
        conn.execute("PRAGMA user_version=2")
        conn.commit()
    snapshot = tmp_path / "legacy-snapshot"
    assert backup_data(source, snapshot)["schema_version"] == 2
    restored = tmp_path / "restored-legacy"
    assert restore_data(snapshot, restored)["schema_version"] == SCHEMA_VERSION
    repo = Repository(restored / "backend.sqlite3")
    assert repo.get_business(business.id).publications_paused
    assert verify_backup(snapshot)["schema_version"] == 2


def test_offline_cli_backup_verify_restore_ignore_credentials(data, tmp_path):
    source, _, _ = data
    backup = tmp_path / "cli backup"
    result = command("backup", "--data-dir", str(source), "--output", str(backup))
    assert result.returncode == 0, result.stderr
    result = command("verify-backup", "--backup-dir", str(backup))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["valid"]
    restored = tmp_path / "cli restored"
    result = command("restore", "--backup-dir", str(backup), "--data-dir", str(restored))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["publications_paused"]
    assert command("restore", "--backup-dir", str(backup), "--data-dir", str(restored)).returncode == 1


def test_symlink_asset_cannot_escape_snapshot(data, tmp_path):
    source, _, _ = data
    snapshot = tmp_path / "snapshot"
    backup_data(source, snapshot)
    blob = next((snapshot / "assets").glob("*.blob"))
    outside = tmp_path / "outside"
    outside.write_bytes(blob.read_bytes())
    blob.unlink()
    try:
        blob.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this host")
    with pytest.raises(InvalidInput):
        verify_backup(snapshot)


@pytest.mark.parametrize(
    "args",
    [
        ("backup",),
        ("restore",),
        ("verify-backup",),
        ("status", "--output", "x"),
        ("demo", "--backup-dir", "x"),
        ("verify-backup", "--data-dir", "x"),
    ],
)
def test_cli_requires_explicit_unambiguous_paths(args):
    assert command(*args).returncode != 0
