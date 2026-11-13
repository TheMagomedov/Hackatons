"""Live factory assembly with offline transports: brand, moderate, store, report."""

import base64
import hashlib
import io
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from PIL import Image

from app.config import Settings
from app.core.runtime import build_runtime
from app.media.branding import SalavatBrandingProvider
from app.media.openai import OpenAIImageProvider, UrllibImageTransport
from app.safety import HTTPModeration, ModeratedImages
from app.safety.client import UrllibSafetyTransport
from app.shared.models import BusinessProfile, ContentPlan, Fact, PlanItem, PostStatus, TextDraft
from app.storage import FileStore
from tests.core.test_service import OWNER, Clock, FakeNotifier, FakePublisher


@pytest.mark.parametrize("local_flux", ["0", "1"], ids=["https", "local-flux"])
async def test_live_branded_bytes_are_moderated_saved_and_reported_without_publication(
    tmp_path, monkeypatch, local_flux,
):
    clock, publisher = Clock(), FakePublisher()
    caption = "Стекло для современной архитектуры."
    feedback = "Больше свободного места"
    image_base = "http://127.0.0.1:8194/v1" if local_flux == "1" else "https://images.example.invalid/v1"
    safety_base = image_base if local_flux == "1" else "https://safety.example.invalid/v1"
    image_model = "flux1-dev" if local_flux == "1" else "fixture-image-model"
    expected_provider = "flux1-dev" if local_flux == "1" else "openai"
    image_requests, moderation_requests, network_attempts = [], [], []
    fixture = io.BytesIO()
    Image.new("RGB", (512, 256), "#417eb1").save(fixture, format="PNG")
    source_png = fixture.getvalue()

    def deny_network(*args, **kwargs):
        network_attempts.append(True)
        raise AssertionError("Only injected offline transports may run")

    monkeypatch.setattr("socket.socket.connect", deny_network)
    monkeypatch.setattr("socket.socket.connect_ex", deny_network)
    monkeypatch.setattr("socket.create_connection", deny_network)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", deny_network)

    def image_send(_transport, request, _timeout, _limit):
        image_requests.append(json.loads(request.data))
        assert request.full_url == image_base + "/images/generations"
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bearer fixture-image-key"
        return json.dumps({"data": [{"b64_json": base64.b64encode(source_png).decode("ascii")}]}).encode()

    def moderation_send(_transport, request, _timeout):
        moderation_requests.append(request)
        assert request.full_url in {safety_base + "/safety/check-text", safety_base + "/safety/check-image"}
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bearer fixture-safety-key"
        return b'{"allowed":true,"reason":"offline fixture","category":"safe"}'

    class TextFixture:
        async def make_plan(self, profile, request):
            return ContentPlan(items=[PlanItem(
                topic="Стекло", goal="Проверить интеграцию", brief="Один тестовый пост",
                image_brief="Стеклянный фасад без надписей", suggested_at=clock() + timedelta(minutes=1),
            )])

        async def write_post(self, profile, item):
            return TextDraft(text=caption, image_brief=item.image_brief,
                             used_source_ids=profile.facts[0].source_ids)

        async def unused(self, *args, **kwargs):
            raise AssertionError("This scenario does not request other text operations")

        build_profile = interpret_request = revise_text = unused

    monkeypatch.setattr("app.ai.provider.build_provider", lambda _settings: TextFixture())
    monkeypatch.setattr("app.core.runtime.TelegramPublisher", lambda _token: publisher)
    monkeypatch.setattr("app.core.runtime.TelegramNotifier", lambda _token: FakeNotifier())
    monkeypatch.setattr(UrllibImageTransport, "send", image_send)
    monkeypatch.setattr(UrllibSafetyTransport, "send", moderation_send)
    for name, value in {
        "IMAGE_API_KEY": "fixture-image-key", "IMAGE_MODEL": image_model,
        "IMAGE_API_BASE": image_base, "IMAGE_LOCAL_FLUX": local_flux,
        "MODERATION_API_KEY": "fixture-safety-key", "MODERATION_API_BASE": safety_base,
    }.items():
        monkeypatch.setenv(name, value)
    runtime = await build_runtime(Settings(
        mode="live", owner_id=OWNER, data_dir=tmp_path, telegram_bot_token="12345:fixture-only-key",
        ai_factory="app.ai.provider:build_provider", image_factory="app.media.runtime:build_provider",
        report_factory="app.reports.provider:build_provider",
    ))
    service = runtime.service
    service.clock = clock
    try:
        assert isinstance(service.images, ModeratedImages)
        assert isinstance(service.images.provider, SalavatBrandingProvider)
        assert isinstance(service.images.provider._provider, OpenAIImageProvider)
        image_provider = service.images.provider._provider
        assert image_provider._endpoint == image_base + "/images/generations"
        assert image_provider._model == image_model
        assert image_provider._local_flux is (local_flux == "1")
        assert isinstance(service.safety, HTTPModeration)
        assert service.images.policy is service.safety
        business = service.create_business(OWNER, "АО «Салаватстекло»")
        source = service.add_text_source(OWNER, business.id, "Стекло для современной архитектуры.")
        business = service.save_profile(OWNER, business.id, 0, BusinessProfile(
            name=business.name, description="Синтетический профиль для проверки интеграции.",
            facts=[Fact(text=source.text, source_ids=[source.id])],
        ))
        service.confirm_profile(OWNER, business.id, business.profile_version)
        business = await service.connect_channel(OWNER, business.id, "-100fixture")
        assert business.profile_confirmed
        with (
            patch.object(service, "_store_image", wraps=service._store_image) as store_image,
            patch.object(image_provider, "generate", wraps=image_provider.generate) as generate,
        ):
            job = service.request_preparation(OWNER, business.id, "Один тестовый пост", "branded-pipeline")
            assert await service.prepare_next_job()
            assert service.get_job(OWNER, job.id).status == "done"
            post, = service.get_job_posts(OWNER, job.id)
            approved = service.approve_post(OWNER, post.id, post.revision)
            assert approved.approved_revision == approved.revision
            revised = await service.revise_image(
                OWNER, post.id, approved.revision, feedback, request_id="branded-revision",
            )
        assert [call.args[2] for call in generate.await_args_list] == [None, feedback]
        assert generate.await_args.args[1].image_brief == feedback
        assert revised.status == PostStatus.REVIEW and revised.revision > approved.revision
        assert service.get_post(OWNER, revised.id) == revised
        assert revised.approved_revision is None and revised.approved_by is None
        assert revised.image_asset_id != approved.image_asset_id
        assert not post.is_demo and not revised.is_demo
        assert post.used_source_ids == [source.id]
        image_checks = [request for request in moderation_requests if request.full_url.endswith("check-image")]
        assert len(image_checks) == store_image.call_count == len(image_requests) == 2
        for saved_post, stored, checked in zip((post, revised), store_image.call_args_list, image_checks):
            generated = stored.args[1]
            asset, content = service.read_asset(OWNER, saved_post.image_asset_id)
            assert generated.provider == expected_provider and generated.is_demo is False
            assert checked.data == generated.content == content and content != source_png
            assert checked.get_header("Content-type") == asset.mime_type == "image/png"
            assert not asset.is_demo and hashlib.sha256(content).hexdigest() == asset.sha256
            assert FileStore(tmp_path / "assets").read(asset) == content
            with Image.open(io.BytesIO(content)) as card:
                assert card.size[0] == 512 and card.size[1] > 256  # Header added to the full source photo.
        assert all(request["model"] == image_model for request in image_requests)
        assert feedback in image_requests[-1]["prompt"]
        clock.advance(minutes=2)
        assert not await service.publish_due_once()
        report = service.build_report(OWNER, business.id, clock() - timedelta(hours=1), clock())
        assert "Реальные посты:\nСоздано за период: 1\nОпубликовано за период: 0" in report
        assert "Демо-посты:" not in report and "Реакции и подписчики: нет данных." in report
        assert publisher.calls == [] and network_attempts == []
    finally:
        await runtime.close()
