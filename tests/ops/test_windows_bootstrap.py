import importlib.util
import json
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


@pytest.mark.parametrize("factory", ["app.ai.chatgpt:build_provider", "app.ai.agentrouter:build_provider"])
def test_private_native_codex_path_changes_only_child_path(tmp_path, monkeypatch, factory):
    directory = tmp_path / "native tools"
    directory.mkdir()
    executable = directory / "codex.exe"
    executable.touch()
    monkeypatch.setenv("PATH", "original-path")
    values = {"AI_PROVIDER_FACTORY": factory, "CODEX_CLI_PATH": str(executable)}
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


@pytest.fixture
def local_config(configured, monkeypatch):
    executable = configured / "local-model" / "ollama.exe"
    executable.parent.mkdir()
    executable.touch()
    models = configured / "local-model" / "models"
    models.mkdir()
    values = {
        "AI_PROVIDER_FACTORY": "app.ai.local:build_provider",
        "LOCAL_TEXT_MODEL": "qwen3:4b-instruct", "LOCAL_TEXT_API_BASE": "http://127.0.0.1:11435",
        "OLLAMA_EXE_PATH": str(executable), "OLLAMA_MODELS_PATH": str(models),
    }
    for key in values:
        monkeypatch.delenv(key, raising=False)
    with (configured / ".env").open("a", encoding="utf-8") as stream:
        stream.write("\n" + "\n".join(key + "=" + value for key, value in values.items()))
    return bootstrap.configuration(configured)


def test_local_configuration_matches_client_and_never_reads_other_env_namespace(local_config, monkeypatch):
    settings = local_config["local_text"]["settings"]
    assert settings.base_url == "http://127.0.0.1:11435" and local_config["local_text"]["port"] == 11435
    assert settings.model == "qwen3:4b-instruct"
    monkeypatch.setenv("LOCAL_TEXT_API_BASE", "https://must-not-be-read.invalid")
    explicit = bootstrap.local_text_configuration({"AI_PROVIDER_FACTORY": "app.ai.local:build_provider"}, Path("."))
    assert explicit["settings"].base_url == "http://127.0.0.1:11434"


@pytest.mark.parametrize("fault", ["version", "status_missing", "cloud_enabled", "truthy", "unknown"])
def test_ollama_identity_requires_explicit_disabled_cloud(local_config, monkeypatch, fault):
    calls = []

    def request(url, key=None, *, payload=None):
        calls.append((url, key, payload))
        if url.endswith("/api/version"):
            return 200, {"version": "" if fault == "version" else "0.40.16"}
        if fault == "status_missing":
            return 404, None
        if fault == "unknown":
            return 200, {"cloud": None}
        return 200, {"cloud": {"disabled": "true" if fault == "truthy" else False}}

    monkeypatch.setattr(bootstrap, "request_json", request)
    assert not bootstrap.ollama_server_ready(local_config["local_text"]["settings"])
    assert all(url.startswith("http://127.0.0.1:11435/") and key is None and payload is None
               for url, key, payload in calls)


def test_ollama_ready_checks_cloud_and_model_without_generation(local_config, configured, monkeypatch):
    calls = []

    def request(url, key=None, *, payload=None):
        calls.append((url, key, payload))
        if url.endswith("/api/version"):
            return 200, {"version": "0.40.16"}
        if url.endswith("/api/status"):
            return 200, {"cloud": {"disabled": True, "source": "env"}}
        assert url.endswith("/api/show")
        return 200, {"details": {"format": "gguf"}, "model_info": {"general.architecture": "qwen3"}}

    monkeypatch.setattr(bootstrap, "request_json", request)
    monkeypatch.setattr(bootstrap, "start_hidden", lambda *_args: pytest.fail("Existing Ollama must be reused"))
    bootstrap.ensure_local_text(local_config, configured, check=True)
    assert len(calls) == 3 and calls[-1][2] == {"model": "qwen3:4b-instruct", "verbose": False}
    assert all(key is None for _, key, _ in calls)


@pytest.mark.parametrize("metadata", [
    None, {}, {"details": None, "model_info": {"general.architecture": "qwen3"}},
    {"details": {"format": "gguf"}, "model_info": {}, "remote_host": "https://ollama.com"},
    {"details": {"format": "gguf"}, "model_info": {"general.architecture": "qwen3"}, "remote_model": "cloud"},
])
def test_unverified_model_never_starts_or_downloads(local_config, configured, monkeypatch, metadata):
    monkeypatch.setattr(bootstrap, "ollama_server_ready", lambda _: True)
    monkeypatch.setattr(bootstrap, "request_json", lambda *_args, **_kwargs: (200, metadata))
    monkeypatch.setattr(bootstrap, "start_hidden", lambda *_args: pytest.fail("No downloads or restart"))
    with pytest.raises(bootstrap.BootstrapError, match="не скачивает модели"):
        bootstrap.ensure_local_text(local_config, configured)


