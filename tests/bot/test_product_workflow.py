"""Telegram UI exercised through real Dispatcher and backend, without network calls."""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetFile, SendMessage, SendPhoto
from aiogram.types import Chat, File, Message, Update

from app.bot.router import build_router
from app.config import Settings
from app.core.demo import DemoAI
from app.core.runtime import build_runtime
from app.reports.provider import PlainTextReportProvider
from app.shared.models import Notification, PostStatus, TaskIntent, TextDraft

OWNER = 42
NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


class EvidenceAI(DemoAI):
    def __init__(self):
        self.calls = Counter()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block_edit = False

    async def build_profile(self, sources):
        self.calls["profile"] += 1
        return await super().build_profile(sources)

    async def interpret_request(self, text, context):
        self.calls["intent"] += 1
        return await super().interpret_request(text, context)

    async def write_post(self, profile, item):
        self.calls["text"] += 1
        return TextDraft(
            text="Демо: кофе и завтраки",
            image_brief="Чашка кофе",
            used_source_ids=profile.facts[0].source_ids,
            warnings=["Уточните время завтраков."],
        )

    async def revise_text(self, profile, current_text, feedback):
        self.calls["edit"] += 1
        if self.block_edit:
            self.started.set()
            await self.release.wait()
        return TextDraft(
            text="Демо: свежий кофе и завтраки",
            image_brief="Чашка кофе",
            used_source_ids=profile.facts[0].source_ids,
            warnings=["Проверьте часы работы."],
        )


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        from tests.ai.test_sources import pdf_bytes
        # The same source now passes real RAG extraction during a conversation.
        self.download = pdf_bytes(text="Coffee and breakfast menu. Opening hours need confirmation.")
        self.slow_callback_id = None
        self.callback_started = asyncio.Event()
        self.callback_release = asyncio.Event()

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, AnswerCallbackQuery):
            if method.callback_query_id == self.slow_callback_id:
                self.callback_started.set()
                await self.callback_release.wait()
            return True
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="test-pdf", file_path="docs/test.pdf")
        if isinstance(method, (SendMessage, SendPhoto)):
            return Message(
                message_id=len(self.requests),
                date=NOW,
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text if isinstance(method, SendMessage) else None,
            )
        raise AssertionError(type(method).__name__)

    async def stream_content(self, *args, **kwargs):
        yield self.download


class Harness:
    def __init__(self, runtime, bot, session):
        self.runtime, self.bot, self.session = runtime, bot, session
        self.service = runtime.service
        self.clock = [NOW]
        self.service.clock = lambda: self.clock[0]
        self.ai = EvidenceAI()
        self.service.ai = self.ai
        self.service.reporter = PlainTextReportProvider()
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(build_router(self.service))
        self.uid = 0

    def event(self, *, text=None, button=None, document=False, actor=OWNER):
        self.uid += 1
        user = {"id": actor, "first_name": "Owner", "is_bot": False}
        message = {
            "message_id": self.uid,
            "chat": {"id": actor, "type": "private"},
            "date": int(NOW.timestamp()),
        }
        if button:
            event = {
                "callback_query": {
                    "id": str(self.uid),
                    "chat_instance": "test",
                    "from": user,
                    "data": button,
                    "message": message,
                }
            }
        elif document:
            event = {
                "message": {
                    **message,
                    "from": user,
                    "document": {
                        "file_id": "test-pdf",
                        "file_unique_id": "test-pdf-unique",
                        "file_name": "Меню.pdf",
                        "file_size": len(self.session.download),
                    },
                }
            }
        else:
            event = {"message": {**message, "from": user, "text": text}}
        return Update.model_validate({"update_id": self.uid, **event})

    async def feed(self, event):
        await self.dispatcher.feed_update(self.bot, event)

    async def send(self, **kwargs):
        event = self.event(**kwargs)
        await self.feed(event)
        return event

    @property
    def messages(self):
        return [request for request in self.session.requests if isinstance(request, SendMessage)]

    @property
    def last_text(self):
        return self.messages[-1].text

    @property
    def buttons(self):
        markup = self.messages[-1].reply_markup
        return [button.callback_data for row in markup.inline_keyboard for button in row] if markup else []

    async def ready_business(self, *, pdf=False):
        await self.send(text="/start")
        await self.send(button="input:name")
        await self.send(text="Кофейня")
        business = self.service.list_businesses(OWNER)[-1]
        await self.send(button=f"input:notes:{business.id}")
        await self.send(text="Кофе и завтраки. Часы работы уточняются.")
        if pdf:
            await self.send(button=f"input:pdf:{business.id}")
            await self.send(document=True)
        await self.send(button=f"analyze:{business.id}")
        business = self.service.get_business(OWNER, business.id)
        await self.send(button=f"confirm:{business.id}:{business.profile_version}")
        await self.send(button=f"input:channel:{business.id}")
        await self.send(text="@test_channel")
        return self.service.get_business(OWNER, business.id)

    async def prepare(self, business):
        await self.send(text="Подготовь один пост")
        job = self.service.list_jobs(OWNER, business.id)[-1]
        assert await self.service.prepare_next_job()
        return self.service.get_job_posts(OWNER, job.id)[0], job


