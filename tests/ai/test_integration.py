"""Actual AI adapter and SQLite backend; only model HTTP is simulated."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.ai.client import AISettings, ChatJSONClient
from app.ai.provider import ModelAI
from app.ai.smoke import run_smoke
from app.config import Settings
from app.core.demo import DemoImages, DemoNotifier, DemoPublisher
from app.core.service import BackendService
from app.shared.models import PostStatus
from app.storage import FileStore, Repository


class ModelFixture:
    """Return deterministic schema-shaped model messages, recording real requests."""

    def __init__(self):
        self.inputs = []

    def __call__(self, request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])["input"]
        self.inputs.append(data)
        if "sources" in data:
            result = {
                "name": "Тёплое зерно", "description": "Кофейня с завтраками",
                "products": ["сырники", "тосты", "фильтр-кофе"],
                "facts": [{
                    "text": "В меню есть сырники, тосты и фильтр-кофе.",
                    "source_ids": [data["sources"][0]["id"]],
                }],
                "assumptions": ["Завтраки могут быть интересны гостям по пути на работу."],
                "questions": ["Какие часы работы и цены?"],
            }
        elif "request" in data:
            count = 3 if "три" in data["request"] else 1
            result = {"items": [{
                "topic": topic, "goal": "Познакомить с меню", "brief": topic,
                "image_brief": "Завтрак с чашкой кофе, без надписей", "suggested_at": None,
            } for topic in ["Сырники к завтраку", "Пауза с фильтр-кофе", "Тосты для утра"][:count]]}
        elif "selected_post_id" in data:
            result = {"action": "revise_text", "post_id": data["selected_post_id"]}
        else:
            revised = "current_text" in data
            result = {
                "text": "На завтрак — сырники и фильтр-кофе." if revised else
                        "Начните утро с завтрака: в меню сырники, тосты и фильтр-кофе.",
                "image_brief": "Сырники рядом с чашкой кофе, без надписей",
                "used_source_ids": data["profile"]["facts"][0]["source_ids"],
                "warnings": ["Цены и часы работы не предоставлены."],
            }
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(result, ensure_ascii=False)},
        }]})


def provider_for(fixture):
    return ModelAI(ChatJSONClient(
        AISettings(api_key="test-only-key", model="test-model", max_retries=0),
        transport=httpx.MockTransport(fixture),
    ))


@pytest.mark.asyncio
async def test_notes_profile_three_topics_one_draft():
    fixture = ModelFixture()
    provider = provider_for(fixture)
    try:
        result = await run_smoke(provider)
    finally:
        await provider.close()
    assert len(fixture.inputs) == 3
    assert result["profile"]["name"] == "Тёплое зерно"
    assert len(result["plan"]["items"]) == 3
    assert result["draft"]["used_source_ids"] == ["smoke-source"]
    assert not result["profile_confirmed"] and not result["published"]


@pytest.mark.asyncio
async def test_ai_backend_preparation_and_revision(tmp_path):
    fixture = ModelFixture()
    ai = provider_for(fixture)
    repo = Repository(tmp_path / "backend.sqlite3")
    repo.initialize()
    publisher = DemoPublisher()
    service = BackendService(
        repo, FileStore(tmp_path / "assets"), Settings(data_dir=tmp_path),
        ai, DemoImages(), publisher, DemoNotifier(),
    )
    try:
        business = service.create_business(1, "Тёплое зерно")
        source = service.add_text_source(
            1, business.id, "Кофейня «Тёплое зерно». В меню сырники, тосты и фильтр-кофе.",
        )
        analyzed = await service.analyze_business(1, business.id)
        assert not analyzed.profile_confirmed
        assert analyzed.profile.facts[0].source_ids == [source.id]
        service.confirm_profile(1, business.id, analyzed.profile_version)
        await service.connect_channel(1, business.id, "@demo")
        job = service.request_preparation(1, business.id, "Подготовь один пост", "integration-update-1")
        await service.prepare_next_job()
        assert service.get_job(1, job.id).status == "done"
        posts = service.get_job_posts(1, job.id)
        assert len(posts) == 1  # Backend's appended limit of 3 is not the requested count.
        post = posts[0]
        assert post.status == PostStatus.REVIEW
        intent = await service.interpret_request(1, business.id, "Сделай короче", post_id=post.id)
        assert intent.post_id == post.id
        post = service.edit_post(
            1, post.id, post.revision, scheduled_at=datetime.now(UTC) + timedelta(days=1),
        )
        approved = service.approve_post(1, post.id, post.revision)
        assert approved.status == PostStatus.APPROVED
        revised = await service.revise_text(1, approved.id, approved.revision, "Сделай короче")
        # Backend owns reservation and completion revisions; adapters never calculate them.
        assert revised.revision > approved.revision
        assert service.get_post(1, revised.id).revision == revised.revision
        assert revised.status == PostStatus.REVIEW and revised.approved_revision is None
        assert revised.text == "На завтрак — сырники и фильтр-кофе."
        assert publisher.sent == []
    finally:
        await ai.close()
        repo.close()
