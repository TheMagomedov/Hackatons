"""Contract and fail-closed checks for the explicitly selected ChatGPT adapter."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, ConfigDict

from app.ai.chatgpt import ChatGPTClient, build_provider, strict_schema
from app.config import Settings
from app.shared.errors import InvalidInput, ProviderUnavailable


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    optional: str | None = None


async def test_chatgpt_keeps_api_credentials_out_and_validates_structured_output(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-cli")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-reach-cli")
    client = ChatGPTClient("codex", 10)
    calls = []

    async def run(args, **kwargs):
        calls.append((args, kwargs))
        if args == ["login", "status"]:
            return "Logged in using ChatGPT"
        assert all(flag in args for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral"))
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert args[args.index("project_doc_max_bytes=0") - 1] == "-c"
        assert args[args.index("skip_host_skill_discovery") - 1] == "--enable"
        for feature in ("shell_tool", "apps", "plugins", "browser_use", "hooks", "memories",
                        "view_image", "skill_search", "workspace_dependencies"):
            assert args[args.index(feature) - 1] == "--disable"
        shape = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
        assert shape["required"] == ["text", "optional"]
        Path(args[args.index("-o") + 1]).write_text('{"text":"ready","optional":null}')
        return ""

    monkeypatch.setattr(client, "_run", run)
    answer = await client.complete(Answer, "Answer", {"notes": "hello"})
    assert answer.text == "ready" and len(calls) == 2
    assert "OPENAI_API_KEY" not in client.env and "TELEGRAM_BOT_TOKEN" not in client.env
    assert not Path(calls[1][1]["cwd"]).exists()


@pytest.mark.parametrize("auth", ["Logged in using an API key", "Not logged in", ""])
async def test_never_silently_falls_back_to_api_auth(monkeypatch, auth):
    client = ChatGPTClient("codex", 10)
    run = AsyncMock(return_value=auth)
    monkeypatch.setattr(client, "_run", run)
    with pytest.raises(ProviderUnavailable, match="ChatGPT"):
        await client.complete(Answer, "Answer", {})
    assert run.await_count == 1


@pytest.mark.parametrize("content", ['{"text":1}', 'not JSON', '{"text":"ok","secret":"bad"}'])
async def test_invalid_output_is_sanitized_without_retry(monkeypatch, content):
    client = ChatGPTClient("codex", 10)
    calls = []

    async def run(args, **kwargs):
        calls.append(args)
        if args == ["login", "status"]:
            return "Logged in using ChatGPT"
        Path(args[args.index("-o") + 1]).write_text(content)
        return ""

    monkeypatch.setattr(client, "_run", run)
    with pytest.raises(ProviderUnavailable) as error:
        await client.complete(Answer, "Answer", {})
    assert content not in str(error.value) and len(calls) == 2


@pytest.mark.skipif(os.name != "posix", reason="ChatGPT CLI process groups require Linux/macOS")
async def test_cancellation_kills_cli_and_does_not_leave_background_generation(monkeypatch):
    started = asyncio.Event()

    class Process:
        pid = 123456
        returncode = None
        killed = False

        async def communicate(self, value):
            started.set()
            await asyncio.Event().wait()

        def kill(self, pid, sig):
            assert pid == self.pid and sig == signal.SIGKILL
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    process = Process()
    monkeypatch.setattr("app.ai.chatgpt.os.killpg", process.kill)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    client = ChatGPTClient("codex", 10)
    task = asyncio.create_task(client._run(["exec"], prompt="sample"))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and not client._processes


@pytest.mark.parametrize("operation", ["cancel", "close", "timeout"])
async def test_real_launcher_child_stops_with_parent_without_network(tmp_path, operation):
    """An npm-style launcher spawns a native child; neither may outlive cleanup."""
    child_code = (
        "import time; from pathlib import Path; time.sleep(0.5); "
        "Path('continued').write_text('generation continued'); time.sleep(30)"
    )
    launcher_code = (
        "import os,subprocess,sys,time; from pathlib import Path; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        "Path('launcher.pid').write_text(str(os.getpid())); "
        "Path('child.pid').write_text(str(p.pid)); time.sleep(30)"
    )
    client = ChatGPTClient(sys.executable, 10)
    task = asyncio.create_task(client._run(["-c", launcher_code], cwd=tmp_path))
    try:
        async with asyncio.timeout(3):
            while not (tmp_path / "child.pid").exists():
                await asyncio.sleep(0.01)
        if operation == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif operation == "close":
            await client.close()
            with pytest.raises(ProviderUnavailable):
                await task
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, timeout=0.05)
        await asyncio.sleep(0.6)
        assert not (tmp_path / "continued").exists(), "Native child continued after launcher was stopped"
        assert not client._processes
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()
        if os.name == "posix" and (tmp_path / "launcher.pid").exists():
            try:
                os.killpg(int((tmp_path / "launcher.pid").read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_windows_adapter_selects_native_executable_without_shell(monkeypatch):
    looked_up = []

    def which(name):
        looked_up.append(name)
        return "C:/Codex/codex.exe"

    monkeypatch.setattr("app.ai.chatgpt.shutil.which", which)
    monkeypatch.setattr("app.ai.chatgpt.os.name", "nt")
    provider = build_provider(Settings())
    assert provider.client.executable == "C:/Codex/codex.exe"
    assert looked_up == ["codex.exe"]


def test_missing_cli_is_explicit_configuration_error(monkeypatch):
    monkeypatch.setattr("app.ai.chatgpt.shutil.which", lambda _: None)
    with pytest.raises(InvalidInput, match="Codex CLI"):
        build_provider(Settings())


def test_schema_normalizes_nested_definitions_without_mutating_input():
    original = {"$defs": {"X": {"type": "object", "properties": {"x": {"type": "string", "default": ""}}}}}
    transformed = strict_schema(original)
    assert transformed["$defs"]["X"]["required"] == ["x"]
    assert "default" in original["$defs"]["X"]["properties"]["x"]


async def test_real_runner_preserves_unicode_stdin_and_filters_private_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "private-bot-token")
    monkeypatch.setenv("IMAGE_API_KEY", "private-image-key")
    monkeypatch.setenv("OPENAI_API_KEY", "private-api-key")
    monkeypatch.setenv("CODEX_API_KEY", "private-codex-key")
    client = ChatGPTClient(sys.executable, 10)
    payload = "Стекло, кавычки ' \" и $() & |\nвторая строка"
    code = (
        "import json,os,sys; print(json.dumps({'input':sys.stdin.buffer.read().decode('utf-8'),"
        "'cwd':os.getcwd(),'secret_keys':[k for k in os.environ if k in "
        "('TELEGRAM_BOT_TOKEN','IMAGE_API_KEY','OPENAI_API_KEY','CODEX_API_KEY')]}))"
    )
    result = json.loads(await client._run(["-c", code], cwd=tmp_path, prompt=payload, capture=True))
    assert result == {"input": payload, "cwd": str(tmp_path), "secret_keys": []}
    assert not client._processes and not client._jobs


async def test_oversized_stdin_never_starts_process(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(InvalidInput):
        await ChatGPTClient(sys.executable, 10)._run(["-c", "pass"], prompt="я" * 350_001)
    create.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
async def test_failed_job_assignment_never_starts_native_child(monkeypatch, tmp_path):
    def reject(_job, _pid):
        raise OSError("Job assignment denied")

    monkeypatch.setattr("app.ai._windows_process.WindowsJob.assign", reject)
    client = ChatGPTClient(sys.executable, 10)
    with pytest.raises(OSError):
        await client._run(["-c", "from pathlib import Path; Path('started').touch()"], cwd=tmp_path)
    assert not (tmp_path / "started").exists()
    assert not client._processes and not client._jobs


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
async def test_job_closes_remaining_child_after_launcher_exits(tmp_path):
    client = ChatGPTClient(sys.executable, 10)
    child = "import time; from pathlib import Path; time.sleep(0.7); Path('continued').touch()"
    launcher = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])"
    await client._run(["-c", launcher], cwd=tmp_path)
    await asyncio.sleep(0.9)
    assert not (tmp_path / "continued").exists()
    assert not client._processes and not client._jobs
