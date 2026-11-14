"""Official community Bot Long Poll + VK API; no personal-account credentials."""

import hashlib
import json
import logging
import re
from urllib.parse import urlsplit

import httpx

from app.shared.errors import InvalidInput, ProviderUnavailable


class _CredentialFilter(logging.Filter):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def filter(self, record):
        message = record.getMessage().replace(self.token, "[REDACTED]")
        message = re.sub(r"([?&]key=)[^&\s'\"]+", r"\1[REDACTED]", message)
        record.msg, record.args = message, ()
        return True


class VKClient:
    def __init__(self, token: str, group_id: int, *, transport=None):
        self.token, self.group_id = token, group_id
        self.http = httpx.AsyncClient(timeout=35, follow_redirects=False, transport=transport)
        self.log_filter = _CredentialFilter(token)
        self.loggers = [logging.getLogger(name) for name in (
            "httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2",
            "httpcore.proxy", "httpcore.socks",
        )]
        for logger in self.loggers:
            logger.addFilter(self.log_filter)

    async def close(self):
        await self.http.aclose()
        for logger in self.loggers:
            logger.removeFilter(self.log_filter)

    async def call(self, method: str, **params):
        try:
            response = await self.http.post("https://api.vk.com/method/" + method,
                                            data={**params, "access_token": self.token, "v": "5.199"})
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or "error" in result or "response" not in result:
                raise ValueError
            return result["response"]
        except (httpx.HTTPError, ValueError, TypeError):
            # VK errors can echo request_params, including the access token.
            raise ProviderUnavailable("ВК отклонил запрос или недоступен. Проверьте ключ и права сообщества.") from None

    async def poll_server(self) -> dict:
        result = await self.call("groups.getLongPollServer", group_id=self.group_id)
        try:
            parts = urlsplit(result["server"])
            allowed = any(parts.hostname and parts.hostname.endswith("." + host)
                          for host in ("vk.com", "vk.ru", "vkuser.net"))
            if (parts.scheme != "https" or not allowed or parts.port not in (None, 443)
                    or parts.username or parts.password or parts.query or parts.fragment
                    or not isinstance(result["key"], str) or not str(result["ts"]).isdigit()):
                raise ValueError
            return result
        except (ValueError, KeyError, TypeError):
            raise ProviderUnavailable("ВК вернул некорректный сервер Bot Long Poll.") from None

    async def verify(self) -> dict:
        response = await self.call("groups.getById")
        groups = response.get("groups", []) if isinstance(response, dict) else response
        if (not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict)
                or groups[0].get("id") != self.group_id):
            raise InvalidInput("Ключ не подтвердил указанное сообщество. Нужен ключ именно этого сообщества.")
        permissions = await self.call("groups.getTokenPermissions")
        if (not isinstance(permissions, dict) or "messages" not in {
            item.get("name") for item in permissions.get("permissions", []) if isinstance(item, dict)
        }):
            raise InvalidInput("У ключа нет права на сообщения сообщества.")
        return {"group_id": self.group_id, "name": groups[0].get("name", "")}

    async def poll(self, server: dict) -> dict:
        try:
            response = await self.http.get(server["server"], params={
                "act": "a_check", "key": server["key"], "ts": server["ts"], "wait": 25,
            })
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (httpx.HTTPError, ValueError):
            raise ProviderUnavailable("Не удалось получить события ВК.") from None

    async def send(self, user_id: int, text: str, event_key: str, studio_url: str | None = None):
        rows = [[{"action": {"type": "text", "label": label}, "color": "secondary"}
                 for label in ("Меню", "Посты", "Задания")]]
        if studio_url:
            rows.insert(0, [{"action": {"type": "open_link", "label": "Открыть студию", "link": studio_url}}])
        # Stable random_id gives VK its native protection against duplicate responses.
        random_id = int.from_bytes(hashlib.sha256(event_key.encode()).digest()[:4], "big") & 0x7fffffff or 1
        await self.call("messages.send", peer_id=user_id, message=text[:4000], random_id=random_id,
                        dont_parse_links=1, keyboard=json.dumps({"inline": True, "buttons": rows}, ensure_ascii=False))