def test_unknown_ollama_listener_blocks_all_startup_without_takeover(local_config, configured, monkeypatch):
    monkeypatch.setattr(bootstrap, "ollama_server_ready", lambda _: False)
    monkeypatch.setattr(bootstrap, "listening", lambda port: port == 11435)
    monkeypatch.setattr(bootstrap, "core_ready", lambda *_: pytest.fail("Do not start FLUX after failed Ollama check"))
    monkeypatch.setattr(bootstrap, "start_hidden", lambda *_args: pytest.fail("No process takeover"))
    with pytest.raises(bootstrap.BootstrapError, match="cloud.disabled=true"):
        bootstrap.bootstrap(configured)


def test_local_check_only_never_starts_even_when_executable_exists(local_config, configured, monkeypatch):
    monkeypatch.setattr(bootstrap, "ollama_server_ready", lambda _: False)
    monkeypatch.setattr(bootstrap, "listening", lambda _: False)
    monkeypatch.setattr(bootstrap, "start_hidden", lambda *_args: pytest.fail("Read-only check"))
    with pytest.raises(bootstrap.BootstrapError, match="--check"):
        bootstrap.bootstrap(configured, check=True)


def test_ollama_start_uses_selected_loopback_port_isolated_env_and_no_pull(local_config, configured, monkeypatch):
    started = []
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:9999")
    monkeypatch.setenv("OLLAMA_NO_CLOUD", "0")
    monkeypatch.setattr(bootstrap, "ollama_server_ready", lambda _: bool(started))
    monkeypatch.setattr(bootstrap, "ollama_model_ready", lambda _: True)
    monkeypatch.setattr(bootstrap, "listening", lambda _: False)

    def start(command, root, name, extra=None):
        started.append((command, name, bootstrap.child_environment(extra)))
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(bootstrap, "start_hidden", start)
    bootstrap.ensure_local_text(local_config, configured)
    assert len(started) == 1
    command, name, environment = started[0]
    assert command == [str(local_config["local_text"]["executable"]), "serve"] and name == "ollama"
    assert environment["OLLAMA_HOST"] == "127.0.0.1:11435" and environment["OLLAMA_NO_CLOUD"] == "1"
    assert environment["OLLAMA_MODELS"] == str(local_config["local_text"]["models"])
    assert environment["OLLAMA_NUM_PARALLEL"] == "1" and environment["OLLAMA_DEBUG"] == "0"
    assert "TELEGRAM_BOT_TOKEN" not in environment and "IMAGE_API_KEY" not in environment
    assert os.environ["OLLAMA_HOST"] == "0.0.0.0:9999" and os.environ["OLLAMA_NO_CLOUD"] == "0"


@pytest.mark.parametrize("missing", ["executable", "models"])
def test_ollama_installation_must_already_exist(local_config, configured, monkeypatch, missing):
    local_config["local_text"][missing] = None
    monkeypatch.setattr(bootstrap, "ollama_server_ready", lambda _: False)
    monkeypatch.setattr(bootstrap, "listening", lambda _: False)
    monkeypatch.setattr(bootstrap, "start_hidden", lambda *_args: pytest.fail("No install/download"))
    with pytest.raises(bootstrap.BootstrapError, match="OLLAMA_"):
        bootstrap.ensure_local_text(local_config, configured)


def test_ollama_configured_port_cannot_overlap_private_service(configured):
    with (configured / ".env").open("a", encoding="utf-8") as stream:
        stream.write("\nAI_PROVIDER_FACTORY=app.ai.local:build_provider\nLOCAL_TEXT_API_BASE=http://127.0.0.1:8188")
    with pytest.raises(bootstrap.BootstrapError, match="отдельный локальный порт"):
        bootstrap.configuration(configured)


def test_readonly_metadata_post_has_no_credential_and_response_is_bounded(monkeypatch):
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, limit):
            assert limit == 1_048_577
            return b"{" + b" " * 1_048_576

    def open_request(request, timeout):
        requests.append(request)
        assert timeout == 5
        return Response()

    monkeypatch.setattr(bootstrap, "HTTP", SimpleNamespace(open=open_request))
    assert bootstrap.request_json("http://127.0.0.1:11434/api/show", payload={"model": "qwen3:4b-instruct"}) == (200, None)
    assert requests[0].get_method() == "POST" and json.loads(requests[0].data) == {"model": "qwen3:4b-instruct"}
    assert not requests[0].has_header("Authorization")
