"""SQLite storage with immutable revisions and transactional publication claims.

All public methods are synchronous and open short-lived connections. Callers may
run them in a worker thread. Remote/provider calls must happen outside a method.
The schema supports a single application process; SQLite CAS also protects
against concurrent workers in that process. Interrupted sends are deliberately
not retried: an operator must resolve their delivery status.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4
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
)

SCHEMA_VERSION = 1
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


class Repository:
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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._target, timeout=5, isolation_level=None, uri=self._uri)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
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
    def _reader(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
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
        with self._transaction() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise Conflict("Версия базы новее поддерживаемой приложением")
            if version == SCHEMA_VERSION:
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
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

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
        allowed = {"text", "image_asset_id", "channel_id", "scheduled_at", "is_demo"}
        if set(data) - allowed:
            raise InvalidInput("Неизвестные поля публикации")
        text = data.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 900:
            raise InvalidInput("Текст публикации должен содержать от 1 до 900 символов")
        channel_id = data.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id or channel_id != business.channel_id:
            raise InvalidInput("Публикация должна быть адресована текущему каналу бизнеса")
        asset = Repository._asset(conn, data.get("image_asset_id", ""), business.id)
        if asset.mime_type not in {"image/png", "image/jpeg"}:
            raise InvalidInput("Для публикации требуется PNG или JPEG")
        scheduled = data.get("scheduled_at")
        if scheduled is not None:
            scheduled = _utc(scheduled)
        return {
            "text": text,
            "image_asset_id": asset.id,
            "channel_id": channel_id,
            "scheduled_at": scheduled,
            "is_demo": bool(data.get("is_demo", False) or asset.is_demo),
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
    ) -> list[PostView]:
        now = _utc(now)
        if not 1 <= len(items) <= 5:
            raise InvalidInput("Создавайте от 1 до 5 публикаций за один раз")
        with self._transaction() as conn:
            business = self._business(conn, business_id)
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
                post = PostView(
                    id=_new_id(),
                    business_id=business_id,
                    revision=1,
                    status=PostStatus.REVIEW,
                    created_at=now,
                    updated_at=now,
                    job_id=job_id,
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
                done = job.model_copy(update={"status": "done", "finished_at": now})
                changed = conn.execute(
                    "UPDATE jobs SET status='done',data=? WHERE id=? AND status='running'",
                    (_dump(done), job.id),
                )
                if changed.rowcount != 1:
                    raise Conflict("Задание уже завершено")
                self._audit(conn, "job", job.id, "done", now, post_ids=[p.id for p in posts])
            return posts

    def get_post(self, post_id: str) -> PostView:
        with self._reader() as conn:
            return self._post(conn, post_id)

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
        allowed = {"text", "image_asset_id", "channel_id", "scheduled_at", "is_demo"}
        if not changes or set(changes) - allowed:
            raise InvalidInput("Укажите допустимые изменения публикации")
        with self._transaction() as conn:
            post = self._post(conn, post_id)
            self._check_revision(post, expected_revision)
            if post.status in {PostStatus.PUBLISHING, PostStatus.PUBLISHED, PostStatus.DELIVERY_UNKNOWN}:
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

    def approve_post(self, post_id: str, expected_revision: int, actor_id: int, now: datetime) -> PostView:
        now = _utc(now)
        with self._transaction() as conn:
            post = self._post(conn, post_id)
            self._check_revision(post, expected_revision)
            business = self._business(conn, post.business_id)
            if actor_id != business.owner_id:
                raise Forbidden("Согласовывать публикации может только владелец")
            if post.channel_id != business.channel_id:
                raise Conflict("Канал бизнеса изменился; обновите предпросмотр")
            if post.status == PostStatus.APPROVED and post.approved_revision == post.revision:
                return post
            if post.status != PostStatus.REVIEW:
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
                if (
                    post.approved_revision != post.revision
                    or post.approved_by != business.owner_id
                    or post.channel_id != business.channel_id
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
        self, post_id: str, revision: int, result: PublicationResult, now: datetime
    ) -> PostView:
        now = _utc(now)
        with self._transaction() as conn:
            post = self._post(conn, post_id)
            self._check_revision(post, revision)
            if post.status != PostStatus.PUBLISHING:
                raise Conflict("Публикация не находится в отправке")
            return self._complete_publication(conn, post, result, now, "published")

    def fail_publication(
        self, post_id: str, revision: int, error: str, unknown: bool, now: datetime
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
    ) -> Job:
        now = _utc(now)
        if not isinstance(request, str) or not 1 <= len(request.strip()) <= 4000:
            raise InvalidInput("Некорректная задача генерации")
        if not isinstance(dedup_key, str) or not 1 <= len(dedup_key) <= 300:
            raise InvalidInput("Некорректный ключ задания")
        with self._transaction() as conn:
            self._business(conn, business_id)
            job, _ = self._enqueue(
                conn,
                business_id,
                request.strip(),
                dedup_key,
                now,
                max_pending=max_pending,
                max_per_day=max_per_day,
            )
            return job

    def get_job(self, job_id: str) -> Job:
        with self._reader() as conn:
            return self._job(conn, job_id)

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
            claimed = job.model_copy(update={"status": "running", "started_at": now})
            result = conn.execute(
                "UPDATE jobs SET status='running',data=? WHERE id=? AND status='pending'",
                (_dump(claimed), job.id),
            )
            if result.rowcount != 1:
                raise Conflict("Задание уже забрано другим обработчиком")
            self._audit(conn, "job", job.id, "claimed", now)
            return claimed

    def fail_job(self, job_id: str, error: str, now: datetime) -> Job:
        now = _utc(now)
        with self._transaction() as conn:
            job = self._job(conn, job_id)
            if job.status not in {"pending", "running"}:
                raise Conflict("Задание уже завершено")
            failed = job.model_copy(
                update={"status": "failed", "finished_at": now, "error": str(error)[:2000]}
            )
            conn.execute(
                "UPDATE jobs SET status='failed',data=? WHERE id=? AND status=?",
                (_dump(failed), job.id, job.status),
            )
            self._audit(conn, "job", job.id, "failed", now)
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
                if not self._has_job_capacity(conn, business, now, max_pending, max_per_day):
                    # Leave due time unchanged; a later scheduler pass retries
                    # when the queue clears or a new local calendar day starts.
                    continue
                interval = timedelta(hours=business.interval_hours)
                # At most one job after downtime, anchored to the latest due slot.
                # Older missed slots are skipped to avoid a paid generation burst.
                skipped = (now - due) // interval
                slot = due + skipped * interval
                key = f"automation:{_stamp(slot)}"
                _, inserted = self._enqueue(conn, business.id, business.automation_request, key, now)
                count += int(inserted)
                new = business.model_copy(update={"next_generation_at": slot + interval})
                self._save_business(conn, new)
                self._audit(
                    conn,
                    "business",
                    business.id,
                    "automation_scheduled",
                    now,
                    slot=_stamp(slot),
                    skipped_slots=int(skipped),
                    inserted=inserted,
                )
        return count

    def recover_interrupted(self, now: datetime) -> dict[str, int]:
        now = _utc(now)
        counts = {"posts": 0, "jobs": 0}
        with self._transaction() as conn:
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
                counts["posts"] += 1
            rows = conn.execute("SELECT data FROM jobs WHERE status='running'").fetchall()
            for row in rows:
                job = Job.model_validate_json(row["data"])
                new = job.model_copy(
                    update={
                        "status": "failed",
                        "finished_at": now,
                        "error": "Генерация прервана перезапуском; автоматический платный повтор отключён",
                    }
                )
                conn.execute(
                    "UPDATE jobs SET status='failed',data=? WHERE id=? AND status='running'",
                    (_dump(new), job.id),
                )
                self._audit(conn, "job", job.id, "interrupted_failed", now)
                counts["jobs"] += 1
        return counts

    def add_notification(self, notification: Notification) -> Notification:
        with self._transaction() as conn:
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