@pytest.fixture
async def harness(tmp_path):
    runtime = await build_runtime(Settings(owner_id=OWNER, data_dir=tmp_path))
    session = RecordingSession()
    async with Bot("123456:simulated", session=session) as bot:
        yield Harness(runtime, bot, session)
    await runtime.close()


async def test_business_menu_explains_setup_generation_and_material_buttons(harness):
    h = harness
    business = h.service.create_business(OWNER, "Стекольная компания")
    await h.send(button=f"business:{business.id}")
    assert h.last_text.startswith("Компания: Стекольная компания")
    assert "Следующий шаг: «Добавить описание компании»" in h.last_text
    labels = {
        button.callback_data: button.text
        for row in h.messages[-1].reply_markup.inline_keyboard for button in row
    }
    assert labels[f"input:notes:{business.id}"] == "Добавить описание компании"
    assert labels[f"input:request:{business.id}"] == "Создать посты с AI"
    assert labels[f"posts:{business.id}"] == "Открыть посты"
    assert labels[f"input:channel:{business.id}"] == "Подключить канал"
    await h.send(button=f"input:notes:{business.id}")
    assert "Компания: Стекольная компания" in h.last_text
    assert "материал для профиля, а не готовый пост" in h.last_text
    await h.send(text="Производим архитектурное стекло.")
    assert "Следующий шаг: «Собрать профиль с AI»" in h.last_text
    await h.send(button=f"analyze:{business.id}")
    assert "Следующий шаг: проверьте сведения" in h.last_text
    business = h.service.get_business(OWNER, business.id)
    await h.send(button=f"confirm:{business.id}:{business.profile_version}")
    assert "Следующий шаг: «Подключить канал»" in h.last_text


async def test_prompt_names_target_company_and_cancel_returns_to_it_without_saving(harness):
    h = harness
    business = await h.ready_business()
    other = h.service.create_business(OWNER, "Другая компания")
    await h.send(button=f"business:{other.id}")
    before = h.service.list_sources(OWNER, business.id)
    await h.send(button=f"input:notes:{business.id}")
    assert h.last_text.startswith(f"Компания: {business.name}")
    assert "/cancel — отменить ввод" in h.last_text
    assert f"business:{business.id}" in h.buttons
    await h.send(button=f"business:{business.id}")
    assert h.last_text.startswith(f"Компания: {business.name}")
    assert "Следующий шаг: «Создать посты с AI»" in h.last_text
    await h.send(text="Что ты умеешь?")
    assert h.ai.calls["intent"] == 1
    assert h.service.list_sources(OWNER, business.id) == before


