"""Local, irreversible masking at external text boundaries.

This is pattern-based DLP, not general name/address recognition. Original
documents stay local; no mapping of placeholders back to personal data exists.
"""

import re

from app.shared.errors import AppError, InvalidInput

PRIVACY_VERSION = "privacy-v1"
_LIMIT = 200_000
_PATTERNS = {
    "SECRET": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
        r"|\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})\b"
        r"|(?<!\w)\d{8,12}:[A-Za-z0-9_-]{30,}(?!\w)"
        r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
        r"|\bBearer\s+[A-Za-z0-9._~+/-]{12,}={0,2}"
        r"|\b(?:api[_ -]?key|(?:bot|access|refresh)[_ -]?token|password|secret|пароль|секретный\s+ключ)"
        r"[\"']?\s*[:=]\s*[\"']?[^\s,;\"']{6,}",
        re.I,
    ),
    "EMAIL": re.compile(r"(?<![\w.+-])[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+(?![\w-])", re.I),
    "PASSPORT": re.compile(r"(?<![\w-])\d{2}\s?\d{2}\s+\d{6}(?![\w-])"),
    "SNILS": re.compile(r"(?<![\w-])\d{3}-\d{3}-\d{3}\s?\d{2}(?![\w-])"),
    "CARD": re.compile(r"(?<![\w-])(?:\d[ -]?){15}\d(?![\w-])"),
    "PHONE": re.compile(r"(?<![\w-])(?:\+?7|8)[\s(.-]*\d{3}[\s).\-]*\d{3}[\s.\-]*\d{2}[\s.\-]*\d{2}(?![\w-])"
                        r"|(?<![\w-])\+(?!7\b)\d(?:[ ().-]?\d){8,14}(?![\w-])"),
}
_HIGH_RISK = frozenset({"PASSPORT", "SNILS", "CARD", "SECRET"})
_SENSITIVE_FIELDS = {
    "password": "SECRET", "api_key": "SECRET", "token": "SECRET", "access_token": "SECRET",
    "refresh_token": "SECRET", "bot_token": "SECRET", "telegram_bot_token": "SECRET",
    "email": "EMAIL", "phone": "PHONE", "passport": "PASSPORT", "snils": "SNILS",
    "card_number": "CARD", "credit_card": "CARD",
}


class SensitiveDataBlocked(AppError):
    code = "sensitive_data"

    def __init__(self):
        super().__init__(
            "Материал содержит похожие на секреты или личные реквизиты данные. "
            "Удалите их перед отправкой или публикацией."
        )


def sensitive_categories(text: str) -> tuple[str, ...]:
    if not isinstance(text, str) or len(text) > _LIMIT:
        raise InvalidInput("Некорректный или слишком большой текст для проверки данных.")
    return tuple(name for name, pattern in _PATTERNS.items() if pattern.search(text))


def redact_text(text: str) -> str:
    if not isinstance(text, str) or len(text) > _LIMIT:
        raise InvalidInput("Некорректный или слишком большой текст для маскировки данных.")
    for name, pattern in _PATTERNS.items():
        text = pattern.sub(f"[{name}]", text)
    return text


def redact_for_egress(value, *, _depth: int = 0):
    """Return a new JSON-compatible structure; source UUIDs and field names remain stable."""
    if _depth > 32:
        raise InvalidInput("Слишком глубокая структура данных для маскировки.")
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: (f"[{_SENSITIVE_FIELDS[key.casefold()]}]"
                  if isinstance(key, str) and key.casefold() in _SENSITIVE_FIELDS and item is not None
                  else redact_for_egress(item, _depth=_depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_for_egress(item, _depth=_depth + 1) for item in value]
    return value


def guard_public_output(text: str) -> None:
    """Block credentials/identity/payment data; business email/phone needs ordinary approval."""
    if _HIGH_RISK.intersection(sensitive_categories(text)):
        raise SensitiveDataBlocked()
