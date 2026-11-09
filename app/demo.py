from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from app.ai.fixture import DEMO_NOTES, FixtureAI
from app.core.service import Marketing
from app.media.generator import FixtureImages
from app.scheduler.worker import Worker
from app.shared.models import Channel, PostView, PublicationResult, Status
from app.shared.ports import AppError
from app.storage.db import Database


class LocalDelivery:
    """In-memory publisher for the offline integration run."""

    def __init__(self, clock):
        self.clock = clock
        self.sent: list[tuple[PostView, bytes]] = []

    async def check_channel(self, channel: int | str) -> Channel:
        return Channel(id=-100123456, title="Локальный тестовый канал")

    async def publish(self, post: PostView, image: bytes) -> PublicationResult:
        self.sent.append((post, image))
        return PublicationResult(message_id=len(self.sent), link=None, published_at=self.clock())

    async def subscriber_count(self, channel: Channel) -> int:
        raise AppError("В локальной проверке нет реального числа подписчиков.")


class ConsoleNotifier:
    async def notify(self, owner_id: int, message: str) -> None:
        print(f"[локальное уведомление] {message.replace('опубликован', 'передан тестовому адаптеру')}")


async def run_demo(timezone: ZoneInfo) -> None:
    print("Локальная демонстрация · Telegram и AI API отключены.")
    now = datetime.now(UTC)

    def clock():
        return now

    publisher = LocalDelivery(clock)
    owner = 1
    with TemporaryDirectory(prefix="ai-marketer-") as folder:
        root = Path(folder)

        def app_from_disk():
            return Marketing(Database(root, owner), FixtureAI(), FixtureImages(), publisher, owner, timezone, clock)

        app = app_from_disk()
        await app.add_source(owner, "notes", "Демо-кофейня", DEMO_NOTES)
        profile, version = await app.build_profile(owner)
        app.confirm_profile(owner, version)
        await app.connect_channel(owner, "@local")
        print(f"1. Карточка подтверждена: {profile.name}. Источников: {len(app.sources(owner))}.")

        intent = await app.interpret_request(owner, "Подготовь три поста на неделю про завтраки", False)
        plan_id = await app.make_plan(owner, intent.brief, intent.count)
        posts = await app.prepare(owner, plan_id)
        print(f"2. План #{plan_id}: подготовлено {len(posts)} черновика с изображениями.")
        post = posts[0]
        await app.approve(owner, post.id, post.version)
        revised = await app.revise(owner, post.id, post.version, "text", "Сделай текст короче")
        assert revised.status == Status.DRAFT
        try:
            await app.approve(owner, post.id, post.version)
        except AppError:
            print(f"3. Версия {post.version} устарела. Новая версия {revised.version} требует согласования.")
        else:
            raise RuntimeError("Старая версия прошла проверку")
        await app.approve(owner, revised.id, revised.version)
        app.configure_automation(owner, 1, "Новые идеи про завтраки")

        app = app_from_disk()
        worker = Worker(app, ConsoleNotifier())
        await worker.recover()
        now = max(revised.scheduled_at, now + timedelta(days=1)) + timedelta(seconds=1)
        await worker.publish_due()
        await worker.publish_due()
        assert len(publisher.sent) == 1 and publisher.sent[0][0].version == revised.version
        assert app.post(owner, post.id).status == Status.PUBLISHED
        print("4. После повторного открытия SQLite отправлена одна согласованная версия.")
        await worker.prepare_automatic()
        await worker.prepare_automatic()
        assert len(app.plans(owner)) == 2
        assert len(publisher.sent) == 1
        report = app.report(owner, 7)
        assert report.published == 1 and report.reactions is None
        print(f"5. Отчёт SQLite: подготовлено {report.prepared}, отправок в тестовый адаптер {report.published}.")
        print("   Реакции: нет данных. Изменение числа подписчиков: нет данных.")
