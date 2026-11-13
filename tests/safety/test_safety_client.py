"""Moderation contract tests: deterministic transport, no network or real credentials."""

import json
from unittest.mock import Mock

import pytest

from app.safety import HTTPModeration, NoopSafety, UnavailableSafety, from_environment
from app.safety.client import MAX_IMAGE_BYTES, MAX_RESPONSE_BYTES
from app.shared.errors import InvalidInput, SafetyBlocked, SafetyUnavailable

BASE = "https://machine.example.ts.net/flux-api/v1"
TEST_KEY = "test-only-not-a-real-key"


def policy_with(verdict):
    transport = Mock()
    transport.send.return_value = json.dumps(verdict).encode()
    return HTTPModeration(BASE, TEST_KEY, transport=transport), transport


def test_exact_text_and_image_contract():
    policy, transport = policy_with({"allowed": True, "reason": "", "category": "safe"})
    policy.check_text(["Р СѓСЃСЃРєРёР№ С‚РµРєСЃС‚", "English"])
    request, timeout = transport.send.call_args.args
    assert request.full_url == BASE + "/safety/check-text"
    assert request.method == "POST" and timeout == 90
    assert request.get_header("Authorization") == "Bearer " + TEST_KEY
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"texts": ["Р СѓСЃСЃРєРёР№ С‚РµРєСЃС‚", "English"]}
    policy.check_image(b"image-data", "image/jpeg")
    request, timeout = transport.send.call_args.args
    assert request.full_url == BASE + "/safety/check-image" and timeout == 30
    assert request.data == b"image-data" and request.get_header("Content-type") == "image/jpeg"
    assert TEST_KEY not in request.full_url and TEST_KEY not in repr(policy)
    assert transport.send.call_count == 2


def test_denial_is_fixed_safe_error_and_is_not_retried():
    policy, transport = policy_with({"allowed": False, "reason": "raw-sensitive-reason", "category": "blocked"})
    with pytest.raises(SafetyBlocked) as error:
        policy.check_text(["text"])
    assert error.value.code == "content_blocked"
    assert "raw-sensitive" not in str(error.value)
    assert transport.send.call_count == 1


@pytest.mark.parametrize("reply", [
    b"broken", b"null", b"[]", b'{"allowed":true}',
    b'{"allowed":1,"reason":"","category":"safe"}',
    b'{"allowed":true,"reason":null,"category":"safe"}', b"x" * (MAX_RESPONSE_BYTES + 1),
])
def test_unusable_verdict_fails_closed_without_retry(reply):
    policy, transport = policy_with({})
    transport.send.return_value = reply
    with pytest.raises(SafetyUnavailable):
        policy.check_text(["text"])
    assert transport.send.call_count == 1


def test_transport_failure_does_not_expose_body_or_key():
    policy, transport = policy_with({})
    transport.send.side_effect = OSError("secret-body " + TEST_KEY)
    with pytest.raises(SafetyUnavailable) as error:
        policy.check_text(["text"])
    assert "secret-body" not in str(error.value) and TEST_KEY not in str(error.value)
    assert transport.send.call_count == 1


@pytest.mark.parametrize("texts", [[], [123], ["a" * 12001], ["x"] * 17])
def test_text_limits_before_request(texts):
    policy, transport = policy_with({})
    with pytest.raises(InvalidInput):
        policy.check_text(texts)
    transport.send.assert_not_called()


@pytest.mark.parametrize("content,mime", [(b"", "image/png"), (b"x", "text/html"),
                                          (b"x" * (MAX_IMAGE_BYTES + 1), "image/png")])
def test_image_limits_before_request(content, mime):
    policy, transport = policy_with({})
    with pytest.raises(InvalidInput):
        policy.check_image(content, mime)
    transport.send.assert_not_called()


@pytest.mark.parametrize("url", ["http://remote.example/v1", "https://user:pass@example.com",
                                  "https://example.com/?key=secret", "https://example.com/#fragment", None])
def test_unsafe_configuration_fails_closed(url):
    with pytest.raises(SafetyUnavailable):
        HTTPModeration(url, TEST_KEY)


def test_explicit_offline_policy_and_missing_live_configuration():
    assert isinstance(from_environment({}), UnavailableSafety)
    assert isinstance(from_environment({"MODERATION_API_BASE": BASE}), UnavailableSafety)
    assert isinstance(from_environment({"MODERATION_API_BASE": BASE, "MODERATION_API_KEY": TEST_KEY}),
                      HTTPModeration)
    for operation in (lambda p: p.require_configured(), lambda p: p.check_text(["a"]),
                      lambda p: p.check_image(b"x", "image/png")):
        operation(NoopSafety())
        with pytest.raises(SafetyUnavailable):
            operation(UnavailableSafety())
