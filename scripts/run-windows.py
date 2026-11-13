#!/usr/bin/env python3
"""Windows demo host: one private runtime, one temporary HTTPS tunnel, graceful stop."""

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
STATE = REPO / "var" / "windows-run"

spec = importlib.util.spec_from_file_location("local_tunnel", REPO / "scripts/run-local-tunnel.py")
local_tunnel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local_tunnel)


def configuration():
    from dotenv import load_dotenv

    from app.config import Settings

    path = REPO / ".env"
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("Создайте локальный .env по .env.live.example.")
    load_dotenv(path, override=False, interpolate=False)
    os.environ["MINI_APP_URL"] = "https://pending.localhost.run/mini/"
    settings = Settings.from_env(path)
    if settings.mode != "live" or settings.mini_app_host != "127.0.0.1":
        raise RuntimeError("Для этого запуска нужны APP_MODE=live и MINI_APP_HOST=127.0.0.1.")
    if settings.bot_factory != "app.bot.adapter:build_bot":
        raise RuntimeError("Укажите BOT_FACTORY=app.bot.adapter:build_bot.")
    return settings


def tunnel_command(ssh, port):
    # Do not inherit personal SSH config, keys, agents or proxy commands.
    return [ssh, "-T", "-F", "NUL", "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes", "-o", "IdentityFile=none",
            "-o", "IdentityAgent=none", "-o", "UserKnownHostsFile=" + str(STATE / "known_hosts"),
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=15", "-R", f"80:127.0.0.1:{port}",
            "nokey@localhost.run", "--", "--output", "json"]


async def cancel_app(task):
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def stop_tunnel(tunnel, job):
    """Close the whole tree before waiting: SSH may still own the stdout pipe."""
    try:
        if job is not None:
            job.close()
    finally:
        if tunnel is not None:
            if tunnel.returncode is None:
                try:
                    tunnel.kill()  # Also handles a blocked runner whose job assignment failed.
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(tunnel.wait(), timeout=5)
            except TimeoutError:
                raise RuntimeError("Не удалось завершить собственный SSH-туннель за 5 секунд.") from None


def tunnel_environment():
    # Windows OpenSSH exits silently without its normal system/user locations.
    # Keep an explicit OS-only list; never inherit backend keys or SSH agents.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "USERPROFILE", "HOME", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP",
               "SYSTEMDRIVE", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "USERNAME", "USERDOMAIN", "PROGRAMDATA",
               "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROCESSOR_ARCHITECTURE",
               "NUMBER_OF_PROCESSORS", "OS", "PATHEXT"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


class PollingConflict(logging.Filter):
    def __init__(self, stop):
        super().__init__()
        self.stop = stop

    def filter(self, record):
        if "TelegramConflictError" in record.getMessage():
            print("Обнаружен другой polling-процесс. Этот запуск остановлен; сначала остановите старую копию бота.", flush=True)
            self.stop.set()
            return False
        return True


async def supervise(settings):
    from app.ai._windows_process import WindowsJob
    from app.core.instance_lock import InstanceLock
    from app.core.runtime import run_application

    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("Установите Windows OpenSSH Client.")
    STATE.mkdir(parents=True, exist_ok=True)
    with InstanceLock(STATE / "supervisor.lock"):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", settings.mini_app_port))
            except OSError:
                raise RuntimeError("Порт Mini App занят: другой процесс не будет остановлен.") from None
        stop_file = STATE / "stop"
        stop_file.unlink(missing_ok=True)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        previous = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        conflict = PollingConflict(stop)
        logger = logging.getLogger("aiogram.dispatcher")
        logger.addFilter(conflict)
        app = tunnel = job = None
        url = None
        try:
            while not stop.is_set():
                job = WindowsJob()
                # Gate before SSH startup so the owner exiting cannot leave a tunnel behind.
                runner = REPO / "app/ai/_windows_process.py"
                tunnel = await asyncio.create_subprocess_exec(
                    sys.executable, str(runner), *tunnel_command(ssh, settings.mini_app_port),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=131_072,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    env=tunnel_environment(),
                )
                job.assign(tunnel.pid)
                tunnel.stdin.write(b"\0")
                await tunnel.stdin.drain()
                tunnel.stdin.close()
                started = loop.time()
                while not stop.is_set():
                    if stop_file.exists():
                        stop.set()
                        break
                    if app is not None and app.done():
                        app.result()
                        raise RuntimeError("Бот завершился. Проверьте журнал; автоматического повторного запуска отправок нет.")
                    try:
                        line = await asyncio.wait_for(tunnel.stdout.readline(), 1)
                    except TimeoutError:
                        if url is None and loop.time() - started > 45:
                            break
                        continue
                    if not line:
                        break
                    next_url = local_tunnel.tunnel_url(line.decode("utf-8", errors="replace"))
                    if next_url and next_url != url:
                        await cancel_app(app)
                        url = next_url
                        current = settings.model_copy(update={"mini_app_url": url})
                        app = asyncio.create_task(run_application(current), name="windows-demo-app")
                        (STATE / "state.json").write_text(json.dumps({
                            "pid": os.getpid(), "url": url, "status": "starting",
                        }), encoding="utf-8")
                        print("Mini App: " + url, flush=True)
                await cancel_app(app)
                app = None
                await stop_tunnel(tunnel, job)
                tunnel = None
                job = None
                url = None
                if not stop.is_set():
                    print("Туннель отключился; повторное подключение через 5 секунд.", flush=True)
                    for _ in range(5):
                        if stop.is_set() or stop_file.exists():
                            stop.set()
                            break
                        await asyncio.sleep(1)
        finally:
            await cancel_app(app)
            await stop_tunnel(tunnel, job)
            logger.removeFilter(conflict)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            (STATE / "state.json").write_text(json.dumps({"status": "stopped"}), encoding="utf-8")
            stop_file.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Windows: бот и Mini App с временным HTTPS")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    if args.stop:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "stop").touch()
        print("Запрошена корректная остановка текущего запуска.")
        return 0
    if os.name != "nt":
        print("Используйте scripts/run-local-tunnel.py на macOS/Linux.")
        return 1
    os.chdir(REPO)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = configuration()
        if args.check:
            print("Конфигурация Windows корректна. Сетевых запросов не было.")
            return 0
        asyncio.run(supervise(settings))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"Запуск остановлен ({type(error).__name__}). Проверьте локальную конфигурацию и подключение; секреты скрыты.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