async def test_complete_bot_workflow_sources_edit_pause_publish_and_report(harness):
    h = harness
    business = await h.ready_business(pdf=True)
    sources = h.service.list_sources(OWNER, business.id)
    assert {source.kind for source in sources} == {"text", "pdf"}
    post, job = await h.prepare(business)
    await h.send(button=f"job:{job.id}")
    assert f"post:{post.id}" in h.buttons
    await h.send(button=f"post:{post.id}")
    assert "Источники:" in h.last_text
    assert sources[0].name in h.last_text
    assert "Уточните время завтраков" in h.last_text
    await h.send(button=f"text:{post.id}:{post.revision}")
    edit_event = await h.send(text="Сделай текст дружелюбнее")
    revised = h.service.get_post(OWNER, post.id)
    assert revised.revision > post.revision
    assert "Проверьте часы работы" in h.last_text
    await h.feed(edit_event)
    assert h.ai.calls["edit"] == 1
    assert h.ai.calls["intent"] == 1
    await h.send(button=f"time:{post.id}:{revised.revision}")
    await h.send(text=(NOW + timedelta(minutes=5)).isoformat())
    timed = h.service.get_post(OWNER, post.id)
    await h.send(button=f"approve:{post.id}:{post.revision}")
    assert h.service.get_post(OWNER, post.id).approved_revision is None
    await h.send(button=f"approve:{post.id}:{timed.revision}")
    await h.send(button=f"pause:{business.id}")
    h.clock[0] += timedelta(minutes=6)
    assert not await h.service.publish_due_once()
    await h.send(button=f"resume:{business.id}")
    assert await h.service.publish_due_once()
    assert not await h.service.publish_due_once()
    assert len(h.service.publisher.sent) == 1
    assert h.service.get_post(OWNER, post.id).status == PostStatus.PUBLISHED
    published = h.service.get_post(OWNER, post.id)
    h.uid += 1
    reactions = Update.model_validate(
        {
            "update_id": h.uid,
            "message_reaction_count": {
                "chat": {"id": int(published.channel_id), "type": "channel", "title": "Offline channel"},
                "message_id": published.message_id,
                "date": int(h.clock[0].timestamp()),
                "reactions": [{"type": {"type": "emoji", "emoji": "👍"}, "total_count": 2}],
            },
        }
    )
    await h.feed(reactions)
    await h.feed(reactions)
    metrics = h.service.report_data(OWNER, business.id, NOW, h.clock[0]).metrics
    assert len(metrics) == 1
    assert metrics[0].value == 2
    assert metrics[0].event_id == f"reactions:telegram:{h.bot.id}:update:{h.uid}"
    await h.send(button=f"report:{business.id}")
    assert any("1" in message.text for message in h.messages[-3:])
    assert "ДЕМО" in "\n".join(message.text for message in h.messages[-3:]).upper()


async def test_queue_progress_cancel_usage_and_owner_access(harness):
    h = harness
    business = await h.ready_business()
    await h.send(text="Подготовь один пост")
    job = h.service.list_jobs(OWNER, business.id)[0]
    assert f"cancel-job:{job.id}" in h.buttons
    await h.send(button=f"cancel-job:{job.id}", actor=99)
    assert h.service.get_job(OWNER, job.id).status == "pending"
    await h.send(button=f"cancel-job:{job.id}")
    assert h.service.get_job(OWNER, job.id).status == "cancelled"
    assert not await h.service.prepare_next_job()
    await h.send(button=f"jobs:{business.id}")
    assert f"job:{job.id}" in h.buttons
    await h.send(button=f"usage:{business.id}")
    assert "Операций:" in h.last_text
    assert "не сумма списания" in h.last_text
    await h.send(button=f"operations:{business.id}")
    assert "Последние AI-операции" in h.last_text
    assert "Обновлено:" in h.last_text


async def test_running_job_shows_stage_and_cannot_be_cancelled(harness):
    h = harness
    business = await h.ready_business()
    await h.send(text="Подготовь один пост")
    job = h.service.list_jobs(OWNER, business.id)[0]
    started, release = asyncio.Event(), asyncio.Event()
    original = h.ai.make_plan

    async def slow_plan(profile, request):
        started.set()
        await release.wait()
        return await original(profile, request)

    h.ai.make_plan = slow_plan
    task = asyncio.create_task(h.service.prepare_next_job())
    await asyncio.wait_for(started.wait(), timeout=3)
    try:
        await h.send(button=f"job:{job.id}")
        assert "Составляю план" in h.last_text
        assert "Последнее изменение:" in h.last_text
        assert f"cancel-job:{job.id}" not in h.buttons
        await h.send(button=f"cancel-job:{job.id}")
        assert h.service.get_job(OWNER, job.id).status == "running"
    finally:
        release.set()
        await task


async def test_duplicate_edit_after_router_restart_never_turns_into_new_paid_request(harness):
    h = harness
    business = await h.ready_business()
    post, _ = await h.prepare(business)
    await h.send(button=f"text:{post.id}:{post.revision}")
    event = await h.send(text="Уточни текст")
    assert h.ai.calls["edit"] == 1
    before = h.service.get_post(OWNER, post.id)
    h.dispatcher = Dispatcher()
    h.dispatcher.include_router(build_router(h.service))
    await h.feed(event)
    assert h.ai.calls["edit"] == 1
    assert h.ai.calls["intent"] == 1
    assert h.service.get_post(OWNER, post.id) == before
    assert len(h.service.list_jobs(OWNER, business.id)) == 1


