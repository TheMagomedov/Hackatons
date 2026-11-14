import asyncio
import json
import logging
from datetime import timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.core.runtime import build_runtime
from app.miniapp.server import create_app
from app.shared.errors import Conflict, Forbidden, InvalidInput, ProviderUnavailable
from app.shared.models import TaskIntent
from app.vk.adapter import VKBotAdapter
from app.vk.client import VKClient
from app.vk.store import VKStore

OWNER, VK_USER, GROUP = 42, 9001, 123


def event(text, ident=1, *, user=VK_USER, group=GROUP, peer=None):
    return {"type": "message_new", "group_id": group, "object": {"message": {
        "id": ident, "conversation_message_id": ident, "from_id": user,
        "peer_id": user if peer is None else peer, "text": text,
    }}}


@pytest.fixture
async def bot(tmp_path):
    runtime = await build_runtime(Settings(owner_id=OWNER, data_dir=tmp_path))
    service = runtime.service
    service.settings = service.settings.model_copy(update={
        "vk_group_id": GROUP, "mini_app_url": "https://studio.example/mini/",
    })
    client = AsyncMock()
    adapter = VKBotAdapter(service, client=client, store=VKStore(tmp_path / "vk.sqlite3", GROUP))
    service.vk = adapter
    try:
        yield adapter
    finally:
        await adapter.close()
        await runtime.close()


def link(bot, actor=OWNER, user=VK_USER):
    token = bot.issue_link(actor)
    bot.store.claim_link(user, token, bot.service.require_access)


async def test_numeric_id_never_grants_access_and_private_group_is_required(bot):
    bot.service.ai.interpret_request = AsyncMock()
    bot.ingest(event("Расскажи секреты", user=OWNER))  # Same number is not same identity.
    await bot.process_next()
    assert "свяжите аккаунты" in bot.client.send.call_args.args[1]
    bot.service.ai.interpret_request.assert_not_called()
    for update in [event("group", ident=2, peer=2000000123), event("foreign", ident=3, group=999),
                   {"type": "message_new", "group_id": GROUP, "object": []}]:
        bot.ingest(update)
    assert not await bot.process_next()


async def test_one_use_link_code_is_not_stored_or_sent_to_ai(bot):
    token = bot.issue_link(OWNER)
    bot.ingest(event("/link " + token))
    bot.ingest(event("/link " + token))
    assert bot.store.actor(VK_USER) == OWNER
    assert token not in "\n".join(bot.store.db.iterdump())
    with pytest.raises(Forbidden):
        bot.store.claim_link(VK_USER, token, bot.service.require_access)
    assert await bot.process_next()
    assert not await bot.process_next()
    assert bot.client.send.call_count == 1
    assert "#studioLaunch=" in bot.client.send.call_args.args[3]


async def test_unlink_revokes_web_sessions_and_pending_messages(bot):
    link(bot)
    launch = bot.launch(OWNER, VK_USER).split("=", 1)[1]
    session = bot.exchange(launch)
    assert bot.session_actor(session) == OWNER
    with pytest.raises(Forbidden):
        bot.exchange(launch)
    bot.ingest(event("секретные документы"))
    bot.unlink(OWNER)
    with pytest.raises(Forbidden):
        bot.session_actor(session)
    await bot.process_next()
    assert "свяжите аккаунты" in bot.client.send.call_args.args[1]


async def test_ticket_expiry_link_replacement_and_account_collision(bot):
    first = bot.issue_link(OWNER)
    second = bot.issue_link(OWNER)
    with pytest.raises(Forbidden):
        bot.store.claim_link(VK_USER, first, bot.service.require_access)
    bot.store.claim_link(VK_USER, second, bot.service.require_access)
    token = bot.issue_link(OWNER)
    with pytest.raises(Conflict):
        bot.store.claim_link(VK_USER + 1, token, bot.service.require_access)
    launch = bot.launch(OWNER, VK_USER).split("=", 1)[1]
    with bot.store.db:
        bot.store.db.execute("UPDATE tickets SET expires=0")
    with pytest.raises(Forbidden):
        bot.exchange(launch)


