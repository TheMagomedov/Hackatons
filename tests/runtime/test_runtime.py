"""Composition, CLI, credentials and shutdown tests with external I/O denied."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError

from app.config import Settings
from app.core import runtime as runtime_module
from app.core.demo import run_demo
from app.core.instance_lock import InstanceLock
from app.core.runtime import Runtime, build_runtime, load_factory, run_application
from app.safety import ModeratedAI, ModeratedImages
from app.shared.errors import Conflict, InvalidInput
from app.storage import Repository

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)
ENV_SETTINGS = {
    "APP_MODE",
    "DATA_DIR",
    "OWNER_TELEGRAM_ID",
    "APP_TIMEZONE",
    "TELEGRAM_BOT_TOKEN",
    "AI_PROVIDER_FACTORY",
    "IMAGE_PROVIDER_FACTORY",
    "REPORT_PROVIDER_FACTORY",
    "BOT_FACTORY",
    "PROVIDER_TIMEOUT_SECONDS",
    "MAX_UPLOAD_BYTES",
    "MAX_POSTS_PER_JOB",
    "MAX_PENDING_JOBS",
    "MAX_GENERATION_JOBS_PER_DAY",
    "MAX_PUBLISH_LATENESS_SECONDS",
}
TOKEN = "123456789:runtime-test-secret-do-not-print"


class Closeable:
    is_demo = False

    def __init__(self, events=None, name="resource"):
        self.close_count = 0
        self.events = events
        self.name = name

    async def close(self):
        self.close_count += 1
        if self.events is not None:
            self.events.append(self.name)


class FakeAI(Closeable):
    async def build_profile(self, sources):
        raise AssertionError("Not called while building runtime")

    async def interpret_request(self, text, context):
        raise AssertionError("Not called while building runtime")

    async def make_plan(self, profile, request):
        raise AssertionError("Not called while building runtime")

    async def write_post(self, profile, item):
        raise AssertionError("Not called while building runtime")

    async def revise_text(self, profile, text, feedback):
        raise AssertionError("Not called while building runtime")


class FakeImages(Closeable):
    async def generate(self, profile, text, feedback=None):
        raise AssertionError("Not called while building runtime")


class FakeReport(Closeable):
    def build(self, data):
        return "report"


class FakeBot(Closeable):
    async def run(self, stop):
        await stop.wait()

    async def send(self, notification):
        return None


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.module = types.ModuleType("app.runtime_test_factories")
        self.ai = FakeAI()
        self.images = FakeImages()
        self.reporter = FakeReport()
        self.bot = FakeBot()
        self.publisher = Closeable()
        self.notifier = Closeable()
        self.module.ai = Mock(return_value=self.ai)
        self.module.images = Mock(return_value=self.images)
        self.module.reporter = Mock(return_value=self.reporter)
        self.module.bot = Mock(return_value=self.bot)
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(sys.modules, {self.module.__name__: self.module}))
        self.stack.enter_context(
            patch.object(runtime_module, "TelegramPublisher", return_value=self.publisher)
        )
        self.stack.enter_context(patch.object(runtime_module, "TelegramNotifier", return_value=self.notifier))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def settings(self, **changes):
        values = dict(
            mode="live",
            data_dir=self.directory,
            owner_id=77,
            telegram_bot_token=TOKEN,
            ai_factory="app.runtime_test_factories:ai",
            image_factory="app.runtime_test_factories:images",
            report_factory="app.runtime_test_factories:reporter",
        )
        values.update(changes)
        return Settings(**values)

    async def test_live_factories_receive_settings_and_bot_receives_service(self):
        settings = self.settings(bot_factory="app.runtime_test_factories:bot")
        runtime = await build_runtime(settings)
        self.module.ai.assert_called_once_with(settings)
        self.module.images.assert_called_once_with(settings)
        self.module.reporter.assert_called_once_with(settings)
        self.module.bot.assert_called_once_with(runtime.service, settings)
        self.assertIsInstance(runtime.service.ai, ModeratedAI)
        self.assertIsInstance(runtime.service.images, ModeratedImages)
        self.assertIs(runtime.service.ai.provider, self.ai)
        self.assertIs(runtime.service.images.provider, self.images)
        self.assertIs(runtime.service.ai.policy, runtime.service.safety)
        self.assertIs(runtime.service.images.policy, runtime.service.safety)
        self.assertIs(runtime.service.reporter, self.reporter)
        self.assertIs(runtime.bot, self.bot)
        self.assertIs(runtime.service.notifier, self.bot)
        await runtime.close()
        for resource in (self.ai, self.images, self.reporter, self.bot, self.publisher, self.notifier):
            self.assertEqual(resource.close_count, 1)

    async def test_invalid_image_provider_and_previous_resources_are_closed(self):
        invalid = Closeable()  # Factory allocated a resource but omitted generate.
        self.module.images.return_value = invalid
        with self.assertRaises(InvalidInput):
            await build_runtime(self.settings())
        self.assertEqual(self.ai.close_count, 1)
        self.assertEqual(invalid.close_count, 1)
        self.module.reporter.assert_not_called()

    async def test_demo_provider_rejected_in_live_is_also_closed(self):
        self.images.is_demo = True
        with self.assertRaises(InvalidInput):
            await build_runtime(self.settings())
        self.assertEqual(self.ai.close_count, 1)
        self.assertEqual(self.images.close_count, 1)

    async def test_invalid_bot_is_closed_with_all_previous_resources(self):
        invalid = Closeable()  # Missing run and send.
        self.module.bot.return_value = invalid
        with self.assertRaises(InvalidInput):
            await build_runtime(self.settings(bot_factory="app.runtime_test_factories:bot"))
        self.assertEqual(invalid.close_count, 1)
        for resource in (self.ai, self.images, self.reporter, self.publisher, self.notifier):
            self.assertEqual(resource.close_count, 1)

    async def test_factory_exception_closes_previously_created_provider(self):
        self.module.images.side_effect = RuntimeError("provider construction failed")
        with self.assertRaises(RuntimeError):
            await build_runtime(self.settings())
        self.assertEqual(self.ai.close_count, 1)
        self.module.reporter.assert_not_called()

    async def test_async_factory_is_rejected_without_unawaited_coroutine(self):
        async def async_factory(settings):
            raise AssertionError("The asynchronous factory body must not run")

        self.module.images = async_factory
        with self.assertRaises(InvalidInput):
            await build_runtime(self.settings())
        self.assertEqual(self.ai.close_count, 1)

    async def test_data_owner_mismatch_rejected_before_live_factories(self):
        repository = Repository(self.directory / "backend.sqlite3")
        repository.initialize()
        repository.bind_runtime("live", 88)
        business = repository.create_business(88, "Another owner", "UTC", NOW)
        with self.assertRaises(Conflict):
            await build_runtime(self.settings(owner_id=77))
        self.module.ai.assert_not_called()
        self.assertEqual(repository.get_business(business.id).owner_id, 88)

    async def test_matching_owner_can_reopen_existing_data(self):
        repository = Repository(self.directory / "backend.sqlite3")
        repository.initialize()
        repository.bind_runtime("live", 77)
        business = repository.create_business(77, "Same owner", "UTC", NOW)
        runtime = await build_runtime(self.settings())
        self.assertEqual(runtime.service.repo.get_business(business.id).owner_id, 77)
        await runtime.close()

    async def test_demo_rejects_bot_factory_without_constructing_it(self):
        with self.assertRaises(InvalidInput):
            await build_runtime(
                Settings(mode="demo", data_dir=self.directory, bot_factory="app.runtime_test_factories:bot")
            )
        self.module.bot.assert_not_called()
        self.module.ai.assert_not_called()

    async def test_cleanup_is_reverse_order_deduplicated_and_survives_errors(self):
        events = []
        first = Closeable(events, "first")
        last = Closeable(events, "last")

        class Broken(Closeable):
            async def close(self):
                self.events.append(self.name)
                raise RuntimeError("secret provider error must not be logged")

        broken = Broken(events, "broken")
        runtime = Runtime(service=None, resources=[first, broken, last, first])
        with self.assertLogs("app.core.runtime", level="WARNING") as captured:
            await runtime.close()
        self.assertEqual(events, ["first", "last", "broken"])
        self.assertEqual(first.close_count, 1)
        self.assertNotIn("secret provider error", " ".join(captured.output))

    async def test_one_cycle_runs_expected_operations_and_closes_runtime(self):
        events = []

        class Service:
            def recover_interrupted(self):
                events.append("recover")
                return {"posts": 0, "jobs": 0}

            def schedule_automations(self):
                events.append("schedule")

            async def prepare_next_job(self):
                events.append("prepare")

            async def publish_due_once(self):
                events.append("publish")

            async def collect_metrics(self):
                events.append("metrics")

            async def dispatch_notifications(self):
                events.append("notify")

        resource = Closeable(events, "close")
        runtime = Runtime(Service(), [resource])
        with patch.object(runtime_module, "build_runtime", AsyncMock(return_value=runtime)):
            await run_application(self.settings(), once=True)
        self.assertEqual(events, ["recover", "schedule", "prepare", "publish", "metrics", "notify", "close"])
        with InstanceLock(self.directory / "backend.lock"):
            pass

    async def test_one_cycle_failure_closes_resources_and_releases_lock(self):
        service = Mock()
        service.recover_interrupted.return_value = {"posts": 0, "jobs": 0}
        service.prepare_next_job = AsyncMock(side_effect=RuntimeError("worker failed"))
        resource = Closeable()
        with patch.object(
            runtime_module, "build_runtime", AsyncMock(return_value=Runtime(service, [resource]))
        ):
            with self.assertRaises(RuntimeError):
                await run_application(self.settings(), once=True)
        self.assertEqual(resource.close_count, 1)
        with InstanceLock(self.directory / "backend.lock"):
            pass

    async def test_full_live_run_requires_bot_factory_before_building(self):
        with patch.object(runtime_module, "build_runtime", AsyncMock()) as build:
            with self.assertRaises(InvalidInput):
                await run_application(self.settings(), once=False)
            build.assert_not_awaited()

    async def test_bot_failure_stops_scheduler_and_closes_runtime(self):
        scheduler_stopped = asyncio.Event()

        class ControlledScheduler:
            def __init__(self, service):
                pass

            async def run(self, stop):
                try:
                    await stop.wait()
                finally:
                    scheduler_stopped.set()

        class FailingBot(Closeable):
            async def run(self, stop):
                await asyncio.sleep(0)
                raise RuntimeError("bot stopped")

        service = Mock()
        service.recover_interrupted.return_value = {"posts": 0, "jobs": 0}
        bot = FailingBot()
        runtime = Runtime(service, [bot], bot=bot)
        with (
            patch.object(runtime_module, "build_runtime", AsyncMock(return_value=runtime)),
            patch.object(runtime_module, "Scheduler", ControlledScheduler),
        ):
            with self.assertRaises(RuntimeError):
                await run_application(self.settings(bot_factory="app.runtime_test_factories:bot"))
        self.assertTrue(scheduler_stopped.is_set())
        self.assertEqual(bot.close_count, 1)
        with InstanceLock(self.directory / "backend.lock"):
            pass

    async def test_application_cancellation_cancels_workers_and_releases_lock(self):
        started = asyncio.Event()
        finished = asyncio.Event()

        class ControlledScheduler:
            def __init__(self, service):
                pass

            async def run(self, stop):
                started.set()
                try:
                    await stop.wait()
                finally:
                    finished.set()

        service = Mock()
        service.recover_interrupted.return_value = {"posts": 0, "jobs": 0}
        resource = Closeable()
        runtime = Runtime(service, [resource])
        with (
            patch.object(runtime_module, "build_runtime", AsyncMock(return_value=runtime)),
            patch.object(runtime_module, "Scheduler", ControlledScheduler),
        ):
            task = asyncio.create_task(run_application(Settings(mode="demo", data_dir=self.directory)))
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        self.assertTrue(finished.is_set())
        self.assertEqual(resource.close_count, 1)
        with InstanceLock(self.directory / "backend.lock"):
            pass

    async def test_offline_demo_makes_no_http_or_socket_requests(self):
        with (
            patch(
                "httpx.AsyncClient.send", AsyncMock(side_effect=AssertionError("network forbidden"))
            ) as async_send,
            patch("httpx.Client.send", side_effect=AssertionError("network forbidden")) as sync_send,
            patch("socket.create_connection", side_effect=AssertionError("network forbidden")) as connect,
        ):
            result = await run_demo(self.directory)
        async_send.assert_not_awaited()
        sync_send.assert_not_called()
        connect.assert_not_called()
        self.assertEqual(result["external_requests"], 0)
        self.assertEqual(result["mode"], "offline-demo")
        self.assertEqual(result["simulated_publications"], 1)
        for name in (
            "request_deduplicated",
            "stale_approval_blocked",
            "unauthorized_approval_blocked",
            "persisted_publication_completed",
        ):
            self.assertTrue(result[name], name)
        self.assertEqual(result["automation_jobs_created"], 1)
        self.assertEqual(result["duplicate_automation_jobs"], 0)
        self.assertGreaterEqual(result["unapproved_drafts"], 1)


class ConfigurationTests(unittest.TestCase):
    def test_live_requires_token_and_ai_image_factories(self):
        for values in (
            {"mode": "live", "owner_id": 77},
            {"mode": "live", "owner_id": 77, "telegram_bot_token": TOKEN},
            {"mode": "live", "owner_id": 77, "telegram_bot_token": TOKEN, "ai_factory": "app.fake:build"},
        ):
            with self.subTest(values=list(values)):
                with self.assertRaises(ValidationError):
                    Settings(**values)

    def test_secret_is_redacted_in_repr_and_serialized_config(self):
        settings = Settings(telegram_bot_token=TOKEN)
        self.assertNotIn(TOKEN, repr(settings))
        self.assertNotIn(TOKEN, settings.model_dump_json())

    def test_env_has_precedence_over_dotenv_and_demo_is_default(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "example.env"
            env_file.write_text("OWNER_TELEGRAM_ID=55\nAPP_TIMEZONE=UTC\n", encoding="utf-8")
            with patch.dict(os.environ, {"OWNER_TELEGRAM_ID": "77"}, clear=True):
                settings = Settings.from_env(env_file)
            self.assertEqual(settings.mode, "demo")
            self.assertEqual(settings.owner_id, 77)
            self.assertEqual(settings.timezone, "UTC")

    def test_live_env_requires_explicit_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {
                    "APP_MODE": "live",
                    "TELEGRAM_BOT_TOKEN": TOKEN,
                    "AI_PROVIDER_FACTORY": "app.fake:ai",
                    "IMAGE_PROVIDER_FACTORY": "app.fake:images",
                },
                clear=True,
            ):
                with self.assertRaises((ValidationError, InvalidInput, ValueError)):
                    Settings.from_env(Path(directory) / "absent.env")

    def test_invalid_timezone_and_limits_rejected(self):
        for values in (
            {"timezone": "Not/AZone"},
            {"owner_id": 0},
            {"max_posts_per_job": 6},
            {"provider_timeout_seconds": 0},
            {"max_upload_bytes": 21_000_000},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    Settings(**values)

    def test_factory_target_rejects_external_modules_missing_and_noncallable(self):
        for target in (
            "os:system",
            "app.core.demo.DemoAI",
            "app.absent_module:build",
            "app.core.demo:missing",
            "app.core.demo:DEMO_PNG",
        ):
            with self.subTest(target=target):
                with self.assertRaises(InvalidInput):
                    load_factory(target)


class InstanceLockTests(unittest.TestCase):
    def test_second_lock_rejected_then_released_even_after_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.lock"
            with self.assertRaises(RuntimeError):
                with InstanceLock(path):
                    with self.assertRaises(Conflict):
                        with InstanceLock(path):
                            self.fail("Second lock should not succeed")
                    raise RuntimeError("shutdown")
            with InstanceLock(path):
                pass

    def test_subprocess_lock_cannot_overlap_and_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.lock"
            script = (
                "import sys\nfrom pathlib import Path\n"
                "from app.core.instance_lock import InstanceLock\n"
                "from app.shared.errors import Conflict\n"
                "try:\n    with InstanceLock(Path(sys.argv[1])): print('acquired')\n"
                "except Conflict:\n    print('locked'); raise SystemExit(3)\n"
            )
            with InstanceLock(path):
                denied = subprocess.run(
                    [sys.executable, "-c", script, str(path)],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            self.assertEqual(denied.returncode, 3, denied.stderr)
            allowed = subprocess.run(
                [sys.executable, "-c", script, str(path)],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertIn("acquired", allowed.stdout)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.guard = self.directory / "guard"
        self.guard.mkdir()
        self.marker = self.directory / "network-attempted"
        # sitecustomize runs in the child interpreter before `python -m app`.
        # A marker detects attempts even if application code swallows the error.
        (self.guard / "sitecustomize.py").write_text(
            "import os, socket\nfrom pathlib import Path\nimport httpx\n"
            "def deny(*args, **kwargs):\n"
            "    Path(os.environ['NETWORK_TEST_MARKER']).write_text('attempted')\n"
            "    raise AssertionError('External network disabled by runtime test')\n"
            "async def deny_async(*args, **kwargs):\n    return deny(*args, **kwargs)\n"
            "socket.create_connection = deny\nhttpx.Client.send = deny\n"
            "httpx.AsyncClient.send = deny_async\n",
            encoding="utf-8",
        )
        self.env = {key: value for key, value in os.environ.items() if key not in ENV_SETTINGS}
        self.env.update(
            {
                "APP_MODE": "demo",
                "APP_TIMEZONE": "UTC",
                "OWNER_TELEGRAM_ID": "1",
                "PYTHONPATH": os.pathsep.join((str(self.guard), str(PROJECT_ROOT))),
                "PYTHONIOENCODING": "utf-8",
                "NETWORK_TEST_MARKER": str(self.marker),
            }
        )

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *arguments, environment=None):
        result = subprocess.run(
            [sys.executable, "-m", "app", *arguments, "--env-file", str(self.directory / "absent.env")],
            cwd=PROJECT_ROOT,
            env={**self.env, **(environment or {})},
            capture_output=True,
            encoding="utf-8",
            timeout=20,
        )
        self.assertFalse(self.marker.exists(), "CLI attempted external network access")
        return result

    def test_default_launch_is_isolated_offline_demo_even_with_live_environment(self):
        destination = self.directory / "data"
        result = self.cli(
            "--data-dir", str(destination), environment={"APP_MODE": "live", "TELEGRAM_BOT_TOKEN": TOKEN}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "offline-demo")
        self.assertEqual(payload["external_requests"], 0)
        self.assertEqual(payload["simulated_publications"], 1)
        database = Path(payload["database"])
        self.assertEqual(database.parent.parent, destination)
        self.assertTrue(database.parent.name.startswith("demo-"))
        self.assertTrue(database.exists())
        self.assertFalse((destination / "backend.sqlite3").exists())

    def test_init_check_and_run_once_work_offline(self):
        destination = self.directory / "data"
        for command in ("init", "check", "run"):
            with self.subTest(command=command):
                args = [command, "--data-dir", str(destination)]
                if command == "run":
                    args.append("--once")
                result = self.cli(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
        repository = Repository(destination / "backend.sqlite3")
        self.assertEqual(repository.list_businesses(), [])
        with InstanceLock(destination / "backend.lock"):
            pass

    def test_invalid_live_config_redacts_secrets_without_traceback(self):
        result = self.cli(
            "check",
            "--data-dir",
            str(self.directory / "data"),
            environment={"APP_MODE": "live", "TELEGRAM_BOT_TOKEN": TOKEN},
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("Invalid configuration", result.stderr)

    def test_unknown_factory_fails_safely_without_network(self):
        result = self.cli(
            "check",
            "--data-dir",
            str(self.directory / "data"),
            environment={
                "APP_MODE": "live",
                "TELEGRAM_BOT_TOKEN": TOKEN,
                "AI_PROVIDER_FACTORY": "app.module_that_does_not_exist:build",
                "IMAGE_PROVIDER_FACTORY": "app.module_that_does_not_exist:build",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("invalid_input", result.stderr)


if __name__ == "__main__":
    unittest.main()
