import asyncio
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "local_tunnel", Path(__file__).resolve().parents[2] / "scripts" / "run-local-tunnel.py",
)
tunnel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tunnel)


def event(host="test123.lhr.life", **changes):
    return json.dumps({"type": "run.localhost.tunnel.registered", "subject": host,
                       "data": {"address": host, "port": 80, "message": "Ignored banner and QR"}, **changes})


def test_structured_tunnel_parser_rejects_banner_injection_and_untrusted_targets():
    assert tunnel.tunnel_url(event()) == "https://test123.lhr.life/mini/"
    assert tunnel.tunnel_url(event("test123.localhost.run")) == "https://test123.localhost.run/mini/"
    for line in ["https://test123.lhr.life", "{broken", "[]", event("evil.example"),
                 event("lhr.life.evil.example"), event("test123.lhr.life/evil"),
                 event("test123.lhr.life@evil.example"), event(type="run.localhost.tunnel.closed"),
                 event(subject="other.lhr.life"), event(data={"address": "test123.lhr.life", "port": 443})]:
        assert tunnel.tunnel_url(line) is None


class Child:
    def __init__(self, events, graceful=True):
        self.returncode = None
        self.events, self.graceful = events, graceful
        self.finished = asyncio.Event()

    def terminate(self):
        self.events.append("terminate")
        if self.graceful:
            self.returncode = 0
            self.finished.set()

    def kill(self):
        self.events.append("kill")
        self.returncode = -9
        self.finished.set()

    async def wait(self):
        await self.finished.wait()
        self.events.append("wait")
        return self.returncode


async def test_rotation_waits_for_owned_child_and_preserves_empty_model_keys():
    events, configs = [], []

    async def spawn(*args, **kwargs):
        events.append("start")
        configs.append(kwargs["env"])
        return Child(events)

    base = {"MINI_APP_URL": "https://old.localhost.run/mini/", "AI_API_KEY": "", "IMAGE_API_KEY": ""}
    app = tunnel.AppChild(base, spawn)
    await app.rotate("https://one.lhr.life/mini/")
    await app.rotate("https://one.lhr.life/mini/")
    assert events == ["start"]
    await app.rotate("https://two.lhr.life/mini/")
    assert events == ["start", "terminate", "wait", "start"]
    assert configs[-1]["MINI_APP_URL"] == "https://two.lhr.life/mini/"
    assert configs[-1]["AI_API_KEY"] == configs[-1]["IMAGE_API_KEY"] == ""
    assert base["MINI_APP_URL"] == "https://old.localhost.run/mini/"
    await app.close()
    assert events[-2:] == ["terminate", "wait"] and app.process is None


async def test_unresponsive_owned_child_is_killed_after_grace_period():
    events = []
    child = Child(events, graceful=False)
    await tunnel.stop_child(child, timeout=0.01)
    assert events == ["terminate", "kill", "wait"]
    await tunnel.stop_child(child, timeout=0.01)
    assert events == ["terminate", "kill", "wait"]


async def test_stop_request_prevents_spawning_replacement():
    async def forbidden_spawn(*args, **kwargs):
        raise AssertionError("No replacement after shutdown")

    events = []
    app = tunnel.AppChild({}, forbidden_spawn, stopping=lambda: True)
    app.process = Child(events)
    await app.rotate("https://next.lhr.life/mini/")
    assert events == ["terminate", "wait"] and app.process is None
