"""VK bot: durable inbound queue, shared backend rules and private studio launch."""

import asyncio
import logging
import re
from datetime import timedelta

from app.bot.router import JOB_STATES, STATES
from app.safety.privacy import redact_text
from app.shared.errors import AppError, InvalidInput, ProviderUnavailable
from app.vk.client import VKClient
from app.vk.store import VKStore, digest

log = logging.getLogger(__name__)
LINK_HELP = ("Для доступа к документам и постам свяжите аккаунты. В нашем Telegram-боте "
             "выполните /link_vk, затем отправьте полученную команду сюда. "
             "Без приглашения команды доступ закрыт.")
ALIASES = {"меню": "/menu", "начать": "/menu", "посты": "/posts", "задания": "/jobs",
           "компании": "/businesses", "отчёт": "/report", "история": "/history"}


class VKBotAdapter:
    def __init__(self, service, *, client=None, store=None):
        self.service = service
        settings = service.settings
        self.client = client or VKClient(settings.vk_group_token.get_secret_value(), settings.vk_group_id)
        self.store = store or VKStore(settings.data_dir / "vk.sqlite3", settings.vk_group_id)
        self.ready = False

    async def close(self):
        await self.client.close()
        self.store.close()

    def issue_link(self, actor: int) -> str:
        self.service.require_access(actor)
        return self.store.issue("link", actor)

    def unlink(self, actor: int):
        self.service.require_access(actor)
        self.store.unlink(actor)

    def launch(self, actor: int, user_id: int) -> str:
        self.service.require_access(actor)
        if self.store.actor(user_id) != actor:
            raise InvalidInput("Сначала свяжите аккаунт ВК.")
        token = self.store.issue("launch", actor, user_id)
        # Fragments never reach HTTP access logs or Referer headers.
        return self.service.settings.mini_app_url + "#studioLaunch=" + token

    def exchange(self, token: str) -> str:
        actor, user_id = self.store.authenticate("launch", token, self.service.require_access, consume=True)
        return self.store.issue("session", actor, user_id)

    def session_actor(self, token: str) -> int:
        return self.store.authenticate("session", token, self.service.require_access)[0]

    def ingest(self, update: dict):
        if update.get("type") != "message_new" or update.get("group_id") != self.service.settings.vk_group_id:
            return
        body = update.get("object")
        if not isinstance(body, dict) or not isinstance(body.get("message"), dict):
            return
        message = body["message"]
        user = message.get("from_id")
        # Only direct community dialogs. Never collect a member's group-chat messages.
        if (type(user) is not int or user <= 0 or message.get("peer_id") != user
                or message.get("out", 0) != 0):
            return
        ident = message.get("conversation_message_id") or message.get("id")
        if type(ident) is not int or ident <= 0:
            return
        key = f"vk:{self.service.settings.vk_group_id}:{user}:{ident}"
        if self.store.seen(key):
            return
        text = message.get("text", "")
        if not isinstance(text, str):
            return
        actor = self.store.actor(user)
        # Link codes never enter durable inbox text, AI prompts, history or logs.
        if text.startswith("/link "):
            try:
                if not re.fullmatch(r"/link [A-Za-z0-9_-]{43}", text.strip()):
                    raise InvalidInput("Некорректный код.")
                actor = self.store.claim_link(user, text.strip()[6:], self.service.require_access)
                text = "/menu"
            except AppError:
                text = "__invalid_link__"
        elif len(text) > 4000:
            text = "__too_long__"
        self.store.enqueue(key, user, text, actor=actor)

    async def poll_loop(self, stop):
        server = None
        while not stop.is_set():
            try:
                if server is None:
                    await self.client.verify()
                    server = await self.client.poll_server()
                    saved = self.store.cursor()
                    if saved:
                        server["ts"] = saved
                    self.ready = True
                    log.info("VK community %s connected", self.service.settings.vk_group_id)
                result = await self.client.poll(server)
                failed = result.get("failed")
                if failed in (2, 3):
                    server = None
                    if failed == 3:
                        with self.store.db:
                            self.store.db.execute("DELETE FROM cursor")
                        log.warning("VK polling history expired; request current state through menu")
                    continue
                if failed not in (None, 1):
                    raise ProviderUnavailable("ВК вернул ошибку Long Poll.")
                if failed == 1:
                    log.warning("VK polling cursor expired; older updates may be unavailable")
                updates = result.get("updates", [])
                if not isinstance(updates, list) or len(updates) > 1000:
                    raise ProviderUnavailable("Некорректный пакет ВК.")
                for event in updates:
                    if isinstance(event, dict):
                        self.ingest(event)
                cursor = str(result.get("ts", ""))
                self.store.cursor(cursor)  # Ack only after all events have been persisted.
                server["ts"] = cursor
                # A transient poll failure must not leave the delivery worker paused
                # after the existing, already verified server starts responding again.
                self.ready = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ready = False
                # No raw VK URL/response/exception: they may contain credentials.
                log.warning("VK polling unavailable (%s)", type(exc).__name__)
                try:
                    await asyncio.wait_for(stop.wait(), 10)
                except TimeoutError:
                    pass

    async def run(self, stop):
        poll = asyncio.create_task(self.poll_loop(stop))
        try:
            while not stop.is_set():
                if not self.ready or not await self.process_next():
                    try:
                        await asyncio.wait_for(stop.wait(), 0.5)
                    except TimeoutError:
                        pass
        finally:
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)

    def business(self, actor, user):
        selected = self.store.selected(user)
        if selected:
            return self.service.get_business(actor, selected)
        businesses = self.service.list_businesses(actor)
        if len(businesses) != 1:
            raise InvalidInput("Выберите компанию: /businesses, затем /business ID.")
        self.store.selected(user, businesses[0].id)
        return businesses[0]

    async def respond(self, item):
        user, text = item["user_id"], item["text"].strip()
        actor = self.store.actor(user)
        if text == "__invalid_link__":
            return "Код привязки недействителен, истёк или аккаунт уже связан. Получите новый через /link_vk.", None
        if actor is None or item["actor"] != actor or self.service.access_role(actor) is None:
            return LINK_HELP, None
        self.service.require_access(actor)
        if item["kind"] == "notification":
            return text, self.launch(actor, user)
        text = ALIASES.get(text.casefold(), text)
        command, _, argument = text.partition(" ")
        command = command.casefold()
        if command in ("/start", "/menu", "/help"):
            return (
                "Контент-студия САЛАВАТСТЕКЛО\n\n"
                "Напишите вопрос о компании или задачу для подготовки поста.\n"
                "Открыть студию — документы, картинки, редактирование и согласование.\n"
                "Посты — список материалов. Задания — состояние генерации.\n"
                "/businesses — компании; /business ID — выбрать компанию;\n"
                "/report — отчёт за 7 дней; /history — история диалога.\n\n"
                "Публикация идёт в канал, выбранный в студии, только после согласования версии и времени.\n"
                "Ссылка входа одноразовая, действует 5 минут. /menu выдаст новую."
            ), self.launch(actor, user)
        if command == "/businesses":
            return "Компании:\n" + "\n".join(f"{b.name}\n/business {b.id}" for b in self.service.list_businesses(actor)), None
        if command == "/business":
            business = self.service.get_business(actor, argument.strip())
            self.store.selected(user, business.id)
            return "Выбрана компания: " + business.name, self.launch(actor, user)
        business = self.business(actor, user)
        if command == "/posts":
            items = self.service.list_posts(actor, business.id)
            return ("Посты · " + business.name + "\n" + ("\n".join(
                f"{p.text[:160]}\nСтатус: {STATES[p.status]}; версия {p.revision}"
                for p in items[:12]) or "Постов пока нет.")), self.launch(actor, user)
        if command == "/jobs":
            items = self.service.list_jobs(actor, business.id)
            return ("Задания · " + business.name + "\n" + ("\n".join(
                f"{j.request[:120]}\n{JOB_STATES[j.status]}: {j.processed}/{j.total}" for j in items[:12]
            ) or "Заданий пока нет.")), self.launch(actor, user)
        if command == "/history":
            rows = self.service.conversation_history(actor, business.id, limit=6)
            return "История · " + business.name + "\n" + ("\n".join(
                f"Вы: {row['user_text']}\nИИ: {row['assistant_text']}" for row in rows
            ) or "Диалогов пока нет."), self.launch(actor, user)
        if command == "/report":
            now = self.service.now()
            return self.service.build_report(actor, business.id, now - timedelta(days=7), now), self.launch(actor, user)
        if command.startswith("/"):
            raise InvalidInput("Неизвестная команда. /menu — помощь и вход в студию.")
        if not text or text == "__too_long__":
            raise InvalidInput("Пришлите текст до 4000 символов. Файлы загружайте кнопкой «Открыть студию».")
        request_id = "vk:" + digest(item["event_key"])
        intent = await self.service.interpret_request(actor, business.id, text, request_id=request_id)
        if intent.action == "prepare":
            job = await self.service.prepare_with_tools(actor, business.id, intent.request or text, request_id)
            return "Задание принято: " + job.id + ". Готовность — кнопка «Задания». Пост потребует согласования.", self.launch(actor, user)
        if intent.action == "report":
            now = self.service.now()
            return self.service.build_report(actor, business.id, now - timedelta(days=7), now), self.launch(actor, user)
        if intent.action == "answer":
            return intent.answer, self.launch(actor, user)
        return "\n".join(intent.questions) or "Выберите пост в студии для изменения текста или картинки.", self.launch(actor, user)

    async def process_next(self):
        item = self.store.take()
        if item is None:
            return False
        try:
            try:
                text, url = await self.respond(item)
            except AppError as exc:
                text, url = str(exc), None
            # Revocation while inference was in flight also prevents output/launch delivery.
            actor = item["actor"]
            if actor is not None and (self.store.actor(item["user_id"]) != actor
                                      or self.service.access_role(actor) is None):
                text, url = LINK_HELP, None
            await self.client.send(item["user_id"], redact_text(text), item["event_key"], url)
            self.store.finish(item["id"])
        except asyncio.CancelledError:
            self.store.finish(item["id"], "interrupted")
            raise
        except Exception as exc:
            self.store.finish(item["id"], "failed")
            log.warning("VK action/delivery failed (%s); no automatic mutation replay", type(exc).__name__)
        return True


class VKNotifications:
    def __init__(self, primary, adapter):
        self.primary, self.adapter = primary, adapter

    async def send(self, notification):
        user = self.adapter.store.user(notification.owner_id)
        if user is not None and self.adapter.service.access_role(notification.owner_id) is not None:
            self.adapter.store.enqueue("notice:" + notification.id, user, redact_text(notification.text),
                                       actor=notification.owner_id, kind="notification")
        await self.primary.send(notification)
