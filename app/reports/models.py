"""Report output only. All input DTOs belong to ``app.shared.models``."""

from dataclasses import dataclass
from datetime import datetime

from app.shared.models import PostStatus


@dataclass(frozen=True)
class ActivitySummary:
    """Unique post events in the period, alongside explicitly current states.

    The backend provides one current PostView per post, not version history.
    We therefore count posts, never revisions or historical failure events.
    Current states include all supplied posts, even posts outside the period.
    """

    is_demo: bool
    created_posts: int
    published_posts: int
    current_states: tuple[tuple[PostStatus, int], ...]


@dataclass(frozen=True)
class CounterReading:
    """Latest known cumulative value of one (name, post_id, source) stream.

    ``observed_at`` can precede the period if only a baseline exists. ``delta``
    is available only with a strictly pre-start baseline and an in-period
    observation of this same stream. A negative delta is valid. Sources and
    demo/real cohorts are never combined. Equal-time conflicting values are
    rejected instead of guessing which event happened last.
    """

    name: str
    source: str
    is_demo: bool
    post_id: str | None
    value: int
    observed_at: datetime
    baseline_value: int | None
    baseline_at: datetime | None
    delta: int | None


@dataclass(frozen=True)
class MetricSummary:
    """Per-source/cohort sum of latest counters, with explicit completeness.

    Reactions use at most one latest counter per post. Subscriber groups have
    a single stream. ``delta`` sums only streams having both baseline and
    in-period data; ``delta_streams`` makes partial changes explicit. Snapshot
    times need not coincide, so these are latest-known values, not an exact
    simultaneous measurement at period end.
    """

    name: str
    source: str
    is_demo: bool
    value: int
    observed_streams: int
    eligible_streams: int
    observed_from: datetime
    observed_to: datetime
    delta: int | None
    delta_streams: int
    baseline_from: datetime | None
    baseline_to: datetime | None


@dataclass(frozen=True)
class Report:
    """Inclusive-period report from a backend snapshot, without invented data."""

    start: datetime
    end: datetime
    activities: tuple[ActivitySummary, ...]
    counters: tuple[CounterReading, ...]
    metrics: tuple[MetricSummary, ...]
    warnings: tuple[str, ...]
