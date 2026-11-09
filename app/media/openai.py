"""Optional OpenAI Images adapter, with no environment/configuration lookup."""

import asyncio
import base64
import binascii
import json
import math
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from app.shared.models import BusinessProfile, GeneratedImage, TextDraft

from .errors import (
    InvalidImageResponseError,
    MediaAuthenticationError,
    MediaConfigurationError,
    MediaRateLimitError,
    MediaTimeoutError,
    MediaUnavailableError,
)
from .limits import MediaLimits
from .validation import DEFAULT_LIMITS, build_image_prompt, validate_limits, validate_png


class BlockingImageTransport(Protocol):
    def send(self, request: urllib.request.Request, timeout_seconds: float, max_response_bytes: int) -> bytes:
        """Perform exactly one bounded request, without retrying or redirecting."""
        ...


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Authorization header to a redirect destination.
        return None


class UrllibImageTransport:
    """Default blocking transport. The provider always calls it in a worker thread."""

    def send(self, request: urllib.request.Request, timeout_seconds: float, max_response_bytes: int) -> bytes:
        opener = urllib.request.build_opener(_NoRedirects())
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise InvalidImageResponseError("The image service returned an unexpected status.")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    content_length = int(length)
                except (ValueError, TypeError):
                    raise InvalidImageResponseError("The response length header is invalid.") from None
                if not 0 <= content_length <= max_response_bytes:
                    raise InvalidImageResponseError("The image response exceeds its size limit.")
            body = response.read(max_response_bytes + 1)
            if len(body) > max_response_bytes:
                raise InvalidImageResponseError("The image response exceeds its size limit.")
            return body


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class OpenAIImageProvider:
    """Explicitly configured example adapter; one paid API call per invocation.

    Cancellation or timeout stops awaiting the result. Python cannot terminate an
    already running urllib worker, so the remote generation may still complete.
    The adapter never automatically repeats that request.
    """

    is_demo = False

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        endpoint: str = "https://api.openai.com/v1/images/generations",
        limits: MediaLimits = DEFAULT_LIMITS,
        transport: BlockingImageTransport | None = None,
    ):
        validate_limits(limits)
        if (
            not isinstance(api_key, str)
            or not api_key.strip()
            or any(not 33 <= ord(char) <= 126 for char in api_key)
        ):
            raise MediaConfigurationError("An image provider API key is required.")
        if (
            not isinstance(model, str)
            or not model.strip()
            or len(model) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in model)
        ):
            raise MediaConfigurationError("An explicit image model is required.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise MediaConfigurationError("timeout_seconds must be a positive finite number.")
        try:
            parsed = urllib.parse.urlsplit(endpoint)
            valid_endpoint = (
                isinstance(endpoint, str)
                and len(endpoint) <= 2_048
                and parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and not any(ord(char) <= 32 or ord(char) == 127 for char in endpoint)
            )
            parsed.port  # Reject malformed/non-numeric ports before network I/O.
        except (ValueError, TypeError, AttributeError):
            valid_endpoint = False
        if not valid_endpoint:
            raise MediaConfigurationError("The image endpoint must be an HTTPS URL without credentials.")
        if transport is not None and not callable(getattr(transport, "send", None)):
            raise MediaConfigurationError("The image transport must implement send().")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = float(timeout_seconds)
        self._endpoint = endpoint
        self._limits = limits
        self._transport = transport if transport is not None else UrllibImageTransport()

    def __repr__(self) -> str:
        return "OpenAIImageProvider(api_key=<redacted>)"

    async def generate(
        self, profile: BusinessProfile, text: TextDraft, feedback: str | None = None
    ) -> GeneratedImage:
        prompt = build_image_prompt(profile, text, feedback, self._limits)
        body = json.dumps(
            {"model": self._model, "prompt": prompt, "n": 1, "output_format": "png"},
            ensure_ascii=False,
        ).encode("utf-8")
        http_request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={
                "Authorization": "Bearer " + self._api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self._transport.send,
                    http_request,
                    self._timeout_seconds,
                    self._limits.max_response_bytes,
                ),
                timeout=self._timeout_seconds,
            )
        except (asyncio.TimeoutError, TimeoutError, socket.timeout):
            raise MediaTimeoutError("The image service request timed out.") from None
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status in (401, 403):
                raise MediaAuthenticationError("The image service rejected its credentials.") from None
            if status == 429:
                raise MediaRateLimitError("The image service usage limit was reached.") from None
            if status in (408, 504):
                raise MediaTimeoutError("The image service request timed out.") from None
            if status >= 500:
                raise MediaUnavailableError("The image service is unavailable.") from None
            raise InvalidImageResponseError("The image service rejected the request or redirect.") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise MediaTimeoutError("The image service request timed out.") from None
            raise MediaUnavailableError("The image service could not be reached.") from None
        except InvalidImageResponseError:
            raise InvalidImageResponseError("The image response failed validation.") from None
        except Exception:
            # Transport messages may contain request headers or provider bodies.
            raise MediaUnavailableError("The image transport failed.") from None
        image_bytes = self._decode_response(response)
        return GeneratedImage(
            content=image_bytes,
            mime_type="image/png",
            provider="openai",
            is_demo=False,
        )

    def _decode_response(self, response: bytes) -> bytes:
        if not isinstance(response, bytes) or len(response) > self._limits.max_response_bytes:
            raise InvalidImageResponseError("The image response exceeds its size limit.")
        try:
            payload = json.loads(response, object_pairs_hook=_unique_json_object)
            if not isinstance(payload, dict):
                raise ValueError("Expected object")
            data = payload["data"]
            if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                raise ValueError("Expected exactly one image")
            encoded = data[0]["b64_json"]
            max_encoded = 4 * ((self._limits.max_image_bytes + 2) // 3)
            if not isinstance(encoded, str) or not encoded or len(encoded) > max_encoded:
                raise ValueError("Invalid base64 size")
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, KeyError, TypeError, UnicodeError, binascii.Error, RecursionError):
            raise InvalidImageResponseError("Expected one inline base64 PNG image.") from None
        validate_png(image_bytes, self._limits)
        return image_bytes
