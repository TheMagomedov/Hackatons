"""Complete report delivery at the shared Telegram/VK single-message boundary."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from app.reports.provider import PlainTextReportProvider
from app.shared.errors import InvalidInput
from app.shared.models import MetricSnapshot, ReportInput
from app.vk.client import VKClient

FINAL_CAVEAT = "Просмотры, охват, пересылки, клики и продажи: нет данных."


def report_input(variant):
    """Synthetic counter streams, with source labels chosen to exercise the limit."""
    end = datetime(2026, 11, 14, tzinfo=UTC)
    sources = [f"telegram:-1001234567{i:03}" for i in range(17)]
    if variant != "4073":
        sources[:4] = ["src0", "src1", "src2", "source3"]
    if variant == "4001":
        sources[3] = "source-3"
    elif variant == "4000-astral":
        sources[0] = "🌿xx"
    elif variant == "4001-astral":
        sources[0] = "🌿xxx"
    metrics = [
        MetricSnapshot(
            business_id="offline-business", name="subscribers", value=i + 10,
            observed_at=end - timedelta(days=8 if baseline else 1),
            source=source, event_id=f"offline-{i}-{baseline}",
        )
        for i, source in enumerate(sources)
        for baseline in (False, True)
    ]
    return ReportInput(
        business_id="offline-business", start=end - timedelta(days=7), end=end,
        posts=[], metrics=metrics,
    )


@pytest.fixture
async def wire(monkeypatch):
    attempts, messages = [], []

    def deny_network(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Report delivery must use the injected HTTP transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_network)
    monkeypatch.setattr("socket.create_connection", deny_network)
    monkeypatch.setattr("socket.socket.connect", deny_network)
    monkeypatch.setattr("socket.socket.connect_ex", deny_network)

    def transport(request):
        assert request.method == "POST" and request.url == "https://api.vk.com/method/messages.send"
        payload = parse_qs(request.content.decode())
        assert payload["access_token"] == ["offline-test-key"]
        messages.append(payload["message"][0])
        return httpx.Response(200, json={"response": 1})

    client = VKClient("offline-test-key", 123, transport=httpx.MockTransport(transport))
    try:
        yield client, messages
    finally:
        await client.close()
        assert attempts == []


@pytest.mark.parametrize("variant,characters", [("4000", 4000), ("4000-astral", 3999)])
async def test_report_at_message_limit_reaches_vk_intact(wire, variant, characters):
    client, messages = wire
    text = PlainTextReportProvider().build(report_input(variant))
    assert len(text) == characters
    assert len(text.encode("utf-16-le")) // 2 == 4000
    assert text.endswith(FINAL_CAVEAT)

    await client.send(9001, text, "offline-report")

    assert messages == [text]
    assert messages[0].endswith(FINAL_CAVEAT)


@pytest.mark.parametrize("variant", ["4001", "4073", "4001-astral"])
async def test_oversized_report_is_rejected_before_vk_transport(wire, variant):
    client, messages = wire
    with pytest.raises(InvalidInput, match="Отчёт превышает лимит одного сообщения"):
        text = PlainTextReportProvider().build(report_input(variant))
        await client.send(9001, text, "offline-report")
    assert messages == []
