#!/usr/bin/env python3
"""Windows demo host: one private runtime, one temporary HTTPS tunnel, graceful stop."""

import argparse
import asyncio
import http.client
import importlib.util
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
STATE = REPO / "var" / "windows-run"
HEALTH_INTERVAL = 15
HEALTH_FAILURE_LIMIT = 3

spec = importlib.util.spec_from_file_location("local_tunnel", REPO / "scripts/run-local-tunnel.py")
local_tunnel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local_tunnel)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def healthy_endpoint(url, *, local=False):
    """Read only a small public health document; never send credentials or follow redirects."""
    handlers = [NoRedirect()]
    if local:
        handlers.append(urllib.request.ProxyHandler({}))
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
            return False
        _ = parsed.port
        request = urllib.request.Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
        with urllib.request.build_opener(*handlers).open(request, timeout=6) as response:
            if response.status != 200 or response.headers.get_content_type() != "application/json":
                return False
            content = response.read(4097)
            if len(content) > 4096:
                return False
            data = json.loads(content)
            return isinstance(data, dict) and all(data.get(key) == value for key, value in {
                "status": "ok", "mode": "live", "runtime_mode": "live",
            }.items())
    except (OSError, ValueError, TypeError, RecursionError, http.client.HTTPException):
        return False


class TunnelWatchdog:
    def __init__(self):
        self.failures = 0

    def observe(self, *, local_ok, public_ok):
        # A tunnel reconnect cannot fix a backend which has not started yet.
        self.failures = self.failures + 1 if local_ok and not public_ok else 0
        return self.failures >= HEALTH_FAILURE_LIMIT


def save_state(url, status):
    (STATE / "state.json").write_text(json.dumps({
        "pid": os.getpid(), "url": url, "status": status, "checked_at": int(time.time()),
    }), encoding="utf-8")


def configuration():
    from dotenv import load_dotenv

    from app.config import Settings

    path = REPO / ".env"
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("Создайте локальный .env по .env.live.example.")
    load_dotenv(path, override=False, interpolate=False)
    os.environ["MINI_APP_URL"] = os.environ.get("WINDOWS_PUBLIC_URL", "").strip() or "https://pending.localhost.run/mini/"
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

    fixed_url = os.environ.get("WINDOWS_PUBLIC_URL", "").strip()
    ssh = shutil.which("ssh") if not fixed_url else None
    if not fixed_url and not ssh:
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
        next_probe = 0
        watchdog = TunnelWatchdog()
        try:
            if fixed_url:
                # The configured HTTPS proxy owns reconnection. Keep the runtime and
                # in-flight work alive when the public network is temporarily unavailable.
                url = settings.mini_app_url
                save_state(url, "starting")
                app = asyncio.create_task(run_application(settings), name="windows-demo-app")
                print("Mini App (постоянный адрес): " + url, flush=True)
                while not stop.is_set():
                    if stop_file.exists():
                        stop.set()
                        break
                    if app.done():
                        app.result()
                        raise RuntimeError("Бот завершился. Проверьте журнал; автоматического повторного запуска отправок нет.")
                    if loop.time() >= next_probe:
                        local_ok, public_ok = await asyncio.gather(
                            asyncio.to_thread(healthy_endpoint, f"http://127.0.0.1:{settings.mini_app_port}/mini/health", local=True),
                            asyncio.to_thread(healthy_endpoint, url + "health"),
                        )
                        next_probe = loop.time() + HEALTH_INTERVAL
                        save_state(url, "ready" if local_ok and public_ok else "degraded")
                    await asyncio.sleep(1)
                return
            while not stop.is_set():
                job = WindowsJob()
                # Gate before SSH startup so the owner exiting cannot leave a tunnel behind.
                runner = REPO / "app/ai/_windows_process.py"
                with (STATE / "tunnel.stderr.log").open("wb") as tunnel_log:
                    tunnel = await asyncio.create_subprocess_exec(
                        sys.executable, str(runner), *tunnel_command(ssh, settings.mini_app_port),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=tunnel_log, limit=131_072,
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
                    if url is not None and loop.time() >= next_probe:
                        local_ok, public_ok = await asyncio.gather(
                            asyncio.to_thread(healthy_endpoint, f"http://127.0.0.1:{settings.mini_app_port}/mini/health", local=True),
                            asyncio.to_thread(healthy_endpoint, url + "health"),
                        )
                        next_probe = loop.time() + HEALTH_INTERVAL
                        reconnect = watchdog.observe(local_ok=local_ok, public_ok=public_ok)
                        save_state(url, "ready" if local_ok and public_ok else "degraded")
                        if reconnect:
                            save_state(None, "reconnecting")
                            print("Внешний адрес трижды недоступен при работающей студии. Переподключаю только свой туннель и бот.", flush=True)
                            break
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
                        watchdog = TunnelWatchdog()
                        next_probe = loop.time() + 2
                        save_state(url, "starting")
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
