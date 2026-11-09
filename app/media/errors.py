"""Stable errors safe to expose without leaking provider response bodies."""

from app.shared.errors import AppError, InvalidInput, ProviderUnavailable


class MediaError(AppError):
    """Base class for recoverable media failures."""


class MediaConfigurationError(MediaError, InvalidInput, ValueError):
    """The backend did not provide a valid provider configuration."""


class InvalidImageRequestError(MediaError, InvalidInput, ValueError):
    """The requested draft violates the media input contract."""


class MediaAuthenticationError(MediaError, ProviderUnavailable):
    """The image service rejected its credentials or permissions."""


class MediaRateLimitError(MediaError, ProviderUnavailable):
    """The image service rejected the request because of a usage limit."""


class MediaUnavailableError(MediaError, ProviderUnavailable):
    """The image service or network could not complete the request."""


class MediaTimeoutError(MediaError, ProviderUnavailable):
    """The network operation timed out."""


class InvalidImageResponseError(MediaError, ProviderUnavailable):
    """The image service returned an invalid or unsupported response."""
