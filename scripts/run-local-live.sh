#!/usr/bin/env bash
# Starts the existing local Telegram configuration. The HTTPS tunnel is a separate process.
set -euo pipefail

studio_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$studio_root"
studio_check=0
studio_without_ai=0
for studio_arg in "$@"; do
  case "$studio_arg" in
    --check) studio_check=1 ;;
    --without-ai) studio_without_ai=1 ;;
    *) printf '%s\n' 'Использование: scripts/run-local-live.sh [--check] [--without-ai]' >&2; exit 2 ;;
  esac
done

if [[ -n "${PYTHON_EXECUTABLE:-}" ]]; then
  studio_python="$PYTHON_EXECUTABLE"
elif [[ -x "$studio_root/.venv/bin/python" ]]; then
  studio_python="$studio_root/.venv/bin/python"
elif [[ -x "$studio_root/../venv/bin/python" ]]; then
  studio_python="$studio_root/../venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  studio_python="$(command -v python3)"
else
  printf '%s\n' 'Нужен Python 3.11+ и зависимости проекта. См. docs/LOCAL_RUN.md.' >&2
  exit 1
fi

# .env is parsed as data, never sourced as shell code. Secrets stay out of argv and output.
exec "$studio_python" - "$studio_root" "$studio_check" "$studio_without_ai" <<'PY'
import os
import stat
import sys
from pathlib import Path


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


repo = Path(sys.argv[1])
check_only = sys.argv[2] == "1"
without_ai = sys.argv[3] == "1"
env_file = repo / ".env"
if sys.version_info < (3, 11):
    fail("Нужен Python 3.11 или новее.")
if not env_file.is_file() or env_file.is_symlink():
    fail("Нужен существующий локальный .env — обычный файл, не ссылка. См. docs/LOCAL_RUN.md.")
if stat.S_IMODE(env_file.stat().st_mode) & 0o077:
    fail("Закройте доступ к конфигурации командой chmod 600 .env и повторите запуск.")
try:
    from dotenv import dotenv_values
    from app.config import Settings
    import aiogram  # noqa: F401
    import aiohttp  # noqa: F401
    from PIL import Image  # noqa: F401
except ImportError:
    fail("Не установлены зависимости. Установите requirements.txt в выбранное виртуальное окружение.")

try:
    values = dotenv_values(env_file, interpolate=False)
    if values.get("APP_MODE") != "live":
        fail("Этот скрипт работает только с APP_MODE=live в локальном .env.")
    if not values.get("MINI_APP_URL", "").strip():
        fail("Сначала запустите HTTPS-туннель и укажите его актуальный адрес /mini/ в MINI_APP_URL.")
    # Explicit file values override stale shell values; empty keys remain intentionally empty.
    for key, value in values.items():
        if value is not None:
            os.environ[key] = value
    if without_ai:
        os.environ.update({
            "AI_API_KEY": "", "TEXT_API_KEY": "", "IMAGE_API_KEY": "",
            "AI_PROVIDER_FACTORY": "app.ai.provider:build_provider",
            "IMAGE_PROVIDER_FACTORY": "app.media.runtime:build_provider",
        })
    settings = Settings.from_env(env_file)
    if not settings.mini_app_url or settings.bot_factory != "app.bot.adapter:build_bot":
        fail("Проверьте MINI_APP_URL и BOT_FACTORY в локальном .env.")
except SystemExit:
    raise
except Exception:
    # Validation errors can embed input values, including tokens. Never print them.
    fail("Некорректная локальная конфигурация. Проверьте обязательные поля .env по инструкции.")

if check_only:
    print("Локальная конфигурация корректна. Сеть, токен и HTTPS-туннель этой командой не проверялись.")
    raise SystemExit(0)
print("Запускаются Telegram-бот и Mini App. HTTPS-туннель должен работать в отдельном окне.", flush=True)
if without_ai:
    print("Ручной режим: нейросеть отключена флагом --without-ai; файл .env не изменён.", flush=True)
os.execv(sys.executable, [sys.executable, "-m", "app", "run", "--env-file", str(env_file)])
PY