@pytest.mark.parametrize("kind", ["name", "notes", "pdf"])
async def test_duplicate_nonpaid_event_after_restart_never_changes_route_or_calls_ai(harness, kind):
    h = harness
    if kind == "name":
        await h.send(button="input:name")
        event = await h.send(text="Кофейня")
        business = h.service.list_businesses(OWNER)[0]
    else:
        business = await h.ready_business()
        await h.send(button=f"input:{kind}:{business.id}")
        event = await h.send(document=True) if kind == "pdf" else await h.send(text="Дополнительный факт")
    before_businesses = h.service.list_businesses(OWNER)
    before_sources = h.service.list_sources(OWNER, business.id)
    before_calls = h.ai.calls.copy()
    h.dispatcher = Dispatcher()
    h.dispatcher.include_router(build_router(h.service))
    await h.feed(event)
    assert h.service.list_businesses(OWNER) == before_businesses
    assert h.service.list_sources(OWNER, business.id) == before_sources
    assert h.service.list_jobs(OWNER, business.id) == []
    assert h.ai.calls == before_calls


async def test_edit_reserves_before_any_telegram_reply_can_delay_it(harness):
    h = harness
    business = await h.ready_business()
    post, _ = await h.prepare(business)
    post = h.service.edit_post(OWNER, post.id, post.revision, scheduled_at=NOW + timedelta(minutes=1))
    post = h.service.approve_post(OWNER, post.id, post.revision)
    await h.send(button=f"text:{post.id}:{post.revision}")
    h.ai.block_edit = True
    original_send = h.session.make_request

    async def verify_reservation(bot, method, timeout=None):
        # A user-facing intro must not allow the previous approved version to be sent.
        if isinstance(method, SendMessage):
            current = h.service.get_post(OWNER, post.id)
            assert current.status != PostStatus.APPROVED
        return await original_send(bot, method, timeout)

    h.session.make_request = verify_reservation
    task = asyncio.create_task(h.send(text="Уточни текст"))
    await asyncio.wait_for(h.ai.started.wait(), timeout=3)
    try:
        h.clock[0] += timedelta(minutes=2)
        assert h.service.get_post(OWNER, post.id).status == PostStatus.EDITING
        assert not await h.service.publish_due_once()
    finally:
        h.ai.release.set()
        await task


async def test_post_pagination_starts_with_newest_and_reaches_oldest(harness):
    h = harness
    business = await h.ready_business()
    first, _ = await h.prepare(business)
    posts = [first]
    for index in range(1, 25):
        posts.extend(
            h.service.repo.create_posts(
                business.id,
                [
                    {
                        "text": f"Пост {index}",
                        "image_asset_id": first.image_asset_id,
                        "channel_id": first.channel_id,
                        "scheduled_at": None,
                        "is_demo": True,
                    }
                ],
                NOW + timedelta(seconds=index),
            )
        )
    await h.send(button=f"posts:{business.id}")
    assert f"post:{posts[-1].id}" in h.buttons
    assert f"post:{posts[0].id}" not in h.buttons
    assert f"posts:{business.id}:1" in h.buttons
    await h.send(button=f"posts:{business.id}:2")
    assert f"post:{posts[0].id}" in h.buttons
    assert "3/3" in h.last_text


async def test_stale_profile_requires_explicit_review_and_new_approval(harness):
    h = harness
    business = await h.ready_business()
    post, _ = await h.prepare(business)
    business = h.service.save_profile(
        OWNER, business.id, business.profile_version, business.profile.model_copy(update={"tone": "Деловой"})
    )
    h.service.confirm_profile(OWNER, business.id, business.profile_version)
    await h.send(button=f"post:{post.id}")
    assert "Профиль бизнеса изменился" in h.last_text
    assert f"approve:{post.id}:{post.revision}" not in h.buttons
    await h.send(button=f"refresh:{post.id}:{post.revision}")
    refreshed = h.service.get_post(OWNER, post.id)
    assert refreshed.revision > post.revision
    assert refreshed.profile_version == business.profile_version
    assert refreshed.approved_revision is None


async def test_long_edit_does_not_erase_new_prompt_or_retarget_business(harness):
    h = harness
    business = await h.ready_business()
    post, _ = await h.prepare(business)
    other = h.service.create_business(OWNER, "Другой бизнес")
    await h.send(button=f"text:{post.id}:{post.revision}")
    h.ai.block_edit = True
    task = asyncio.create_task(h.send(text="Уточни текст"))
    await asyncio.wait_for(h.ai.started.wait(), timeout=3)
    try:
        await h.send(button=f"business:{other.id}")
        await h.send(button=f"input:notes:{other.id}")
        h.ai.release.set()
        await task
        await h.send(text="Данные именно второго бизнеса")
        assert h.service.list_sources(OWNER, other.id)[0].text == "Данные именно второго бизнеса"
        assert len(h.service.list_sources(OWNER, business.id)) == 1
        assert h.ai.calls["intent"] == 1
    finally:
        h.ai.release.set()
        await task


