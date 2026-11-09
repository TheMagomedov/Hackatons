import os

from app.ai.client import Provider
from app.config import Settings
from app.media.generator import ModelImages


def build_provider(settings: Settings) -> ModelImages:
    provider = Provider(
        os.getenv("IMAGE_API_BASE", "https://api.openai.com/v1"),
        os.getenv("IMAGE_API_KEY", "").strip(), "IMAGE_API_KEY", settings.provider_timeout_seconds,
    )
    return ModelImages(provider, os.getenv("IMAGE_MODEL") or "gpt-image-1")
