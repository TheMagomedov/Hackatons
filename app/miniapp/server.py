import asyncio
import logging
from collections import Counter
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web
from pydantic import AwareDatetime, Field, ValidationError

from app.config import Settings
from app.core.service import BackendService
from app.miniapp.auth import validate_init_data
from app.shared.errors import AppError, Conflict, Forbidden, InvalidInput, NotFound, ProviderUnavailable
from app.shared.models import Business, Contract, PostStatus

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
SERVICE = web.AppKey("service", BackendService)
TOKEN = web.AppKey("token", str)
ACTOR = web.RequestKey("actor", int)
PAGE_SIZE = 12


class NewBusiness(Contract):
    name: str = Field(min_length=1, max_length=150)


class SourceInput(Contract):
    kind: str
    value: str = Field(min_length=1, max_length=16_000)


class ChannelInput(Contract):
    value: str = Field(min_length=1, max_length=128)


class RevisionInput(Contract):
    revision: int = Field(strict=True, ge=0)


class PostEdit(RevisionInput):
    scheduled_at: AwareDatetime | None = None
    text: str | None = Field(default=None, min_length=1, max_length=900)


class NewJob(Contract):
    request: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=100)


@web.middleware
async def guard(request, handler):
    try:
        if request.path.startswith("/mini/api/"):
            if SERVICE not in request.app:
                raise NotFound("API недоступен в режиме предпросмотра.")
            actor = validate_init_data(
                request.headers.get("X-Telegram-Init-Data", ""), request.app[TOKEN],
                request.app[SERVICE].now(),
            )
            if actor != request.app[SERVICE].settings.owner_id:
                raise Forbidden("Доступ к этому пространству есть только у владельца бота.")
            request[ACTOR] = actor
        response = await handler(request)
    except (ValueError, ValidationError):
        response = web.json_response({"error": "Проверьте введённые данные."}, status=400)
    except AppError as exc:
        status = {
            Forbidden: 403, NotFound: 404, Conflict: 409, ProviderUnavailable: 502,
        }.get(type(exc), 400)
        response = web.json_response({"error": str(exc)}, status=status)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self' https://telegram.org; style-src 'self'; "
        "img-src 'self' blob: data:; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
    )
    return response


def business_view(business: Business) -> dict:
    return business.model_dump(mode="json", exclude={"owner_id"})


def show(model) -> web.Response:
    return web.json_response(model.model_dump(mode="json"))


def context(request) -> tuple[BackendService, int]:
    return request.app[SERVICE], request[ACTOR]


