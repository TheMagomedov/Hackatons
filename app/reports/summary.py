from collections import Counter
from datetime import datetime

from app.shared.models import ReactionSnapshot, ReportData, SubscriberSnapshot


def summarize(
    start: datetime, end: datetime, events: list[str], states: list[str],
    reactions: list[ReactionSnapshot], subscribers: list[SubscriberSnapshot],
) -> ReportData:
    activity = Counter(events)
    current = Counter(states)
    latest: dict[tuple[int, int], ReactionSnapshot] = {}
    for sample in sorted(reactions, key=lambda s: s.measured_at):
        if start <= sample.measured_at <= end:
            latest[sample.channel_id, sample.message_id] = sample
    ending = max((s for s in subscribers if start <= s.measured_at <= end),
                 key=lambda s: s.measured_at, default=None)
    baseline = None
    if ending:
        baseline = max(
            (s for s in subscribers if s.measured_at <= start and s.measured_at < ending.measured_at
             and s.channel_id == ending.channel_id and s.source == ending.source),
            key=lambda s: s.measured_at, default=None,
        )
    return ReportData(
        start=start, end=end, prepared=activity["prepared"], awaiting_approval=current["draft"],
        scheduled=current["scheduled"] + current["sending"], published=activity["published"],
        rejected=activity["rejected"], errors=activity["error"],
        reactions=sum(s.total for s in latest.values()) if latest else None,
        posts_with_reactions=len(latest),
        subscriber_change=ending.total - baseline.total if baseline and ending else None,
        subscriber_start=baseline, subscriber_end=ending,
    )
