"""Deterministic cards using the supplied Salavatsteklo artwork, never an AI logo."""

import asyncio
import io
import math
import re
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from app.shared.models import BusinessProfile, GeneratedImage, TextDraft
from app.shared.ports import ImageProvider

from .errors import InvalidImageResponseError, MediaConfigurationError
from .validation import DEFAULT_LIMITS, validate_png

ASSETS = Path(__file__).resolve().parents[1] / "miniapp" / "static"
PETROL = "#196C7F"
WHITE = "#FFFFFF"


def is_salavatsteklo(name: str) -> bool:
    normalized = unicodedata.normalize("NFKC", name).casefold()
    normalized = re.sub(r'''[\s"'«»“”„‘’]+''', "", normalized)
    return normalized in {"салаватстекло", "аосалаватстекло"}


def _headline(caption: str) -> str:
    first = re.split(r"(?<=[.!?])\s+|[\r\n]+", caption.strip(), maxsplit=1)[0]
    first = " ".join(first.split())
    if re.search(r"https?://|www\.|(?:^|\s)[@#]|[<>*`]", first, re.IGNORECASE):
        return ""
    if re.search(r'''[^A-Za-zА-Яа-яЁё0-9\s.,!?;:%()«»“”„'"/&+\-–—…№=°×]''', first):
        return ""
    if len(first) <= 80:
        return first
    shortened = first[:79]
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(" ,;:-") + "…"


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines = []
    current = ""
    for word in text.split():
        if current and font.getlength(current + " " + word) > width:
            lines.append(current)
            current = ""
        for character in (" " if current else "") + word:
            if current and font.getlength(current + character) > width:
                lines.append(current)
                current = ""
            current += character
    if current:
        lines.append(current)
    return lines


def _title_layout(text: str, width: int, canvas_width: int):
    largest = max(20, round(canvas_width * 0.040))
    smallest = max(16, round(largest * 0.75))
    for size in range(largest, smallest - 1, -1):
        font = ImageFont.truetype(str(ASSETS / "brand-intro.ttf"), size)
        lines = _wrap(text, font, width)
        if len(lines) <= 2:
            return lines, font
    lines = lines[:2]
    ending = lines[-1].rstrip(" .,…")
    while ending and font.getlength(ending + "…") > width:
        ending = ending[:-1]
    lines[-1] = ending.rstrip() + "…"
    return lines, font


def render_card(content: bytes, caption: str) -> bytes:
    validate_png(content, DEFAULT_LIMITS)
    try:
        with Image.open(io.BytesIO(content)) as original:
            original.load()
            rgba = original.convert("RGBA")
            photo = Image.new("RGBA", original.size, WHITE)
            photo.alpha_composite(rgba)
            photo = photo.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise InvalidImageResponseError("Не удалось открыть изображение для фирменной карточки.") from None

    width = min(2048, max(512, photo.width))
    margin = round(width * 0.047)
    try:
        with Image.open(ASSETS / "brand-logo.png") as asset:
            logo = asset.convert("RGBA")
            bounds = logo.getbbox()
            if bounds is None:
                raise ValueError("Empty logo")
            # Remove only empty canvas; the complete original mark is kept.
            logo = logo.crop(bounds)
        logo_width = round(width * 0.205)
        logo_height = round(logo_width * logo.height / logo.width)
        logo = logo.resize((logo_width, logo_height), Image.Resampling.LANCZOS)
        clear_space = math.ceil(logo_height / 7)
        title = _headline(caption)
        lines, font = _title_layout(title, width - 2 * margin, width) if title else ([], None)
    except (OSError, ValueError):
        raise MediaConfigurationError("Недоступны оригинальный логотип или фирменный шрифт САЛАВАТСТЕКЛО.") from None

    rule_y = margin + logo_height + clear_space + max(8, round(width * 0.01))
    title_y = rule_y + max(16, round(width * 0.023))
    line_height = round(font.size * 1.24) if font else 0
    header_height = title_y + len(lines) * line_height + margin
    available_height = DEFAULT_LIMITS.max_dimension - header_height
    scale = min(width / photo.width, available_height / photo.height)
    photo = photo.resize(
        (max(1, round(photo.width * scale)), max(1, round(photo.height * scale))),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGB", (width, header_height + photo.height), WHITE)
    canvas.paste(logo, (margin, margin), logo)
    draw = ImageDraw.Draw(canvas)
    draw.line((margin, rule_y, width - margin, rule_y), fill=PETROL, width=max(1, round(width / 512)))
    for line in lines:
        draw.text((margin, title_y), line, fill=PETROL, font=font, anchor="lt")
        title_y += line_height
    canvas.paste(photo, ((width - photo.width) // 2, header_height))

    while True:
        output = io.BytesIO()
        canvas.save(output, format="PNG", optimize=True)
        encoded = output.getvalue()
        if len(encoded) <= DEFAULT_LIMITS.max_image_bytes:
            validate_png(encoded, DEFAULT_LIMITS)
            return encoded
        # Reduce the whole composition proportionally, preserving logo and photograph geometry.
        next_width = round(canvas.width * 0.85)
        if logo_height * next_width / width < 35:
            raise InvalidImageResponseError("Фирменная карточка превышает допустимый размер PNG.")
        canvas = canvas.resize((next_width, round(canvas.height * 0.85)), Image.Resampling.LANCZOS)


class SalavatBrandingProvider:
    def __init__(self, provider: ImageProvider):
        self._provider = provider
        self.is_demo = getattr(provider, "is_demo", False)

    async def generate(
        self, profile: BusinessProfile, text: TextDraft, feedback: str | None = None
    ) -> GeneratedImage:
        image = await self._provider.generate(profile, text, feedback)
        if not isinstance(image, GeneratedImage):
            raise InvalidImageResponseError("Провайдер не вернул изображение для фирменной карточки.")
        if not is_salavatsteklo(profile.name) or image.is_demo:
            return image
        if image.mime_type != "image/png":
            raise InvalidImageResponseError("Для фирменной карточки нужен PNG.")
        content = await asyncio.to_thread(render_card, image.content, text.text)
        return GeneratedImage(content=content, mime_type="image/png", provider=image.provider, is_demo=image.is_demo)
