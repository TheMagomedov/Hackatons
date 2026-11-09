from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto
from aiogram.types import Chat, Message, Update
from PIL import Image

from app.ai.provider import ModelAI
from app.bot.router import build_router
from app.config import Settings
from app.core.runtime import build_runtime
from app.media.generator import normalize_image
from app.reports.summary import Reporter
from app.shared.errors import InvalidInput
from app.shared.models import MetricSnapshot, ReportInput, Source, SourceInput


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


async def test_bot_owner_onboarding_queue_preview_and_stale_approval(tmp_path):
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
            await send(button=f"input:channel:{business.id}")
            await send(text="@test_channel")
            await send(text="Подготовь один пост")
            job_button = session.requests[-1].reply_markup.inline_keyboard[0][0].callback_data
            await send(text="Подготовь один пост", repeat=True)
            assert session.requests[-1].reply_markup.inline_keyboard[0][0].callback_data == job_button
            assert await service.prepare_next_job()
            await send(button=job_button)
            post = service.list_posts(owner, business.id)[0]
            await send(button=f"post:{post.id}")
            assert isinstance(session.requests[-2], SendPhoto)
            await send(button=f"time:{post.id}:{post.revision}")
            await send(text=(service.now() + timedelta(hours=1)).isoformat())
            updated = service.get_post(owner, post.id)
            await send(button=f"approve:{post.id}:{post.revision}")
            assert service.get_post(owner, post.id).approved_revision is None
            await send(button=f"approve:{post.id}:{updated.revision}")
            assert service.get_post(owner, post.id).approved_revision == updated.revision
    finally:
        await runtime.close()


async def test_ai_uses_backend_source_ids_and_rejects_nonpublic_urls():
    calls = []

    class Model:
        async def request(self, path, payload):
            calls.append((path, payload))
            return {"choices": [{"message": {"content": (
                '{"name":"Кофейня","description":"Кофейня","facts":[{"text":"Кофе","source_ids":["s1"]}]}'
            )}}]}

    source = Source(
        id="s1", business_id="b1", kind="text", name="Описание", text="Кофе и тосты",
        created_at=datetime.now(UTC),
    )
    ai = ModelAI(Model(), "test", Settings())
    profile = await ai.build_profile([SourceInput(source=source)])
    assert profile.facts[0].source_ids == ["s1"]
    assert "Кофе и тосты" in calls[0][1]["messages"][1]["content"]
    unsafe = source.model_copy(update={"kind": "url", "text": None, "url": "http://127.0.0.1/"})
    with pytest.raises(InvalidInput):
        await ai.build_profile([SourceInput(source=unsafe)])


def test_report_keeps_latest_reactions_and_measured_zero():
    end = datetime(2026, 11, 9, tzinfo=UTC)
    start = end - timedelta(days=7)
    sample = MetricSnapshot(
        business_id="b1", name="reactions", post_id="p1", value=5, observed_at=start, source="telegram",
        event_id="a",
    )
    latest = sample.model_copy(update={"value": 0, "observed_at": end, "event_id": "b"})
    data = ReportInput(business_id="b1", start=start, end=end, posts=[], metrics=[latest, sample])
    assert "Реакции: 0 по 1 постам" in Reporter().build(data)
    assert "Реакции: нет данных" in Reporter().build(data.model_copy(update={"metrics": []}))


def test_image_converts_to_backend_asset_contract():
    buffer = BytesIO()
    Image.new("RGB", (16, 16)).save(buffer, "PNG")
    image = normalize_image(buffer.getvalue(), "test")
    assert image.mime_type == "image/jpeg"
    assert image.content.startswith(b"\xff\xd8")
