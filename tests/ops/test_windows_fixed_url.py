import asyncio
import importlib.util
import itertools
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

SPEC = importlib.util.spec_from_file_location("fixed_url_launcher", Path(__file__).parents[2] / "scripts/run-windows.py")
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


def setup_host(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    clock = itertools.count(0, 20)
    api = SimpleNamespace(**vars(asyncio))
    api.get_running_loop = lambda: SimpleNamespace(time=lambda: next(clock), call_soon_threadsafe=loop.call_soon_threadsafe)

    async def no_delay(_seconds):
        await asyncio.sleep(0)

    api.sleep = no_delay
    api.create_subprocess_exec = lambda *_a, **_k: pytest.fail("Fixed URL must not spawn SSH or another runtime")
    monkeypatch.setattr(launcher, "asyncio", api)
    monkeypatch.setattr(launcher, "STATE", tmp_path)
    monkeypatch.setattr(launcher, "socket", SimpleNamespace(socket=MagicMock()))
    monkeypatch.setattr(launcher.shutil, "which", lambda _: pytest.fail("Fixed URL must not require SSH"))
    monkeypatch.setattr(launcher.signal, "signal", lambda *_: None)
    monkeypatch.setenv("WINDOWS_PUBLIC_URL", "https://studio.test-tailnet.ts.net:8443/mini/")
    return SimpleNamespace(mini_app_port=8766, mini_app_url="https://studio.test-tailnet.ts.net:8443/mini/")


async def test_fixed_url_preserves_running_app_during_public_outage_and_recovers(tmp_path, monkeypatch):
    import app.core.runtime as runtime

    settings = setup_host(monkeypatch, tmp_path)
    events, states = [], []
    attempts = iter([False, False, False, True])

    def health(_url, *, local=False):
        if local:
            return True
        result = next(attempts)
        if result:
            (tmp_path / "stop").touch()
        return result

    async def application(received):
        assert received is settings
        events.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("stopped")

    monkeypatch.setattr(runtime, "run_application", application)
    monkeypatch.setattr(launcher, "healthy_endpoint", health)
    monkeypatch.setattr(launcher, "save_state", lambda url, state: states.append((url, state)))
    await asyncio.wait_for(launcher.supervise(settings), 3)
    assert [state for _, state in states] == ["starting", "degraded", "degraded", "degraded", "ready"]
    assert all(url == settings.mini_app_url for url, _ in states)
    assert events == ["started", "stopped"]
    assert not (tmp_path / "stop").exists()


async def test_fixed_url_does_not_restart_failed_application(tmp_path, monkeypatch):
    import app.core.runtime as runtime

    settings = setup_host(monkeypatch, tmp_path)
    starts = []

    async def application(_settings):
        starts.append(1)
        raise RuntimeError("test backend failed")

    monkeypatch.setattr(runtime, "run_application", application)
    monkeypatch.setattr(launcher, "healthy_endpoint", lambda *_a, **_k: False)
    with pytest.raises(RuntimeError, match="test backend failed"):
        await asyncio.wait_for(launcher.supervise(settings), 3)
    assert starts == [1]
