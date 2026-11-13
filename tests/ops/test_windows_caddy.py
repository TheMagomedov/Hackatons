import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "windows_caddy_tests", Path(__file__).parents[2] / "scripts/start_windows_dependencies.py",
)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


def document():
    return {
        "admin": {"listen": "127.0.0.1:12019", "config": {"persist": False}},
        "storage": {"module": "file_system", "root": "C:/private/caddy"},
        "apps": {
            "http": {"servers": {"studio": {
                "listen": ["192.168.1.200:443", "192.168.1.200:80"],
                "automatic_https": {"disable_redirects": True},
                "routes": [
                    {"match": [{"path": ["/mini/*"]}], "handle": [{
                        "handler": "subroute", "routes": [{"handle": [{
                            "handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8766"}],
                        }]}],
                    }]},
                    {"handle": [{"handler": "static_response", "status_code": 404}]},
                ],
            }}},
            "tls": {"automation": {"policies": [{"subjects": ["studio.example.org"]}]}},
        },
    }


@pytest.fixture
def adapted(tmp_path, monkeypatch):
    executable, path = tmp_path / "caddy.exe", tmp_path / "Caddyfile.private"
    executable.touch()
    path.write_text("# Local fixture; the official adapter is mocked here.", encoding="utf-8")
    values = {
        "CADDY_EXE_PATH": str(executable), "CADDY_CONFIG_PATH": str(path),
        "WINDOWS_PUBLIC_URL": "https://studio.example.org/mini/",
        "TELEGRAM_BOT_TOKEN": "not-for-caddy", "IMAGE_API_KEY": "not-for-caddy-either",
    }
    payload, calls = document(), []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    return values, payload, calls


def test_opt_in_configuration_does_not_probe_or_execute_without_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *_a, **_k: pytest.fail("Unexpected execution"))
    assert bootstrap.caddy_configuration({}, tmp_path, 8766) is None
    with pytest.raises(bootstrap.BootstrapError, match="CADDY_EXE_PATH"):
        bootstrap.caddy_configuration({"CADDY_CONFIG_PATH": "Caddyfile"}, tmp_path, 8766)


def test_adapter_only_and_child_environment_have_no_application_secrets(adapted, tmp_path, monkeypatch):
    values, payload, calls = adapted
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "global-token-must-not-inherit")
    monkeypatch.setenv("IMAGE_API_KEY", "global-image-key-must-not-inherit")
    values["CADDY_PUBLIC_HOST"] = "studio.example.org"
    config = bootstrap.caddy_configuration(values, tmp_path, 8766)
    command, args = calls[0]
    assert command[1:] == ["adapt", "--config", values["CADDY_CONFIG_PATH"], "--adapter", "caddyfile"]
    assert "--validate" not in command and args["timeout"] == 15
    assert config["expected"] == payload and config["admin_url"] == "http://127.0.0.1:12019/config/"
    assert config["env"] == {"CADDY_PUBLIC_HOST": "studio.example.org"}
    assert "TELEGRAM_BOT_TOKEN" not in args["env"] and "IMAGE_API_KEY" not in args["env"]


@pytest.mark.parametrize("url", [
    "", "http://studio.example.org/mini/", "https://studio.example.org:8443/mini/",
    "https://studio.example.org/", "https://user:secret@studio.example.org/mini/",
    "https://studio.example.org/mini/?key=secret", "https://[invalid/mini/",
])
def test_public_endpoint_must_be_explicit_standard_https(adapted, tmp_path, url):
    values, _, calls = adapted
    values["WINDOWS_PUBLIC_URL"] = url
    with pytest.raises(bootstrap.BootstrapError, match="WINDOWS_PUBLIC_URL"):
        bootstrap.caddy_configuration(values, tmp_path, 8766)
    assert not calls


