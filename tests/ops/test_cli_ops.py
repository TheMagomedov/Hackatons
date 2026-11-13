"""Offline operator commands must inspect/adopt existing data without API keys."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.instance_lock import InstanceLock
from app.storage import Repository
from app.storage.repository import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 9, tzinfo=UTC)


def command(*args):
    env = dict(os.environ)
    # Deliberately invalid live configuration must not affect these offline commands.
    env.update(
        APP_MODE="live",
        OWNER_TELEGRAM_ID="invalid",
        TELEGRAM_BOT_TOKEN="operator-secret-marker",
        PYTHONUTF8="1",
    )
    result = subprocess.run(
        [sys.executable, "-m", "app", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert "operator-secret-marker" not in result.stdout + result.stderr
    return result


def database(directory: Path, *, legacy=False):
    directory.mkdir(parents=True, exist_ok=True)
    repo = Repository(directory / "backend.sqlite3")
    repo.initialize()
    business = repo.create_business(77, "private-business-name-must-not-be-exposed", "UTC", NOW)
    if legacy:
        with closing(sqlite3.connect(directory / "backend.sqlite3")) as conn:
            conn.execute("DROP TABLE conversation_turns")
            conn.execute("DROP TABLE ai_operations")
            conn.execute("DROP TABLE inbound_events")
            conn.execute("DROP TABLE runtime_binding")
            conn.execute("PRAGMA user_version=1")
            conn.commit()
    return repo, business


def version(directory):
    with closing(sqlite3.connect(directory / "backend.sqlite3")) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def test_status_reads_legacy_without_migrating_or_loading_settings(tmp_path):
    directory = tmp_path / "legacy"
    _, business = database(directory, legacy=True)
    result = command("status", "--data-dir", str(directory))
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["schema_version"] == 1
    assert output["binding"] is None
    assert output["counts"]["businesses"] == 1
    assert business.name not in result.stdout
    assert version(directory) == 1


def test_status_missing_database_does_not_create_one(tmp_path):
    directory = tmp_path / "missing"
    result = command("status", "--data-dir", str(directory))
    assert result.returncode == 1
    assert "not_found" in result.stderr
    assert not directory.exists()


def test_operator_commands_require_explicit_destination_and_adoption_identity(tmp_path):
    assert command("status").returncode == 1
    directory = tmp_path / "legacy"
    database(directory, legacy=True)
    for flags in ([], ["--mode", "live"], ["--owner-id", "77"]):
        result = command("adopt-data", "--data-dir", str(directory), *flags)
        assert result.returncode == 1
        assert version(directory) == 1


def test_adopt_legacy_keeps_data_and_locks_mode_and_owner(tmp_path):
    directory = tmp_path / "legacy"
    _, business = database(directory, legacy=True)
    result = command("adopt-data", "--data-dir", str(directory), "--mode", "live", "--owner-id", "77")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["external_requests"] == 0
    repo = Repository(directory / "backend.sqlite3")
    assert repo.runtime_binding() == {"mode": "live", "owner_id": 77}
    assert repo.get_business(business.id) == business
    assert version(directory) == SCHEMA_VERSION
    # Adoption is repeatable for the same identity, but never a mode-switch override.
    assert (
        command("adopt-data", "--data-dir", str(directory), "--mode", "live", "--owner-id", "77").returncode
        == 0
    )
    for mode, owner in (("demo", "77"), ("live", "88")):
        assert (
            command(
                "adopt-data", "--data-dir", str(directory), "--mode", mode, "--owner-id", owner
            ).returncode
            == 1
        )
        assert repo.runtime_binding() == {"mode": "live", "owner_id": 77}


def test_adopt_refuses_while_worker_holds_directory_lock(tmp_path):
    directory = tmp_path / "legacy"
    database(directory, legacy=True)
    with InstanceLock(directory / "backend.lock"):
        result = command("adopt-data", "--data-dir", str(directory), "--mode", "live", "--owner-id", "77")
    assert result.returncode == 1
    assert "conflict" in result.stderr
    assert version(directory) == 1


@pytest.mark.parametrize(
    "args", [("demo", "--mode", "live"), ("check", "--owner-id", "77"), ("status", "--once")]
)
def test_no_misleading_ignored_operator_flags(args):
    assert command(*args).returncode == 2
