import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.ai.fixture import DEMO_NOTES, FixtureAI
from app.core.service import Marketing
from app.demo import LocalDelivery
from app.media.generator import FixtureImages
from app.scheduler.worker import Worker
from app.shared.models import Channel, Status
from app.shared.ports import AppError, DeliveryUnknown
from app.storage.db import Database


OWNER = 42


class SimulatedTelegram(LocalDelivery):
    def __init__(self, clock):
        super().__init__(clock)
        self.failure = None
        self.attempts = 0
        self.notifications = []

    async def check_channel(self, channel):
        return Channel(id=int(channel), title=f"Channel {channel}")

    async def publish(self, post, image):
        self.attempts += 1
        if self.failure is not None:
            raise self.failure
        return await super().publish(post, image)

    async def notify(self, owner_id, message):
        self.notifications.append((owner_id, message))


class Scenario:
    def __init__(self, root):
        self.now = datetime(2026, 11, 9, 9, tzinfo=UTC)
        self.root = root
        self.telegram = SimulatedTelegram(self.clock)

    def clock(self):
        return self.now

    def reopen(self):
        app = Marketing(
            Database(self.root, OWNER), FixtureAI(), FixtureImages(), self.telegram, OWNER,
            ZoneInfo("Europe/Moscow"), self.clock,
        )
        return app, Worker(app, self.telegram)

    async def prepare(self, app):
        await app.add_source(OWNER, "notes", "Example", DEMO_NOTES)
        _, revision = await app.build_profile(OWNER)
        app.confirm_profile(OWNER, revision)
        await app.connect_channel(OWNER, -100123456)
        plan_id = await app.make_plan(OWNER, "Три поста про завтраки")
        return (await app.prepare(OWNER, plan_id))[0]


def test_version_approval_restart_and_double_click(tmp_path):
    async def scenario():
        setup = Scenario(tmp_path)
        app, worker = setup.reopen()
        post = await setup.prepare(app)
        assert post.status == Status.DRAFT
        with pytest.raises(AppError, match="владелец"):
            await app.approve(OWNER + 1, post.id, post.version)
        approvals = await asyncio.gather(*[app.approve(OWNER, post.id, post.version) for _ in range(2)])
        assert sorted(approvals) == [False, True]
        updated = await app.revise(OWNER, post.id, post.version, "image", "Измените фон")
        with pytest.raises(AppError, match="устарела"):
            await app.approve(OWNER, post.id, post.version)
        await app.connect_channel(OWNER, -100987654)
        latest = app.post(OWNER, post.id)
        assert latest.version == updated.version + 1
        assert latest.status == Status.DRAFT
        assert latest.channel.id == -100987654
        assert await app.approve(OWNER, latest.id, latest.version)
        app, worker = setup.reopen()
        await worker.recover()
        setup.now = latest.scheduled_at + timedelta(minutes=1)
        await asyncio.gather(worker.publish_due(), worker.publish_due())
        sent, image = setup.telegram.sent[0]
        assert len(setup.telegram.sent) == 1
        assert sent.version == latest.version and image == app.db.read_asset(latest.image_id)
        assert app.post(OWNER, post.id).status == Status.PUBLISHED
        assert app.report(OWNER, 7).published == 1
    asyncio.run(scenario())


def test_unknown_delivery_and_manual_recovery(tmp_path):
    async def scenario():
        setup = Scenario(tmp_path)
        app, worker = setup.reopen()
        post = await setup.prepare(app)
        assert await app.approve(OWNER, post.id, post.version)
        setup.now = post.scheduled_at + timedelta(seconds=10)
        setup.telegram.failure = DeliveryUnknown("Результат неизвестен.")
        await worker.publish_due()
        assert app.post(OWNER, post.id).status == Status.DELIVERY_UNKNOWN
        app, worker = setup.reopen()
        await worker.recover()
        await worker.publish_due()
        assert setup.telegram.attempts == 1
        with pytest.raises(AppError, match="точным"):
            worker.resolve_unknown(OWNER, post.id, post.version, post.channel.id, 99, setup.now, "other", True)
        worker.resolve_unknown(
            OWNER, post.id, post.version, post.channel.id, 99, setup.now, post.draft.text, True,
        )
        published = app.post(OWNER, post.id)
        assert published.status == Status.PUBLISHED and published.link.endswith("/99")
        assert app.report(OWNER, 7).errors == 1
    asyncio.run(scenario())


def test_overdue_unapproved_and_tampered_assets_are_not_sent(tmp_path):
    async def scenario():
        setup = Scenario(tmp_path)
        app, worker = setup.reopen()
        post = await setup.prepare(app)
        setup.now = post.scheduled_at + timedelta(seconds=1)
        await worker.publish_due()
        with pytest.raises(AppError, match="время прошло"):
            await app.approve(OWNER, post.id, post.version)
        replacement = await app.revise(
            OWNER, post.id, post.version, "time", (setup.now + timedelta(minutes=3)).isoformat(),
        )
        assert await app.approve(OWNER, replacement.id, replacement.version)
        app.db.asset_path(post.image_id).write_bytes(b"changed bytes")
        setup.now = replacement.scheduled_at
        await worker.publish_due()
        assert setup.telegram.attempts == 0
        assert app.post(OWNER, post.id).status == Status.FAILED
    asyncio.run(scenario())


def test_automatic_drafts_are_unique_and_can_be_disabled(tmp_path):
    async def scenario():
        setup = Scenario(tmp_path)
        app, worker = setup.reopen()
        await setup.prepare(app)
        app.configure_automation(OWNER, 1, "Новый план про завтраки")
        setup.now += timedelta(days=1)
        await asyncio.gather(worker.prepare_automatic(), worker.prepare_automatic())
        assert len(app.plans(OWNER)) == 2
        assert all(post.status == Status.DRAFT for post in app.posts(OWNER))
        app, worker = setup.reopen()
        await worker.recover()
        await worker.prepare_automatic()
        assert len(app.plans(OWNER)) == 2
        app.configure_automation(OWNER, None)
        setup.now += timedelta(days=2)
        await worker.prepare_automatic()
        assert len(app.plans(OWNER)) == 2
        assert setup.telegram.sent == []
    asyncio.run(scenario())
