"""Company-scoped conversation memory survives retries and process restarts."""

import json
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.core.runtime import build_runtime
from app.shared.errors import Conflict, Forbidden, ProviderUnavailable
from app.shared.models import BusinessProfile, Fact, TaskIntent
from app.storage.repository import Repository

OWNER = 42


@pytest.fixture
async def runtime(tmp_path):
    runtime = await build_runtime(Settings(owner_id=OWNER, data_dir=tmp_path))
    yield runtime
    await runtime.close()


def company(service, name, fact):
    business = service.create_business(OWNER, name)
    source = service.add_text_source(OWNER, business.id, fact)
    profile = BusinessProfile(name=name, description=fact, facts=[Fact(text=fact, source_ids=[source.id])])
    business = service.save_profile(OWNER, business.id, 0, profile)
    return service.confirm_profile(OWNER, business.id, business.profile_version)


async def test_clarification_history_and_resolved_brief_survive_restart(runtime):
    service = runtime.service
    first = company(service, "Кофейня", "Завтраки подаются до 12 часов")
    await service.connect_channel(OWNER, first.id, "@test_coffee")
    interpret = AsyncMock(side_effect=[
        TaskIntent(action="clarify", questions=["Сколько постов подготовить о завтраках?"]),
        TaskIntent(action="prepare", request="Подготовь два поста о завтраках, без цен."),
    ])
    service.ai.interpret_request = interpret
    initial = await service.interpret_request(OWNER, first.id, "Хочу посты о завтраках", request_id="turn:1")
    clarified = await service.interpret_request(OWNER, first.id, "Два, без цен", request_id="turn:2")
    job = service.request_preparation(OWNER, first.id, clarified.request, "turn:2:prepare")
    assert job.request == "Подготовь два поста о завтраках, без цен."
    history = service.conversation_history(OWNER, first.id)
    assert [turn["action"] for turn in history] == ["clarify", "prepare"]
    assert history[0]["assistant_text"] == initial.questions[0]
    second_context = interpret.call_args_list[1].args[1]
    assert second_context["profile"]["facts"][0]["text"] == "Завтраки подаются до 12 часов"
    assert second_context["history"][0]["user_text"] == "Хочу посты о завтраках"
    assert service.repo.list_jobs(first.id)[0].id == job.id
    assert service.publisher.sent == []

    # A new runtime has no in-memory conversation state.
    settings = service.settings
    await runtime.close()
    reopened = await build_runtime(settings)
    try:
        reader = reopened.service
        assert reader.conversation_history(OWNER, first.id) == history
        answer = AsyncMock(return_value=TaskIntent(action="answer", answer="Два поста о завтраках ожидают подготовки."))
        reader.ai.interpret_request = answer
        result = await reader.interpret_request(OWNER, first.id, "Что я попросил подготовить?", request_id="turn:3")
        assert "Два поста" in result.answer
        context = answer.call_args.args[1]
        assert context["history"][-1]["action"] == "prepare"
        assert context["recent_jobs"][0]["request"] == job.request
        assert context["recent_jobs"][0]["status"] == "pending"
        assert reader.publisher.sent == []
    finally:
        await reopened.close()


async def test_context_never_reads_other_company_and_replays_do_not_duplicate(runtime):
    service = runtime.service
    first = company(service, "Компания A", "Уникальный закрытый факт A-729")
    second = company(service, "Компания B", "Уникальный закрытый факт B-418")
    interpret = AsyncMock(return_value=TaskIntent(action="answer", answer="Сведения сохранены."))
    service.ai.interpret_request = interpret
    one = await service.interpret_request(OWNER, first.id, "Секретная тема A-881", request_id="company-a:1")
    assert await service.interpret_request(OWNER, first.id, "Секретная тема A-881", request_id="company-a:1") == one
    assert interpret.await_count == 1
    assert len(service.conversation_history(OWNER, first.id)) == 1
    with pytest.raises(Conflict):
        await service.interpret_request(OWNER, second.id, "Тот же ключ", request_id="company-a:1")
    with pytest.raises(Forbidden):
        service.conversation_history(OWNER + 1, first.id)
    with pytest.raises(Forbidden):
        await service.interpret_request(OWNER + 1, first.id, "Покажи историю")
    await service.interpret_request(OWNER, second.id, "Что известно о компании?", request_id="company-b:1")
    context = interpret.call_args.args[1]
    assert context["history"] == []
    encoded = json.dumps(context, ensure_ascii=False)
    assert "B-418" in encoded and "A-729" not in encoded and "A-881" not in encoded
    assert first.id not in encoded
    assert service.conversation_history(OWNER, second.id)[0]["user_text"] == "Что известно о компании?"


async def test_dialogue_and_cached_result_commit_atomically(runtime):
    service = runtime.service
    business = company(service, "Компания", "Проверенный факт")
    service.ai.interpret_request = AsyncMock(return_value=TaskIntent(action="answer", answer="Ответ"))
    original = service.repo._audit

    def fail_on_done(conn, entity, key, action, now, **details):
        if entity == "ai_operation" and action == "done":
            raise RuntimeError("Simulated crash before commit")
        return original(conn, entity, key, action, now, **details)

    with patch.object(service.repo, "_audit", side_effect=fail_on_done):
        with pytest.raises(ProviderUnavailable):
            await service.interpret_request(OWNER, business.id, "Вопрос", request_id="atomic:1")
    assert service.conversation_history(OWNER, business.id) == []
    with pytest.raises(Conflict):
        await service.interpret_request(OWNER, business.id, "Вопрос", request_id="atomic:1")
    assert service.ai.interpret_request.await_count == 1


async def test_recent_post_state_overrides_an_earlier_assistant_promise(runtime):
    service = runtime.service
    business = company(service, "Компания", "Подтверждённый товар")
    await service.connect_channel(OWNER, business.id, "@channel")
    service.request_preparation(OWNER, business.id, "Один пост", "make-post")
    assert await service.prepare_next_job()
    post = service.list_posts(OWNER, business.id)[0]
    interpret = AsyncMock(return_value=TaskIntent(action="answer", answer="Пост ожидает согласования."))
    service.ai.interpret_request = interpret
    await service.interpret_request(OWNER, business.id, "Уже опубликовано?", request_id="status:1")
    state = interpret.call_args.args[1]["recent_posts"][0]
    assert state["id"] == post.id and state["status"] == "review"
    assert state["published_at"] is None
    assert service.publisher.sent == []


def test_v3_migration_preserves_company_and_adds_empty_history(tmp_path):
    repo = Repository(tmp_path / "legacy.sqlite3")
    repo.initialize()
    from datetime import UTC, datetime
    business = repo.create_business(OWNER, "Существующая компания", "Europe/Moscow", datetime.now(UTC))
    with closing(sqlite3.connect(repo.db_path)) as conn:
        conn.execute("DROP TABLE conversation_turns")
        conn.execute("PRAGMA user_version=3")
        conn.commit()
    repo.initialize()
    repo.initialize()
    assert repo.get_business(business.id) == business
    assert repo.conversation_history(business.id) == []
    repo.close()
