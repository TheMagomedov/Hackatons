import asyncio
import hashlib
import io
import json
import logging
import re
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    MessageOriginChannel,
    MessageReactionCountUpdated,
    Update,
    WebAppInfo,
)

from app.core.service import BackendService
from app.shared.errors import AppError, Conflict, InvalidInput
from app.shared.models import Business, PostStatus, PostView

log = logging.getLogger(__name__)
STUDIO_TITLE = "САЛАВАТСТЕКЛО · Контент-студия"
STUDIO_BUTTON = "Открыть студию"
STATES = {
    PostStatus.REVIEW: "ожидает согласования",
    PostStatus.EDITING: "редактируется",
    PostStatus.APPROVED: "в расписании",
    PostStatus.PUBLISHING: "отправляется",
    PostStatus.PUBLISHED: "опубликован",
    PostStatus.REJECTED: "отклонён",
    PostStatus.FAILED: "нужно исправить",
    PostStatus.DELIVERY_UNKNOWN: "результат неизвестен",
}
EDITABLE = {PostStatus.REVIEW, PostStatus.APPROVED, PostStatus.FAILED, PostStatus.REJECTED}
PAGE_SIZE = 10
QUICK_SCHEDULE = {"in15": 15, "in60": 60}
JOB_STATES = {
    "pending": "в очереди",
    "running": "готовится",
    "done": "готово",
    "failed": "ошибка",
    "cancelled": "отменено",
}
STAGES = {
    "queued": "Ожидает начала",
    "plan": "Составляю план",
    "planning": "Составляю план",
    "text": "Готовлю текст",
    "image": "Создаю изображение",
    "saving": "Сохраняю результат",
    "done": "Материалы готовы",
    "failed": "Подготовка остановлена",
    "cancelled": "Задание отменено",
}
INPUTS = {
    "name": "Введите название компании — так она будет называться в списке.",
    "notes": (
        "Пришлите описание компании: что она делает, для кого и какие факты можно использовать в постах. "
        "Это материал для профиля, а не готовый пост."
    ),
    "url": "Пришлите публичную ссылку на страницу с информацией о компании или её продуктах.",
    "pdf": "Прикрепите текстовый PDF до 8 МиБ и 24 страниц.",
    "channel": "Добавьте бота администратором канала с правом публиковать и пришлите @имя или ID канала.",
    "request": (
        "Задайте вопрос AI-маркетологу или опишите пост: тема, аудитория и количество. "
        "Например: «Подготовь один пост о стекле для архитекторов». "
        "AI создаст черновик; перед отправкой его нужно согласовать."
    ),
    "auto": (
        "Укажите, как часто AI должен готовить новые черновики: интервал в часах и тему. "
        "Например: «24 Пост о применении стекла в архитектуре». "
        "Каждый пост потребует отдельного согласования."
    ),
    "text": "Опишите, как AI должен изменить текст этой версии. Например: «Сделай короче и убери призыв к покупке».",
    "image": "Опишите, как AI должен изменить изображение этой версии. Например: «Добавь больше дневного света».",
    "time": "Пришлите дату со смещением, например 2026-12-15 10:30+03:00.",
    "found": "Перешлите опубликованное сообщение с фото и текстом напрямую из канала.",
    "profile-tone": "Укажите новый стиль общения.",
}


def menu(*rows: list[tuple[str, str]], mini_app_url: str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=([
            [InlineKeyboardButton(text=STUDIO_BUTTON, web_app=WebAppInfo(url=mini_app_url))]
        ] if mini_app_url else []) + [
            [InlineKeyboardButton(text=label, callback_data=action) for label, action in row]
            for row in (*rows, [("Главное меню", "home")])
        ]
    )


async def install_mini_app_menu(bot: Bot, owner_id: int, mini_app_url: str | None) -> bool:
    """Update one member's menu; an inline launch remains available on Telegram failure."""
    if not mini_app_url:
        return False
    try:
        await bot.set_chat_menu_button(
            chat_id=owner_id,
            menu_button=MenuButtonWebApp(text=STUDIO_BUTTON, web_app=WebAppInfo(url=mini_app_url)),
            request_timeout=15,
        )
    except TelegramAPIError:
        log.warning("Telegram не установил меню Mini App. Кнопка запуска доступна в главном меню бота.")
        return False
    return True


