import json
import logging
import traceback
from datetime import datetime, timezone

import httpx
import pytest

from app.publishing import TelegramNotifier, TelegramPublisher
from app.shared.errors import (
    DeliveryRejected,
    DeliveryUnknown,
    Forbidden,
    InvalidInput,
    ProviderUnavailable,
)
from app.shared.models import Notification, PostStatus, PostView

TOKEN = "123456:very-secret-token"
CHANNEL = "-1001234567890"
PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"
NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def post(**changes):
    values = dict(
        id="p1",
        business_id="b1",
        revision=1,
        text="A useful post",
        image_asset_id="a1",
        channel_id=CHANNEL,
        scheduled_at=NOW,
        status=PostStatus.PUBLISHING,
        approved_revision=1,
        approved_by=42,
        created_at=NOW,
        updated_at=NOW,
    )
    values.update(changes)
    return PostView(**values)


def success_message(username="our_channel"):
    chat = {"id": int(CHANNEL), "type": "channel", "title": "Our channel"}
    if username is not None:
        chat["username"] = username
    return {"message_id": 12, "chat": chat}


def response(result, status=200):
    return httpx.Response(status, json={"ok": True, "result": result})


@pytest.mark.asyncio
async def test_verify_resolves_username_and_checks_both_members():
    calls = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        data = json.loads(request.content)
        calls.append((method, data))
        if method == "getMe":
            return response({"id": 7, "is_bot": True})
        if method == "getChat":
            return response({"id": int(CHANNEL), "type": "channel", "title": "Coffee"})
        assert method == "getChatMember"
        if data["user_id"] == 7:
            return response({"user": {"id": 7}, "status": "administrator", "can_post_messages": True})
        return response({"user": {"id": 42}, "status": "creator"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TelegramPublisher(TOKEN, client)
        result = await publisher.verify_channel("@coffee_shop", 42)
        assert result.id == CHANNEL
        assert result.title == "Coffee"
        assert calls[2:] == [
            ("getChatMember", {"chat_id": CHANNEL, "user_id": 7}),
            ("getChatMember", {"chat_id": CHANNEL, "user_id": 42}),
        ]
        await publisher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bot_status,can_post,owner_status",
    [
        ("member", True, "creator"),
        ("administrator", False, "creator"),
        ("administrator", "true", "creator"),
        ("administrator", True, "member"),
        ("administrator", True, "left"),
    ],
)
async def test_verify_requires_permissions(bot_status, can_post, owner_status):
    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        data = json.loads(request.content)
        if method == "getMe":
            return response({"id": 7, "is_bot": True})
        if method == "getChat":
            return response({"id": int(CHANNEL), "type": "channel", "title": "Coffee"})
        if data["user_id"] == 7:
            return response({"user": {"id": 7}, "status": bot_status, "can_post_messages": can_post})
        return response({"user": {"id": 42}, "status": owner_status})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TelegramPublisher(TOKEN, client)
        with pytest.raises(Forbidden):
            await publisher.verify_channel(CHANNEL, 42)
        await publisher.close()


@pytest.mark.asyncio
async def test_verify_rejects_group_destination():
    def handler(request):
        if request.url.path.endswith("getMe"):
            return response({"id": 7, "is_bot": True})
        return response({"id": -12345, "type": "supergroup", "title": "Group"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TelegramPublisher(TOKEN, client)
        with pytest.raises(InvalidInput):
            await publisher.verify_channel("@some_group", 42)
        await publisher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "username,expected",
    [
        ("our_channel", "https://t.me/our_channel/12"),
        (None, "https://t.me/c/1234567890/12"),
    ],
)
async def test_publish_plain_photo_and_exact_result(username, expected):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path.endswith("/sendPhoto")
        assert b'name="caption"' in request.content
        assert b"<b>literal text</b>" in request.content
        assert b"parse_mode" not in request.content
        assert b"image/png" in request.content
        return response(success_message(username))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TelegramPublisher(TOKEN, client)
        result = await publisher.publish(post(text="<b>literal text</b>"), PNG, "image/png")
        assert result.channel_id == CHANNEL
        assert result.message_id == 12
        assert result.message_url == expected
        assert len(calls) == 1
        await publisher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error_code", [(400, 400), (403, 403), (429, 429), (200, 400), (200, 429)])
async def test_explicit_rejection_is_safe_and_never_retried(status, error_code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"ok": False, "error_code": error_code, "description": TOKEN})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TelegramPublisher(TOKEN, client)
        with pytest.raises(DeliveryRejected) as caught:
            await publisher.publish(post(), PNG, "image/png")
        assert TOKEN not in str(caught.value)
        assert TOKEN not in repr(caught.value)
        assert len(calls) == 1
        await publisher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["timeout", "write", "server", "invalid_json", "missing_result", "redirect"]
)
async def test_ambiguous_delivery_is_unknown_and_never_retried(failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(TOKEN, request=request)
        if failure == "write":
            raise httpx.WriteError(TOKEN, request=request)
        if failure == "server":
            return httpx.Response(503, text=TOKEN)
        if failure == "invalid_json":
            return httpx.Response(200, text=TOKEN)
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://example.invalid/"})
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TelegramPublisher(TOKEN, client)
        with pytest.raises(DeliveryUnknown) as caught:
            await publisher.publish(post(), PNG, "image/png")
        formatted = "".join(
            traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
        )
        assert TOKEN not in formatted
        assert len(calls) == 1
        await publisher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("message_id", True),
        ("message_id", "12"),
        ("message_id", 0),
        ("message_id", -1),
        ("message_id", 2**52),
        ("chat_id", "-1001234567890"),
        ("chat_id", True),
        ("chat_id", -100999),
        ("chat_id", 12345),
    ],
)
async def test_malformed_or_mismatched_result_ids_are_unknown(field, value):
    result = success_message()
    if field == "message_id":
        result["message_id"] = value
    else:
        result["chat"]["id"] = value
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(result))) as client:
        publisher = TelegramPublisher(TOKEN, client)
        with pytest.raises(DeliveryUnknown):
            await publisher.publish(post(), PNG, "image/png")
        await publisher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,image,mime",
    [
        ({"is_demo": True}, PNG, "image/png"),
        ({"text": "x" * 901}, PNG, "image/png"),
        ({"text": "😀" * 513}, PNG, "image/png"),
        ({"text": "\ud800"}, PNG, "image/png"),
        ({"text": " "}, PNG, "image/png"),
        ({"channel_id": "@our_channel"}, PNG, "image/png"),
        ({}, b"", "image/png"),
        ({}, b"x" * 10_000_001, "image/png"),
        ({}, PNG, "image/jpeg"),
        ({}, PNG, "image/svg+xml"),
    ],
)
async def test_invalid_input_rejected_before_network(changes, image, mime):
    def handler(_):
        pytest.fail("Invalid publication made a network request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = TelegramPublisher(TOKEN, client)
        with pytest.raises(DeliveryRejected):
            await publisher.publish(post(**changes), image, mime)
        await publisher.close()


@pytest.mark.asyncio
async def test_utf16_limit_allows_exact_boundary():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response(success_message()))
    ) as client:
        publisher = TelegramPublisher(TOKEN, client)
        await publisher.publish(post(text="😀" * 512), PNG, "image/png")
        await publisher.close()


