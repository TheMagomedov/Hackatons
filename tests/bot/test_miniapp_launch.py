"""A first /start must expose the studio even if Telegram rejected the startup menu."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetMe, GetWebhookInfo, SendMessage, SetChatMenuButton
from aiogram.types import User, WebhookInfo

from app.ai.provider import UnconfiguredAI
from app.bot.router import STUDIO_BUTTON, STUDIO_TITLE, build_router, install_mini_app_menu
from app.config import Settings
from app.core.runtime import build_runtime
from tests.bot.test_product_workflow import OWNER, Harness, RecordingSession

URL = "https://studio.example.com/mini/"


class FirstContactSession(RecordingSession):
    def __init__(self, failures=1):
        super().__init__()
        self.failures = failures

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetMe):
            return User(id=12345, first_name="Studio", username="studio_bot", is_bot=True)
        if isinstance(method, GetWebhookInfo):
            return WebhookInfo(url="", has_custom_certificate=False, pending_update_count=0)
        if isinstance(method, SetChatMenuButton):
            self.requests.append(method)
            if self.failures:
                self.failures -= 1
                raise TelegramBadRequest(method=method, message="chat not found")
            return True
        return await super().make_request(bot, method, timeout)


@pytest.fixture
async def studio(tmp_path):
    settings = Settings(
        mode="live", data_dir=tmp_path, owner_id=OWNER, telegram_bot_token="12345:test-only-key",
        ai_factory="app.ai.provider:build_provider", image_factory="app.media.runtime:build_provider",
        bot_factory="app.bot.adapter:build_bot", mini_app_url=URL,
    )
    runtime = await build_runtime(settings)
    session = FirstContactSession()
    bot = Bot("12345:test-only-key", session=session)
    harness = Harness(runtime, bot, session)
    # Harness supplies deterministic events; retain the actual unconfigured providers.
    runtime.service.ai = UnconfiguredAI()
    harness.dispatcher = Dispatcher()
    harness.dispatcher.include_router(build_router(runtime.service))
    try:
        yield harness
    finally:
        await bot.session.close()
        await runtime.close()


def launch_button(message):
    return [button for row in message.reply_markup.inline_keyboard for button in row if button.web_app]


async def test_first_owner_start_launches_studio_and_retries_failed_startup_menu(studio):
    # This is the same installer used by TelegramBotAdapter.run before polling.
    assert await install_mini_app_menu(studio.bot, OWNER, URL) is False
    await studio.dispatcher.feed_update(studio.bot, studio.event(text="/start"))
    assert [type(request) for request in studio.session.requests] == [SetChatMenuButton, SendMessage, SetChatMenuButton]
    greeting = studio.session.requests[1]
    assert greeting.text.startswith(STUDIO_TITLE)
    assert "без нейросети" in greeting.text
    button, = launch_button(greeting)
    assert button.text == STUDIO_BUTTON and button.web_app.url == URL
    assert button.callback_data is None
    installed = studio.session.requests[-1]
    assert installed.chat_id == OWNER
    assert installed.menu_button.text == STUDIO_BUTTON and installed.menu_button.web_app.url == URL
    assert studio.service.ai_usage(OWNER)["used_today"] == 0


async def test_inline_launch_remains_when_retry_also_fails(studio):
    studio.session.failures = 2
    assert await install_mini_app_menu(studio.bot, OWNER, URL) is False
    await studio.dispatcher.feed_update(studio.bot, studio.event(text="/start@studio_bot"))
    greeting = next(request for request in studio.session.requests if isinstance(request, SendMessage))
    assert launch_button(greeting)[0].web_app.url == URL
    assert studio.service.ai_usage(OWNER)["used_today"] == 0


async def test_home_exposes_current_launch_and_repairs_personal_menu(studio):
    studio.session.failures = 0
    await studio.dispatcher.feed_update(studio.bot, studio.event(button="home"))
    greeting = next(request for request in studio.session.requests if isinstance(request, SendMessage))
    assert launch_button(greeting)[0].web_app.url == URL
    updated = next(request for request in studio.session.requests if isinstance(request, SetChatMenuButton))
    assert updated.chat_id == OWNER and updated.menu_button.web_app.url == URL


@pytest.mark.parametrize("command", ["/menu", "/cancel"])
async def test_menu_commands_update_invited_users_after_url_rotation(studio, command):
    actor = OWNER + 1
    service = studio.service
    invite = service.repo.create_access_invite(
        OWNER, "reviewer", 1, service.now() + timedelta(days=1), service.now(),
    )
    service.claim_invite(actor, invite["token"])
    rotated = "https://rotated.example.com/mini/"
    service.settings = service.settings.model_copy(update={"mini_app_url": rotated})
    studio.session.failures = 0
    await studio.dispatcher.feed_update(studio.bot, studio.event(text=command, actor=actor))
    greeting = next(r for r in studio.session.requests if isinstance(r, SendMessage))
    assert launch_button(greeting)[0].web_app.url == rotated
    updated = next(r for r in studio.session.requests if isinstance(r, SetChatMenuButton))
    assert updated.chat_id == actor and updated.menu_button.web_app.url == rotated
    assert studio.service.ai_usage(actor)["used_today"] == 0


async def test_startup_refreshes_all_active_members_before_polling_even_after_one_rejection(studio, monkeypatch):
    service = studio.service
    for actor, role in [(1001, "operator"), (1002, "reviewer"), (1003, "operator")]:
        invite = service.repo.create_access_invite(
            OWNER, role, 1, service.now() + timedelta(days=1), service.now(),
        )
        service.claim_invite(actor, invite["token"])
    service.repo.revoke_access_member(OWNER, 1003, service.now())
    adapter = studio.runtime.bot
    await adapter.bot.session.close()
    adapter.bot = studio.bot
    adapter.mini_app_url = "https://new-host.example.com/mini/"
    stop = asyncio.Event()

    async def poll(*args, **kwargs):
        requests = [r for r in studio.session.requests if isinstance(r, SetChatMenuButton)]
        assert {r.chat_id for r in requests} == {OWNER, 1001, 1002}
        assert all(r.menu_button.web_app.url == adapter.mini_app_url for r in requests)
        assert not any(isinstance(r, SendMessage) for r in studio.session.requests)
        stop.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(adapter.dispatcher, "start_polling", poll)
    monkeypatch.setattr(adapter.dispatcher, "stop_polling", AsyncMock())
    await asyncio.wait_for(adapter.run(stop), timeout=2)


async def test_unconfigured_url_omits_launch_and_menu_api(studio):
    studio.service.settings = studio.service.settings.model_copy(update={"mini_app_url": None})
    await studio.dispatcher.feed_update(studio.bot, studio.event(text="/start"))
    greeting = next(request for request in studio.session.requests if isinstance(request, SendMessage))
    assert launch_button(greeting) == []
    assert not any(isinstance(request, SetChatMenuButton) for request in studio.session.requests)


async def test_non_owner_cannot_get_studio_launcher_or_change_menu(studio):
    await studio.dispatcher.feed_update(studio.bot, studio.event(text="/start", actor=OWNER + 1))
    assert not any(isinstance(request, SetChatMenuButton) for request in studio.session.requests)
    assert all(not request.reply_markup for request in studio.session.requests if isinstance(request, SendMessage))
