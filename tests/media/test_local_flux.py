import io
import json
import urllib.error
import urllib.request
from unittest.mock import Mock

import pytest

from app.config import Settings
from app.media import runtime
from app.media.branding import SalavatBrandingProvider
from app.media.errors import InvalidImageResponseError, MediaConfigurationError, MediaUnavailableError
from app.media.openai import OpenAIImageProvider, UrllibImageTransport, _NoRedirects
from app.safety.wrappers import ModeratedImages
from app.shared.errors import SafetyBlocked, SafetyUnavailable
from tests.media.test_media import PNG, PROFILE, TEXT, FakeTransport, encoded_response

ENDPOINT = "http://127.0.0.1:8194/v1/images/generations"


def local_provider(transport=None, **overrides):
    options = {
        "api_key": "test-local-key", "model": "flux1-dev", "timeout_seconds": 1,
        "endpoint": ENDPOINT, "allow_local_flux": True,
        "transport": transport if transport is not None else FakeTransport(),
        **overrides,
    }
    return OpenAIImageProvider(**options)


def live_settings():
    return Settings(mode="live", owner_id=42, telegram_bot_token="test-token",
                    ai_factory="test:ai", image_factory="app.media.runtime:build_provider")


@pytest.mark.parametrize("endpoint", [
    "http://localhost:8194/v1/images/generations", "http://127.0.0.2:8194/v1/images/generations",
    "http://[::1]:8194/v1/images/generations", "http://192.168.1.2:8194/v1/images/generations",
    "http://example.test:8194/v1/images/generations", "http://127.0.0.1.attacker.test:8194/v1/images/generations",
    "http://2130706433:8194/v1/images/generations", "http://127.000.000.001:8194/v1/images/generations",
    "http://127.0.0.1.:8194/v1/images/generations", "http://127.0.0.1:08194/v1/images/generations",
    "http://127.0.0.1:80/v1/images/generations", "http://127.0.0.1:65536/v1/images/generations",
    "http://user:secret@127.0.0.1:8194/v1/images/generations",
    "http://127.0.0.1:8194/v1/images/generations?secret=x",
    "http://127.0.0.1:8194/v1/images/generations?", "http://127.0.0.1:8194/v1/images/generations#",
    "http://127.0.0.1:8194/v1/images/generations#fragment",
    "http://127.0.0.1:8194/v1/images/generations/", "http://127.0.0.1:8194/prompt",
    "http://127.0.0.1:8194/v1/../v1/images/generations",
    "http://127.0.0.1:8194/v1/%69mages/generations",
    "http://127.0.0.1:8194/v1/images/generations\n",
    "HTTP://127.0.0.1:8194/v1/images/generations", "https://127.0.0.1:8194/v1/images/generations",
    "https://remote.example/v1/images/generations",
])
def test_local_mode_rejects_every_noncanonical_or_nonlocal_endpoint(endpoint):
    with pytest.raises(MediaConfigurationError):
        local_provider(endpoint=endpoint)


@pytest.mark.parametrize("port", [1024, 8194, 65535])
def test_local_mode_accepts_explicit_unprivileged_loopback_port(port):
    provider = local_provider(endpoint=f"http://127.0.0.1:{port}/v1/images/generations")
    assert provider._endpoint.startswith(f"http://127.0.0.1:{port}/")


@pytest.mark.parametrize("overrides", [
    {"model": "gpt-image-1"}, {"model": "flux1-dev "}, {"allow_local_flux": False},
    {"allow_local_flux": 1}, {"allow_local_flux": "true"}, {"allow_local_flux": None},
])
def test_http_needs_exact_flux_model_and_explicit_boolean_opt_in(overrides):
    with pytest.raises(MediaConfigurationError):
        local_provider(**overrides)


async def test_authenticated_local_contract_preserves_one_post_and_png_validation():
    transport = FakeTransport()
    image = await local_provider(transport).generate(PROFILE, TEXT)
    assert image.content == PNG and image.provider == "flux1-dev" and image.is_demo is False
    assert len(transport.calls) == 1
    request, _, _, _ = transport.calls[0]
    assert request.full_url == ENDPOINT and request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer test-local-key"
    body = json.loads(request.data)
    assert body["model"] == "flux1-dev" and body["n"] == 1 and body["output_format"] == "png"
    assert "test-local-key" not in request.data.decode()
    with pytest.raises(InvalidImageResponseError):
        await local_provider(FakeTransport(response=encoded_response(b"not a PNG"))).generate(PROFILE, TEXT)


