"""Offline tools must never report real data as externally published."""

import asyncio
from datetime import UTC, datetime

import pytest

from app.core.demo import DEMO_PNG, DemoPublisher, run_demo
from app.shared.errors import Conflict, DeliveryRejected
from app.shared.models import PostStatus, PostView
from app.storage import Repository


def test_demo_publisher_cannot_produce_receipt_for_real_post():
    publisher = DemoPublisher()
    now = datetime(2026, 11, 9, tzinfo=UTC)
    post = PostView(
        id="real-post",
        business_id="real-business",
        text="Real content",
        channel_id="-1001234567890",
        image_asset_id="real-image",
        revision=1,
        scheduled_at=now,
        status=PostStatus.PUBLISHING,
        created_at=now,
        updated_at=now,
        is_demo=False,
    )
    with pytest.raises(DeliveryRejected):
        asyncio.run(publisher.publish(post, DEMO_PNG, "image/png"))
    assert publisher.sent == []


def test_full_demo_refuses_live_directory_before_creating_demo_business(tmp_path):
    repo = Repository(tmp_path / "backend.sqlite3")
    repo.initialize()
    repo.bind_runtime("live", 1)
    before = repo.diagnostics()
    with pytest.raises(Conflict):
        asyncio.run(run_demo(tmp_path))
    assert repo.diagnostics() == before
    assert not (tmp_path / "assets").exists()
