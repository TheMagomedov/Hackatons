"""Review outgoing AI content without treating company sources as generated posts."""

import asyncio

from pydantic import ValidationError

from app.shared.errors import ProviderUnavailable
from app.shared.models import ContentPlan, GeneratedImage, TaskIntent, TextDraft

from .client import MAX_TEXT_CHARACTERS, MAX_TEXT_ITEMS, SafetyPolicy
from .privacy import guard_public_output


def _model(model, value):
    try:
        return model.model_validate(value)
    except (ValueError, TypeError, ValidationError):
        raise ProviderUnavailable("AI вернул некорректный материал.") from None


async def _review_texts(policy: SafetyPolicy, texts: list[str]) -> None:
    batch, length = [], 0
    for text in texts:
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARACTERS:
            raise ProviderUnavailable("AI вернул слишком большой или некорректный текст.")
        if not text.strip():
            continue
        guard_public_output(text)
        if length + len(text) > MAX_TEXT_CHARACTERS or len(batch) >= MAX_TEXT_ITEMS:
            await asyncio.to_thread(policy.check_text, batch)
            batch, length = [], 0
        batch.append(text)
        length += len(text)
    if batch:
        await asyncio.to_thread(policy.check_text, batch)


class ModeratedAI:
    def __init__(self, provider, policy: SafetyPolicy):
        self.provider = provider
        self.policy = policy

    async def build_profile(self, sources):
        # Business facts and source documents are not publication content.
        return await self.provider.build_profile(sources)

    async def interpret_request(self, text, context):
        self.policy.require_configured()
        result = _model(TaskIntent, await self.provider.interpret_request(text, context))
        await _review_texts(self.policy, [result.answer, result.request, *result.questions])
        return result

    async def make_plan(self, profile, request):
        self.policy.require_configured()
        result = _model(ContentPlan, await self.provider.make_plan(profile, request))
        for item in result.items:
            await _review_texts(self.policy, [item.topic, item.goal, item.brief, item.image_brief])
        return result

    async def write_post(self, profile, item):
        self.policy.require_configured()
        result = _model(TextDraft, await self.provider.write_post(profile, item))
        await _review_texts(self.policy, [result.text, result.image_brief, *result.warnings])
        return result

    async def revise_text(self, profile, current_text, feedback):
        self.policy.require_configured()
        result = _model(TextDraft, await self.provider.revise_text(profile, current_text, feedback))
        await _review_texts(self.policy, [result.text, result.image_brief, *result.warnings])
        return result


class ModeratedImages:
    def __init__(self, provider, policy: SafetyPolicy):
        self.provider = provider
        self.policy = policy

    async def generate(self, profile, text, feedback=None):
        self.policy.require_configured()
        result = _model(GeneratedImage, await self.provider.generate(profile, text, feedback))
        await asyncio.to_thread(self.policy.check_image, result.content, result.mime_type)
        return result
