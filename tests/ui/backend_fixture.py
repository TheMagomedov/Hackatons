"""Isolated browser-test backend: fake providers, temporary DB, no publisher loop.

Started/stopped by backend.cjs. Never reads .env or connects to Telegram/AI.
Optional --role reviewer provides a seeded, loopback-only CUA inspection page.
"""
import argparse
import asyncio
import json
import signal
import socket
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from aiohttp import web

from app.config import Settings
from app.core.runtime import build_runtime
from app.miniapp.server import STATIC, create_app
from tests.test_miniapp import OWNER, TOKEN, signed_data


async def seed_role_preview(service, role):
    service.create_business(OWNER, "Второе демо-пространство")
    business = service.create_business(OWNER, "САЛАВАТСТЕКЛО")
    business = service.save_manual_profile(
        OWNER, business.id, 0,
        description="Демонстрационное пространство для проверки интерфейса жюри.",
        tone="Профессионально и понятно", constraints=["Демо-материалы не публикуются."],
    )
    service.confirm_profile(OWNER, business.id, business.profile_version)
    await service.connect_channel(OWNER, business.id, "@salavatsteklo_qa_demo")
    image = service.upload_post_image(
        OWNER, business.id, "demo-pattern.png", (STATIC / "brand-pattern-1.png").read_bytes(),
    )
    service.create_manual_draft(
        OWNER, business.id, "Стекло. Свет. Пространство.\nДемонстрационный материал для просмотра жюри.",
        image.id, "qa-review",
    )
    planned = service.create_manual_draft(
        OWNER, business.id, "В деталях — характер бренда.\nДемонстрационная публикация в календаре.",
        image.id, "qa-calendar", scheduled_at=service.now() + timedelta(days=1),
    )
    service.approve_post(OWNER, planned.id, planned.revision)
    job = service.request_preparation(OWNER, business.id, "Демонстрационный пост о стекле", "qa-job")
    await service.prepare_next_job()
    assert service.get_job(OWNER, job.id).status == "done"
    actor = OWNER if role == "owner" else 3003
    if role != "owner":
        invitation = service.repo.create_access_invite(
            OWNER, role, 1, service.now() + timedelta(hours=1), service.now(),
        )
        service.claim_invite(actor, invitation["token"])
    return actor, business.id


def add_local_bridge(app, service, actor):
    """Fake Telegram is confined to this test process; production routes are unchanged."""
    @web.middleware
    async def local_bridge(request, handler):
        response = await handler(request)
        if request.path == "/mini/":
            response.text = response.text.replace(
                "https://telegram.org/js/telegram-web-app.js", "/mini/test-telegram.js",
            )
        return response

    async def telegram_script(_request):
        session = json.dumps(signed_data(service.now(), actor=actor))
        return web.Response(
            text=f"window.Telegram={{WebApp:{{initData:{session},colorScheme:'light',"
                 "isVersionAtLeast:()=>false,ready:()=>{},expand:()=>{},onEvent:()=>{}}};",
            content_type="application/javascript",
        )

    app.middlewares.append(local_bridge)
    app.router.add_get("/mini/test-telegram.js", telegram_script)


async def main(*, role=None, port=0, vk=False) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
    with TemporaryDirectory(prefix="tirazh-ui-") as destination:
        # Explicit default demo mode and demo factories, no env-file loading.
        runtime = await build_runtime(Settings(mode="demo", owner_id=OWNER, data_dir=Path(destination).resolve()))
        app = create_app(runtime.service, token=TOKEN)

        @web.middleware
        async def connected_contract(request, handler):
            # This isolated fixture exercises the live UI contract with fake
            # providers. Never add this override to the application server.
            response = await handler(request)
            if request.path == "/mini/api/state" and response.status == 200:
                payload = json.loads(response.text)
                payload["runtime_mode"] = "live"
                response.text = json.dumps(payload, ensure_ascii=False)
            return response

        app.middlewares.append(connected_contract)
        selected = ""
        if role:
            actor, selected = await seed_role_preview(runtime.service, role)
            if not vk:
                add_local_bridge(app, runtime.service, actor)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", port))
        port = sock.getsockname()[1]
        await web.SockSite(runner, sock).start()
        if vk:
            from app.vk.adapter import VKBotAdapter
            from app.vk.store import VKStore
            service = runtime.service
            service.settings = service.settings.model_copy(update={
                "mini_app_url": f"http://127.0.0.1:{port}/mini/", "vk_group_id": 123,
            })
            adapter = VKBotAdapter(service, client=AsyncMock(), store=VKStore(Path(destination) / "vk.sqlite3", 123))
            service.vk = adapter
            runtime.resources.append(adapter)
            actor = actor if role else OWNER
            adapter.store.claim_link(9001, adapter.issue_link(actor), service.require_access)
            print("UI_VK_READY " + adapter.launch(actor, 9001), flush=True)
        print(f"UI_BACKEND_READY http://127.0.0.1:{port}/mini/", flush=True)
        if selected:
            print(f"UI_ROLE_PREVIEW {role}; selected workspace: {selected}", flush=True)
        try:
            while not stop.is_set():
                # Generation only. Publishing/scheduler/notification loops are absent.
                await runtime.service.prepare_next_job()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.1)
                except TimeoutError:
                    pass
        finally:
            await runner.cleanup()
            await runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=["owner", "operator", "reviewer"])
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--vk", action="store_true", help="Use an isolated, synthetic VK login ticket")
    args = parser.parse_args()
    asyncio.run(main(role=args.role, port=args.port, vk=args.vk))