async def reply(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    from app.safety.privacy import redact_text

    text = redact_text(text)
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


@dataclass
class BotState:
    pending: Prompt | None = None
    selected: str | None = None
    context_epoch: int = 0
    inflight_inputs: set[int] = field(default_factory=set)


_STATE: ContextVar[BotState] = ContextVar("bot_user_state")
READ_CALLBACKS = {"home", "business", "posts", "post", "jobs", "job", "job-posts", "report", "history"}


def state():
    return _STATE.get()


class OwnerAccess(BaseMiddleware):
    def __init__(self, service: BackendService):
        self.service = service
        self.owner = service.settings.owner_id
        self.states: dict[int, BotState] = {}
        # Fast local cache complements the durable receipt of every owner update.
        self.seen: OrderedDict[int, None] = OrderedDict()

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message) or message.chat.type != "private":
            if isinstance(event, CallbackQuery):
                await event.answer("Откройте личный чат с ботом.")
            return
        actor = event.from_user.id if event.from_user is not None else None
        if isinstance(event, Message) and actor is not None:
            invitation = re.fullmatch(r"/start(?:@[A-Za-z0-9_]+)?\s+join_([A-Za-z0-9_-]{43})", event.text or "")
            if invitation:
                try:
                    self.service.claim_invite(actor, invitation.group(1))
                except AppError as exc:
                    await message.answer(str(exc))
                    return
        role = self.service.access_role(actor)
        if role is None:
            if isinstance(event, CallbackQuery):
                await event.answer("Для входа нужно приглашение команды.", show_alert=True)
            else:
                await message.answer("Для входа нужно приглашение команды. Откройте личную ссылку приглашения.")
            return
        if role == "reviewer":
            readable = ((event.data or "").split(":", 1)[0] in READ_CALLBACKS) if isinstance(
                event, CallbackQuery,
            ) else bool(re.match(r"^/(start|menu|cancel|help|link_vk|unlink_vk)(?:@[A-Za-z0-9_]+)?(?:\s|$)", event.text or ""))
            if not readable:
                if isinstance(event, CallbackQuery):
                    await event.answer("Режим наблюдателя: доступен только просмотр.", show_alert=True)
                else:
                    await message.answer("Режим наблюдателя: откройте /menu для просмотра готовых материалов.")
                return
        update = data.get("event_update")
        state_token = _STATE.set(self.states.setdefault(actor, BotState()))
        try:
            if isinstance(update, Update):
                if update.update_id in self.seen:
                    if isinstance(event, CallbackQuery):
                        await event.answer("Это действие уже обработано. Откройте актуальное меню.")
                    return
                digest = hashlib.sha256(
                    json.dumps(
                        update.model_dump(mode="json", exclude_none=True),
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                accepted = self.service.accept_telegram_update(
                    actor,
                    event_key(update, event.bot.id),
                    digest,
                )
                self.seen[update.update_id] = None
                if len(self.seen) > 512:
                    self.seen.popitem(last=False)
                if not accepted:
                    if isinstance(event, CallbackQuery):
                        await event.answer("Это действие уже принято. Проверьте результат через меню.")
                    return
            return await handler(event, data)
        except AppError as exc:
            await reply(message, str(exc), menu())
        except Exception as exc:
            log.warning(
                "Telegram handler failed update=%s (%s)",
                update.update_id if isinstance(update, Update) else "unknown",
                type(exc).__name__,
            )
            await reply(message, "Действие не завершилось. Проверьте состояние через меню.", menu())
        finally:
            _STATE.reset(state_token)


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


def page_number(value: str) -> int:
    if not value:
        return 0
    if not value.isascii() or not value.isdigit() or len(value) > 6:
        raise InvalidInput("Откройте список заново.")
    return int(value)


def page_rows(items: list, page: int, callback_prefix: str):
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages - 1)
    navigation = []
    if page:
        navigation.append(("← Назад", f"{callback_prefix}:{page - 1}"))
    if page + 1 < pages:
        navigation.append(("Далее →", f"{callback_prefix}:{page + 1}"))
    return items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE], navigation, f"{page + 1}/{pages}"


def event_key(update: Update, bot_id: int) -> str:
    return f"telegram:{bot_id}:update:{update.update_id}"


