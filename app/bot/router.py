import io
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
    MessageReactionCountUpdated,
    Update,
)

from app.core.service import BackendService
from app.shared.errors import AppError, Conflict, InvalidInput
from app.shared.models import Business, PostStatus, PostView

log = logging.getLogger(__name__)
STATES = {
    PostStatus.REVIEW: "ожидает согласования",
    PostStatus.APPROVED: "в расписании",
    PostStatus.PUBLISHING: "отправляется",
    PostStatus.PUBLISHED: "опубликован",
    PostStatus.REJECTED: "отклонён",
    PostStatus.FAILED: "ошибка отправки",
    PostStatus.DELIVERY_UNKNOWN: "результат неизвестен",
}
EDITABLE = {PostStatus.REVIEW, PostStatus.APPROVED, PostStatus.FAILED, PostStatus.REJECTED}
INPUTS = {
    "name": "Введите название бизнеса.",
    "notes": "Пришлите сведения о бизнесе и пожелания к стилю.",
    "url": "Пришлите конечный публичный адрес страницы.",
    "pdf": "Прикрепите текстовый PDF до 8 МиБ и 24 страниц.",
    "channel": "Добавьте бота администратором канала с правом публиковать и пришлите @имя или ID канала.",
    "request": "Опишите задачу, например «Подготовь три поста про завтраки».",
    "auto": "Введите интервал в часах и задачу, например «24 Пост о завтраках».",
    "text": "Что изменить в тексте?",
    "image": "Что изменить на изображении?",
    "time": "Пришлите дату со смещением, например 2026-12-15 10:30+03:00.",
    "found": "Перешлите опубликованное сообщение с фото и текстом напрямую из канала.",
    "profile-tone": "Укажите новый стиль общения.",
}


def menu(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=action) for label, action in row]
        for row in (*rows, [("Главное меню", "home")])
    ])


async def reply(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    while len(text) > 1800:
        cut = text.rfind("\n", 0, 1800)
        cut = cut if cut > 600 else 1800
        await message.answer(text[:cut])
        text = text[cut:].lstrip()
    await message.answer(text, reply_markup=markup)


@dataclass(frozen=True)
class Prompt:
    kind: str
    object_id: str = ""
    revision: int = 0


class OwnerAccess(BaseMiddleware):
    def __init__(self, owner: int):
        self.owner = owner

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message) or message.chat.type != "private":
            if isinstance(event, CallbackQuery):
                await event.answer("Откройте личный чат с ботом.")
            return
        if event.from_user is None or event.from_user.id != self.owner:
            if isinstance(event, CallbackQuery):
                await event.answer("Управлять ботом может только владелец.", show_alert=True)
            else:
                await message.answer("Управлять ботом может только владелец.")
            return
        try:
            return await handler(event, data)
        except AppError as exc:
            await reply(message, str(exc), menu())
        except Exception as exc:
            log.warning("Telegram handler failed (%s)", type(exc).__name__)
            await reply(message, "Действие не завершилось. Проверьте состояние через меню.", menu())


class LimitedFile(io.BytesIO):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > self.limit:
            raise InvalidInput("Размер файла превышает лимит.")
        return super().write(data)


def format_time(value: datetime | None, timezone: str) -> str:
    return value.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M (%z)") if value else "не указано"


