import asyncio
import http.client
import itertools
import json
from email.message import Message
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from tests.ops.test_windows_launcher import launcher

GOOD = b'{"status":"ok","mode":"live","runtime_mode":"live"}'


def mock_opener(monkeypatch, body=GOOD, status=200, mime="application/json"):
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = status
    response.headers = Message()
    response.headers["Content-Type"] = mime
    response.read.return_value = body
    opener = Mock()
    opener.open.return_value = response
    build = Mock(return_value=opener)
    monkeypatch.setattr(launcher.urllib.request, "build_opener", build)
    return build, opener, response


def test_health_is_bounded_get_no_auth_no_redirect_and_local_proxy_bypass(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-be-used")
    monkeypatch.setenv("IMAGE_API_KEY", "must-not-be-used")
    build, opener, response = mock_opener(monkeypatch)
    assert launcher.healthy_endpoint("http://127.0.0.1:8766/mini/health", local=True)
    request = opener.open.call_args.args[0]
    assert request.get_method() == "GET" and request.data is None
    assert request.get_header("Authorization") is None
    assert request.get_header("X-telegram-init-data") is None
    assert set(dict(request.header_items())) == {"Accept", "Cache-control"}
    assert opener.open.call_args.kwargs == {"timeout": 6}
    response.read.assert_called_once_with(4097)
    handlers = build.call_args.args
    assert isinstance(handlers[0], launcher.NoRedirect)
    assert handlers[0].redirect_request(None, None, 302, None, None, "https://other.example") is None
    assert handlers[1].proxies == {}


@pytest.mark.parametrize("body,status,mime", [
    (GOOD, 302, "application/json"), (GOOD, 502, "application/json"), (GOOD, 200, "text/html"),
    (b"x" * 4097, 200, "application/json"), (b"broken", 200, "application/json"),
    (b"[" * 1500 + b"]" * 1500, 200, "application/json"),
    (b'{"status":"ok","mode":"preview","runtime_mode":"live"}', 200, "application/json"),
    (b"[]", 200, "application/json"), (b"\xff", 200, "application/json"),
])
def test_invalid_health_never_looks_ready(monkeypatch, body, status, mime):
    mock_opener(monkeypatch, body, status, mime)
    assert not launcher.healthy_endpoint("https://studio.lhr.life/mini/health")


@pytest.mark.parametrize("url", ["bad-url", "file:///local", "https://user:pass@example.com/mini/health",
                                 "https://example.com:bad/mini/health", "https://example.com/?key=secret"])
def test_invalid_health_url_does_not_send_request(monkeypatch, url):
    build, _, _ = mock_opener(monkeypatch)
    assert not launcher.healthy_endpoint(url)
    build.assert_not_called()


@pytest.mark.parametrize("error", [OSError(), http.client.BadStatusLine("invalid"),
                                   http.client.IncompleteRead(b"partial"), RecursionError()])
def test_bad_network_or_parser_result_is_failure_not_supervisor_crash(monkeypatch, error):
    _, opener, _ = mock_opener(monkeypatch)
    opener.open.side_effect = error
    assert not launcher.healthy_endpoint("https://studio.lhr.life/mini/health")


def test_watchdog_requires_three_consecutive_remote_only_failures_and_resets():
    watchdog = launcher.TunnelWatchdog()
    assert not watchdog.observe(local_ok=True, public_ok=False)
    assert not watchdog.observe(local_ok=True, public_ok=False)
    assert not watchdog.observe(local_ok=True, public_ok=True)
    assert watchdog.failures == 0
    assert not watchdog.observe(local_ok=True, public_ok=False)
    assert not watchdog.observe(local_ok=False, public_ok=False)
    assert watchdog.failures == 0
    assert not watchdog.observe(local_ok=True, public_ok=False)
    assert not watchdog.observe(local_ok=True, public_ok=False)
    assert watchdog.observe(local_ok=True, public_ok=False)


async def test_supervisor_reconnects_only_owned_tunnel_after_three_failures(tmp_path, monkeypatch):
    import app.ai._windows_process as process_module
    import app.core.runtime as runtime_module

    events, states, probes = [], [], []
    loop = asyncio.get_running_loop()
    clock = itertools.count(0, 20)
    api = SimpleNamespace(**vars(asyncio))
    api.get_running_loop = lambda: SimpleNamespace(time=lambda: next(clock),
                                                    call_soon_threadsafe=loop.call_soon_threadsafe)
    async def no_delay(_seconds):
        await asyncio.sleep(0)
    api.sleep = no_delay
    monkeypatch.setattr(launcher, "asyncio", api)
    monkeypatch.setattr(launcher, "STATE", tmp_path)
    monkeypatch.setattr(launcher.shutil, "which", lambda _name: "fake-ssh")
    monkeypatch.setattr(launcher, "socket", SimpleNamespace(socket=MagicMock()))
    monkeypatch.setattr(launcher.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(launcher, "save_state", lambda url, status: states.append((url, status)))
    def health(url, *, local=False):
        probes.append((url, local))
        return local
    monkeypatch.setattr(launcher, "healthy_endpoint", health)

    class Job:
        def assign(self, pid):
            self.pid = pid
        def close(self):
            events.append(("close_job", self.pid))
    monkeypatch.setattr(process_module, "WindowsJob", Job)

    async def application(_settings):
        try:
            events.append(("app_started", 1))
            await asyncio.Event().wait()
        finally:
            events.append(("app_stopped", 1))
    monkeypatch.setattr(runtime_module, "run_application", application)
    registration = json.dumps({"type": "run.localhost.tunnel.registered", "subject": "studio.lhr.life",
                               "data": {"port": 80, "address": "studio.lhr.life"}}).encode()
    processes = []
    class Process:
        returncode = None
        def __init__(self, pid):
            self.pid, self.lines = pid, 0
            self.stdin = SimpleNamespace(write=lambda _data: None, drain=no_drain, close=lambda: None)
            self.stdout = self
        async def readline(self):
            self.lines += 1
            await asyncio.sleep(0)
            return registration if self.lines == 1 else b"heartbeat\n"
        def kill(self):
            self.returncode = 0
            events.append(("kill_runner", self.pid))
        async def wait(self):
            return self.returncode
    async def no_drain():
        pass
    async def spawn(*_args, **_kwargs):
        process = Process(len(processes) + 1)
        if processes:
            assert ("app_stopped", 1) in events and ("close_job", 1) in events
            (tmp_path / "stop").touch()
        processes.append(process)
        return process
    api.create_subprocess_exec = spawn
    settings = SimpleNamespace(mini_app_port=8766, model_copy=lambda **kwargs: SimpleNamespace(**kwargs["update"]))
    await asyncio.wait_for(launcher.supervise(settings), 3)
    assert len(processes) == 2 and len(probes) == 6
    assert [state for _, state in states] == ["starting", "degraded", "degraded", "degraded", "reconnecting"]
    assert events.index(("app_stopped", 1)) < events.index(("close_job", 1))
    assert all(process.returncode == 0 for process in processes)
