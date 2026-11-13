"""Check/start installed FLUX dependencies, packaged gateway and Windows demo host.

No downloads, task scheduling, global environment writes, key creation or process
takeover. Existing listeners must prove their protocol identity before reuse.
"""

import argparse
import hmac
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


class BootstrapError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def request_json(url, key=None, *, payload=None):
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        with HTTP.open(urllib.request.Request(url, data=body, headers=headers), timeout=5) as response:
            content = response.read(1_048_577)
            return response.status, json.loads(content) if len(content) <= 1_048_576 else None
    except urllib.error.HTTPError as error:
        return error.code, None
    except (OSError, ValueError, http.client.HTTPException, RecursionError):
        return None, None


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def core_ready(port):
    path = "/system_stats" if port == 8188 else "/health"
    status, data = request_json(f"http://127.0.0.1:{port}{path}")
    if status != 200 or not isinstance(data, dict):
        return False
    if port == 8188:
        system = data.get("system")
        if not isinstance(system, dict) or not system.get("comfyui_version") or not isinstance(data.get("devices"), list):
            return False
        code, guard = request_json("http://127.0.0.1:8188/flux-safety/health")
        return code == 200 and isinstance(guard, dict) and guard.get("active") is True and guard.get("ready") is True
    return data.get("ready") is True and data.get("service") == {8190: "flux-translate", 8192: "flux-safety"}[port]


def gateway_ready(base, key):
    status, data = request_json(base.removesuffix("/v1") + "/health")
    if (status != 200 or not isinstance(data, dict) or data.get("service") != "flux-image-api"
            or data.get("ready") is not True or data.get("configured") is not True):
        return False
    # Refuse a listener which merely returns a matching public health document.
    if request_json(base + "/models")[0] != 401:
        return False
    status, models = request_json(base + "/models", key)
    return status == 200 and isinstance(models, dict) and isinstance(models.get("data"), list) and any(
        isinstance(item, dict) and item.get("id") == "flux1-dev" for item in models["data"]
    )


def local_text_configuration(values, root):
    if values.get("AI_PROVIDER_FACTORY") != "app.ai.local:build_provider":
        return None
    from app.ai.local import LocalAISettings
    from app.shared.errors import InvalidInput

    try:
        settings = LocalAISettings.from_env(values)
    except InvalidInput:
        raise BootstrapError("Проверьте LOCAL_TEXT_*: разрешён только локальный Ollama на 127.0.0.1.") from None
    return {
        "settings": settings, "port": urlsplit(settings.base_url).port,
        "executable": (root / values["OLLAMA_EXE_PATH"]).resolve() if values.get("OLLAMA_EXE_PATH") else None,
        "models": (root / values["OLLAMA_MODELS_PATH"]).resolve() if values.get("OLLAMA_MODELS_PATH") else None,
    }


def ollama_server_ready(settings):
    status, version = request_json(settings.base_url + "/api/version")
    if status != 200 or not isinstance(version, dict) or not isinstance(version.get("version"), str):
        return False
    if not version["version"].strip():
        return False
    status, info = request_json(settings.base_url + "/api/status")
    return (
        status == 200 and isinstance(info, dict) and isinstance(info.get("cloud"), dict)
        and info["cloud"].get("disabled") is True
    )


def ollama_model_ready(settings):
    status, model = request_json(settings.base_url + "/api/show", payload={"model": settings.model, "verbose": False})
    return (
        status == 200 and isinstance(model, dict) and not model.get("error")
        and not model.get("remote_host") and not model.get("remote_model")
        and isinstance(model.get("details"), dict) and model["details"].get("format") in ("gguf", "safetensors")
        and isinstance(model.get("model_info"), dict) and bool(model["model_info"])
    )


def ensure_local_text(config, root, *, check=False):
    local = config.get("local_text")
    if local is None:
        return
    settings = local["settings"]
    if not ollama_server_ready(settings):
        if listening(local["port"]):
            raise BootstrapError(
                "Порт Ollama занят, но локальный сервер с cloud.disabled=true не подтверждён. "
                "Чужой или облачный сервер не будет использован или перезапущен."
            )
        if check:
            raise BootstrapError("Локальный Ollama не готов. --check ничего не запускает и не скачивает.")
        executable, models = local["executable"], local["models"]
        if executable is None or not executable.is_file() or executable.name.lower() != "ollama.exe":
            raise BootstrapError("Укажите OLLAMA_EXE_PATH к заранее установленному ollama.exe.")
        if models is None or not models.is_dir():
            raise BootstrapError("Укажите OLLAMA_MODELS_PATH к существующему каталогу локальных весов.")
        environment = {
            "OLLAMA_HOST": f"127.0.0.1:{local['port']}", "OLLAMA_MODELS": str(models),
            "OLLAMA_NO_CLOUD": "1", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1",
            "OLLAMA_DEBUG": "0",
        }
        child = start_hidden([str(executable), "serve"], root, "ollama", environment)
        wait_ready(lambda: ollama_server_ready(settings), child, 30, "Локальный Ollama")
    if not ollama_model_ready(settings):
        raise BootstrapError(
            "Выбранная LOCAL_TEXT_MODEL не подтверждена как установленная локальная модель. "
            "Подготовьте веса заранее: bootstrap не скачивает модели и не использует облако."
        )
    print("Ollama подтвердил отключение облака и наличие выбранной локальной модели.", flush=True)


