"""Mini App graph and reporter must agree on independent, inclusive event times."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.core.runtime import build_runtime
from app.miniapp.server import create_app
from app.reports.provider import PlainTextReportProvider
from app.shared.models import PostStatus, PostView, ReportInput
from tests.test_miniapp import OWNER, TOKEN, signed_data


@pytest.mark.parametrize("timezone", ["Europe/Moscow", "America/New_York"])
async def test_graph_matches_report_at_exact_period_boundaries(tmp_path, monkeypatch, timezone):
    runtime = await build_runtime(Settings(owner_id=OWNER, data_dir=tmp_path))
    service = runtime.service
    end = datetime(2026, 11, 13, 12, tzinfo=UTC)
    local_end = end.astimezone(ZoneInfo(timezone))
    start = (local_end - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    tick = timedelta(microseconds=1)
    service.clock = lambda: end
    service.reporter = PlainTextReportProvider()
    try:
        business = service.create_business(OWNER, "Границы отчёта", timezone)

        def post(identifier, created, published=None, *, demo=False):
            return PostView(
                id=identifier, business_id=business.id, revision=1, text="Синтетический пост",
                image_asset_id="unused-fixture", channel_id="unused-fixture", scheduled_at=None,
                created_at=created, updated_at=published or created, published_at=published,
                status=PostStatus.PUBLISHED if published else PostStatus.REVIEW, is_demo=demo,
            )

        # ReportInput intentionally includes all posts, not only creations in the period.
        # Future timestamps are synthetic boundary cases, not claims of live observations.
        posts = [
            post("created-before-sent-at-start", start - tick, start),
            post("created-at-start-sent-after-end", start, end + tick),
            post("created-earlier-sent-at-end", end - timedelta(hours=1), end),
            post("created-at-end", end),
            post("created-after-end", end + tick),
            post("demo", end, end, demo=True),
            post("old-publication", start - timedelta(days=2), start - tick),
        ]

        def report_input(business_id, report_start, report_end):
            assert (business_id, report_start, report_end) == (business.id, start, end)
            return ReportInput(business_id=business.id, start=start, end=end, posts=posts, metrics=[])

        monkeypatch.setattr(service.repo, "report_input", report_input)
        async with TestClient(TestServer(create_app(service, token=TOKEN))) as client:
            response = await client.get(
                f"/mini/api/business/{business.id}/report",
                headers={"X-Telegram-Init-Data": signed_data(end)},
            )
            assert response.status == 200
            result = await response.json()
        assert len(result["series"]) == 7
        assert result["series"][0] == {"label": start.strftime("%d.%m"), "created": 1, "sent": 1}
        assert result["series"][-1] == {"label": local_end.strftime("%d.%m"), "created": 2, "sent": 1}
        assert sum(row["created"] for row in result["series"]) == 3
        assert sum(row["sent"] for row in result["series"]) == 2
        assert "Реальные посты:\nСоздано за период: 3\nОпубликовано за период: 2" in result["detail"]
        assert "Демо-посты:\nСоздано за период: 1\nОпубликовано за период: 1" in result["detail"]
        assert "Реакции и подписчики: нет данных." in result["detail"]
        assert service.publisher.sent == []
    finally:
        await runtime.close()
