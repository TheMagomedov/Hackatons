"""Opt-in text provider using the official Codex CLI's ChatGPT sign-in.

This is a private, single-operator integration, not the ChatGPT web API. Images
still use the separately configured image provider. No fallback to an API key.
"""

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import ValidationError

from app.ai.client import SYSTEM
from app.ai.provider import ModelAI
from app.config import Settings
from app.shared.errors import InvalidInput, ProviderUnavailable

_ENV_KEYS = {
    "PATH", "HOME", "USER", "USERPROFILE", "SYSTEMROOT", "LOCALAPPDATA", "APPDATA",
    "TMPDIR", "TEMP", "TMP", "CODEX_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
}


def strict_schema(value):
    """Structured-output schemas require every property, including defaults."""
    if isinstance(value, list):
        return [strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: strict_schema(item) for key, item in value.items() if key != "default"}
    if result.get("type") == "object" and "properties" in result:
        result["additionalProperties"] = False
        result["required"] = list(result["properties"])
    return result


class ChatGPTClient:
    def __init__(self, executable: str, timeout: float):
        self.executable = executable
        self.timeout = timeout
        self.env = {key: value for key, value in os.environ.items() if key in _ENV_KEYS}
        self._processes: set[asyncio.subprocess.Process] = set()
        self._jobs = {}
        self._closed = False

    async def close(self):
        self._closed = True
        for process in tuple(self._processes):
            await self._stop(process)

    async def _stop(self, process):
        """Stop the owned process tree, including children after their launcher exits."""
        if os.name == "nt":
            job = self._jobs.pop(process, None)
            if job is not None:
                job.close()
            if process.returncode is None:
                try:
                    process.kill()  # Also covers a runner whose job assignment failed.
                except ProcessLookupError:
                    pass
        elif process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            raise ProviderUnavailable("Не удалось завершить локальный процесс ChatGPT CLI.") from None

    async def _run(self, arguments, *, cwd=None, prompt=None, capture=False):
        if self._closed:
            raise ProviderUnavailable("Локальный процесс ChatGPT CLI остановлен.")
        command = [self.executable, *arguments]
        options = {"start_new_session": True}
        input_bytes = prompt.encode("utf-8") if prompt is not None else None
        if input_bytes is not None and len(input_bytes) > 700_000:
            raise InvalidInput("Слишком много данных для одного запроса.")
        job = None
        if os.name == "nt":
            from app.ai._windows_process import WindowsJob

            job = WindowsJob()
            command = [sys.executable, str(Path(__file__).with_name("_windows_process.py")), *command]
            options = {"creationflags": subprocess.CREATE_NO_WINDOW}
            input_bytes = b"\0" + (input_bytes or b"")
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=cwd, env=self.env, **options,
                stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.STDOUT if capture else asyncio.subprocess.DEVNULL,
            )
        except BaseException:
            if job is not None:
                job.close()
            raise
        self._processes.add(process)
        try:
            if job is not None:
                self._jobs[process] = job
                job.assign(process.pid)
            output, _ = await process.communicate(input_bytes)
            if self._closed or process.returncode:
                raise ProviderUnavailable("ChatGPT CLI не выполнил запрос. Проверьте вход, лимит и версию Codex.")
            return (output or b"")[:4096].decode("utf-8", errors="replace")
        finally:
            try:
                await self._stop(process)
            finally:
                self._processes.discard(process)

    async def complete(self, schema, instruction, data, validate=None):
        payload = json.dumps({"input": data}, ensure_ascii=False)
        if len(payload) > 160_000:
            raise InvalidInput("Слишком много данных для одного запроса.")
        prompt = (
            SYSTEM + "\nВерни только JSON по переданной схеме. Не вызывай инструменты, "
            "не читай файлы, не выполняй команды.\nЗадание: " + instruction + "\n" + payload
        )
        try:
            async with asyncio.timeout(self.timeout):
                auth = await self._run(["login", "status"], capture=True)
                if not re.search(r"\bLogged in using ChatGPT\b", auth):
                    raise ProviderUnavailable(
                        "Этот адаптер требует входа через ChatGPT: выполните codex login. "
                        "API-авторизация не подменяет аккаунт ChatGPT."
                    )
                with tempfile.TemporaryDirectory(prefix="marketer-chatgpt-") as directory:
                    root = Path(directory).resolve()
                    shape, result = root / "schema.json", root / "result.json"
                    shape.write_text(json.dumps(strict_schema(schema.model_json_schema())), encoding="utf-8")
                    args = [
                        "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
                        "--sandbox", "read-only", "-c", 'web_search="disabled"',
                        "-c", "project_doc_max_bytes=0", "--enable", "skip_host_skill_discovery",
                    ]
                    for feature in (
                        "shell_tool", "apps", "plugins", "browser_use", "computer_use",
                        "multi_agent", "image_generation", "hooks", "memories", "view_image",
                        "skill_search", "workspace_dependencies",
                    ):
                        args.extend(["--disable", feature])
                    args.extend(["--output-schema", str(shape), "-o", str(result), "-"])
                    await self._run(args, cwd=root, prompt=prompt)
                    if not result.is_file() or result.is_symlink() or result.stat().st_size > 512_000:
                        raise ProviderUnavailable("ChatGPT не вернул допустимый структурированный ответ.")
                    output = schema.model_validate_json(result.read_bytes())
                    if validate is not None and not validate(output):
                        raise ProviderUnavailable("Ответ ChatGPT не прошёл проверку фактов и ограничений.")
                    return output
        except (TimeoutError, OSError, ValidationError, ValueError):
            raise ProviderUnavailable(
                "Не удалось получить ответ ChatGPT. Проверьте Codex CLI, вход и лимиты аккаунта."
            ) from None


def build_provider(settings: Settings) -> ModelAI:
    """No credentials are read and no process is started during construction."""
    executable = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if executable is None:
        raise InvalidInput(
            "Для ChatGPT-режима нужен официальный Codex CLI и вход через ChatGPT. "
            "На Windows добавьте каталог с нативным codex.exe в PATH (.cmd/.ps1 не подходят)."
        )
    return ModelAI(ChatGPTClient(executable, settings.provider_timeout_seconds), max_posts=settings.max_posts_per_job)
