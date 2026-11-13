"""The shipped AI, image and report adapters together, with HTTP simulated."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest

from app.ai.client import AISettings, ChatJSONClient
from app.ai.local import LocalAISettings, OllamaJSONClient
from app.ai.provider import ModelAI
from app.config import Settings
from app.core.demo import DemoNotifier, DemoPublisher
from app.core.service import BackendService
from app.media import DemoImageProvider, MediaLimits
from app.media.validation import validate_png
from app.reports.provider import build_provider as build_report_provider
from app.reports.service import build_report
from app.shared.models import PostStatus, TextDraft
from app.storage import FileStore, Repository
from tests.ai.test_integration import ModelFixture


class OllamaModelFixture(ModelFixture):
    """Reuse the synthetic content through Ollama's native local-only handshake."""

    def __init__(self):
        super().__init__()
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.host == "127.0.0.1"
        assert "authorization" not in request.headers
        if request.url.path == "/api/status":
            assert request.method == "GET" and not request.content
            return httpx.Response(200, json={"cloud": {"disabled": True}})
        if request.url.path == "/api/show":
            assert json.loads(request.content) == {"model": "offline-test-model", "verbose": False}
            return httpx.Response(200, json={
                "details": {"format": "gguf"},
                "model_info": {"general.architecture": "test-fixture"},
            })
        assert request.url.path == "/api/chat" and request.method == "POST"
        content = super().__call__(request).json()["choices"][0]["message"]["content"]
        return httpx.Response(200, json={
            "model": "offline-test-model", "done": True, "done_reason": "stop",
            "message": {"role": "assistant", "content": content},
        })


@pytest.mark.parametrize("client_kind", ["chat_completions", "ollama"])
async def test_real_ai_to_demo_image_and_report_preserves_source_and_demo_cohort(
    tmp_path, monkeypatch, client_kind,
):
    root = tmp_path.resolve()  # Resolve macOS's /var -> /private/var storage alias.
    owner = 51
    now = datetime(2026, 11, 9, 9, tzinfo=UTC)
    model_http = OllamaModelFixture() if client_kind == "ollama" else ModelFixture()
    network_attempts = []

    def deny_network(*args, **kwargs):
        network_attempts.append(True)
        raise AssertionError("Only the injected HTTP mock may serve model requests")

    # MockTransport dispatches directly, so these guards detect accidentally
    # constructing a real network transport without disabling the actual AI client.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_network)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", deny_network)
    monkeypatch.setattr("socket.create_connection", deny_network)
    monkeypatch.setattr("socket.socket.connect", deny_network)
    monkeypatch.setattr("socket.socket.connect_ex", deny_network)

    if client_kind == "ollama":
        client = OllamaJSONClient(
            LocalAISettings(model="offline-test-model", max_retries=0),
            transport=httpx.MockTransport(model_http),
        )
    else:
        client = ChatJSONClient(
            AISettings(api_key="offline-test-key", model="offline-test-model", max_retries=0),
            transport=httpx.MockTransport(model_http),
        )
    ai = ModelAI(client)
    images = DemoImageProvider()
    settings = Settings(mode="demo", owner_id=owner, data_dir=root)
    reporter = build_report_provider(settings)
    publisher = DemoPublisher()
    repository = Repository(root / "backend.sqlite3")
    repository.initialize()
    service = BackendService(
        repository,
        FileStore(root / "assets"),
        settings,
        ai,
        images,
        publisher,
        DemoNotifier(),
        reporter,
        clock=lambda: now,
    )
    try:
        business = service.create_business(owner, "Тёплое зерно")
        notes = "Кофейня «Тёплое зерно». В меню сырники, тосты и фильтр-кофе."
        source = service.add_text_source(owner, business.id, notes)
        analyzed = await service.analyze_business(owner, business.id)
        assert not analyzed.profile_confirmed
        assert analyzed.profile.facts[0].source_ids == [source.id]
        assert model_http.inputs[0]["sources"] == [{"id": source.id, "text": notes}]
        service.confirm_profile(owner, business.id, analyzed.profile_version)
        business = await service.connect_channel(owner, business.id, "@demo_channel")

        # Observe arguments while still running the real DemoImageProvider. The
        # existing AI integration used core.DemoImages rather than this adapter.
        with patch.object(images, "generate", wraps=images.generate) as generate:
            job = service.request_preparation(owner, business.id, "Подготовь один пост", "role4-ai-1")
            assert await service.prepare_next_job()
        assert service.get_job(owner, job.id).status == "done"
        assert len(model_http.inputs) == 3  # Actual client: profile -> plan -> text.
        if client_kind == "ollama":
            assert [(request.method, request.url.path) for request in model_http.requests] == [
                ("GET", "/api/status"), ("POST", "/api/show"), ("POST", "/api/chat"),
            ] * 3
        generate.assert_awaited_once()
        image_profile, image_text = generate.await_args.args
        assert image_profile == business.profile
        assert isinstance(image_text, TextDraft)
        assert image_text.used_source_ids == [source.id]
        assert image_text.image_brief == "Сырники рядом с чашкой кофе, без надписей"
        plan = service.get_plan(owner, job.id)
        assert len(plan.items) == 1
        assert model_http.inputs[2]["item"]["image_brief"] == plan.items[0].image_brief

        posts = service.get_job_posts(owner, job.id)
        assert len(posts) == 1
        post = posts[0]
        assert post.text == image_text.text
        assert post.status == PostStatus.REVIEW
        assert post.approved_revision is None
        assert post.is_demo
        asset, png = service.read_asset(owner, post.image_asset_id)
        assert asset.is_demo and asset.mime_type == "image/png"
        validate_png(png, MediaLimits())
        assert len(png) == asset.size
        assert hashlib.sha256(png).hexdigest() == asset.sha256
        assert FileStore(root / "assets").read(asset) == png
        reopened = Repository(root / "backend.sqlite3")
        try:
            assert reopened.get_post(post.id) == post
            assert reopened.get_business(business.id).profile.facts[0].source_ids == [source.id]
        finally:
            reopened.close()

        # This scenario deliberately leaves the post unapproved and never
        # publishes. The local demo subscriber snapshot must remain demo too.
        assert not await service.publish_due_once()
        assert await service.collect_metrics() == 1
        start, end = now - timedelta(hours=1), now + timedelta(seconds=1)
        report_data = service.report_data(owner, business.id, start, end)
        report = build_report(report_data)
        cohorts = {activity.is_demo: activity for activity in report.activities}
        assert (cohorts[False].created_posts, cohorts[False].published_posts) == (0, 0)
        assert (cohorts[True].created_posts, cohorts[True].published_posts) == (1, 0)
        assert dict(cohorts[True].current_states)[PostStatus.REVIEW] == 1
        assert len(report.metrics) == 1
        assert report.metrics[0].is_demo and report.metrics[0].value == 42
        assert report.metrics[0].source == f"demo:{business.channel_id}"
        rendered = service.build_report(owner, business.id, start, end)
        assert rendered == reporter.build(report_data)
        assert "Демо-посты:" in rendered
        assert "ДЕМО: 42" in rendered
        assert "Просмотры, охват, пересылки, клики и продажи: нет данных." in rendered
        assert publisher.sent == []
        assert network_attempts == []
    finally:
        await ai.close()
        repository.close()
    assert client._http.is_closed
