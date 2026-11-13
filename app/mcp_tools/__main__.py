"""Local MCP process: existing data only, no providers, scheduler, bot or HTTP server."""

import argparse
import asyncio
import logging
import os
import sys

from app.ai.resilience import InferenceStore
from app.config import Settings
from app.core.service import BackendService
from app.knowledge import KnowledgeIndex
from app.shared.errors import InvalidInput, ProviderUnavailable
from app.storage import FileStore, Repository

from .dispatcher import ToolDispatcher
from .server import MCPServer, serve_stdio


class DisabledAdapter:
    """A protocol tool cannot accidentally gain an outbound provider by refactoring."""

    def __getattr__(self, _name):
        def unavailable(*_args, **_kwargs):
            raise ProviderUnavailable("MCP-процесс не вызывает внешние провайдеры.")
        return unavailable


def build_dispatcher(settings, *, business_ids, actor_id=None, allow_prepare=False):
    path = settings.data_dir / "backend.sqlite3"
    if not path.is_file() or path.is_symlink():
        raise InvalidInput("MCP требует существующую привязанную базу проекта.")
    repository = Repository(path)
    knowledge = None
    telemetry = None
    try:
        if repository.runtime_binding() != {"mode": settings.mode, "owner_id": settings.owner_id}:
            raise InvalidInput("База не соответствует выбранному владельцу и режиму.")
        assets = settings.data_dir / "assets"
        if not assets.is_dir():
            raise InvalidInput("Хранилище файлов проекта не найдено.")
        disabled = DisabledAdapter()
        knowledge = KnowledgeIndex(settings.data_dir / "knowledge.sqlite3")
        telemetry = InferenceStore(settings.data_dir / "ai-runtime.sqlite3",
                                   ttl_seconds=settings.ai_cache_ttl_seconds)
        service = BackendService(
            repository, FileStore(assets, max_bytes=settings.max_upload_bytes), settings,
            disabled, disabled, disabled, disabled, knowledge=knowledge, telemetry=telemetry,
        )
        dispatcher = ToolDispatcher(
            service, settings.owner_id if actor_id is None else actor_id,
            business_ids=business_ids, allow_prepare=allow_prepare,
        )
        dispatcher.list_tools()
        for bid in business_ids:
            dispatcher._business(bid)
        return dispatcher
    except BaseException:
        if telemetry is not None:
            telemetry.close()
        if knowledge is not None:
            knowledge.close()
        repository.close()
        raise


async def run(args):
    settings = Settings.from_env(args.env_file)
    raw_actor = os.environ.get("MCP_ACTOR_ID", "").strip()
    if raw_actor and (not raw_actor.isascii() or not raw_actor.isdigit()):
        raise InvalidInput("MCP_ACTOR_ID должен быть положительным Telegram ID из локальной конфигурации.")
    dispatcher = build_dispatcher(
        settings, business_ids=set(args.business_id), actor_id=int(raw_actor) if raw_actor else None,
        allow_prepare=args.allow_prepare,
    )
    try:
        await serve_stdio(MCPServer(dispatcher), sys.stdin.buffer, sys.stdout.buffer)
    finally:
        dispatcher.service.telemetry.close()
        dispatcher.service.knowledge.close()
        dispatcher.service.repo.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--business-id", required=True, action="append", help="Repeat for each permitted company")
    parser.add_argument("--allow-prepare", action="store_true", help="Allow durable draft requests for operators")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("MCP startup/operation failed; check local configuration and access. Details hidden.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
