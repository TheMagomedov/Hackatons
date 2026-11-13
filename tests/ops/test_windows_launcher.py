import asyncio
import importlib.util
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

SPEC = importlib.util.spec_from_file_location("windows_launcher_tests", Path(__file__).parents[2] / "scripts/run-windows.py")
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


async def test_rotation_waits_for_cancelled_application_cleanup():
    ready, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def application():
        try:
            ready.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish.wait()

    app = asyncio.create_task(application())
    await ready.wait()
    rotation = asyncio.create_task(launcher.cancel_app(app))
    await cleaning.wait()
    assert not rotation.done()
    finish.set()
    await asyncio.wait_for(rotation, 1)
    assert app.done()


@pytest.mark.parametrize("returncode", [None, 0])
async def test_tree_is_closed_before_waiting_for_ssh_pipe(returncode):
    events = []

    class Job:
        def close(self):
            events.append("close_tree")

    class Process:
        def kill(self):
            events.append("kill_runner")

        async def wait(self):
            assert events[0] == "close_tree"
            events.append("wait")

    process = Process()
    process.returncode = returncode
    await launcher.stop_tunnel(process, Job())
    assert events[-1] == "wait"


async def test_assignment_failure_still_kills_blocked_runner():
    process = SimpleNamespace(returncode=None, kill=lambda: None, wait=AsyncMock(return_value=1))
    await launcher.stop_tunnel(process, None)
    process.wait.assert_awaited_once()


async def test_conflict_filter_requests_stop_and_omits_raw_error(capsys):
    stop = asyncio.Event()
    filter_ = launcher.PollingConflict(stop)
    assert filter_.filter(logging.LogRecord("aiogram.dispatcher", 40, "", 0, "unrelated", (), None))
    record = logging.LogRecord("aiogram.dispatcher", 40, "", 0,
                               "TelegramConflictError private-detail-token", (), None)
    assert not filter_.filter(record) and stop.is_set()
    assert "private-detail-token" not in capsys.readouterr().out


def test_tunnel_flags_exclude_personal_ssh_and_backend_secrets(monkeypatch):
    command = launcher.tunnel_command("ssh.exe", 8766)
    assert command[command.index("-F") + 1] == "NUL"
    for flag in ("IdentitiesOnly=yes", "IdentityFile=none", "IdentityAgent=none", "BatchMode=yes",
                 "ExitOnForwardFailure=yes", "StrictHostKeyChecking=accept-new"):
        assert flag in command
    assert command[command.index("-R") + 1] == "80:127.0.0.1:8766"
    assert "nokey@localhost.run" in command
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setenv("IMAGE_API_KEY", "secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "private-agent")
    assert not {"TELEGRAM_BOT_TOKEN", "IMAGE_API_KEY", "SSH_AUTH_SOCK"} & launcher.tunnel_environment().keys()


def test_stop_only_creates_owned_stopfile_without_loading_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "STATE", tmp_path)
    monkeypatch.setattr("sys.argv", ["run-windows.py", "--stop"])
    monkeypatch.setattr(launcher, "configuration", lambda: pytest.fail("Stop loaded configuration"))
    assert launcher.main() == 0
    assert (tmp_path / "stop").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows Job Object lifecycle")
async def test_real_job_stops_runner_and_pipe_inheriting_child_without_network():
    from app.ai._windows_process import WindowsJob

    runner = Path(__file__).parents[2] / "app/ai/_windows_process.py"
    child = "import time; time.sleep(30)"
    code = (f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); "
            "print('ready',flush=True); time.sleep(30)")
    job, process = WindowsJob(), None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(runner), sys.executable, "-c", code,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        job.assign(process.pid)
        process.stdin.write(b"\0")
        await process.stdin.drain()
        process.stdin.close()
        assert await asyncio.wait_for(process.stdout.readline(), 3) == b"ready\r\n"
        await asyncio.wait_for(launcher.stop_tunnel(process, job), 2)
        assert process.returncode is not None
    finally:
        await launcher.stop_tunnel(process, job)
