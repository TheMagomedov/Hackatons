"""Local workspace-access administration; never loads env/providers or calls Telegram."""

import argparse
import functools
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.shared.errors import AppError, InvalidInput


def actor_action(function):
    """Record the real Telegram actor without substituting the workspace owner."""
    def begin(service, actor, args):
        service.require_access(actor, write=True)
        target = args[0] if args and isinstance(args[0], str) else "workspace"
        service.repo.record_actor_action(actor, function.__name__ + ":requested", target, service.now())
        return target

    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def asynchronous(service, actor_id, *args, **kwargs):
            target = begin(service, actor_id, args)
            result = await function(service, actor_id, *args, **kwargs)
            service.repo.record_actor_action(actor_id, function.__name__ + ":completed", target, service.now())
            return result
        return asynchronous

    @functools.wraps(function)
    def synchronous(service, actor_id, *args, **kwargs):
        target = begin(service, actor_id, args)
        result = function(service, actor_id, *args, **kwargs)
        service.repo.record_actor_action(actor_id, function.__name__ + ":completed", target, service.now())
        return result
    return synchronous


def main():
    parser = argparse.ArgumentParser(description="Локальные приглашения в командное пространство")
    parser.add_argument("--data-dir", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-invite")
    create.add_argument("--role", required=True, choices=("operator", "reviewer"))
    create.add_argument("--max-uses", type=int, default=1)
    create.add_argument("--hours", type=int, default=168)
    create.add_argument("--bot-username")
    commands.add_parser("list")
    commands.add_parser("revoke-member").add_argument("--actor-id", type=int, required=True)
    commands.add_parser("revoke-invite").add_argument("--invite-id", required=True)
    args = parser.parse_args()
    from app.storage import Repository

    path = Path(args.data_dir) / "backend.sqlite3"
    if not path.is_file():
        parser.error("В выбранном каталоге нет backend.sqlite3")
    repo = Repository(path)
    try:
        repo.initialize()
        binding = repo.runtime_binding()
        if binding is None:
            raise InvalidInput("Сначала привяжите базу к режиму и владельцу штатной командой init/adopt-data.")
        owner, now = binding["owner_id"], datetime.now(timezone.utc)
        if args.command == "create-invite":
            if not 1 <= args.hours <= 720:
                raise InvalidInput("Срок приглашения от 1 до 720 часов.")
            if args.bot_username:
                import re
                if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", args.bot_username):
                    raise InvalidInput("Укажите username бота без @.")
            result = repo.create_access_invite(owner, args.role, args.max_uses, now + timedelta(hours=args.hours), now)
            # The raw token is shown once to this local administrator, never persisted/logged.
            result["start_parameter"] = "join_" + result.pop("token")
            if args.bot_username:
                result["url"] = f"https://t.me/{args.bot_username}?start={result['start_parameter']}"
        elif args.command == "revoke-member":
            repo.revoke_access_member(owner, args.actor_id, now)
            result = {"revoked": True, "actor_id": args.actor_id}
        elif args.command == "revoke-invite":
            repo.revoke_access_invite(owner, args.invite_id, now)
            result = {"revoked": True, "invite_id": args.invite_id}
        else:
            result = repo.list_access(owner)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except AppError as error:
        print(f"Ошибка: {error}")
        return 1
    finally:
        repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
