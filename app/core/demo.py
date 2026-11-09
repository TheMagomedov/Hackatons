"""Explicitly fake, offline adapters for integration tests and a credential-free demo.

These are NOT implementations of roles 1/4 and must never be selected in live mode.
"""

import base64
from datetime import timedelta
from pathlib import Path

from app.shared.errors import Conflict, DeliveryRejected, Forbidden
from app.shared.models import (
    BusinessProfile,
    ChannelInfo,
    ContentPlan,
    Fact,
    GeneratedImage,
    Notification,
    PlanItem,
    PostStatus,
    PublicationResult,
    ReportInput,
    SourceInput,
    TaskIntent,
    TextDraft,
)
from app.shared.time import utc_now

# One transparent pixel is only a test fixture, never a marketing deliverable.
DEMO_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jO2sAAAAASUVORK5CYII="
)


class DemoAI:
    is_demo = True

    async def build_profile(self, sources: list[SourceInput]) -> BusinessProfile:
        return BusinessProfile(
            name="Демо-бизнес",
            description="Явно обозначенные тестовые данные, без вызова AI.",
            facts=[Fact(text="Демонстрационный бизнес", source_ids=[sources[0].source.id])],
        )

    async def interpret_request(self, text: str, context: dict) -> TaskIntent:
        return TaskIntent(action="prepare", request=text)

    async def make_plan(self, profile: BusinessProfile, request: str) -> ContentPlan:
        return ContentPlan(
            items=[
                PlanItem(
                    topic="Тестовый пост",
                    goal="Проверка backend",
                    brief="Проверить согласование и очередь",
                    image_brief="Тестовое изображение",
                )
            ]
        )

    async def write_post(self, profile: BusinessProfile, item: PlanItem) -> TextDraft:
        return TextDraft(
            text="[ДЕМО] Проверяем подготовку, согласование и публикацию. Этот текст не создан AI.",
            image_brief=item.image_brief,
        )

    async def revise_text(self, profile: BusinessProfile, current_text: str, feedback: str) -> TextDraft:
        return TextDraft(text=f"[ДЕМО: правка] {feedback[:700]}", image_brief="Тестовая иллюстрация")


class DemoImages:
    is_demo = True

    async def generate(self, profile, text, feedback=None) -> GeneratedImage:
        return GeneratedImage(
            content=DEMO_PNG, mime_type="image/png", provider="offline-test-fixture", is_demo=True
        )


class DemoPublisher:
    is_demo = True

    def __init__(self):
        self.sent = []

    async def verify_channel(self, channel_id: str, owner_id: int) -> ChannelInfo:
        return ChannelInfo(id="-1001234567890", title="Offline demo channel")

    async def publish(self, post, image, mime_type) -> PublicationResult:
        if not post.is_demo:
            raise DeliveryRejected("DemoPublisher принимает только явно помеченные тестовые публикации.")
        self.sent.append(post)
        return PublicationResult(channel_id=post.channel_id, message_id=100 + len(self.sent))

    async def subscriber_count(self, channel_id: str) -> int:
        return 42  # Explicit demo metric; source is marked 'demo' by BackendService.

    async def close(self):
        pass


class DemoNotifier:
    is_demo = True

    def __init__(self):
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)


class DemoReporter:
    is_demo = True

    def build(self, data: ReportInput) -> str:
        published = sum(
            p.published_at is not None and data.start <= p.published_at <= data.end for p in data.posts
        )
        return f"[OFFLINE DEMO] Опубликовано: {published}. Все внешние сервисы заменены тестовыми адаптерами."


async def run_demo(data_dir: Path) -> dict:
    from app.config import Settings
    from app.core.service import BackendService
    from app.storage import FileStore, Repository

    now = [utc_now()]
    settings = Settings(data_dir=data_dir, mode="demo")
    repo = Repository(data_dir / "backend.sqlite3")
    repo.initialize()
    repo.bind_runtime("demo", settings.owner_id)
    files = FileStore(data_dir / "assets", max_bytes=settings.max_upload_bytes)
    publisher, notifier = DemoPublisher(), DemoNotifier()
    service = BackendService(
        repo,
        files,
        settings,
        DemoAI(),
        DemoImages(),
        publisher,
        notifier,
        DemoReporter(),
        clock=lambda: now[0],
    )
    business = service.create_business(1, "Демонстрационная кофейня")
    service.add_text_source(1, business.id, "Тестовое описание, не реальные коммерческие сведения.")
    await service.analyze_business(1, business.id)
    service.confirm_profile(1, business.id, 1)
    await service.connect_channel(1, business.id, "@demo_channel")
    job = service.request_preparation(1, business.id, "Один тестовый пост", "demo-request")
    duplicate = service.request_preparation(1, business.id, "Один тестовый пост", "demo-request")
    await service.prepare_next_job()
    post = service.get_job_posts(1, job.id)[0]
    post = service.edit_post(1, post.id, post.revision, scheduled_at=now[0] + timedelta(minutes=1))
    post = service.approve_post(1, post.id, post.revision)
    old_revision = post.revision
    post = service.edit_post(
        1, post.id, post.revision, text="[ДЕМО] Обновлённая версия требует нового согласования."
    )
    stale_blocked = False
    try:
        service.approve_post(1, post.id, old_revision)
    except Conflict:
        stale_blocked = True
    forbidden_blocked = False
    try:
        service.approve_post(2, post.id, post.revision)
    except Forbidden:
        forbidden_blocked = True
    service.approve_post(1, post.id, post.revision)
    # New repository/service instances verify persisted scheduling, not only in-memory state.
    restored = Repository(data_dir / "backend.sqlite3")
    restored.initialize()
    service.repo = restored
    service.recover_interrupted()
    now[0] += timedelta(minutes=2)
    await service.publish_due_once()
    await service.publish_due_once()
    service.configure_automation(1, business.id, True, first_run_at=now[0] + timedelta(minutes=1))
    now[0] += timedelta(minutes=2)
    scheduled = service.schedule_automations()
    repeated = service.schedule_automations()
    await service.prepare_next_job()
    await service.publish_due_once()  # New drafts are deliberately NOT approved.
    await service.collect_metrics()
    await service.dispatch_notifications()
    posts = service.list_posts(1, business.id)
    return {
        "mode": "offline-demo",
        "external_requests": 0,
        "database": str(data_dir / "backend.sqlite3"),
        "request_deduplicated": duplicate.id == job.id,
        "stale_approval_blocked": stale_blocked,
        "unauthorized_approval_blocked": forbidden_blocked,
        "persisted_publication_completed": service.get_post(1, post.id).status == PostStatus.PUBLISHED,
        "simulated_publications": len(publisher.sent),
        "automation_jobs_created": scheduled,
        "duplicate_automation_jobs": repeated,
        "unapproved_drafts": sum(p.status == PostStatus.REVIEW for p in posts),
        "notifications_delivered": len(notifier.sent),
    }
