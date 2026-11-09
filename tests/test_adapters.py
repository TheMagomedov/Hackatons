import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto
from aiogram.types import Chat, Message, Update

from app.ai.provider import build_provider as build_ai_provider
from app.bot.router import build_router
from app.config import Settings
from app.core.runtime import build_runtime
from app.media.runtime import build_provider
from app.shared.errors import ProviderUnavailable
from app.shared.models import BusinessProfile, TextDraft


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendMessage, SendPhoto)):
            return Message(
                message_id=len(self.requests), date=datetime.now(UTC), chat=Chat(id=method.chat_id, type="private"),
                text=method.text if isinstance(method, SendMessage) else None,
            )
        raise AssertionError(type(method).__name__)

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("Unexpected download")
        yield b""


async def test_bot_owner_onboarding_queue_preview_and_stale_approval(tmp_path, monkeypatch):
    owner = 42
    runtime = await build_runtime(Settings(owner_id=owner, data_dir=tmp_path))
    service = runtime.service
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(service))
    assert "message_reaction_count" in dispatcher.resolve_used_update_types()
    session = RecordingSession()
    try:
        async with Bot("123456:simulated", session=session) as bot:
            uid = 0

            async def send(*, text=None, button=None, actor=owner, repeat=False):
                nonlocal uid
                if not repeat:
                    uid += 1
                user = {"id": actor, "first_name": "Owner", "is_bot": False}
                msg = {"message_id": uid, "chat": {"id": actor, "type": "private"}, "date": 1794200000}
                event = {"message": {**msg, "from": user, "text": text}}
                if button:
                    event = {"callback_query": {
                        "id": str(uid), "chat_instance": "test", "from": user, "data": button, "message": msg,
                    }}
                await dispatcher.feed_update(bot, Update.model_validate({"update_id": uid, **event}))

            await send(text="/start", actor=77)
            assert "владелец" in session.requests[-1].text
            await send(text="/start")
            assert "AI-маркетолог" in session.requests[-1].text
            await send(button="input:name")
            await send(text="Тестовая кофейня")
            business = service.list_businesses(owner)[0]
            await send(button=f"input:notes:{business.id}")
            await send(text="Кофе и завтраки")
            await send(button=f"analyze:{business.id}")
            business = service.get_business(owner, business.id)
            await send(button=f"confirm:{business.id}:{business.profile_version}")
            entered = asyncio.Event()
            release = asyncio.Event()
            original_connect = service.connect_channel
            connect_calls = []

            async def delayed_connect(actor, business_id, channel):
                connect_calls.append(channel)
                entered.set()
                await release.wait()
                return await original_connect(actor, business_id, channel)

            monkeypatch.setattr(service, "connect_channel", delayed_connect)
            await send(button=f"input:channel:{business.id}")
            connecting = asyncio.create_task(send(text="@test_channel"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await send(text="@test_channel")
                assert "ещё обрабатывается" in session.requests[-1].text
            finally:
                release.set()
                await connecting
            assert connect_calls == ["@test_channel"]

            await send(button=f"input:request:{business.id}")
            await send(text="Подготовь один пост")
            job_button = session.requests[-1].reply_markup.inline_keyboard[0][0].callback_data
            await send(text="Подготовь один пост", repeat=True)
            assert session.requests[-1].reply_markup.inline_keyboard[0][0].callback_data == job_button
            assert await service.prepare_next_job()
            await send(button=job_button)
            post = service.list_posts(owner, business.id)[0]
            await send(button=f"post:{post.id}")
            assert isinstance(session.requests[-2], SendPhoto)
            assert not any(
                button.callback_data.startswith("approve:")
                for row in session.requests[-1].reply_markup.inline_keyboard for button in row
            )
            before = service.now()
            await send(button=f"in15:{post.id}:{post.revision}")
            updated = service.get_post(owner, post.id)
            assert before + timedelta(minutes=15) <= updated.scheduled_at <= service.now() + timedelta(minutes=15)
            await send(button=f"approve:{post.id}:{post.revision}")
            assert service.get_post(owner, post.id).approved_revision is None
            await send(button=f"approve:{post.id}:{updated.revision}")
            assert service.get_post(owner, post.id).approved_revision == updated.revision

            values = [
                {"text": f"Черновик {n}", "image_asset_id": post.image_asset_id, "channel_id": post.channel_id,
                 "scheduled_at": None, "is_demo": True}
                for n in range(21)
            ]
            posts = []
            for offset in range(0, len(values), 5):
                posts.extend(service.repo.create_posts(business.id, values[offset:offset + 5], service.now()))
            await send(button=f"posts:{business.id}")
            keyboard = session.requests[-1].reply_markup.inline_keyboard
            assert keyboard[0][0].callback_data == f"post:{posts[-1].id}"
            assert keyboard[-2][0].callback_data == f"posts:{business.id}:1"
            await send(button=f"posts:{business.id}:2")
            assert any(row[0].callback_data == f"post:{post.id}"
                       for row in session.requests[-1].reply_markup.inline_keyboard)
    finally:
        await runtime.close()


async def test_live_adapters_defer_missing_generation_credentials(monkeypatch):
    for key in ("TEXT_API_KEY", "AI_API_KEY", "IMAGE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(
        mode="live", owner_id=42, telegram_bot_token="123456:simulated",
        ai_factory="app.ai.provider:build_provider", image_factory="app.media.runtime:build_provider",
    )
    ai = build_ai_provider(settings)
    try:
        with pytest.raises(ProviderUnavailable, match="TEXT_API_KEY"):
            await ai.interpret_request("Подготовь пост", {})
    finally:
        await ai.close()
    images = build_provider(settings)
    with pytest.raises(ProviderUnavailable, match="IMAGE_API_KEY"):
        await images.generate(BusinessProfile(name="Кофейня", description="Тест"),
                              TextDraft(text="Кофе", image_brief="Чашка"))
