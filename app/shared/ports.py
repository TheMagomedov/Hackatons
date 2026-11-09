from datetime import datetime
from pathlib import Path
from typing import Protocol

from .models import (
    BusinessProfile, Channel, ContentPlanDraft, ExtractedSource, GeneratedImage,
    PlanItem, PostView, PublicationResult, TaskIntent, TextDraft,
)


class AppError(Exception):
    """A safe explanation suitable for the Telegram owner."""


class DeliveryUnknown(AppError):
    """Telegram may have accepted the send request."""


class ContentAI(Protocol):
    async def build_profile(self, sources: list[ExtractedSource]) -> BusinessProfile: ...
    async def interpret_request(self, text: str, has_post: bool) -> TaskIntent: ...
    async def make_plan(
        self, profile: BusinessProfile, brief: str, count: int, now: datetime
    ) -> ContentPlanDraft: ...
    async def write_post(self, profile: BusinessProfile, item: PlanItem) -> TextDraft: ...
    async def revise_text(
        self, profile: BusinessProfile, draft: TextDraft, feedback: str
    ) -> TextDraft: ...


class ImageGenerator(Protocol):
    async def generate(
        self, profile: BusinessProfile, item: PlanItem, draft: TextDraft,
        feedback: str = "", current_image: bytes | None = None,
    ) -> GeneratedImage: ...


class Publisher(Protocol):
    async def check_channel(self, channel: int | str) -> Channel: ...
    async def publish(self, post: PostView, image_path: Path) -> PublicationResult: ...
    async def verify_message(self, channel: Channel, message_id: int) -> PublicationResult: ...
    async def subscriber_count(self, channel: Channel) -> int: ...


class Notifier(Protocol):
    async def notify(self, owner_id: int, message: str) -> None: ...
