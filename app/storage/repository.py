"""SQLite storage with immutable revisions and transactional publication claims.

All public methods are synchronous and open short-lived connections. Callers may
run them in a worker thread. Remote/provider calls must happen outside a method.
The schema supports a single application process; SQLite CAS also protects
against concurrent workers in that process. Interrupted sends are deliberately
not retried: an operator must resolve their delivery status.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.shared.errors import Conflict, Forbidden, InvalidInput, NotFound
from app.shared.models import (
    Asset,
    Business,
    BusinessProfile,
    ChannelInfo,
    ContentPlan,
    Job,
    MetricSnapshot,
    Notification,
    PostStatus,
    PostView,
    PublicationResult,
    ReportInput,
    Source,
    TaskIntent,
)
from app.storage.access import AccessRepository
from app.storage.api_keys import APIKeyRepository

SCHEMA_VERSION = 7
MAX_NOTIFICATION_ATTEMPTS = 5


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidInput("Требуется дата с часовым поясом")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _dump(value: Any) -> str:
    return value.model_dump_json()


def _new_id() -> str:
    return str(uuid4())


class Repository(AccessRepository, APIKeyRepository):
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._uri = self.db_path == ":memory:"
        self._target = (
            f"file:ai-marketer-{uuid4().hex}?mode=memory&cache=shared" if self._uri else self.db_path
        )
        self._anchor: sqlite3.Connection | None = None
        if self._uri:
            self._anchor = self._connect()

    def close(self) -> None:
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        target = self._target
        use_uri = self._uri
        if read_only and not self._uri:
            target = Path(self.db_path).resolve().as_uri() + "?mode=ro"
            use_uri = True
        conn = sqlite3.connect(target, timeout=5, isolation_level=None, uri=use_uri)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def _transaction(self, *, schema_change: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            if schema_change:
                # Only this migration connection suspends FK enforcement. The
                # entire rebuilt schema is checked before the atomic commit.
                conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            if schema_change and conn.execute("PRAGMA foreign_key_check").fetchone():
                raise Conflict("Миграция нарушает связи данных")
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise Conflict("Операция нарушает целостность сохранённых данных") from exc
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _reader(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self._connect(read_only=read_only)
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        if not self._uri:
            Path(self.db_path).absolute().parent.mkdir(parents=True, exist_ok=True)
        with self._reader() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise Conflict("Версия базы новее поддерживаемой приложением")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
        with self._transaction(schema_change=True) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise Conflict("Версия базы новее поддерживаемой приложением")
            if version == SCHEMA_VERSION:
                return
            if version == 1:
                self._migrate_v1_to_v2(conn)
                version = 2
            if version == 2:
                self._migrate_v2_to_v3(conn)
                version = 3
            if version == 3:
                self._migrate_v3_to_v4(conn)
                version = 4
            if version == 4:
                self._migrate_v4_to_v5(conn)
                version = 5
            if version == 5:
                self._migrate_v5_to_v6(conn)
                version = 6
            if version == 6:
                self._migrate_v6_to_v7(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                return
            if version != 0:
                raise Conflict("Неподдерживаемая версия базы")
            # Individual execute calls preserve the enclosing transaction;
            # executescript would implicitly commit it.
            statements = [
                "CREATE TABLE businesses (id TEXT PRIMARY KEY, data TEXT NOT NULL)",
                "CREATE TABLE assets (id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES businesses(id), data TEXT NOT NULL)",
                "CREATE TABLE sources (id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES businesses(id), asset_id TEXT REFERENCES assets(id), data TEXT NOT NULL)",
                "CREATE TABLE posts (id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES businesses(id), revision INTEGER NOT NULL CHECK(revision > 0), status TEXT NOT NULL, scheduled_at TEXT, channel_id TEXT NOT NULL, image_asset_id TEXT NOT NULL REFERENCES assets(id), data TEXT NOT NULL)",
                "CREATE INDEX posts_due ON posts(status, scheduled_at)",
                "CREATE INDEX posts_business ON posts(business_id)",
                "CREATE TABLE post_revisions (post_id TEXT NOT NULL REFERENCES posts(id), revision INTEGER NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(post_id, revision))",
                "CREATE TRIGGER immutable_revision_update BEFORE UPDATE ON post_revisions BEGIN SELECT RAISE(ABORT, 'immutable revision'); END",
                "CREATE TRIGGER immutable_revision_delete BEFORE DELETE ON post_revisions BEGIN SELECT RAISE(ABORT, 'immutable revision'); END",
                "CREATE TABLE jobs (id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES businesses(id), dedup_key TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(business_id, dedup_key))",
                "CREATE INDEX jobs_pending ON jobs(status, created_at)",
                "CREATE TABLE plans (job_id TEXT PRIMARY KEY REFERENCES jobs(id), business_id TEXT NOT NULL REFERENCES businesses(id), data TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE notifications (id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES businesses(id), created_at TEXT NOT NULL, data TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, acknowledged_at TEXT, last_attempt_at TEXT)",
                "CREATE INDEX notifications_pending ON notifications(acknowledged_at, next_attempt_at)",
                "CREATE TABLE metrics (business_id TEXT NOT NULL REFERENCES businesses(id), event_id TEXT NOT NULL, name TEXT NOT NULL, post_id TEXT REFERENCES posts(id), source TEXT NOT NULL, observed_at TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(business_id, event_id))",
                "CREATE INDEX metrics_period ON metrics(business_id, observed_at)",
                "CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL, details TEXT NOT NULL)",
                "CREATE INDEX audit_entity ON audit(entity_type, entity_id, id)",
            ]
            for statement in statements:
                conn.execute(statement)
            self._migrate_v1_to_v2(conn)
            self._migrate_v2_to_v3(conn)
            self._migrate_v3_to_v4(conn)
            self._migrate_v4_to_v5(conn)
            self._migrate_v5_to_v6(conn)
            self._migrate_v6_to_v7(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
        # SQLite's documented create/copy/drop/rename procedure preserves child
        # rows and their references to posts (including immutable revisions).
        conn.execute("CREATE TABLE posts_v7 (id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES businesses(id), revision INTEGER NOT NULL CHECK(revision > 0), status TEXT NOT NULL, scheduled_at TEXT, channel_id TEXT NOT NULL, image_asset_id TEXT REFERENCES assets(id), data TEXT NOT NULL)")
        conn.execute("INSERT INTO posts_v7 SELECT * FROM posts")
        conn.execute("DROP TABLE posts")
        conn.execute("ALTER TABLE posts_v7 RENAME TO posts")
        conn.execute("CREATE INDEX posts_due ON posts(status, scheduled_at)")
        conn.execute("CREATE INDEX posts_business ON posts(business_id)")
        APIKeyRepository.initialize_api_keys(conn)

    @staticmethod
    def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
        """Run inside initialize's transaction; never commit independently."""
        conn.execute(
            "CREATE TABLE runtime_binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
            "mode TEXT NOT NULL CHECK(mode IN ('demo','live')), "
            "owner_id INTEGER NOT NULL CHECK(owner_id>0))"
        )

    @staticmethod
    def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
        # Additive DTO fields use defaults when old JSON is read. Preserve all
        # original data and immutable revisions, including unknown provenance.
        conn.execute(
            "CREATE TABLE ai_operations (id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, "
            "business_id TEXT NOT NULL REFERENCES businesses(id), kind TEXT NOT NULL, "
            "request_key TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result TEXT, error_code TEXT, "
            "UNIQUE(owner_id,request_key))"
        )
        conn.execute("CREATE INDEX ai_operations_usage ON ai_operations(owner_id,created_at,status)")
        conn.execute(
            "CREATE TABLE inbound_events (owner_id INTEGER NOT NULL, event_key TEXT NOT NULL, "
            "digest TEXT NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY(owner_id,event_key))"
        )

    @staticmethod
    def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE conversation_turns (sequence INTEGER PRIMARY KEY, "
            "operation_id TEXT NOT NULL UNIQUE REFERENCES ai_operations(id), "
            "business_id TEXT NOT NULL REFERENCES businesses(id), "
            "user_text TEXT NOT NULL, assistant_text TEXT NOT NULL, "
            "action TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute("CREATE INDEX conversation_business ON conversation_turns(business_id,sequence)")

    def conversation_history(self, business_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Completed turns, bounded and chronological, scoped before reading any text."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise InvalidInput("Размер истории должен быть от 1 до 50")
        with self._reader() as conn:
            self._business(conn, business_id)
            rows = conn.execute(
                "SELECT operation_id AS id,business_id,user_text,assistant_text,action,created_at "
                "FROM conversation_turns WHERE business_id=? ORDER BY sequence DESC LIMIT ?",
                (business_id, limit),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def claim_inbound_event(self, owner_id: int, event_key: str, digest: str, now: datetime) -> bool:
        """Claim an incoming event once, before mutable dialog state routes it.

        Receipts intentionally survive handler failure and process restarts. An
        interrupted user action requires a fresh event, not ambiguous replay.
        """
        now = _utc(now)
        if type(owner_id) is not int or owner_id <= 0:
            raise InvalidInput("Некорректный владелец входящего события")
        if not isinstance(event_key, str) or not 1 <= len(event_key.strip()) <= 200:
            raise InvalidInput("Некорректный ключ входящего события")
        try:
            event_key.encode("utf-8")
        except UnicodeEncodeError:
            raise InvalidInput("Некорректный ключ входящего события") from None
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise InvalidInput("Некорректный отпечаток входящего события")
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT digest FROM inbound_events WHERE owner_id=? AND event_key=?", (owner_id, event_key)
            ).fetchone()
            if existing is not None:
                if existing["digest"] != digest:
                    raise Conflict("Ключ входящего события уже использован с другим содержимым")
                return False
            conn.execute(
                "INSERT INTO inbound_events(owner_id,event_key,digest,received_at) VALUES (?,?,?,?)",
                (owner_id, event_key, digest, _stamp(now)),
            )
            return True

    @staticmethod
    def _runtime_binding(conn: sqlite3.Connection) -> dict[str, Any] | None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_binding'"
        ).fetchone()
        if exists is None:
            return None  # Read-only diagnostics also supports an unmigrated v1.
        row = conn.execute("SELECT mode,owner_id FROM runtime_binding WHERE singleton=1").fetchone()
        return dict(row) if row is not None else None

    def runtime_binding(self) -> dict[str, Any] | None:
        with self._reader(read_only=True) as conn:
            return self._runtime_binding(conn)

    def bind_runtime(self, mode: str, owner_id: int, *, adopt_legacy: bool = False) -> dict[str, Any]:
        if not isinstance(mode, str) or mode not in {"demo", "live"}:
            raise InvalidInput("Режим приложения должен быть demo или live")
        if type(adopt_legacy) is not bool:
            raise InvalidInput("Принятие legacy-данных требует явного логического флага")
        if not isinstance(owner_id, int) or isinstance(owner_id, bool) or owner_id <= 0:
            raise InvalidInput("Некорректный владелец приложения")
        requested = {"mode": mode, "owner_id": owner_id}
        with self._transaction() as conn:
            existing = self._runtime_binding(conn)
            if existing is not None and existing != requested:
                raise Conflict(
                    "DATA_DIR уже привязан к другому режиму или владельцу; выберите соответствующую папку данных"
                )
            businesses = [
                Business.model_validate_json(row["data"])
                for row in conn.execute("SELECT data FROM businesses")
            ]
            if any(business.owner_id != owner_id for business in businesses):
                raise Conflict("Владелец сохранённых бизнесов не совпадает с OWNER_TELEGRAM_ID")
            if existing is not None:
                return existing
            if businesses and not adopt_legacy:
                raise Conflict(
                    "Существующие данные ещё не привязаны к runtime. Проверьте режим и владельца и выполните явное принятие legacy-данных"
                )
            if businesses and mode == "demo":
                if any(
                    not PostView.model_validate_json(row["data"]).is_demo
                    for row in conn.execute("SELECT data FROM posts")
                ):
                    raise Conflict(
                        "Legacy-данные содержат реальные публикации; принятие в demo-режиме запрещено"
                    )
            conn.execute(
                "INSERT INTO runtime_binding(singleton,mode,owner_id) VALUES (1,?,?)", (mode, owner_id)
            )
            self._audit(
                conn,
                "runtime",
                "singleton",
                "bound",
                datetime.now(timezone.utc),
                mode=mode,
                owner_id=owner_id,
                adopted_legacy=bool(businesses),
            )
            return requested

    def diagnostics(self) -> dict[str, Any]:
        """Read-only metadata/counts, including for populated unmigrated v1 data."""
        with self._reader(read_only=True) as conn:
            conn.execute("BEGIN")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            counts = {
                "businesses": conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0],
                "posts": {
                    row["status"]: row["count"]
                    for row in conn.execute("SELECT status,COUNT(*) AS count FROM posts GROUP BY status")
                },
                "jobs": {
                    row["status"]: row["count"]
                    for row in conn.execute("SELECT status,COUNT(*) AS count FROM jobs GROUP BY status")
                },
                "notifications": {
                    "pending": conn.execute(
                        "SELECT COUNT(*) FROM notifications WHERE acknowledged_at IS NULL AND attempts<?",
                        (MAX_NOTIFICATION_ATTEMPTS,),
                    ).fetchone()[0],
                    "exhausted": conn.execute(
                        "SELECT COUNT(*) FROM notifications WHERE acknowledged_at IS NULL AND attempts>=?",
                        (MAX_NOTIFICATION_ATTEMPTS,),
                    ).fetchone()[0],
                    "delivered": conn.execute(
                        "SELECT COUNT(*) FROM notifications WHERE acknowledged_at IS NOT NULL"
                    ).fetchone()[0],
                },
            }
            operation_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_operations'"
            ).fetchone()
            operations = []
            if operation_table is not None:
                counts["ai_operations"] = {
                    row["status"]: row["count"]
                    for row in conn.execute(
                        "SELECT status,COUNT(*) AS count FROM ai_operations GROUP BY status"
                    )
                }
                operations = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT id,business_id,kind,status,created_at,updated_at,error_code "
                        "FROM ai_operations ORDER BY created_at DESC,id DESC LIMIT 20"
                    )
                ]
            jobs = []
            for row in conn.execute(
                "SELECT data FROM jobs WHERE status IN ('pending','running','failed') ORDER BY created_at DESC,id DESC LIMIT 20"
            ):
                job = Job.model_validate_json(row["data"])
                jobs.append(
                    {
                        "id": job.id,
                        "business_id": job.business_id,
                        "status": job.status,
                        "stage": job.stage,
                        "processed": job.processed,
                        "total": job.total,
                        "updated_at": _stamp(
                            job.updated_at or job.finished_at or job.started_at or job.created_at
                        ),
                        "error_code": "preparation_failed" if job.status == "failed" else None,
                    }
                )
            notifications = [
                dict(row)
                for row in conn.execute(
                    "SELECT id,business_id,attempts,last_attempt_at FROM notifications "
                    "WHERE acknowledged_at IS NULL AND attempts>=? ORDER BY created_at DESC,id DESC LIMIT 20",
                    (MAX_NOTIFICATION_ATTEMPTS,),
                )
            ]
            last_activity = conn.execute("SELECT MAX(at) FROM audit").fetchone()[0]
            return {
                "schema_version": version,
                "binding": self._runtime_binding(conn),
                "counts": counts,
                "recent_activity_at": last_activity,
                "details": {
                    "jobs": jobs,
                    "ai_operations": operations,
                    "exhausted_notifications": notifications,
                },
            }

    @staticmethod
    def _audit(
        conn: sqlite3.Connection, entity_type: str, entity_id: str, action: str, now: datetime, **details: Any
    ) -> None:
        conn.execute(
            "INSERT INTO audit(entity_type, entity_id, action, at, details) VALUES (?, ?, ?, ?, ?)",
            (entity_type, entity_id, action, _stamp(now), json.dumps(details, ensure_ascii=False)),
        )

    @staticmethod
    def _business(conn: sqlite3.Connection, business_id: str) -> Business:
        row = conn.execute("SELECT data FROM businesses WHERE id = ?", (business_id,)).fetchone()
        if row is None:
            raise NotFound("Бизнес не найден")
        return Business.model_validate_json(row["data"])

    @staticmethod
    def _post(conn: sqlite3.Connection, post_id: str) -> PostView:
        row = conn.execute("SELECT data FROM posts WHERE id = ?", (post_id,)).fetchone()
        if row is None:
            raise NotFound("Публикация не найдена")
        return PostView.model_validate_json(row["data"])

    @staticmethod
    def _job(conn: sqlite3.Connection, job_id: str) -> Job:
        row = conn.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFound("Задание не найдено")
        return Job.model_validate_json(row["data"])

    @staticmethod
    def _save_business(conn: sqlite3.Connection, business: Business) -> Business:
        conn.execute("UPDATE businesses SET data = ? WHERE id = ?", (_dump(business), business.id))
        return business

    @staticmethod
    def _save_post(
        conn: sqlite3.Connection, post: PostView, *, previous_revision: int, previous_status: PostStatus
    ) -> PostView:
        result = conn.execute(
            "UPDATE posts SET revision=?, status=?, scheduled_at=?, channel_id=?, image_asset_id=?, data=? "
            "WHERE id=? AND revision=? AND status=?",
            (
                post.revision,
                post.status.value,
                _stamp(post.scheduled_at) if post.scheduled_at else None,
                post.channel_id,
                post.image_asset_id,
                _dump(post),
                post.id,
                previous_revision,
                previous_status.value,
            ),
        )
        if result.rowcount != 1:
            raise Conflict("Публикация уже изменена другим действием")
        return post

    @staticmethod
    def _check_revision(post: PostView, expected_revision: int) -> None:
        if type(expected_revision) is not int or expected_revision <= 0:
            raise InvalidInput("Номер версии должен быть положительным целым числом")
        if post.revision != expected_revision:
            raise Conflict("Версия публикации устарела; откройте актуальный предпросмотр")

    @staticmethod
    def _asset(conn: sqlite3.Connection, asset_id: str, business_id: str | None = None) -> Asset:
        row = conn.execute("SELECT data FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            raise NotFound("Файл не найден")
        asset = Asset.model_validate_json(row["data"])
        if business_id is not None and asset.business_id != business_id:
            raise InvalidInput("Файл принадлежит другому бизнесу")
        return asset

    def create_business(self, owner_id: int, name: str, timezone: str, now: datetime) -> Business:
        now = _utc(now)
        if not isinstance(owner_id, int) or isinstance(owner_id, bool) or owner_id <= 0:
            raise InvalidInput("Некорректный владелец бизнеса")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise InvalidInput("Укажите название бизнеса до 200 символов")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise InvalidInput("Неизвестный часовой пояс") from exc
        business = Business(
            id=_new_id(), owner_id=owner_id, name=name.strip(), timezone=timezone, created_at=now
        )
        with self._transaction() as conn:
            conn.execute("INSERT INTO businesses(id, data) VALUES (?, ?)", (business.id, _dump(business)))
            self._audit(conn, "business", business.id, "created", now, owner_id=owner_id)
        return business

    def get_business(self, business_id: str) -> Business:
        with self._reader() as conn:
            return self._business(conn, business_id)

    def list_businesses(self) -> list[Business]:
        with self._reader() as conn:
            return [
                Business.model_validate_json(r["data"])
                for r in conn.execute("SELECT data FROM businesses ORDER BY rowid")
            ]

    def archive_business(self, business_id: str, archived: bool, now: datetime) -> Business:
        with self._transaction() as conn:
            old = self._business(conn, business_id)
            for table, statuses in (("jobs", ("pending", "running")), ("posts", ("publishing", "editing")),
                                    ("ai_operations", ("running",))):
                marks = ",".join("?" for _ in statuses)
                if conn.execute(f"SELECT 1 FROM {table} WHERE business_id=? AND status IN ({marks}) LIMIT 1",
                                (business_id, *statuses)).fetchone():
                    raise Conflict("Дождитесь активных операций и отмените ожидающие задания компании.")
            new = old.model_copy(update={"archived": archived, "publications_paused": True,
                                         "automation_enabled": False, "next_generation_at": None})
            self._save_business(conn, new)
            self._audit(conn, "business", business_id, "archived" if archived else "restored", now)
            return new

    def set_channel(self, business_id: str, channel: ChannelInfo, now: datetime) -> Business:
        now = _utc(now)
        if not channel.id.strip():
            raise InvalidInput("Не указан канал")
        with self._transaction() as conn:
            old = self._business(conn, business_id)
            new = old.model_copy(update={"channel_id": channel.id, "channel_title": channel.title})
            self._save_business(conn, new)
            self._audit(
                conn,
                "business",
                business_id,
                "channel_changed",
                now,
                previous_channel=old.channel_id,
                channel_id=channel.id,
            )
            # Approval includes the delivery destination. Changing it invalidates
            # outstanding approval even if the owner later changes it back.
            if old.channel_id != channel.id:
                rows = conn.execute(
                    "SELECT data FROM posts WHERE business_id=? AND status=?",
                    (business_id, PostStatus.APPROVED.value),
                ).fetchall()
                for row in rows:
                    post = PostView.model_validate_json(row["data"])
                    changed = post.model_copy(
                        update={
                            "status": PostStatus.REVIEW,
                            "approved_revision": None,
                            "approved_by": None,
                            "updated_at": now,
                        }
                    )
                    self._save_post(
                        conn, changed, previous_revision=post.revision, previous_status=post.status
                    )
                    self._audit(
                        conn, "post", post.id, "approval_invalidated_channel", now, revision=post.revision
                    )
            return new

    def set_profile(
        self,
        business_id: str,
        profile: BusinessProfile,
        now: datetime,
        *,
        expected_version: int | None = None,
    ) -> Business:
        with self._transaction() as conn:
            old = self._business(conn, business_id)
            if expected_version is not None and old.profile_version != expected_version:
                raise Conflict("Версия профиля изменилась во время обработки")
            new = old.model_copy(
                update={
                    "profile": profile,
                    "profile_version": old.profile_version + 1,
                    "profile_confirmed": False,
                }
            )
            self._save_business(conn, new)
            self._audit(conn, "business", business_id, "profile_changed", now, version=new.profile_version)
            return new

    def confirm_profile(self, business_id: str, expected_version: int, now: datetime) -> Business:
        with self._transaction() as conn:
            old = self._business(conn, business_id)
            if old.profile is None:
                raise InvalidInput("Сначала подготовьте профиль бизнеса")
            if old.profile_version != expected_version:
                raise Conflict("Версия профиля устарела")
            if old.profile_confirmed:
                return old
            new = old.model_copy(update={"profile_confirmed": True})
            self._save_business(conn, new)
            self._audit(conn, "business", business_id, "profile_confirmed", now, version=expected_version)
            return new

    def set_manual_profile(
        self, business_id: str, expected_version: int, profile: BusinessProfile,
        source: Source, actor_id: int, now: datetime,
    ) -> Business:
        """The owner's source and unconfirmed profile are one revision and one transaction."""
        with self._transaction() as conn:
            old = self._business(conn, business_id)
            if not self._may_operate(conn, old.owner_id, actor_id, old.id):
                raise Forbidden("Нет доступа к этому бизнесу")
            if old.profile_version != expected_version:
                raise Conflict("Версия профиля изменилась. Откройте актуальный профиль.")
            if source.business_id != business_id or source.kind != "text" or source.asset_id is not None:
                raise InvalidInput("Некорректный источник ручного профиля")
            if any(fact.source_ids != [source.id] for fact in profile.facts):
                raise InvalidInput("Ручной профиль должен ссылаться на сведения владельца")
            conn.execute("INSERT INTO sources(id,business_id,asset_id,data) VALUES (?,?,NULL,?)",
                         (source.id, business_id, _dump(source)))
            new = old.model_copy(update={"profile": profile, "profile_version": expected_version + 1,
                                         "profile_confirmed": False})
            self._save_business(conn, new)
            self._audit(conn, "source", source.id, "created", now, business_id=business_id, actor_id=actor_id)
            self._audit(conn, "business", business_id, "manual_profile_saved", now,
                        version=new.profile_version, source_id=source.id, actor_id=actor_id)
            return new

    def set_automation(
        self,
        business_id: str,
        enabled: bool,
        request: str,
        interval_hours: int,
        next_at: datetime | None,
        now: datetime,
    ) -> Business:
        now = _utc(now)
        if (
            not isinstance(interval_hours, int)
            or isinstance(interval_hours, bool)
            or interval_hours < 1
            or interval_hours > 8760
        ):
            raise InvalidInput("Интервал должен быть от 1 до 8760 часов")
        if not isinstance(request, str) or not 1 <= len(request.strip()) <= 4000:
            raise InvalidInput("Укажите задачу длиной до 4000 символов")
        next_at = _utc(next_at) if next_at is not None else None
        if enabled and next_at is None:
            raise InvalidInput("Не указано время следующего запуска")
        with self._transaction() as conn:
            old = self._business(conn, business_id)
            if enabled and (not old.profile_confirmed or not old.channel_id):
                raise InvalidInput("Для автоматизации подтвердите профиль и подключите канал")
            new = old.model_copy(
                update={
                    "automation_enabled": bool(enabled),
                    "automation_request": request.strip(),
                    "interval_hours": interval_hours,
                    "next_generation_at": next_at,
                }
            )
            self._save_business(conn, new)
            self._audit(
                conn,
                "business",
                business_id,
                "automation_configured",
                now,
                enabled=bool(enabled),
                interval_hours=interval_hours,
                next_at=_stamp(next_at) if next_at else None,
            )
            return new

    def set_publications_paused(self, business_id: str, paused: bool, now: datetime) -> Business:
        if type(paused) is not bool:
            raise InvalidInput("Пауза публикаций требует логического значения")
        now = _utc(now)
        with self._transaction() as conn:
            business = self._business(conn, business_id)
            if business.publications_paused == paused:
                return business
            updated = business.model_copy(update={"publications_paused": paused})
            self._save_business(conn, updated)
            self._audit(
                conn,
                "business",
                business_id,
                "publications_paused" if paused else "publications_resumed",
                now,
            )
            return updated

    def add_source(self, source: Source) -> Source:
        with self._transaction() as conn:
            business = self._business(conn, source.business_id)
            if source.asset_id:
                self._asset(conn, source.asset_id, source.business_id)
            old = conn.execute("SELECT data FROM sources WHERE id=?", (source.id,)).fetchone()
            if old:
                if Source.model_validate_json(old["data"]) != source:
                    raise Conflict("Идентификатор источника уже используется")
                return source
            conn.execute(
                "INSERT INTO sources(id,business_id,asset_id,data) VALUES (?,?,?,?)",
                (source.id, source.business_id, source.asset_id, _dump(source)),
            )
            self._audit(
                conn, "source", source.id, "created", source.created_at, business_id=source.business_id
            )
            if business.profile is not None:
                changed = business.model_copy(
                    update={
                        "profile_version": business.profile_version + 1,
                        "profile_confirmed": False,
                    }
                )
                self._save_business(conn, changed)
                self._audit(
                    conn,
                    "business",
                    business.id,
                    "profile_invalidated_new_source",
                    source.created_at,
                    source_id=source.id,
                    previous_version=business.profile_version,
                    version=changed.profile_version,
                )
        return source

    def list_sources(self, business_id: str) -> list[Source]:
        with self._reader() as conn:
            self._business(conn, business_id)
            return [
                Source.model_validate_json(r["data"])
                for r in conn.execute(
                    "SELECT data FROM sources WHERE business_id=? ORDER BY rowid", (business_id,)
                )
            ]

    def get_source(self, source_id: str) -> Source:
        with self._reader() as conn:
            row = conn.execute("SELECT data FROM sources WHERE id=?", (source_id,)).fetchone()
            if row is None:
                raise NotFound("Источник не найден")
            return Source.model_validate_json(row["data"])

    def add_asset(self, asset: Asset) -> Asset:
        with self._transaction() as conn:
            self._business(conn, asset.business_id)
            old = conn.execute("SELECT data FROM assets WHERE id=?", (asset.id,)).fetchone()
            if old:
                if Asset.model_validate_json(old["data"]) != asset:
                    raise Conflict("Идентификатор файла уже используется")
                return asset
            conn.execute(
                "INSERT INTO assets(id,business_id,data) VALUES (?,?,?)",
                (asset.id, asset.business_id, _dump(asset)),
            )
            self._audit(conn, "asset", asset.id, "created", asset.created_at, business_id=asset.business_id)
        return asset

    def get_asset(self, asset_id: str) -> Asset:
        with self._reader() as conn:
            return self._asset(conn, asset_id)

    @staticmethod
    def _validate_post_data(
        conn: sqlite3.Connection, business: Business, data: dict[str, Any]
    ) -> dict[str, Any]:
        allowed = {
            "text",
            "image_asset_id",
            "channel_id",
            "scheduled_at",
            "is_demo",
            "profile_version",
            "used_source_ids",
            "warnings",
        }
        if set(data) - allowed:
            raise InvalidInput("Неизвестные поля публикации")
        text = data.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 900:
            raise InvalidInput("Текст публикации должен содержать от 1 до 900 символов")
        channel_id = data.get("channel_id")
        if not isinstance(channel_id, str) or channel_id != (business.channel_id or ''):
            raise InvalidInput("Публикация должна быть адресована текущему каналу бизнеса")
        image_id = data.get("image_asset_id")
        asset = Repository._asset(conn, image_id, business.id) if image_id is not None else None
        if asset and asset.mime_type not in {"image/png", "image/jpeg"}:
            raise InvalidInput("Для публикации требуется PNG или JPEG")
        scheduled = data.get("scheduled_at")
        if scheduled is not None:
            scheduled = _utc(scheduled)
        profile_version = data.get("profile_version", business.profile_version)
        if profile_version is not None and (
            type(profile_version) is not int or not 0 <= profile_version <= business.profile_version
        ):
            raise InvalidInput("Некорректная версия профиля публикации")
        source_ids = data.get("used_source_ids", [])
        if (
            not isinstance(source_ids, list)
            or len(source_ids) > 100
            or any(not isinstance(value, str) or not value or len(value) > 200 for value in source_ids)
        ):
            raise InvalidInput("Некорректные источники публикации")
        for source_id in source_ids:
            row = conn.execute("SELECT business_id FROM sources WHERE id=?", (source_id,)).fetchone()
            if row is None or row["business_id"] != business.id:
                raise InvalidInput("Источник публикации неизвестен или принадлежит другому бизнесу")
        warnings = data.get("warnings", [])
        if (
            not isinstance(warnings, list)
            or len(warnings) > 50
            or any(not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in warnings)
        ):
            raise InvalidInput("Некорректные предупреждения публикации")
        try:
            for warning in warnings:
                warning.encode("utf-16-le")
        except UnicodeEncodeError:
            raise InvalidInput("Некорректные предупреждения публикации") from None
        return {
            "text": text,
            "image_asset_id": asset.id if asset else None,
            "channel_id": channel_id,
            "scheduled_at": scheduled,
            "is_demo": bool(data.get("is_demo", False) or (asset and asset.is_demo)),
            "profile_version": profile_version,
            "used_source_ids": list(dict.fromkeys(source_ids)),
            "warnings": list(warnings),
        }

    def create_posts(
        self,
        business_id: str,
        items: list[dict[str, Any]],
        now: datetime,
        job_id: str | None = None,
        *,
        expected_profile_version: int | None = None,
        expected_channel_id: str | None = None,
        notification: Notification | None = None,
        manual_request_id: str | None = None,
        manual_payload_hash: str | None = None,
        manual_actor_id: int | None = None,
    ) -> list[PostView]:
        now = _utc(now)
        if not 1 <= len(items) <= 5:
            raise InvalidInput("Создавайте от 1 до 5 публикаций за один раз")
        with self._transaction() as conn:
            business = self._business(conn, business_id)
            manual = any(value is not None for value in (manual_request_id, manual_payload_hash, manual_actor_id))
            if manual:
                if (job_id is not None or len(items) != 1 or not self._may_operate(conn, business.owner_id, manual_actor_id, business.id)
                        or not isinstance(manual_request_id, str) or not 1 <= len(manual_request_id) <= 100
                        or not isinstance(manual_payload_hash, str)
                        or re.fullmatch(r"[0-9a-f]{64}", manual_payload_hash) is None):
                    raise InvalidInput("Некорректный запрос ручного черновика")
                duplicate = self._manual_post(conn, business_id, manual_request_id, manual_payload_hash)
                if duplicate:
                    return [duplicate]
                if not business.profile_confirmed or business.profile is None:
                    raise Conflict("Подтвердите профиль перед созданием поста.")
            if expected_profile_version is not None and business.profile_version != expected_profile_version:
                raise Conflict("Профиль изменился во время генерации; результат не сохранён")
            if expected_channel_id is not None and business.channel_id != expected_channel_id:
                raise Conflict("Канал изменился во время генерации; результат не сохранён")
            job = self._job(conn, job_id) if job_id else None
            if job and (job.business_id != business_id or job.status != "running"):
                raise Conflict("Задание не находится в работе для этого бизнеса")
            posts = []
            for item in items:
                values = self._validate_post_data(conn, business, item)
                if values["profile_version"] != business.profile_version:
                    raise Conflict("Новая публикация должна использовать текущую версию профиля")
                post = PostView(
                    id=_new_id(),
                    business_id=business_id,
                    revision=1,
                    status=PostStatus.REVIEW,
                    created_at=now,
                    updated_at=now,
                    job_id=job_id,
                    content_origin="manual" if manual else ("generated" if job_id else "legacy"),
                    created_by=manual_actor_id if manual else None,
                    **values,
                )
                conn.execute(
                    "INSERT INTO posts(id,business_id,revision,status,scheduled_at,channel_id,image_asset_id,data) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        post.id,
                        business_id,
                        1,
                        post.status.value,
                        _stamp(post.scheduled_at) if post.scheduled_at else None,
                        post.channel_id,
                        post.image_asset_id,
                        _dump(post),
                    ),
                )
                conn.execute(
                    "INSERT INTO post_revisions(post_id,revision,data,created_at) VALUES (?,?,?,?)",
                    (post.id, 1, _dump(post), _stamp(now)),
                )
                self._audit(conn, "post", post.id, "created", now, revision=1, job_id=job_id)
                posts.append(post)
            if job:
                done = job.model_copy(
                    update={
                        "status": "done",
                        "stage": "done",
                        "processed": max(job.total, 1 + 2 * len(posts)),
                        "total": max(job.total, 1 + 2 * len(posts)),
                        "updated_at": now,
                        "finished_at": now,
                    }
                )
                changed = conn.execute(
                    "UPDATE jobs SET status='done',data=? WHERE id=? AND status='running'",
                    (_dump(done), job.id),
                )
                if changed.rowcount != 1:
                    raise Conflict("Задание уже завершено")
                self._audit(conn, "job", job.id, "done", now, post_ids=[p.id for p in posts])
            if manual:
                self._audit(conn, "manual_draft", f"{business_id}:{manual_request_id}", "created", now,
                            post_id=posts[0].id, payload_hash=manual_payload_hash, actor_id=manual_actor_id)
                if notification is not None:
                    notification = notification.model_copy(update={"post_id": posts[0].id})
                self._outbox(conn, notification, business_id=business_id, post_id=posts[0].id)
            else:
                self._outbox(conn, notification, business_id=business_id, job_id=job_id)
            return posts

    @staticmethod
    def _manual_post(conn, business_id: str, request_id: str, payload_hash: str) -> PostView | None:
        row = conn.execute(
            "SELECT details FROM audit WHERE entity_type='manual_draft' AND entity_id=? ORDER BY id LIMIT 1",
            (f"{business_id}:{request_id}",),
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["details"])
        if data["payload_hash"] != payload_hash:
            raise Conflict("Ключ черновика уже использован для другого материала.")
        post = Repository._post(conn, data["post_id"])
        if post.business_id != business_id:
            raise Forbidden("Черновик относится к другому бизнесу.")
        return post

    def find_manual_post(self, business_id: str, request_id: str, payload_hash: str) -> PostView | None:
        with self._reader() as conn:
            self._business(conn, business_id)
            return self._manual_post(conn, business_id, request_id, payload_hash)

    def get_post(self, post_id: str) -> PostView:
        with self._reader() as conn:
            return self._post(conn, post_id)

    def post_revisions(self, post_id: str) -> list[PostView]:
        with self._reader() as conn:
            self._post(conn, post_id)
            return [PostView.model_validate_json(row['data']) for row in conn.execute(
                "SELECT data FROM post_revisions WHERE post_id=? ORDER BY revision DESC LIMIT 100", (post_id,)
            )]

    def list_posts(self, business_id: str) -> list[PostView]:
        with self._reader() as conn:
            self._business(conn, business_id)
            return [
                PostView.model_validate_json(r["data"])
                for r in conn.execute(
                    "SELECT data FROM posts WHERE business_id=? ORDER BY rowid", (business_id,)
                )
            ]

    def revise_post(
        self, post_id: str, expected_revision: int, changes: dict[str, Any], now: datetime
    ) -> PostView:
        now = _utc(now)
        allowed = {
            "text",
            "image_asset_id",
            "channel_id",
            "scheduled_at",
            "is_demo",
            "profile_version",
            "used_source_ids",
            "warnings",
        }
        if not changes or set(changes) - allowed:
            raise InvalidInput("Укажите допустимые изменения публикации")
        with self._transaction() as conn:
            post = self._post(conn, post_id)
            self._check_revision(post, expected_revision)
            if post.status in {
                PostStatus.EDITING,
                PostStatus.PUBLISHING,
                PostStatus.PUBLISHED,
                PostStatus.DELIVERY_UNKNOWN,
            }:
                raise Conflict("Публикация уже отправляется или отправлена; правки запрещены")
            business = self._business(conn, post.business_id)
            values = {key: getattr(post, key) for key in allowed}
            values.update(changes)
            values = self._validate_post_data(conn, business, values)
            new = post.model_copy(
                update={
                    **values,
                    "revision": post.revision + 1,
                    "status": PostStatus.REVIEW,
                    "approved_revision": None,
                    "approved_by": None,
                    "updated_at": now,
                    "last_error": None,
                }
            )
            self._save_post(conn, new, previous_revision=post.revision, previous_status=post.status)
            conn.execute(
                "INSERT INTO post_revisions(post_id,revision,data,created_at) VALUES (?,?,?,?)",
                (new.id, new.revision, _dump(new), _stamp(now)),
            )
            self._audit(
                conn,
                "post",
                post.id,
                "revised",
                now,
                previous_revision=post.revision,
                revision=new.revision,
                changed_fields=sorted(changes),
            )
            return new

    def begin_edit(self, post_id: str, expected_revision: int, now: datetime) -> PostView:
        """Revoke approval before awaiting a paid external text/image operation."""
        now = _utc(now)
        with self._transaction() as conn:
            post = self._post(conn, post_id)
            self._check_revision(post, expected_revision)
            if post.status not in {
                PostStatus.REVIEW,
                PostStatus.APPROVED,
                PostStatus.REJECTED,
                PostStatus.FAILED,
            }:
                raise Conflict("Публикация уже редактируется, отправляется или отправлена")
            new = post.model_copy(
                update={
                    "revision": post.revision + 1,
                    "status": PostStatus.EDITING,
                    "approved_revision": None,
                    "approved_by": None,
                    "updated_at": now,
                    "last_error": None,
                }
            )
            self._save_post(conn, new, previous_revision=post.revision, previous_status=post.status)
            conn.execute(
                "INSERT INTO post_revisions(post_id,revision,data,created_at) VALUES (?,?,?,?)",
                (new.id, new.revision, _dump(new), _stamp(now)),
            )
            self._audit(
                conn,
                "post",
                post.id,
                "edit_started",
                now,
                previous_revision=post.revision,
                revision=new.revision,
            )
            return new

    def complete_edit(
        self,
        post_id: str,
        editing_revision: int,
        changes: dict[str, Any],
        now: datetime,
        *,
        expected_profile_version: int | None = None,
        expected_channel_id: str | None = None,
    ) -> PostView:
        now = _utc(now)
        allowed = {
            "text",
            "image_asset_id",
            "channel_id",
            "scheduled_at",
            "is_demo",
            "profile_version",
            "used_source_ids",
            "warnings",
        }
        if not changes or set(changes) - allowed:
            raise InvalidInput("Укажите допустимые изменения публикации")
        with self._transaction() as conn:
            post = self._post(conn, post_id)
            self._check_revision(post, editing_revision)
            if post.status != PostStatus.EDITING:
                raise Conflict("Публикация не находится в редактировании")
            business = self._business(conn, post.business_id)
            if expected_profile_version is not None and business.profile_version != expected_profile_version:
                raise Conflict("Профиль изменился во время редактирования")
            if expected_channel_id is not None and business.channel_id != expected_channel_id:
                raise Conflict("Канал изменился во время редактирования")
            values = {key: getattr(post, key) for key in allowed}
            values.update(changes)
            values = self._validate_post_data(conn, business, values)
            new = post.model_copy(
                update={
                    **values,
                    "revision": post.revision + 1,
                    "status": PostStatus.REVIEW,
                    "approved_revision": None,
                    "approved_by": None,
                    "updated_at": now,
                    "last_error": None,
                }
            )
            self._save_post(conn, new, previous_revision=post.revision, previous_status=post.status)
            conn.execute(
                "INSERT INTO post_revisions(post_id,revision,data,created_at) VALUES (?,?,?,?)",
                (new.id, new.revision, _dump(new), _stamp(now)),
            )
            self._audit(
                conn,
                "post",
                post.id,
                "edit_completed",
                now,
                previous_revision=post.revision,
                revision=new.revision,
                changed_fields=sorted(changes),
            )
            return new

    def fail_edit(
        self,
        post_id: str,
        editing_revision: int,
        error: str,
        now: datetime,
        *,
        notification: Notification | None = None,
    ) -> PostView:
        now = _utc(now)
        with self._transaction() as conn:
            post = self._post(conn, post_id)
            self._check_revision(post, editing_revision)
            if post.status != PostStatus.EDITING:
                raise Conflict("Публикация не находится в редактировании")
            new = post.model_copy(
                update={
                    "status": PostStatus.FAILED,
                    "updated_at": now,
                    "last_error": str(error)[:2000],
                    "approved_revision": None,
                    "approved_by": None,
                }
            )
            self._save_post(conn, new, previous_revision=post.revision, previous_status=post.status)
            self._audit(conn, "post", post.id, "edit_failed", now, revision=post.revision)
            self._outbox(
                conn, notification, business_id=post.business_id, post_id=post.id, job_id=post.job_id
            )
            return new

    def approve_post(self, post_id: str, expected_revision: int, actor_id: int, now: datetime) -> PostView:
        now = _utc(now)
        with self._transaction() as conn:
            post = self._post(conn, post_id)
            self._check_revision(post, expected_revision)
            business = self._business(conn, post.business_id)
            if not self._may_operate(conn, business.owner_id, actor_id, business.id):
                raise Forbidden("Согласовывать публикации может только владелец или оператор")
            if post.channel_id != business.channel_id:
                raise Conflict("Канал бизнеса изменился; обновите предпросмотр")
            if post.profile_version != business.profile_version:
                raise Conflict(
                    "Профиль бизнеса изменился или версия источников неизвестна; проверьте и обновите текст поста"
                )
            if business.profile is not None and not business.profile_confirmed:
                raise Conflict("Подтвердите актуальный профиль бизнеса")
            if (post.status == PostStatus.APPROVED and post.approved_revision == post.revision
                    and self._approval_allowed(conn, business.owner_id, post)):
                return post
            if post.status not in {PostStatus.REVIEW, PostStatus.APPROVED}:
                raise Conflict("Согласование возможно только из состояния предпросмотра")
            if post.scheduled_at is None or post.scheduled_at <= now:
                raise InvalidInput("Для согласования выберите время в будущем")
            new = post.model_copy(
                update={
                    "status": PostStatus.APPROVED,
                    "approved_revision": post.revision,
                    "approved_by": actor_id,
                    "updated_at": now,
                    "last_error": None,
                }
            )
            self._save_post(conn, new, previous_revision=post.revision, previous_status=post.status)
            self._audit(conn, "post", post.id, "approved", now, revision=post.revision, actor_id=actor_id)
            return new

    def reject_post(self, post_id: str, expected_revision: int, now: datetime) -> PostView:
        now = _utc(now)
        with self._transaction() as conn:
            post = self._post(conn, post_id)
            self._check_revision(post, expected_revision)
            if post.status not in {PostStatus.REVIEW, PostStatus.APPROVED, PostStatus.FAILED}:
                raise Conflict("Публикацию в текущем состоянии нельзя отклонить")
            new = post.model_copy(
                update={
                    "status": PostStatus.REJECTED,
                    "approved_revision": None,
                    "approved_by": None,
                    "updated_at": now,
                }
            )
            self._save_post(conn, new, previous_revision=post.revision, previous_status=post.status)
            self._audit(conn, "post", post.id, "rejected", now, revision=post.revision)
            return new

    def claim_due_post(self, now: datetime) -> PostView | None:
        now = _utc(now)
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT data FROM posts WHERE status=? AND scheduled_at<=? ORDER BY scheduled_at,id",
                (PostStatus.APPROVED.value, _stamp(now)),
            ).fetchall()
            for row in rows:
                post = PostView.model_validate_json(row["data"])
                business = self._business(conn, post.business_id)
                if business.publications_paused:
                    continue
                if (
                    post.approved_revision != post.revision
                    or not self._approval_allowed(conn, business.owner_id, post)
                    or post.channel_id != business.channel_id
                    or post.profile_version != business.profile_version
                    or (business.profile is not None and not business.profile_confirmed)
                ):
                    new = post.model_copy(
                        update={
                            "status": PostStatus.REVIEW,
                            "approved_revision": None,
                            "approved_by": None,
                            "updated_at": now,
                        }
                    )
                    self._save_post(conn, new, previous_revision=post.revision, previous_status=post.status)
                    self._audit(conn, "post", post.id, "approval_invalidated", now, revision=post.revision)
                    continue
                claimed = post.model_copy(update={"status": PostStatus.PUBLISHING, "updated_at": now})
                self._save_post(conn, claimed, previous_revision=post.revision, previous_status=post.status)
                self._audit(conn, "post", post.id, "publication_claimed", now, revision=post.revision)
                return claimed
            return None

    def _complete_publication(
        self, conn: sqlite3.Connection, post: PostView, result: PublicationResult, now: datetime, action: str
    ) -> PostView:
        if result.channel_id != post.channel_id:
            raise Conflict("Результат отправки относится к другому каналу")
        new = post.model_copy(
            update={
                "status": PostStatus.PUBLISHED,
                "message_id": result.message_id,
                "message_url": result.message_url,
                "published_at": now,
                "updated_at": now,
                "last_error": None,
            }
        )
        self._save_post(conn, new, previous_revision=post.revision, previous_status=post.status)
        self._audit(
            conn,
            "post",
            post.id,
            action,
            now,
            revision=post.revision,
            channel_id=result.channel_id,
            message_id=result.message_id,
        )
        return new

    def finish_publication(
        self,
        post_id: str,
        revision: int,
        result: PublicationResult,
        now: datetime,
        *,
        notification: Notification | None = None,
    ) -> PostView:
        now = _utc(now)
        with self._transaction() as conn:
            post = self._post(conn, post_id)
            self._check_revision(post, revision)
            if post.status != PostStatus.PUBLISHING:
                raise Conflict("Публикация не находится в отправке")
            completed = self._complete_publication(conn, post, result, now, "published")
            self._outbox(
                conn, notification, business_id=post.business_id, post_id=post.id, job_id=post.job_id
            )
            return completed

    def fail_publication(
        self,
        post_id: str,
        revision: int,
        error: str,
        unknown: bool,
        now: datetime,
        *,
        notification: Notification | None = None,
    ) -> PostView:
        now = _utc(now)
        with self._transaction() as conn:
            post = self._post(conn, post_id)
            self._check_revision(post, revision)
            if post.status != PostStatus.PUBLISHING:
                raise Conflict("Публикация не находится в отправке")
            status = PostStatus.DELIVERY_UNKNOWN if unknown else PostStatus.FAILED
            new = post.model_copy(
                update={"status": status, "updated_at": now, "last_error": str(error)[:2000]}
            )
            self._save_post(conn, new, previous_revision=post.revision, previous_status=post.status)
            self._audit(conn, "post", post.id, status.value, now, revision=revision)
            self._outbox(
                conn, notification, business_id=post.business_id, post_id=post.id, job_id=post.job_id
            )
            return new

    def resolve_unknown(
        self, post_id: str, expected_revision: int, result_or_none: PublicationResult | None, now: datetime
    ) -> PostView:
        now = _utc(now)
        with self._transaction() as conn:
            post = self._post(conn, post_id)
            self._check_revision(post, expected_revision)
            if post.status != PostStatus.DELIVERY_UNKNOWN:
                raise Conflict("Ручное решение доступно только для неизвестного результата доставки")
            if result_or_none is not None:
                return self._complete_publication(
                    conn, post, result_or_none, now, "delivery_manually_confirmed"
                )
            new = post.model_copy(
                update={
                    "status": PostStatus.FAILED,
                    "updated_at": now,
                    "approved_revision": None,
                    "approved_by": None,
                    "last_error": "Владелец подтвердил отсутствие публикации; для повтора нужна новая версия и согласование",
                }
            )
            self._save_post(conn, new, previous_revision=post.revision, previous_status=post.status)
            self._audit(conn, "post", post.id, "non_delivery_manually_confirmed", now, revision=post.revision)
            return new

    @staticmethod
    def _has_job_capacity(
        conn: sqlite3.Connection,
        business: Business,
        now: datetime,
        max_pending: int | None,
        max_per_day: int | None,
    ) -> bool:
        for value in (max_pending, max_per_day):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
                raise InvalidInput("Лимиты заданий должны быть положительными целыми числами")
        if max_pending is not None:
            count = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE business_id=? AND status IN ('pending','running')",
                (business.id,),
            ).fetchone()[0]
            if count >= max_pending:
                return False
        if max_per_day is not None:
            local_start = now.astimezone(ZoneInfo(business.timezone)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE business_id=? AND created_at>=?",
                (business.id, _stamp(local_start)),
            ).fetchone()[0]
            if count >= max_per_day:
                return False
        return True

    def _enqueue(
        self,
        conn: sqlite3.Connection,
        business_id: str,
        request: str,
        dedup_key: str,
        now: datetime,
        *,
        max_pending: int | None = None,
        max_per_day: int | None = None,
    ) -> tuple[Job, bool]:
        existing = conn.execute(
            "SELECT data FROM jobs WHERE business_id=? AND dedup_key=?", (business_id, dedup_key)
        ).fetchone()
        if existing:
            job = Job.model_validate_json(existing["data"])
            if job.request != request:
                raise Conflict("Ключ задания уже использован для другого запроса")
            return job, False
        business = self._business(conn, business_id)
        if not self._has_job_capacity(conn, business, now, max_pending, max_per_day):
            raise Conflict("Достигнут лимит ожидающих или дневных заданий генерации")
        job = Job(
            id=_new_id(),
            business_id=business_id,
            request=request,
            status="pending",
            dedup_key=dedup_key,
            created_at=now,
            updated_at=now,
        )
        conn.execute(
            "INSERT INTO jobs(id,business_id,dedup_key,status,created_at,data) VALUES (?,?,?,?,?,?)",
            (job.id, business_id, dedup_key, job.status, _stamp(now), _dump(job)),
        )
        self._audit(conn, "job", job.id, "enqueued", now, business_id=business_id)
        return job, True

    def enqueue_job(
        self,
        business_id: str,
        request: str,
        dedup_key: str,
        now: datetime,
        *,
        max_pending: int | None = None,
        max_per_day: int | None = None,
        kind: str = 'prepare', requested_by: int | None = None, caption: str | None = None,
        profile_version: int | None = None,
    ) -> Job:
        now = _utc(now)
        if not isinstance(request, str) or not 1 <= len(request.strip()) <= 4000:
            raise InvalidInput("Некорректная задача генерации")
        if not isinstance(dedup_key, str) or not 1 <= len(dedup_key) <= 300:
            raise InvalidInput("Некорректный ключ задания")
        with self._transaction() as conn:
            self._business(conn, business_id)
            job, inserted = self._enqueue(
                conn,
                business_id,
                request.strip(),
                dedup_key,
                now,
                max_pending=max_pending,
                max_per_day=max_per_day,
            )
            if inserted:
                job = Job.model_validate({**job.model_dump(), 'kind': kind, 'requested_by': requested_by,
                                          'caption': caption, 'profile_version': profile_version})
                conn.execute('UPDATE jobs SET data=? WHERE id=?', (_dump(job), job.id))
            elif (job.kind, job.request, job.caption) != (kind, request.strip(), caption):
                raise Conflict('Ключ задания уже использован с другими параметрами.')
            return job

    def finish_visual_job(self, job_id, result, now):
        with self._transaction() as conn:
            job = self._job(conn, job_id)
            if job.status != 'running' or job.kind != 'visual':
                raise Conflict('Задание изображения уже завершено.')
            business = self._business(conn, job.business_id)
            if (business.archived or business.profile_version != result.profile_version
                    or not self._may_operate(conn, business.owner_id, job.requested_by, business.id)):
                raise Conflict('Профиль или права изменились во время создания изображения.')
            done = job.model_copy(update={'status': 'done', 'stage': 'done', 'processed': 2, 'total': 2,
                                          'visual_result': result, 'finished_at': now, 'updated_at': now})
            conn.execute("UPDATE jobs SET status='done',data=? WHERE id=?", (_dump(done), job.id))
            self._audit(conn, 'job', job.id, 'done', now, asset_id=result.asset.id)
            return done

    def get_job(self, job_id: str) -> Job:
        with self._reader() as conn:
            return self._job(conn, job_id)

    def find_job(self, business_id: str, dedup_key: str) -> Job | None:
        with self._reader() as conn:
            self._business(conn, business_id)
            row = conn.execute(
                "SELECT data FROM jobs WHERE business_id=? AND dedup_key=?", (business_id, dedup_key)
            ).fetchone()
            return Job.model_validate_json(row["data"]) if row is not None else None

    @staticmethod
    def _operation_dict(row: sqlite3.Row) -> dict[str, Any]:
        values = dict(row)
        values["result"] = json.loads(values["result"]) if values["result"] is not None else None
        return values

    def get_ai_operation(self, operation_id: str) -> dict[str, Any]:
        with self._reader() as conn:
            row = conn.execute("SELECT * FROM ai_operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise NotFound("AI-операция не найдена")
            return self._operation_dict(row)

    def begin_ai_operation(
        self,
        owner_id: int,
        business_id: str,
        kind: str,
        request_key: str,
        payload_hash: str,
        now: datetime,
        *,
        daily_limit: int,
        max_parallel: int,
    ) -> dict[str, Any]:
        """Reserve one logical provider operation before doing paid work.

        An owner-wide key cannot be reused for another business, kind or input.
        Failed/interrupted operations consume their quota and never auto-retry.
        """
        now = _utc(now)
        if type(owner_id) is not int or owner_id <= 0:
            raise InvalidInput("Некорректный владелец AI-операции")
        if not isinstance(kind, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,39}", kind) is None:
            raise InvalidInput("Некорректный тип AI-операции")
        if not isinstance(request_key, str) or not 1 <= len(request_key.strip()) <= 400:
            raise InvalidInput("Укажите стабильный ключ AI-операции до 400 символов")
        if not isinstance(payload_hash, str) or re.fullmatch(r"[0-9a-f]{64}", payload_hash) is None:
            raise InvalidInput("Некорректный отпечаток AI-запроса")
        if any(type(value) is not int or value < 1 for value in (daily_limit, max_parallel)):
            raise InvalidInput("Лимиты AI-операций должны быть положительными целыми числами")
        with self._transaction() as conn:
            business = self._business(conn, business_id)
            if business.owner_id != owner_id:
                raise Forbidden("AI-операция относится к другому владельцу")
            row = conn.execute(
                "SELECT * FROM ai_operations WHERE owner_id=? AND request_key=?", (owner_id, request_key)
            ).fetchone()
            if row is not None:
                if (row["business_id"], row["kind"], row["payload_hash"]) != (
                    business_id,
                    kind,
                    payload_hash,
                ):
                    raise Conflict("Ключ AI-операции уже использован для другого действия")
                return {**self._operation_dict(row), "created": False}
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            used = conn.execute(
                "SELECT COUNT(*) FROM ai_operations WHERE owner_id=? AND created_at>=? AND created_at<?",
                (owner_id, _stamp(start), _stamp(start + timedelta(days=1))),
            ).fetchone()[0]
            if used >= daily_limit:
                raise Conflict("Достигнут дневной лимит AI-операций владельца (сутки UTC)")
            if business.ai_daily_limit is not None:
                company_used = conn.execute(
                    'SELECT COUNT(*) FROM ai_operations WHERE business_id=? AND created_at>=? AND created_at<?',
                    (business_id, _stamp(start), _stamp(start+timedelta(days=1))),
                ).fetchone()[0]
                if company_used >= business.ai_daily_limit:
                    raise Conflict('Достигнут дневной лимит AI-операций компании (сутки UTC).')
            running = conn.execute(
                "SELECT COUNT(*) FROM ai_operations WHERE owner_id=? AND status='running'", (owner_id,)
            ).fetchone()[0]
            if running >= max_parallel:
                raise Conflict("Достигнут лимит одновременных AI-операций; дождитесь завершения текущих")
            operation_id = _new_id()
            conn.execute(
                "INSERT INTO ai_operations(id,owner_id,business_id,kind,request_key,payload_hash,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'running',?,?)",
                (
                    operation_id,
                    owner_id,
                    business_id,
                    kind,
                    request_key,
                    payload_hash,
                    _stamp(now),
                    _stamp(now),
                ),
            )
            self._audit(
                conn, "ai_operation", operation_id, "started", now, business_id=business_id, kind=kind
            )
            row = conn.execute("SELECT * FROM ai_operations WHERE id=?", (operation_id,)).fetchone()
            return {**self._operation_dict(row), "created": True}

    def finish_ai_operation(
        self, operation_id: str, result: dict | None, now: datetime, *, dialogue_text: str | None = None
    ) -> dict[str, Any]:
        now = _utc(now)
        if result is not None and not isinstance(result, dict):
            raise InvalidInput("Результат AI-операции должен быть JSON-объектом")
        try:
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False) if result is not None else None
            if encoded is not None and len(encoded.encode("utf-8")) > 2_000_000:
                raise ValueError
        except (TypeError, ValueError, UnicodeEncodeError):
            raise InvalidInput("Результат AI-операции не подходит для хранения") from None
        intent = None
        if dialogue_text is not None:
            if not isinstance(dialogue_text, str) or not 1 <= len(dialogue_text.strip()) <= 4000:
                raise InvalidInput("Сообщение диалога должно содержать от 1 до 4000 символов")
            intent = TaskIntent.model_validate(result)
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM ai_operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise NotFound("AI-операция не найдена")
            if row["status"] == "done" and self._operation_dict(row)["result"] == result:
                return self._operation_dict(row)
            if row["status"] != "running":
                raise Conflict("AI-операция уже завершена")
            if intent is not None:
                if row["kind"] != "interpret":
                    raise InvalidInput("История диалога относится только к интерпретации запроса")
                # A turn and its cached AI result commit together. A replay cannot
                # duplicate history or pay for a second interpretation.
                assistant = {
                    "prepare": "Бриф для подготовки: " + intent.request,
                    "report": "Запрошен отчёт по компании.",
                    "revise_text": "Предложена правка текста: " + intent.request,
                    "revise_image": "Предложена правка изображения: " + intent.request,
                    "clarify": "\n".join(intent.questions),
                    "answer": intent.answer,
                }[intent.action]
                conn.execute(
                    "INSERT INTO conversation_turns(operation_id,business_id,user_text,assistant_text,"
                    "action,created_at) VALUES (?,?,?,?,?,?)",
                    (operation_id, row["business_id"], dialogue_text, assistant, intent.action, _stamp(now)),
                )
            conn.execute(
                "UPDATE ai_operations SET status='done',result=?,updated_at=? WHERE id=?",
                (encoded, _stamp(now), operation_id),
            )
            self._audit(conn, "ai_operation", operation_id, "done", now)
            return self._operation_dict(
                conn.execute("SELECT * FROM ai_operations WHERE id=?", (operation_id,)).fetchone()
            )

    def fail_ai_operation(self, operation_id: str, error_code: str, now: datetime) -> dict[str, Any]:
        now = _utc(now)
        if not isinstance(error_code, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) is None:
            raise InvalidInput("Укажите безопасный код ошибки AI-операции")
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM ai_operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise NotFound("AI-операция не найдена")
            if row["status"] == "failed" and row["error_code"] == error_code:
                return self._operation_dict(row)
            if row["status"] != "running":
                raise Conflict("AI-операция уже завершена")
            conn.execute(
                "UPDATE ai_operations SET status='failed',error_code=?,updated_at=? WHERE id=?",
                (error_code, _stamp(now), operation_id),
            )
            self._audit(conn, "ai_operation", operation_id, "failed", now, error_code=error_code)
            return self._operation_dict(
                conn.execute("SELECT * FROM ai_operations WHERE id=?", (operation_id,)).fetchone()
            )

    def ai_usage(self, owner_id: int, now: datetime) -> dict[str, Any]:
        now = _utc(now)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) AS count FROM ai_operations WHERE owner_id=? AND created_at>=? AND created_at<? GROUP BY status",
                (owner_id, _stamp(start), _stamp(start + timedelta(days=1))),
            )
            counts = {row["status"]: row["count"] for row in rows}
            running = conn.execute(
                "SELECT COUNT(*) FROM ai_operations WHERE owner_id=? AND status='running'", (owner_id,)
            ).fetchone()[0]
            return {
                "used_today": sum(counts.values()),
                "running": running,
                "done_today": counts.get("done", 0),
                "failed_today": counts.get("failed", 0),
                "day_start": _stamp(start),
            }

    def list_ai_operations(
        self, owner_id: int, business_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInput("Лимит AI-операций должен быть от 1 до 100")
        with self._reader() as conn:
            if business_id is not None and self._business(conn, business_id).owner_id != owner_id:
                raise Forbidden("Бизнес принадлежит другому владельцу")
            rows = conn.execute(
                "SELECT id,business_id,kind,status,created_at,updated_at,error_code FROM ai_operations "
                "WHERE owner_id=? AND (? IS NULL OR business_id=?) ORDER BY created_at DESC,id DESC LIMIT ?",
                (owner_id, business_id, business_id, limit),
            )
            return [dict(row) for row in rows]

    def archive_asset(self, asset_id: str, archived: bool, actor_id: int, now: datetime) -> Asset:
        with self._transaction() as conn:
            asset = self._asset(conn, asset_id)
            business = self._business(conn, asset.business_id)
            if not self._may_operate(conn, business.owner_id, actor_id, business.id) or business.archived:
                raise Forbidden('Нет доступа к галерее.')
            if type(archived) is not bool or asset.mime_type not in {'image/png', 'image/jpeg'}:
                raise InvalidInput('Выберите изображение для архива.')
            updated = asset.model_copy(update={'archived': archived})
            conn.execute('UPDATE assets SET data=? WHERE id=?', (_dump(updated), asset_id))
            self._audit(conn, 'asset', asset_id, 'archived' if archived else 'restored', now, actor_id=actor_id)
            return updated

    def list_visual_results(self, business_id: str, *, archived: bool = False) -> list[dict[str, Any]]:
        with self._reader() as conn:
            self._business(conn, business_id)
            results = []
            for row in conn.execute(
                "SELECT o.result,a.data FROM ai_operations o JOIN assets a ON a.id=json_extract(o.result,'$.asset.id') "
                "WHERE o.business_id=? AND a.business_id=? AND o.kind='visual' AND o.status='done' "
                "AND COALESCE(json_extract(a.data,'$.archived'),0)=? ORDER BY o.created_at DESC,o.id DESC LIMIT 30",
                (business_id, business_id, int(archived)),
            ):
                item = json.loads(row['result'])
                item['asset'] = json.loads(row['data'])
                results.append(item)
            return results

    def list_jobs(self, business_id: str) -> list[Job]:
        with self._reader() as conn:
            self._business(conn, business_id)
            return [
                Job.model_validate_json(row["data"])
                for row in conn.execute(
                    "SELECT data FROM jobs WHERE business_id=? ORDER BY created_at,id", (business_id,)
                )
            ]

    def save_plan(self, business_id: str, job_id: str, plan: ContentPlan, now: datetime) -> None:
        with self._transaction() as conn:
            job = self._job(conn, job_id)
            if job.business_id != business_id:
                raise InvalidInput("План относится к заданию другого бизнеса")
            row = conn.execute("SELECT data FROM plans WHERE job_id=?", (job_id,)).fetchone()
            if row is not None:
                if ContentPlan.model_validate_json(row["data"]) != plan:
                    raise Conflict("Для задания уже сохранён другой план")
                return
            if job.status != "running":
                raise Conflict("План сохраняется только для выполняемого задания")
            conn.execute(
                "INSERT INTO plans(job_id,business_id,data,created_at) VALUES (?,?,?,?)",
                (job_id, business_id, _dump(plan), _stamp(now)),
            )
            self._audit(conn, "job", job_id, "plan_saved", now, item_count=len(plan.items))

    def get_plan_for_job(self, job_id: str) -> ContentPlan | None:
        with self._reader() as conn:
            row = conn.execute("SELECT data FROM plans WHERE job_id=?", (job_id,)).fetchone()
            return ContentPlan.model_validate_json(row["data"]) if row is not None else None

    def claim_job(self, now: datetime) -> Job | None:
        now = _utc(now)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT data FROM jobs WHERE status='pending' ORDER BY created_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            job = Job.model_validate_json(row["data"])
            claimed = job.model_copy(
                update={"status": "running", "stage": "plan", "started_at": now, "updated_at": now}
            )
            result = conn.execute(
                "UPDATE jobs SET status='running',data=? WHERE id=? AND status='pending'",
                (_dump(claimed), job.id),
            )
            if result.rowcount != 1:
                raise Conflict("Задание уже забрано другим обработчиком")
            self._audit(conn, "job", job.id, "claimed", now)
            return claimed

    def set_job_progress(self, job_id: str, stage: str, processed: int, total: int, now: datetime) -> Job:
        now = _utc(now)
        if stage not in {"plan", "text", "image", "saving"}:
            raise InvalidInput("Некорректный этап подготовки")
        if any(type(value) is not int for value in (processed, total)) or not 0 <= processed <= total <= 11:
            raise InvalidInput("Некорректный прогресс подготовки")
        with self._transaction() as conn:
            job = self._job(conn, job_id)
            if job.status != "running":
                raise Conflict("Прогресс доступен только для выполняемого задания")
            updated = job.model_copy(
                update={"stage": stage, "processed": processed, "total": total, "updated_at": now}
            )
            conn.execute("UPDATE jobs SET data=? WHERE id=? AND status='running'", (_dump(updated), job_id))
            self._audit(conn, "job", job_id, "progress", now, stage=stage, processed=processed, total=total)
            return updated

    def cancel_job(self, job_id: str, now: datetime) -> Job:
        now = _utc(now)
        with self._transaction() as conn:
            job = self._job(conn, job_id)
            if job.status == "cancelled":
                return job
            if job.status != "pending" and not (job.kind == 'visual' and job.status == 'running'):
                raise Conflict("Отменить можно только ещё не начатое задание")
            updated = job.model_copy(
                update={"status": "cancelled", "stage": "cancelled", "finished_at": now, "updated_at": now}
            )
            conn.execute(
                "UPDATE jobs SET status='cancelled',data=? WHERE id=? AND status=?",
                (_dump(updated), job_id, job.status),
            )
            self._audit(conn, "job", job_id, "cancelled", now)
            return updated

    def fail_job(
        self, job_id: str, error: str, now: datetime, *, notification: Notification | None = None
    ) -> Job:
        now = _utc(now)
        with self._transaction() as conn:
            job = self._job(conn, job_id)
            if job.status not in {"pending", "running"}:
                raise Conflict("Задание уже завершено")
            failed = job.model_copy(
                update={
                    "status": "failed",
                    "stage": "failed",
                    "finished_at": now,
                    "updated_at": now,
                    "error": str(error)[:2000],
                }
            )
            conn.execute(
                "UPDATE jobs SET status='failed',data=? WHERE id=? AND status=?",
                (_dump(failed), job.id, job.status),
            )
            self._audit(conn, "job", job.id, "failed", now)
            self._outbox(conn, notification, business_id=job.business_id, job_id=job.id)
            return failed

    def schedule_automations(
        self, now: datetime, *, max_pending: int | None = 5, max_per_day: int | None = 10
    ) -> int:
        now = _utc(now)
        count = 0
        with self._transaction() as conn:
            businesses = [
                Business.model_validate_json(row["data"])
                for row in conn.execute("SELECT data FROM businesses")
            ]
            for business in businesses:
                due = business.next_generation_at
                if not (
                    business.automation_enabled
                    and business.profile_confirmed
                    and business.channel_id
                    and due
                    and due <= now
                ):
                    continue
                interval = timedelta(hours=business.interval_hours)
                # At most one job after downtime, anchored to the latest due slot.
                # Older missed slots are skipped to avoid a paid generation burst.
                skipped = (now - due) // interval
                slot = due + skipped * interval
                key = f"automation:{_stamp(slot)}"
                existing = conn.execute(
                    "SELECT id FROM jobs WHERE business_id=? AND dedup_key=?", (business.id, key)
                ).fetchone()
                if existing is not None:
                    # A settings change must not re-run a paid generation or
                    # make this slot's previous payload block other businesses.
                    inserted = False
                else:
                    if not self._has_job_capacity(conn, business, now, max_pending, max_per_day):
                        # A genuinely new job waits until capacity is available.
                        continue
                    _, inserted = self._enqueue(conn, business.id, business.automation_request, key, now)
                count += int(inserted)
                new = business.model_copy(update={"next_generation_at": slot + interval})
                self._save_business(conn, new)
                self._audit(
                    conn,
                    "business",
                    business.id,
                    "automation_slot_skipped" if existing is not None else "automation_scheduled",
                    now,
                    slot=_stamp(slot),
                    skipped_slots=int(skipped),
                    inserted=inserted,
                )
        return count

    def recover_interrupted(self, now: datetime) -> dict[str, int]:
        now = _utc(now)
        counts = {"posts": 0, "jobs": 0, "edits": 0}
        with self._transaction() as conn:
            for row in conn.execute("SELECT id FROM ai_operations WHERE status='running'").fetchall():
                conn.execute(
                    "UPDATE ai_operations SET status='failed',error_code='interrupted',updated_at=? WHERE id=?",
                    (_stamp(now), row["id"]),
                )
                self._audit(conn, "ai_operation", row["id"], "interrupted", now, error_code="interrupted")
            rows = conn.execute(
                "SELECT data FROM posts WHERE status=?", (PostStatus.PUBLISHING.value,)
            ).fetchall()
            for row in rows:
                post = PostView.model_validate_json(row["data"])
                new = post.model_copy(
                    update={
                        "status": PostStatus.DELIVERY_UNKNOWN,
                        "updated_at": now,
                        "last_error": "Процесс прервался во время отправки; проверьте канал перед решением о повторе",
                    }
                )
                self._save_post(conn, new, previous_revision=post.revision, previous_status=post.status)
                self._audit(
                    conn, "post", post.id, "interrupted_delivery_unknown", now, revision=post.revision
                )
                self._recovery_notification(
                    conn,
                    post.business_id,
                    "delivery_unknown",
                    post.id,
                    post.revision,
                    now,
                    "После перезапуска результат отправки неизвестен. Проверьте канал перед повтором.",
                    post_id=post.id,
                    job_id=post.job_id,
                )
                counts["posts"] += 1
            rows = conn.execute(
                "SELECT data FROM posts WHERE status=?", (PostStatus.EDITING.value,)
            ).fetchall()
            for row in rows:
                post = PostView.model_validate_json(row["data"])
                new = post.model_copy(
                    update={
                        "status": PostStatus.FAILED,
                        "updated_at": now,
                        "approved_revision": None,
                        "approved_by": None,
                        "last_error": "Редактирование прервано перезапуском; повторите запрос и согласуйте результат заново",
                    }
                )
                self._save_post(conn, new, previous_revision=post.revision, previous_status=post.status)
                self._audit(conn, "post", post.id, "interrupted_edit_failed", now, revision=post.revision)
                self._recovery_notification(
                    conn,
                    post.business_id,
                    "edit_failed",
                    post.id,
                    post.revision,
                    now,
                    "Редактирование прервано перезапуском. Предыдущее согласование отменено.",
                    post_id=post.id,
                    job_id=post.job_id,
                )
                counts["edits"] += 1
            rows = conn.execute("SELECT data FROM jobs WHERE status='running'").fetchall()
            for row in rows:
                job = Job.model_validate_json(row["data"])
                if job.kind == 'visual':
                    operation = conn.execute("SELECT result FROM ai_operations WHERE request_key=? AND status='done'",
                                             (f'visual-job:{job.id}',)).fetchone()
                    business = self._business(conn, job.business_id)
                    if (operation and not business.archived and business.profile_version == job.profile_version
                            and self._may_operate(conn, business.owner_id, job.requested_by, business.id)):
                        done = Job.model_validate({**job.model_dump(), 'visual_result': json.loads(operation['result']),
                                                   'status': 'done', 'stage': 'done', 'processed': 2, 'total': 2,
                                                   'finished_at': now, 'updated_at': now})
                        conn.execute("UPDATE jobs SET status='done',data=? WHERE id=?", (_dump(done), job.id))
                        self._audit(conn, 'job', job.id, 'result_recovered', now)
                        counts['jobs'] += 1
                        continue
                new = job.model_copy(
                    update={
                        "status": "failed",
                        "stage": "failed",
                        "updated_at": now,
                        "finished_at": now,
                        "error": "Генерация прервана перезапуском; автоматический платный повтор отключён",
                    }
                )
                conn.execute(
                    "UPDATE jobs SET status='failed',data=? WHERE id=? AND status='running'",
                    (_dump(new), job.id),
                )
                self._audit(conn, "job", job.id, "interrupted_failed", now)
                self._recovery_notification(
                    conn,
                    job.business_id,
                    "preparation_failed",
                    job.id,
                    None,
                    now,
                    "Подготовка материалов прервана перезапуском. Автоматического платного повтора не будет.",
                    job_id=job.id,
                )
                counts["jobs"] += 1
        return counts

    def _recovery_notification(
        self,
        conn: sqlite3.Connection,
        business_id: str,
        kind: str,
        entity_id: str,
        revision: int | None,
        now: datetime,
        text: str,
        *,
        post_id: str | None = None,
        job_id: str | None = None,
    ) -> None:
        business = self._business(conn, business_id)
        notification = Notification(
            id=str(uuid5(NAMESPACE_URL, f"ai-marketer:recovery:{business_id}:{kind}:{entity_id}:{revision}")),
            business_id=business_id,
            owner_id=business.owner_id,
            kind=kind,
            text=text,
            post_id=post_id,
            job_id=job_id,
            created_at=now,
        )
        self._add_notification(conn, notification)

    def _outbox(
        self,
        conn: sqlite3.Connection,
        notification: Notification | None,
        *,
        business_id: str,
        post_id: str | None = None,
        job_id: str | None = None,
    ) -> None:
        if notification is None:
            return
        if notification.business_id != business_id:
            raise InvalidInput("Уведомление относится к другому бизнесу")
        if notification.post_id is not None and notification.post_id != post_id:
            raise InvalidInput("Уведомление относится к другой публикации")
        if notification.job_id is not None and notification.job_id != job_id:
            raise InvalidInput("Уведомление относится к другому заданию")
        self._add_notification(conn, notification)

    def add_notification(self, notification: Notification) -> Notification:
        with self._transaction() as conn:
            return self._add_notification(conn, notification)

    def _add_notification(self, conn: sqlite3.Connection, notification: Notification) -> Notification:
        business = self._business(conn, notification.business_id)
        if notification.owner_id != business.owner_id:
            raise InvalidInput("Получатель уведомления не владелец бизнеса")
        if notification.post_id:
            post = self._post(conn, notification.post_id)
            if post.business_id != business.id:
                raise InvalidInput("Публикация уведомления принадлежит другому бизнесу")
        if notification.job_id:
            job = self._job(conn, notification.job_id)
            if job.business_id != business.id:
                raise InvalidInput("Задание уведомления принадлежит другому бизнесу")
        old = conn.execute("SELECT data FROM notifications WHERE id=?", (notification.id,)).fetchone()
        if old:
            if Notification.model_validate_json(old["data"]) != notification:
                raise Conflict("Идентификатор уведомления уже используется")
            return notification
        conn.execute(
            "INSERT INTO notifications(id,business_id,created_at,data,next_attempt_at) VALUES (?,?,?,?,?)",
            (
                notification.id,
                notification.business_id,
                _stamp(notification.created_at),
                _dump(notification),
                _stamp(notification.created_at),
            ),
        )
        self._audit(conn, "notification", notification.id, "queued", notification.created_at)
        return notification

    def pending_notifications(self, limit: int, now: datetime | None = None) -> list[Notification]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise InvalidInput("Лимит уведомлений должен быть от 1 до 1000")
        now = _utc(now or datetime.now(timezone.utc))
        with self._reader() as conn:
            return [
                Notification.model_validate_json(row["data"])
                for row in conn.execute(
                    "SELECT data FROM notifications WHERE acknowledged_at IS NULL AND attempts<? AND next_attempt_at<=? ORDER BY created_at,id LIMIT ?",
                    (MAX_NOTIFICATION_ATTEMPTS, _stamp(now), limit),
                )
            ]

    def acknowledge_notification(self, notification_id: str, now: datetime | None = None) -> None:
        now = _utc(now or datetime.now(timezone.utc))
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT acknowledged_at FROM notifications WHERE id=?", (notification_id,)
            ).fetchone()
            if row is None:
                raise NotFound("Уведомление не найдено")
            if row["acknowledged_at"]:
                return
            conn.execute(
                "UPDATE notifications SET acknowledged_at=? WHERE id=?", (_stamp(now), notification_id)
            )
            self._audit(conn, "notification", notification_id, "acknowledged", now)

    def list_failed_notifications(self, business_id: str, limit: int = 20) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInput("Лимит уведомлений должен быть от 1 до 100")
        with self._reader() as conn:
            self._business(conn, business_id)
            result = []
            for row in conn.execute(
                "SELECT data,attempts,last_attempt_at FROM notifications WHERE business_id=? "
                "AND acknowledged_at IS NULL AND attempts>=? ORDER BY created_at DESC,id DESC LIMIT ?",
                (business_id, MAX_NOTIFICATION_ATTEMPTS, limit),
            ):
                notification = Notification.model_validate_json(row["data"])
                result.append(
                    {
                        "id": notification.id,
                        "business_id": business_id,
                        "post_id": notification.post_id,
                        "job_id": notification.job_id,
                        "kind": notification.kind,
                        "attempts": row["attempts"],
                        "created_at": _stamp(notification.created_at),
                        "last_attempt_at": row["last_attempt_at"],
                        "error_code": "delivery_failed",
                    }
                )
            return result

    def retry_notification(self, notification_id: str, business_id: str, now: datetime) -> Notification:
        now = _utc(now)
        with self._transaction() as conn:
            self._business(conn, business_id)
            row = conn.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
            if row is None:
                raise NotFound("Уведомление не найдено")
            if row["business_id"] != business_id:
                raise Forbidden("Уведомление принадлежит другому бизнесу")
            if row["acknowledged_at"] is not None:
                raise Conflict("Уведомление уже доставлено")
            notification = Notification.model_validate_json(row["data"])
            if row["attempts"] < MAX_NOTIFICATION_ATTEMPTS:
                # A repeated button must not keep resetting the retry budget.
                return notification
            conn.execute(
                "UPDATE notifications SET attempts=0,next_attempt_at=?,last_attempt_at=NULL WHERE id=?",
                (_stamp(now), notification_id),
            )
            self._audit(
                conn, "notification", notification_id, "manual_retry", now, previous_attempts=row["attempts"]
            )
            return notification

    def get_failed_notification(self, notification_id: str) -> dict[str, Any]:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT data,attempts,last_attempt_at FROM notifications WHERE id=? "
                "AND acknowledged_at IS NULL AND attempts>=?",
                (notification_id, MAX_NOTIFICATION_ATTEMPTS),
            ).fetchone()
            if row is None:
                raise NotFound("Неудачное уведомление не найдено или уже возвращено в очередь")
            notification = Notification.model_validate_json(row["data"])
            return {
                "id": notification.id,
                "business_id": notification.business_id,
                "post_id": notification.post_id,
                "job_id": notification.job_id,
                "kind": notification.kind,
                "attempts": row["attempts"],
                "created_at": _stamp(notification.created_at),
                "last_attempt_at": row["last_attempt_at"],
                "error_code": "delivery_failed",
            }

    def record_notification_failure(self, notification_id: str, now: datetime) -> None:
        now = _utc(now)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT attempts,acknowledged_at FROM notifications WHERE id=?", (notification_id,)
            ).fetchone()
            if row is None:
                raise NotFound("Уведомление не найдено")
            if row["acknowledged_at"] or row["attempts"] >= MAX_NOTIFICATION_ATTEMPTS:
                return
            attempts = row["attempts"] + 1
            delay = min(3600, 30 * 2 ** (attempts - 1))
            conn.execute(
                "UPDATE notifications SET attempts=?,last_attempt_at=?,next_attempt_at=? WHERE id=?",
                (attempts, _stamp(now), _stamp(now + timedelta(seconds=delay)), notification_id),
            )
            self._audit(
                conn,
                "notification",
                notification_id,
                "exhausted" if attempts == MAX_NOTIFICATION_ATTEMPTS else "retry_scheduled",
                now,
                attempts=attempts,
            )

    def record_metric(self, metric: MetricSnapshot) -> bool:
        with self._transaction() as conn:
            self._business(conn, metric.business_id)
            if metric.name == "reactions" and not metric.post_id:
                raise InvalidInput("Реакции должны относиться к публикации")
            if metric.name == "subscribers" and metric.post_id:
                raise InvalidInput("Число подписчиков относится ко всему бизнесу")
            if metric.post_id:
                post = self._post(conn, metric.post_id)
                if post.business_id != metric.business_id:
                    raise InvalidInput("Метрика относится к публикации другого бизнеса")
            old = conn.execute(
                "SELECT data FROM metrics WHERE business_id=? AND event_id=?",
                (metric.business_id, metric.event_id),
            ).fetchone()
            if old:
                if MetricSnapshot.model_validate_json(old["data"]) != metric:
                    raise Conflict("Событие метрики уже сохранено с другими данными")
                return False
            conn.execute(
                "INSERT INTO metrics(business_id,event_id,name,post_id,source,observed_at,data) VALUES (?,?,?,?,?,?,?)",
                (
                    metric.business_id,
                    metric.event_id,
                    metric.name,
                    metric.post_id,
                    metric.source,
                    _stamp(metric.observed_at),
                    _dump(metric),
                ),
            )
            return True

    def report_input(self, business_id: str, start: datetime, end: datetime) -> ReportInput:
        """Return all business posts, period samples, and prior baseline samples.

        The interval is inclusive [start, end]. Posts are intentionally not
        filtered: a post created earlier can be published or receive reactions
        during this period. The report builder filters created_at/published_at
        according to each counter's definition. For every metric stream
        (name, post_id, source), include its latest sample strictly before start
        as the baseline. No future samples are included; missing stays missing.
        """
        start, end = _utc(start), _utc(end)
        if end < start:
            raise InvalidInput("Конец отчётного периода раньше начала")
        with self._reader() as conn:
            conn.execute("BEGIN")  # Consistent snapshot across both tables.
            self._business(conn, business_id)
            posts = [
                PostView.model_validate_json(row["data"])
                for row in conn.execute(
                    "SELECT data FROM posts WHERE business_id=? ORDER BY rowid", (business_id,)
                )
            ]
            rows = conn.execute(
                "SELECT data FROM metrics WHERE business_id=? AND observed_at<=? ORDER BY observed_at,event_id",
                (business_id, _stamp(end)),
            ).fetchall()
            baseline: dict[tuple[str, str | None, str], MetricSnapshot] = {}
            period: list[MetricSnapshot] = []
            for row in rows:
                metric = MetricSnapshot.model_validate_json(row["data"])
                if metric.observed_at < start:
                    baseline[(metric.name, metric.post_id, metric.source)] = metric
                else:
                    period.append(metric)
            metrics = sorted([*baseline.values(), *period], key=lambda m: (m.observed_at, m.event_id))
            return ReportInput(business_id=business_id, start=start, end=end, posts=posts, metrics=metrics)

    def audit_events(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        """Read-only diagnostic history; payloads contain no provider responses."""
        with self._reader() as conn:
            return [
                {**dict(row), "details": json.loads(row["details"])}
                for row in conn.execute(
                    "SELECT id,entity_type,entity_id,action,at,details FROM audit WHERE entity_type=? AND entity_id=? ORDER BY id",
                    (entity_type, entity_id),
                )
            ]
