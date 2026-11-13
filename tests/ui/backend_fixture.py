"""Isolated browser-test backend: fake providers, temporary DB, no publisher loop.

Started/stopped by backend.cjs. Never reads .env or connects to Telegram/AI.
"""
import asyncio
import signal
import socket
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp import web

from app.config import Settings
from app.core.runtime import build_runtime
from app.miniapp.server import create_app


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
    with TemporaryDirectory(prefix="tirazh-ui-") as destination:
        # Explicit default demo mode and demo factories, no env-file loading.
        runtime = await build_runtime(Settings(mode="demo", owner_id=42, data_dir=Path(destination).resolve()))
        runner = web.AppRunner(create_app(runtime.service, token="12345:test-only-key"))
        await runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        await web.SockSite(runner, sock).start()
        print(f"UI_BACKEND_READY http://127.0.0.1:{port}/mini/", flush=True)
        try:
            while not stop.is_set():
                # Generation only. Publishing/scheduler/notification loops are absent.
                await runtime.service.prepare_next_job()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.1)
                except TimeoutError:
                    pass
        finally:
            await runner.cleanup()
            await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
