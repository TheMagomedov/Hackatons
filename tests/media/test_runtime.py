"""Runtime configuration must preserve the user's explicit image-model choice."""

import os
import unittest
from unittest.mock import patch, sentinel

from app.config import Settings
from app.media.demo import DemoImageProvider
from app.media.provider import build_provider as configured_provider
from app.media.runtime import UnconfiguredImages, build_provider
from app.shared.errors import ProviderUnavailable
from app.shared.models import BusinessProfile, TextDraft


def live_settings():
    return Settings(
        mode="live", owner_id=42, telegram_bot_token="123456:simulated",
        ai_factory="app.ai.provider:build_provider",
        image_factory="app.media.runtime:build_provider",
    )


PROFILE = BusinessProfile(name="Кофейня", description="Тест")
TEXT = TextDraft(text="Кофе", image_brief="Чашка кофе")


class RuntimeImageConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_absent_or_blank_model_with_key_defers_without_constructing_adapter(self):
        for model in (None, "", " \t\n"):
            with self.subTest(model=model):
                environment = {"IMAGE_API_KEY": "private-test-key", "IMAGE_API_BASE": "invalid://"}
                if model is not None:
                    environment["IMAGE_MODEL"] = model
                with patch.dict(os.environ, environment, clear=True), patch(
                    "app.media.runtime.configured_provider"
                ) as constructor:
                    images = build_provider(live_settings())
                    self.assertIsInstance(images, UnconfiguredImages)
                    constructor.assert_not_called()
                    with self.assertRaises(ProviderUnavailable) as caught:
                        await images.generate(PROFILE, TEXT, "Поменять фон")
                    self.assertIn("IMAGE_MODEL", str(caught.exception))
                    self.assertNotIn("private-test-key", str(caught.exception))
                    self.assertNotIn("gpt-image-1", str(caught.exception))
                    constructor.assert_not_called()

    async def test_missing_key_and_model_error_lists_names_without_values(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "app.media.runtime.configured_provider"
        ) as constructor:
            images = build_provider(live_settings())
            with self.assertRaises(ProviderUnavailable) as caught:
                await images.generate(PROFILE, TEXT)
            self.assertIn("IMAGE_API_KEY", str(caught.exception))
            self.assertIn("IMAGE_MODEL", str(caught.exception))
            constructor.assert_not_called()

    async def test_absent_or_blank_key_with_model_does_not_construct_adapter(self):
        for key in (None, "", " \t"):
            with self.subTest(key=key):
                environment = {"IMAGE_MODEL": "user-selected-private-model"}
                if key is not None:
                    environment["IMAGE_API_KEY"] = key
                with patch.dict(os.environ, environment, clear=True), patch(
                    "app.media.runtime.configured_provider"
                ) as constructor:
                    images = build_provider(live_settings())
                    with self.assertRaises(ProviderUnavailable) as caught:
                        await images.generate(PROFILE, TEXT)
                    self.assertIn("IMAGE_API_KEY", str(caught.exception))
                    self.assertNotIn("user-selected-private-model", str(caught.exception))
                    constructor.assert_not_called()

    def test_explicit_configuration_is_forwarded_unchanged_without_generation(self):
        environment = {
            "IMAGE_API_KEY": "explicit-private-key",
            "IMAGE_MODEL": "chosen-image-model-v7",
            "IMAGE_API_BASE": "https://images.example.invalid/custom/v1///",
        }
        settings = live_settings()
        with patch.dict(os.environ, environment, clear=True), patch(
            "app.media.runtime.configured_provider", return_value=sentinel.images
        ) as constructor:
            self.assertIs(build_provider(settings), sentinel.images)
            constructor.assert_called_once_with(
                settings, api_key=environment["IMAGE_API_KEY"], model=environment["IMAGE_MODEL"],
                endpoint="https://images.example.invalid/custom/v1/images/generations",
            )

    def test_demo_delegates_before_reading_any_live_environment(self):
        settings = Settings(mode="demo")
        with patch(
            "app.media.runtime.os.getenv", side_effect=AssertionError("Demo read live configuration")
        ), patch(
            "app.media.runtime.configured_provider", wraps=configured_provider
        ) as constructor:
            images = build_provider(settings)
            self.assertIsInstance(images, DemoImageProvider)
            self.assertTrue(images.is_demo)
            constructor.assert_called_once_with(settings)


if __name__ == "__main__":
    unittest.main()
