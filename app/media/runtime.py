"""Read deployment settings at the runtime factory boundary."""

import os

from app.config import Settings
from app.shared.errors import ProviderUnavailable
from app.shared.models import BusinessProfile, GeneratedImage, TextDraft
from app.shared.ports import ImageProvider

from .branding import SalavatBrandingProvider
from .provider import build_provider as configured_provider


class UnconfiguredImages:
    """Keep menus available while reporting only missing configuration names."""

    def __init__(self, missing: tuple[str, ...]):
        self._message = "Укажите " + ", ".join(missing) + " в локальной конфигурации."

    async def generate(
        self, profile: BusinessProfile, text: TextDraft, feedback: str | None = None
    ) -> GeneratedImage:
        raise ProviderUnavailable(self._message)


def build_provider(settings: Settings) -> ImageProvider:
    """Preserve explicit model choice; defer missing live settings until use."""
    if settings.mode == "demo":
        return configured_provider(settings)
    key = os.getenv("IMAGE_API_KEY", "")
    model = os.getenv("IMAGE_MODEL", "")
    missing = tuple(
        name for name, value in (("IMAGE_API_KEY", key), ("IMAGE_MODEL", model))
        if not value.strip()
    )
    if settings.mode == "live" and missing:
        return UnconfiguredImages(missing)
    base = os.getenv("IMAGE_API_BASE", "https://api.openai.com/v1").rstrip("/")
    provider = configured_provider(
        settings, api_key=key, model=model,
        endpoint=f"{base}/images/generations",
    )
    return SalavatBrandingProvider(provider)
