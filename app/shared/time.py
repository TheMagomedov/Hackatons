"""All persisted instants are timezone-aware; the UI chooses presentation timezone."""

from datetime import UTC, datetime

from app.shared.errors import InvalidInput


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidInput("Укажите дату с часовым поясом.")
    return value.astimezone(UTC)