def configuration(root):
    from dotenv import dotenv_values

    path = root / ".env"
    if not path.is_file() or path.is_symlink():
        raise BootstrapError("Нужен обычный локальный .env в корне проекта.")
    values = {key: value for key, value in dotenv_values(path, interpolate=False).items() if value is not None}
    # Match backend precedence without placing any secrets in global os.environ.
    values.update({key: value for key, value in os.environ.items() if key in values})
    port = values.get("FLUX_GATEWAY_PORT", "8194")
    app_port = values.get("MINI_APP_PORT", "8766")
    if not port.isascii() or not port.isdigit() or not 1024 <= int(port) <= 65535:
        raise BootstrapError("FLUX_GATEWAY_PORT должен быть числом 1024–65535.")
    if not app_port.isascii() or not app_port.isdigit() or not 1024 <= int(app_port) <= 65535:
        raise BootstrapError("MINI_APP_PORT должен быть числом 1024–65535.")
    if int(port) in {8188, 8190, 8192, 8193, int(app_port)}:
        raise BootstrapError("Packaged gateway нужен отдельный порт, например 8194; старый 8193 не используется.")
    image_base = values.get("IMAGE_API_BASE", "").rstrip("/")
    moderation_base = values.get("MODERATION_API_BASE", "").rstrip("/")
    key = values.get("IMAGE_API_KEY", "")
    moderation_key = values.get("MODERATION_API_KEY", "")
    if (image_base != moderation_base or not key.isascii() or len(key) < 32 or any(c.isspace() for c in key)
            or not hmac.compare_digest(key, moderation_key) or values.get("IMAGE_MODEL") != "flux1-dev"):
        raise BootstrapError("IMAGE/MODERATION_API_BASE и ключи должны совпадать; IMAGE_MODEL=flux1-dev.")
    parsed = urlsplit(image_base)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.path.endswith("/v1")
            or not ((parsed.scheme == "https" and (parsed.hostname or "").endswith(".ts.net"))
                    or (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}))):
        raise BootstrapError("Укажите приватный HTTPS Tailscale или loopback IMAGE_API_BASE с окончанием /v1.")
    hostname = values.get("FLUX_GATEWAY_HOSTNAME", parsed.hostname if parsed.scheme == "https" else "")
    prefix = values.get("FLUX_GATEWAY_PREFIX", parsed.path.removesuffix("/v1"))
    gateway_env = {"FLUX_GATEWAY_PORT": port, "FLUX_GATEWAY_HOSTNAME": hostname, "FLUX_GATEWAY_PREFIX": prefix}
    key_file = values.get("FLUX_GATEWAY_KEY_FILE", "")
    if key_file:
        gateway_env["FLUX_GATEWAY_KEY_FILE"] = str((root / key_file).resolve())
    starter = values.get("FLUX_STARTER_PATH", "")
    if starter:
        candidate = (root / starter).resolve()
        if candidate.is_dir():
            candidate = candidate / "start_flux.py"
        elif candidate.suffix.lower() == ".cmd":
            candidate = candidate.parent / "Flux_4070Ti_Starter" / "start_flux.py"
        starter = candidate
    local_text = local_text_configuration(values, root)
    if local_text and local_text["port"] in {8188, 8190, 8192, 8193, int(port), int(app_port)}:
        raise BootstrapError("Ollama нужен отдельный локальный порт, по умолчанию 11434.")
    return {"port": int(port), "app_port": int(app_port), "base": image_base, "key": key, "local_text": local_text,
            "starter": starter, "gateway_env": gateway_env, "app_env": values}


def child_environment(extra=None):
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "USERPROFILE", "HOME", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP",
               "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_HOME",
               "SYSTEMDRIVE", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "USERNAME", "USERDOMAIN",
               "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
               "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS", "PATHEXT"}
    return {**{k: v for k, v in os.environ.items() if k.upper() in allowed}, "PYTHONUTF8": "1", **(extra or {})}


def application_environment(config, root):
    values = dict(config["app_env"])
    if values.get("AI_PROVIDER_FACTORY") == "app.ai.chatgpt:build_provider":
        supplied = values.get("CODEX_CLI_PATH")
        executable = (root / supplied).resolve() if supplied else None
        if executable is None:
            found = shutil.which("codex.exe", path=values.get("PATH", os.environ.get("PATH", "")))
            executable = Path(found) if found else None
        if executable is None or executable.name.lower() != "codex.exe" or not executable.is_file():
            raise BootstrapError("Нужен native codex.exe. Укажите его существующий путь в CODEX_CLI_PATH локального .env.")
        values["PATH"] = str(executable.parent) + os.pathsep + values.get("PATH", os.environ.get("PATH", ""))
    return values