def build_router(service: BackendService) -> Router:
    router = Router()
    owner_access = OwnerAccess(service.settings.owner_id)
    router.message.outer_middleware(owner_access)
    router.callback_query.outer_middleware(owner_access)
    pending: Prompt | None = None
    selected: str | None = None

    async def home(message: Message, actor: int) -> None:
        nonlocal pending
        pending = None
        businesses = service.list_businesses(actor)
        rows = [[(b.name[:50], f"business:{b.id}")] for b in businesses[-15:]]
        await reply(message, "Автономный AI-маркетолог. Выберите бизнес или создайте карточку.", menu(
            *rows, [("Создать бизнес", "input:name")],
        ))

    def active(actor: int) -> Business:
        if selected:
            return service.get_business(actor, selected)
        businesses = service.list_businesses(actor)
        if len(businesses) == 1:
            return businesses[0]
        raise InvalidInput("Выберите бизнес в главном меню.")

    async def business_menu(message: Message, actor: int, business: Business) -> None:
        nonlocal selected
        selected = business.id
        sources = service.list_sources(actor, business.id)
        profile = business.profile
        parts = [business.name, f"Канал: {business.channel_title or business.channel_id or 'не подключён'}",
                 f"Источников: {len(sources)}"]
        rows = [
            [("Текст", f"input:notes:{business.id}"), ("Сайт", f"input:url:{business.id}"),
             ("PDF", f"input:pdf:{business.id}")],
            [("Собрать профиль", f"analyze:{business.id}"), ("Канал", f"input:channel:{business.id}")],
            [("Подготовить посты", f"input:request:{business.id}"), ("Посты", f"posts:{business.id}")],
            [("Отчёт за 7 дней", f"report:{business.id}"), ("Автоподготовка", f"input:auto:{business.id}")],
        ]
        if business.automation_enabled:
            rows.append([("Отключить автоподготовку", f"auto-off:{business.id}")])
        if profile:
            parts.extend([
                "Профиль подтверждён" if business.profile_confirmed else "Проверьте и подтвердите профиль",
                profile.description, f"Стиль: {profile.tone}", f"Аудитория: {profile.audience or 'не указана'}",
            ])
            if profile.products:
                parts.append("Продукты: " + ", ".join(profile.products))
            names = {s.id: s.name for s in sources}
            for fact in profile.facts:
                evidence = ", ".join(names.get(source_id, source_id) for source_id in fact.source_ids)
                parts.append(f"• {fact.text}" + (f" ({evidence})" if evidence else ""))
            parts.extend(f"Предположение: {value}" for value in profile.assumptions)
            parts.extend(f"Уточните: {value}" for value in profile.questions)
            rows.append([("Изменить стиль", f"profile-tone:{business.id}:{business.profile_version}")])
            if not business.profile_confirmed:
                rows.append([("Подтвердить профиль", f"confirm:{business.id}:{business.profile_version}")])
        await reply(message, "\n".join(parts), menu(*rows))

    async def preview(message: Message, actor: int, post: PostView) -> None:
        business = service.get_business(actor, post.business_id)
        asset, content = service.read_asset(actor, post.image_asset_id)
        await message.answer_photo(BufferedInputFile(content, asset.filename), caption=post.text)
        suffix = f"{post.id}:{post.revision}"
        rows = []
        if post.status in EDITABLE:
            rows.extend([
                [("Изменить текст", f"text:{suffix}"), ("Изменить фото", f"image:{suffix}")],
                [("Изменить время", f"time:{suffix}")],
            ])
            if post.status == PostStatus.REVIEW:
                rows.append([("Согласовать версию", f"approve:{suffix}")])
            if post.status in {PostStatus.REVIEW, PostStatus.APPROVED}:
                rows.append([("Отклонить", f"reject:{suffix}")])
        if post.status == PostStatus.DELIVERY_UNKNOWN:
            rows.extend([
                [("Подтвердить пересланным сообщением", f"found:{suffix}")],
                [("Проверил канал: поста нет", f"missing:{suffix}")],
            ])
        info = (
            f"Версия {post.revision} · {STATES[post.status]}\nКанал: {post.channel_id}\n"
            f"Время: {format_time(post.scheduled_at, business.timezone)}"
        )
        for detail in (post.last_error, post.message_url):
            if detail:
                info += f"\n{detail}"
        await reply(message, info, menu(*rows))

    async def show_posts(message: Message, posts: list[PostView]) -> None:
        rows = [[(f"{STATES[p.status]} · {p.text[:25]}", f"post:{p.id}")] for p in posts[:20]]
        await reply(message, "Посты бизнеса" if rows else "Постов пока нет.", menu(*rows))

    async def show_job(message: Message, actor: int, job_id: str) -> None:
        job = service.get_job(actor, job_id)
        if job.status in {"pending", "running"}:
            await reply(message, "Задание принято. Подготовка продолжается.", menu([("Обновить", f"job:{job.id}")]))
            return
        if job.status == "failed":
            await reply(message, job.error or "Подготовка не завершилась.", menu())
            return
        plan = service.get_plan(actor, job.id)
        if plan:
            await reply(message, "Контент-план\n" + "\n".join(f"• {item.topic} — {item.goal}" for item in plan.items))
        await show_posts(message, service.get_job_posts(actor, job.id))

    async def ask(message: Message, prompt: Prompt) -> None:
        nonlocal pending
        pending = prompt
        await reply(message, INPUTS[prompt.kind], menu())

    @router.message(Command("start", "menu", "cancel"))
    async def start(message: Message) -> None:
        await home(message, message.from_user.id)

    @router.callback_query()
    async def callback(query: CallbackQuery) -> None:
        nonlocal pending
        await query.answer()
        message, actor = query.message, query.from_user.id
        action, _, tail = (query.data or "").partition(":")
        target, _, version = tail.partition(":")
        pending = None
        if action == "home":
            await home(message, actor)
        elif action == "business":
            await business_menu(message, actor, service.get_business(actor, target))
        elif action == "input" and target in {"name", "notes", "url", "pdf", "channel", "request", "auto"}:
            business_id = "" if target == "name" else service.get_business(actor, version).id
            await ask(message, Prompt(target, business_id))
        elif action == "analyze":
            await reply(message, "Изучаю материалы…")
            await business_menu(message, actor, await service.analyze_business(actor, target))
        elif action == "posts":
            await show_posts(message, service.list_posts(actor, target))
        elif action == "post":
            await preview(message, actor, service.get_post(actor, target))
        elif action == "job":
            await show_job(message, actor, target)
        elif action == "report":
            now = service.now()
            await reply(message, service.build_report(actor, target, now - timedelta(days=7), now), menu())
        elif action == "auto-off":
            await business_menu(message, actor, service.configure_automation(actor, target, False))
        elif action in {"confirm", "profile-tone", "text", "image", "time", "found", "approve", "reject", "missing"}:
            if not version.isascii() or not version.isdigit() or len(version) > 9:
                raise InvalidInput("Кнопка устарела. Откройте актуальное меню.")
            revision = int(version)
            if action == "confirm":
                await business_menu(message, actor, service.confirm_profile(actor, target, revision))
            elif action == "profile-tone":
                service.get_business(actor, target)
                await ask(message, Prompt(action, target, revision))
            elif action == "approve":
                post = service.approve_post(actor, target, revision)
                await reply(message, f"Версия {post.revision} согласована и поставлена в расписание.", menu())
            elif action == "reject":
                service.reject_post(actor, target, revision)
                await reply(message, "Пост отклонён.", menu())
            elif action == "missing":
                post = service.resolve_unknown_delivery(actor, target, revision, confirmed_not_sent=True)
                await preview(message, actor, post)
            else:
                post = service.get_post(actor, target)
                if post.revision != revision:
                    raise Conflict("Версия изменилась. Откройте актуальный предпросмотр.")
                await ask(message, Prompt(action, target, revision))
        else:
            raise InvalidInput("Кнопка устарела. Откройте главное меню.")

    @router.message(F.document)
    async def document(message: Message) -> None:
        nonlocal pending
        actor = message.from_user.id
        context = pending
        if context and context.kind != "pdf":
            raise InvalidInput("Завершите текущее действие или нажмите /cancel перед загрузкой PDF.")
        business = service.get_business(actor, context.object_id) if context else active(actor)
        file = message.document
        limit = min(8 * 1024 * 1024, service.settings.max_upload_bytes)
        if not (file.file_name or "").lower().endswith(".pdf") or (file.file_size or 0) > limit:
            raise InvalidInput("Пришлите PDF до 8 МиБ.")
        with LimitedFile(limit) as destination:
            await message.bot.download(file, destination=destination, timeout=20)
            service.add_pdf_source(actor, business.id, file.file_name, destination.getvalue())
        if pending is context:
            pending = None
        await business_menu(message, actor, business)

    async def handle_post_input(message: Message, actor: int, prompt: Prompt, text: str) -> None:
        post_id, revision = prompt.object_id, prompt.revision
        if prompt.kind == "found":
            post = service.get_post(actor, post_id)
            origin = message.forward_origin
            if not isinstance(origin, MessageOriginChannel) or str(origin.chat.id) != post.channel_id \
                    or not message.photo or message.caption != post.text:
                raise InvalidInput("Перешлите нужный пост с фото и точной подписью из подключённого канала.")
            updated = service.resolve_unknown_delivery(actor, post_id, revision, message_id=origin.message_id)
        elif prompt.kind == "time":
            try:
                when = datetime.fromisoformat(text)
                if when.utcoffset() is None:
                    raise ValueError
            except ValueError as exc:
                raise InvalidInput(INPUTS["time"]) from exc
            updated = service.edit_post(actor, post_id, revision, scheduled_at=when)
        elif prompt.kind == "text":
            updated = await service.revise_text(actor, post_id, revision, text)
        else:
            updated = await service.revise_image(actor, post_id, revision, text)
        await preview(message, actor, updated)

    @router.message()
    async def receive(message: Message, event_update: Update) -> None:
        nonlocal pending
        actor = message.from_user.id
        context = pending
        text = (message.text or "").strip()
        if context and context.kind == "found":
            await handle_post_input(message, actor, context, text)
        elif not text:
            raise InvalidInput("Пришлите текст или выберите загрузку PDF.")
        elif context and context.kind in {"text", "image", "time"}:
            await handle_post_input(message, actor, context, text)
        elif context and context.kind == "name":
            await business_menu(message, actor, service.create_business(actor, text))
        else:
            business = service.get_business(actor, context.object_id) if context else active(actor)
            kind = context.kind if context else "request"
            if kind == "notes":
                service.add_text_source(actor, business.id, text)
            elif kind == "url":
                service.add_url_source(actor, business.id, text)
            elif kind == "channel":
                business = await service.connect_channel(actor, business.id, text)
            elif kind == "auto":
                hours, _, request = text.partition(" ")
                if not hours.isascii() or not hours.isdigit() or len(hours) > 4 or not request.strip():
                    raise InvalidInput(INPUTS["auto"])
                business = service.configure_automation(actor, business.id, True, request=request,
                                                        interval_hours=int(hours))
            elif kind == "profile-tone":
                if not business.profile or len(text) > 400:
                    raise InvalidInput("Нужен профиль и описание стиля до 400 символов.")
                business = service.save_profile(actor, business.id, context.revision,
                                                business.profile.model_copy(update={"tone": text}))
            elif kind == "pdf":
                raise InvalidInput(INPUTS["pdf"])
            elif kind == "request":
                intent = await service.interpret_request(actor, business.id, text)
                if intent.action == "prepare":
                    job = service.request_preparation(actor, business.id, intent.request or text,
                                                      f"telegram_update:{event_update.update_id}")
                    await show_job(message, actor, job.id)
                elif intent.action == "report":
                    now = service.now()
                    await reply(message, service.build_report(actor, business.id, now - timedelta(days=7), now), menu())
                else:
                    question = "\n".join(intent.questions) or "Уточните запрос или выберите конкретный пост."
                    await reply(message, question, menu())
                if pending is context:
                    pending = None
                return
            await business_menu(message, actor, service.get_business(actor, business.id))
        if pending is context:
            pending = None

    @router.message_reaction_count()
    async def reactions(event: MessageReactionCountUpdated, event_update: Update) -> None:
        service.record_reactions(str(event.chat.id), event.message_id, sum(r.total_count for r in event.reactions),
                                 event.date, f"telegram_update:{event_update.update_id}")

    return router
