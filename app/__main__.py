"""CLI: python -m app [demo|init|check|run]. No live requests on default launch."""

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
from app.shared.errors import AppError
from app.storage import Repository


async def execute(args) -> None:
    if args.command == "demo":
        destination = Path(args.data_dir or "var") / f"demo-{uuid4().hex[:12]}"
        with InstanceLock(destination / "backend.lock"):
            result = await run_demo(destination)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    settings = Settings.from_env(args.env_file)
    if args.data_dir:
        settings = Settings.model_validate({**settings.model_dump(), "data_dir": Path(args.data_dir)})
    if args.command == "init":
        with InstanceLock(settings.data_dir / "backend.lock"):
            repo = Repository(settings.data_dir / "backend.sqlite3")
            repo.initialize()
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
    parser.add_argument("command", nargs="?", choices=("demo", "init", "check", "run"), default="demo")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--once", action="store_true", help="Один цикл backend для команды run")
    args = parser.parse_args()
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
