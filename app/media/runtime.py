"""Read deployment settings at the runtime factory boundary."""

import os

from app.config import Settings
from app.shared.errors import ProviderUnavailable
from app.shared.models import BusinessProfile, GeneratedImage, TextDraft
from app.shared.ports import ImageProvider

from .branding import SalavatBrandingProvider
from .provider import build_provider as configured_provider


class UnconfiguredImages:
    async def generate(
        self, profile: BusinessProfile, text: TextDraft, feedback: str | None = None
    ) -> GeneratedImage:
        raise ProviderUnavailable("Укажите IMAGE_API_KEY в локальной конфигурации.")


def build_provider(settings: Settings) -> ImageProvider:
    key = os.getenv("IMAGE_API_KEY", "").strip()
    if settings.mode == "live" and not key:
        return UnconfiguredImages()
    base = os.getenv("IMAGE_API_BASE", "https://api.openai.com/v1").rstrip("/")
    provider = configured_provider(
        settings, api_key=key, model=os.getenv("IMAGE_MODEL") or "gpt-image-1",
        endpoint=f"{base}/images/generations",
    )
    return provider if settings.mode == "demo" else SalavatBrandingProvider(provider)
