import asyncio
import io
import logging
import re
from datetime import datetime

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    MessageOriginChannel, MessageReactionCountUpdated,
)

from app.ai.fixture import DEMO_NOTES
from app.ai.sources import MAX_PDF_BYTES
from app.core.service import Marketing
from app.scheduler.worker import Worker
from app.shared.models import BusinessProfile, Channel, PostView, ReactionSnapshot, Status
from app.shared.ports import AppError

log = logging.getLogger(__name__)

LABELS = {
    Status.DRAFT: "ожидает согласования",
    Status.SCHEDULED: "запланирован",
    Status.SENDING: "отправляется",
    Status.PUBLISHED: "опубликован",
    Status.REJECTED: "отклонён",
    Status.FAILED: "ошибка отправки",
    Status.DELIVERY_UNKNOWN: "результат неизвестен",
}
PLAN_STATES = {
    "proposed": "предложен", "generating": "готовятся материалы",
    "failed": "ошибка подготовки", "ready": "материалы готовы",
}

MENU = [
    [("Мой бизнес", "business"), ("Создать контент", "compose")],
    [("План и посты", "plans"), ("Расписание", "schedule")],
    [("Отчёт", "reports"), ("Настройки автоматизации", "automation")],
]


def keys(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows
    ])


def keyboard_with_menu(*rows) -> InlineKeyboardMarkup:
    return keys(*rows, [("В меню", "menu")])


async def long_message(message: Message, text: str, markup=None) -> None:
    while len(text) > 1800:
        split = text.rfind("\n", 0, 1800)
        split = split if split > 600 else 1800
        await message.answer(text[:split])
        text = text[split:].lstrip()
    await message.answer(text, reply_markup=markup)


class LimitedFile(io.BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_PDF_BYTES:
            raise AppError("Пришлите PDF до 8 МиБ.")
        return super().write(data)


class OwnerMiddleware(BaseMiddleware):
    def __init__(self, app: Marketing):
        self.app = app

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message) or message.chat.type != "private":
            if isinstance(event, CallbackQuery):
                await event.answer("Откройте личный чат с ботом.")
            return
        try:
            self.app.authorize(event.from_user.id if event.from_user else 0)
        except AppError as exc:
            if isinstance(event, CallbackQuery):
                await event.answer(str(exc), show_alert=True)
            else:
                await message.answer(str(exc))
            return
        try:
            return await handler(event, data)
        except AppError as exc:
            await message.answer(str(exc), reply_markup=keyboard_with_menu())
        except Exception as exc:
            log.error("Telegram interface error (%s)", type(exc).__name__)
            await message.answer("Действие не завершилось. Проверьте состояние в меню и повторите при необходимости.")


