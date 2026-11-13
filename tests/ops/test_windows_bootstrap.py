import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("windows_bootstrap_tests",
                                            Path(__file__).parents[2] / "scripts/start_windows_dependencies.py")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)
KEY = "test-bootstrap-key-not-a-real-secret-0123456789"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    values = {
        "IMAGE_API_BASE": "https://test-device.example.ts.net/flux-demo-api/v1",
        "MODERATION_API_BASE": "https://test-device.example.ts.net/flux-demo-api/v1",
        "IMAGE_API_KEY": KEY, "MODERATION_API_KEY": KEY, "IMAGE_MODEL": "flux1-dev",
        "FLUX_GATEWAY_KEY_FILE": "key.txt", "FLUX_STARTER_PATH": "start_flux.py",
        "TELEGRAM_BOT_TOKEN": "test-bot-token", "MINI_APP_PORT": "8766",
    }
    for key in values:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text("\n".join(k + "=" + v for k, v in values.items()), encoding="utf-8")
    (tmp_path / "key.txt").write_text(KEY, encoding="ascii")
    (tmp_path / "start_flux.py").write_text("# configured test launcher", encoding="utf-8")
    return tmp_path


def test_configuration_keeps_secrets_local_and_uses_new_gateway(configured):
    before = dict(os.environ)
    config = bootstrap.configuration(configured)
    assert os.environ == before
    assert config["port"] == 8194 and config["gateway_env"]["FLUX_GATEWAY_PREFIX"] == "/flux-demo-api"
    assert config["gateway_env"]["FLUX_GATEWAY_HOSTNAME"] == "test-device.example.ts.net"
    assert "TELEGRAM_BOT_TOKEN" not in config["gateway_env"]


def test_windows_native_runtime_variables_survive_without_credentials(monkeypatch):
    monkeypatch.setenv("SYSTEMDRIVE", "C:")
    monkeypatch.setenv("COMSPEC", "C:/Windows/System32/cmd.exe")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-inherit")
    environment = bootstrap.child_environment()
    assert environment["SYSTEMDRIVE"] == "C:" and environment["COMSPEC"].endswith("cmd.exe")
    assert environment["PATHEXT"] == ".COM;.EXE;.BAT;.CMD"
    assert environment["PYTHONUTF8"] == "1"
    assert "TELEGRAM_BOT_TOKEN" not in environment


def test_private_native_codex_path_changes_only_child_path(tmp_path, monkeypatch):
    directory = tmp_path / "native tools"
    directory.mkdir()
    executable = directory / "codex.exe"
    executable.touch()
    monkeypatch.setenv("PATH", "original-path")
    values = {"AI_PROVIDER_FACTORY": "app.ai.chatgpt:build_provider", "CODEX_CLI_PATH": str(executable)}
    result = bootstrap.application_environment({"app_env": values}, tmp_path)
    assert result["PATH"] == str(directory) + os.pathsep + "original-path"
    assert os.environ["PATH"] == "original-path" and "PATH" not in values


def test_missing_native_codex_is_clear_error_without_running_wrapper(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda *_args, **_kwargs: None)
    config = {"app_env": {"AI_PROVIDER_FACTORY": "app.ai.chatgpt:build_provider"}}
    with pytest.raises(bootstrap.BootstrapError, match="CODEX_CLI_PATH"):
        bootstrap.application_environment(config, tmp_path)
    wrapper = tmp_path / "codex.cmd"
    wrapper.touch()
    config["app_env"]["CODEX_CLI_PATH"] = str(wrapper)
    with pytest.raises(bootstrap.BootstrapError, match="native codex.exe"):
        bootstrap.application_environment(config, tmp_path)


@pytest.mark.parametrize("fault", ["identity", "open_models", "wrong_key", "invalid_models"])
def test_matching_identity_auth_and_model_are_all_required(monkeypatch, fault):
    calls = []

    def request(url, key=None):
        calls.append((url, key))
        if url.endswith("/health"):
            return 200, {"service": "foreign" if fault == "identity" else "flux-image-api",
                         "ready": True, "configured": True}
        if key is None:
            return (200, {}) if fault == "open_models" else (401, None)
        if fault == "wrong_key":
            return 401, None
        return 200, {"data": None if fault == "invalid_models" else [{"id": "flux1-dev"}]}

    monkeypatch.setattr(bootstrap, "request_json", request)
    assert not bootstrap.gateway_ready("http://127.0.0.1:8194/v1", KEY)
    if fault == "identity":
        assert len(calls) == 1  # Do not send a credential to an unidentified listener.