@pytest.mark.parametrize("fault", [
    "public_admin", "admin_backend_collision", "wildcard", "loopback_web", "wrong_upstream", "extra_upstream",
    "unscoped", "path_alternative", "file_server", "implicit_http", "different_subject", "extra_app", "access_logs",
])
def test_configuration_cannot_expose_another_service_or_lose_route_scope(adapted, tmp_path, fault):
    values, payload, _ = adapted
    server = payload["apps"]["http"]["servers"]["studio"]
    route = server["routes"][0]
    proxy = route["handle"][0]["routes"][0]["handle"][0]
    if fault == "public_admin":
        payload["admin"]["listen"] = "0.0.0.0:12019"
    elif fault == "admin_backend_collision":
        payload["admin"]["listen"] = "127.0.0.1:8766"
    elif fault == "wildcard":
        server["listen"] = ["0.0.0.0:443"]
    elif fault == "loopback_web":
        server["listen"] = ["127.0.0.1:443"]
    elif fault == "wrong_upstream":
        proxy["upstreams"] = [{"dial": "127.0.0.1:8188"}]
    elif fault == "extra_upstream":
        proxy["upstreams"].append({"dial": "127.0.0.1:11434"})
    elif fault == "unscoped":
        route.pop("match")
    elif fault == "path_alternative":
        route["match"].append({"path": ["/*"]})
    elif fault == "file_server":
        proxy["handler"] = "file_server"
    elif fault == "implicit_http":
        server.pop("automatic_https")
    elif fault == "different_subject":
        payload["apps"]["tls"]["automation"]["policies"][0]["subjects"] = ["wrong.example.org"]
    elif fault == "extra_app":
        payload["apps"]["layer4"] = {}
    elif fault == "access_logs":
        server["logs"] = {"default_logger_name": "unsafe-request-log"}
    with pytest.raises(bootstrap.BootstrapError, match="Caddyfile"):
        bootstrap.caddy_configuration(values, tmp_path, 8766)


def test_adapt_failure_does_not_echo_configuration_or_credentials(adapted, tmp_path, monkeypatch):
    values, _, _ = adapted
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=1, stdout=b"private configuration", stderr=b"secret fixture",
    ))
    with pytest.raises(bootstrap.BootstrapError) as error:
        bootstrap.caddy_configuration(values, tmp_path, 8766)
    assert "private configuration" not in str(error.value) and "secret fixture" not in str(error.value)


def test_reuse_requires_identical_active_configuration_and_all_listeners(adapted, tmp_path, monkeypatch):
    values, _, _ = adapted
    config = bootstrap.caddy_configuration(values, tmp_path, 8766)
    active = copy.deepcopy(config["expected"])
    monkeypatch.setattr(bootstrap, "request_json", lambda url: (200, active))
    monkeypatch.setattr(bootstrap, "endpoint_listening", lambda *_: True)
    assert bootstrap.caddy_ready(config)
    active["admin"]["config"]["persist"] = True
    assert not bootstrap.caddy_ready(config)
    active["admin"]["config"]["persist"] = False
    monkeypatch.setattr(bootstrap, "endpoint_listening", lambda host, port: port != 443)
    assert not bootstrap.caddy_ready(config)


@pytest.mark.parametrize("occupied", [False, True])
def test_check_or_foreign_port_never_starts_or_reloads_caddy(adapted, tmp_path, monkeypatch, occupied):
    values, _, _ = adapted
    config = {"caddy": bootstrap.caddy_configuration(values, tmp_path, 8766)}
    monkeypatch.setattr(bootstrap, "caddy_ready", lambda _: False)
    monkeypatch.setattr(bootstrap, "endpoint_listening", lambda *_: occupied)
    monkeypatch.setattr(bootstrap, "start_hidden", lambda *_a, **_k: pytest.fail("Unexpected mutation"))
    with pytest.raises(bootstrap.BootstrapError, match="занят|--check"):
        bootstrap.ensure_direct_https(config, tmp_path, check=not occupied)


def test_only_missing_caddy_starts_with_reviewed_file_and_minimal_values(adapted, tmp_path, monkeypatch):
    values, _, _ = adapted
    config = {"caddy": bootstrap.caddy_configuration(values, tmp_path, 8766)}
    starts = []
    monkeypatch.setattr(bootstrap, "caddy_ready", lambda _: bool(starts))
    monkeypatch.setattr(bootstrap, "endpoint_listening", lambda *_: False)

    def spawn(command, root, name, extra):
        starts.append((command, root, name, extra))
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(bootstrap, "start_hidden", spawn)
    bootstrap.ensure_direct_https(config, tmp_path)
    assert len(starts) == 1 and starts[0][2] == "caddy" and starts[0][3] == {}
    assert starts[0][0][1:] == ["run", "--config", values["CADDY_CONFIG_PATH"], "--adapter", "caddyfile"]
    bootstrap.ensure_direct_https(config, tmp_path, check=True)
    assert len(starts) == 1
