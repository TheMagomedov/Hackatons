"""Loading assets stay local; authentication remains at the signed API boundary."""

from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.core.runtime import build_runtime
from app.miniapp.server import create_app
from tests.test_miniapp import OWNER, TOKEN, signed_data


async def test_boot_assets_and_icons_are_local_and_external_sdk_is_not_blocking(tmp_path):
    runtime = await build_runtime(Settings(owner_id=OWNER, data_dir=tmp_path))
    try:
        async with TestClient(TestServer(create_app(runtime.service, token=TOKEN))) as client:
            response = await client.get("/mini/")
            html = await response.text()
            assert response.headers["Cache-Control"] == "no-store"
            assert '<script id="telegram-sdk" src="https://telegram.org/js/telegram-web-app.js" async></script>' in html
            assert html.index('src="./session.js"') < html.index('src="./app.js"')
            assert 'rel="icon" type="image/png" href="./brand-logo.png"' in html
            assert 'rel="apple-touch-icon" href="./brand-logo.png"' in html
            assert "Обновить загрузку" in html and "/menu" in html
            for name in ("session.js", "app.js", "brand-logo.png"):
                asset = await client.get("/mini/" + name)
                assert asset.status == 200
                assert asset.headers["Cache-Control"] == "no-cache"
                if name.endswith(".png"):
                    assert asset.content_type == "image/png"
                    assert (await asset.read()).startswith(b"\x89PNG\r\n\x1a\n")
            # URL data is never accepted by the server as an alternative auth path.
            raw = signed_data(runtime.service.now())
            assert (await client.get("/mini/api/state?" + urlencode({"tgWebAppData": raw}))).status == 403
            header = {"X-Telegram-Init-Data": raw}
            assert (await client.get("/mini/api/state", headers=header)).status == 200
            header["X-Telegram-Init-Data"] = raw.replace("test-only", "tampered")
            assert (await client.get("/mini/api/state", headers=header)).status == 403
    finally:
        await runtime.close()
