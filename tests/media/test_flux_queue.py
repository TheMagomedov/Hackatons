import asyncio
import io
import json
import threading
import urllib.error
from unittest.mock import AsyncMock, patch

import pytest

from app.media.errors import MediaRateLimitError, MediaTimeoutError, MediaUnavailableError
from app.media.openai import OpenAIImageProvider
from tests.media.test_media import PROFILE, TEXT, FakeTransport, encoded_response


def local(transport, timeout=2):
    return OpenAIImageProvider(api_key="fixture-secret", model="flux1-dev", timeout_seconds=timeout,
                               endpoint="http://127.0.0.1:8194/v1/images/generations",
                               allow_local_flux=True, transport=transport)


def http_error(status=429, code="busy"):
    return urllib.error.HTTPError("http://127.0.0.1:8194/v1/images/generations", status, "fixture-secret", {},
                                  io.BytesIO(json.dumps({"error": {"code": code}}).encode()))


class Occupied:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def send(self, *args):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            assert self.release.wait(3), "Test did not release the first generation"
        return encoded_response()


async def entered(transport):
    async with asyncio.timeout(1):
        while not transport.entered.is_set():
            await asyncio.sleep(.001)


async def test_simultaneous_flux_requests_wait_for_previous_image():
    transport = Occupied()
    adapter = local(transport)
    first = asyncio.create_task(adapter.generate(PROFILE, TEXT))
    second = None
    try:
        await entered(transport)
        second = asyncio.create_task(adapter.generate(PROFILE, TEXT))
        await asyncio.sleep(.02)
        assert transport.calls == 1 and not second.done()
        transport.release.set()
        results = await asyncio.gather(first, second)
        assert len(results) == 2 and transport.calls == 2
        assert all(result.provider == "flux1-dev" for result in results)
    finally:
        transport.release.set()
        await asyncio.gather(*[t for t in (first, second) if t is not None], return_exceptions=True)


async def test_cancelled_waiter_never_submits_and_cancelled_owner_keeps_lock_until_thread_finishes():
    transport = Occupied()
    adapter = local(transport)
    first = asyncio.create_task(adapter.generate(PROFILE, TEXT))
    tasks = [first]
    try:
        await entered(transport)
        waiter = asyncio.create_task(adapter.generate(PROFILE, TEXT))
        tasks.append(waiter)
        await asyncio.sleep(.01)
        waiter.cancel()
        first.cancel()
        await asyncio.gather(first, waiter, return_exceptions=True)
        following = asyncio.create_task(adapter.generate(PROFILE, TEXT))
        tasks.append(following)
        await asyncio.sleep(.02)
        assert transport.calls == 1 and not following.done()
        transport.release.set()
        await following
        assert transport.calls == 2
    finally:
        transport.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_timed_out_worker_does_not_release_gpu_or_retry_generation():
    transport = Occupied()
    adapter = local(transport, timeout=.05)
    try:
        with pytest.raises(MediaTimeoutError):
            await adapter.generate(PROFILE, TEXT)
        with pytest.raises(MediaTimeoutError):
            await adapter.generate(PROFILE, TEXT)
        assert transport.calls == 1
    finally:
        transport.release.set()
    async with asyncio.timeout(1):
        while adapter._local_lock.locked():
            await asyncio.sleep(.001)
    await adapter.generate(PROFILE, TEXT)
    assert transport.calls == 2


async def test_only_confirmed_local_busy_response_can_wait_and_submit_once_accepted():
    class BusyOnce(FakeTransport):
        def send(self, *args):
            self.calls.append(args)
            if len(self.calls) == 1:
                raise http_error()
            return encoded_response()

    transport = BusyOnce()
    with patch("app.media.openai.asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await local(transport).generate(PROFILE, TEXT)
    assert result.provider == "flux1-dev" and len(transport.calls) == 2
    sleep.assert_awaited_once()
    assert transport.calls[1][1] <= transport.calls[0][1]


async def test_persistent_busy_has_bounded_wait_and_russian_message():
    transport = FakeTransport(error=http_error())
    with pytest.raises(MediaTimeoutError, match="FLUX"):
        await local(transport, timeout=.02).generate(PROFILE, TEXT)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status,code,expected", [(429, "quota", MediaRateLimitError),
                                                 (503, "busy", MediaUnavailableError),
                                                 (504, "busy", MediaTimeoutError)])
async def test_ambiguous_or_non_busy_errors_never_repeat(status, code, expected):
    error = http_error(status, code)
    transport = FakeTransport(error=error)
    with pytest.raises(expected) as caught:
        await local(transport).generate(PROFILE, TEXT)
    assert "fixture-secret" not in str(caught.value)
    assert len(transport.calls) == 1
    assert error.fp.closed


async def test_external_busy_error_is_not_treated_as_local_queue():
    transport = FakeTransport(error=http_error())
    adapter = OpenAIImageProvider(api_key="fixture-secret", model="external-image", timeout_seconds=1,
                                   transport=transport)
    with pytest.raises(MediaRateLimitError, match="внешнего"):
        await adapter.generate(PROFILE, TEXT)
    assert len(transport.calls) == 1
