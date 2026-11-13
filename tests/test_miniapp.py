import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.core.runtime import build_runtime
from app.miniapp.auth import validate_init_data
from app.miniapp.server import create_app
from app.shared.errors import Forbidden, InvalidInput

TOKEN = "12345:test-only-key"
OWNER = 42


def signed_data(now, *, actor=OWNER, fields=None):
    data = {
        "auth_date": str(int(now.timestamp())),
        "user": json.dumps({"id": actor, "first_name": "Владелец"}, ensure_ascii=False),
        "query_id": "test-only",
        **(fields or {}),
    }
    check = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    key = hmac.digest(b"WebAppData", TOKEN.encode(), hashlib.sha256)
    return urlencode({**data, "hash": hmac.new(key, check.encode(), hashlib.sha256).hexdigest()})


def test_init_data_must_be_recent_and_untampered():
    now = datetime.now(UTC)
    valid = signed_data(now)
    assert validate_init_data(valid, TOKEN, now) == OWNER
    for raw in (
        valid.replace("test-only", "modified"),
        valid + "&auth_date=42",
        signed_data(now - timedelta(hours=2)),
        signed_data(now + timedelta(minutes=1)),
    ):
        with pytest.raises(Forbidden):
            validate_init_data(raw, TOKEN, now)


@pytest.mark.parametrize("url", [
    "http://pulse.example/mini/", "https://user@pulse.example/mini/",
    "https://pulse.example/mini/?foo=bar", "https://pulse.example/other/",
    "https://pulse.example:8443/mini/", "https://127.0.0.1:8443/mini/",
    "https://ts.net:8443/mini/", "https://.ts.net:8443/mini/",
    "https://demo.ts.net.attacker.example:8443/mini/", "https://notts.net:8443/mini/",
    "http://demo.tailtest.ts.net:8443/mini/", "https://demo.tailtest.ts.net:10000/mini/",
    "https://user:password@demo.tailtest.ts.net:8443/mini/",
    "https://demo.tailtest.ts.net:8443/other/", "https://demo.tailtest.ts.net:8443/mini",
    "https://demo.tailtest.ts.net:8443/mini/?foo=bar",
    "https://demo.tailtest.ts.net:8443/mini/#content", "https://demo.tailtest.ts.net:8443/mini/\n",
])
def test_live_url_requires_https_and_exact_path(url):
    with pytest.raises(InvalidInput, match="MINI_APP_URL"):
        Settings(
            mode="live", owner_id=OWNER, telegram_bot_token=TOKEN, ai_factory="app.test:ai",
            image_factory="app.test:image", bot_factory="app.bot.adapter:build_bot", mini_app_url=url,
        )


@pytest.mark.parametrize("url", [
    "https://pulse.example/mini/", "https://pulse.example:443/mini/",
    "https://demo.tailtest.ts.net/mini/", "https://demo.tailtest.ts.net:443/mini/",
    "https://demo.tailtest.ts.net:8443/mini/", "https://DEMO.TAILTEST.TS.NET:8443/mini/",
])
def test_live_url_accepts_standard_https_and_separate_tailscale_funnel_port(url):
    settings = Settings(
        mode="live", owner_id=OWNER, telegram_bot_token=TOKEN, ai_factory="app.test:ai",
        image_factory="app.test:image", bot_factory="app.bot.adapter:build_bot", mini_app_url=url,
    )
    assert settings.mini_app_url == url


async def test_demo_preview_never_exposes_the_live_api():
    async with TestClient(TestServer(create_app())) as browser:
        response = await browser.get("/mini/")
        page = await response.text()
        assert 'data-mode="preview"' in page and "telegram-web-app.js" not in page
        assert (await browser.get("/mini/app.js")).status == 200
        assert (await browser.get("/mini/api/state")).status == 404


async def test_owner_workflow_keeps_approval_bound_to_the_post_revision(tmp_path):
    runtime = await build_runtime(Settings(owner_id=OWNER, data_dir=tmp_path))
    service = runtime.service
    try:
        async with TestClient(TestServer(create_app(service, token=TOKEN))) as browser:
            page = await browser.get("/mini/")
            assert 'data-mode="live"' in await page.text()
            assert (await browser.get("/mini/api/state")).status == 403
            wrong = {"X-Telegram-Init-Data": signed_data(service.now(), actor=OWNER + 1)}
            assert (await browser.get("/mini/api/state", headers=wrong)).status == 403
            browser.session.headers.update({"X-Telegram-Init-Data": signed_data(service.now())})

            response = await browser.post("/mini/api/business", json={"name": "Кофейня"})
            assert response.status == 201
            business = await response.json()
            assert "owner_id" not in business
            prefix = f"/mini/api/business/{business['id']}"
            assert (await browser.post(prefix + "/source", json={"kind": "text", "value": "Кофе и тосты"})).ok
            assert (await browser.post(prefix + "/channel", json={"value": "@sample"})).ok
            profile = await (await browser.post(prefix + "/profile/analyze", json={})).json()
            assert (await browser.post(prefix + "/profile/confirm", json={"revision": profile["profile_version"]})).ok
            job_request = {"request": "Один пост", "request_id": "test-click"}
            job = await (await browser.post(prefix + "/prepare", json=job_request)).json()
            duplicate = await (await browser.post(prefix + "/prepare", json=job_request)).json()
            assert duplicate["id"] == job["id"]
            assert await service.prepare_next_job()

            listing = await (await browser.get(prefix + "/posts?status=review")).json()
            post = listing["items"][0]
            post_url = f"/mini/api/post/{post['id']}"
            asset = await browser.get(f"/mini/api/asset/{post['image_asset_id']}")
            assert asset.content_type == "image/png"
            assert asset.headers["Cache-Control"] == "no-store"
            await asset.read()

            schedule = (service.now() + timedelta(hours=1)).isoformat()
            edit = await browser.post(post_url + "/edit", json={"revision": post["revision"], "scheduled_at": schedule})
            assert edit.ok
            updated = await edit.json()
            assert updated["revision"] > post["revision"]
            assert (await browser.post(post_url + "/approve", json={"revision": post["revision"]})).status == 409
            approval = await browser.post(post_url + "/approve", json={"revision": updated["revision"]})
            assert (await approval.json())["status"] == "approved"
            assert service.publisher.sent == []
            report = await (await browser.get(prefix + "/report")).json()
            assert len(report["series"]) == 7
    finally:
        await runtime.close()
