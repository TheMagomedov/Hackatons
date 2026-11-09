import asyncio
import logging
from datetime import datetime, timedelta

from app.core.service import Marketing
from app.publishing.links import message_link
from app.shared.models import Channel, PublicationResult, Status, SubscriberSnapshot
from app.shared.ports import AppError, DeliveryUnknown, Notifier
from app.storage.db import POST_QUERY, as_post, stamp

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, app: Marketing, notifier: Notifier):
        self.app = app
        self.notifier = notifier

    async def _notify(self, text: str) -> None:
        try:
            await self.notifier.notify(self.app.owner_id, text)
        except Exception as exc:
            log.warning("Owner notification failed (%s)", type(exc).__name__)

    async def recover(self) -> None:
        with self.app.db.connect(write=True) as db:
            interrupted = db.execute("SELECT id FROM posts WHERE status='sending'").fetchall()
            for row in interrupted:
                db.execute(
                    "UPDATE posts SET status='delivery_unknown', error=? WHERE id=?",
                    ("Отправка прервалась. Проверьте канал перед любым повтором.", row["id"]),
                )
                self.app.db.event(db, "error", self.app.now())
            db.execute(
                "UPDATE plans SET status='failed', error=? WHERE status='generating'",
                ("Подготовка прервалась. Её можно запустить повторно вручную.",),
            )
            db.execute("UPDATE automatic_runs SET status='interrupted' WHERE status='running'")
        for row in interrupted:
            await self._notify(f"Пост #{row['id']}: после перезапуска результат неизвестен. Проверьте канал.")

    async def publish_due(self) -> None:
        now = self.app.now()
        with self.app.db.connect(write=True) as db:
            candidates = db.execute(
                POST_QUERY + """
                WHERE p.status='scheduled' AND p.approved_version=p.version AND p.approved_by=?
                  AND v.scheduled_at<=? ORDER BY v.scheduled_at LIMIT 10
                """, (self.app.owner_id, stamp(now)),
            ).fetchall()
        for row in candidates:
            with self.app.db.connect(write=True) as db:
                claimed = db.execute(
                    """UPDATE posts SET status='sending'
                       WHERE id=? AND version=? AND status='scheduled'
                         AND approved_version=version AND approved_by=?""",
                    (row["id"], row["version"], self.app.owner_id),
                ).rowcount
                channel_json = db.execute("SELECT channel FROM business").fetchone()[0]
            if not claimed:
                continue
            post = as_post(row)
            try:
                self.app._validate_caption(post.draft.text)
                if not channel_json:
                    raise AppError("Выбранный канал недоступен.")
                if Channel.model_validate_json(channel_json).id != post.channel.id:
                    raise AppError("Канал изменился. Согласуйте пост заново.")
                channel = await self.app.publisher.check_channel(post.channel.id)
                image = await asyncio.to_thread(self.app.db.read_asset, post.image_id)
            except Exception as exc:
                await self._fail(post.id, Status.FAILED, exc)
                continue
            try:
                result = await self.app.publisher.publish(post.model_copy(update={"channel": channel}), image)
                self._mark_published(post.id, result)
            except DeliveryUnknown as exc:
                await self._fail(post.id, Status.DELIVERY_UNKNOWN, exc)
            except AppError as exc:
                await self._fail(post.id, Status.FAILED, exc)
            except Exception as exc:
                await self._fail(post.id, Status.DELIVERY_UNKNOWN, exc)
            else:
                suffix = f"\n{result.link}" if result.link else ""
                await self._notify(f"Пост #{post.id} опубликован.{suffix}")

    async def _fail(self, post_id: int, status: Status, exc: Exception) -> None:
        if isinstance(exc, AppError):
            explanation = str(exc)
        elif status == Status.DELIVERY_UNKNOWN:
            explanation = "Результат неизвестен. Проверьте канал перед любым повтором."
        else:
            explanation = "Не удалось проверить условия публикации."
        log.warning("Publish %s for post %d (%s)", status.value, post_id, type(exc).__name__)
        with self.app.db.connect(write=True) as db:
            db.execute("UPDATE posts SET status=?, error=? WHERE id=?", (status, explanation, post_id))
            self.app.db.event(db, "error", self.app.now())
        await self._notify(f"Пост #{post_id}: {explanation}")

    def _mark_published(self, post_id: int, result: PublicationResult) -> None:
        with self.app.db.connect(write=True) as db:
            db.execute(
                """UPDATE posts SET status='published', message_id=?, publication_link=?, published_at=?,
                   error=NULL WHERE id=?""",
                (result.message_id, result.link, stamp(result.published_at), post_id),
            )
            self.app.db.event(db, "published", result.published_at)

    def resolve_unknown(
        self, user: int, post_id: int, version: int, channel_id: int | None = None,
        message_id: int | None = None, published_at: datetime | None = None,
        caption: str = "", has_photo: bool = False,
    ) -> None:
        self.app.authorize(user)
        with self.app.db.connect(write=True) as db:
            post = self.app._post(db, post_id)
            self.app._expect(post, version, {Status.DELIVERY_UNKNOWN})
            if message_id is None:
                db.execute(
                    """UPDATE posts SET status='rejected', error=NULL, approved_version=NULL,
                       approved_by=NULL WHERE id=?""", (post_id,),
                )
                self.app.db.event(db, "rejected", self.app.now())
                return
            if (
                channel_id != post.channel.id or not has_photo or caption != post.draft.text
                or published_at is None or published_at < post.scheduled_at.replace(microsecond=0)
            ):
                raise AppError("Перешлите опубликованный пост с фото и точным текстом из выбранного канала.")
            if db.execute(
                """SELECT 1 FROM posts p JOIN versions v ON v.post_id=p.id AND v.version=p.version
                   WHERE p.message_id=? AND v.channel_id=? AND p.status='published'""",
                (message_id, channel_id),
            ).fetchone():
                raise AppError("Это сообщение уже связано с другой публикацией.")
            link = message_link(post.channel, message_id)
            db.execute(
                """UPDATE posts SET status='published', message_id=?, publication_link=?,
                   published_at=?, error=NULL WHERE id=?""",
                (message_id, link, stamp(published_at), post_id),
            )
            self.app.db.event(db, "published", published_at)

    async def prepare_automatic(self) -> None:
        now = self.app.now()
        with self.app.db.connect(write=True) as db:
            row = db.execute("SELECT auto_days, auto_next, auto_brief FROM business").fetchone()
            period = row["auto_next"]
            if not period or period > stamp(now):
                return
            next_run = stamp(now + timedelta(days=row["auto_days"]))
            db.execute("UPDATE business SET auto_next=?", (next_run,))
            if not db.execute("INSERT OR IGNORE INTO automatic_runs VALUES (?, 'running')", (period,)).rowcount:
                return
        try:
            plan_id = await self.app.make_plan(self.app.owner_id, row["auto_brief"], automatic=True)
            business = self.app.business(self.app.owner_id)
            if business["auto_next"] != next_run:
                with self.app.db.connect(write=True) as db:
                    db.execute("UPDATE automatic_runs SET status='stopped' WHERE period=?", (period,))
                return
            posts = await self.app.prepare(self.app.owner_id, plan_id)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, AppError) else "Не удалось подготовить материалы."
            with self.app.db.connect(write=True) as db:
                db.execute("UPDATE automatic_runs SET status='failed' WHERE period=?", (period,))
            await self._notify(f"Автоподготовка: {reason}")
        else:
            with self.app.db.connect(write=True) as db:
                db.execute("UPDATE automatic_runs SET status='ready' WHERE period=?", (period,))
            await self._notify(
                f"Автоподготовка создала {len(posts)} черновика. Откройте план #{plan_id} в разделе «План и посты»."
            )

    async def sample_subscribers(self) -> None:
        row = self.app.business(self.app.owner_id)
        if not row["channel"]:
            return
        channel = Channel.model_validate_json(row["channel"])
        try:
            total = await self.app.publisher.subscriber_count(channel)
        except Exception as exc:
            log.warning("Subscriber sample unavailable (%s)", type(exc).__name__)
            return
        sample = SubscriberSnapshot(
            channel_id=channel.id, measured_at=self.app.now(), source="telegram:getChatMemberCount", total=total,
        )
        with self.app.db.connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO subscriber_samples VALUES (?, ?, ?, ?)",
                (sample.channel_id, stamp(sample.measured_at), sample.source, sample.total),
            )

    async def run(self) -> None:
        await self.recover()

        async def periodically(operation, interval):
            while True:
                try:
                    await operation()
                except Exception as exc:
                    log.error("Worker %s failed (%s)", operation.__name__, type(exc).__name__)
                await asyncio.sleep(interval)

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(periodically(self.publish_due, 4))
            tasks.create_task(periodically(self.prepare_automatic, 20))
            tasks.create_task(periodically(self.sample_subscribers, 3600))