def create_app(service: BackendService | None = None, *, token: str = "") -> web.Application:
    if service is not None and not token:
        raise ValueError("A bot token is required for the live Mini App")
    app = web.Application(middlewares=[guard], client_max_size=96 * 1024)
    if service is not None:
        app[SERVICE] = service
        app[TOKEN] = token

    async def index(request):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        if SERVICE in request.app:
            html = html.replace('data-mode="preview"', 'data-mode="live"').replace(
                "<!-- TELEGRAM_BRIDGE -->",
                '<script src="https://telegram.org/js/telegram-web-app.js" defer></script>',
            )
        return web.Response(text=html, content_type="text/html")

    async def static_file(request):
        return web.FileResponse(STATIC / request.match_info["name"])

    async def state(request):
        svc, actor = context(request)
        businesses = svc.list_businesses(actor)
        selected_id = request.query.get("business")
        selected = svc.get_business(actor, selected_id) if selected_id else (businesses[-1] if businesses else None)
        if selected is None:
            return web.json_response({"businesses": [], "business": None, "posts": [], "counts": {}})
        posts = svc.list_posts(actor, selected.id)
        counts = Counter(p.status for p in posts)
        sources = svc.list_sources(actor, selected.id)
        scheduled = [p for p in posts if p.status == PostStatus.APPROVED and p.scheduled_at]
        upcoming = min(scheduled, key=lambda post: post.scheduled_at, default=None)
        return web.json_response({
            "businesses": [{"id": b.id, "name": b.name} for b in businesses],
            "business": business_view(selected),
            "sources": [{"id": s.id, "name": s.name, "kind": s.kind} for s in sources],
            "posts": [p.model_dump(mode="json") for p in posts[-6:][::-1]],
            "counts": {**counts, "total": len(posts)},
            "upcoming": upcoming.model_dump(mode="json") if upcoming else None,
        })

    async def posts(request):
        svc, actor = context(request)
        page = int(request.query.get("page", "0"))
        if not 0 <= page <= 100_000:
            raise InvalidInput("Откройте список заново.")
        entries = svc.list_posts(actor, request.match_info["id"])[::-1]
        if status := request.query.get("status"):
            entries = [p for p in entries if p.status == PostStatus(status)]
        start = page * PAGE_SIZE
        return web.json_response({
            "items": [p.model_dump(mode="json") for p in entries[start:start + PAGE_SIZE]],
            "more": len(entries) > start + PAGE_SIZE,
        })

    async def create_business(request):
        svc, actor = context(request)
        body = NewBusiness.model_validate(await request.json())
        return web.json_response(business_view(svc.create_business(actor, body.name)), status=201)

    async def add_source(request):
        svc, actor = context(request)
        business_id = request.match_info["id"]
        body = SourceInput.model_validate(await request.json())
        if body.kind == "text":
            return show(svc.add_text_source(actor, business_id, body.value))
        if body.kind == "url":
            return show(svc.add_url_source(actor, business_id, body.value))
        raise InvalidInput("Выберите описание или ссылку.")

    async def profile(request):
        svc, actor = context(request)
        business_id = request.match_info["id"]
        if request.match_info["action"] == "analyze":
            business = await svc.analyze_business(actor, business_id)
        else:
            body = RevisionInput.model_validate(await request.json())
            business = svc.confirm_profile(actor, business_id, body.revision)
        return web.json_response(business_view(business))

    async def connect(request):
        svc, actor = context(request)
        body = ChannelInput.model_validate(await request.json())
        business = await svc.connect_channel(actor, request.match_info["id"], body.value)
        return web.json_response(business_view(business))

    async def prepare(request):
        svc, actor = context(request)
        body = NewJob.model_validate(await request.json())
        return show(svc.request_preparation(actor, request.match_info["id"], body.request,
                                            f"miniapp:{body.request_id}"))

    async def job(request):
        svc, actor = context(request)
        return show(svc.get_job(actor, request.match_info["id"]))

    async def post(request):
        svc, actor = context(request)
        return show(svc.get_post(actor, request.match_info["id"]))

    async def change_post(request):
        svc, actor = context(request)
        post_id, action = request.match_info["id"], request.match_info["action"]
        payload = await request.json()
        if action == "edit":
            edit = PostEdit.model_validate(payload)
            return show(svc.edit_post(actor, post_id, edit.revision, text=edit.text,
                                      scheduled_at=edit.scheduled_at))
        body = RevisionInput.model_validate(payload)
        change = svc.approve_post if action == "approve" else svc.reject_post
        return show(change(actor, post_id, body.revision))

    async def asset(request):
        svc, actor = context(request)
        item, data = svc.read_asset(actor, request.match_info["id"])
        if item.mime_type not in {"image/png", "image/jpeg"}:
            raise NotFound("Изображение недоступно.")
        return web.Response(body=data, content_type=item.mime_type)

    async def report(request):
        svc, actor = context(request)
        business_id = request.match_info["id"]
        business = svc.get_business(actor, business_id)
        end = svc.now()
        local = end.astimezone(ZoneInfo(business.timezone))
        first = (local - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        start = min(first, end - timedelta(seconds=1))
        data = svc.report_data(actor, business_id, start, end)
        dates = [(first + timedelta(days=index)).date() for index in range(7)]
        created = Counter(p.created_at.astimezone(local.tzinfo).date() for p in data.posts if not p.is_demo)
        sent = Counter(p.published_at.astimezone(local.tzinfo).date() for p in data.posts
                       if p.published_at and not p.is_demo)
        detail = svc.build_report(actor, business_id, start, end) if svc.reporter else ""
        return web.json_response({
            "series": [{"label": d.strftime("%d.%m"), "created": created[d], "sent": sent[d]} for d in dates],
            "detail": detail,
        })

    app.add_routes([
        web.get("/mini/", index),
        web.get(r"/mini/{name:styles\.css|app\.js|forest-hero\.jpg|inter-variable\.woff2|inter-OFL\.txt}", static_file),
        web.get("/mini/api/state", state),
        web.get("/mini/api/business/{id}/posts", posts),
        web.post("/mini/api/business", create_business),
        web.post("/mini/api/business/{id}/source", add_source),
        web.post("/mini/api/business/{id}/profile/{action:analyze|confirm}", profile),
        web.post("/mini/api/business/{id}/channel", connect),
        web.post("/mini/api/business/{id}/prepare", prepare),
        web.get("/mini/api/job/{id}", job),
        web.get("/mini/api/post/{id}", post),
        web.post("/mini/api/post/{id}/{action:edit|approve|reject}", change_post),
        web.get("/mini/api/asset/{id}", asset),
        web.get("/mini/api/business/{id}/report", report),
    ])
    return app


async def serve(app: web.Application, port: int, stop: asyncio.Event) -> None:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        log.info("Mini App: http://127.0.0.1:%s/mini/", port)
        await stop.wait()
    finally:
        await runner.cleanup()


async def run_live(service: BackendService, settings: Settings, stop: asyncio.Event) -> None:
    token = settings.telegram_bot_token.get_secret_value()
    await serve(create_app(service, token=token), settings.mini_app_port, stop)
