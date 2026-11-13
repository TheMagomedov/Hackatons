"""Outgoing content is reviewed; internal raw company facts remain available."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.safety import ModeratedAI, ModeratedImages, UnavailableSafety
from app.safety.wrappers import _review_texts
from app.shared.errors import ProviderUnavailable, SafetyBlocked, SafetyUnavailable
from app.shared.models import GeneratedImage, TaskIntent, TextDraft


class RecordingSafety:
    def __init__(self):
        self.texts = []
        self.images = []
        self.text_error = None
        self.image_error = None

    def require_configured(self):
        pass

    def check_text(self, texts):
        self.texts.append(texts)
        if self.text_error:
            raise self.text_error

    def check_image(self, content, mime_type):
        self.images.append((content, mime_type))
        if self.image_error:
            raise self.image_error


async def test_answers_and_draft_preserve_metadata_after_check():
    policy = RecordingSafety()
    answer = TaskIntent(action="answer", answer="Ответ", questions=["Уточните?"])
    draft = TextDraft(text="Подпись", image_brief="Стекло", used_source_ids=["source-1"], warnings=["Проверьте"])
    provider = SimpleNamespace(interpret_request=AsyncMock(return_value=answer),
                               write_post=AsyncMock(return_value=draft))
    wrapped = ModeratedAI(provider, policy)
    assert await wrapped.interpret_request("question", {}) == answer
    assert await wrapped.write_post(None, None) == draft
    assert policy.texts == [["Ответ", "Уточните?"], ["Подпись", "Стекло", "Проверьте"]]


async def test_blocked_generated_draft_never_returns():
    policy = RecordingSafety()
    policy.text_error = SafetyBlocked()
    provider = SimpleNamespace(revise_text=AsyncMock(return_value=TextDraft(text="caption", image_brief="image")))
    with pytest.raises(SafetyBlocked):
        await ModeratedAI(provider, policy).revise_text(None, "old", "feedback")
    assert provider.revise_text.await_count == 1


async def test_raw_company_sources_are_not_treated_as_outgoing_post():
    policy = RecordingSafety()
    policy.text_error = SafetyBlocked()
    provider = SimpleNamespace(build_profile=AsyncMock(return_value={"profile": "clinical facts"}))
    sources = ["raw company facts"]
    assert await ModeratedAI(provider, policy).build_profile(sources) == {"profile": "clinical facts"}
    provider.build_profile.assert_awaited_once_with(sources)
    assert not policy.texts


async def test_missing_policy_blocks_before_paid_generation():
    provider = SimpleNamespace(write_post=AsyncMock(), generate=AsyncMock())
    with pytest.raises(SafetyUnavailable):
        await ModeratedAI(provider, UnavailableSafety()).write_post(None, None)
    with pytest.raises(SafetyUnavailable):
        await ModeratedImages(provider, UnavailableSafety()).generate(None, "caption")
    provider.write_post.assert_not_awaited()
    provider.generate.assert_not_awaited()


async def test_generated_image_denied_before_return():
    policy = RecordingSafety()
    policy.image_error = SafetyBlocked()
    image = GeneratedImage(content=b"image-data", mime_type="image/png", provider="fake")
    provider = SimpleNamespace(generate=AsyncMock(return_value=image))
    with pytest.raises(SafetyBlocked):
        await ModeratedImages(provider, policy).generate(None, "caption")
    assert policy.images == [(image.content, image.mime_type)]


async def test_large_lists_split_at_real_gateway_limits():
    policy = RecordingSafety()
    texts = ["x" * 800] * 32
    await _review_texts(policy, texts)
    assert [item for batch in policy.texts for item in batch] == texts
    assert all(len(batch) <= 16 and sum(map(len, batch)) <= 12000 for batch in policy.texts)


async def test_malformed_provider_result_has_no_raw_fields():
    provider = SimpleNamespace(write_post=AsyncMock(return_value={"text": "private-provider-body"}))
    with pytest.raises(ProviderUnavailable) as error:
        await ModeratedAI(provider, RecordingSafety()).write_post(None, None)
    assert "private-provider-body" not in str(error.value)
