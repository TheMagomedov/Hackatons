"""Private data must not escape via drafts, bot replies or either notifier adapter."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.methods import SendMessage, SendPhoto

from app.bot.adapter import TelegramBotAdapter
from app.bot.router import reply
from app.publishing.telegram import TelegramNotifier, TelegramPublisher
from app.safety.privacy import SensitiveDataBlocked
from app.shared.models import Notification, TextDraft
from tests.bot.test_product_workflow import NOW, OWNER, harness
from tests.core.test_product_controls import setup
from tests.core.test_service import OWNER as CORE_OWNER
from tests.publishing.test_telegram import PNG, TOKEN, post

__all__ = ["harness", "setup"]


@pytest.mark.parametrize("field", ["text", "warnings"])
async def test_legacy_sensitive_preview_never_sends_photo_or_raw_caption(harness, field):
    business = await harness.ready_business()
    safe, _job = await harness.prepare(business)
    secret = "Паспорт 45 01 123456"
    # Repository import represents old data; current service creation rejects this content.
    values = {"text": secret if field == "text" else "Безопасный черновик",
              "warnings": [secret] if field == "warnings" else [],
              "image_asset_id": safe.image_asset_id, "channel_id": safe.channel_id,
              "is_demo": True, "scheduled_at": None}
    legacy = harness.service.repo.create_posts(business.id, [values], harness.service.now())[0]
    before = len(harness.session.requests)
    await harness.send(button=f"post:{legacy.id}")
    outbound = harness.session.requests[before:]
    assert not any(isinstance(request, SendPhoto) for request in outbound)
    assert not any(secret in request.text for request in outbound if isinstance(request, SendMessage))
    assert "личные реквизиты" in harness.last_text


@pytest.mark.parametrize("field", ["text", "image_brief", "warnings"])
async def test_sensitive_generated_draft_fails_before_images_or_post_persistence(setup, field):
    values = {"text": "О стекле", "image_brief": "Стеклянный фасад", "warnings": []}
    values[field] = ["Паспорт 45 01 123456"] if field == "warnings" else "Паспорт 45 01 123456"
    setup.ai.write_post = AsyncMock(return_value=TextDraft(**values))
    setup.images.generate = AsyncMock(wraps=setup.images.generate)
    job = setup.service.request_preparation(CORE_OWNER, setup.business.id, "Один пост", "private-draft")
    assert await setup.service.prepare_next_job()
    assert setup.service.get_job(CORE_OWNER, job.id).status == "failed"
    assert not setup.service.list_posts(CORE_OWNER, setup.business.id)
    setup.images.generate.assert_not_awaited()


async def test_bot_reply_masks_contact_and_secret_before_sending():
    message = SimpleNamespace(answer=AsyncMock())
    await reply(message, "Контакт person@example.org, +7 (999) 123-45-67. password=fixture-secret-only")
    content = " ".join(call.args[0] for call in message.answer.await_args_list)
    assert all(value not in content for value in ["person@example.org", "+7 (999) 123-45-67", "fixture-secret-only"])
    assert all(marker in content for marker in ["[EMAIL]", "[PHONE]", "[SECRET]"])


async def test_http_notifier_masks_notification_before_network_transport():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1, "chat": {"id": OWNER}}})

    notice = Notification(id="notice-1", business_id="business-1", owner_id=OWNER, kind="test",
                          text="Контакт person@example.org; паспорт 45 01 123456", created_at=NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = TelegramNotifier(TOKEN, client)
        await notifier.send(notice)
    text = requests[0]["text"]
    assert "person@example.org" not in text and "45 01 123456" not in text
    assert "[EMAIL]" in text and "[PASSPORT]" in text


async def test_live_bot_notifier_also_masks_notification_text():
    bot = SimpleNamespace(send_message=AsyncMock())
    adapter = SimpleNamespace(bot=bot)
    notice = Notification(id="notice-1", business_id="business-1", owner_id=OWNER, kind="test",
                          text="Контакт person@example.org; паспорт 45 01 123456", created_at=NOW)
    await TelegramBotAdapter.send(adapter, notice)
    sent = bot.send_message.await_args.args[1]
    assert "person@example.org" not in sent and "45 01 123456" not in sent
    assert "[EMAIL]" in sent and "[PASSPORT]" in sent


async def test_publisher_rejects_sensitive_caption_before_first_network_request():
    def never_contact(_request):
        pytest.fail("Sensitive caption must be blocked before contacting Telegram")

    async with httpx.AsyncClient(transport=httpx.MockTransport(never_contact)) as client:
        publisher = TelegramPublisher(TOKEN, client)
        with pytest.raises(SensitiveDataBlocked):
            await publisher.publish(post(text="Паспорт 45 01 123456"), PNG, "image/png")