async def test_real_backend_queues_once_and_keeps_publication_approval(bot):
    service = bot.service
    business = service.create_business(OWNER, "Стекло")
    service.add_text_source(OWNER, business.id, "Производство стекла для архитектуры.")
    business = await service.analyze_business(OWNER, business.id)
    service.confirm_profile(OWNER, business.id, business.profile_version)
    await service.connect_channel(OWNER, business.id, "@example_channel")
    service.ai.interpret_request = AsyncMock(return_value=TaskIntent(action="prepare", request="Пост о стекле"))
    link(bot)
    bot.ingest(event("Подготовь пост о стекле"))
    bot.ingest(event("Подготовь пост о стекле"))
    await bot.process_next()
    assert len(service.list_jobs(OWNER, business.id)) == 1
    assert not service.list_posts(OWNER, business.id)
    assert not await bot.process_next()
    service.ai.interpret_request.assert_awaited_once()
    assert "согласования" in bot.client.send.call_args.args[1]


async def test_viewer_cannot_generate_and_revocation_during_ai_blocks_output(bot):
    service = bot.service
    business = service.create_business(OWNER, "Стекло")
    invitation = service.repo.create_access_invite(
        OWNER, "reviewer", 1, service.now() + timedelta(hours=1), service.now())
    service.claim_invite(77, invitation["token"])
    link(bot, actor=77)
    bot.ingest(event("Сделай пост"))
    await bot.process_next()
    assert "наблюдателя" in bot.client.send.call_args.args[1]
    assert not service.list_jobs(OWNER, business.id)
    bot.unlink(77)
    link(bot)
    async def answer(*args, **kwargs):
        bot.unlink(OWNER)
        return TaskIntent(action="answer", answer="Sensitive company answer")
    service.interpret_request = answer
    bot.ingest(event("Вопрос", ident=2))
    await bot.process_next()
    assert "Sensitive" not in bot.client.send.call_args.args[1]
    assert bot.client.send.call_args.args[3] is None


async def test_reboot_does_not_replay_interrupted_delivery(bot, tmp_path):
    link(bot)
    bot.ingest(event("Меню"))
    item = bot.store.take()
    bot.store.close()
    bot.store = VKStore(tmp_path / "vk.sqlite3", GROUP)
    assert bot.store.db.execute("SELECT state,text FROM inbox WHERE id=?", (item["id"],)).fetchone()[:] == (
        "interrupted", "")
    assert not await bot.process_next()
    with pytest.raises(Conflict):
        VKStore(tmp_path / "vk.sqlite3", GROUP + 1)


async def test_studio_session_real_http_no_telegram_impersonation(bot):
    link(bot)
    service = bot.service
    business = service.create_business(OWNER, "Стекло")
    launch = bot.launch(OWNER, VK_USER).split("=", 1)[1]
    async with TestClient(TestServer(create_app(service, token="test-token"))) as browser:
        assert (await browser.get("/mini/api/state", headers={"X-VK-User": str(OWNER)})).status == 403
        response = await browser.post("/mini/session/vk", json={"launch": launch})
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        session = (await response.json())["session"]
        assert session not in "\n".join(bot.store.db.iterdump())
        assert (await browser.post("/mini/session/vk", json={"launch": launch})).status == 403
        headers = {"X-Studio-Session": session}
        state = await browser.get("/mini/api/state", headers=headers)
        assert state.status == 200
        assert (await state.json())["businesses"][0]["id"] == business.id
        bot.unlink(OWNER)
        assert (await browser.get("/mini/api/state", headers=headers)).status == 403


async def test_http_session_preserves_reviewer_role(bot):
    service = bot.service
    invitation = service.repo.create_access_invite(
        OWNER, "reviewer", 1, service.now() + timedelta(hours=1), service.now())
    service.claim_invite(77, invitation["token"])
    link(bot, actor=77)
    session = bot.exchange(bot.launch(77, VK_USER).split("=", 1)[1])
    async with TestClient(TestServer(create_app(service, token="test-token"))) as browser:
        headers = {"X-Studio-Session": session}
        assert (await browser.get("/mini/api/state", headers=headers)).status == 200
        assert (await browser.post("/mini/api/business", headers=headers, json={"name": "Forbidden"})).status == 403
        service.repo.revoke_access_member(OWNER, 77, service.now())
        assert (await browser.get("/mini/api/state", headers=headers)).status == 403


