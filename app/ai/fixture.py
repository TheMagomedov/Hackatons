from datetime import datetime, timedelta

from app.shared.models import (
    BusinessProfile, ContentPlanDraft, ExtractedSource, Fact, PlanItem, TaskIntent, TextDraft,
)

DEMO_NOTES = (
    "Кофейня «Тёплое зерно». В меню завтраков сырники и тосты. "
    "Готовим фильтр-кофе. Стиль общения — тёплый, короткие предложения. "
    "Для иллюстраций подходят кремовые и кофейные оттенки."
)


class FixtureAI:
    """Deterministic test data for the demonstration coffee shop."""

    async def build_profile(self, sources: list[ExtractedSource]) -> BusinessProfile:
        seed = next((s for s in sources if DEMO_NOTES in s.text), None)
        facts = [
            Fact(statement="В меню есть сырники, тосты и фильтр-кофе.", source_ids=[seed.source.id])
        ] if seed else []
        return BusinessProfile(
            name="Тёплое зерно" if seed else "Демонстрационный бизнес",
            description="Тестовый профиль кофейни" if seed else "Тестовый профиль без извлечения фактов",
            products=["сырники", "тосты", "фильтр-кофе"] if seed else [],
            audience="Гости, которые завтракают вне дома", audience_assumption=True,
            style="Тёплый и лаконичный", restrictions=["Не указывать неподтверждённые цены"],
            facts=facts, unknowns=["Цены", "Часы работы"], questions=[],
        )

    async def interpret_request(self, text: str, has_post: bool) -> TaskIntent:
        lower = text.casefold()
        if "отч" in lower:
            return TaskIntent(action="report", brief=text, days=7)
        if any(word in lower for word in ("короче", "исправ", "перепиши", "правк")):
            if has_post:
                return TaskIntent(action="revise_text", brief=text)
            return TaskIntent(action="clarify", brief=text, questions=["Сначала откройте нужный пост."])
        if any(word in lower for word in ("пост", "план", "контент")):
            count = 5 if ("пять" in lower or "5" in lower) else 4 if ("четыре" in lower or "4" in lower) else 3
            return TaskIntent(action="plan", brief=text, count=count)
        return TaskIntent(action="clarify", brief=text, questions=["Уточните задачу для контента."])

    async def make_plan(
        self, profile: BusinessProfile, brief: str, count: int, now: datetime
    ) -> ContentPlanDraft:
        topics = ("Утро с кофе", "Идея завтрака", "Повод заглянуть", "Пауза на кофе", "Утро без спешки")
        return ContentPlanDraft(items=[
            PlanItem(topic=topic, goal="Познакомить гостей с кофейней",
                     takeaway=f"Задача: {brief[:240]}", image_prompt="Кофейная чашка сверху",
                     scheduled_at=(now + timedelta(days=index + 1)).replace(hour=10, minute=0, second=0, microsecond=0))
            for index, topic in enumerate(topics[:count])
        ])

    async def write_post(self, profile: BusinessProfile, item: PlanItem) -> TextDraft:
        detail = "На завтрак есть сырники и тосты. К ним — фильтр-кофе." if profile.facts else ""
        return TextDraft(
            text=f"[ДЕМО] {item.topic}. {detail}".strip(), fact_numbers=[1] if profile.facts else [],
            image_prompt=item.image_prompt, warnings=["Тестовый текст, внешняя модель выключена."],
        )

    async def revise_text(self, profile: BusinessProfile, draft: TextDraft, feedback: str) -> TextDraft:
        text = draft.text.split(".")[0] + "." if "короче" in feedback.casefold() else (
            "[ДЕМО] Доброе утро! " + draft.text.removeprefix("[ДЕМО] ")
        )
        return draft.model_copy(update={"text": text[:950], "fact_numbers": []})
