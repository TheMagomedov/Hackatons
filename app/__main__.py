"""CLI with offline diagnostics and explicit legacy-data adoption."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from app.config import Settings
from app.core.demo import run_demo
from app.core.instance_lock import InstanceLock
from app.core.runtime import build_runtime, run_application
from app.shared.errors import AppError, InvalidInput, NotFound
from app.storage import Repository


async def execute(args) -> None:
    if args.command in {"backup", "verify-backup", "restore"}:
        from app.ops.backup import backup_data, restore_data, verify_backup

        if args.command == "backup":
            if not args.data_dir or not args.output:
                raise InvalidInput("Для backup укажите --data-dir и новый --output.")
            result = backup_data(args.data_dir, args.output)
        elif args.command == "verify-backup":
            if not args.backup_dir:
                raise InvalidInput("Укажите --backup-dir.")
            result = verify_backup(args.backup_dir)
        else:
            if not args.backup_dir or not args.data_dir:
                raise InvalidInput("Для restore укажите --backup-dir и новый --data-dir.")
            result = restore_data(args.backup_dir, args.data_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "demo":
        destination = Path(args.data_dir or "var") / f"demo-{uuid4().hex[:12]}"
        with InstanceLock(destination / "backend.lock"):
            result = await run_demo(destination)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command in {"status", "adopt-data"}:
        # Operational commands deliberately do not load API keys or provider factories.
        # DATA_DIR must be explicit: an operator should never inspect/adopt the wrong database.
        if not args.data_dir:
            raise InvalidInput("Укажите --data-dir с существующей базой.")
        destination = Path(args.data_dir)
        db_path = destination / "backend.sqlite3"
        if not db_path.is_file():
            raise NotFound("В выбранном DATA_DIR нет backend.sqlite3.")
        repository = Repository(db_path)
        try:
            if args.command == "status":
                # diagnostics() is read-only and can inspect an unbound schema-v1 database.
                print(json.dumps(repository.diagnostics(), ensure_ascii=False, indent=2))
            else:
                if args.mode is None or args.owner_id is None:
                    raise InvalidInput("Для adopt-data явно укажите --mode и --owner-id.")
                with InstanceLock(destination / "backend.lock"):
                    repository.initialize()
                    repository.bind_runtime(args.mode, args.owner_id, adopt_legacy=True)
                print(json.dumps({"binding": repository.runtime_binding(), "external_requests": 0}, indent=2))
        finally:
            repository.close()
        return
    settings = Settings.from_env(args.env_file)
    if args.data_dir:
        settings = Settings.model_validate({**settings.model_dump(), "data_dir": Path(args.data_dir)})
    if args.command == "init":
        with InstanceLock(settings.data_dir / "backend.lock"):
            repo = Repository(settings.data_dir / "backend.sqlite3")
            try:
                repo.initialize()
                repo.bind_runtime(settings.mode, settings.owner_id)
            finally:
                repo.close()
        print("Database initialized. No external requests were made.")
    elif args.command == "check":
        with InstanceLock(settings.data_dir / "backend.lock"):
            runtime = await build_runtime(settings)
            await runtime.close()
        print(f"Configuration and adapters loaded: mode={settings.mode}. Provider access is not live-tested.")
    else:
        await run_application(settings, once=args.once)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backend автономного AI-маркетолога. По умолчанию — офлайн-демо."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "demo",
            "init",
            "check",
            "run",
            "status",
            "adopt-data",
            "backup",
            "verify-backup",
            "restore",
        ),
        default="demo",
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--output", help="Новый каталог резервной копии для backup")
    parser.add_argument("--backup-dir", help="Существующая копия для verify-backup/restore")
    parser.add_argument("--once", action="store_true", help="Один цикл backend для команды run")
    parser.add_argument("--mode", choices=("demo", "live"), help="Режим существующих данных для adopt-data")
    parser.add_argument("--owner-id", type=int, help="Владелец существующих данных для adopt-data")
    args = parser.parse_args()
    if args.command != "adopt-data" and (args.mode is not None or args.owner_id is not None):
        parser.error("--mode и --owner-id используются только с adopt-data")
    if args.once and args.command != "run":
        parser.error("--once используется только с run")
    if args.output and args.command != "backup":
        parser.error("--output используется только с backup")
    if args.backup_dir and args.command not in {"verify-backup", "restore"}:
        parser.error("--backup-dir используется только с verify-backup/restore")
    if args.data_dir and args.command == "verify-backup":
        parser.error("Для verify-backup используется --backup-dir")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        asyncio.run(execute(args))
    except AppError as exc:
        print(f"Error [{exc.code}]: {exc}", file=sys.stderr)
        return 1
    except ValidationError:
        # Pydantic input values may contain credentials. Never dump a config error.
        print(
            "Invalid configuration or adapter contract. Check .env.example and integration documentation.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(
            f"Startup/operation failed ({type(exc).__name__}); provider details are intentionally hidden.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
