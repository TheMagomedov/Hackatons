"""Read deployment settings at the runtime factory boundary."""

import os

from app.config import Settings
from app.shared.errors import ProviderUnavailable
from app.shared.models import BusinessProfile, GeneratedImage, TextDraft
from app.shared.ports import ImageProvider

from .branding import SalavatBrandingProvider
from .errors import MediaConfigurationError
from .provider import build_provider as configured_provider


class UnconfiguredImages:
    async def generate(
        self, profile: BusinessProfile, text: TextDraft, feedback: str | None = None
    ) -> GeneratedImage:
        raise ProviderUnavailable("Укажите IMAGE_API_KEY в локальной конфигурации.")


def build_provider(settings: Settings) -> ImageProvider:
    key = os.getenv("IMAGE_API_KEY", "").strip()
    local_flux = os.getenv("IMAGE_LOCAL_FLUX", "0")
    if settings.mode == "live" and local_flux not in ("0", "1"):
        raise MediaConfigurationError("IMAGE_LOCAL_FLUX must be exactly 0 or 1.")
    if settings.mode == "live" and not key:
        return UnconfiguredImages()
    base = os.getenv("IMAGE_API_BASE", "https://api.openai.com/v1").rstrip("/")
    provider = configured_provider(
        settings, api_key=key, model=os.getenv("IMAGE_MODEL") or "gpt-image-1",
        endpoint=f"{base}/images/generations",
        allow_local_flux=settings.mode == "live" and local_flux == "1",
    )
    return provider if settings.mode == "demo" else SalavatBrandingProvider(provider)