def build_router(app: Marketing, worker: Worker, demo: bool) -> Router:
    router = Router()
    router.message.outer_middleware(OwnerMiddleware(app))
    router.callback_query.outer_middleware(OwnerMiddleware(app))
    pending: dict[int, tuple[str, int, int]] = {}
    previews: dict[int, tuple[int, int]] = {}
    running: set[tuple[int, str]] = set()

    def date(value: datetime) -> str:
        return value.astimezone(app.timezone).strftime("%d.%m.%Y %H:%M %Z (%z)")

    async def show_menu(message: Message):
        suffix = "\nДемо-режим: тексты и изображения тестовые." if demo else ""
        await message.answer("Автономный AI-маркетолог" + suffix, reply_markup=keys(*MENU))

    async def show_business(message: Message, user: int):
        record = app.business(user)
        sources = app.sources(user)
        source_list = "\n".join(f"#{source.id} {source.label[:130]}" for source in sources)
        channel = Channel.model_validate_json(record["channel"]) if record["channel"] else None
        channel_label = channel.title if channel else "не подключён"
        rows = [
            [("Название", "input:name"), ("Добавить сайт", "input:website")],
            [("Добавить PDF", "input:pdf"), ("Стиль и уточнения", "input:notes")],
            [("Собрать карточку", "build"), ("Подключить канал", "input:channel")],
        ]
        if record["profile"]:
            rows.append([("Открыть карточку", "profile")])
        if demo:
            rows.append([("Загрузить пример кофейни", "seed")])
        await long_message(
            message, f"{record['name'] or 'Мой бизнес'}\nКанал: {channel_label}\n"
            f"Источники:\n{source_list or 'пока нет'}",
            keyboard_with_menu(*rows),
        )

    async def show_profile(message: Message, profile: BusinessProfile, revision: int, confirmed: bool = False):
        facts = "\n".join(
            f"• {fact.statement} ({', '.join('#' + str(i) if i else 'название владельца' for i in fact.source_ids)})"
            for fact in profile.facts
        ) or "Нет подтверждённых фактов."
        questions = "\n".join(f"• {question}" for question in profile.questions)
        audience = profile.audience or "не определена"
        assumption = " (предположение)" if profile.audience_assumption else ""
        text = (
            f"{profile.name}\n{profile.description}\n\n"
            f"Продукты: {', '.join(profile.products) or 'нет данных'}\n"
            f"Аудитория: {audience}{assumption}\nСтиль: {profile.style or 'не указан'}\n"
            f"Ограничения: {', '.join(profile.restrictions) or 'нет данных'}\n\n"
            f"Факты и источники\n{facts}\n\n"
            f"Неизвестно: {', '.join(profile.unknowns) or 'нет отмеченных пробелов'}\n"
            f"{'Уточните\n' + questions + '\n' if questions else ''}"
            f"{'Карточка подтверждена.' if confirmed else 'Проверьте карточку перед подготовкой плана.'}"
        )
        rows = [[("Дополнить или исправить", "input:notes")]]
        if not confirmed:
            rows.insert(0, [("Подтвердить карточку", f"confirm:{revision}")])
        await long_message(message, text, keyboard_with_menu(*rows))

    async def show_plan(message: Message, user: int, plan_id: int):
        record, plan = app.plan(user, plan_id)
        posts = app.posts(user, plan_id)
        parts = [f"План #{plan_id} · {PLAN_STATES[record['status']]}"]
        if record["error"]:
            parts.append(record["error"])
        rows = []
        for index, item in enumerate(plan.items):
            when = posts[index].scheduled_at if posts else item.scheduled_at
            status = f" · {LABELS[posts[index].status]}" if posts else ""
            parts.append(f"{index + 1}. {item.topic}{status}\nЦель: {item.goal}\n{date(when)}")
            if posts:
                rows.append([(f"Открыть пост #{posts[index].id}", f"post:{posts[index].id}")])
        if record["status"] in {"proposed", "failed"}:
            parts.append("Подготовка создаст черновики. Каждый пост затем согласуется отдельно.")
            rows.append([("Подготовить черновики", f"prepare:{plan_id}")])
        await long_message(message, "\n\n".join(parts), keyboard_with_menu(*rows))

    async def preview(message: Message, post: PostView):
        editable = {Status.DRAFT, Status.SCHEDULED, Status.FAILED, Status.REJECTED}
        suffix = f"{post.id}:{post.version}"
        rows = []
        if post.status in editable:
            rows = [
                [("Изменить текст", f"text:{suffix}"), ("Изменить изображение", f"image:{suffix}")],
                [("Изменить время", f"time:{suffix}")],
            ]
            if post.status == Status.DRAFT:
                rows.insert(0, [("Согласовать публикацию", f"approve:{suffix}")])
            if post.status != Status.REJECTED:
                rows.append([("Отклонить", f"reject:{suffix}")])
        if post.status == Status.DELIVERY_UNKNOWN:
            rows = [
                [("Подтвердить пересланным постом", f"forward:{suffix}")],
                [("Проверил канал: поста нет", f"missing:{suffix}")],
            ]
        if post.link:
            rows.append([("Открыть опубликованный пост", f"link:{post.id}")])
        info = (
            f"Пост #{post.id}, версия {post.version} · {LABELS[post.status]}\n"
            f"Канал: {post.channel.title} ({post.channel.id})\nВремя: {date(post.scheduled_at)}"
        )
        if post.error:
            info += f"\n{post.error}"
        warnings = "\n".join(warning[:300] for warning in post.draft.warnings)
        if warnings:
            info += f"\n\nЗамечания редактора\n{warnings}"
        try:
            image = await asyncio.to_thread(app.db.read_asset, post.image_id)
        except AppError as exc:
            info += f"\n\n{exc}\n{post.draft.text}"
        else:
            photo = await message.answer_photo(BufferedInputFile(image, post.image_id), caption=post.draft.text)
            previews[photo.message_id] = (post.id, post.version)
            if len(previews) > 200:
                previews.pop(next(iter(previews)))
        await long_message(message, info, keyboard_with_menu(*rows))

    async def show_report(message: Message, user: int, days: int):
        report = app.report(user, days)
        reactions = (
            "нет данных" if report.reactions is None else
            f"{report.reactions} по {report.posts_with_reactions} постам с измерениями"
        )
        delta = "нет сопоставимых измерений"
        if report.subscriber_change is not None:
            delta = f"{report.subscriber_change:+d}"
        current = report.subscriber_end
        samples = f"\nПоследний замер: {current.total} ({date(current.measured_at)})" if current else ""
        await message.answer(
            f"Отчёт за {days} дн.\n{date(report.start)} — {date(report.end)}\n\n"
            f"Подготовлено: {report.prepared}\nОпубликовано: {report.published}\n"
            f"Отклонено: {report.rejected}\nОшибок: {report.errors}\n\n"
            f"Текущая очередь\nОжидают согласования: {report.awaiting_approval}\n"
            f"Запланированы или отправляются: {report.scheduled}\n\n"
            f"Реакции по снимкам Telegram: {reactions}\nИзменение числа подписчиков: {delta}{samples}",
            reply_markup=keyboard_with_menu(),
        )

    @router.message(Command("start", "menu", "cancel"))
    async def start(message: Message):
        pending.pop(message.from_user.id, None)
        await show_menu(message)

    @router.callback_query()
    async def callback(query: CallbackQuery):
        await query.answer()
        message, user = query.message, query.from_user.id
        parts = (query.data or "").split(":")
        action = parts[0]
        if action == "menu":
            pending.pop(user, None)
            await show_menu(message)
        elif action == "business":
            await show_business(message, user)
        elif action == "compose":
            pending.pop(user, None)
            await message.answer(
                "Напишите задачу. Например: «Подготовь три поста на неделю про наши завтраки».",
                reply_markup=keyboard_with_menu(),
            )
        elif action == "plans":
            plans = app.plans(user)
            rows = [[(f"План #{r['id']} · {PLAN_STATES[r['status']]}", f"plan:{r['id']}")] for r in plans]
            await message.answer("Последние планы" if plans else "Планов пока нет.", reply_markup=keyboard_with_menu(*rows))
        elif action == "schedule":
            posts = app.queue(user)
            rows = [[(f"#{p.id} · {date(p.scheduled_at)} · {LABELS[p.status]}", f"post:{p.id}")] for p in posts]
            await message.answer("Очередь публикаций" if posts else "Очередь пуста.", reply_markup=keyboard_with_menu(*rows))
        elif action == "reports":
            await message.answer("Выберите период.", reply_markup=keyboard_with_menu(
                [("7 дней", "report:7"), ("30 дней", "report:30"), ("90 дней", "report:90")]
            ))
        elif action == "automation":
            record = app.business(user)
            label = (
                f"Каждые {record['auto_days']} дн. Следующий запуск: {date(datetime.fromisoformat(record['auto_next']))}"
                if record["auto_days"] else "Автоподготовка выключена."
            )
            await message.answer(label, reply_markup=keyboard_with_menu(
                [("Настроить", "input:auto"), ("Выключить", "stopauto")],
            ))
        elif action == "stopauto":
            pending.pop(user, None)
            app.configure_automation(user, None)
            await message.answer("Новые автоматические циклы выключены.", reply_markup=keyboard_with_menu())
        elif action == "seed" and demo:
            if not any(s.label == "Демо-кофейня" for s in app.sources(user)):
                await add_demo_source(user)
            await show_business(message, user)
        elif action == "build":
            await message.answer("Собираю карточку по сохранённым источникам…")
            profile, revision = await app.build_profile(user)
            await show_profile(message, profile, revision)
        elif action == "profile":
            row = app.business(user)
            if not row["profile"]:
                raise AppError("Карточки пока нет. Сначала соберите её по источникам.")
            await show_profile(
                message, BusinessProfile.model_validate_json(row["profile"]), row["profile_revision"], bool(row["confirmed"]),
            )
        elif action == "input" and len(parts) == 2:
            prompts = {
                "name": "Введите название бизнеса.",
                "website": "Пришлите конечный публичный HTTP(S)-адрес страницы сайта.",
                "pdf": "Прикрепите PDF с текстовым слоем, до 8 МиБ и 24 страниц.",
                "notes": "Напишите пожелания к стилю, уточнения или исправленные факты.",
                "channel": "Добавьте бота администратором канала с правом публиковать. Пришлите @имя канала или его ID -100…",
                "auto": "Введите интервал и задачу, например «7 Завтраки и кофе на следующую неделю».",
            }
            field = parts[1]
            if field not in prompts:
                raise AppError("Это действие устарело. Откройте меню.")
            pending[user] = (field, 0, 0)
            await message.answer(prompts[field], reply_markup=keyboard_with_menu())
        elif len(parts) == 2 and parts[1].isascii() and parts[1].isdigit():
            target = int(parts[1])
            if action == "confirm":
                app.confirm_profile(user, target)
                await message.answer("Карточка подтверждена.", reply_markup=keyboard_with_menu([("Создать контент", "compose")]))
            elif action == "plan":
                await show_plan(message, user, target)
            elif action == "prepare":
                await message.answer("Готовлю тексты и изображения. Это может занять несколько минут.")
                await app.prepare(user, target)
                await show_plan(message, user, target)
            elif action == "post":
                await preview(message, app.post(user, target))
            elif action == "report":
                await show_report(message, user, target)
            elif action == "link":
                post = app.post(user, target)
                if post.link:
                    await message.answer(post.link)
        elif len(parts) == 3 and all(part.isascii() and part.isdigit() for part in parts[1:]):
            post_id, version = map(int, parts[1:])
            post = app.post(user, post_id)
            if post.version != version:
                raise AppError("Эта версия устарела. Откройте пост через меню.")
            if action == "approve":
                fresh = await app.approve(user, post_id, version)
                await message.answer(
                    f"Версия {version} запланирована на {date(post.scheduled_at)}." if fresh
                    else "Эта версия уже запланирована.", reply_markup=keyboard_with_menu(),
                )
            elif action == "reject":
                app.reject(user, post_id, version)
                await message.answer("Пост отклонён.", reply_markup=keyboard_with_menu())
            elif action == "missing":
                await message.answer(
                    "Подтвердите, что проверили канал. После закрытия случая можно отредактировать пост и согласовать новую версию.",
                    reply_markup=keyboard_with_menu([("Закрыть как неотправленный", f"close:{post_id}:{version}")]),
                )
            elif action == "close":
                worker.resolve_unknown(user, post_id, version)
                await message.answer("Случай закрыт вручную.", reply_markup=keyboard_with_menu())
            elif action in {"text", "image", "time", "forward"}:
                prompts = {
                    "text": "Напишите, что изменить в тексте.",
                    "image": "Опишите, что изменить на изображении.",
                    "time": "Введите новую дату со смещением, например 2026-12-15 10:30+03:00.",
                    "forward": "Перешлите именно опубликованный пост из канала с фото и текстом.",
                }
                pending[user] = (action, post_id, version)
                await message.answer(f"Пост #{post_id}, версия {version}. {prompts[action]}", reply_markup=keyboard_with_menu())
        else:
            raise AppError("Кнопка устарела. Откройте меню.")

    async def add_demo_source(user: int):
        app.set_name(user, "Тёплое зерно")
        await app.add_source(user, "notes", "Демо-кофейня", DEMO_NOTES)

    @router.message(F.document)
    async def document(message: Message):
        user = message.from_user.id
        file = message.document
        if (
            (file.file_size is not None and file.file_size > MAX_PDF_BYTES)
            or not (file.file_name or "").lower().endswith(".pdf")
        ):
            raise AppError("Нужен PDF с текстовым слоем размером до 8 МиБ.")
        if not app.claim_update(user, f"message:{message.chat.id}:{message.message_id}"):
            return
        await message.answer("Загружаю PDF и проверяю текстовый слой…")
        with LimitedFile() as target:
            await message.bot.download(file, destination=target, timeout=15)
            await app.add_source(user, "pdf", file.file_name, target.getvalue())
        if pending.get(user, ("", 0, 0))[0] == "pdf":
            pending.pop(user, None)
        await message.answer("PDF обработан. Соберите карточку.", reply_markup=keyboard_with_menu([("Мой бизнес", "business")]))

    @router.message()
    async def input_message(message: Message):
        user = message.from_user.id
        if not app.claim_update(user, f"message:{message.chat.id}:{message.message_id}"):
            return
        context = pending.get(user)
        text = (message.text or "").strip()
        signature = (user, f"{context!s}:{text}")
        if signature in running:
            raise AppError("Этот запрос уже обрабатывается.")
        running.add(signature)
        try:
            if context:
                await handle_input(message, user, context, text)
                if pending.get(user) == context:
                    pending.pop(user, None)
            elif text:
                await natural_request(message, user, text)
            else:
                raise AppError("Напишите задачу текстом или загрузите PDF.")
        finally:
            running.discard(signature)

    async def handle_input(message: Message, user: int, context, text: str):
        action, post_id, version = context
        if action == "forward":
            origin = message.forward_origin
            if not isinstance(origin, MessageOriginChannel):
                raise AppError("Перешлите сообщение напрямую из канала.")
            worker.resolve_unknown(
                user, post_id, version, origin.chat.id, origin.message_id, origin.date,
                message.caption or "", bool(message.photo),
            )
            await preview(message, app.post(user, post_id))
            return
        if action == "pdf" or not text:
            raise AppError("Прикрепите PDF." if action == "pdf" else "Введите текст или отмените действие через меню.")
        if action in {"text", "image", "time"}:
            await message.answer("Готовлю новую версию…")
            await preview(message, await app.revise(user, post_id, version, action, text))
            return
        if action == "name":
            app.set_name(user, text)
        elif action == "website":
            await message.answer("Загружаю страницу…")
            await app.add_source(user, "website", text, text)
        elif action == "notes":
            await app.add_source(user, "notes", "Заметки владельца", text)
        elif action == "channel":
            if not re.fullmatch(r"@[A-Za-z0-9_]{4,32}|-100[0-9]{3,17}", text):
                raise AppError("Укажите @имя канала или числовой ID вида -100…")
            channel = await app.connect_channel(user, int(text) if text.startswith("-") else text)
            await message.answer(f"Канал подключён: {channel.title}.", reply_markup=keyboard_with_menu())
            await worker.sample_subscribers()
            return
        elif action == "auto":
            interval, _, brief = text.partition(" ")
            if not re.fullmatch(r"[0-9]{1,2}", interval):
                raise AppError("Введите число дней и задачу, например «7 Завтраки на неделю».")
            app.configure_automation(user, int(interval), brief)
            await message.answer("Автоподготовка включена.", reply_markup=keyboard_with_menu())
            return
        await show_business(message, user)

    async def natural_request(message: Message, user: int, text: str):
        if len(text) > 2000:
            raise AppError("Сократите запрос до 2000 символов.")
        reply = message.reply_to_message
        target = previews.get(reply.message_id) if reply else None
        await message.answer("Разбираю задачу…")
        intent = await app.interpret_request(user, text, target is not None)
        if intent.questions:
            await long_message(message, "\n".join(intent.questions), keyboard_with_menu())
        elif intent.action == "plan" and intent.count:
            await show_plan(message, user, await app.make_plan(user, intent.brief or text, intent.count))
        elif intent.action == "revise_text" and target:
            await preview(message, await app.revise(user, *target, "text", intent.brief or text))
        elif intent.action == "report":
            await show_report(message, user, intent.days or 7)
        else:
            raise AppError("Уточните запрос. Для правки откройте пост и нажмите «Изменить текст».")

    @router.message_reaction_count()
    async def reactions(update: MessageReactionCountUpdated):
        app.record_reaction(ReactionSnapshot(
            channel_id=update.chat.id, message_id=update.message_id, measured_at=update.date,
            source="telegram:message_reaction_count", total=sum(reaction.total_count for reaction in update.reactions),
        ))

    return router
