"""Safe domain errors. Never put credentials or raw provider responses here."""


class AppError(Exception):
    code = "application_error"


class NotFound(AppError):
    code = "not_found"


class Forbidden(AppError):
    code = "forbidden"


class Conflict(AppError):
    code = "conflict"


class RateLimited(Conflict):
    code = 'rate_limited'


class InvalidInput(AppError):
    code = "invalid_input"


class ProviderUnavailable(AppError):
    code = "provider_unavailable"


class SafetyBlocked(AppError):
    code = "content_blocked"

    def __init__(self):
        super().__init__("Материал не прошёл проверку безопасности. Измените текст или изображение.")


class SafetyUnavailable(AppError):
    code = "safety_unavailable"

    def __init__(self):
        super().__init__("Проверка безопасности недоступна. Материал не будет отправлен; повторите позже.")


class DeliveryRejected(AppError):
    """Telegram explicitly rejected the request; no message was accepted."""

    code = "delivery_rejected"


class DeliveryUnknown(AppError):
    """The request might have been accepted. A blind retry is unsafe."""

    code = "delivery_unknown"