async def test_slow_old_callback_cannot_replace_new_prompt(harness):
    h = harness
    business = await h.ready_business()
    old = h.event(button=f"input:request:{business.id}")
    h.session.slow_callback_id = old.callback_query.id
    task = asyncio.create_task(h.feed(old))
    await asyncio.wait_for(h.session.callback_started.wait(), timeout=3)
    try:
        await h.send(button=f"input:notes:{business.id}")
        h.session.callback_release.set()
        await task
        await h.send(text="Добавленный факт")
        assert h.service.list_sources(OWNER, business.id)[-1].text == "Добавленный факт"
        assert h.ai.calls["intent"] == 0
    finally:
        h.session.callback_release.set()
        await task


async def test_failed_notice_retry_does_not_repeat_business_operation(harness):
    h = harness
    business = await h.ready_business()
    notification = Notification(
        id="f07b6132-eaff-40cc-9570-ebc605c5a275",
        business_id=business.id,
        owner_id=OWNER,
        kind="test",
        text="Статус задания",
        created_at=NOW,
    )
    h.service.repo.add_notification(notification)
    for _ in range(5):
        h.service.repo.record_notification_failure(notification.id, NOW)
    before_jobs = h.service.list_jobs(OWNER, business.id)
    await h.send(button=f"notices:{business.id}")
    assert f"notice:{notification.id}" in h.buttons
    await h.send(button=f"notice:{notification.id}")
    assert notification.id in h.last_text
    assert f"retry-notice:{notification.id}" in h.buttons
    await h.send(button=f"retry-notice:{notification.id}", actor=99)
    assert len(h.service.failed_notifications(OWNER, business.id)) == 1
    await h.send(button=f"retry-notice:{notification.id}")
    assert h.service.failed_notifications(OWNER, business.id) == []
    assert h.service.list_jobs(OWNER, business.id) == before_jobs
    assert h.service.list_posts(OWNER, business.id) == []
    assert h.ai.calls["text"] == 0


async def test_every_button_fits_telegram_callback_limit(harness):
    h = harness
    business = await h.ready_business()
    post, job = await h.prepare(business)
    for button in (f"business:{business.id}", f"post:{post.id}", f"jobs:{business.id}", f"job:{job.id}"):
        await h.send(button=button)
    for request in h.messages:
        if request.reply_markup:
            assert all(
                1 <= len(button.callback_data.encode()) <= 64
                for row in request.reply_markup.inline_keyboard
                for button in row
            )


async def test_help_is_free_and_prepare_button_uses_company_context(harness):
    h = harness
    business = await h.ready_business()
    await h.send(text="/help")
    assert "согласуйте" in h.last_text
    assert h.ai.calls["intent"] == 0
    await h.send(button=f"input:request:{business.id}")
    await h.send(text="Подготовь один пост")
    assert h.ai.calls["intent"] == 1
    assert len(h.service.list_jobs(OWNER, business.id)) == 1


async def test_unknown_delivery_rejects_old_forward_and_accepts_corrected_input(harness):
    h = harness
    business = await h.ready_business()
    post, _ = await h.prepare(business)
    post = h.service.edit_post(OWNER, post.id, post.revision, scheduled_at=NOW + timedelta(minutes=1))
    post = h.service.approve_post(OWNER, post.id, post.revision)
    h.clock[0] += timedelta(minutes=2)
    h.service.repo.claim_due_post(h.clock[0])
    post = h.service.repo.fail_publication(post.id, post.revision, "Unknown outcome", True, h.clock[0])
    await h.send(button=f"found:{post.id}:{post.revision}")

    async def forward(date):
        h.uid += 1
        event = Update.model_validate(
            {
                "update_id": h.uid,
                "message": {
                    "message_id": h.uid,
                    "date": int(h.clock[0].timestamp()),
                    "chat": {"id": OWNER, "type": "private"},
                    "from": {"id": OWNER, "first_name": "Owner", "is_bot": False},
                    "caption": post.text,
                    "photo": [{"file_id": "image", "file_unique_id": "image", "width": 10, "height": 10}],
                    "forward_origin": {
                        "type": "channel",
                        "date": int(date.timestamp()),
                        "chat": {"id": int(post.channel_id), "type": "channel", "title": "Test"},
                        "message_id": 999,
                    },
                },
            }
        )
        await h.feed(event)

    await forward(NOW - timedelta(days=1))
    assert h.service.get_post(OWNER, post.id).status == PostStatus.DELIVERY_UNKNOWN
    assert "Перешлите нужный пост" in h.last_text
    await forward(NOW + timedelta(minutes=1))
    resolved = h.service.get_post(OWNER, post.id)
    assert resolved.status == PostStatus.PUBLISHED
    assert resolved.message_id == 999
    assert h.ai.calls["intent"] == 1


