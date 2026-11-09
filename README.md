# Автономный AI-маркетолог

Telegram-бот для изучения бизнеса, подготовки материалов и публикации по расписанию. Утверждение контент-плана разрешает подготовку черновиков. Для каждой версии поста требуется отдельное согласование.

## Запуск каркаса

Нужен Python 3.12. Из корня репозитория в PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m app --check
```

В Linux и macOS используйте `.venv/bin/python` и `cp .env.example .env`.

Общие структуры хранятся в `app/shared/models.py`, интерфейсы модулей в `app/shared/ports.py`. Конфигурация читается из окружения и локального `.env`. Рабочая база и файлы будут храниться в `var/`.
