from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

CAPTION_LIMIT = 950


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(Contract):
    id: int
    kind: Literal["website", "pdf", "notes"]
    label: str


class ExtractedSource(Contract):
    source: Source
    text: str


class Fact(Contract):
    statement: str = Field(min_length=1, max_length=300)
    source_ids: list[int] = Field(min_length=1)


class BusinessProfile(Contract):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    products: list[str] = Field(max_length=8)
    audience: str = Field(max_length=300)
    audience_assumption: bool
    style: str = Field(max_length=300)
    restrictions: list[str] = Field(max_length=8)
    facts: list[Fact] = Field(max_length=12)
    unknowns: list[str] = Field(max_length=8)
    questions: list[str] = Field(max_length=5)


class TaskIntent(Contract):
    action: Literal["plan", "revise_text", "report", "clarify"]
    brief: str = Field(max_length=2000)
    count: int | None = Field(default=None, ge=3, le=5)
    days: int | None = Field(default=None, ge=1, le=90)
    questions: list[str] = Field(default_factory=list, max_length=5)


class PlanItem(Contract):
    topic: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=160)
    takeaway: str = Field(min_length=1, max_length=300)
    image_prompt: str = Field(min_length=1, max_length=700)
    scheduled_at: AwareDatetime


class ContentPlanDraft(Contract):
    items: list[PlanItem] = Field(min_length=3, max_length=5)


class TextDraft(Contract):
    text: str = Field(min_length=1, max_length=CAPTION_LIMIT)
    fact_numbers: list[int] = Field(default_factory=list, max_length=12)
    image_prompt: str = Field(min_length=1, max_length=700)
    warnings: list[str] = Field(default_factory=list, max_length=5)


class GeneratedImage(Contract):
    data: bytes = Field(min_length=1, max_length=9_000_000)
    format: Literal["png", "jpeg"]
    provider: str


class Channel(Contract):
    id: int
    title: str
    username: str | None = None


class Status(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    SENDING = "sending"
    PUBLISHED = "published"
    REJECTED = "rejected"
    FAILED = "failed"
    DELIVERY_UNKNOWN = "delivery_unknown"


class PostView(Contract):
    id: int
    version: int
    plan_id: int
    draft: TextDraft
    image_id: str
    channel: Channel
    scheduled_at: AwareDatetime
    status: Status
    error: str | None = None
    link: str | None = None


class PublicationResult(Contract):
    message_id: int = Field(gt=0)
    link: str | None
    published_at: AwareDatetime


class ReactionSnapshot(Contract):
    channel_id: int
    message_id: int
    measured_at: AwareDatetime
    source: str
    total: int = Field(ge=0)


class SubscriberSnapshot(Contract):
    channel_id: int
    measured_at: AwareDatetime
    source: str
    total: int = Field(ge=0)


class ReportData(Contract):
    start: datetime
    end: datetime
    prepared: int
    awaiting_approval: int
    scheduled: int
    published: int
    rejected: int
    errors: int
    reactions: int | None
    posts_with_reactions: int
    subscriber_change: int | None
    subscriber_start: SubscriberSnapshot | None
    subscriber_end: SubscriberSnapshot | None
