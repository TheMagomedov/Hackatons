"""Wire contracts. IDs and persistence belong to the backend (role 2)."""

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Fact(Contract):
    text: str = Field(min_length=1, max_length=2000)
    source_ids: list[str] = Field(default_factory=list)


class Source(Contract):
    id: str
    business_id: str
    kind: Literal["text", "pdf", "url", "docx"]
    name: str
    asset_id: str | None = None
    text: str | None = None
    url: str | None = None
    created_at: AwareDatetime


class SourceInput(Contract):
    """Runtime input: metadata plus bytes, never a developer-specific local path."""

    source: Source
    content: bytes | None = None


class BusinessProfile(Contract):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=5000)
    products: list[str] = Field(default_factory=list)
    audience: str = ""
    tone: str = "Дружелюбный, конкретный"
    constraints: list[str] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)


class Business(Contract):
    id: str
    owner_id: int
    archived: bool = False
    ai_daily_limit: int | None = Field(default=None, ge=0, le=10000)
    name: str
    timezone: str
    channel_id: str | None = None
    channel_title: str | None = None
    profile: BusinessProfile | None = None
    profile_version: int = 0
    profile_confirmed: bool = False
    automation_enabled: bool = False
    publications_paused: bool = False
    automation_request: str = "Подготовь один полезный пост о бизнесе"
    interval_hours: int = 24
    next_generation_at: AwareDatetime | None = None
    created_at: AwareDatetime


class TaskIntent(Contract):
    action: Literal["prepare", "revise_text", "revise_image", "report", "clarify", "answer"]
    request: str = Field(default="", max_length=4000)
    post_id: str | None = None
    questions: list[str] = Field(default_factory=list, max_length=5)
    answer: str = Field(default="", max_length=4000)


class PlanItem(Contract):
    topic: str = Field(min_length=1, max_length=300)
    goal: str = Field(min_length=1, max_length=1000)
    brief: str = Field(min_length=1, max_length=4000)
    image_brief: str = Field(min_length=1, max_length=4000)
    suggested_at: AwareDatetime | None = None


class ContentPlan(Contract):
    items: list[PlanItem] = Field(min_length=1, max_length=5)


class TextDraft(Contract):
    # A deliberately conservative MVP limit: one photo with a plain-text caption.
    text: str = Field(min_length=1, max_length=900)
    image_brief: str = Field(min_length=1, max_length=4000)
    used_source_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GeneratedImage(Contract):
    content: bytes = Field(min_length=1, max_length=10_000_000)
    mime_type: Literal["image/png", "image/jpeg"]
    provider: str
    is_demo: bool = False


class Asset(Contract):
    id: str
    business_id: str
    filename: str
    mime_type: str
    size: int
    sha256: str
    created_at: AwareDatetime
    is_demo: bool = False
    archived: bool = False


class VisualDraft(Contract):
    asset: Asset
    draft: TextDraft
    request: str = Field(min_length=1, max_length=4000)
    profile_version: int = Field(ge=0)


class PostStatus(StrEnum):
    REVIEW = "review"
    EDITING = "editing"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    REJECTED = "rejected"
    FAILED = "failed"
    DELIVERY_UNKNOWN = "delivery_unknown"


class PostView(Contract):
    id: str
    business_id: str
    job_id: str | None = None
    revision: int
    text: str
    image_asset_id: str | None
    channel_id: str
    scheduled_at: AwareDatetime | None
    status: PostStatus
    approved_revision: int | None = None
    approved_by: int | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    message_id: int | None = None
    message_url: str | None = None
    published_at: AwareDatetime | None = None
    last_error: str | None = None
    is_demo: bool = False
    # None means legacy content whose original business context is unknown.
    profile_version: int | None = Field(default=None, ge=0)
    used_source_ids: list[str] = Field(default_factory=list, max_length=100)
    warnings: list[str] = Field(default_factory=list, max_length=50)
    # How the initial draft was authored; later edits keep this provenance.
    content_origin: Literal["manual", "generated", "legacy"] = "legacy"
    created_by: int | None = Field(default=None, strict=True, gt=0)


class ChannelInfo(Contract):
    id: str
    title: str


class PublicationResult(Contract):
    channel_id: str
    message_id: int = Field(gt=0)
    message_url: str | None = None


class Job(Contract):
    id: str
    business_id: str
    kind: Literal["prepare", "visual"] = "prepare"
    requested_by: int | None = Field(default=None, strict=True, gt=0)
    caption: str | None = Field(default=None, max_length=900)
    profile_version: int | None = None
    visual_result: VisualDraft | None = None
    request: str
    status: Literal["pending", "running", "done", "failed", "cancelled"]
    dedup_key: str
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error: str | None = None
    stage: Literal["queued", "plan", "text", "image", "saving", "done", "failed", "cancelled"] = "queued"
    processed: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    updated_at: AwareDatetime | None = None


class Notification(Contract):
    id: str
    business_id: str
    owner_id: int
    kind: str
    text: str
    post_id: str | None = None
    job_id: str | None = None
    created_at: AwareDatetime


class MetricSnapshot(Contract):
    business_id: str
    name: Literal["reactions", "subscribers"]
    value: int = Field(ge=0)
    observed_at: AwareDatetime
    source: str
    post_id: str | None = None
    event_id: str


class ReportInput(Contract):
    business_id: str
    start: AwareDatetime
    end: AwareDatetime
    posts: list[PostView]
    metrics: list[MetricSnapshot]
