import os
from datetime import datetime
from zoneinfo import ZoneInfo

from app.ai.client import Provider, structured
from app.ai.sources import extract_source
from app.config import Settings
from app.shared.models import BusinessProfile, ContentPlan, PlanItem, SourceInput, TaskIntent, TextDraft


class ModelAI:
    def __init__(self, provider: Provider, model: str, settings: Settings):
        self.provider = provider
        self.model = model
        self.timezone = ZoneInfo(settings.timezone)
        self.max_posts = settings.max_posts_per_job

    async def build_profile(self, sources: list[SourceInput]) -> BusinessProfile:
        extracted = [await extract_source(item) for item in sources]
        ids = {item.source.id for item in sources}
        return await structured(
            self.provider, self.model, BusinessProfile,
            "Опиши бизнес. У каждого подтверждённого факта укажи source_ids из входных данных. "
            "Выводы об аудитории пометь как assumptions. По противоречиям задай questions.",
            {"sources": extracted},
            lambda profile: all(fact.source_ids and set(fact.source_ids) <= ids for fact in profile.facts),
        )

    async def interpret_request(self, text: str, context: dict) -> TaskIntent:
        selected = context.get("selected_post_id")
        return await structured(
            self.provider, self.model, TaskIntent,
            "Определи намерение: prepare, revise_text, revise_image, report или clarify. "
            "Для правки без выбранного поста или запроса на публикацию задай уточняющий вопрос. "
            "request должен сохранять просьбу пользователя. Не придумывай post_id.",
            {"text": text, "selected_post_id": selected},
            lambda intent: intent.post_id in (None, selected),
        )

    async def make_plan(self, profile: BusinessProfile, request: str) -> ContentPlan:
        now = datetime.now(self.timezone)
        return await structured(
            self.provider, self.model, ContentPlan,
            f"Предложи до {self.max_posts} различных публикаций. suggested_at должен быть в будущем "
            "со смещением часового пояса или null. Учитывай цель и ограничения бизнеса.",
            {"profile": profile.model_dump(mode="json"), "request": request, "now": now.isoformat()},
            lambda plan: len(plan.items) <= self.max_posts and len({
                item.topic.strip().casefold() for item in plan.items
            }) == len(plan.items),
        )

    async def write_post(self, profile: BusinessProfile, item: PlanItem) -> TextDraft:
        return await self._draft(profile, {"item": item.model_dump(mode="json")}, "Напиши пост по плану.")

    async def revise_text(self, profile: BusinessProfile, current_text: str, feedback: str) -> TextDraft:
        return await self._draft(
            profile, {"current_text": current_text, "feedback": feedback},
            "Исправь пост по замечанию, сохраняя подтверждённые факты и остальные требования.",
        )

    async def _draft(self, profile, data, task):
        ids = {source_id for fact in profile.facts for source_id in fact.source_ids}
        return await structured(
            self.provider, self.model, TextDraft,
            task + " До 900 символов, обычный текст. Укажи used_source_ids и image_brief. "
            "Если фактов недостаточно, укажи это в warnings.",
            {"profile": profile.model_dump(mode="json"), **data},
            lambda draft: 0 < len(draft.text.strip().encode("utf-16-le")) // 2 <= 900
            and set(draft.used_source_ids) <= ids,
        )

    async def close(self) -> None:
        await self.provider.close()


def build_provider(settings: Settings) -> ModelAI:
    provider = Provider(
        os.getenv("TEXT_API_BASE", "https://api.openai.com/v1"),
        os.getenv("TEXT_API_KEY", "").strip(), "TEXT_API_KEY", settings.provider_timeout_seconds,
    )
    return ModelAI(provider, os.getenv("TEXT_MODEL") or "gpt-4.1-mini", settings)
