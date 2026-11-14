import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from app.media.openai import OpenAIImageProvider
from app.shared.errors import Conflict, Forbidden, ProviderUnavailable, SafetyBlocked
from app.shared.models import BusinessProfile, TextDraft
from tests import test_miniapp_manual
from tests.core.test_service import FakeAI, FakeImages
from tests.media.test_media import FakeTransport
from tests.test_miniapp import OWNER, signed_data

manual = test_miniapp_manual.manual


def configure(ctx):
    ctx.service.ai = FakeAI(lambda: ctx.now[0])
    ctx.service.images = FakeImages()
    ctx.service.ai.write_post = AsyncMock(return_value=TextDraft(
        text="Свет в архитектуре.", image_brief="A glass facade in a pine forest, soft morning light.",
    ))
    ctx.service.images.generate = AsyncMock(wraps=ctx.service.images.generate)


async def test_image_without_profile_channel_or_uploaded_file_and_replay(manual):
    configure(manual)
    data = {"request": "Стеклянный дом в сосновом лесу", "request_id": "first-image"}
    response = await manual.client.post(manual.prefix + "/visuals", json=data)
    assert response.status == 200
    result = await response.json()
    assert result["asset"]["business_id"] == manual.business.id
    assert result["draft"]["image_brief"].startswith("A glass facade")
    assert manual.service.images.generate.call_args.args[1].image_brief == result["draft"]["image_brief"]
    assert manual.service.ai.write_post.call_args.args[1].brief == data["request"]
    replay = await manual.client.post(manual.prefix + "/visuals", json=data)
    assert await replay.json() == result
    assert manual.service.images.generate.await_count == manual.service.ai.write_post.await_count == 1
    gallery = await manual.client.get(manual.prefix + "/visuals")
    assert (await gallery.json())["items"] == [result]
    image = await manual.client.get("/mini/api/asset/" + result["asset"]["id"])
    assert image.status == 200 and image.content_type == "image/png"
    assert not manual.publisher.calls
    assert manual.repo.list_posts(manual.business.id) == []


async def test_visuals_require_write_permission_but_gallery_allows_reviewer(manual):
    configure(manual)
    invite = manual.repo.create_access_invite(OWNER, "reviewer", 1, manual.now[0] + timedelta(hours=1), manual.now[0])
    manual.service.claim_invite(3003, invite["token"])
    manual.client.session.headers["X-Telegram-Init-Data"] = signed_data(manual.now[0], actor=3003)
    assert (await manual.client.get(manual.prefix + "/visuals")).status == 200
    response = await manual.client.post(manual.prefix + "/visuals", json={"request": "Стекло", "request_id": "denied"})
    assert response.status == 403
    manual.service.ai.write_post.assert_not_called()


async def test_visual_context_change_and_access_revocation_before_image_call(manual):
    configure(manual)

    async def change_profile(*args):
        b = manual.service.get_business(OWNER, manual.business.id)
        manual.service.save_profile(OWNER, b.id, b.profile_version, BusinessProfile(name=b.name, description="Изменено"))
        return TextDraft(text="Текст", image_brief="Glass")

    manual.service.ai.write_post.side_effect = change_profile
    with pytest.raises(Conflict):
        await manual.service.generate_visual(OWNER, manual.business.id, "Стекло", "changing-profile")
    manual.service.images.generate.assert_not_called()


@pytest.mark.parametrize("error", [ProviderUnavailable("Unavailable"), SafetyBlocked()])
async def test_failed_or_blocked_visual_has_no_asset_and_is_not_retried(manual, error):
    configure(manual)
    manual.service.ai.write_post.side_effect = error
    with pytest.raises(type(error)):
        await manual.service.generate_visual(OWNER, manual.business.id, "Стекло", "failure")
    with pytest.raises(Conflict):
        await manual.service.generate_visual(OWNER, manual.business.id, "Стекло", "failure")
    manual.service.images.generate.assert_not_called()
    assert manual.service.list_visuals(OWNER, manual.business.id) == []


async def test_revoked_operator_cannot_store_finished_visual(manual):
    configure(manual)
    invite = manual.repo.create_access_invite(OWNER, "operator", 1, manual.now[0] + timedelta(hours=1), manual.now[0])
    manual.service.claim_invite(3004, invite["token"])
    began, finish = asyncio.Event(), asyncio.Event()
    original = manual.service.images.generate

    async def image(*args):
        began.set()
        await finish.wait()
        return await original(*args)

    manual.service.images.generate = image
    task = asyncio.create_task(manual.service.generate_visual(3004, manual.business.id, "Стекло", "revoked"))
    await began.wait()
    manual.repo.revoke_access_member(OWNER, 3004, manual.now[0])
    finish.set()
    with pytest.raises(Forbidden):
        await task
    assert manual.service.list_visuals(OWNER, manual.business.id) == []


async def test_post_caption_is_preserved_for_generated_card(manual):
    configure(manual)
    caption = "Точный текст пользователя без изменений."
    response = await manual.client.post(manual.prefix + "/visuals", json={
        "request": "Картинка для поста: " + caption, "caption": caption, "request_id": "existing-caption",
    })
    assert response.status == 200
    assert (await response.json())["draft"]["text"] == caption
    assert manual.service.images.generate.call_args.args[1].text == caption


async def test_caption_is_moderated_before_visual_generation(manual):
    configure(manual)
    caption = "Текст подписи для проверки."
    manual.service.safety.check_text = Mock(side_effect=SafetyBlocked())
    with pytest.raises(SafetyBlocked):
        await manual.service.generate_visual(OWNER, manual.business.id, "Стекло", "blocked-caption", caption=caption)
    manual.service.safety.check_text.assert_called_once_with(["Стекло", caption])
    manual.service.ai.write_post.assert_not_called()
    manual.service.images.generate.assert_not_called()


async def test_local_flux_receives_scene_not_the_chat_json_or_post_text():
    transport = FakeTransport()
    provider = OpenAIImageProvider(api_key="fake-local-test-key", model="flux1-dev",
                                   endpoint="http://127.0.0.1:8194/v1/images/generations",
                                   allow_local_flux=True, transport=transport, timeout_seconds=10)
    draft = TextDraft(text="Текст публикации не надо рисовать.", image_brief="A glass building in a pine forest.")
    await provider.generate(BusinessProfile(name="Company", description=""), draft)
    payload = json.loads(transport.calls[0][0].data)
    assert payload["prompt"].startswith(draft.image_brief)
    assert "Source data:" not in payload["prompt"] and draft.text not in payload["prompt"]
