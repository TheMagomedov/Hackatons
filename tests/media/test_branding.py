import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops

from app.config import Settings
from app.media import runtime
from app.media.branding import (
    ASSETS,
    SalavatBrandingProvider,
    _headline,
    _title_layout,
    is_salavatsteklo,
    render_card,
)
from app.media.errors import InvalidImageResponseError, MediaConfigurationError, MediaUnavailableError
from app.media.validation import DEFAULT_LIMITS, validate_png
from app.shared.models import BusinessProfile, GeneratedImage, TextDraft


def photo_bytes(size=(1024, 512)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (65, 126, 177)).save(buffer, format="PNG")
    return buffer.getvalue()


class BrandCardTests(unittest.TestCase):
    def test_official_artwork_and_font_match_supplied_manifest(self):
        expected = {
            "brand-logo.png": "ae74554a266df31483f96949e63de3b922f7726b75e7e318e408fe908123e950",
            "brand-intro.ttf": "d3fb6359c13b64e194951159d1f3837ad8d3d199b310953ad52513fc39b0953c",
        }
        for name, digest in expected.items():
            with self.subTest(asset=name):
                self.assertEqual(hashlib.sha256((ASSETS / name).read_bytes()).hexdigest(), digest)

    def test_original_photo_is_whole_and_undistorted_below_header(self):
        content = photo_bytes()
        result = render_card(content, "Стеклянные фасады в современной архитектуре.")
        validate_png(result, DEFAULT_LIMITS)
        with Image.open(io.BytesIO(result)) as card, Image.open(io.BytesIO(content)) as source:
            self.assertEqual(card.width, source.width)
            self.assertGreater(card.height, source.height + 150)
            photo = card.crop((0, card.height - source.height, card.width, card.height))
            self.assertIsNone(ImageChops.difference(photo, source).getbbox())
            self.assertLessEqual(len(result), 5 * 1024 * 1024)

    def test_logo_keeps_exact_artwork_and_white_clear_space(self):
        result = render_card(photo_bytes(), "#Публикация")
        with Image.open(io.BytesIO(result)) as card, Image.open(ASSETS / "brand-logo.png") as source:
            # Verify the rendered mark against the full original, not a recreated wordmark.
            logo = source.crop(source.getbbox()).resize((210, 112), Image.Resampling.LANCZOS)
            expected = Image.new("RGB", (210, 112), "white")
            expected.paste(logo, (0, 0), logo)
            self.assertIsNone(ImageChops.difference(card.crop((48, 48, 258, 160)), expected).getbbox())
            self.assertGreaterEqual(112, 35)
            # X=ceil(112/7)=16px. No line or text enters the four surrounding strips.
            for bounds in ((32, 32, 274, 48), (32, 160, 274, 176),
                           (32, 48, 48, 160), (258, 48, 274, 160)):
                area = card.crop(bounds)
                self.assertIsNone(ImageChops.difference(area, Image.new("RGB", area.size, "white")).getbbox())

    def test_only_explicit_company_names_are_branded(self):
        for name in ("САЛАВАТСТЕКЛО", "Салаватстекло", 'АО «Салаватстекло»', '  ао "салаватстекло"  '):
            with self.subTest(name=name):
                self.assertTrue(is_salavatsteklo(name))
        for name in ("Стекольный завод", "Партнёр Салаватстекло", "ООО Салаватстекло", "Салаватстекло сервис"):
            with self.subTest(name=name):
                self.assertFalse(is_salavatsteklo(name))

    def test_caption_is_used_without_inventing_a_headline(self):
        self.assertEqual(_headline("Стекло толщиной 3.5 мм. Остальная подпись."), "Стекло толщиной 3.5 мм.")
        long_caption = "Современные стеклянные фасады дополняют архитектуру и создают визуальную лёгкость пространства."
        title = _headline(long_caption)
        self.assertLessEqual(len(title), 80)
        self.assertTrue(title.endswith("…"))
        self.assertTrue(long_caption.startswith(title[:-1]))
        for caption in ("https://example.invalid", "#Стекло", "✨ Новая архитектура", "**Наш новый пост**"):
            with self.subTest(caption=caption):
                self.assertEqual(_headline(caption), "")

    def test_long_russian_title_fits_two_lines_in_small_card(self):
        for caption in ("Современная архитектура и выразительные стеклянные фасады в городской среде", "Архитектура" * 8):
            lines, font = _title_layout(_headline(caption), 464, 512)
            self.assertLessEqual(len(lines), 2)
            self.assertTrue(all(font.getlength(line) <= 464 for line in lines))
            result = render_card(photo_bytes((512, 512)), caption)
            validate_png(result, DEFAULT_LIMITS)

    def test_tall_image_stays_within_shared_dimensions(self):
        result = render_card(photo_bytes((512, 4096)), "Архитектура.")
        validate_png(result, DEFAULT_LIMITS)
        with Image.open(io.BytesIO(result)) as card:
            self.assertLessEqual(card.height, 4096)
            self.assertEqual(card.width, 512)

    def test_invalid_image_and_missing_artwork_fail_explicitly(self):
        with self.assertRaises(InvalidImageResponseError):
            render_card(b"not a PNG", "Архитектура.")
        with tempfile.TemporaryDirectory() as directory, patch("app.media.branding.ASSETS", Path(directory)):
            with self.assertRaises(MediaConfigurationError):
                render_card(photo_bytes(), "Архитектура.")


