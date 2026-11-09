"""Composition root. Teammates provide factories; the backend owns lifecycle."""

import asyncio
import importlib
import inspect
import logging
import signal
from dataclasses import dataclass

from app.config import Settings
from app.core.demo import DemoAI, DemoImages, DemoNotifier, DemoPublisher, DemoReporter
from app.core.instance_lock import InstanceLock
from app.core.service import BackendService
from app.publishing import TelegramNotifier, TelegramPublisher
from app.scheduler import Scheduler
from app.shared.errors import Conflict, InvalidInput
from app.storage import FileStore, Repository

log = logging.getLogger(__name__)


def load_factory(target: str):
    module_name, separator, name = target.partition(":")
    if not separator or not module_name.startswith("app.") or not name.isidentifier():
        raise InvalidInput("Фабрика должна иметь вид app.module:build_provider.")
    try:
        factory = getattr(importlib.import_module(module_name), name)
    except (ImportError, AttributeError):
        raise InvalidInput("Фабрика не найдена. Подключите модуль соответствующей роли.") from None
    if not callable(factory):
        raise InvalidInput("Указанная фабрика не является функцией.")
    return factory


def _provider(settings: Settings, target: str | None, methods: tuple[str, ...], resources: list):
    if target is None:
        return None
    provider = load_factory(target)(settings)
    resources.append(provider)
    if inspect.isawaitable(provider):
        if inspect.iscoroutine(provider):
            provider.close()
        raise InvalidInput("build_provider(settings) должна быть синхронной фабрикой.")
    if not all(callable(getattr(provider, method, None)) for method in methods):
        raise InvalidInput("Провайдер не реализует обязательные методы общего интерфейса.")
    if settings.mode == "live" and getattr(provider, "is_demo", False):
        raise InvalidInput("Тестовые провайдеры запрещены в live-режиме.")
    return provider


@dataclass
class Runtime:
    service: BackendService
    resources: list
    bot: object | None = None

    async def close(self):
        seen = set()
        cancelled = False
        for resource in reversed(self.resources):
            if id(resource) in seen:
                continue
            seen.add(id(resource))
            close = getattr(resource, "close", None)
            if close:
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        async with asyncio.timeout(10):
                            await result
                except asyncio.CancelledError:
                    cancelled = True
                except Exception as exc:
                    log.warning("Resource cleanup failed: %s", type(exc).__name__)
        if cancelled:
            raise asyncio.CancelledError


async def build_runtime(settings: Settings) -> Runtime:
    repository = Repository(settings.data_dir / "backend.sqlite3")
    repository.initialize()
    if any(b.owner_id != settings.owner_id for b in repository.list_businesses()):
        raise Conflict(
            "OWNER_TELEGRAM_ID не совпадает с владельцем сохранённых данных. Используйте соответствующий DATA_DIR."
        )
    files = FileStore(settings.data_dir / "assets", max_bytes=settings.max_upload_bytes)
    resources = [repository]
    runtime = Runtime(service=None, resources=resources)  # type: ignore[arg-type]
    try:
        if settings.mode == "demo":
            ai, images = DemoAI(), DemoImages()
            publisher, notifier, reporter = DemoPublisher(), DemoNotifier(), DemoReporter()
            resources.extend([ai, images, publisher, notifier, reporter])
        else:
            ai = _provider(
                settings,
                settings.ai_factory,
                ("build_profile", "interpret_request", "make_plan", "write_post", "revise_text"),
                resources,
            )
            images = _provider(settings, settings.image_factory, ("generate",), resources)
            reporter = _provider(settings, settings.report_factory, ("build",), resources)
            publisher = TelegramPublisher(settings.telegram_bot_token.get_secret_value())
            resources.append(publisher)
            notifier = TelegramNotifier(settings.telegram_bot_token.get_secret_value())
            resources.append(notifier)
        runtime.service = BackendService(
            repository, files, settings, ai, images, publisher, notifier, reporter
        )
        if settings.bot_factory:
            if settings.mode != "live":
                raise InvalidInput("BOT_FACTORY включается только в live-режиме: demo не использует сеть.")
            bot = load_factory(settings.bot_factory)(runtime.service, settings)
            resources.append(bot)
            if inspect.isawaitable(bot):
                if inspect.iscoroutine(bot):
                    bot.close()
                raise InvalidInput("build_bot(service, settings) должна быть синхронной фабрикой.")
            if not all(callable(getattr(bot, method, None)) for method in ("run", "send")):
                raise InvalidInput(
                    "Адаптер Telegram должен реализовать run(stop_event) и send(notification)."
                )
            runtime.bot = bot
            runtime.service.notifier = bot
        return runtime
    except BaseException:
        await runtime.close()
        raise


async def run_application(settings: Settings, *, once: bool = False) -> None:
    if settings.mode == "live" and not settings.bot_factory and not once:
        raise InvalidInput(
            "Для полного запуска подключите BOT_FACTORY роли 3; --once запускает один цикл backend без интерфейса."
        )
    with InstanceLock(settings.data_dir / "backend.lock"):
        runtime = await build_runtime(settings)
        try:
            service = runtime.service
            recovered = service.recover_interrupted()
            log.info(
                "Backend ready: mode=%s, recovered_posts=%s, recovered_jobs=%s",
                settings.mode,
                recovered.get("posts", 0),
                recovered.get("jobs", 0),
            )
            if once:
                service.schedule_automations()
                await service.prepare_next_job()
                await service.publish_due_once()
                await service.collect_metrics()
                await service.dispatch_notifications()
                return
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            previous_signals = {}
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    previous_signals[sig] = signal.getsignal(sig)
                    signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
                except (ValueError, OSError):
                    pass
            tasks = []
            stopped = asyncio.create_task(stop.wait(), name="shutdown")
            try:
                tasks.append(asyncio.create_task(Scheduler(service).run(stop), name="scheduler"))
                if runtime.bot:
                    tasks.append(asyncio.create_task(runtime.bot.run(stop), name="telegram-ui"))
                done, _ = await asyncio.wait([*tasks, stopped], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task is not stopped:
                        task.result()
            finally:
                stop.set()
                for task in [*tasks, stopped]:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, stopped, return_exceptions=True)
                for sig, previous in previous_signals.items():
                    signal.signal(sig, previous)
        finally:
            await runtime.close()
