"""Bounded authenticated moderation requests; no retry or raw provider errors."""

import ipaddress
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Protocol

import certifi

from app.shared.errors import InvalidInput, SafetyBlocked, SafetyUnavailable

MAX_TEXT_CHARACTERS = 12000
MAX_TEXT_ITEMS = 16
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024


class SafetyPolicy(Protocol):
    def require_configured(self) -> None: ...
    def check_text(self, texts: list[str]) -> None: ...
    def check_image(self, content: bytes, mime_type: str) -> None: ...


class NoopSafety:
    """Explicit network-free policy for offline demo and direct unit-test fixtures."""

    def require_configured(self) -> None:
        pass

    def check_text(self, texts: list[str]) -> None:
        pass

    def check_image(self, content: bytes, mime_type: str) -> None:
        pass


class UnavailableSafety:
    """Live mode without usable configuration must never silently allow content."""

    def require_configured(self) -> None:
        raise SafetyUnavailable()

    def check_text(self, texts: list[str]) -> None:
        raise SafetyUnavailable()

    def check_image(self, content: bytes, mime_type: str) -> None:
        raise SafetyUnavailable()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibSafetyTransport:
    def send(self, request: urllib.request.Request, timeout: float) -> bytes:
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=certifi.where())
        host = (urllib.parse.urlsplit(request.full_url).hostname or "").lower().rstrip(".")
        direct = host == "localhost" or host.endswith(".ts.net")
        try:
            direct = direct or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        handlers = [_NoRedirect(), urllib.request.HTTPSHandler(context=context)]
        if direct:
            handlers.insert(0, urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200 or response.headers.get_content_type() != "application/json":
                raise SafetyUnavailable()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise SafetyUnavailable()
            return body


class HTTPModeration:
    def __init__(self, base_url: str, api_key: str, *, transport=None):
        if not isinstance(base_url, str):
            raise SafetyUnavailable()
        try:
            url = urllib.parse.urlsplit(base_url)
            host = (url.hostname or "").lower().rstrip(".")
            loopback = host == "localhost"
            try:
                loopback = loopback or ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
            valid = (
                bool(host) and url.scheme in {"http", "https"}
                and (url.scheme == "https" or loopback)
                and url.username is None and url.password is None
                and not url.query and not url.fragment and not any(c.isspace() for c in base_url)
            )
            _ = url.port
        except (TypeError, ValueError):
            valid = False
        if (not valid or not isinstance(api_key, str) or not api_key or not api_key.isascii()
                or any(c.isspace() for c in api_key)):
            raise SafetyUnavailable()
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._transport = transport or UrllibSafetyTransport()

    def __repr__(self):
        return "HTTPModeration(api_key=<redacted>)"

    def require_configured(self) -> None:
        pass

    def _check(self, path: str, body: bytes, content_type: str, timeout: float) -> None:
        request = urllib.request.Request(
            self._base_url + path, data=body, method="POST",
            headers={"Authorization": "Bearer " + self._api_key,
                     "Content-Type": content_type, "Accept": "application/json"},
        )
        try:
            raw = self._transport.send(request, timeout)
            if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
                raise SafetyUnavailable()
            verdict = json.loads(raw.decode("utf-8"))
            if (not isinstance(verdict, dict) or type(verdict.get("allowed")) is not bool
                    or not isinstance(verdict.get("reason"), str)
                    or not isinstance(verdict.get("category"), str)):
                raise SafetyUnavailable()
        except Exception:
            raise SafetyUnavailable() from None
        if not verdict["allowed"]:
            raise SafetyBlocked()

    def check_text(self, texts: list[str]) -> None:
        if (not isinstance(texts, list) or not texts or len(texts) > MAX_TEXT_ITEMS
                or any(not isinstance(text, str) for text in texts)
                or sum(map(len, texts)) > MAX_TEXT_CHARACTERS):
            raise InvalidInput("Для проверки нужен текст общим объёмом не более 12000 символов.")
        try:
            body = json.dumps({"texts": texts}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError):
            raise InvalidInput("Текст содержит неподдерживаемые символы.") from None
        self._check("/safety/check-text", body, "application/json", 90)

    def check_image(self, content: bytes, mime_type: str) -> None:
        if (not isinstance(content, bytes) or not 0 < len(content) <= MAX_IMAGE_BYTES
                or mime_type not in {"image/png", "image/jpeg"}):
            raise InvalidInput("Для проверки нужно изображение PNG или JPEG размером не более 5 МиБ.")
        self._check("/safety/check-image", content, mime_type, 30)


def from_environment(environ: Mapping[str, str] | None = None) -> SafetyPolicy:
    env = os.environ if environ is None else environ
    base = env.get("MODERATION_API_BASE", "").strip()
    key = env.get("MODERATION_API_KEY", "").strip()
    if not base or not key:
        return UnavailableSafety()
    try:
        return HTTPModeration(base, key)
    except SafetyUnavailable:
        return UnavailableSafety()
