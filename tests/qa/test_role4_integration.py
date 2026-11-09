"""Offline checks of real role 4 adapters at the BackendService boundary."""

import base64
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.core.demo import DemoAI, DemoNotifier, DemoPublisher
from app.core.service import BackendService
from app.media import DemoImageProvider, OpenAIImageProvider
from app.media.provider import build_provider as build_image_provider
from app.reports.provider import PlainTextReportProvider
from app.reports.provider import build_provider as build_report_provider
from app.reports.service import build_report
from app.shared.errors import Conflict
from app.shared.models import PostStatus, ReportInput
from app.storage import FileStore, Repository

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
OWNER = 51


class InlineImageTransport:
    """Return a real PNG fixture while leaving all external requests disabled."""

    def __init__(self):
        self.requests = []

    def send(self, request, timeout_seconds, max_response_bytes):
        self.requests.append(request)
        return json.dumps({
            "data": [{"b64_json": base64.b64encode(PNG).decode("ascii")}],
        }).encode("utf-8")


class Role4IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="role4-integration-")
        self.addCleanup(temporary.cleanup)
        # Resolve macOS's /var -> /private/var alias before FileStore validation.
        self.root = Path(temporary.name).resolve()
        self.now = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)
        self.settings = Settings(mode="demo", owner_id=OWNER, data_dir=self.root)
        self.repository = Repository(self.root / "backend.sqlite3")
        self.repository.initialize()
        self.addCleanup(self.repository.close)
        self.images = build_image_provider(self.settings)
        self.reporter = build_report_provider(self.settings)
        self.publisher = DemoPublisher()
        self.service = BackendService(
            self.repository,
            FileStore(self.root / "assets"),
            self.settings,
            DemoAI(),
            self.images,
            self.publisher,
            DemoNotifier(),
            self.reporter,
            clock=lambda: self.now,
        )
        business = self.service.create_business(OWNER, "Тестовая кофейня")
        self.service.add_text_source(OWNER, business.id, "Явные тестовые данные кофейни.")
        business = await self.service.analyze_business(OWNER, business.id)
        self.service.confirm_profile(OWNER, business.id, business.profile_version)
        self.business = await self.service.connect_channel(OWNER, business.id, "@demo_channel")

    async def prepare(self):
        job = self.service.request_preparation(OWNER, self.business.id, "Один пост", "update-1")
        self.assertTrue(await self.service.prepare_next_job())
        self.assertEqual(self.service.get_job(OWNER, job.id).status, "done")
        posts = self.service.get_job_posts(OWNER, job.id)
        self.assertEqual(len(posts), 1)
        return posts[0]

    def schedule_and_approve(self, post):
        post = self.service.edit_post(
            OWNER, post.id, post.revision, scheduled_at=self.now + timedelta(minutes=1),
        )
        return self.service.approve_post(OWNER, post.id, post.revision)

    async def test_demo_factory_image_persists_and_revision_requires_reapproval(self):
        self.assertIsInstance(self.images, DemoImageProvider)
        post = await self.prepare()
        asset, content = self.service.read_asset(OWNER, post.image_asset_id)
        self.assertTrue(content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(asset.is_demo)
        self.assertTrue(post.is_demo)
        self.assertEqual(asset.mime_type, "image/png")
        # A fresh storage object must read the same persisted bytes and checksum.
        self.assertEqual(FileStore(self.root / "assets").read(asset), content)

        approved = self.schedule_and_approve(post)
        revised = await self.service.revise_image(
            OWNER, approved.id, approved.revision, "Светлый фон",
        )
        self.assertGreater(revised.revision, approved.revision)
        self.assertNotEqual(revised.image_asset_id, approved.image_asset_id)
        self.assertEqual(revised.status, PostStatus.REVIEW)
        self.assertIsNone(revised.approved_revision)
        self.assertTrue(self.service.read_asset(OWNER, revised.image_asset_id)[0].is_demo)
        with self.assertRaises(Conflict):
            self.service.approve_post(OWNER, revised.id, approved.revision)
        reapproved = self.service.approve_post(OWNER, revised.id, revised.revision)
        self.assertEqual(reapproved.approved_revision, revised.revision)

    async def test_backend_invokes_report_factory_with_shared_input_and_demo_cohort(self):
        self.assertIsInstance(self.reporter, PlainTextReportProvider)
        post = self.schedule_and_approve(await self.prepare())
        self.now += timedelta(minutes=2)
        self.assertTrue(await self.service.publish_due_once())
        self.assertEqual(self.service.get_post(OWNER, post.id).status, PostStatus.PUBLISHED)
        self.assertEqual(await self.service.collect_metrics(), 1)
        start, end = self.now - timedelta(days=1), self.now

        with patch.object(self.reporter, "build", wraps=self.reporter.build) as build:
            text = self.service.build_report(OWNER, self.business.id, start, end)
        build.assert_called_once()
        data = build.call_args.args[0]
        self.assertIsInstance(data, ReportInput)
        self.assertEqual(data.business_id, self.business.id)
        self.assertEqual(
            {sample.source for sample in data.metrics}, {f"demo:{self.business.channel_id}"},
        )
        report = build_report(data)
        cohorts = {activity.is_demo: activity for activity in report.activities}
        self.assertEqual(cohorts[False].created_posts, 0)
        self.assertEqual(cohorts[False].published_posts, 0)
        self.assertEqual(cohorts[True].created_posts, 1)
        self.assertEqual(cohorts[True].published_posts, 1)
        self.assertEqual(len(report.metrics), 1)
        self.assertTrue(report.metrics[0].is_demo)
        self.assertEqual(report.metrics[0].value, 42)
        self.assertIn("Демо-посты:", text)
        self.assertIn("ДЕМО: 42", text)
        self.assertIn("Просмотры, охват, пересылки, клики и продажи: нет данных.", text)

    async def test_live_image_factory_mocked_transport_returns_backend_usable_bytes(self):
        transport = InlineImageTransport()
        live_settings = Settings(
            mode="live", owner_id=OWNER, data_dir=self.root,
            telegram_bot_token="test-token", ai_factory="test:ai", image_factory="test:images",
        )
        images = build_image_provider(
            live_settings, api_key="test-image-key", model="explicit-test-model", transport=transport,
        )
        self.assertIsInstance(images, OpenAIImageProvider)
        # The backend stays in demo mode with DemoAI/Publisher. Only the actual
        # image adapter is exercised as live, through an injected offline transport.
        self.service.images = images
        post = await self.prepare()
        asset, content = self.service.read_asset(OWNER, post.image_asset_id)
        self.assertEqual(content, PNG)
        self.assertEqual(asset.mime_type, "image/png")
        self.assertTrue(asset.is_demo)  # Whole backend scenario remains a demo.
        self.assertEqual(len(transport.requests), 1)
        request = json.loads(transport.requests[0].data)
        self.assertEqual(request["model"], "explicit-test-model")
        self.assertIn(self.business.profile.facts[0].text, request["prompt"])
        self.assertEqual(self.publisher.sent, [])


if __name__ == "__main__":
    unittest.main()