class StaticProvider:
    is_demo = False

    def __init__(self, image):
        self.image = image
        self.calls = []

    async def generate(self, profile, text, feedback=None):
        self.calls.append((profile, text, feedback))
        return self.image


class BrandingProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_card_preserves_provider_and_entire_caption(self):
        raw = GeneratedImage(content=photo_bytes(), mime_type="image/png", provider="flux", is_demo=False)
        source = StaticProvider(raw)
        wrapped = SalavatBrandingProvider(source)
        profile = BusinessProfile(name="АО «Салаватстекло»", description="")
        draft = TextDraft(text="Стеклянные фасады. Полная подпись остаётся в публикации.", image_brief="Стеклянное здание")
        original_caption = draft.text
        card = await wrapped.generate(profile, draft, "Больше неба")
        self.assertNotEqual(card.content, raw.content)
        self.assertEqual((card.provider, card.mime_type, card.is_demo), ("flux", "image/png", False))
        self.assertEqual(draft.text, original_caption)
        self.assertEqual(source.calls, [(profile, draft, "Больше неба")])

    async def test_other_company_and_demo_keep_original_result(self):
        for name, demo in (("Другая компания", False), ("САЛАВАТСТЕКЛО", True)):
            raw = GeneratedImage(content=photo_bytes(), mime_type="image/png", provider="example", is_demo=demo)
            wrapper = SalavatBrandingProvider(StaticProvider(raw))
            result = await wrapper.generate(
                BusinessProfile(name=name, description=""), TextDraft(text="Описание", image_brief="Здание")
            )
            self.assertIs(result, raw)

    async def test_provider_failure_is_not_retried_or_replaced(self):
        class FailingProvider:
            calls = 0

            async def generate(self, *args):
                self.calls += 1
                raise MediaUnavailableError("Unavailable")

        source = FailingProvider()
        with self.assertRaises(MediaUnavailableError):
            await SalavatBrandingProvider(source).generate(
                BusinessProfile(name="САЛАВАТСТЕКЛО", description=""), TextDraft(text="Описание", image_brief="Здание")
            )
        self.assertEqual(source.calls, 1)

    def test_runtime_wraps_configured_live_provider_only(self):
        settings = Settings(mode="live", owner_id=1, telegram_bot_token="unused-test-token",
                            ai_factory="test:ai", image_factory="app.media.runtime:build_provider")
        source = StaticProvider(None)
        with patch.dict(os.environ, {
            "IMAGE_API_KEY": "unused-test-key", "IMAGE_MODEL": "fixture-image-model",
        }), patch.object(
            runtime, "configured_provider", return_value=source
        ):
            self.assertIsInstance(runtime.build_provider(settings), SalavatBrandingProvider)
            self.assertIs(runtime.build_provider(Settings()), source)
        with patch.dict(os.environ, {"IMAGE_API_KEY": ""}):
            self.assertIsInstance(runtime.build_provider(settings), runtime.UnconfiguredImages)


if __name__ == "__main__":
    unittest.main()
