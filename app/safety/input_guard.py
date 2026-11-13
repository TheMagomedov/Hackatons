"""Conservative, local prompt-injection screening; authorization lives outside the model.

This deliberately does not claim to recognize every paraphrase, encoding or
malicious image. Never log the matched text; callers may record category names.
"""

import re
import unicodedata
from typing import Literal

from app.shared.errors import AppError, InvalidInput

GUARD_VERSION = "input-guard-v1"
_LIMIT = 160_000
_RULES = {
    "override": re.compile(
        r"\b(?:ignore|disregard|forget|override)\b.{0,45}\b(?:previous|prior|system|all)\b"
        r".{0,35}\b(?:instructions?|rules?|prompts?)\b"
        r"|\b(?:игнорируй|забудь|отмени|отключи|обойди)\b.{0,55}"
        r"(?:предыдущ\w*|системн\w*|все\b|правил\w*|защит\w*|фильтр\w*|модераци\w*)"
        r"|\b(?:ты теперь|you are now)\b.{0,45}(?:без ограничений|unrestricted|jailbroken)",
        re.I | re.S,
    ),
    "prompt_extraction": re.compile(
        r"\b(?:reveal|print|show|repeat|dump|send)\b.{0,55}"
        r"\b(?:system prompt|hidden instructions?|developer message|api keys?|passwords?|access tokens?)\b"
        r"|\b(?:покажи|выведи|раскрой|повтори|напечатай|пришли|отправь)\b.{0,55}"
        r"(?:системн\w*\s+(?:промпт|инструкц)|скрыт\w*\s+инструкц|"
        r"(?:api|апи)[ -]?ключ|парол\w*|токен\w*\s+(?:бота|доступа))",
        re.I | re.S,
    ),
    "role_spoofing": re.compile(
        r"<\|(?:im_start|start_header_id)\|>\s*(?:system|developer)"
        r"|\[\s*(?:system|developer)\s*\]\s*[:\n]"
        r"|</?\s*(?:system|developer)\s*>", re.I,
    ),
    "approval_bypass": re.compile(
        r"\b(?:publish|approve|send)\b.{0,55}\bwithout\b.{0,25}\b(?:approval|confirmation|permission)\b"
        r"|\b(?:опубликуй|одобри|публикуй|отправь)\b.{0,55}без\s+(?:согласован\w*|подтвержден\w*|разрешен\w*)",
        re.I | re.S,
    ),
    "tool_exfiltration": re.compile(
        r"(?:curl|wget|fetch\s*\(|requests\.(?:get|post)).{0,100}"
        r"(?:\.env\b|api[_ -]?key|bot[_ -]?token|password|secret)"
        r"|(?:\.env\b|api[_ -]?key|bot[_ -]?token|парол\w*).{0,80}https?://", re.I | re.S,
    ),
}


class PromptInjectionBlocked(AppError):
    code = "prompt_injection"

    def __init__(self):
        super().__init__(
            "В тексте обнаружена попытка изменить правила AI или раскрыть защищённые данные. "
            "Уберите такие инструкции и повторите запрос."
        )


def _normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return text.casefold().replace("ё", "е")


def inspect_input(text: str, *, source: Literal["user", "document"] = "user") -> tuple[str, ...]:
    if source not in {"user", "document"} or not isinstance(text, str) or len(text) > _LIMIT:
        raise InvalidInput("Некорректный или слишком большой текст для проверки безопасности.")
    normalized = _normalized(text)
    # A second view catches common Cyrillic lookalikes in English control phrases.
    ascii_view = normalized.translate(str.maketrans("аесорхуіј", "aecopxyij"))
    def detected(name, rule, view):
        for match in rule.finditer(view):
            # Regulations commonly prohibit exactly these actions. A directly
            # negated instruction is not itself evidence of an attack.
            negated = re.search(r"\b(?:не|нельзя|запрещено|never|not|don't)\s*$", view[:match.start()], re.I)
            if name == "role_spoofing" or not negated:
                return True
        return False

    return tuple(name for name, rule in _RULES.items()
                 if detected(name, rule, normalized) or detected(name, rule, ascii_view))


def guard_input(text: str, *, source: Literal["user", "document"] = "user") -> None:
    if inspect_input(text, source=source):
        raise PromptInjectionBlocked()


def guard_payload(value, *, depth: int = 0) -> None:
    """Defence in depth for provider callers; only input data, never server instructions."""
    if depth > 32:
        raise InvalidInput("Слишком глубокая структура AI-запроса.")
    if isinstance(value, str):
        guard_input(value, source="document")
    elif isinstance(value, dict):
        for item in value.values():
            guard_payload(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            guard_payload(item, depth=depth + 1)
