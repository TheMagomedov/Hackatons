import asyncio
import base64
import binascii
import io

from PIL import Image, ImageOps, UnidentifiedImageError

from app.ai.client import Provider
from app.shared.errors import ProviderUnavailable
from app.shared.models import BusinessProfile, GeneratedImage, TextDraft


def normalize_image(data: bytes, provider: str) -> GeneratedImage:
    try:
        if len(data) > 20_000_000:
            raise ValueError
        with Image.open(io.BytesIO(data)) as original:
            if original.format not in {"PNG", "JPEG", "WEBP"} or original.width * original.height > 16_000_000:
                raise ValueError
            original.thumbnail((1600, 1600))
            image = ImageOps.exif_transpose(original).convert("RGB")
            if max(image.size) > 20 * min(image.size):
                raise ValueError
            output = io.BytesIO()
            image.save(output, "JPEG", quality=90, optimize=True)
            return GeneratedImage(content=output.getvalue(), mime_type="image/jpeg", provider=provider)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ProviderUnavailable("Полученное изображение повреждено или не подходит для Telegram.") from exc


class ModelImages:
    def __init__(self, provider: Provider, model: str):
        self.provider = provider
        self.model = model

    async def generate(
        self, profile: BusinessProfile, text: TextDraft, feedback: str | None = None
    ) -> GeneratedImage:
        prompt = (
            "Создай квадратную иллюстрацию к посту в Telegram. Используй естественный свет, "
            "читаемую композицию, без надписей, цен, скидок и неподтверждённых свойств.\n"
            f"Бизнес: {profile.name}. Стиль: {profile.tone}.\n"
            f"Идея изображения: {text.image_brief}. Текст: {text.text}.\n"
            f"Уточнение композиции: {feedback or ''}"
        )
        response = await self.provider.request(
            "images/generations", {"model": self.model, "prompt": prompt, "size": "1024x1024"},
            limit=28_000_000,
        )
        try:
            data = base64.b64decode(response["data"][0]["b64_json"], validate=True)
        except (KeyError, IndexError, TypeError, ValueError, binascii.Error) as exc:
            raise ProviderUnavailable("Генератор не вернул изображение в ожидаемом формате.") from exc
        return await asyncio.to_thread(normalize_image, data, self.model)

    async def close(self) -> None:
        await self.provider.close()
