import asyncio
import base64
import binascii
import io
import json
import socket
import struct
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from pydantic import ValidationError

from app.config import Settings
from app.core.runtime import _provider as runtime_provider
from app.media import (
    BusinessProfile,
    DemoImageProvider,
    GeneratedImage,
    InvalidImageRequestError,
    InvalidImageResponseError,
    MediaAuthenticationError,
    MediaConfigurationError,
    MediaLimits,
    MediaRateLimitError,
    MediaTimeoutError,
    MediaUnavailableError,
    OpenAIImageProvider,
    TextDraft,
    UrllibImageTransport,
    generate_image,
)
from app.media.openai import _NoRedirects
from app.media.provider import build_provider
from app.media.validation import build_image_prompt, validate_png
from app.shared.errors import InvalidInput, ProviderUnavailable
from app.shared.models import Fact

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
PROFILE = BusinessProfile(
    name="Кофейня",
    description="",
    facts=[Fact(text="Кофейня открыта с 09:00")],
    assumptions=["Непроверенная цена 100"],
)
TEXT = TextDraft(text="Зайдите на чашку кофе", image_brief="Чашка кофе на столе")


def encoded_response(image=PNG):
    return json.dumps({"data": [{"b64_json": base64.b64encode(image).decode("ascii")}]}).encode()


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = encoded_response() if response is None else response
        self.error = error
        self.calls = []

    def send(self, request, timeout_seconds, max_response_bytes):
        self.calls.append((request, timeout_seconds, max_response_bytes, threading.get_ident()))
        if self.error is not None:
            raise self.error
        return self.response


class BlockingTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.starts = 0

    def send(self, request, timeout_seconds, max_response_bytes):
        self.starts += 1
        self.entered.set()
        self.release.wait(timeout=2)
        return super().send(request, timeout_seconds, max_response_bytes)


class StaticProvider:
    def __init__(self, draft):
        self.draft = draft
        self.calls = 0

    async def generate(self, profile, text, feedback=None):
        self.calls += 1
        return self.draft


def provider(transport, **kwargs):
    return OpenAIImageProvider(
        api_key="test-secret-key",
        model="explicit-image-model",
        timeout_seconds=1,
        transport=transport,
        **kwargs,
    )


class MediaServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_injected_provider_returns_shared_frozen_model(self):
        draft = GeneratedImage(content=PNG, mime_type="image/png", provider="fake")
        fake = StaticProvider(draft)
        result = await generate_image(PROFILE, TEXT, provider=fake)
        self.assertIs(result, draft)
        self.assertEqual(fake.calls, 1)
        self.assertFalse(result.is_demo)
        with self.assertRaises(ValidationError):
            result.provider = "other"

    async def test_demo_is_explicitly_marked(self):
        draft = await generate_image(PROFILE, TEXT, provider=DemoImageProvider())
        self.assertTrue(draft.is_demo)
        self.assertEqual(draft.provider, "demo")
        validate_png(draft.content, MediaLimits())

    async def test_missing_provider_is_a_configuration_error(self):
        with self.assertRaises(MediaConfigurationError):
            await generate_image(PROFILE, TEXT, provider=None)

    async def test_input_errors_prevent_provider_calls(self):
        fake = StaticProvider(GeneratedImage(content=PNG, mime_type="image/png", provider="fake"))
        requests = [
            (None, TEXT, None),
            (PROFILE, None, None),
            (PROFILE.model_copy(update={"facts": ["bad"]}), TEXT, None),
            (PROFILE.model_copy(update={"facts": [Fact(text=" ")]}), TEXT, None),
            (PROFILE.model_copy(update={"facts": [Fact(text="x")] * 21}), TEXT, None),
            (PROFILE.model_copy(update={"facts": [Fact(text="x" * 2000)] * 8}), TEXT, None),
            (PROFILE.model_copy(update={"tone": "x" * 2001}), TEXT, None),
            (PROFILE.model_copy(update={"constraints": ["x"] * 21}), TEXT, None),
            (PROFILE, TEXT.model_copy(update={"text": " "}), None),
            (PROFILE, TEXT.model_copy(update={"image_brief": ""}), None),
            (PROFILE, TEXT.model_copy(update={"image_brief": "x" * 4001}), None),
            (PROFILE, TEXT, ""),
            (PROFILE, TEXT, "x" * 4001),
        ]
        for profile, text, feedback in requests:
            with self.subTest(profile=profile, text=text, feedback=feedback):
                with self.assertRaises(InvalidImageRequestError):
                    await generate_image(profile, text, feedback, provider=fake)
        self.assertEqual(fake.calls, 0)

    async def test_invalid_provider_results_are_rejected(self):
        valid = GeneratedImage(content=PNG, mime_type="image/png", provider="fake")
        drafts = [
            None,
            valid.model_copy(update={"content": b""}),
            valid.model_copy(update={"content": b"bad"}),
            valid.model_copy(update={"mime_type": "image/jpeg"}),
            valid.model_copy(update={"is_demo": "yes"}),
            valid.model_copy(update={"provider": ""}),
        ]
        for draft in drafts:
            with self.subTest(draft=draft):
                with self.assertRaises(InvalidImageResponseError):
                    await generate_image(PROFILE, TEXT, provider=StaticProvider(draft))

    async def test_provider_failure_is_not_retried_or_replaced_with_demo(self):
        class FailingProvider:
            calls = 0

            async def generate(self, profile, text, feedback=None):
                self.calls += 1
                raise MediaUnavailableError("Unavailable")

        fake = FailingProvider()
        with self.assertRaises(MediaUnavailableError):
            await generate_image(PROFILE, TEXT, provider=fake)
        self.assertEqual(fake.calls, 1)

    async def test_factory_demo_and_live_use_the_shared_protocol(self):
        transport = FakeTransport()
        demo = build_provider(Settings(), transport=transport)
        self.assertTrue(demo.is_demo)
        image = await demo.generate(PROFILE, TEXT, "Больше света")
        self.assertTrue(image.is_demo)
        self.assertEqual(transport.calls, [])

        settings = Settings(
            mode="live",
            telegram_bot_token="test-bot-token",
            ai_factory="test:ai",
            image_factory="test:image",
            provider_timeout_seconds=5,
        )
        live = build_provider(
            settings, api_key="test-secret-key", model="explicit-image-model", transport=transport
        )
        image = await live.generate(PROFILE, TEXT)
        self.assertFalse(live.is_demo)
        self.assertFalse(image.is_demo)
        self.assertEqual(image.provider, "openai")
        self.assertEqual(transport.calls[0][1], 5)

    async def test_factory_live_without_injected_config_never_falls_back_to_demo(self):
        settings = Settings(
            mode="live",
            telegram_bot_token="test-bot-token",
            ai_factory="test:ai",
            image_factory="test:image",
        )
        for kwargs in ({}, {"api_key": "test-secret-key"}, {"model": "explicit-image-model"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MediaConfigurationError) as raised:
                build_provider(settings, **kwargs)
            self.assertIsInstance(raised.exception, InvalidInput)
            self.assertIn("wrapper factory", str(raised.exception))
            self.assertNotIn("test-secret-key", str(raised.exception))


class OpenAIAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_explicit_request_and_png_response(self):
        transport = FakeTransport()
        result = await generate_image(PROFILE, TEXT, provider=provider(transport))
        self.assertEqual(result, GeneratedImage(content=PNG, mime_type="image/png", provider="openai"))
        self.assertEqual(len(transport.calls), 1)
        request, timeout, limit, thread_id = transport.calls[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/images/generations")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret-key")
        body = json.loads(request.data)
        self.assertEqual(set(body), {"model", "prompt", "n", "output_format"})
        self.assertEqual(body["model"], "explicit-image-model")
        self.assertEqual(body["n"], 1)
        self.assertEqual(body["output_format"], "png")
        self.assertIn(PROFILE.facts[0].text, body["prompt"])
        self.assertNotIn("backend-post-id", body["prompt"])
        self.assertEqual(timeout, 1)
        self.assertEqual(limit, MediaLimits().max_response_bytes)
        self.assertNotEqual(thread_id, threading.get_ident())

    async def test_feedback_changes_prompt_without_requesting_pixel_editing(self):
        transport = FakeTransport()
        adapter = provider(transport)
        await adapter.generate(PROFILE, TEXT)
        await adapter.generate(PROFILE, TEXT, "Добавьте больше свободного места")
        first = json.loads(transport.calls[0][0].data)["prompt"]
        second = json.loads(transport.calls[1][0].data)["prompt"]
        self.assertNotEqual(first, second)
        self.assertIn("Добавьте больше свободного места", second)
        self.assertEqual(len(transport.calls), 2)

    async def test_shared_maximum_fact_brief_and_feedback_lengths_are_supported(self):
        profile = PROFILE.model_copy(update={"facts": [Fact(text="x" * 2000)]})
        text = TEXT.model_copy(update={"image_brief": "x" * 4000})
        transport = FakeTransport()
        image = await provider(transport).generate(profile, text, "x" * 4000)
        self.assertEqual(image.content, PNG)
        self.assertEqual(len(transport.calls), 1)

    async def test_malformed_json_base64_and_url_only_responses(self):
        responses = [
            b"not JSON",
            b"\xff",
            b"[]",
            b"{}",
            b'{"data": []}',
            b'{"data": [{"b64_json": "!!!"}]}',
            b'{"data": [{"b64_json": "\\u00e9"}]}',
            b'{"data": [{"url": "https://untrusted.invalid/image.png"}]}',
            b'{"data": [{"b64_json": "YQ=="}]}',
            b'{"data": [], "data": []}',
            b'{"data": [{"b64_json": ""}]}',
            b'{"data": [{}, {}]}',
            encoded_response(PNG + b"trailing data"),
        ]
        for response in responses:
            with self.subTest(response=response):
                transport = FakeTransport(response=response)
                with self.assertRaises(InvalidImageResponseError):
                    await provider(transport).generate(PROFILE, TEXT)
                self.assertEqual(len(transport.calls), 1)

    async def test_response_and_decoded_image_limits(self):
        cases = [
            (MediaLimits(max_response_bytes=10), encoded_response()),
            (MediaLimits(max_image_bytes=10), encoded_response()),
        ]
        for limits, response in cases:
            with self.subTest(limits=limits):
                with self.assertRaises(InvalidImageResponseError):
                    await provider(FakeTransport(response=response), limits=limits).generate(PROFILE, TEXT)

    async def test_http_errors_are_typed_and_do_not_leak_body_or_secret(self):
        cases = [
            (401, MediaAuthenticationError),
            (403, MediaAuthenticationError),
            (429, MediaRateLimitError),
            (408, MediaTimeoutError),
            (504, MediaTimeoutError),
            (503, MediaUnavailableError),
            (400, InvalidImageResponseError),
            (302, InvalidImageResponseError),
        ]
        for status, expected in cases:
            with self.subTest(status=status):
                body = io.BytesIO(b"test-secret-key provider diagnostic")
                error = urllib.error.HTTPError("https://example.invalid", status, "test-secret-key", {}, body)
                transport = FakeTransport(error=error)
                with self.assertRaises(expected) as raised:
                    await provider(transport).generate(PROFILE, TEXT)
                self.assertNotIn("test-secret-key", str(raised.exception))
                self.assertTrue(body.closed)
                self.assertEqual(len(transport.calls), 1)

    async def test_network_timeout_and_unexpected_errors_are_safe(self):
        cases = [
            (socket.timeout("test-secret-key"), MediaTimeoutError),
            (urllib.error.URLError(socket.timeout("secret")), MediaTimeoutError),
            (urllib.error.URLError("test-secret-key"), MediaUnavailableError),
            (RuntimeError("test-secret-key"), MediaUnavailableError),
            (InvalidImageResponseError("test-secret-key"), InvalidImageResponseError),
        ]
        for error, expected in cases:
            with self.subTest(error_type=type(error)):
                with self.assertRaises(expected) as raised:
                    await provider(FakeTransport(error=error)).generate(PROFILE, TEXT)
                self.assertNotIn("test-secret-key", str(raised.exception))

    async def test_transport_does_not_block_other_coroutines(self):
        transport = BlockingTransport()
        task = asyncio.create_task(provider(transport).generate(PROFILE, TEXT))
        try:
            for _ in range(100):
                if transport.entered.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(transport.entered.is_set())
            self.assertFalse(task.done())
            second = await provider(FakeTransport()).generate(PROFILE, TEXT)
            self.assertEqual(second.content, PNG)
        finally:
            transport.release.set()
            await task

    async def test_cancellation_propagates_without_retry(self):
        transport = BlockingTransport()
        task = asyncio.create_task(provider(transport).generate(PROFILE, TEXT))
        try:
            for _ in range(100):
                if transport.entered.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(transport.entered.is_set())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            transport.release.set()
        # The worker may finish its original call; cancellation never starts another.
        self.assertEqual(transport.starts, 1)

    async def test_overall_timeout_returns_without_waiting_for_worker(self):
        transport = BlockingTransport()
        adapter = OpenAIImageProvider(
            api_key="test-secret-key",
            model="explicit-image-model",
            timeout_seconds=0.02,
            transport=transport,
        )
        try:
            with self.assertRaises(MediaTimeoutError):
                await asyncio.wait_for(adapter.generate(PROFILE, TEXT), timeout=0.5)
        finally:
            transport.release.set()


class ConfigurationAndValidationTests(unittest.TestCase):
    def test_runtime_rejects_explicit_demo_image_provider_before_generation_in_live(self):
        settings = Settings(
            mode="live",
            telegram_bot_token="test-bot-token",
            ai_factory="test:ai",
            image_factory="test:image",
        )
        with patch("app.core.runtime.load_factory", return_value=lambda settings: DemoImageProvider()):
            with self.assertRaises(InvalidInput):
                runtime_provider(settings, "test:image", ("generate",), [])

    def test_key_model_timeout_and_https_are_required(self):
        invalid = [
            {"api_key": ""},
            {"api_key": "\nsecret"},
            {"model": ""},
            {"timeout_seconds": 0},
            {"timeout_seconds": True},
            {"timeout_seconds": float("nan")},
            {"timeout_seconds": float("inf")},
            {"endpoint": "http://example.invalid/generate"},
            {"endpoint": "https://user:secret@example.invalid/generate"},
            {"endpoint": "https://example.invalid/generate?key=secret"},
            {"endpoint": "https://example.invalid/generate#fragment"},
            {"endpoint": "https://example.invalid:invalid/generate"},
            {"endpoint": "https://example.invalid/\npath"},
            {"endpoint": None},
            {"limits": MediaLimits(max_image_bytes=0)},
            {"limits": MediaLimits(max_image_bytes=10_000_001)},
            {"transport": object()},
        ]
        for overrides in invalid:
            config = {"api_key": "test-secret-key", "model": "explicit-image-model", "timeout_seconds": 1}
            config.update(overrides)
            with self.subTest(overrides=overrides):
                with self.assertRaises(MediaConfigurationError) as raised:
                    OpenAIImageProvider(**config)
                self.assertNotIn("test-secret-key", str(raised.exception))
        with self.assertRaises(TypeError):
            OpenAIImageProvider(api_key="secret", timeout_seconds=1)
        self.assertNotIn("test-secret-key", repr(provider(FakeTransport())))
        self.assertIsInstance(MediaUnavailableError("Unavailable"), ProviderUnavailable)

    def test_prompt_contains_only_supplied_facts_and_data_boundaries(self):
        prompt = build_image_prompt(PROFILE, TEXT, None, MediaLimits())
        source = json.loads(prompt.split("Source data:\n", 1)[1])
        self.assertEqual(source["facts"], [fact.text for fact in PROFILE.facts])
        self.assertEqual(source["visual_idea"], TEXT.image_brief)
        self.assertEqual(source["post_text"], TEXT.text)
        self.assertNotIn(PROFILE.assumptions[0], prompt)
        self.assertIn("Do not invent prices", prompt)

    def test_png_header_chunk_checksums_and_dimensions(self):
        invalid = [PNG[:20], PNG[:-12], PNG + b"extra", b"JPEG", PNG.replace(b"IHDR", b"JHDR", 1)]
        for data in invalid:
            with self.subTest(data=data):
                with self.assertRaises(InvalidImageResponseError):
                    validate_png(data, MediaLimits())
        mutated = bytearray(PNG)
        struct.pack_into(">I", mutated, 16, 5000)
        crc = binascii.crc32(mutated[12:29]) & 0xFFFFFFFF
        struct.pack_into(">I", mutated, 29, crc)
        with self.assertRaises(InvalidImageResponseError):
            validate_png(bytes(mutated), MediaLimits())
        with self.assertRaises(InvalidImageResponseError):
            validate_png(PNG, MediaLimits(max_png_chunks=1))

    def test_redirect_handler_rejects_redirect_instead_of_forwarding_credentials(self):
        request = urllib.request.Request("https://source.invalid", headers={"Authorization": "Bearer secret"})
        self.assertIsNone(
            _NoRedirects().redirect_request(request, None, 302, "Found", {}, "https://untrusted.invalid")
        )

    def test_default_transport_bounds_stream_and_content_length(self):
        class Response(io.BytesIO):
            status = 200

            def __init__(self, body, headers):
                super().__init__(body)
                self.headers = headers

        class Opener:
            def __init__(self, response):
                self.response = response
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                return self.response

        cases = [
            (b"abc", {}, False),
            (b"abcd", {}, True),
            (b"abc", {"Content-Length": "4"}, True),
            (b"abc", {"Content-Length": "invalid"}, True),
            (b"abc", {"Content-Length": "-1"}, True),
        ]
        for body, headers, should_fail in cases:
            opener = Opener(Response(body, headers))
            with (
                self.subTest(headers=headers, body=body),
                patch("app.media.openai.urllib.request.build_opener", return_value=opener),
            ):
                if should_fail:
                    with self.assertRaises(InvalidImageResponseError):
                        UrllibImageTransport().send(urllib.request.Request("https://example.invalid"), 1, 3)
                else:
                    self.assertEqual(
                        UrllibImageTransport().send(urllib.request.Request("https://example.invalid"), 1, 3),
                        b"abc",
                    )
                self.assertEqual(opener.calls, 1)


if __name__ == "__main__":
    unittest.main()