def start_hidden(command, root, name, extra=None):
    logs = root / "var" / "windows-run"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / (name + ".log")).open("ab") as stream:
        return subprocess.Popen(
            command, cwd=root, env=child_environment(extra), stdin=subprocess.DEVNULL,
            stdout=stream, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True,
        )


def wait_ready(check, child, timeout, label):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        if child.poll() is not None:
            raise BootstrapError(f"{label} завершился до готовности. Проверьте var/windows-run; чужие процессы не остановлены.")
        time.sleep(1)
    raise BootstrapError(f"{label} не подтвердил готовность за отведённое время. Процессы не остановлены.")


def bootstrap(root, *, check=False, dependencies_only=False, foreground=False):
    from app.core.instance_lock import InstanceLock

    config = configuration(root)
    app_env = None if dependencies_only else application_environment(config, root)
    with InstanceLock(root / "var/windows-run/bootstrap.lock"):
        ensure_local_text(config, root, check=check)
        ready = {port: core_ready(port) for port in (8188, 8190, 8192)}
        for port, valid in ready.items():
            if not valid and listening(port):
                raise BootstrapError(f"Порт {port} занят, но нужный FLUX-компонент не подтвердил готовность. Он не будет заменён.")
        if not all(ready.values()):
            if check:
                raise BootstrapError("FLUX/переводчик/модератор ещё не готовы. --check ничего не запускает.")
            starter = config["starter"]
            if not isinstance(starter, Path) or not starter.is_file() or starter.suffix.lower() != ".py":
                raise BootstrapError("Укажите FLUX_STARTER_PATH к существующему start_flux.py или START_FLUX.cmd.")
            print("Запускаю установленный FLUX starter без браузера…", flush=True)
            child = start_hidden([sys.executable, "-u", str(starter), "--no-browser"], root, "flux-startup")
            wait_ready(lambda: all(core_ready(p) for p in (8188, 8190, 8192)), child, 300, "FLUX")
        print("ComfyUI, guard, переводчик и модератор подтвердили готовность.", flush=True)
        local = f"http://127.0.0.1:{config['port']}/v1"
        if not gateway_ready(local, config["key"]):
            if listening(config["port"]):
                raise BootstrapError("Порт gateway занят, но identity/readiness/key не подтверждены. Перезапуск запрещён.")
            if check:
                raise BootstrapError("Packaged gateway не запущен. --check ничего не запускает.")
            script = root / "services/flux_gateway/image_api/image_api_server.py"
            key_file = Path(config["gateway_env"].get("FLUX_GATEWAY_KEY_FILE", script.parent / "api_key.txt"))
            try:
                stored = key_file.read_text(encoding="ascii").strip()
            except (OSError, UnicodeError):
                raise BootstrapError("Укажите FLUX_GATEWAY_KEY_FILE с существующим ключом; ключи не создаются и не копируются.") from None
            if not hmac.compare_digest(stored, config["key"]):
                raise BootstrapError("Файл ключа gateway не совпадает с IMAGE_API_KEY; процесс не запущен.")
            child = start_hidden([sys.executable, "-u", "-B", str(script)], root, "flux-gateway", config["gateway_env"])
            wait_ready(lambda: gateway_ready(local, config["key"]), child, 30, "Gateway")
        if not gateway_ready(config["base"], config["key"]):
            raise BootstrapError("Приватный API-адрес не подтвердил identity/key. Проверьте Tailscale и сохранённый Serve path.")
        print("Packaged gateway и приватный API подтвердили ключ и готовность.", flush=True)
        if check or dependencies_only:
            return
        if listening(config["app_port"]):
            raise BootstrapError("Порт Mini App уже занят. Новый бот не запущен; работающий процесс не остановлен.")
        command = [sys.executable, "-u", str(root / "scripts/run-windows.py")]
        if foreground:
            subprocess.run(command, cwd=root, env=child_environment(app_env), check=True)
        else:
            child = start_hidden(command, root, "demo-host", app_env)
            print(f"Windows launcher запущен, PID {child.pid}. Готовность и URL: var/windows-run/state.json; лог demo-host.log.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Only identity/readiness checks; start nothing")
    parser.add_argument("--dependencies-only", action="store_true", help="Do not start the Telegram runtime")
    parser.add_argument("--foreground", action="store_true", help="Run the final Windows host in this console")
    args = parser.parse_args()
    if os.name != "nt":
        print("Этот bootstrap предназначен для Windows.")
        return 1
    try:
        bootstrap(REPO, check=args.check, dependencies_only=args.dependencies_only, foreground=args.foreground)
    except BootstrapError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Запуск не завершён ({type(error).__name__}); секреты и значения конфигурации скрыты.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
