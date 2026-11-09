from datetime import UTC, datetime, timedelta

import pytest

from app.media.generator import FixtureImages, normalize_image
from app.reports.summary import summarize
from app.shared.models import ReactionSnapshot, SubscriberSnapshot
from app.shared.ports import AppError


def test_reactions_use_latest_snapshots_and_zero_is_a_measurement():
    end = datetime(2026, 11, 9, tzinfo=UTC)
    start = end - timedelta(days=7)

    def reaction(message_id, time, total):
        return ReactionSnapshot(
            channel_id=-1001, message_id=message_id, measured_at=time, source="test", total=total,
        )

    old = reaction(1, start, 2)
    new = reaction(1, end, 5)
    zero = reaction(2, end, 0)
    report = summarize(start, end, ["prepared", "published"], ["draft"], [new, old, zero], [])
    assert (report.reactions, report.posts_with_reactions) == (5, 2)
    assert (report.prepared, report.published, report.awaiting_approval) == (1, 1, 1)
    assert summarize(start, end, [], [], [zero], []).reactions == 0
    assert summarize(start, end, [], [], [], []).reactions is None


def test_subscriber_change_needs_comparable_boundary_samples():
    end = datetime(2026, 11, 9, tzinfo=UTC)
    start = end - timedelta(days=7)
    latest = SubscriberSnapshot(channel_id=1, source="test", measured_at=end, total=12)
    wrong = latest.model_copy(update={"channel_id": 2, "measured_at": start, "total": 8})
    first = latest.model_copy(update={"measured_at": start - timedelta(hours=1), "total": 10})
    assert summarize(start, end, [], [], [], [latest, wrong]).subscriber_change is None
    assert summarize(start, end, [], [], [], [latest, first, wrong]).subscriber_change == 2


def test_image_normalization_and_revisions():
    original = FixtureImages._draw("Завтрак", "", None)
    updated = FixtureImages._draw("Завтрак", "Измени фон", original.data)
    assert original.data != updated.data
    assert normalize_image(updated.data, "test").format == "jpeg"
    with pytest.raises(AppError, match="изображение"):
        normalize_image(b"broken image", "test")
