import asyncio
import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import BodyPartReader, web
from pydantic import AwareDatetime, Field, ValidationError

from app.config import Settings
from app.core.service import BackendService
from app.miniapp.auth import validate_init_data
from app.shared.errors import (
    AppError,
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    ProviderUnavailable,
    SafetyBlocked,
    SafetyUnavailable,
)
from app.shared.models import Business, Contract, PostStatus

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
SERVICE = web.AppKey("service", BackendService)
TOKEN = web.AppKey("token", str)
ACTOR = web.RequestKey("actor", int)
PAGE_SIZE = 12
JSON_LIMIT = 96 * 1024


class NewBusiness(Contract):
    name: str = Field(min_length=1, max_length=150)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)


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
    image_asset_id: str | None = Field(default=None, min_length=1, max_length=100)


class ManualProfileInput(RevisionInput):
    description: str = Field(min_length=1, max_length=2000)
    products: list[str] = Field(default_factory=list, max_length=19)
    audience: str = Field(default="", max_length=1000)
    tone: str = Field(default="Дружелюбный, конкретный", min_length=1, max_length=400)
    constraints: list[str] = Field(default_factory=list, max_length=20)


class ManualDraftInput(Contract):
    text: str = Field(min_length=1, max_length=900)
    image_asset_id: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=100)
    scheduled_at: AwareDatetime | None = None
    source_ids: list[str] = Field(default_factory=list, max_length=100)


class NewJob(Contract):
    request: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=100)


class IdentityInput(Contract):
    request_id: str | None = Field(default=None, min_length=1, max_length=100)


class PauseInput(Contract):
    paused: bool = Field(strict=True)


class AutomationInput(Contract):
    enabled: bool = Field(strict=True)
    interval_hours: int = Field(default=24, strict=True, ge=1, le=720)
    request: str = Field(default="Подготовь один полезный пост о бизнесе", min_length=1, max_length=4000)
    first_run_at: AwareDatetime | None = None


class RevisionFeedback(RevisionInput):
    feedback: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=100)


class ChatInput(Contract):
    text: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=100)
    post_id: str | None = Field(default=None, min_length=1, max_length=100)


async def json_body(request):
    """JSON stays small even though the upload route accepts larger PDF bodies."""
    if request.content_type != "application/json":
        raise InvalidInput("Ожидается JSON-запрос.")
    if request.content_length is not None and request.content_length > JSON_LIMIT:
        raise web.HTTPRequestEntityTooLarge(max_size=JSON_LIMIT, actual_size=request.content_length)
    content = bytearray()
    async for chunk in request.content.iter_chunked(16 * 1024):
        content.extend(chunk)
        if len(content) > JSON_LIMIT:
            raise web.HTTPRequestEntityTooLarge(max_size=JSON_LIMIT, actual_size=len(content))
    return json.loads(content)


async def file_body(request, maximum: int) -> tuple[str, bytes]:
    if request.content_length is not None and request.content_length > maximum + JSON_LIMIT:
        raise web.HTTPRequestEntityTooLarge(max_size=maximum, actual_size=request.content_length)
    if request.content_type != "multipart/form-data":
        raise InvalidInput("Загрузите файл через форму.")
    reader = await request.multipart()
    part = await reader.next()
    if not isinstance(part, BodyPartReader) or part.name != "file" or not part.filename:
        raise InvalidInput("Выберите один файл.")
    if part.headers.get("Content-Encoding") or part.headers.get("Content-Transfer-Encoding"):
        raise InvalidInput("Сжатые или перекодированные загрузки не поддерживаются.")
    filename = part.filename.replace("\\", "/").rsplit("/", 1)[-1]
    content = bytearray()
    while chunk := await part.read_chunk(64 * 1024):
        content.extend(chunk)
        if len(content) > maximum:
            raise web.HTTPRequestEntityTooLarge(max_size=maximum, actual_size=len(content))
    if await reader.next() is not None:
        raise InvalidInput("Загружайте по одному файлу.")
    return filename, bytes(content)


