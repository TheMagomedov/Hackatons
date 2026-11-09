"""Telegram delivery without implicit retries or unsafe provider error messages."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from pydantic import SecretStr

from app.shared.errors import (
    DeliveryRejected,
    DeliveryUnknown,
    Forbidden,
    InvalidInput,
    ProviderUnavailable,
)
from app.shared.models import ChannelInfo, Notification, PostView, PublicationResult

_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z")
_TOKEN = re.compile(r"[0-9]+:[A-Za-z0-9_-]+\Z")
_CHANNEL = re.compile(r"-100[0-9]+\Z")
_MAX_IMAGE_BYTES = 10_000_000


class _TokenLogFilter(logging.Filter):
    """httpx logs request URLs at INFO; redact our credential before formatting."""

    def __init__(self, token: SecretStr) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        secret = self._token.get_secret_value()
        message = record.getMessage()
        if secret in message:
            record.msg = message.replace(secret, "[REDACTED]")
            record.args = ()
        return True


def _positive_id(value: Any) -> bool:
    return type(value) is int and 0 < value < 2**52


def _canonical_channel(value: Any) -> str:
    if type(value) is not int or not (-(2**52) < value < 0):
        raise ValueError("Invalid channel identifier")
    result = str(value)
    if not _CHANNEL.fullmatch(result) or int(result[4:]) <= 0:
        raise ValueError("Invalid channel identifier")
    return result


def _channel_argument(value: str, *, canonical_only: bool = False) -> str:
    if not isinstance(value, str):
        raise InvalidInput("A valid Telegram channel identifier is required.")
    if value.startswith("@") and not canonical_only and _USERNAME.fullmatch(value[1:]):
        return value
    if _CHANNEL.fullmatch(value):
        try:
            return _canonical_channel(int(value))
        except ValueError:
            pass
    raise InvalidInput("A valid Telegram channel identifier is required.")


def _plain_text_valid(value: str, codepoints: int, utf16_units: int) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > codepoints:
        return False
    try:
        return len(value.encode("utf-16-le")) // 2 <= utf16_units
    except UnicodeEncodeError:
        return False


class _TelegramClient:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise InvalidInput("A valid Telegram bot token is required.")
        self._token = SecretStr(token)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False
        )
        self._closed = False
        self._log_filter = _TokenLogFilter(self._token)
        self._loggers = tuple(
            logging.getLogger(name)
            for name in (
                "httpx",
                "httpcore",
                "httpcore.connection",
                "httpcore.http11",
                "httpcore.http2",
                "httpcore.proxy",
                "httpcore.socks",
            )
        )
        for logger in self._loggers:
            logger.addFilter(self._log_filter)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(token=[REDACTED], closed={self._closed})"

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()
        for logger in self._loggers:
            logger.removeFilter(self._log_filter)

    async def _call(
        self,
        method: str,
        data: dict[str, Any],
        *,
        delivery: bool = False,
        files: dict[str, Any] | None = None,
    ) -> Any:
        if self._closed:
            if delivery:
                raise DeliveryRejected("Telegram adapter is closed.")
            raise ProviderUnavailable("Telegram adapter is closed.")
        url = f"https://api.telegram.org/bot{self._token.get_secret_value()}/{method}"
        try:
            if files is None:
                response = await self._client.post(url, json=data, follow_redirects=False)
            else:
                response = await self._client.post(url, data=data, files=files, follow_redirects=False)
        except (httpx.HTTPError, RuntimeError):
            if delivery:
                raise DeliveryUnknown("Telegram delivery outcome is unknown; do not retry blindly.") from None
            raise ProviderUnavailable("Telegram is temporarily unavailable.") from None

        if 400 <= response.status_code < 500:
            if delivery:
                raise DeliveryRejected("Telegram explicitly rejected the request.")
            raise ProviderUnavailable("Telegram rejected the request.")
        if not 200 <= response.status_code < 300:
            if delivery:
                raise DeliveryUnknown("Telegram delivery outcome is unknown; do not retry blindly.")
            raise ProviderUnavailable("Telegram is temporarily unavailable.")
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            if delivery:
                raise DeliveryUnknown("Telegram returned an unverifiable delivery result.") from None
            raise ProviderUnavailable("Telegram returned an invalid response.") from None
        if not isinstance(payload, dict):
            if delivery:
                raise DeliveryUnknown("Telegram returned an unverifiable delivery result.")
            raise ProviderUnavailable("Telegram returned an invalid response.")
        if payload.get("ok") is not True:
            error_code = payload.get("error_code")
            if delivery:
                if payload.get("ok") is False and type(error_code) is int and 400 <= error_code < 500:
                    raise DeliveryRejected("Telegram explicitly rejected the request.")
                raise DeliveryUnknown("Telegram delivery outcome is unknown; do not retry blindly.")
            raise ProviderUnavailable("Telegram rejected the request.")
        if "result" not in payload:
            if delivery:
                raise DeliveryUnknown("Telegram returned an unverifiable delivery result.")
            raise ProviderUnavailable("Telegram returned an invalid response.")
        return payload["result"]


class TelegramPublisher(_TelegramClient):
    """Publisher protocol implementation. A publish call makes one send attempt."""

    async def verify_channel(self, channel_id: str, owner_id: int) -> ChannelInfo:
        channel_id = _channel_argument(channel_id)
        if not _positive_id(owner_id):
            raise InvalidInput("A valid Telegram owner identifier is required.")
        bot = await self._call("getMe", {})
        if not isinstance(bot, dict) or not _positive_id(bot.get("id")) or bot.get("is_bot") is not True:
            raise ProviderUnavailable("Telegram returned invalid bot information.")
        chat = await self._call("getChat", {"chat_id": channel_id})
        if not isinstance(chat, dict):
            raise ProviderUnavailable("Telegram returned invalid channel information.")
        if chat.get("type") != "channel":
            raise InvalidInput("The publication destination must be a Telegram channel.")
        try:
            canonical_id = _canonical_channel(chat.get("id"))
        except ValueError:
            raise ProviderUnavailable("Telegram returned invalid channel information.") from None
        if channel_id.startswith("-") and canonical_id != channel_id:
            raise ProviderUnavailable("Telegram returned mismatched channel information.")
        title = chat.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ProviderUnavailable("Telegram returned invalid channel information.")
        bot_member = await self._call("getChatMember", {"chat_id": canonical_id, "user_id": bot["id"]})
        owner_member = await self._call("getChatMember", {"chat_id": canonical_id, "user_id": owner_id})
        for member, user_id in ((bot_member, bot["id"]), (owner_member, owner_id)):
            if (
                not isinstance(member, dict)
                or not isinstance(member.get("user"), dict)
                or type(member["user"].get("id")) is not int
                or member["user"]["id"] != user_id
                or not isinstance(member.get("status"), str)
            ):
                raise ProviderUnavailable("Telegram returned invalid membership information.")
        if bot_member.get("status") != "administrator" or bot_member.get("can_post_messages") is not True:
            raise Forbidden("The bot must be a channel administrator with publication permission.")
        if owner_member.get("status") not in {"creator", "administrator"}:
            raise Forbidden("The owner must be an administrator of this channel.")
        return ChannelInfo(id=canonical_id, title=title)

    async def publish(self, post: PostView, image: bytes, mime_type: str) -> PublicationResult:
        if post.is_demo:
            raise DeliveryRejected("Demo posts cannot be published by the real Telegram adapter.")
        if not _plain_text_valid(post.text, 900, 1024):
            raise DeliveryRejected("The photo caption exceeds the supported limits or is invalid.")
        if not isinstance(image, bytes) or not 0 < len(image) <= _MAX_IMAGE_BYTES:
            raise DeliveryRejected("The publication image has an invalid size.")
        if mime_type == "image/png" and image.startswith(b"\x89PNG\r\n\x1a\n"):
            filename = "publication.png"
        elif mime_type == "image/jpeg" and image.startswith(b"\xff\xd8\xff"):
            filename = "publication.jpg"
        else:
            raise DeliveryRejected("The publication image must be a matching PNG or JPEG file.")
        try:
            channel_id = _channel_argument(post.channel_id, canonical_only=True)
        except InvalidInput:
            raise DeliveryRejected("A verified numeric channel identifier is required.") from None
        result = await self._call(
            "sendPhoto",
            {"chat_id": channel_id, "caption": post.text},
            files={"photo": (filename, image, mime_type)},
            delivery=True,
        )
        try:
            if not isinstance(result, dict) or not _positive_id(result.get("message_id")):
                raise ValueError
            chat = result.get("chat")
            if not isinstance(chat, dict) or chat.get("type") != "channel":
                raise ValueError
            returned_channel = _canonical_channel(chat.get("id"))
            if returned_channel != channel_id:
                raise ValueError
            username = chat.get("username")
            if username is not None and (not isinstance(username, str) or not _USERNAME.fullmatch(username)):
                raise ValueError
            message_id = result["message_id"]
            link = (
                f"https://t.me/{username}/{message_id}"
                if username
                else f"https://t.me/c/{returned_channel[4:]}/{message_id}"
            )
            return PublicationResult(channel_id=returned_channel, message_id=message_id, message_url=link)
        except (ValueError, TypeError, KeyError):
            raise DeliveryUnknown("Telegram returned an unverifiable delivery result.") from None

    async def subscriber_count(self, channel_id: str) -> int:
        channel_id = _channel_argument(channel_id, canonical_only=True)
        count = await self._call("getChatMemberCount", {"chat_id": channel_id})
        if type(count) is not int or count < 0:
            raise ProviderUnavailable("Telegram returned an invalid subscriber count.")
        return count


class TelegramNotifier(_TelegramClient):
    """Notification retries and deduplication are owned by the core outbox."""

    async def send(self, notification: Notification) -> None:
        if not _positive_id(notification.owner_id):
            raise InvalidInput("A valid Telegram notification recipient is required.")
        if not _plain_text_valid(notification.text, 4096, 4096):
            raise InvalidInput("The notification text exceeds the supported limits or is invalid.")
        result = await self._call(
            "sendMessage", {"chat_id": notification.owner_id, "text": notification.text}
        )
        if (
            not isinstance(result, dict)
            or not _positive_id(result.get("message_id"))
            or not isinstance(result.get("chat"), dict)
            or type(result["chat"].get("id")) is not int
            or result["chat"]["id"] != notification.owner_id
        ):
            raise ProviderUnavailable("Telegram returned an invalid notification result.")