def build_router(service: BackendService) -> Router:
    router = Router()
    owner_access = OwnerAccess(service)
    router.message.outer_middleware(owner_access)
    router.callback_query.outer_middleware(owner_access)

    def check_input_available() -> None:
        if state().pending is None and state().context_epoch in state().inflight_inputs:
            raise Conflict("Предыдущий ответ ещё обрабатывается. Можно открыть другое действие через меню.")

    def restore_prompt(context: Prompt | None, epoch: int, actor: int) -> None:
        if context is None or epoch != state().context_epoch or state().pending is not None:
            return
        # A failed paid edit already consumed its revision; never restore its old prompt.
        if context.kind in {"text", "image", "time", "found"}:
            try:
                if service.get_post(actor, context.object_id).revision != context.revision:
                    return
            except AppError:
                return
        state().pending = context

    async def home(message: Message, actor: int) -> None:
        state().context_epoch += 1
        state().pending = None
        state().selected = None
        businesses = service.list_businesses(actor)
        rows = [[(b.name[:50], f"business:{b.id}")] for b in businesses[-15:]]
        viewer = service.access_role(actor) == "reviewer"
        if not viewer:
            rows.append([("Добавить компанию", "input:name")])
        await reply(
            message,
            STUDIO_TITLE + (
                "\n\nНаблюдатель · только просмотр. Выберите компанию, чтобы посмотреть профиль, посты и отчёт."
                if viewer else "\n\nВыберите компанию ниже. В её меню можно добавить материалы, "
                "подготовить посты с AI и согласовать отправку в канал."
            )
            + (
                "\n\n«Открыть студию» — редактор постов и расписание. "
                "Там можно написать пост и загрузить своё изображение без нейросети."
                if service.settings.mini_app_url and not viewer else ""
            )
            + ("\n\nЕсли студия не открывается, отправьте /menu и используйте новую кнопку."
               if service.settings.mini_app_url else ""),
            menu(
                *rows,
                mini_app_url=service.settings.mini_app_url,
            ),
        )
        # Every way back home repairs a stale personal menu after a URL rotation.
        # Reply first so a slow/failed Telegram menu update cannot hide the fresh inline button.
        await install_mini_app_menu(message.bot, actor, service.settings.mini_app_url)

    def active(actor: int) -> Business:
        if state().selected:
            return service.get_business(actor, state().selected)
        businesses = service.list_businesses(actor)
        if len(businesses) == 1:
            return businesses[0]
        raise InvalidInput("Выберите бизнес в главном меню.")

    async def business_menu(message: Message, actor: int, business: Business) -> None:
        sources = service.list_sources(actor, business.id)
        profile = business.profile
        viewer = service.access_role(actor) == "reviewer"
        parts = [
            f"Компания: {business.name}",
            f"Канал: {business.channel_title or business.channel_id or 'не подключён'}",
            f"Материалов для профиля: {len(sources)}",
            "Публикации приостановлены"
            if business.publications_paused
            else "Публикации по расписанию включены",
        ]
        rows = [
            [("Добавить описание компании", f"input:notes:{business.id}")],
            [
                ("Добавить сайт", f"input:url:{business.id}"),
                ("Загрузить PDF", f"input:pdf:{business.id}"),
            ],
            [("Собрать профиль с AI", f"analyze:{business.id}")],
            [("Сменить канал" if business.channel_id else "Подключить канал", f"input:channel:{business.id}")],
            [("Создать посты с AI", f"input:request:{business.id}"), ("Открыть посты", f"posts:{business.id}")],
            [("Отчёт за 7 дней", f"report:{business.id}")],
            [("Настроить автогенерацию", f"input:auto:{business.id}")],
            [("Задания генерации", f"jobs:{business.id}"), ("Лимиты запросов AI", f"usage:{business.id}")],
            [("Недоставленные уведомления", f"notices:{business.id}")],
            [
                (
                    "Возобновить отправку постов" if business.publications_paused else "Приостановить отправку постов",
                    f"resume:{business.id}" if business.publications_paused else f"pause:{business.id}",
                )
            ],
        ]
        if business.automation_enabled:
            parts.append(
                f"Следующая автоподготовка: {format_time(business.next_generation_at, business.timezone)}"
            )
            rows.append([("Отключить автоподготовку", f"auto-off:{business.id}")])
        if viewer:
            parts.append("Доступен просмотр: откройте посты, задания генерации, историю или отчёт.")
        elif not profile:
            parts.append("Следующий шаг: «Собрать профиль с AI» — он использует добавленные материалы."
                         if sources else "Следующий шаг: «Добавить описание компании», сайт или PDF.")
        elif not business.profile_confirmed:
            parts.append("Следующий шаг: проверьте сведения ниже и нажмите «Подтвердить профиль».")
        elif not business.channel_id:
            parts.append("Следующий шаг: «Подключить канал» — укажите, куда отправлять согласованные посты.")
        else:
            parts.append("Следующий шаг: «Создать посты с AI» или «Открыть посты», чтобы проверить готовые. "
                         "Отправка начнётся только после согласования версии и выбора времени.")
        if service.settings.mini_app_url and not viewer:
            parts.append("Пост вручную: откройте студию, выберите эту компанию, затем «Посты → Создать вручную». "
                         "Можно написать свой текст и загрузить изображение без AI.")
        if profile:
            parts.extend(
                [
                    "Профиль подтверждён"
                    if business.profile_confirmed
                    else "Проверьте и подтвердите профиль",
                    profile.description,
                    f"Стиль: {profile.tone}",
                    f"Аудитория: {profile.audience or 'не указана'}",
                ]
            )
            if profile.products:
                parts.append("Продукты: " + ", ".join(profile.products))
            names = {s.id: s.name for s in sources}
            for fact in profile.facts:
                evidence = ", ".join(names.get(source_id, source_id) for source_id in fact.source_ids)
                parts.append(f"• {fact.text}" + (f" ({evidence})" if evidence else ""))
            parts.extend(f"Предположение: {value}" for value in profile.assumptions)
            parts.extend(f"Уточните: {value}" for value in profile.questions)
            rows.append([("Изменить стиль общения", f"profile-tone:{business.id}:{business.profile_version}")])
            if not business.profile_confirmed:
                rows.append([("Подтвердить профиль", f"confirm:{business.id}:{business.profile_version}")])
        rows.append([("История диалога", f"history:{business.id}")])
        if viewer:
            parts.insert(1, "Наблюдатель · только просмотр")
            rows = [[(label, action) for label, action in row if action.split(":", 1)[0] in READ_CALLBACKS]
                    for row in rows]
            rows = [row for row in rows if row]
        await reply(message, "\n".join(parts), menu(*rows, mini_app_url=service.settings.mini_app_url))

    async def preview(message: Message, actor: int, post: PostView) -> None:
        from app.safety.privacy import guard_public_output

        business = service.get_business(actor, post.business_id)
        guard_public_output(post.text)
        for warning in post.warnings:
            guard_public_output(warning)
        if post.image_asset_id is None:
            image_available = True
            await reply(message, post.text)
        else:
            try:
                asset, content = service.read_asset(actor, post.image_asset_id)
            except AppError:
                image_available = False
                await reply(message, f"{post.text}\n\nФото недоступно. Для черновика можно запросить новое.")
            else:
                image_available = True
                await message.answer_photo(BufferedInputFile(content, asset.filename), caption=post.text)
        suffix = f"{post.id}:{post.revision}"
        stale_profile = post.profile_version != business.profile_version or not business.profile_confirmed
        rows = []
        if post.status in EDITABLE:
            rows.extend(
                [
                    [("Правка текста с AI", f"text:{suffix}")],
                    [("Правка изображения с AI", f"image:{suffix}")],
                    [("Время: через 15 мин", f"in15:{suffix}"), ("Время: через час", f"in60:{suffix}")],
                    [("Выбрать дату и время", f"time:{suffix}")],
                ]
            )
            if stale_profile:
                rows.append([("Проверил текст по новому профилю", f"refresh:{suffix}")])
            if (
                post.status == PostStatus.REVIEW
                and not stale_profile
                and image_available
                and post.scheduled_at
                and post.scheduled_at > service.now()
            ):
                rows.append([("Согласовать отправку версии", f"approve:{suffix}")])
            if post.status in {PostStatus.REVIEW, PostStatus.APPROVED}:
                rows.append([("Отклонить", f"reject:{suffix}")])
        if post.status == PostStatus.DELIVERY_UNKNOWN:
            rows.extend(
                [
                    [("Подтвердить пересланным сообщением", f"found:{suffix}")],
                    [("Проверил канал: поста нет", f"missing:{suffix}")],
                ]
            )
        info = (
            f"Компания: {business.name}\nВерсия {post.revision} · {STATES[post.status]}\nКанал: {post.channel_id}\n"
            f"Время: {format_time(post.scheduled_at, business.timezone)}"
        )
        if stale_profile:
            info += (
                "\nПрофиль бизнеса изменился или ещё не подтверждён. Сверьте текст с актуальными сведениями."
            )
        if post.profile_version is not None:
            info += f"\nПодготовлено по версии профиля: {post.profile_version}."
        if post.used_source_ids:
            names = {source.id: source.name for source in service.list_sources(actor, post.business_id)}
            info += "\nИсточники: " + ", ".join(
                names.get(source_id, "Источник недоступен") for source_id in post.used_source_ids
            )
        if post.warnings:
            info += "\nПеред согласованием проверьте:\n" + "\n".join(
                f"• {warning}" for warning in post.warnings
            )
        if business.publications_paused:
            info += "\nПубликации приостановлены. Согласование сохранится, отправка ждёт возобновления."
        for detail in (post.last_error, post.message_url):
            if detail:
                info += f"\n{detail}"
        if service.access_role(actor) == "reviewer":
            rows = []
            info += "\nНаблюдатель · редактирование и публикация недоступны."
        await reply(message, info, menu(*rows))

    async def show_posts(message: Message, posts: list[PostView], prefix: str, page: int = 0) -> None:
        # Repository insertion order breaks ties when a batch shares one timestamp.
        ordered = sorted(reversed(posts), key=lambda post: post.created_at, reverse=True)
        visible, navigation, counter = page_rows(ordered, page, prefix)
        rows = [[(f"{STATES[p.status]} · {p.text[:25]}", f"post:{p.id}")] for p in visible]
        if navigation:
            rows.append(navigation)
        await reply(message, f"Посты · страница {counter}" if ordered else "Постов пока нет.", menu(*rows))

    async def show_jobs(message: Message, actor: int, business_id: str, page: int = 0) -> None:
        jobs = sorted(
            service.list_jobs(actor, business_id), key=lambda job: (job.created_at, job.id), reverse=True
        )
        visible, navigation, counter = page_rows(jobs, page, f"jobs:{business_id}")
        rows = [[(f"{JOB_STATES[job.status]} · {job.request[:35]}", f"job:{job.id}")] for job in visible]
        if navigation:
            rows.append(navigation)
        await reply(message, f"Задания · страница {counter}" if jobs else "Заданий пока нет.", menu(*rows))

    async def show_job(message: Message, actor: int, job_id: str) -> None:
        job = service.get_job(actor, job_id)
        if job.status in {"pending", "running"}:
            progress = STAGES.get(job.stage, "Подготовка продолжается")
            if job.total:
                progress += f" · этапов завершено: {job.processed} из {job.total}"
            if job.updated_at:
                business = service.get_business(actor, job.business_id)
                progress += f"\nПоследнее изменение: {format_time(job.updated_at, business.timezone)}"
            rows = [[("Обновить", f"job:{job.id}")]]
            if job.status == "pending" and service.access_role(actor) != "reviewer":
                rows.append([("Отменить задание", f"cancel-job:{job.id}")])
            await reply(message, "Задание принято. " + progress + ".", menu(*rows))
            return
        if job.status == "cancelled":
            await reply(
                message,
                "Задание отменено до начала подготовки.",
                menu([("Задания", f"jobs:{job.business_id}")]),
            )
            return
        if job.status == "failed":
            await reply(
                message,
                (job.error or "Подготовка не завершилась.") + f"\nНомер задания: {job.id}",
                menu([("Задания", f"jobs:{job.business_id}")]),
            )
            return
        plan = service.get_plan(actor, job.id)
        if plan:
            await reply(
                message, "Контент-план\n" + "\n".join(f"• {item.topic} — {item.goal}" for item in plan.items)
            )
        await show_posts(message, service.get_job_posts(actor, job.id), f"job-posts:{job.id}")

    async def show_usage(message: Message, actor: int, business_id: str) -> None:
        usage = service.ai_usage(actor)
        await reply(
            message,
            f"Использование AI за сегодня (UTC)\n"
            f"Операций: {usage['used_today']} из {usage['daily_limit']}\n"
            f"Сейчас выполняется: {usage['running']} из {usage['parallel_limit']}\n"
            f"Завершено: {usage['done_today']} · С ошибкой: {usage['failed_today']}\n"
            "Это количество операций, а не сумма списания провайдером.",
            menu([("Последние операции", f"operations:{business_id}")]),
        )

    async def show_operations(message: Message, actor: int, business_id: str) -> None:
        entries = service.list_ai_operations(actor, business_id, limit=10)
        if not entries:
            await reply(message, "AI-операций для этого бизнеса пока нет.", menu())
            return
        lines = ["Последние AI-операции"]
        states = {"running": "выполняется", "done": "готово", "failed": "ошибка"}
        for entry in entries:
            lines.append(f"{entry['id']} · {states.get(entry['status'], entry['status'])}")
            lines.append(f"Обновлено: {entry['updated_at']}")
            if entry.get("error_code"):
                lines.append(f"Код причины: {entry['error_code']}")
        await reply(message, "\n".join(lines), menu([("Обновить", f"operations:{business_id}")]))

    async def show_notices(message: Message, actor: int, business_id: str) -> None:
        entries = service.failed_notifications(actor, business_id, limit=50)
        rows = [
            [(f"Уведомление {entry['id'][:8]} · {entry['attempts']} попыток", f"notice:{entry['id']}")]
            for entry in entries
        ]
        await reply(
            message,
            "Не удалось доставить уведомления. Выберите конкретное для повтора."
            if entries
            else "Недоставленных после всех попыток уведомлений нет.",
            menu(*rows),
        )

    async def ask(message: Message, actor: int, prompt: Prompt) -> None:
        business_id = prompt.object_id
        if prompt.kind in {"text", "image", "time", "found"}:
            business_id = service.get_post(actor, prompt.object_id).business_id
        context = f"Компания: {service.get_business(actor, business_id).name}\n\n" if business_id else ""
        state().pending = prompt
        rows = [[("Отменить ввод · к компании", f"business:{business_id}")]] if business_id else []
        await reply(
            message,
            context + INPUTS[prompt.kind] + "\n\nЖду ваш ответ. /cancel — отменить ввод и вернуться в меню.",
            menu(*rows),
        )

    @router.message(Command("start", "menu", "cancel"))
    async def start(message: Message) -> None:
        await home(message, message.from_user.id)

    @router.message(Command("link_vk", "unlink_vk"))
    async def link_vk(message: Message) -> None:
        adapter = getattr(service, "vk", None)
        if adapter is None:
            await reply(message, "Бот ВК пока не подключён: нужен ключ сообщества в настройках сервера.")
            return
        actor = message.from_user.id
        if message.text.split()[0].split("@")[0] == "/unlink_vk":
            adapter.unlink(actor)
            await reply(message, "Привязка ВК и все выданные через неё сеансы студии отозваны.")
            return
        token = adapter.issue_link(actor)
        await message.answer(
            "Отправьте эту команду в личные сообщения нашего сообщества ВК. "
            "Код одноразовый, действует 10 минут. Не передавайте его другим людям.\n\n/link " + token
        )

    @router.message(Command("help"))
    async def help_message(message: Message) -> None:
        if service.access_role(message.from_user.id) == "reviewer":
            await reply(message, "Вы можете просматривать профиль компании, посты с изображениями, "
                        "историю и отчёт. Генерация, правки и публикация доступны команде. Откройте /menu.", menu())
            return
        await reply(
            message,
            "1. Выберите компанию или нажмите «Добавить компанию».\n"
            "2. Добавьте описание, сайт или PDF. «Собрать профиль с AI» извлечёт сведения из этих материалов.\n"
            "3. Проверьте и подтвердите профиль, затем подключите Telegram-канал.\n"
            "4. «Создать посты с AI» — задайте тему. Для своего текста и изображения: студия → «Посты → Создать вручную».\n"
            "5. «Открыть посты» — проверьте черновик, выберите время и согласуйте отправку версии.\n\n"
            "Автогенерация готовит черновики по интервалу; согласование каждого поста остаётся за вами.\n"
            "/menu — список компаний. /cancel — отменить ожидаемый ответ. "
            "/link_vk — связать аккаунт ВК; /unlink_vk — отозвать привязку. "
            "Название компании указано перед каждым запросом ввода.",
            menu(mini_app_url=service.settings.mini_app_url),
        )

    @router.callback_query()
    async def callback(query: CallbackQuery, event_update: Update) -> None:
        message, actor = query.message, query.from_user.id
        action, _, tail = (query.data or "").partition(":")
        target, _, version = tail.partition(":")
        changes_context = action in {
            "home",
            "business",
            "input",
            "profile-tone",
            "text",
            "image",
            "time",
            "found",
        }
        if changes_context:
            state().context_epoch += 1
            state().pending = None
        epoch = state().context_epoch
        await query.answer()
        if changes_context and epoch != state().context_epoch:
            return  # A newer selection/input won while Telegram acknowledged this old button.
        if action == "home":
            await home(message, actor)
        elif action == "business":
            business = service.get_business(actor, target)
            state().pending = None
            state().selected = business.id  # Select before awaiting; a late render never retargets input.
            await business_menu(message, actor, business)
        elif action == "input" and target in {"name", "notes", "url", "pdf", "channel", "request", "auto"}:
            business_id = "" if target == "name" else service.get_business(actor, version).id
            if target == "request":
                # A reply to the interpreter's clarification must stay in the
                # company of this button, even when it came from an older menu.
                state().selected = business_id
            await ask(message, actor, Prompt(target, business_id))
        elif action == "analyze":
            await reply(message, "Изучаю материалы…")
            await business_menu(
                message,
                actor,
                await service.analyze_business(
                    actor, target, request_id=event_key(event_update, query.bot.id)
                ),
            )
        elif action == "posts":
            await show_posts(
                message, service.list_posts(actor, target), f"posts:{target}", page_number(version)
            )
        elif action == "job-posts":
            await show_posts(
                message, service.get_job_posts(actor, target), f"job-posts:{target}", page_number(version)
            )
        elif action == "post":
            await preview(message, actor, service.get_post(actor, target))
        elif action == "job":
            await show_job(message, actor, target)
        elif action == "jobs":
            await show_jobs(message, actor, target, page_number(version))
        elif action == "cancel-job":
            service.cancel_job(actor, target)
            await show_job(message, actor, target)
        elif action in {"pause", "resume"}:
            business = service.set_publications_paused(actor, target, action == "pause")
            await reply(
                message,
                "Новые отправки приостановлены. Уже начавшаяся отправка может завершиться; проверьте её статус."
                if action == "pause"
                else "Публикации возобновлены. Посты с пропущенным временем потребуют нового расписания и согласования.",
            )
            await business_menu(message, actor, business)
        elif action == "usage":
            service.get_business(actor, target)
            await show_usage(message, actor, target)
        elif action == "operations":
            await show_operations(message, actor, target)
        elif action == "notices":
            await show_notices(message, actor, target)
        elif action in {"notice", "retry-notice"}:
            entry = service.get_failed_notification(actor, target)
            if action == "retry-notice":
                service.retry_notification(actor, entry["business_id"], target)
                await reply(
                    message,
                    "Уведомление поставлено на повторную доставку. Посты и генерация не запускались.",
                    menu([("К списку уведомлений", f"notices:{entry['business_id']}")]),
                )
            else:
                await reply(
                    message,
                    f"Уведомление {entry['id']}\nНе доставлено после {entry['attempts']} попыток.\n"
                    "Повтор отправит только это уведомление. После обрыва связи ранее отправленное сообщение может повториться.",
                    menu([("Повторить это уведомление", f"retry-notice:{target}")]),
                )
        elif action == "history":
            history = service.conversation_history(actor, target)
            lines = [f"Вы: {turn['user_text']}\nАссистент: {turn['assistant_text']}" for turn in history]
            await reply(message, "\n\n".join(lines) if lines else "В этой компании пока нет истории диалога.", menu())
        elif action == "report":
            now = service.now()
            await reply(message, service.build_report(actor, target, now - timedelta(days=7), now), menu())
        elif action == "auto-off":
            await business_menu(message, actor, service.configure_automation(actor, target, False))
        elif action in {
            "confirm",
            "profile-tone",
            "text",
            "image",
            "time",
            "found",
            "approve",
            "reject",
            "missing",
            "refresh",
            *QUICK_SCHEDULE,
        }:
            if not version.isascii() or not version.isdigit() or len(version) > 9:
                raise InvalidInput("Кнопка устарела. Откройте актуальное меню.")
            revision = int(version)
            if action == "confirm":
                await business_menu(message, actor, service.confirm_profile(actor, target, revision))
            elif action == "profile-tone":
                service.get_business(actor, target)
                await ask(message, actor, Prompt(action, target, revision))
            elif action == "approve":
                post = service.approve_post(actor, target, revision)
                await reply(message, f"Версия {post.revision} согласована и поставлена в расписание.", menu())
            elif action == "reject":
                service.reject_post(actor, target, revision)
                await reply(message, "Пост отклонён.", menu())
            elif action == "refresh":
                updated = service.refresh_post_context(actor, target, revision)
                await preview(message, actor, updated)
            elif action in QUICK_SCHEDULE:
                when = service.now() + timedelta(minutes=QUICK_SCHEDULE[action])
                updated = service.edit_post(actor, target, revision, scheduled_at=when)
                await preview(message, actor, updated)
            elif action == "missing":
                post = service.resolve_unknown_delivery(actor, target, revision, confirmed_not_sent=True)
                await preview(message, actor, post)
            else:
                post = service.get_post(actor, target)
                if post.revision != revision:
                    raise Conflict("Версия изменилась. Откройте актуальный предпросмотр.")
                await ask(message, actor, Prompt(action, target, revision))
        else:
            raise InvalidInput("Кнопка устарела. Откройте главное меню.")

    @router.message(F.document)
    async def document(message: Message) -> None:
        check_input_available()
        actor = message.from_user.id
        context = state().pending
        if context and context.kind != "pdf":
            raise InvalidInput("Завершите текущее действие или нажмите /cancel перед загрузкой PDF.")
        business = service.get_business(actor, context.object_id) if context else active(actor)
        file = message.document
        limit = min(8 * 1024 * 1024, service.settings.max_upload_bytes)
        if not (file.file_name or "").lower().endswith((".pdf", ".docx")) or (file.file_size or 0) > limit:
            raise InvalidInput("Пришлите PDF или DOCX до 8 МиБ.")
        state().pending = None  # Consume before downloading; later callbacks can safely set a new prompt.
        state().context_epoch += 1
        epoch = state().context_epoch
        state().inflight_inputs.add(epoch)
        try:
            with LimitedFile(limit) as destination:
                await message.bot.download(file, destination=destination, timeout=20)
                add = service.add_docx_source if file.file_name.lower().endswith('.docx') else service.add_pdf_source
                await asyncio.to_thread(add, actor, business.id, file.file_name, destination.getvalue())
        except AppError:
            restore_prompt(context, epoch, actor)
            raise
        finally:
            state().inflight_inputs.discard(epoch)
        await business_menu(message, actor, service.get_business(actor, business.id))

    async def handle_post_input(
        message: Message, actor: int, prompt: Prompt, text: str, request_id: str
    ) -> None:
        post_id, revision = prompt.object_id, prompt.revision
        if prompt.kind == "found":
            post = service.get_post(actor, post_id)
            origin = message.forward_origin
            if (
                not isinstance(origin, MessageOriginChannel)
                or str(origin.chat.id) != post.channel_id
                or not message.photo
                or message.caption != post.text
                or (post.scheduled_at and origin.date < post.scheduled_at.replace(microsecond=0))
            ):
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
            updated = await service.revise_text(actor, post_id, revision, text, request_id=request_id)
        else:
            updated = await service.revise_image(actor, post_id, revision, text, request_id=request_id)
        await preview(message, actor, updated)

    @router.message()
    async def receive(message: Message, event_update: Update) -> None:
        check_input_available()
        context = state().pending
        state().pending = None  # Never clear or replace a newer prompt after a provider await.
        state().context_epoch += 1
        epoch = state().context_epoch
        if context is not None:
            state().inflight_inputs.add(epoch)
        try:
            await receive_text(message, event_update, context)
        except AppError:
            restore_prompt(context, epoch, message.from_user.id)
            raise
        finally:
            state().inflight_inputs.discard(epoch)

    async def receive_text(message: Message, event_update: Update, context: Prompt | None) -> None:
        actor = message.from_user.id
        text = (message.text or "").strip()
        if context and context.kind == "found":
            await handle_post_input(message, actor, context, text, event_key(event_update, message.bot.id))
        elif not text:
            raise InvalidInput("Пришлите текст или выберите загрузку PDF.")
        elif context and context.kind in {"text", "image", "time"}:
            await handle_post_input(message, actor, context, text, event_key(event_update, message.bot.id))
        elif context and context.kind == "name":
            business = service.create_business(actor, text)
            state().selected = business.id
            await business_menu(message, actor, business)
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
                business = service.configure_automation(
                    actor, business.id, True, request=request, interval_hours=int(hours)
                )
            elif kind == "profile-tone":
                if not business.profile or len(text) > 400:
                    raise InvalidInput("Нужен профиль и описание стиля до 400 символов.")
                business = service.save_profile(
                    actor, business.id, context.revision, business.profile.model_copy(update={"tone": text})
                )
            elif kind == "pdf":
                raise InvalidInput(INPUTS["pdf"])
            elif kind == "request":
                intent = await service.interpret_request(
                    actor, business.id, text, request_id=event_key(event_update, message.bot.id)
                )
                if intent.action == "prepare":
                    request = intent.request or text
                    job = await service.prepare_with_tools(
                        actor, business.id, request, event_key(event_update, message.bot.id)
                    )
                    await show_job(message, actor, job.id)
                elif intent.action == "report":
                    now = service.now()
                    await reply(
                        message,
                        service.build_report(actor, business.id, now - timedelta(days=7), now),
                        menu(),
                    )
                elif intent.action == "answer":
                    await reply(message, intent.answer, menu())
                else:
                    question = "\n".join(intent.questions) or "Уточните запрос или выберите конкретный пост."
                    await reply(message, question, menu())
                return
            await business_menu(message, actor, service.get_business(actor, business.id))

    @router.message_reaction_count()
    async def reactions(event: MessageReactionCountUpdated, event_update: Update) -> None:
        service.record_reactions(
            str(event.chat.id),
            event.message_id,
            sum(r.total_count for r in event.reactions),
            event.date,
            event_key(event_update, event.bot.id),
        )

    return router