@web.middleware
async def guard(request, handler):
    try:
        if request.path.startswith("/mini/api/"):
            if SERVICE not in request.app:
                raise NotFound("API недоступен в режиме предпросмотра.")
            studio_session = request.headers.get("X-Studio-Session", "")
            if studio_session:
                vk = getattr(request.app[SERVICE], "vk", None)
                if vk is None:
                    raise Forbidden("Вход через ВК не настроен.")
                actor = vk.session_actor(studio_session)
            else:
                actor = validate_init_data(
                    request.headers.get("X-Telegram-Init-Data", ""), request.app[TOKEN],
                    request.app[SERVICE].now(),
                )
            request.app[SERVICE].require_access(actor, write=request.method not in ("GET", "HEAD"))
            request[ACTOR] = actor
        response = await handler(request)
    except (ValueError, ValidationError):
        response = web.json_response({"error": "Проверьте введённые данные."}, status=400)
    except AppError as exc:
        status = {
            Forbidden: 403, NotFound: 404, Conflict: 409, ProviderUnavailable: 502,
            SafetyBlocked: 422, SafetyUnavailable: 503,
        }.get(type(exc), 400)
        response = web.json_response({"error": str(exc)}, status=status)
    except web.HTTPException as exc:
        message = "Файл или запрос превышает допустимый размер." if exc.status == 413 else "Запрос недоступен."
        response = web.json_response({"error": message}, status=exc.status)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers.setdefault("Cache-Control", "no-store")
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
    upload_limit = service.settings.max_upload_bytes if service else JSON_LIMIT
    app = web.Application(middlewares=[guard], client_max_size=upload_limit + JSON_LIMIT)
    if service is not None:
        app[SERVICE] = service
        app[TOKEN] = token

    async def index(request):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        if SERVICE in request.app:
            html = html.replace('data-mode="preview"', 'data-mode="live"').replace(
                "<!-- TELEGRAM_BRIDGE -->",
                '<script id="telegram-sdk" src="https://telegram.org/js/telegram-web-app.js" async></script>',
            )
        else:
            html = html.replace("<!-- PREVIEW_RUNTIME -->",
                                '<script src="./demo-store.js" defer></script>'
                                '<script src="./preview-runtime.js" defer></script>')
        return web.Response(text=html, content_type="text/html")

    async def health(request):
        return web.json_response({
            "status": "ok", "mode": "live" if SERVICE in request.app else "preview",
            "runtime_mode": request.app[SERVICE].settings.mode if SERVICE in request.app else "preview",
        })

    async def vk_session(request):
        vk = getattr(request.app.get(SERVICE), "vk", None)
        if vk is None:
            raise NotFound("Вход через ВК не настроен.")
        body = await json_body(request)
        if not isinstance(body, dict) or set(body) != {"launch"}:
            raise InvalidInput("Нужна одноразовая ссылка входа.")
        return web.json_response({"session": vk.exchange(body["launch"])})

    async def static_file(request):
        name = request.match_info["name"]
        if name in {"demo-store.js", "preview-runtime.js"} and SERVICE in request.app:
            raise web.HTTPNotFound()
        response = web.FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})
        if name.endswith(".woff2"):
            response.content_type = "font/woff2"
        elif name.endswith(".ttf"):
            response.content_type = "font/ttf"
        return response

    async def state(request):
        svc, actor = context(request)
        role = svc.access_role(actor)
        access = {"role": role, "can_write": role in ("owner", "operator")}
        businesses = svc.list_businesses(actor)
        selected_id = request.query.get("business")
        selected = svc.get_business(actor, selected_id) if selected_id else (businesses[-1] if businesses else None)
        if selected is None:
            return web.json_response({"businesses": [], "business": None, "posts": [], "counts": {},
                                      "sources": [], "jobs": [], "usage": svc.ai_usage(actor),
                                      "runtime_mode": svc.settings.mode, "capabilities": svc.capabilities(actor),
                                      "access": access})
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
            "jobs": [j.model_dump(mode="json") for j in svc.list_jobs(actor, selected.id)[-6:][::-1]],
            "usage": svc.ai_usage(actor),
            "runtime_mode": svc.settings.mode,
            "capabilities": svc.capabilities(actor),
            "access": access,
        })

    async def posts(request):
        svc, actor = context(request)
        page = int(request.query.get("page", "0"))
        if not 0 <= page <= 100_000:
            raise InvalidInput("Откройте список заново.")
        entries = svc.list_posts(actor, request.match_info["id"])[::-1]
        if status := request.query.get("status"):
            entries = [p for p in entries if p.status == PostStatus(status)]
        if search := request.query.get("search", "").strip():
            if len(search) > 200:
                raise InvalidInput("Поисковый запрос должен быть до 200 символов.")
            entries = [p for p in entries if search.casefold() in p.text.casefold()]
        start = page * PAGE_SIZE
        return web.json_response({
            "items": [p.model_dump(mode="json") for p in entries[start:start + PAGE_SIZE]],
            "more": len(entries) > start + PAGE_SIZE,
        })

    async def create_business(request):
        svc, actor = context(request)
        body = NewBusiness.model_validate(await json_body(request))
        return web.json_response(business_view(svc.create_business(actor, body.name, body.timezone)), status=201)

    async def add_source(request):
        svc, actor = context(request)
        business_id = request.match_info["id"]
        body = SourceInput.model_validate(await json_body(request))
        if body.kind == "text":
            return show(svc.add_text_source(actor, business_id, body.value))
        if body.kind == "url":
            return show(svc.add_url_source(actor, business_id, body.value))
        raise InvalidInput("Выберите описание или ссылку.")

    async def upload_pdf(request):
        svc, actor = context(request)
        business_id = request.match_info["id"]
        svc.get_business(actor, business_id)
        filename, content = await file_body(request, svc.settings.max_upload_bytes)
        if not filename.lower().endswith(".pdf"):
            raise InvalidInput("Выберите PDF-файл.")
        return show(svc.add_pdf_source(actor, business_id, filename, content))

    async def upload_image(request):
        svc, actor = context(request)
        business_id = request.match_info["id"]
        svc.get_business(actor, business_id)
        filename, content = await file_body(request, min(svc.settings.max_upload_bytes, 10_000_000))
        return show(await asyncio.to_thread(svc.upload_post_image, actor, business_id, filename, content))

    async def manual_profile(request):
        svc, actor = context(request)
        body = ManualProfileInput.model_validate(await json_body(request))
        return web.json_response(business_view(svc.save_manual_profile(
            actor, request.match_info["id"], body.revision, description=body.description, products=body.products,
            audience=body.audience, tone=body.tone, constraints=body.constraints,
        )))

    async def manual_draft(request):
        svc, actor = context(request)
        body = ManualDraftInput.model_validate(await json_body(request))
        return show(await asyncio.to_thread(svc.create_manual_draft,
            actor, request.match_info["id"], body.text, body.image_asset_id, body.request_id,
            scheduled_at=body.scheduled_at, source_ids=body.source_ids,
        ))

    async def calendar(request):
        svc, actor = context(request)
        business_id = request.match_info["id"]
        business = svc.get_business(actor, business_id)
        timezone = ZoneInfo(business.timezone)
        month = request.query.get("month") or svc.now().astimezone(timezone).strftime("%Y-%m")
        if re.fullmatch(r"\d{4}-\d{2}", month) is None:
            raise InvalidInput("Укажите месяц в формате ГГГГ-ММ.")
        first = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone)
        entries = svc.list_posts(actor, business_id)
        selected = [p for p in entries if p.scheduled_at
                    and p.scheduled_at.astimezone(timezone).year == first.year
                    and p.scheduled_at.astimezone(timezone).month == first.month]
        selected.sort(key=lambda p: (p.scheduled_at, p.id))
        return web.json_response({"month": month, "timezone": business.timezone,
                                  "items": [p.model_dump(mode="json") for p in selected],
                                  "unscheduled_count": sum(p.scheduled_at is None for p in entries)})

    async def profile(request):
        svc, actor = context(request)
        business_id = request.match_info["id"]
        if request.match_info["action"] == "analyze":
            body = IdentityInput.model_validate(await json_body(request))
            business = await svc.analyze_business(
                actor, business_id, request_id=f"miniapp:{body.request_id}" if body.request_id else None,
            )
        else:
            body = RevisionInput.model_validate(await json_body(request))
            business = svc.confirm_profile(actor, business_id, body.revision)
        return web.json_response(business_view(business))

    async def connect(request):
        svc, actor = context(request)
        body = ChannelInput.model_validate(await json_body(request))
        business = await svc.connect_channel(actor, request.match_info["id"], body.value)
        return web.json_response(business_view(business))

    async def prepare(request):
        svc, actor = context(request)
        body = NewJob.model_validate(await json_body(request))
        return show(svc.request_preparation(actor, request.match_info["id"], body.request,
                                            f"miniapp:{body.request_id}"))

    async def job(request):
        svc, actor = context(request)
        return show(svc.get_job(actor, request.match_info["id"]))

    async def jobs(request):
        svc, actor = context(request)
        entries = svc.list_jobs(actor, request.match_info["id"])[::-1]
        page = int(request.query.get("page", "0"))
        if not 0 <= page <= 100_000:
            raise InvalidInput("Откройте список заново.")
        start = page * PAGE_SIZE
        return web.json_response({"items": [j.model_dump(mode="json") for j in entries[start:start + PAGE_SIZE]],
                                  "more": len(entries) > start + PAGE_SIZE})

    async def cancel_job(request):
        svc, actor = context(request)
        return show(svc.cancel_job(actor, request.match_info["id"]))

    async def control(request):
        svc, actor = context(request)
        business_id = request.match_info["id"]
        business = svc.get_business(actor, business_id)
        return web.json_response({
            "business": business_view(business),
            "jobs": [j.model_dump(mode="json") for j in svc.list_jobs(actor, business_id)[-20:][::-1]],
            "operations": svc.list_ai_operations(actor, business_id),
            "knowledge": svc.knowledge_status(actor, business_id),
            "traces": svc.ai_traces(actor, business_id),
            "usage": svc.ai_usage(actor),
            "failed_notifications": svc.failed_notifications(actor, business_id),
        })

    async def pause(request):
        svc, actor = context(request)
        body = PauseInput.model_validate(await json_body(request))
        return web.json_response(business_view(
            svc.set_publications_paused(actor, request.match_info["id"], body.paused),
        ))

    async def automation(request):
        svc, actor = context(request)
        body = AutomationInput.model_validate(await json_body(request))
        return web.json_response(business_view(svc.configure_automation(
            actor, request.match_info["id"], body.enabled, body.request, body.interval_hours, body.first_run_at,
        )))

    async def history(request):
        svc, actor = context(request)
        return web.json_response({"items": svc.conversation_history(actor, request.match_info["id"])})

    async def knowledge_search(request):
        svc, actor = context(request)
        body = await json_body(request)
        if not isinstance(body, dict) or set(body) - {"query", "limit"}:
            raise InvalidInput("Передайте query и необязательный limit.")
        return web.json_response({"items": await svc.search_knowledge(
            actor, request.match_info["id"], body.get("query", ""), limit=body.get("limit", 4),
        )})

    async def traces(request):
        svc, actor = context(request)
        return web.json_response({"items": svc.ai_traces(actor, request.match_info["id"])})

    async def chat(request):
        svc, actor = context(request)
        business_id = request.match_info["id"]
        body = ChatInput.model_validate(await json_body(request))
        request_id = f"miniapp:{body.request_id}"
        intent = await svc.interpret_request(actor, business_id, body.text, body.post_id, request_id=request_id)
        result = {"intent": intent.model_dump(mode="json")}
        if intent.action == "prepare":
            result["job"] = (await svc.prepare_with_tools(
                actor, business_id, intent.request, request_id,
            )).model_dump(mode="json")
        elif intent.action == "report":
            end = svc.now()
            result["report"] = svc.build_report(actor, business_id, end - timedelta(days=7), end)
        # Revising requires an explicit post/revision confirmation in the UI. Model output never approves.
        return web.json_response(result)

    async def post(request):
        svc, actor = context(request)
        return show(svc.get_post(actor, request.match_info["id"]))

    async def change_post(request):
        svc, actor = context(request)
        post_id, action = request.match_info["id"], request.match_info["action"]
        payload = await json_body(request)
        if action == "edit":
            edit = PostEdit.model_validate(payload)
            return show(await asyncio.to_thread(
                svc.edit_post, actor, post_id, edit.revision, text=edit.text,
                scheduled_at=edit.scheduled_at, image_asset_id=edit.image_asset_id,
            ))
        body = RevisionInput.model_validate(payload)
        change = svc.approve_post if action == "approve" else svc.reject_post
        return show(change(actor, post_id, body.revision))

    async def revise_post(request):
        svc, actor = context(request)
        body = RevisionFeedback.model_validate(await json_body(request))
        revise = svc.revise_text if request.match_info["kind"] == "text" else svc.revise_image
        return show(await revise(actor, request.match_info["id"], body.revision, body.feedback,
                                 request_id=f"miniapp:{body.request_id}"))

    async def refresh_context(request):
        svc, actor = context(request)
        body = RevisionInput.model_validate(await json_body(request))
        return show(svc.refresh_post_context(actor, request.match_info["id"], body.revision))

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
        web.get("/mini/health", health),
        web.post("/mini/session/vk", vk_session),
        web.get(r"/mini/{name:styles\.css|overview\.css|calendar\.css|brand\.css|app\.js|session\.js|demo-store\.js|preview-runtime\.js|transport\.js|access\.js|inter-variable\.woff2|inter-OFL\.txt|brand-logo(?:-white)?\.png|brand-pattern-[123]\.png|brand-(?:intro|avenir-regular|avenir-medium|avenir-demi|avenir-bold)\.ttf}", static_file),
        web.get("/mini/api/state", state),
        web.get("/mini/api/business/{id}/posts", posts),
        web.get("/mini/api/business/{id}/calendar", calendar),
        web.post("/mini/api/business", create_business),
        web.post("/mini/api/business/{id}/source", add_source),
        web.post("/mini/api/business/{id}/pdf", upload_pdf),
        web.post("/mini/api/business/{id}/image", upload_image),
        web.post("/mini/api/business/{id}/profile/manual", manual_profile),
        web.post("/mini/api/business/{id}/draft", manual_draft),
        web.post("/mini/api/business/{id}/profile/{action:analyze|confirm}", profile),
        web.post("/mini/api/business/{id}/channel", connect),
        web.post("/mini/api/business/{id}/prepare", prepare),
        web.get("/mini/api/business/{id}/control", control),
        web.post("/mini/api/business/{id}/pause", pause),
        web.post("/mini/api/business/{id}/automation", automation),
        web.get("/mini/api/business/{id}/jobs", jobs),
        web.get("/mini/api/business/{id}/history", history),
        web.post("/mini/api/business/{id}/chat", chat),
        web.post("/mini/api/business/{id}/knowledge/search", knowledge_search),
        web.get("/mini/api/business/{id}/traces", traces),
        web.get("/mini/api/job/{id}", job),
        web.post("/mini/api/job/{id}/cancel", cancel_job),
        web.get("/mini/api/post/{id}", post),
        web.post("/mini/api/post/{id}/{action:edit|approve|reject}", change_post),
        web.post("/mini/api/post/{id}/revise-{kind:text|image}", revise_post),
        web.post("/mini/api/post/{id}/refresh-context", refresh_context),
        web.get("/mini/api/asset/{id}", asset),
        web.get("/mini/api/business/{id}/report", report),
    ])
    return app


async def serve(app: web.Application, port: int, stop: asyncio.Event, *, host: str = "127.0.0.1") -> None:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host, port)
        await site.start()
        log.info("Mini App: http://%s:%s/mini/", host, port)
        await stop.wait()
    finally:
        await runner.cleanup()


async def run_live(service: BackendService, settings: Settings, stop: asyncio.Event) -> None:
    token = settings.telegram_bot_token.get_secret_value()
    await serve(create_app(service, token=token), settings.mini_app_port, stop, host=settings.mini_app_host)