async def test_telegram_followup_uses_history_and_answer_does_not_enqueue(harness):
    h = harness
    business = await h.ready_business()
    h.ai.interpret_request = AsyncMock(side_effect=[
        TaskIntent(action="clarify", questions=["О завтраках или о кофе?"]),
        TaskIntent(action="prepare", request="Подготовь один пост о завтраках, без цен."),
        TaskIntent(action="answer", answer="Вы попросили один пост о завтраках, без цен."),
    ])
    await h.send(text="Подготовь один пост")
    assert h.last_text == "О завтраках или о кофе?"
    await h.send(text="О завтраках, без цен")
    assert h.service.list_jobs(OWNER, business.id)[0].request == "Подготовь один пост о завтраках, без цен."
    event = await h.send(text="Что я поручил?")
    assert h.last_text == "Вы попросили один пост о завтраках, без цен."
    assert len(h.service.list_jobs(OWNER, business.id)) == 1
    context = h.ai.interpret_request.call_args.args[1]
    assert context["history"][0]["assistant_text"] == "О завтраках или о кофе?"
    assert len(h.service.conversation_history(OWNER, business.id)) == 3
    await h.feed(event)
    assert h.ai.interpret_request.await_count == 3
    assert len(h.service.conversation_history(OWNER, business.id)) == 3
    assert h.service.publisher.sent == []


async def test_prepare_button_resolves_followup_without_losing_company_or_creating_early_job(harness):
    h = harness
    business = await h.ready_business()
    h.ai.interpret_request = AsyncMock(side_effect=[
        TaskIntent(action="prepare", request="Подготовь один пост о завтраках, без цен."),
        TaskIntent(action="clarify", questions=["Два новых поста о завтраках, тоже без цен?"]),
        TaskIntent(action="prepare", request="Подготовь два новых поста о завтраках, без цен."),
        TaskIntent(action="answer", answer="Вы попросили ещё два поста о завтраках, без цен."),
    ])
    await h.send(button=f"input:request:{business.id}")
    await h.send(text="Один пост о завтраках, без цен")
    assert len(h.service.list_jobs(OWNER, business.id)) == 1

    # An old company-A button remains safe after the owner selected company B.
    other = h.service.create_business(OWNER, "Студия стекла")
    await h.send(button=f"business:{other.id}")
    await h.send(button=f"input:request:{business.id}")
    await h.send(text="Ещё два таких")
    assert h.last_text == "Два новых поста о завтраках, тоже без цен?"
    assert len(h.service.list_jobs(OWNER, business.id)) == 1
    assert h.service.list_jobs(OWNER, other.id) == []
    context = h.ai.interpret_request.call_args.args[1]
    assert context["business_id"] == business.id
    assert context["history"][0]["user_text"] == "Один пост о завтраках, без цен"

    # A plain reply to that clarification must use A's history, not B's selection.
    response = await h.send(text="Да, без цен")
    jobs = h.service.list_jobs(OWNER, business.id)
    assert len(jobs) == 2
    assert "Подготовь два новых поста о завтраках, без цен." in {job.request for job in jobs}
    context = h.ai.interpret_request.call_args.args[1]
    assert context["business_id"] == business.id
    assert context["history"][-1]["action"] == "clarify"
    await h.feed(response)
    assert h.ai.interpret_request.await_count == 3
    assert len(h.service.list_jobs(OWNER, business.id)) == 2

    await h.send(button=f"input:request:{business.id}")
    await h.send(text="Что я попросил?")
    assert h.last_text == "Вы попросили ещё два поста о завтраках, без цен."
    assert len(h.service.list_jobs(OWNER, business.id)) == 2
    assert h.service.list_jobs(OWNER, other.id) == []
    assert h.service.conversation_history(OWNER, other.id) == []
    assert h.service.publisher.sent == []
