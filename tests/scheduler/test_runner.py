from __future__ import annotations

import asyncio
import logging
import unittest

from app.scheduler import Scheduler, SchedulerSettings


class FakeService:
    def __init__(self) -> None:
        self.publish_calls = 0
        self.generation_calls = 0
        self.notification_calls = 0
        self.automation_calls = 0
        self.metrics_calls = 0
        self.notification_limits: list[int] = []
        self.generation_started = asyncio.Event()
        self.generation_cancelled = asyncio.Event()
        self.generation_release = asyncio.Event()
        self.progress = asyncio.Event()

    async def publish_due_once(self) -> bool:
        self.publish_calls += 1
        self._update_progress()
        return True

    async def prepare_next_job(self) -> bool:
        self.generation_calls += 1
        self.generation_started.set()
        try:
            await self.generation_release.wait()
        except asyncio.CancelledError:
            # Simulate the core persisting a failed generation before exiting.
            await asyncio.sleep(0)
            self.generation_cancelled.set()
            raise
        return True

    async def dispatch_notifications(self, limit: int = 20) -> int:
        self.notification_calls += 1
        self.notification_limits.append(limit)
        self._update_progress()
        return 0

    def schedule_automations(self) -> int:
        self.automation_calls += 1
        return 0

    async def collect_metrics(self) -> int:
        self.metrics_calls += 1
        self._update_progress()
        return 0

    def _update_progress(self) -> None:
        if self.publish_calls >= 3 and self.notification_calls >= 3:
            self.progress.set()