def test_foreign_core_port_prevents_any_start(configured, monkeypatch):
    monkeypatch.setattr(bootstrap, "core_ready", lambda _: False)
    monkeypatch.setattr(bootstrap, "listening", lambda _: True)
    monkeypatch.setattr(bootstrap, "start_hidden", lambda *args: pytest.fail("Unexpected process launch"))
    with pytest.raises(bootstrap.BootstrapError, match="занят"):
        bootstrap.bootstrap(configured)


def test_check_only_never_launches_missing_dependency(configured, monkeypatch):
    monkeypatch.setattr(bootstrap, "core_ready", lambda _: False)
    monkeypatch.setattr(bootstrap, "listening", lambda _: False)
    monkeypatch.setattr(bootstrap, "start_hidden", lambda *args: pytest.fail("Unexpected process launch"))
    with pytest.raises(bootstrap.BootstrapError, match="--check"):
        bootstrap.bootstrap(configured, check=True)


def test_occupied_unverified_gateway_is_not_replaced(configured, monkeypatch):
    monkeypatch.setattr(bootstrap, "core_ready", lambda _: True)
    monkeypatch.setattr(bootstrap, "gateway_ready", lambda *_: False)
    monkeypatch.setattr(bootstrap, "listening", lambda _: True)
    monkeypatch.setattr(bootstrap, "start_hidden", lambda *args: pytest.fail("Unexpected process launch"))
    with pytest.raises(bootstrap.BootstrapError, match="Перезапуск запрещён"):
        bootstrap.bootstrap(configured)


def test_missing_services_start_in_order_without_forwarding_bot_secret_to_gateway(configured, monkeypatch):
    running, calls = set(), []
    monkeypatch.setattr(bootstrap, "core_ready", lambda _: "flux-startup" in running)
    monkeypatch.setattr(bootstrap, "gateway_ready", lambda *_: "flux-gateway" in running)
    monkeypatch.setattr(bootstrap, "listening", lambda _: False)

    def spawn(command, root, name, extra=None):
        calls.append((command, name, extra))
        running.add(name)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(bootstrap, "start_hidden", spawn)
    bootstrap.bootstrap(configured)
    assert [call[1] for call in calls] == ["flux-startup", "flux-gateway", "demo-host"]
    assert calls[0][0][-1] == "--no-browser"
    assert "TELEGRAM_BOT_TOKEN" not in calls[1][2]
    assert calls[2][2]["TELEGRAM_BOT_TOKEN"] == "test-bot-token"


def test_ready_dependencies_are_reused_and_occupied_miniapp_is_not_taken_over(configured, monkeypatch):
    monkeypatch.setattr(bootstrap, "core_ready", lambda _: True)
    monkeypatch.setattr(bootstrap, "gateway_ready", lambda *_: True)
    monkeypatch.setattr(bootstrap, "listening", lambda _: True)
    monkeypatch.setattr(bootstrap, "start_hidden", lambda *args: pytest.fail("Unexpected process launch"))
    bootstrap.bootstrap(configured, dependencies_only=True)
    with pytest.raises(bootstrap.BootstrapError, match="Mini App"):
        bootstrap.bootstrap(configured)


def test_remote_health_failure_does_not_start_bot(configured, monkeypatch):
    monkeypatch.setattr(bootstrap, "core_ready", lambda _: True)
    monkeypatch.setattr(bootstrap, "gateway_ready", lambda base, _: base.startswith("http://127.0.0.1"))
    monkeypatch.setattr(bootstrap, "start_hidden", lambda *args: pytest.fail("Unexpected process launch"))
    with pytest.raises(bootstrap.BootstrapError, match="Приватный API"):
        bootstrap.bootstrap(configured)


def test_gateway_key_mismatch_stops_before_spawn(configured, monkeypatch):
    (configured / "key.txt").write_text("different-existing-key", encoding="ascii")
    monkeypatch.setattr(bootstrap, "core_ready", lambda _: True)
    monkeypatch.setattr(bootstrap, "gateway_ready", lambda *_: False)
    monkeypatch.setattr(bootstrap, "listening", lambda _: False)
    monkeypatch.setattr(bootstrap, "start_hidden", lambda *args: pytest.fail("Unexpected process launch"))
    with pytest.raises(bootstrap.BootstrapError, match="не совпадает"):
        bootstrap.bootstrap(configured)
