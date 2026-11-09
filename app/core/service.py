import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.ai.sources import extract_source
from app.reports.summary import summarize
from app.shared.models import (
    CAPTION_LIMIT, BusinessProfile, Channel, ContentPlanDraft, ExtractedSource,
    PostView, ReactionSnapshot, Source, Status, SubscriberSnapshot,
)
from app.shared.ports import AppError, ContentAI, ImageGenerator, Publisher
from app.storage.db import POST_QUERY, Database, as_post, stamp


class Marketing:
    def __init__(
        self, db: Database, ai: ContentAI, images: ImageGenerator, publisher: Publisher,
        owner_id: int, timezone: ZoneInfo, clock: Callable[[], datetime] | None = None,
    ):
        self.db = db
        self.ai = ai
        self.images = images
        self.publisher = publisher
        self.owner_id = owner_id
        self.timezone = timezone
        self.clock = clock or (lambda: datetime.now(UTC))
        self.generation = asyncio.Semaphore(2)

    def now(self) -> datetime:
        return self.clock().astimezone(self.timezone)

    def authorize(self, user: int) -> None:
        if user != self.owner_id:
            raise AppError("Управлять этим бизнесом может только назначенный владелец.")

    def claim_update(self, user: int, key: str) -> bool:
        self.authorize(user)
        with self.db.connect(write=True) as db:
            db.execute("DELETE FROM updates WHERE received_at < ?", (stamp(self.now() - timedelta(days=7)),))
            return bool(db.execute("INSERT OR IGNORE INTO updates VALUES (?, ?)", (key, stamp(self.now()))).rowcount)

    def business(self, user: int):
        self.authorize(user)
        with self.db.connect() as db:
            return db.execute("SELECT * FROM business WHERE id = 1").fetchone()

    def set_name(self, user: int, name: str) -> None:
        self.authorize(user)
        name = name.strip()
        if not 1 <= len(name) <= 100:
            raise AppError("Название должно содержать от 1 до 100 символов.")
        with self.db.connect(write=True) as db:
            db.execute(
                "UPDATE business SET name=?, confirmed=0, profile=NULL, profile_revision=profile_revision+1",
                (name,),
            )

    async def add_source(self, user: int, kind: str, label: str, data: str | bytes) -> int:
        self.authorize(user)
        if kind not in {"website", "pdf", "notes"}:
            raise AppError("Поддерживаются публичная страница, текстовый PDF и заметки.")
        extracted = await extract_source(Source(id=0, kind=kind, label=label[:300]), data)
        asset = (
            await asyncio.to_thread(self.db.save_asset, data, "pdf")
            if isinstance(data, bytes) else None
        )
        with self.db.connect(write=True) as db:
            if db.execute("SELECT count(*) FROM sources").fetchone()[0] >= 20:
                raise AppError("Достигнут лимит из 20 источников.")
            source_id = db.execute(
                "INSERT INTO sources(kind, label, body, asset) VALUES (?, ?, ?, ?)",
                (kind, extracted.source.label, extracted.text, asset),
            ).lastrowid
            db.execute("UPDATE business SET confirmed=0, profile=NULL, profile_revision=profile_revision+1")
        return source_id

    def sources(self, user: int) -> list[Source]:
        self.authorize(user)
        with self.db.connect() as db:
            return [Source(**dict(row)) for row in db.execute("SELECT id, kind, label FROM sources ORDER BY id")]

    async def build_profile(self, user: int) -> tuple[BusinessProfile, int]:
        self.authorize(user)
        with self.db.connect() as db:
            business = db.execute("SELECT name, profile_revision FROM business").fetchone()
            rows = db.execute("SELECT * FROM sources ORDER BY id").fetchall()
        sources = [ExtractedSource(
            source=Source(id=r["id"], kind=r["kind"], label=r["label"]), text=r["body"],
        ) for r in rows]
        if not sources:
            raise AppError("Добавьте хотя бы один источник.")
        if business["name"]:
            sources.insert(0, ExtractedSource(
                source=Source(id=0, kind="notes", label="Название от владельца"), text=business["name"],
            ))
        async with self.generation:
            profile = await self.ai.build_profile(sources)
        if business["name"]:
            profile = profile.model_copy(update={"name": business["name"]})
        with self.db.connect(write=True) as db:
            revision = business["profile_revision"] + 1
            result = db.execute(
                "UPDATE business SET profile=?, confirmed=0, profile_revision=? WHERE profile_revision=?",
                (profile.model_dump_json(), revision, business["profile_revision"]),
            )
            if not result.rowcount:
                raise AppError("Источники изменились. Соберите карточку заново.")
        return profile, revision

    def confirm_profile(self, user: int, revision: int) -> None:
        self.authorize(user)
        with self.db.connect(write=True) as db:
            if not db.execute(
                "UPDATE business SET confirmed=1 WHERE profile IS NOT NULL AND profile_revision=?", (revision,),
            ).rowcount:
                raise AppError("Карточка уже изменилась. Откройте актуальную версию.")

    async def connect_channel(self, user: int, value: int | str) -> Channel:
        self.authorize(user)
        channel = await self.publisher.check_channel(value)
        with self.db.connect(write=True) as db:
            old = db.execute("SELECT channel FROM business").fetchone()[0]
            previous = Channel.model_validate_json(old) if old else None
            if previous is not None and previous.id != channel.id:
                if db.execute("SELECT 1 FROM posts WHERE status='sending'").fetchone():
                    raise AppError("Идёт отправка. Смените канал после её завершения.")
                rows = db.execute(POST_QUERY + " WHERE p.status IN ('draft', 'scheduled', 'failed')").fetchall()
                for row in rows:
                    post = as_post(row)
                    self._new_version(db, post, channel=channel)
            db.execute("UPDATE business SET channel=?", (channel.model_dump_json(),))
        return channel

    def _confirmed(self, user: int):
        record = self.business(user)
        if not record["profile"] or not record["confirmed"]:
            raise AppError("Сначала соберите и подтвердите карточку бизнеса.")
        return BusinessProfile.model_validate_json(record["profile"]), record["profile_revision"]

    async def interpret_request(self, user: int, text: str, has_post: bool):
        self.authorize(user)
        async with self.generation:
            return await self.ai.interpret_request(text, has_post)

    async def make_plan(self, user: int, brief: str, count: int = 3, automatic: bool = False) -> int:
        profile, revision = self._confirmed(user)
        if not 3 <= count <= 5 or not brief.strip() or len(brief) > 2000:
            raise AppError("Укажите задачу длиной до 2000 символов и от 3 до 5 постов.")
        async with self.generation:
            draft = await self.ai.make_plan(profile, brief, count, self.now())
        now = self.now()
        times = [item.scheduled_at for item in draft.items]
        if len(times) != count or times != sorted(set(times)) or times[0] <= now or times[-1] > now + timedelta(days=90):
            raise AppError("Модель предложила неверные даты. Уточните период и повторите запрос.")
        if len({item.topic.strip().casefold() for item in draft.items}) != count:
            raise AppError("Модель повторила темы. Уточните задачу и повторите запрос.")
        with self.db.connect(write=True) as db:
            if db.execute("SELECT profile_revision FROM business").fetchone()[0] != revision:
                raise AppError("Карточка изменилась во время работы. Создайте план заново.")
            return db.execute(
                "INSERT INTO plans(profile, body, created_at, automatic) VALUES (?, ?, ?, ?)",
                (profile.model_dump_json(), draft.model_dump_json(), stamp(now), int(automatic)),
            ).lastrowid

    def plan(self, user: int, plan_id: int):
        self.authorize(user)
        with self.db.connect() as db:
            row = db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise AppError("План не найден.")
        return row, ContentPlanDraft.model_validate_json(row["body"])

    def plans(self, user: int):
        self.authorize(user)
        with self.db.connect() as db:
            return db.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 15").fetchall()

    def posts(self, user: int, plan_id: int | None = None) -> list[PostView]:
        self.authorize(user)
        with self.db.connect() as db:
            if plan_id is None:
                rows = db.execute(POST_QUERY + " ORDER BY v.scheduled_at LIMIT 50")
            else:
                rows = db.execute(POST_QUERY + " WHERE p.plan_id=? ORDER BY p.item_index", (plan_id,))
            return [as_post(row) for row in rows]

    def queue(self, user: int) -> list[PostView]:
        self.authorize(user)
        with self.db.connect() as db:
            rows = db.execute(POST_QUERY + """
                WHERE p.status IN ('draft','scheduled','sending','delivery_unknown','failed')
                ORDER BY CASE p.status WHEN 'delivery_unknown' THEN 0 WHEN 'failed' THEN 1
                                       WHEN 'scheduled' THEN 2 ELSE 3 END,
                         v.scheduled_at LIMIT 15
            """)
            return [as_post(row) for row in rows]

    def post(self, user: int, post_id: int) -> PostView:
        self.authorize(user)
        with self.db.connect() as db:
            return self._post(db, post_id)

    @staticmethod
    def _post(db, post_id: int) -> PostView:
        row = db.execute(POST_QUERY + " WHERE p.id=?", (post_id,)).fetchone()
        if row is None:
            raise AppError("Пост не найден.")
        return as_post(row)

    async def prepare(self, user: int, plan_id: int) -> list[PostView]:
        record, plan = self.plan(user, plan_id)
        with self.db.connect(write=True) as db:
            status = db.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
            if status == "ready":
                return self.posts(user, plan_id)
            if status == "generating":
                raise AppError("Этот план уже готовится.")
            channel_json = db.execute("SELECT channel FROM business").fetchone()[0]
            if not channel_json:
                raise AppError("Сначала подключите Telegram-канал.")
            db.execute("UPDATE plans SET status='generating', error=NULL WHERE id=?", (plan_id,))
        profile = BusinessProfile.model_validate_json(record["profile"])
        channel = Channel.model_validate_json(channel_json)
        prepared = []
        try:
            async with self.generation:
                for item in plan.items:
                    draft = await self.ai.write_post(profile, item)
                    self._validate_caption(draft.text)
                    image = await self.images.generate(profile, item, draft)
                    asset = await asyncio.to_thread(self.db.save_asset, image.data, image.format)
                    prepared.append((item, draft, asset))
            with self.db.connect(write=True) as db:
                current = Channel.model_validate_json(db.execute("SELECT channel FROM business").fetchone()[0])
                if current.id != channel.id:
                    raise AppError("Канал изменился. Запустите подготовку повторно.")
                for index, (item, draft, asset) in enumerate(prepared):
                    post_id = db.execute(
                        "INSERT INTO posts(plan_id, item_index) VALUES (?, ?)", (plan_id, index),
                    ).lastrowid
                    db.execute(
                        "INSERT INTO versions VALUES (?, 1, ?, ?, ?, ?, ?)",
                        (post_id, draft.model_dump_json(), asset, current.model_dump_json(),
                         current.id, stamp(item.scheduled_at)),
                    )
                    self.db.event(db, "prepared", self.now())
                db.execute("UPDATE plans SET status='ready' WHERE id=?", (plan_id,))
        except (Exception, asyncio.CancelledError) as exc:
            message = str(exc) if isinstance(exc, AppError) else "Подготовка материалов прервалась."
            with self.db.connect(write=True) as db:
                db.execute("UPDATE plans SET status='failed', error=? WHERE id=?", (message, plan_id))
                self.db.event(db, "error", self.now())
            raise
        return self.posts(user, plan_id)

    @staticmethod
    def _validate_caption(text: str) -> None:
        if not text.strip() or len(text.encode("utf-16-le")) // 2 > CAPTION_LIMIT:
            raise AppError("Текст пуст или превышает лимит 950 символов.")

    @staticmethod
    def _expect(post: PostView, version: int, allowed: set[Status]) -> None:
        if post.version != version:
            raise AppError("Эта версия устарела. Откройте актуальный предпросмотр.")
        if post.status not in allowed:
            raise AppError("Действие недоступно для текущего статуса поста.")

    def _new_version(self, db, post: PostView, **changes) -> PostView:
        updated = post.model_copy(update={
            "version": post.version + 1, "status": Status.DRAFT, "error": None, **changes,
        })
        db.execute(
            "INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (updated.id, updated.version, updated.draft.model_dump_json(), updated.image_id,
             updated.channel.model_dump_json(), updated.channel.id, stamp(updated.scheduled_at)),
        )
        db.execute(
            "UPDATE posts SET version=?, status='draft', approved_version=NULL, approved_by=NULL, error=NULL WHERE id=?",
            (updated.version, updated.id),
        )
        return updated

    async def revise(self, user: int, post_id: int, version: int, field: str, value: str) -> PostView:
        post = self.post(user, post_id)
        allowed = {Status.DRAFT, Status.SCHEDULED, Status.FAILED, Status.REJECTED}
        self._expect(post, version, allowed)
        if not value.strip() or len(value) > 2000:
            raise AppError("Введите правку длиной от 1 до 2000 символов.")
        changes = {}
        if field == "time":
            try:
                when = datetime.fromisoformat(value.strip())
            except ValueError as exc:
                raise AppError("Введите дату со смещением, например 2026-12-15 10:30+03:00.") from exc
            now = self.now()
            if when.utcoffset() is None or not now < when <= now + timedelta(days=90):
                raise AppError("Нужно время со смещением, в будущем и не дальше 90 дней.")
            changes["scheduled_at"] = when
        elif field in {"text", "image"}:
            record, plan = self.plan(user, post.plan_id)
            with self.db.connect() as db:
                index = db.execute("SELECT item_index FROM posts WHERE id=?", (post_id,)).fetchone()[0]
            profile = BusinessProfile.model_validate_json(record["profile"])
            async with self.generation:
                if field == "text":
                    draft = await self.ai.revise_text(profile, post.draft, value)
                    self._validate_caption(draft.text)
                    changes["draft"] = draft.model_copy(update={"image_prompt": post.draft.image_prompt})
                else:
                    try:
                        current = await asyncio.to_thread(self.db.read_asset, post.image_id)
                    except AppError:
                        current = None
                    image = await self.images.generate(profile, plan.items[index], post.draft, value, current)
                    asset = await asyncio.to_thread(self.db.save_asset, image.data, image.format)
                    if current is not None and asset == post.image_id:
                        raise AppError("Генератор вернул прежнее изображение. Уточните правку.")
                    changes["image_id"] = asset
        else:
            raise AppError("Неизвестный тип правки.")
        with self.db.connect(write=True) as db:
            current = self._post(db, post_id)
            self._expect(current, version, allowed)
            return self._new_version(db, current, **changes)

    async def approve(self, user: int, post_id: int, version: int) -> bool:
        post = self.post(user, post_id)
        self._expect(post, version, {Status.DRAFT, Status.SCHEDULED})
        if post.status == Status.SCHEDULED:
            return False
        await self.publisher.check_channel(post.channel.id)
        await asyncio.to_thread(self.db.read_asset, post.image_id)
        with self.db.connect(write=True) as db:
            post = self._post(db, post_id)
            self._expect(post, version, {Status.DRAFT, Status.SCHEDULED})
            if post.status == Status.SCHEDULED:
                return False
            if post.scheduled_at <= self.now():
                raise AppError("Выбранное время прошло. Измените время и согласуйте новую версию.")
            self._validate_caption(post.draft.text)
            db.execute(
                "UPDATE posts SET status='scheduled', approved_version=?, approved_by=? WHERE id=?",
                (version, user, post_id),
            )
        return True

    def reject(self, user: int, post_id: int, version: int) -> None:
        self.authorize(user)
        with self.db.connect(write=True) as db:
            post = self._post(db, post_id)
            self._expect(post, version, {Status.DRAFT, Status.SCHEDULED, Status.FAILED})
            db.execute(
                "UPDATE posts SET status='rejected', approved_version=NULL, approved_by=NULL, error=NULL WHERE id=?",
                (post_id,),
            )
            self.db.event(db, "rejected", self.now())

    def configure_automation(self, user: int, days: int | None, brief: str = "") -> None:
        self.authorize(user)
        next_run = None
        if days is not None:
            if not 1 <= days <= 30 or not brief.strip() or len(brief) > 2000:
                raise AppError("Введите интервал 1–30 дней и тему длиной до 2000 символов.")
            self._confirmed(user)
            if not self.business(user)["channel"]:
                raise AppError("Сначала подключите канал.")
            next_run = stamp(self.now() + timedelta(days=days))
        with self.db.connect(write=True) as db:
            db.execute("UPDATE business SET auto_days=?, auto_next=?, auto_brief=?", (days, next_run, brief))

    def record_reaction(self, sample: ReactionSnapshot) -> None:
        with self.db.connect(write=True) as db:
            if db.execute(
                """SELECT 1 FROM posts p JOIN versions v ON v.post_id=p.id AND v.version=p.version
                   WHERE p.message_id=? AND v.channel_id=? AND p.status='published'""",
                (sample.message_id, sample.channel_id),
            ).fetchone():
                db.execute(
                    "INSERT OR REPLACE INTO reaction_samples VALUES (?, ?, ?, ?, ?)",
                    (sample.channel_id, sample.message_id, stamp(sample.measured_at), sample.source, sample.total),
                )

    def report(self, user: int, days: int):
        self.authorize(user)
        if not 1 <= days <= 90:
            raise AppError("Выберите период от 1 до 90 дней.")
        end = self.now()
        start = end - timedelta(days=days)
        lower, upper = stamp(start), stamp(end)
        record = self.business(user)
        channel = Channel.model_validate_json(record["channel"]) if record["channel"] else None
        with self.db.connect() as db:
            events = [r[0] for r in db.execute("SELECT event FROM events WHERE at BETWEEN ? AND ?", (lower, upper))]
            states = [r[0] for r in db.execute("SELECT status FROM posts WHERE status IN ('draft','scheduled','sending')")]
            reactions = [
                ReactionSnapshot(**dict(r)) for r in db.execute(
                    "SELECT * FROM reaction_samples WHERE measured_at BETWEEN ? AND ?", (lower, upper),
                )
            ]
            subscribers = [
                SubscriberSnapshot(**dict(r)) for r in db.execute(
                    "SELECT * FROM subscriber_samples WHERE channel_id=? AND measured_at<=?",
                    (channel.id if channel else 0, upper),
                )
            ]
        return summarize(start, end, events, states, reactions, subscribers)