async def test_vk_wire_contract_random_id_and_no_secret_logs(caplog):
    captured = []
    async def transport(request):
        captured.append(request)
        if "getLongPollServer" in str(request.url):
            return httpx.Response(200, json={"response": {
                "server": "https://lp.vk.com/wh123", "key": "private-poll-key", "ts": "10"}})
        if request.method == "GET":
            return httpx.Response(200, json={"ts": "11", "updates": []})
        return httpx.Response(200, json={"response": 55})
    client = VKClient("private-vk-token", GROUP, transport=httpx.MockTransport(transport))
    try:
        with caplog.at_level(logging.INFO, logger="httpx"):
            server = await client.poll_server()
            assert (await client.poll(server))["ts"] == "11"
            await client.send(VK_USER, "Ответ", "same-event", "https://studio.example/mini/#studioLaunch=secret")
            await client.send(VK_USER, "Ответ", "same-event")
        assert "private-poll-key" not in caplog.text
        assert "private-vk-token" not in caplog.text
        payloads = [parse_qs(r.content.decode()) for r in captured if "messages.send" in str(r.url)]
        assert payloads[0]["random_id"] == payloads[1]["random_id"]
        assert payloads[0]["access_token"] == ["private-vk-token"]
        keyboard = json.loads(payloads[0]["keyboard"][0])
        assert keyboard["buttons"][0][0]["action"]["type"] == "open_link"
    finally:
        await client.close()


@pytest.mark.parametrize("server", ["http://lp.vk.com/path", "https://lp.vk.com.evil.test/path",
                                    "https://127.0.0.1/path", "https://user@lp.vk.com/path"])
async def test_poll_refuses_foreign_hosts_and_tls_downgrade(server):
    client = VKClient("secret", GROUP, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "response": {"server": server, "key": "key", "ts": "1"}})))
    try:
        with pytest.raises(ProviderUnavailable):
            await client.poll_server()
    finally:
        await client.close()


async def test_vk_error_does_not_echo_token_or_retry_delivery():
    transport = AsyncMock(return_value=httpx.Response(200, json={"error": {
        "error_code": 5, "request_params": [{"key": "access_token", "value": "credential-secret"}]}}))
    client = VKClient("credential-secret", GROUP, transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(ProviderUnavailable) as error:
            await client.send(VK_USER, "text", "key")
        assert "credential-secret" not in str(error.value)
        assert transport.await_count == 1
    finally:
        await client.close()


async def test_poll_cursor_advances_only_after_persistence(bot):
    stop = asyncio.Event()
    bot.client.poll_server.return_value = {"server": "https://lp.vk.com/wh", "key": "private", "ts": "1"}
    async def poll(server):
        stop.set()
        return {"ts": "2", "updates": [event("Меню")]}
    bot.client.poll.side_effect = poll
    await bot.poll_loop(stop)
    assert bot.store.cursor() == "2"
    assert bot.store.take()["text"] == "Меню"


@pytest.mark.parametrize("extra", [{"vk_group_id": 1}, {"vk_group_token": "secret"},
                                   {"vk_group_id": 1, "vk_group_token": "secret"}])
def test_vk_partial_or_demo_configuration_fails_closed(extra):
    with pytest.raises(InvalidInput):
        Settings(**extra)


@pytest.mark.parametrize("group,permissions,valid", [
    (GROUP, ["messages"], True), (GROUP + 1, ["messages"], False), (GROUP, ["photos"], False),
])
async def test_actual_community_and_message_permission_are_required(group, permissions, valid):
    async def transport(request):
        data = ({"groups": [{"id": group, "name": "Студия"}]} if "getById" in str(request.url)
                else {"permissions": [{"name": p} for p in permissions]})
        return httpx.Response(200, json={"response": data})
    client = VKClient("test-only", GROUP, transport=httpx.MockTransport(transport))
    try:
        if valid:
            assert (await client.verify())["group_id"] == GROUP
        else:
            with pytest.raises(InvalidInput):
                await client.verify()
    finally:
        await client.close()


async def test_delivery_timeout_cannot_replay_mutation_or_response(bot):
    link(bot)
    bot.client.send.side_effect = ProviderUnavailable("Delivery uncertain")
    bot.ingest(event("Меню"))
    await bot.process_next()
    bot.ingest(event("Меню"))
    assert not await bot.process_next()
    assert bot.client.send.await_count == 1
    row = bot.store.db.execute("SELECT state,text FROM inbox").fetchone()
    assert row[:] == ("failed", "")
