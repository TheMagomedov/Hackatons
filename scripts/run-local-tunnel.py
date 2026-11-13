#!/usr/bin/env python3
"""Own one temporary SSH tunnel and restart only our app when its URL rotates."""

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import socket
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def tunnel_url(line: str) -> str | None:
    """Read structured registration events, never URLs embedded in banners or QR text."""
    if len(line) > 65_536:
        return None
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict) or event.get("type") not in {
        "run.localhost.tunnel.registered", "run.localhost.tunnel.rotated",
    }:
        return None
    data = event.get("data")
    if not isinstance(data, dict) or type(data.get("port")) is not int or data["port"] != 80:
        return None
    host = data.get("address")
    if not isinstance(host, str) or event.get("subject") != host:
        return None
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:lhr\.life|localhost\.run)", host):
        return None
    return "https://" + host + "/mini/"


async def stop_child(child, timeout: float = 20) -> None:
    if child is None or child.returncode is not None:
        return
    try:
        child.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(child.wait(), timeout)
    except TimeoutError:
        try:
            child.kill()
        except ProcessLookupError:
            pass
        await child.wait()


class AppChild:
    def __init__(self, env: dict, spawn=asyncio.create_subprocess_exec, stopping=lambda: False):
        self.env, self.spawn = env, spawn
        self.stopping = stopping
        self.process = None
        self.url = None

    async def rotate(self, url: str):
        if self.url == url and self.process is not None and self.process.returncode is None:
            return
        await self.close()
        if self.stopping():
            return
        self.process = await self.spawn(
            sys.executable, "-m", "app", "run", "--env-file", str(REPO / ".env"),
            cwd=REPO, env={**self.env, "MINI_APP_URL": url}, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        self.url = url
        print("Mini App: " + url, flush=True)
        print("Запускаем приложение. После запуска откройте актуальную кнопку меню бота или отправьте /start.", flush=True)

    async def close(self):
        await stop_child(self.process)
        self.process = None
        self.url = None


def configuration(without_ai: bool):
    from dotenv import dotenv_values

    from app.config import Settings

    env_file = REPO / ".env"
    if not env_file.is_file() or env_file.is_symlink() or stat.S_IMODE(env_file.stat().st_mode) & 0o077:
        raise RuntimeError("Нужен обычный локальный .env с правами 600.")
    values = dotenv_values(env_file, interpolate=False)
    if values.get("APP_MODE") != "live":
        raise RuntimeError("Для этого запуска укажите APP_MODE=live в .env.")
    env = {**os.environ, **{key: value for key, value in values.items() if value is not None}}
    if without_ai:
        env.update({"AI_API_KEY": "", "TEXT_API_KEY": "", "IMAGE_API_KEY": "",
                    "AI_PROVIDER_FACTORY": "app.ai.provider:build_provider",
                    "IMAGE_PROVIDER_FACTORY": "app.media.runtime:build_provider"})
    env["MINI_APP_URL"] = "https://pending.localhost.run/mini/"
    # Settings.from_env gives environment values precedence; the real URL is injected per child.
    before = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env)
        settings = Settings.from_env(env_file)
    finally:
        os.environ.clear()
        os.environ.update(before)
    if settings.bot_factory != "app.bot.adapter:build_bot":
        raise RuntimeError("Проверьте BOT_FACTORY в .env.")
    return settings, env


async def supervise(settings, env):
    from app.core.instance_lock import InstanceLock

    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("Нужен установленный OpenSSH.")
    directory = REPO / "var" / "local-tunnel"
    directory.mkdir(parents=True, exist_ok=True)
    with InstanceLock(directory / "supervisor.lock"):
        # Refuse to take over another user's app/process. Root stops its old launch explicitly.
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", settings.mini_app_port))
            except OSError:
                raise RuntimeError("Порт Mini App занят. Сначала остановите прежний запуск приложения.") from None
        command = [ssh, "-T", "-o", "IdentitiesOnly=yes", "-o", "IdentityFile=/dev/null",
                   "-o", "UserKnownHostsFile=" + str(directory / "known_hosts"),
                   "-o", "StrictHostKeyChecking=accept-new", "-o", "ExitOnForwardFailure=yes",
                   "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                   "-o", "ConnectTimeout=15", "-R", f"80:127.0.0.1:{settings.mini_app_port}",
                   "nokey@localhost.run", "--", "--output", "json"]
        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stopping.set)
        app = AppChild(env, stopping=stopping.is_set)
        tunnel = None
        try:
            while not stopping.is_set():
                tunnel = await asyncio.create_subprocess_exec(
                    *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=131_072,
                )
                started = loop.time()
                try:
                    while not stopping.is_set():
                        if app.process is not None and app.process.returncode is not None:
                            raise RuntimeError("Приложение завершилось. Проверьте .env и отсутствие второго polling-процесса.")
                        try:
                            line = await asyncio.wait_for(tunnel.stdout.readline(), 2)
                        except TimeoutError:
                            if app.process is None and loop.time() - started > 45:
                                break
                            continue
                        if not line:
                            break
                        url = tunnel_url(line.decode("utf-8", errors="replace"))
                        if url:
                            await app.rotate(url)
                    await app.close()
                finally:
                    await stop_child(tunnel, timeout=5)
                    tunnel = None
                if not stopping.is_set():
                    print("Туннель отключён. Повторное соединение через 5 секунд.", flush=True)
                    try:
                        await asyncio.wait_for(stopping.wait(), 5)
                    except TimeoutError:
                        pass
        finally:
            await app.close()
            await stop_child(tunnel, timeout=5)


def main():
    parser = argparse.ArgumentParser(description="Локальная студия с временным HTTPS и обновлением адреса меню бота.")
    parser.add_argument("--check", action="store_true", help="Проверить локальную конфигурацию без сети и запуска.")
    parser.add_argument("--without-ai", action="store_true", help="Явно отключить модель только в дочернем процессе.")
    args = parser.parse_args()
    try:
        settings, env = configuration(args.without_ai)
        if args.check:
            print("Локальная конфигурация корректна. Telegram и туннель не запускались.")
            return 0
        if os.name != "posix":
            print("Этот launcher предназначен для macOS/Linux. Для Windows используйте Docker/WSL.", file=sys.stderr)
            return 1
        print("Временный HTTPS: адрес может смениться; процесс обновит меню через перезапуск своего приложения.", flush=True)
        asyncio.run(supervise(settings, env))
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("Не удалось запустить студию. Проверьте локальную конфигурацию и SSH; секреты в лог не выводятся.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