def fast_settings(**overrides: object) -> SchedulerSettings:
    settings = dict(
        publish_interval_seconds=0.005,
        job_interval_seconds=0.005,
        notification_interval_seconds=0.005,
        metrics_interval_seconds=3600.0,
        notification_batch_size=7,
    )
    settings.update(overrides)
    return SchedulerSettings(**settings)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_generation_does_not_stall_publication_or_notifications(self) -> None:
        service = FakeService()
        stop = asyncio.Event()
        task = asyncio.create_task(Scheduler(service, fast_settings()).run(stop))
        try:
            await asyncio.wait_for(service.generation_started.wait(), timeout=1)
            await asyncio.wait_for(service.progress.wait(), timeout=1)
            self.assertEqual(service.generation_calls, 1)
            self.assertEqual(set(service.notification_limits), {7})
            self.assertEqual(service.metrics_calls, 1)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=1)
        self.assertTrue(service.generation_cancelled.is_set())

    async def test_shutdown_cancels_publication_and_awaits_service_cleanup(self) -> None:
        class PublishingService(FakeService):
            def __init__(self) -> None:
                super().__init__()
                self.publish_started = asyncio.Event()
                self.delivery_unknown = False

            async def publish_due_once(self) -> bool:
                self.publish_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await asyncio.sleep(0)
                    self.delivery_unknown = True
                    raise

        service = PublishingService()
        stop = asyncio.Event()
        scheduler = Scheduler(service, fast_settings())
        task = asyncio.create_task(scheduler.run(stop))
        try:
            await asyncio.wait_for(service.publish_started.wait(), timeout=1)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=1)
        self.assertTrue(service.delivery_unknown)
        self.assertTrue(service.generation_cancelled.is_set())
        self.assertFalse(
            any(
                pending.get_name().startswith("scheduler-")
                for pending in asyncio.all_tasks()
                if not pending.done()
            )
        )

    async def test_external_cancellation_propagates_and_cleans_up_workers(self) -> None:
        service = FakeService()
        task = asyncio.create_task(Scheduler(service, fast_settings()).run(asyncio.Event()))
        await asyncio.wait_for(service.generation_started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(service.generation_cancelled.is_set())

    async def test_iteration_failures_are_isolated_and_do_not_log_secret_text(self) -> None:
        secret = "DO_NOT_LOG_PROVIDER_TOKEN"
        metrics_recovered = asyncio.Event()

        class FailingService(FakeService):
            async def publish_due_once(self) -> bool:
                await super().publish_due_once()
                if self.publish_calls == 1:
                    raise RuntimeError(secret)
                return True

            async def prepare_next_job(self) -> bool:
                if self.generation_calls == 0:
                    self.generation_calls += 1
                    raise ValueError(secret)
                return await super().prepare_next_job()

            async def dispatch_notifications(self, limit: int = 20) -> int:
                await super().dispatch_notifications(limit)
                if self.notification_calls == 1:
                    raise OSError(secret)
                return 0

            def schedule_automations(self) -> int:
                super().schedule_automations()
                if self.automation_calls == 1:
                    raise RuntimeError(secret)
                return 0

            async def collect_metrics(self) -> int:
                await super().collect_metrics()
                if self.metrics_calls == 1:
                    raise ValueError(secret)
                metrics_recovered.set()
                return 0

        service = FailingService()
        stop = asyncio.Event()
        logger = logging.getLogger("scheduler-test-safe-logging")
        with self.assertLogs(logger, level="WARNING") as captured:
            task = asyncio.create_task(
                Scheduler(service, fast_settings(metrics_interval_seconds=0.001), logger).run(stop)
            )
            try:
                await asyncio.wait_for(service.progress.wait(), timeout=1)
                await asyncio.wait_for(service.generation_started.wait(), timeout=1)
                # Other workers' progress does not imply a metrics retry ran.
                await asyncio.wait_for(metrics_recovered.wait(), timeout=1)
            finally:
                stop.set()
                await asyncio.wait_for(task, timeout=1)
        self.assertEqual(len(captured.output), 5)
        self.assertNotIn(secret, "\n".join(captured.output))
        self.assertGreaterEqual(service.metrics_calls, 2)
        self.assertEqual(service.generation_calls, 2)

    async def test_stop_interrupts_long_polling_sleep(self) -> None:
        service = FakeService()
        service.generation_release.set()
        stop = asyncio.Event()
        task = asyncio.create_task(
            Scheduler(
                service,
                SchedulerSettings(
                    publish_interval_seconds=3600,
                    job_interval_seconds=3600,
                    notification_interval_seconds=3600,
                ),
            ).run(stop)
        )
        await asyncio.wait_for(service.generation_started.wait(), timeout=1)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        self.assertEqual(service.publish_calls, 1)
        self.assertEqual(service.generation_calls, 1)

    async def test_already_stopped_event_runs_no_service_work(self) -> None:
        service = FakeService()
        stop = asyncio.Event()
        stop.set()
        await Scheduler(service, fast_settings()).run(stop)
        self.assertEqual(service.publish_calls, 0)
        self.assertEqual(service.generation_calls, 0)
        self.assertEqual(service.automation_calls, 0)

    async def test_rejects_concurrent_run_and_can_restart_after_shutdown(self) -> None:
        service = FakeService()
        scheduler = Scheduler(service, fast_settings())
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler.run(stop))
        try:
            await asyncio.wait_for(service.generation_started.wait(), timeout=1)
            with self.assertRaisesRegex(RuntimeError, "already running"):
                await scheduler.run(asyncio.Event())
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=1)

        next_stop = asyncio.Event()
        service.generation_started.clear()
        next_task = asyncio.create_task(scheduler.run(next_stop))
        try:
            await asyncio.wait_for(service.generation_started.wait(), timeout=1)
        finally:
            next_stop.set()
            await asyncio.wait_for(next_task, timeout=1)
        self.assertEqual(service.generation_calls, 2)


class SettingsTests(unittest.TestCase):
    def test_invalid_intervals_are_rejected(self) -> None:
        for name in (
            "publish_interval_seconds",
            "job_interval_seconds",
            "notification_interval_seconds",
            "metrics_interval_seconds",
        ):
            for value in (0, -1, float("nan"), float("inf")):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        SchedulerSettings(**{name: value})

    def test_invalid_batch_sizes_are_rejected(self) -> None:
        for value in (0, -1, 1.5, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    SchedulerSettings(notification_batch_size=value)


if __name__ == "__main__":
    unittest.main()
