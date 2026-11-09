"""A small provider check, without a database, bot, images or publication.

Call ``await run_smoke(provider)`` with a configured AIProvider. This helper
does not select credentials, open an event loop or replace the app entrypoint.
The fixed source ID labels this in-memory sample only; production IDs come from
BackendService.
"""

from datetime import UTC, datetime

from app.shared.errors import ProviderUnavailable
from app.shared.models import Source, SourceInput
from app.shared.ports import AIProvider

DEMO_NOTES = (
    "Кофейня «Тёплое зерно». В меню завтраков сырники и тосты. "
    "Готовим фильтр-кофе. Стиль общения — тёплый, короткие предложения. "
    "Для иллюстраций подходят кремовые и кофейные оттенки. "
    "Цены, адрес и часы работы не предоставлены."
)


async def run_smoke(provider: AIProvider) -> dict:
    """Make three AI calls: sample notes -> profile -> 3 topics -> one draft.

    An actual provider incurs its usual API charges. The caller owns closing it.
    The profile is a proposed draft, not a user-confirmed business profile.
    """
    sample = SourceInput(source=Source(
        id="smoke-source",
        business_id="smoke-business",
        kind="text",
        name="Тестовое описание кофейни",
        text=DEMO_NOTES,
        created_at=datetime.now(UTC),
    ))
    profile = await provider.build_profile([sample])
    plan = await provider.make_plan(profile, "Подготовь ровно три разных поста про завтраки.")
    if len(plan.items) != 3:
        raise ProviderUnavailable("Для smoke-проверки ожидаются ровно три темы.")
    draft = await provider.write_post(profile, plan.items[0])
    return {
        "mode": "ai-provider-smoke",
        "profile_confirmed": False,
        "published": False,
        "profile": profile.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "draft": draft.model_dump(mode="json"),
    }
