"""Implement these protocols in roles 1, 3 and 4; do not fork the backend."""

from typing import Protocol

from app.shared.models import (
    BusinessProfile,
    ChannelInfo,
    ContentPlan,
    GeneratedImage,
    Notification,
    PlanItem,
    PostView,
    PublicationResult,
    ReportInput,
    SourceInput,
    TaskIntent,
    TextDraft,
)


class AIProvider(Protocol):
    async def build_profile(self, sources: list[SourceInput]) -> BusinessProfile: ...
    async def interpret_request(self, text: str, context: dict) -> TaskIntent: ...
    async def make_plan(self, profile: BusinessProfile, request: str) -> ContentPlan: ...
    async def write_post(self, profile: BusinessProfile, item: PlanItem) -> TextDraft: ...
    async def revise_text(self, profile: BusinessProfile, current_text: str, feedback: str) -> TextDraft: ...


class ImageProvider(Protocol):
    async def generate(
        self, profile: BusinessProfile, text: TextDraft, feedback: str | None = None
    ) -> GeneratedImage: ...


class Publisher(Protocol):
    async def verify_channel(self, channel_id: str, owner_id: int) -> ChannelInfo: ...
    async def publish(self, post: PostView, image: bytes | None, mime_type: str | None) -> PublicationResult: ...
    async def subscriber_count(self, channel_id: str) -> int: ...
    async def close(self) -> None: ...


class Notifier(Protocol):
    async def send(self, notification: Notification) -> None: ...


class ReportProvider(Protocol):
    def build(self, data: ReportInput) -> str: ...