@pytest.mark.parametrize("status,exception", [(302, InvalidImageResponseError), (307, InvalidImageResponseError),
                                             (422, InvalidImageResponseError), (503, MediaUnavailableError)])
async def test_local_gateway_denial_outage_or_redirect_never_retries_or_falls_back(status, exception):
    body = io.BytesIO(b"private upstream body")
    error = urllib.error.HTTPError(ENDPOINT, status, "private error", {"Location": "https://cloud.invalid"}, body)
    transport = FakeTransport(error=error)
    with pytest.raises(exception) as raised:
        await local_provider(transport).generate(PROFILE, TEXT)
    assert len(transport.calls) == 1 and body.closed
    assert "private" not in str(raised.value)


def test_local_http_transport_bypasses_proxy_and_never_follows_redirect(monkeypatch):
    response = io.BytesIO(b"ok")
    response.status = 200
    response.headers = {}
    opener = Mock()
    opener.open.return_value = response
    build = Mock(return_value=opener)
    monkeypatch.setattr("app.media.openai.urllib.request.build_opener", build)
    request = urllib.request.Request(ENDPOINT, headers={"Authorization": "Bearer test-local-key"})
    assert UrllibImageTransport().send(request, 3, 10) == b"ok"
    handlers = build.call_args.args
    proxies = [handler for handler in handlers if isinstance(handler, urllib.request.ProxyHandler)]
    assert len(proxies) == 1 and proxies[0].proxies == {}
    redirect = next(handler for handler in handlers if isinstance(handler, _NoRedirects))
    assert redirect.redirect_request(request, None, 302, "Found", {}, "https://cloud.invalid") is None
    opener.open.assert_called_once_with(request, timeout=3)


@pytest.fixture
def local_environment(monkeypatch):
    monkeypatch.setenv("IMAGE_LOCAL_FLUX", "1")
    monkeypatch.setenv("IMAGE_API_BASE", "http://127.0.0.1:8194/v1")
    monkeypatch.setenv("IMAGE_MODEL", "flux1-dev")
    monkeypatch.setenv("IMAGE_API_KEY", "test-local-key")


def test_runtime_flag_wires_real_provider_and_preserves_branding(local_environment):
    wrapped = runtime.build_provider(live_settings())
    assert isinstance(wrapped, SalavatBrandingProvider)
    assert isinstance(wrapped._provider, OpenAIImageProvider)
    assert wrapped._provider._endpoint == ENDPOINT and wrapped._provider._local_flux is True


@pytest.mark.parametrize("flag", ["", "true", "yes", "01", " 1", "False"])
def test_runtime_local_flag_has_no_truthy_or_typo_fallback(local_environment, monkeypatch, flag):
    monkeypatch.setenv("IMAGE_LOCAL_FLUX", flag)
    with pytest.raises(MediaConfigurationError, match="exactly 0 or 1"):
        runtime.build_provider(live_settings())


def test_runtime_default_stays_https_only(local_environment, monkeypatch):
    monkeypatch.delenv("IMAGE_LOCAL_FLUX")
    with pytest.raises(MediaConfigurationError, match="HTTPS"):
        runtime.build_provider(live_settings())
    monkeypatch.setenv("IMAGE_API_BASE", "https://api.example/v1")
    assert runtime.build_provider(live_settings())._provider._local_flux is False


async def test_safety_still_fails_closed_before_generation_and_before_returning_local_image():
    transport = FakeTransport()

    class Policy:
        ready = False

        def require_configured(self):
            if not self.ready:
                raise SafetyUnavailable()

        def check_image(self, content, mime_type):
            assert content == PNG and mime_type == "image/png"
            raise SafetyBlocked()

    policy = Policy()
    moderated = ModeratedImages(SalavatBrandingProvider(local_provider(transport)), policy)
    with pytest.raises(SafetyUnavailable):
        await moderated.generate(PROFILE, TEXT)
    assert not transport.calls
    policy.ready = True
    with pytest.raises(SafetyBlocked):
        await moderated.generate(PROFILE, TEXT)
    assert len(transport.calls) == 1
