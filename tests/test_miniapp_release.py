from aiohttp.test_utils import TestClient, TestServer

from app.miniapp.server import create_app
from tests.test_miniapp_controls import mini

__all__ = ["mini"]


async def test_connected_page_never_serves_preview_data_or_provider(mini):
    html = await (await mini.client.get('/mini/')).text()
    assert 'data-mode="live"' in html
    assert 'src="./demo-store.js"' not in html
    assert 'src="./preview-runtime.js"' not in html
    for name in ('demo-store.js', 'preview-runtime.js'):
        assert (await mini.client.get('/mini/' + name)).status == 404
    source = await (await mini.client.get('/mini/app.js')).text()
    for marker in ('businessSeed', 'demoWorkspaces', 'function demoApi(', '@salavatsteklo_demo',
                   'Это демонстрационный ответ', 'Демонстрационные показатели'):
        assert marker not in source


async def test_explicit_development_preview_loads_its_separate_provider():
    async with TestClient(TestServer(create_app())) as browser:
        html = await (await browser.get('/mini/')).text()
        assert 'data-mode="preview"' in html
        assert 'src="./preview-runtime.js"' in html
        assert 'src="./demo-store.js"' in html
        assert (await browser.get('/mini/preview-runtime.js')).status == 200
        assert (await browser.get('/mini/api/state')).status == 404
