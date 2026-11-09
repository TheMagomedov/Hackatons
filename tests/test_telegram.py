import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetChat, GetChatMember, SendMessage, SendPhoto
from aiogram.types import Chat, ChatFullInfo, ChatMemberOwner, Message, Update, User

from app.ai.fixture import FixtureAI
from app.bot.router import build_router
from app.core.service import Marketing
from app.media.generator import FixtureImages
from app.publishing.telegram import TelegramNotifier, TelegramPublisher
from app.scheduler.worker import Worker
from app.shared.models import Status
from app.storage.db import Database

OWNER_ID = 42
CHANNEL_ID = -100123456
OWNER = User(id=OWNER_ID, first_name="Owner", is_bot=False)


class RecordingSession(BaseSession):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.requests = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, GetChat):
            return ChatFullInfo(
                id=CHANNEL_ID, type="channel", title="Тестовый канал", username="test_channel",
                accent_color_id=0, max_reaction_count=11, accepted_gift_types={
                    "unlimited_gifts": False, "limited_gifts": False, "unique_gifts": False,
                    "premium_subscription": False, "gifts_from_channels": False,
                },
            )
        if isinstance(method, GetChatMember):
            return ChatMemberOwner(status="creator", user=OWNER, is_anonymous=False)
        if isinstance(method, (SendMessage, SendPhoto)):
            return Message(
                message_id=1000 + len(self.requests), date=self.clock(),
                chat=Chat(id=int(method.chat_id), type="private" if method.chat_id == OWNER_ID else "channel"),
                text=method.text if isinstance(method, SendMessage) else None,
                caption=method.caption if isinstance(method, SendPhoto) else None,
            )
        raise AssertionError(type(method).__name__)

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("Unexpected download")
        yield b""


def test_bot_menu_preview_approval_and_reaction_update(tmp_path):
    async def scenario():
        now = datetime(2026, 11, 9, 9, tzinfo=UTC)

        def clock():
            return now

        session = RecordingSession(clock)
        async with Bot("123456:simulated", session=session) as bot:
            publisher = TelegramPublisher(bot, OWNER_ID)
            app = Marketing(Database(tmp_path, OWNER_ID), FixtureAI(), FixtureImages(), publisher,
                            OWNER_ID, ZoneInfo("Europe/Moscow"), clock)
            worker = Worker(app, TelegramNotifier(bot))
            dispatcher = Dispatcher()
            dispatcher.include_router(build_router(app, worker, True))
            assert "message_reaction_count" in dispatcher.resolve_used_update_types()
            uid = 0

            async def send(text=None, action=None, owner=OWNER_ID):
                nonlocal uid
                uid += 1
                person = {"id": owner, "first_name": "Owner", "is_bot": False}
                message = {"message_id": uid, "chat": {"id": owner, "type": "private"}, "date": int(now.timestamp())}
                if action:
                    event = {"callback_query": {
                        "id": str(uid), "chat_instance": "test", "from": person, "data": action,
                        "message": message,
                    }}
                else:
                    event = {"message": {**message, "from": person, "text": text}}
                await dispatcher.feed_update(bot, Update.model_validate({"update_id": uid, **event}))

            await send("/start", owner=777)
            assert "владелец" in session.requests[-1].text
            await send("/start")
            assert session.requests[-1].reply_markup.inline_keyboard[0][0].text == "Мой бизнес"
            await send(action="seed")
            await send(action="build")
            await send(action=f"confirm:{app.business(OWNER_ID)['profile_revision']}")
            await app.connect_channel(OWNER_ID, "@test_channel")
            await send("Подготовь три поста про завтраки")
            plan = app.plans(OWNER_ID)[0]["id"]
            await send(action=f"prepare:{plan}")
            post = app.posts(OWNER_ID, plan)[0]
            await send(action=f"post:{post.id}")
            assert isinstance(session.requests[-2], SendPhoto)
            assert session.requests[-2].chat_id == OWNER_ID
            assert "Согласовать публикацию" in session.requests[-1].reply_markup.inline_keyboard[0][0].text
            await send(action=f"approve:{post.id}:{post.version}")
            await send(action=f"approve:{post.id}:{post.version}")
            assert "уже запланирована" in session.requests[-1].text
            assert not any(isinstance(r, SendPhoto) and r.chat_id == CHANNEL_ID for r in session.requests)
            now = post.scheduled_at + timedelta(seconds=1)
            await worker.publish_due()
            assert app.post(OWNER_ID, post.id).status == Status.PUBLISHED
            deliveries = [r for r in session.requests if isinstance(r, SendPhoto) and r.chat_id == CHANNEL_ID]
            assert len(deliveries) == 1
            assert deliveries[0].photo.data == app.db.read_asset(post.image_id)
            with app.db.connect() as db:
                external_id = db.execute("SELECT message_id FROM posts WHERE id=?", (post.id,)).fetchone()[0]
            await dispatcher.feed_update(bot, Update.model_validate({
                "update_id": uid + 1,
                "message_reaction_count": {
                    "chat": {"id": CHANNEL_ID, "type": "channel"}, "message_id": external_id,
                    "date": int(now.timestamp()), "reactions": [
                        {"type": {"type": "emoji", "emoji": "👍"}, "total_count": 2},
                    ],
                },
            }))
            assert app.report(OWNER_ID, 7).reactions == 2

    asyncio.run(scenario())
