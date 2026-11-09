"""Verified database + registered-asset snapshots under the application lock.

Backups contain private business data, never environment files or API keys.
Restore only writes a new directory and disables future autonomous actions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path

from app.core.instance_lock import InstanceLock
from app.shared.errors import Conflict, InvalidInput, NotFound
from app.shared.models import Asset
from app.storage import FileStore, Repository
from app.storage.repository import SCHEMA_VERSION

FORMAT_VERSION = 1
MAX_DATABASE_BYTES = 2_000_000_000
MAX_MANIFEST_BYTES = 16_000_000
MAX_ASSETS = 100_000


def _path(value: str | Path) -> Path:
    path = Path(value).absolute()
    FileStore._check_components(path)
    return path.resolve()


@contextmanager
def _read_database(path: Path, *, immutable: bool = False):
    FileStore._check_components(path)
    if not path.is_file():
        raise NotFound("Файл backend.sqlite3 отсутствует.")
    uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA trusted_schema=OFF")
        yield conn


def _inventory(path: Path) -> tuple[int, list[Asset]]:
    # Snapshots are self-contained. Never let an unmanifested journal change
    # the state being validated after the database hash was checked.
    for suffix in ("-wal", "-journal"):
        journal = path.with_name(path.name + suffix)
        FileStore._check_components(journal)
        if journal.exists() and journal.stat().st_size:
            raise Conflict("В резервной копии найден несохранённый журнал базы.")
    with _read_database(path, immutable=True) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if not 1 <= version <= SCHEMA_VERSION:
            raise Conflict("Версия резервной копии не поддерживается.")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise Conflict("Проверка целостности базы не пройдена.")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise Conflict("В базе нарушены связи данных.")
        if conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] > MAX_ASSETS:
            raise InvalidInput("Слишком много файлов для одной резервной копии.")
        assets = [Asset.model_validate_json(row[0]) for row in conn.execute("SELECT data FROM assets")]
        return version, assets


def _copy_verified(source: Path, destination: Path | None, *, maximum: int) -> str:
    FileStore._check_components(source)
    before = source.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
        raise InvalidInput("Неверный тип или размер файла резервной копии.")
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as reader:
        opened = os.fstat(reader.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise Conflict("Файл заменён во время проверки.")
        with destination.open("xb") if destination else nullcontext() as writer:
            size = 0
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                size += len(chunk)
                if size > maximum:
                    raise InvalidInput("Размер файла превышает допустимый предел.")
                digest.update(chunk)
                if writer:
                    writer.write(chunk)
            if writer:
                writer.flush()
                os.fsync(writer.fileno())
    if size != before.st_size:
        raise Conflict("Файл изменился во время копирования.")
    return digest.hexdigest()


@contextmanager
def _staging(destination: Path):
    if destination.exists():
        raise Conflict("Каталог назначения уже существует; выберите новый путь.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    FileStore._check_components(destination.parent)
    temporary = Path(tempfile.mkdtemp(prefix=".marketer-snapshot-", dir=destination.parent))
    try:
        yield temporary
        FileStore._check_components(destination)
        if destination.exists():
            raise Conflict("Каталог назначения появился во время операции.")
        temporary.rename(destination)
    finally:
        # Only this operation's known sibling staging directory may be removed.
        if temporary.exists():
            if temporary.resolve().parent != destination.parent or not temporary.name.startswith(
                ".marketer-snapshot-"
            ):
                raise Conflict("Неожиданный путь временной копии.")
            shutil.rmtree(temporary)


def _distinct(source: Path, destination: Path) -> None:
    if source == destination or source in destination.parents or destination in source.parents:
        raise InvalidInput("Источник и назначение должны быть отдельными каталогами.")


def backup_data(data_dir: str | Path, destination: str | Path) -> dict:
    source, target = _path(data_dir), _path(destination)
    _distinct(source, target)
    if not (source / "backend.sqlite3").is_file():
        raise NotFound("В выбранном DATA_DIR нет backend.sqlite3.")
    with InstanceLock(source / "backend.lock"), _staging(target) as stage:
        # SQLite's backup API includes committed WAL content. Holding the instance
        # lock also keeps registered files and metadata stable for one snapshot.
        with _read_database(source / "backend.sqlite3") as original:
            with closing(sqlite3.connect(stage / "backend.sqlite3")) as copied:
                original.backup(copied)
        version, assets = _inventory(stage / "backend.sqlite3")
        (stage / "assets").mkdir()
        files = {
            "backend.sqlite3": _copy_verified(stage / "backend.sqlite3", None, maximum=MAX_DATABASE_BYTES)
        }
        store = FileStore(source / "assets", max_bytes=20_000_000) if assets else None
        for asset in assets:
            content = store.read(asset)
            relative = f"assets/{asset.id}.blob"
            with (stage / relative).open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            files[relative] = asset.sha256
        manifest = {
            "format": FORMAT_VERSION,
            "schema_version": version,
            "created_at": datetime.now(UTC).isoformat(),
            "files": files,
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        verify_backup(stage)
    return {"backup": str(target), "schema_version": version, "assets": len(assets), "external_requests": 0}


def _verified_backup(backup_dir: str | Path) -> tuple[dict, dict]:
    source = _path(backup_dir)
    manifest_path = source / "manifest.json"
    FileStore._check_components(manifest_path)
    if not manifest_path.is_file() or manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise InvalidInput("Отсутствует допустимый манифест резервной копии.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest["files"]
        if (
            manifest["format"] != FORMAT_VERSION
            or not isinstance(files, dict)
            or not 1 <= len(files) <= MAX_ASSETS + 1
        ):
            raise ValueError
        for name, checksum in files.items():
            if name != "backend.sqlite3" and not re.fullmatch(r"assets/[a-f0-9-]{36}\.blob", name):
                raise ValueError
            if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
                raise ValueError
            actual = _copy_verified(
                source / name, None, maximum=MAX_DATABASE_BYTES if name == "backend.sqlite3" else 20_000_000
            )
            if actual != checksum:
                raise Conflict("Контрольная сумма резервной копии не совпадает.")
        version, assets = _inventory(source / "backend.sqlite3")
        expected = {"backend.sqlite3", *(f"assets/{asset.id}.blob" for asset in assets)}
        if set(files) != expected or version != manifest["schema_version"]:
            raise ValueError
        if assets:
            store = FileStore(source / "assets", max_bytes=20_000_000)
            for asset in assets:
                store.read(asset)
        return {
            "valid": True,
            "schema_version": version,
            "assets": len(assets),
            "external_requests": 0,
        }, manifest
    except (KeyError, ValueError, TypeError, OSError, sqlite3.Error):
        raise InvalidInput("Резервная копия повреждена или имеет неподдерживаемую структуру.") from None


def verify_backup(backup_dir: str | Path) -> dict:
    return _verified_backup(backup_dir)[0]


def restore_data(backup_dir: str | Path, destination: str | Path) -> dict:
    source, target = _path(backup_dir), _path(destination)
    _distinct(source, target)
    checked, manifest = _verified_backup(source)
    with _staging(target) as stage:
        (stage / "assets").mkdir()
        for name, checksum in manifest["files"].items():
            actual = _copy_verified(
                source / name,
                stage / name,
                maximum=MAX_DATABASE_BYTES if name == "backend.sqlite3" else 20_000_000,
            )
            if actual != checksum:
                raise Conflict("Копия изменилась во время восстановления.")
        repo = Repository(stage / "backend.sqlite3")
        try:
            repo.initialize()
            now = datetime.now(UTC)
            cancelled = 0
            for business in repo.list_businesses():
                repo.set_publications_paused(business.id, True, now)
                repo.set_automation(
                    business.id, False, business.automation_request, business.interval_hours, None, now
                )
                for job in repo.list_jobs(business.id):
                    if job.status == "pending":
                        repo.cancel_job(job.id, now)
                        cancelled += 1
            # Explicitly failed/unknown, never repeat work from a stale snapshot.
            recovered = repo.recover_interrupted(now)
        finally:
            repo.close()
        (stage / "restore-info.json").write_text(
            json.dumps(
                {
                    "restored_at": now.isoformat(),
                    "source_created_at": manifest["created_at"],
                    "publications_paused": True,
                    "automation_disabled": True,
                    "pending_jobs_cancelled": cancelled,
                    "recovered": recovered,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return {
        **checked,
        "restored_to": str(target),
        "schema_version": SCHEMA_VERSION,
        "publications_paused": True,
        "automation_disabled": True,
        "pending_jobs_cancelled": cancelled,
    }
