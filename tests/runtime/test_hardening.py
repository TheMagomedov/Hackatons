"""Runtime identity and worker supervision regressions; no real providers used."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.config import Settings
from app.core import runtime as runtime_module
from app.core.demo import DEMO_PNG
from app.core.instance_lock import InstanceLock
from app.core.runtime import Runtime, build_runtime, run_application
from app.shared.errors import Conflict, InvalidInput
from app.shared.models import BusinessProfile, ChannelInfo, Notification
from app.storage import FileStore, Repository

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)
TOKEN = "123456:explicit-owner-secret"


def live_values(**changes):
    values = dict(
        mode="live",
        telegram_bot_token=TOKEN,
        ai_factory="app.hardening_fake:ai",
        image_factory="app.hardening_fake:images",
    )
    values.update(changes)
    return values


class SettingsIdentityTests(unittest.TestCase):
    def test_live_constructor_requires_explicit_owner_without_leaking_input(self):
        with self.assertRaises(InvalidInput) as caught:
            Settings(**live_values())
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertNotIn(TOKEN, repr(caught.exception))

    def test_live_model_validate_requires_explicit_owner(self):
        with self.assertRaises(InvalidInput):
            Settings.model_validate(live_values())

    def test_demo_default_and_explicit_live_owner_remain_supported(self):
        self.assertEqual(Settings().owner_id, 1)
        self.assertEqual(Settings(**live_values(owner_id=77)).owner_id, 77)


class RuntimeBindingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def seed_bound_data(self, mode):
        repo = Repository(self.directory / "backend.sqlite3")
        repo.initialize()
        repo.bind_runtime(mode, 77)
        business = repo.create_business(77, "Existing business", "UTC", NOW)
        repo.set_profile(
            business.id, BusinessProfile(name="Existing business", description="Known facts"), NOW
        )
        repo.confirm_profile(business.id, 1, NOW)
        repo.set_channel(business.id, ChannelInfo(id="-1001234567890", title="Channel"), NOW)
        files = FileStore(self.directory / "assets")
        asset = files.write(business.id, "image.png", DEMO_PNG, "image/png", NOW, is_demo=mode == "demo")
        repo.add_asset(asset)
        post = repo.create_posts(
            business.id,
            [
                {
                    "text": "Existing approved content",
                    "image_asset_id": asset.id,
                    "channel_id": "-1001234567890",
                    "scheduled_at": NOW + timedelta(minutes=1),
                }
            ],
            NOW,
        )[0]
        approved = repo.approve_post(post.id, post.revision, 77, NOW)
        job = repo.enqueue_job(business.id, "Existing generation", "existing-job", NOW)
        notification = Notification(
            id="existing-notification",
            business_id=business.id,
            owner_id=77,
            kind="ready",
            text="Existing notification",
            created_at=NOW,
        )
        repo.add_notification(notification)
        return repo, approved, job, notification

    async def test_mode_mismatch_fails_before_factories_and_preserves_data(self):
        for source_mode, target_mode in (("live", "demo"), ("demo", "live")):
            with self.subTest(source_mode=source_mode, target_mode=target_mode):
                # Give each direction an independent database.
                self.directory = Path(self.temp.name) / source_mode
                repo, post, job, notification = self.seed_bound_data(source_mode)
                settings = (
                    Settings(mode="demo", owner_id=77, data_dir=self.directory)
                    if target_mode == "demo"
                    else Settings(**live_values(owner_id=77, data_dir=self.directory))
                )
                with (
                    patch.object(runtime_module, "load_factory") as factory,
                    patch.object(runtime_module, "DemoAI") as demo_factory,
                ):
                    with self.assertRaises(Conflict):
                        await build_runtime(settings)
                factory.assert_not_called()
                demo_factory.assert_not_called()
                self.assertEqual(repo.get_post(post.id), post)
                self.assertEqual(repo.get_job(job.id), job)
                self.assertEqual(repo.pending_notifications(20, now=NOW), [notification])
                self.assertEqual(repo.runtime_binding()["mode"], source_mode)

    async def test_populated_unbound_database_requires_explicit_adoption(self):
        repo = Repository(self.directory / "backend.sqlite3")
        repo.initialize()
        business = repo.create_business(77, "Legacy business", "UTC", NOW)
        with patch.object(runtime_module, "DemoAI") as demo_factory:
            with self.assertRaises(Conflict):
                await build_runtime(Settings(mode="demo", owner_id=77, data_dir=self.directory))
        demo_factory.assert_not_called()
        self.assertIsNone(repo.runtime_binding())
        self.assertEqual(repo.get_business(business.id), business)

    async def test_new_empty_database_is_bound_automatically(self):
        runtime = await build_runtime(Settings(mode="demo", owner_id=77, data_dir=self.directory))
        try:
            binding = runtime.service.repo.runtime_binding()
            self.assertEqual(binding["mode"], "demo")
            self.assertEqual(binding["owner_id"], 77)
        finally:
            await runtime.close()


class RuntimeSupervisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_normal_worker_return_fails_and_releases_resources(self):
        for exits in ("bot", "scheduler"):
            with self.subTest(exits=exits), tempfile.TemporaryDirectory() as directory:
                closed = asyncio.Event()
                peer_stopped = asyncio.Event()

                class Worker:
                    async def close(self):
                        closed.set()

                    async def run(self, stop):
                        if exits == "bot":
                            return
                        try:
                            await stop.wait()
                        finally:
                            peer_stopped.set()

                class ControlledScheduler:
                    def __init__(self, service):
                        pass

                    async def run(self, stop):
                        if exits == "scheduler":
                            return
                        try:
                            await stop.wait()
                        finally:
                            peer_stopped.set()

                service = Mock()
                service.recover_interrupted.return_value = {"posts": 0, "jobs": 0}
                bot = Worker()
                runtime = Runtime(service, [bot], bot=bot)
                with (
                    patch.object(runtime_module, "build_runtime", AsyncMock(return_value=runtime)),
                    patch.object(runtime_module, "Scheduler", ControlledScheduler),
                ):
                    with self.assertRaisesRegex(RuntimeError, "worker stopped unexpectedly"):
                        await run_application(Settings(mode="demo", data_dir=Path(directory)))
                self.assertTrue(closed.is_set())
                self.assertTrue(peer_stopped.is_set())
                with InstanceLock(Path(directory) / "backend.lock"):
                    pass

    async def test_requested_stop_remains_successful(self):
        class StoppingBot:
            async def run(self, stop):
                stop.set()

        class ControlledScheduler:
            def __init__(self, service):
                pass

            async def run(self, stop):
                await stop.wait()

        service = Mock()
        service.recover_interrupted.return_value = {"posts": 0, "jobs": 0}
        runtime = Runtime(service, [], bot=StoppingBot())
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(runtime_module, "build_runtime", AsyncMock(return_value=runtime)),
            patch.object(runtime_module, "Scheduler", ControlledScheduler),
        ):
            await run_application(Settings(mode="demo", data_dir=Path(directory)))
