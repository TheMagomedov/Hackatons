"""Run one operation per worker tick without coupling AI to publication.

The service owns durable claims and recovery, including cancellation handling:
cancelled generation must fail its claimed job; cancelled publication must be
marked delivery_unknown when delivery cannot be ruled out. This scheduler never
retries a claimed operation or mutates business state itself.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


class SchedulerService(Protocol):
    """Structural interface implemented by the backend service.

    Each call must do bounded work. schedule_automations is a short local database
    operation; network I/O belongs in the asynchronous methods. Metric collection
    and notification delivery should enforce their own network timeouts.
    """

    async def publish_due_once(self) -> bool: ...

    async def prepare_next_job(self) -> bool: ...

    async def dispatch_notifications(self, limit: int = 20) -> int: ...

    async def collect_metrics(self) -> int: ...

    def schedule_automations(self) -> int: ...


@dataclass(frozen=True, slots=True)
class SchedulerSettings:
    publish_interval_seconds: float = 2.0
    job_interval_seconds: float = 2.0
    notification_interval_seconds: float = 2.0
    metrics_interval_seconds: float = 3600.0
    notification_batch_size: int = 20

    def __post_init__(self) -> None:
        for name in (
            "publish_interval_seconds",
            "job_interval_seconds",
            "notification_interval_seconds",
            "metrics_interval_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and greater than zero")
        if (
            isinstance(self.notification_batch_size, bool)
            or not isinstance(self.notification_batch_size, int)
            or self.notification_batch_size < 1
        ):
            raise ValueError("notification_batch_size must be a positive integer")


class Scheduler:
    """Three workers, with no queue of unbounded background tasks.

    Use ``await Scheduler(service).run(stop_event)``. Setting stop_event or
    cancelling run cancels in-flight calls and awaits their cleanup. Service
    implementations must propagate CancelledError after persisting recovery
    state. Reusing this instance after shutdown is supported; concurrent run
    calls on the same instance are rejected.
    """

    def __init__(
        self,
        service: SchedulerService,
        settings: SchedulerSettings | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.service = service
        self.settings = settings or SchedulerSettings()
        self.logger = logger or logging.getLogger(__name__)
        self._running = False

    async def run(self, stop_event: asyncio.Event) -> None:
        if self._running:
            raise RuntimeError("This scheduler is already running")
        if stop_event.is_set():
            return

        self._running = True
        tasks: list[asyncio.Task[None]] = []
        stop_waiter: asyncio.Task[bool] | None = None
        try:
            tasks = [
                asyncio.create_task(
                    self._operation_loop(
                        "publication",
                        self.service.publish_due_once,
                        self.settings.publish_interval_seconds,
                        stop_event,
                    ),
                    name="scheduler-publication",
                ),
                asyncio.create_task(
                    self._operation_loop(
                        "generation",
                        self.service.prepare_next_job,
                        self.settings.job_interval_seconds,
                        stop_event,
                    ),
                    name="scheduler-generation",
                ),
                asyncio.create_task(
                    self._maintenance_loop(stop_event),
                    name="scheduler-maintenance",
                ),
            ]
            stop_waiter = asyncio.create_task(stop_event.wait(), name="scheduler-stop-waiter")
            await asyncio.wait([*tasks, stop_waiter], return_when=asyncio.FIRST_COMPLETED)
            if not stop_event.is_set():
                raise RuntimeError("A scheduler worker stopped unexpectedly")
        finally:
            pending = [*tasks]
            if stop_waiter is not None:
                pending.append(stop_waiter)
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._running = False

    async def _operation_loop(
        self,
        name: str,
        operation: Callable[[], Awaitable[object]],
        interval: float,
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            await self._call_async(name, operation)
            await self._wait_for_tick(stop_event, interval)

    async def _maintenance_loop(self, stop_event: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        next_metrics_at = loop.time()  # Collect once on startup, then periodically.
        while not stop_event.is_set():
            try:
                self.service.schedule_automations()
            except Exception as error:
                self._log_failure("automation scheduling", error)

            if stop_event.is_set():
                return
            await self._call_async(
                "notifications",
                lambda: self.service.dispatch_notifications(limit=self.settings.notification_batch_size),
            )

            if stop_event.is_set():
                return
            if loop.time() >= next_metrics_at:
                await self._call_async("metrics", self.service.collect_metrics)
                next_metrics_at = loop.time() + self.settings.metrics_interval_seconds
            await self._wait_for_tick(stop_event, self.settings.notification_interval_seconds)

    async def _call_async(self, name: str, operation: Callable[[], Awaitable[object]]) -> None:
        try:
            await operation()
        except Exception as error:
            # CancelledError inherits BaseException and must reach the service
            # and caller unchanged. Never log raw provider errors or tracebacks.
            self._log_failure(name, error)

    def _log_failure(self, name: str, error: Exception) -> None:
        self.logger.warning("Scheduler %s failed (%s)", name, type(error).__name__)

    @staticmethod
    async def _wait_for_tick(stop_event: asyncio.Event, interval: float) -> None:
        if stop_event.is_set():
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            pass
