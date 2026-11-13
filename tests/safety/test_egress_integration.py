"""Text egress at image/moderation transports; no network or image generation."""

import json

import pytest

from app.media.openai import OpenAIImageProvider
from app.safety.client import HTTPModeration
from app.shared.models import BusinessProfile, TextDraft
from tests.media.test_media import FakeTransport


@pytest.mark.parametrize("local", [True, False])
async def test_image_transport_masks_external_content_and_preserves_local_content(local):
    transport = FakeTransport()
    provider = OpenAIImageProvider(
        api_key="fixture-header-only", model="flux1-dev" if local else "test-model",
        endpoint=("http://127.0.0.1:8194/v1/images/generations" if local
                  else "https://images.example.test/v1/images/generations"),
        allow_local_flux=local, transport=transport, timeout_seconds=1,
    )
    contact = "person@example.org"
    await provider.generate(BusinessProfile(name="Glass", description="Стекло"),
                            TextDraft(text=f"Стекло. Контакт: {contact}", image_brief="Светлый стеклянный фасад"))
    request = transport.calls[0][0]
    prompt = json.loads(request.data)["prompt"]
    assert (contact in prompt) is local
    assert ("[EMAIL]" in prompt) is not local
    assert "fixture-header-only" not in prompt
    assert request.get_header("Authorization") == "Bearer fixture-header-only"


@pytest.mark.parametrize("base,local", [("http://127.0.0.1:8194/v1", True),
                                       ("https://moderation.example.test/v1", False)])
def test_moderation_text_boundary_masks_only_external_content(base, local):
    class Capture:
        request = None

        def send(self, request, timeout):
            self.request = request
            return b'{"allowed":true,"reason":"","category":"safe"}'

    transport = Capture()
    policy = HTTPModeration(base, "fixture-header-only", transport=transport)
    policy.check_text(["Контакт person@example.org, +7 (999) 123-45-67"])
    text = json.loads(transport.request.data)["texts"][0]
    assert ("person@example.org" in text) is local
    assert ("+7 (999) 123-45-67" in text) is local
    assert "fixture-header-only" not in text