@pytest.mark.asyncio
async def test_httpx_logs_and_adapter_repr_redact_token(caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response(success_message()))
    ) as client:
        publisher = TelegramPublisher(TOKEN, client)
        await publisher.publish(post(), PNG, "image/png")
        assert TOKEN not in repr(publisher)
        assert TOKEN not in caplog.text
        assert "[REDACTED]" in caplog.text
        await publisher.close()


@pytest.mark.asyncio
async def test_close_preserves_external_client_and_closes_owned_client():
    async with httpx.AsyncClient() as client:
        publisher = TelegramPublisher(TOKEN, client)
        await publisher.close()
        await publisher.close()
        assert not client.is_closed
    owned = TelegramPublisher(TOKEN)
    await owned.close()
    assert owned._client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [True, -1, "5", None])
async def test_subscriber_count_rejects_invalid_values(count):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(count))) as client:
        publisher = TelegramPublisher(TOKEN, client)
        with pytest.raises(ProviderUnavailable):
            await publisher.subscriber_count(CHANNEL)
        await publisher.close()


@pytest.mark.asyncio
async def test_subscriber_count_accepts_zero():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(0))) as client:
        publisher = TelegramPublisher(TOKEN, client)
        assert await publisher.subscriber_count(CHANNEL) == 0
        await publisher.close()


@pytest.mark.asyncio
async def test_notification_sends_owner_plain_text():
    def handler(request):
        assert request.url.path.endswith("sendMessage")
        assert json.loads(request.content) == {"chat_id": 42, "text": "Post ready"}
        return response({"message_id": 10, "chat": {"id": 42, "type": "private"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = TelegramNotifier(TOKEN, client)
        await notifier.send(
            Notification(
                id="n1",
                business_id="b1",
                owner_id=42,
                kind="ready",
                text="Post ready",
                created_at=NOW,
            )
        )
        await notifier.close()
