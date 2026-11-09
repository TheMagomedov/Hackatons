import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.shared.models import Channel, PostView, TextDraft
from app.shared.ports import AppError


SCHEMA = """
CREATE TABLE IF NOT EXISTS business (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner_id INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    profile TEXT,
    profile_revision INTEGER NOT NULL DEFAULT 0,
    confirmed INTEGER NOT NULL DEFAULT 0,
    channel TEXT,
    auto_days INTEGER,
    auto_next TEXT,
    auto_brief TEXT
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    body TEXT NOT NULL,
    asset TEXT
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    profile TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    error TEXT,
    automatic INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    item_index INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'draft',
    approved_version INTEGER,
    approved_by INTEGER,
    error TEXT,
    message_id INTEGER,
    publication_link TEXT,
    published_at TEXT,
    UNIQUE (plan_id, item_index)
);
CREATE TABLE IF NOT EXISTS versions (
    post_id INTEGER NOT NULL REFERENCES posts(id),
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    image TEXT NOT NULL,
    channel TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    scheduled_at TEXT NOT NULL,
    PRIMARY KEY (post_id, version)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    event TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automatic_runs (
    period TEXT PRIMARY KEY,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reaction_samples (
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    measured_at TEXT NOT NULL,
    source TEXT NOT NULL,
    total INTEGER NOT NULL,
    PRIMARY KEY (channel_id, message_id, measured_at, source)
);
CREATE TABLE IF NOT EXISTS subscriber_samples (
    channel_id INTEGER NOT NULL,
    measured_at TEXT NOT NULL,
    source TEXT NOT NULL,
    total INTEGER NOT NULL,
    PRIMARY KEY (channel_id, measured_at, source)
);
CREATE TABLE IF NOT EXISTS updates (
    request_key TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS events_at ON events(at);
"""

POST_QUERY = """
SELECT p.*, v.body, v.image, v.channel, v.scheduled_at
FROM posts p JOIN versions v ON v.post_id = p.id AND v.version = p.version
"""


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def as_post(row: sqlite3.Row) -> PostView:
    return PostView(
        id=row["id"], plan_id=row["plan_id"], version=row["version"],
        status=row["status"], draft=TextDraft.model_validate_json(row["body"]),
        image_id=row["image"], channel=Channel.model_validate_json(row["channel"]),
        scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
        error=row["error"], link=row["publication_link"],
    )


class Database:
    def __init__(self, root: Path, owner_id: int):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "market.db"
        self.assets = root / "assets"
        self.assets.mkdir(exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT OR IGNORE INTO business(id, owner_id) VALUES (1, ?)", (owner_id,))
            if db.execute("SELECT owner_id FROM business").fetchone()[0] != owner_id:
                raise AppError("OWNER_ID отличается от владельца этой базы. Проверьте настройки.")

    @contextmanager
    def connect(self, *, write: bool = False):
        db = sqlite3.connect(self.path, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def save_asset(self, data: bytes, extension: str) -> str:
        name = f"{hashlib.sha256(data).hexdigest()}.{extension}"
        path = self.assets / name
        if not path.exists() or path.read_bytes() != data:
            temporary = self.assets / f"{uuid4().hex}.tmp"
            try:
                temporary.write_bytes(data)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return name

    def asset_path(self, name: str) -> Path:
        return self.assets / name

    def read_asset(self, name: str) -> bytes:
        try:
            data = self.asset_path(name).read_bytes()
        except OSError as exc:
            raise AppError("Файл изображения недоступен.") from exc
        if hashlib.sha256(data).hexdigest() != name.split(".", 1)[0]:
            raise AppError("Файл изображения изменился. Запросите новую картинку.")
        return data

    @staticmethod
    def event(db: sqlite3.Connection, event: str, now: datetime) -> None:
        db.execute("INSERT INTO events(event, at) VALUES (?, ?)", (event, stamp(now)))
