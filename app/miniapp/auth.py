import hashlib
import hmac
import json
from datetime import datetime
from urllib.parse import parse_qsl

from app.shared.errors import Forbidden

MAX_AGE_SECONDS = 3600


def validate_init_data(raw: str, token: str, now: datetime) -> int:
    """Authenticate the Telegram user using the signed, short-lived initData."""
    invalid = "Откройте мини-приложение из меню Telegram-бота."
    try:
        if not raw or len(raw) > 8192:
            raise ValueError
        items = parse_qsl(raw, strict_parsing=True, max_num_fields=32, errors="strict")
        data = dict(items)
        if len(data) != len(items) or any(
            "\n" in key or "\r" in key or "\n" in value or "\r" in value for key, value in items
        ):
            raise ValueError
        received = data.pop("hash")
        if len(received) != 64 or not received.isascii():
            raise ValueError
        check = "\n".join(f"{key}={data[key]}" for key in sorted(data))
        secret = hmac.digest(b"WebAppData", token.encode(), hashlib.sha256)
        calculated = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(received, calculated):
            raise ValueError
        age = now.timestamp() - int(data["auth_date"])
        if not -30 <= age <= MAX_AGE_SECONDS:
            raise ValueError
        actor = json.loads(data["user"])["id"]
        if type(actor) is not int or actor <= 0:
            raise ValueError
        return actor
    except (ValueError, KeyError, TypeError, UnicodeError, OverflowError) as exc:
        raise Forbidden(invalid) from exc
