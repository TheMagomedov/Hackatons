"""Check community credentials without starting a second polling consumer."""

import argparse
import asyncio
import json
import logging

from dotenv import dotenv_values

from app.shared.errors import AppError, InvalidInput
from app.vk.client import VKClient


async def check(config, *, configure=False):
    token = config.get("VK_GROUP_TOKEN", "")
    group = config.get("VK_GROUP_ID", "")
    if not token or not group or not group.isdigit() or int(group) <= 0:
        raise InvalidInput("В локальном .env нужны VK_GROUP_ID и VK_GROUP_TOKEN.")
    client = VKClient(token, int(group))
    try:
        identity = await client.verify()
        if configure:
            await client.call("groups.setLongPollSettings", group_id=int(group), enabled=1,
                              api_version="5.199", message_new=1)
        await client.poll_server()
        return {"status": "ok", **identity,
                "bot_chat": "https://vk.com/im?sel=-" + group, "polling_started": False}
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description="Проверка ключа сообщества ВК без отправки сообщений")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--configure-long-poll", action="store_true",
                        help="Включить Bot Long Poll и событие message_new в указанном сообществе")
    args = parser.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        result = asyncio.run(check(dotenv_values(args.env_file), configure=args.configure_long_poll))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except AppError as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
