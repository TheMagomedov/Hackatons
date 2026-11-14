"""All shipped factories together; only network transports are simulated."""

import json
from datetime import UTC, datetime, timedelta

import httpx
from aiogram.types import Update

import app.ai.provider as ai_module
import app.core.runtime as runtime_module
from app.ai.client import ChatJSONClient
from app.config import Settings
from app.media.openai import UrllibImageTransport
from app.publishing.telegram import TelegramPublisher
from app.safety.client import UrllibSafetyTransport
from app.shared.models import PostStatus
from tests.ai.test_integration import ModelFixture
from tests.qa.test_role4_integration import InlineImageTransport
from tests.test_adapters import RecordingSession


async def test_all_live_adapters_bot_to_publication_report_and_restart(tmp_path, monkeypatch):
    owner, channel = 42, -1001234567890
    now = [datetime(2026, 11, 9, 12, tzinfo=UTC)]
    fixture = ModelFixture()
    model_requests, telegram_requests, safety_requests = [], [], []
    image_transport = InlineImageTransport()
    publisher_clients = []

    def model_http(request):
        body = json.loads(request.content)
        data = json.loads(body["messages"][1]["content"])["input"]
        model_requests.append(data)
        if "selected_post_id" in data:
            content = {"action": "prepare", "request": data["text"]}
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(content)},
                        }
                    ]
                },
            )
        return fixture(request)

    def telegram_http(request):
        method = request.url.path.rsplit("/", 1)[-1]
        telegram_requests.append(method)
        if method == "getMe":
            result = {"id": 99, "is_bot": True}
        elif method == "getChat":
            result = {"id": channel, "type": "channel", "title": "Integration channel"}
        elif method == "getChatMember":
            uid = json.loads(request.content)["user_id"]
            result = {"user": {"id": uid}, "status": "administrator", "can_post_messages": True}
        elif method == "sendPhoto":
            result = {"message_id": 81, "chat": {"id": channel, "type": "channel"}}
        elif method == "getChatMemberCount":
            result = 23
        else:
            raise AssertionError(f"Unexpected Telegram method: {method}")
        return httpx.Response(200, json={"ok": True, "result": result})

    def publisher_factory(token):
        client = httpx.AsyncClient(transport=httpx.MockTransport(telegram_http))
        publisher_clients.append(client)
        return TelegramPublisher(token, client=client)

    def safety_http(_transport, request, timeout):
        # Keep the real configured live policy and verdict parser; replace network I/O only.
        assert request.get_header("Authorization") == "Bearer fixture-safety-key"
        assert request.method == "POST"
        base = "https://moderator.example.test/v1/safety/"
        assert request.full_url in (base + "check-text", base + "check-image")
        is_image = request.full_url.endswith("check-image")
        assert request.get_header("Content-type") == ("image/png" if is_image else "application/json")
        assert timeout == (30 if is_image else 90)
        safety_requests.append((request.full_url, request.data))
        return b'{"allowed":true,"reason":"","category":"safe"}'

    monkeypatch.setenv("AI_API_KEY", "fixture-ai-key")
    monkeypatch.setenv("AI_MODEL", "fixture-text-model")
    monkeypatch.setenv("IMAGE_API_KEY", "fixture-image-key")
    monkeypatch.setenv("IMAGE_MODEL", "fixture-image-model")
    monkeypatch.setenv("MODERATION_API_BASE", "https://moderator.example.test/v1")
    monkeypatch.setenv("MODERATION_API_KEY", "fixture-safety-key")
    monkeypatch.setattr(
        ai_module,
        "ChatJSONClient",
        lambda config: ChatJSONClient(
            config,
            transport=httpx.MockTransport(model_http),
        ),
    )
    monkeypatch.setattr(UrllibImageTransport, "send", lambda _, *args: image_transport.send(*args))
    monkeypatch.setattr(UrllibSafetyTransport, "send", safety_http)
    monkeypatch.setattr(runtime_module, "TelegramPublisher", publisher_factory)
    settings = Settings(
        mode="live",
        owner_id=owner,
        data_dir=tmp_path,
        telegram_bot_token="123456:fixture",
        ai_factory="app.ai.provider:build_provider",
        image_factory="app.media.runtime:build_provider",
        report_factory="app.reports.provider:build_provider",
        bot_factory="app.bot.adapter:build_bot",
    )
    runtime = await runtime_module.build_runtime(settings)
    session = RecordingSession()
    await runtime.bot.bot.session.close()
    runtime.bot.bot.session = session
    runtime.service.clock = lambda: now[0]
    uid = 0

    async def send(*, text=None, button=None):
        nonlocal uid
        uid += 1
        user = {"id": owner, "first_name": "Owner", "is_bot": False}
        message = {"message_id": uid, "chat": {"id": owner, "type": "private"}, "date": now[0]}
        event = {"message": {**message, "from": user, "text": text}}
        if button:
            event = {
                "callback_query": {
                    "id": str(uid),
                    "chat_instance": "integration",
                    "from": user,
                    "data": button,
                    "message": message,
                }
            }
        update = Update.model_validate({"update_id": uid, **event})
        await runtime.bot.dispatcher.feed_update(runtime.bot.bot, update)
        return update

    try:
        service = runtime.service
        await send(text="/start")
        await send(button="input:name")
        await send(text="Тёплое зерно")
        business = service.list_businesses(owner)[0]
        await send(button=f"input:notes:{business.id}")
        await send(text="В меню сырники, тосты и фильтр-кофе.")
        await send(button=f"analyze:{business.id}")
        assert not safety_requests  # Internal source/profile analysis is not an outgoing post.
        business = service.get_business(owner, business.id)
        assert business.profile is not None
        await send(button=f"confirm:{business.id}:{business.profile_version}")
        await send(button=f"input:channel:{business.id}")
        await send(text="@integration_channel")
        assert service.get_business(owner, business.id).channel_id == str(channel)
        event = await send(text="Подготовь один пост")
        before = len(model_requests)
        await runtime.bot.dispatcher.feed_update(runtime.bot.bot, event)
        assert len(model_requests) == before
        assert len(service.list_jobs(owner, business.id)) == 1
        assert await service.prepare_next_job()
        post = service.list_posts(owner, business.id)[0]
        assert not post.is_demo and post.warnings and post.used_source_ids
        assert len(image_transport.requests) == 1
        text_batches = [json.loads(body)["texts"] for url, body in safety_requests if url.endswith("check-text")]
        assert any(post.text in batch for batch in text_batches)
        assert sum(url.endswith("check-image") for url, _ in safety_requests) == 1
        await send(button=f"post:{post.id}")
        assert any(post.warnings[0] in (getattr(request, "text", "") or "") for request in session.requests)
        await send(button=f"text:{post.id}:{post.revision}")
        await send(text="Сделай короче")
        revised = service.get_post(owner, post.id)
        assert revised.revision > post.revision
        await send(button=f"time:{post.id}:{revised.revision}")
        await send(text=(now[0] + timedelta(minutes=1)).isoformat())
        timed = service.get_post(owner, post.id)
        await send(button=f"approve:{post.id}:{post.revision}")
        assert service.get_post(owner, post.id).status == PostStatus.REVIEW
        await send(button=f"approve:{post.id}:{timed.revision}")
        assert service.get_post(owner, post.id).status == PostStatus.APPROVED
        await send(button=f"pause:{business.id}")
        now[0] += timedelta(minutes=2)
        assert not await service.publish_due_once()
        await send(button=f"resume:{business.id}")
        safety_before_send = len(safety_requests)
        assert await service.publish_due_once()
        assert len(safety_requests) == safety_before_send + 2
        assert safety_requests[-2][0].endswith("/check-text")
        assert json.loads(safety_requests[-2][1]) == {"texts": [timed.text]}
        asset, exact_image = service.read_asset(owner, timed.image_asset_id)
        assert asset.mime_type == "image/png"
        assert safety_requests[-1][0].endswith("/check-image")
        assert safety_requests[-1][1] == exact_image
        assert not await service.publish_due_once()
        published = service.get_post(owner, post.id)
        assert published.message_id == 81 and published.status == PostStatus.PUBLISHED
        assert telegram_requests.count("sendPhoto") == 1
        assert await service.collect_metrics() == 1
        assert await service.dispatch_notifications() >= 2
        await send(button=f"report:{business.id}")
        report = service.build_report(owner, business.id, now[0] - timedelta(days=1), now[0])
        assert "1" in report and "23" in report
        await runtime.close()
        runtime = await runtime_module.build_runtime(settings)
        runtime.service.clock = lambda: now[0]
        runtime.service.recover_interrupted()
        assert not await runtime.service.publish_due_once()
        assert runtime.service.get_post(owner, post.id) == published
        assert telegram_requests.count("sendPhoto") == 1
    finally:
        await runtime.close()
        for client in publisher_clients:
            await client.aclose()
