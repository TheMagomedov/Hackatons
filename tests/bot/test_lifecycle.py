import asyncio
from types import SimpleNamespace

from app.bot.adapter import TelegramBotAdapter, UpdateLifecycle


async def test_shutdown_waits_for_user_handler_cleanup_before_closing_clients():
    lifecycle = UpdateLifecycle()
    started, cleaned, polling_stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sequence = []

    async def long_action(event, data):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            sequence.append("saved-interrupted-action")
            cleaned.set()

    class Dispatcher:
        def resolve_used_update_types(self):
            return ["message"]

        async def start_polling(self, bot, **kwargs):
            assert kwargs["tasks_concurrency_limit"] == 16
            # Like aiogram, update handlers are separate tasks, not polling children.
            self.handler = asyncio.create_task(lifecycle(long_action, object(), {}))
            await polling_stopped.wait()

        async def stop_polling(self):
            polling_stopped.set()

    class FakeBot:
        async def get_me(self, **kwargs):
            return SimpleNamespace(username="offline-test")

        async def get_webhook_info(self, **kwargs):
            return SimpleNamespace(url="")

        @property
        def session(self):
            return self

        async def close(self):
            assert cleaned.is_set()
            sequence.append("closed-client")

    adapter = object.__new__(TelegramBotAdapter)
    adapter.bot, adapter.dispatcher, adapter.updates = FakeBot(), Dispatcher(), lifecycle
    stop = asyncio.Event()
    running = asyncio.create_task(adapter.run(stop))
    await asyncio.wait_for(started.wait(), timeout=2)
    stop.set()
    await asyncio.wait_for(running, timeout=2)
    assert adapter.dispatcher.handler.done()
    assert not lifecycle.active
    await adapter.close()
    assert sequence == ["saved-interrupted-action", "closed-client"]
    # A scheduled update that reaches middleware after shutdown cannot start work.
    assert await lifecycle(long_action, object(), {}) is None
