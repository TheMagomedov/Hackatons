"""Runtime factory. Image secrets must be explicitly wired by the backend."""

from app.config import Settings
from app.shared.ports import ImageProvider

from .demo import DemoImageProvider
from .errors import MediaConfigurationError
from .limits import MediaLimits
from .openai import BlockingImageTransport, OpenAIImageProvider
from .validation import DEFAULT_LIMITS


def build_provider(
    settings: Settings,
    *,
    api_key: str | None = None,
    model: str | None = None,
    endpoint: str = "https://api.openai.com/v1/images/generations",
    limits: MediaLimits = DEFAULT_LIMITS,
    transport: BlockingImageTransport | None = None,
) -> ImageProvider:
    """No network, persistence, environment lookup, or live-to-demo fallback."""
    if settings.mode == "demo":
        return DemoImageProvider(limits=limits)
    if settings.mode != "live":
        raise MediaConfigurationError("Unknown backend mode for the image provider.")
    if not api_key or not model:
        raise MediaConfigurationError(
            "Live image generation requires an explicitly injected API key and image model. "
            "Configure a backend wrapper factory calling app.media.provider.build_provider "
            "with api_key and model; image credentials are not yet standardized in Settings."
        )
    return OpenAIImageProvider(
        api_key=api_key,
        model=model,
        timeout_seconds=settings.provider_timeout_seconds,
        endpoint=endpoint,
        limits=limits,
        transport=transport,
    )
