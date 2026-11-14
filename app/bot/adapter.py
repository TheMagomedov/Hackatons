import asyncio
import logging
from contextlib import suppress

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.token import TokenValidationError

from app.bot.router import build_router, install_mini_app_menu, menu
from app.config import Settings
from app.core.service import BackendService
from app.shared.errors import InvalidInput, ProviderUnavailable
from app.shared.models import Notification

log = logging.getLogger(__name__)


class UpdateLifecycle(BaseMiddleware):
    """Drain asynchronous user actions before their backend providers are closed."""

    def __init__(self):
        self.active: set[asyncio.Task] = set()
        self.stopping = False

    async def __call__(self, handler, event, data):
        if self.stopping:
            return None
        task = asyncio.current_task()
        self.active.add(task)
        try:
            return await handler(event, data)
        finally:
            self.active.discard(task)

    async def stop(self):
        self.stopping = True
        tasks = tuple(self.active - {asyncio.current_task()})
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class TelegramBotAdapter:
    def __init__(self, service: BackendService, token: str, mini_app_url: str | None = None):
        try:
            self.bot = Bot(token)
        except TokenValidationError as exc:
            raise InvalidInput("Проверьте формат TELEGRAM_BOT_TOKEN.") from exc
        self.dispatcher = Dispatcher()
        self.updates = UpdateLifecycle()
        self.dispatcher.update.outer_middleware(self.updates)
        self.dispatcher.include_router(build_router(service))
        self.service = service
        self.owner_id = service.settings.owner_id
        self.mini_app_url = mini_app_url

    async def refresh_mini_app_menus(self) -> None:
        """A rotated host must reach every active member's persistent Telegram menu."""
        if not self.mini_app_url:
            return
        members = self.service.repo.list_access(self.owner_id)["members"]
        actors = {self.owner_id, *(member["actor_id"] for member in members if member["revoked_at"] is None)}
        limit = asyncio.Semaphore(4)

        async def install(actor: int) -> None:
            async with limit:
                # Membership may have changed while another menu request was in flight.
                if self.service.access_role(actor) is not None:
                    await install_mini_app_menu(self.bot, actor, self.mini_app_url)

        await asyncio.gather(*(install(actor) for actor in sorted(actors)))

    async def run(self, stop_event: asyncio.Event) -> None:
        delay = 2
        while not stop_event.is_set():
            try:
                await self._run_connected(stop_event)
                return
            except (ProviderUnavailable, TelegramAPIError):
                log.warning("Telegram connection unavailable; reconnecting in %s seconds", delay)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, 60)

    async def _run_connected(self, stop_event: asyncio.Event) -> None:
        if stop_event.is_set():
            return
        self.updates.stopping = False
        try:
            me = await self.bot.get_me(request_timeout=15)
            webhook = await self.bot.get_webhook_info(request_timeout=15)
        except TelegramAPIError as exc:
            raise ProviderUnavailable("Telegram не подтвердил подключение бота.") from exc
        if webhook.url:
            raise InvalidInput(
                "У этого бота настроен webhook. Для polling нужен отдельный токен или отключение webhook."
            )
        if self.mini_app_url:
            await self.refresh_mini_app_menus()
        log.info("Telegram bot @%s connected", me.username)
        polling = asyncio.create_task(
            self.dispatcher.start_polling(
                self.bot,
                handle_signals=False,
                close_bot_session=False,
                allowed_updates=self.dispatcher.resolve_used_update_types(),
                tasks_concurrency_limit=16,
            )
        )
        stopping = asyncio.create_task(stop_event.wait())
        try:
            done, _ = await asyncio.wait((polling, stopping), return_when=asyncio.FIRST_COMPLETED)
            if polling in done:
                await polling
        finally:
            stopping.cancel()
            if not polling.done():
                with suppress(RuntimeError):
                    await self.dispatcher.stop_polling()
                polling.cancel()
            await asyncio.gather(polling, stopping, return_exceptions=True)
            await self.updates.stop()

    async def send(self, notification: Notification) -> None:
        from app.safety.privacy import redact_text

        rows = []
        if notification.job_id:
            rows.append([("Открыть задание", f"job:{notification.job_id}")])
        if notification.post_id:
            rows.append([("Открыть пост", f"post:{notification.post_id}")])
        await self.bot.send_message(
            notification.owner_id, redact_text(notification.text), reply_markup=menu(*rows), request_timeout=15
        )

    async def close(self) -> None:
        await self.updates.stop()
        await self.bot.session.close()


def build_bot(service: BackendService, settings: Settings) -> TelegramBotAdapter:
    if not settings.telegram_bot_token:
        raise InvalidInput("Укажите TELEGRAM_BOT_TOKEN.")
    return TelegramBotAdapter(service, settings.telegram_bot_token.get_secret_value(), settings.mini_app_url)
