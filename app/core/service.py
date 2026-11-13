"""Role 2's application boundary. Adapters never approve or publish directly."""

import asyncio
import hashlib
import ipaddress
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from app.config import Settings
from app.shared.errors import (
    AppError,
    Conflict,
    DeliveryRejected,
    DeliveryUnknown,
    Forbidden,
    InvalidInput,
    ProviderUnavailable,
)
from app.shared.models import (
    Asset,
    Business,
    BusinessProfile,
    ChannelInfo,
    ContentPlan,
    Job,
    MetricSnapshot,
    Notification,
    PostStatus,
    PostView,
    PublicationResult,
    ReportInput,
    Source,
    SourceInput,
    TaskIntent,
    TextDraft,
)
from app.shared.ports import AIProvider, ImageProvider, Notifier, Publisher, ReportProvider
from app.shared.time import as_utc, utc_now
from app.storage import FileStore, Repository

log = logging.getLogger(__name__)


class BackendService:
    def __init__(
        self,
        repository: Repository,
        files: FileStore,
        settings: Settings,
        ai: AIProvider,
        images: ImageProvider,
        publisher: Publisher,
        notifier: Notifier,
        reporter: ReportProvider | None = None,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.repo = repository
        self.files = files
        self.settings = settings
        self.ai = ai
        self.images = images
        self.publisher = publisher
        self.notifier = notifier
        self.reporter = reporter
        self.clock = clock
        self._notification_lock = asyncio.Lock()

    def now(self) -> datetime:
        return as_utc(self.clock())

    def _owner(self, actor_id: int) -> None:
        if type(actor_id) is not int or actor_id != self.settings.owner_id:
            raise Forbidden("Только владелец может управлять этим приложением.")

    def _business(self, actor_id: int, business_id: str) -> Business:
        self._owner(actor_id)
        business = self.repo.get_business(business_id)
        if business.owner_id != actor_id:
            raise Forbidden("Нет доступа к этому бизнесу.")
        return business

    def _post(self, actor_id: int, post_id: str) -> PostView:
        self._owner(actor_id)
        post = self.repo.get_post(post_id)
        self._business(actor_id, post.business_id)
        return post

    async def _external(self, call):
        try:
            async with asyncio.timeout(self.settings.provider_timeout_seconds):
                return await call
        except AppError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not leak provider bodies, prompts, credentials or transport URLs.
            log.warning("Provider failed: %s", type(exc).__name__)
            raise ProviderUnavailable("Внешний сервис недоступен или вернул неверный ответ.") from None

    @staticmethod
    def _provider_result(model, result):
        try:
            return model.model_validate(result)
        except ValidationError:
            raise ProviderUnavailable(
                "Внешний сервис вернул ответ, не соответствующий общему контракту."
            ) from None

    async def _ai_operation(
        self,
        business: Business,
        kind: str,
        payload: dict,
        model,
        operation: Callable[[], Awaitable],
        *,
        request_id: str | None = None,
        dialogue_text: str | None = None,
    ):
        """Reserve one durable logical provider operation before creating its coroutine.

        Retries inside an adapter are bounded separately. This is an operation
        quota, not a currency or token estimate. Terminal failed/interrupted
        operations are never paid again for the same incoming event.
        """
        if request_id is not None:
            self._plain_text(request_id, 200, "Некорректный идентификатор действия.")
        key = request_id if request_id is not None else f"action:{uuid4()}"
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        entry = self.repo.begin_ai_operation(
            business.owner_id,
            business.id,
            kind,
            key,
            fingerprint,
            self.now(),
            daily_limit=self.settings.max_ai_operations_per_day,
            max_parallel=self.settings.max_concurrent_ai_operations,
        )
        if not entry["created"]:
            if entry["status"] == "done":
                return self._provider_result(model, entry["result"])
            raise Conflict(
                f"Действие {entry['id']} уже выполняется или завершилось с ошибкой. "
                "Проверьте состояние; повторная платная обработка этого события отключена."
            )
        operation_id = entry["id"]
        log.info("AI operation started id=%s kind=%s", operation_id, kind)
        try:
            result = self._provider_result(model, await operation())
            self.repo.finish_ai_operation(
                operation_id, result.model_dump(mode="json"), self.now(), dialogue_text=dialogue_text
            )
            log.info("AI operation completed id=%s kind=%s", operation_id, kind)
            return result
        except BaseException as exc:
            code = (
                "cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else (exc.code if isinstance(exc, AppError) else "operation_failed")
            )
            try:
                self.repo.fail_ai_operation(operation_id, code, self.now())
            except Exception as persist_error:
                log.warning(
                    "AI operation persistence failed id=%s code=%s",
                    operation_id,
                    type(persist_error).__name__,
                )
            log.warning("AI operation failed id=%s kind=%s code=%s", operation_id, kind, code)
            if isinstance(exc, (AppError, asyncio.CancelledError)):
                raise
            if not isinstance(exc, Exception):
                raise
            raise ProviderUnavailable(
                f"Не удалось завершить AI-действие. Код операции: {operation_id}."
            ) from None

    def _notification(
        self, business: Business, kind: str, text: str, post_id: str | None = None, job_id: str | None = None
    ) -> Notification:
        """Construct an outbox entry; the outcome transaction persists it."""
        return Notification(
            id=str(uuid4()),
            business_id=business.id,
            owner_id=business.owner_id,
            kind=kind,
            text=text,
            post_id=post_id,
            job_id=job_id,
            created_at=self.now(),
        )

    @staticmethod
    def _plain_text(value: str, maximum: int, message: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise InvalidInput(message)
        try:
            value.encode("utf-16-le")
        except UnicodeEncodeError:
            raise InvalidInput(message) from None
        return value

    def _current_context(self, business: Business) -> Business:
        current = self.repo.get_business(business.id)
        if (
            current.profile_version != business.profile_version
            or current.channel_id != business.channel_id
            or not current.profile_confirmed
        ):
            raise Conflict("Профиль или канал изменились во время подготовки. Повторите запрос.")
        return current

    def create_business(self, actor_id: int, name: str, timezone: str | None = None) -> Business:
        self._owner(actor_id)
        name = name.strip()
        if not name or len(name) > 200:
            raise InvalidInput("Название бизнеса должно содержать от 1 до 200 символов.")
        timezone = timezone or self.settings.timezone
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise InvalidInput("Неизвестный часовой пояс.") from None
        return self.repo.create_business(actor_id, name, timezone, self.now())

    def get_business(self, actor_id: int, business_id: str) -> Business:
        return self._business(actor_id, business_id)

    def list_businesses(self, actor_id: int) -> list[Business]:
        self._owner(actor_id)
        return [b for b in self.repo.list_businesses() if b.owner_id == actor_id]

    def accept_telegram_update(self, actor_id: int, event_key: str, digest: str) -> bool:
        """Persist a receipt before routing, including for non-AI user actions.

        This is at-most-once admission, separate from AI quota. After a process
        interruption the owner can inspect state and send a new explicit action.
        """
        self._owner(actor_id)
        return self.repo.claim_inbound_event(actor_id, event_key, digest, self.now())

    async def connect_channel(self, actor_id: int, business_id: str, channel_id: str) -> Business:
        business = self._business(actor_id, business_id)
        channel = self._provider_result(
            ChannelInfo, await self._external(self.publisher.verify_channel(channel_id, actor_id))
        )
        if not channel.id.strip() or not channel.title.strip():
            raise ProviderUnavailable("Провайдер вернул некорректные данные канала.")
        business = self._business(actor_id, business_id)
        if channel.id != business.channel_id:
            active = {PostStatus.APPROVED, PostStatus.PUBLISHING, PostStatus.DELIVERY_UNKNOWN}
            if any(post.status in active for post in self.repo.list_posts(business_id)):
                raise Conflict("Сначала отмените согласованные посты и проверьте незавершённые отправки.")
        return self.repo.set_channel(business_id, channel, self.now())

    def add_text_source(
        self, actor_id: int, business_id: str, text: str, name: str = "Описание владельца"
    ) -> Source:
        self._business(actor_id, business_id)
        if not text.strip() or len(text.encode("utf-8")) > self.settings.max_upload_bytes:
            raise InvalidInput("Пустой или слишком большой источник.")
        self._check_source_limits(business_id, extra_bytes=len(text.encode("utf-8")))
        source = Source(
            id=str(uuid4()),
            business_id=business_id,
            kind="text",
            name=name[:200],
            text=text,
            created_at=self.now(),
        )
        self.repo.add_source(source)
        return source

    def add_pdf_source(self, actor_id: int, business_id: str, filename: str, content: bytes) -> Source:
        self._business(actor_id, business_id)
        if not content.startswith(b"%PDF-"):
            raise InvalidInput("Ожидается PDF. Извлечение текста выполняет AI-модуль.")
        self._check_source_limits(business_id, extra_bytes=len(content))
        asset = self.files.write(business_id, filename, content, "application/pdf", self.now())
        self.repo.add_asset(asset)
        source = Source(
            id=str(uuid4()),
            business_id=business_id,
            kind="pdf",
            name=filename[:200],
            asset_id=asset.id,
            created_at=self.now(),
        )
        self.repo.add_source(source)
        return source

    def add_url_source(self, actor_id: int, business_id: str, url: str) -> Source:
        self._business(actor_id, business_id)
        # This boundary does not fetch URLs. Role 1 must revalidate DNS + every redirect at fetch time.
        try:
            parsed = urlsplit(url)
            hostname = (parsed.hostname or "").rstrip(".").lower()
            if (
                len(url) > 2048
                or parsed.scheme not in {"https", "http"}
                or not hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError
            if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
                (".localhost", ".local", ".internal")
            ):
                raise ValueError
            try:
                address = ipaddress.ip_address(hostname)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ValueError
            if parsed.port not in {None, 80, 443}:
                raise ValueError
        except ValueError:
            raise InvalidInput(
                "Укажите публичную HTTP(S)-страницу без пароля и нестандартного порта."
            ) from None
        self._check_source_limits(business_id, extra_bytes=len(url.encode("utf-8")))
        source = Source(
            id=str(uuid4()), business_id=business_id, kind="url", name=url, url=url, created_at=self.now()
        )
        self.repo.add_source(source)
        return source

    def list_sources(self, actor_id: int, business_id: str) -> list[Source]:
        self._business(actor_id, business_id)
        return self.repo.list_sources(business_id)

    def _check_source_limits(self, business_id: str, *, extra_bytes: int | None = None) -> list[Source]:
        sources = self.repo.list_sources(business_id)
        count = len(sources) + int(extra_bytes is not None)
        if count > self.settings.max_sources_per_business:
            raise InvalidInput(
                f"Для бизнеса допускается до {self.settings.max_sources_per_business} источников."
            )
        # Inspect stored sizes before loading PDF bodies into memory.
        total = extra_bytes or 0
        for source in sources:
            total += (
                self.repo.get_asset(source.asset_id).size
                if source.asset_id
                else len((source.text or source.url or "").encode("utf-8"))
            )
            if total > self.settings.max_source_bytes_total:
                raise InvalidInput("Суммарный объём источников превышает лимит бизнеса.")
        if total > self.settings.max_source_bytes_total:
            raise InvalidInput("Суммарный объём источников превышает лимит бизнеса.")
        return sources

    async def analyze_business(
        self, actor_id: int, business_id: str, *, request_id: str | None = None
    ) -> Business:
        business = self._business(actor_id, business_id)
        await self._ai_operation(
            business,
            "analyze",
            {"business_id": business_id},
            Business,
            lambda: self._analyze_business(actor_id, business_id),
            request_id=request_id,
        )
        return self._business(actor_id, business_id)

    async def _analyze_business(self, actor_id: int, business_id: str) -> Business:
        business = self._business(actor_id, business_id)
        sources = self._check_source_limits(business_id)
        if not sources:
            raise InvalidInput("Добавьте хотя бы один источник о бизнесе.")
        inputs = [
            SourceInput(
                source=s, content=self.files.read(self.repo.get_asset(s.asset_id)) if s.asset_id else None
            )
            for s in sources
        ]
        profile = self._provider_result(BusinessProfile, await self._external(self.ai.build_profile(inputs)))
        known = {s.id for s in sources}
        if {source.id for source in self.repo.list_sources(business_id)} != known:
            raise Conflict("Источники изменились во время анализа. Запустите анализ заново.")
        if any(set(fact.source_ids) - known for fact in profile.facts):
            raise InvalidInput("AI сослался на неизвестный источник.")
        return self.repo.set_profile(
            business_id, profile, self.now(), expected_version=business.profile_version
        )

    def save_profile(
        self, actor_id: int, business_id: str, expected_version: int, profile: BusinessProfile
    ) -> Business:
        self._business(actor_id, business_id)
        try:
            profile = BusinessProfile.model_validate(profile)
        except ValidationError:
            raise InvalidInput("Профиль бизнеса не соответствует общей схеме.") from None
        return self.repo.set_profile(business_id, profile, self.now(), expected_version=expected_version)

    def confirm_profile(self, actor_id: int, business_id: str, expected_version: int) -> Business:
        self._business(actor_id, business_id)
        return self.repo.confirm_profile(business_id, expected_version, self.now())

    def _ready(self, business: Business) -> None:
        if not business.profile or not business.profile_confirmed:
            raise Conflict("Сначала подтвердите актуальный профиль бизнеса.")
        if not business.channel_id:
            raise Conflict("Сначала подключите канал.")

    def conversation_history(self, actor_id: int, business_id: str, limit: int = 20) -> list[dict]:
        self._business(actor_id, business_id)
        return self.repo.conversation_history(business_id, limit)

    def _conversation_context(self, business: Business, post_id: str | None) -> dict:
        """Only this company's bounded data can reach the language model.

        Conversation entries are evidence of a dialogue, never authority to
        approve/publish. Live job/post state overrides past assistant prose.
        """
        profile = business.profile
        history = self.repo.conversation_history(business.id, limit=8)
        posts = sorted(self.repo.list_posts(business.id), key=lambda p: (p.updated_at, p.id), reverse=True)
        jobs = sorted(self.repo.list_jobs(business.id), key=lambda j: (j.created_at, j.id), reverse=True)
        selected = next((post for post in posts if post.id == post_id), None)
        return {
            "business_id": business.id,
            "business_name": business.name,
            "timezone": business.timezone,
            "now": self.now().isoformat(),
            "profile_confirmed": business.profile_confirmed,
            "profile_version": business.profile_version,
            "profile": None if profile is None else {
                "name": profile.name,
                "description": profile.description[:2000],
                "products": [item[:250] for item in profile.products[:10]],
                "audience": profile.audience[:500],
                "tone": profile.tone[:500],
                "constraints": [item[:400] for item in profile.constraints[:10]],
                "facts": [{"text": fact.text[:700], "source_ids": fact.source_ids[:5]}
                          for fact in profile.facts[:12]],
                "questions": [item[:300] for item in profile.questions[:5]],
            },
            "history": [
                {"user_text": item["user_text"][:1200], "assistant_text": item["assistant_text"][:1200],
                 "action": item["action"], "created_at": item["created_at"]}
                for item in history
            ],
            "recent_jobs": [
                {"id": job.id, "request": job.request[:800], "status": job.status, "stage": job.stage,
                 "created_at": job.created_at.isoformat()}
                for job in jobs[:4]
            ],
            "recent_posts": [
                {"id": post.id, "text": post.text, "status": post.status.value, "revision": post.revision,
                 "scheduled_at": post.scheduled_at.isoformat() if post.scheduled_at else None,
                 "published_at": post.published_at.isoformat() if post.published_at else None,
                 "message_url": post.message_url, "is_demo": post.is_demo}
                for post in posts[:4]
            ],
            "selected_post_id": post_id,
            "selected_post": selected.model_dump(mode="json") if selected else None,
        }

    async def interpret_request(
        self,
        actor_id: int,
        business_id: str,
        text: str,
        post_id: str | None = None,
        *,
        request_id: str | None = None,
    ) -> TaskIntent:
        business = self._business(actor_id, business_id)
        self._plain_text(text, 4000, "Запрос должен содержать от 1 до 4000 символов.")
        if post_id and self._post(actor_id, post_id).business_id != business_id:
            raise Forbidden("Пост относится к другому бизнесу.")

        # Intent is a suggestion only; there is deliberately no 'approve/publish' model action.
        async def interpret():
            intent = self._provider_result(
                TaskIntent,
                await self._external(
                    self.ai.interpret_request(text, self._conversation_context(business, post_id))
                ),
            )
            if intent.post_id and intent.post_id != post_id:
                raise InvalidInput("Выберите пост явно перед изменением.")
            if intent.action == "answer" and not intent.answer.strip():
                raise ProviderUnavailable("AI вернул пустой ответ. Уточните вопрос.")
            if any(not question.strip() or len(question) > 1000 for question in intent.questions):
                raise ProviderUnavailable("AI вернул некорректные уточняющие вопросы.")
            return intent

        return await self._ai_operation(
            business,
            "interpret",
            {"text": text, "post_id": post_id},
            TaskIntent,
            interpret,
            request_id=request_id,
            dialogue_text=text,
        )

    def request_preparation(self, actor_id: int, business_id: str, request: str, request_id: str) -> Job:
        business = self._business(actor_id, business_id)
        request = self._plain_text(request, 4000, "Укажите запрос длиной до 4000 символов.").strip()
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise InvalidInput("Укажите запрос и стабильный идентификатор действия.")
        key = f"manual:{business_id}:{request_id}"
        existing = self.repo.find_job(business_id, key)
        if existing is not None:
            if existing.request != request:
                raise Conflict("Ключ задания уже использован для другого запроса.")
            return existing
        self._ready(business)
        # Same Telegram update/callback ID -> same durable job, including after restart.
        return self.repo.enqueue_job(
            business_id,
            request,
            key,
            self.now(),
            max_pending=self.settings.max_pending_jobs,
            max_per_day=self.settings.max_generation_jobs_per_day,
        )

    def get_job(self, actor_id: int, job_id: str) -> Job:
        self._owner(actor_id)
        job = self.repo.get_job(job_id)
        self._business(actor_id, job.business_id)
        return job

    def list_jobs(self, actor_id: int, business_id: str) -> list[Job]:
        self._business(actor_id, business_id)
        return self.repo.list_jobs(business_id)

    def cancel_job(self, actor_id: int, job_id: str) -> Job:
        self.get_job(actor_id, job_id)
        return self.repo.cancel_job(job_id, self.now())

    def set_publications_paused(self, actor_id: int, business_id: str, paused: bool) -> Business:
        self._business(actor_id, business_id)
        if type(paused) is not bool:
            raise InvalidInput("Признак паузы должен быть логическим значением.")
        return self.repo.set_publications_paused(business_id, paused, self.now())

    def ai_usage(self, actor_id: int) -> dict:
        self._owner(actor_id)
        return {
            **self.repo.ai_usage(actor_id, self.now()),
            "daily_limit": self.settings.max_ai_operations_per_day,
            "parallel_limit": self.settings.max_concurrent_ai_operations,
            "period_timezone": "UTC",
        }

    def list_ai_operations(
        self,
        actor_id: int,
        business_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        self._owner(actor_id)
        if business_id is not None:
            self._business(actor_id, business_id)
        return self.repo.list_ai_operations(actor_id, business_id, limit)

    def failed_notifications(self, actor_id: int, business_id: str, limit: int = 20) -> list[dict]:
        self._business(actor_id, business_id)
        return self.repo.list_failed_notifications(business_id, limit=limit)

    def get_failed_notification(self, actor_id: int, notification_id: str) -> dict:
        self._owner(actor_id)
        notification = self.repo.get_failed_notification(notification_id)
        self._business(actor_id, notification["business_id"])
        return notification

    def retry_notification(self, actor_id: int, business_id: str, notification_id: str) -> Notification:
        self._business(actor_id, business_id)
        return self.repo.retry_notification(notification_id, business_id, self.now())

    def get_plan(self, actor_id: int, job_id: str) -> ContentPlan | None:
        self.get_job(actor_id, job_id)
        return self.repo.get_plan_for_job(job_id)

    def get_job_posts(self, actor_id: int, job_id: str) -> list[PostView]:
        job = self.get_job(actor_id, job_id)
        return [post for post in self.repo.list_posts(job.business_id) if post.job_id == job_id]

    def list_posts(self, actor_id: int, business_id: str) -> list[PostView]:
        self._business(actor_id, business_id)
        return self.repo.list_posts(business_id)

    def get_post(self, actor_id: int, post_id: str) -> PostView:
        return self._post(actor_id, post_id)

    def read_asset(self, actor_id: int, asset_id: str) -> tuple[Asset, bytes]:
        self._owner(actor_id)
        asset = self.repo.get_asset(asset_id)
        self._business(actor_id, asset.business_id)
        return asset, self.files.read(asset)

    def _store_image(self, business_id: str, image) -> Asset:
        from app.shared.models import GeneratedImage

        image = self._provider_result(GeneratedImage, image)
        signature = b"\x89PNG\r\n\x1a\n" if image.mime_type == "image/png" else b"\xff\xd8\xff"
        if not image.content.startswith(signature):
            raise InvalidInput("Сервис изображений вернул неподдерживаемый файл.")
        if self.settings.mode == "live" and image.is_demo:
            raise InvalidInput("Тестовое изображение нельзя использовать в live-режиме.")
        asset = self.files.write(
            business_id,
            "generated.png" if image.mime_type == "image/png" else "generated.jpg",
            image.content,
            image.mime_type,
            self.now(),
            is_demo=image.is_demo or self.settings.mode == "demo",
        )
        self.repo.add_asset(asset)
        return asset

    @staticmethod
    def _validate_caption(text: str) -> str:
        message = "Подпись должна быть непустой и помещаться в один пост с изображением."
        BackendService._plain_text(text, 900, message)
        if len(text.encode("utf-16-le")) // 2 > 1024:
            raise InvalidInput(message)
        return text

    def edit_post(
        self,
        actor_id: int,
        post_id: str,
        expected_revision: int,
        *,
        text: str | None = None,
        image_asset_id: str | None = None,
        scheduled_at: datetime | None = None,
        channel_id: str | None = None,
    ) -> PostView:
        post = self._post(actor_id, post_id)
        business = self._business(actor_id, post.business_id)
        changes = {}
        if text is not None:
            self._ready(business)
            changes["text"] = self._validate_caption(text)
            changes["is_demo"] = post.is_demo or self.settings.mode == "demo"
            changes["profile_version"] = business.profile_version
            changes["warnings"] = list(
                dict.fromkeys(
                    [*post.warnings, "Текст изменён вручную: проверьте соответствие указанным источникам."]
                )
            )
        if image_asset_id is not None:
            asset = self.repo.get_asset(image_asset_id)
            if asset.business_id != post.business_id or asset.mime_type not in {"image/png", "image/jpeg"}:
                raise InvalidInput("Изображение должно принадлежать этому бизнесу.")
            self.files.read(asset)
            changes["image_asset_id"] = asset.id
            changes["is_demo"] = post.is_demo or asset.is_demo or self.settings.mode == "demo"
        if scheduled_at is not None:
            scheduled_at = as_utc(scheduled_at)
            if scheduled_at <= self.now():
                raise InvalidInput("Выберите время в будущем.")
            changes["scheduled_at"] = scheduled_at
        if channel_id is not None:
            if channel_id != business.channel_id:
                raise InvalidInput("Выберите проверенный канал этого бизнеса.")
            changes["channel_id"] = channel_id
        if not changes:
            raise InvalidInput("Нет изменений.")
        return self.repo.revise_post(post_id, expected_revision, changes, self.now())

    def refresh_post_context(self, actor_id: int, post_id: str, expected_revision: int) -> PostView:
        """Explicit human review against the current profile; never an approval."""
        post = self._post(actor_id, post_id)
        business = self._business(actor_id, post.business_id)
        self._ready(business)
        return self.repo.revise_post(
            post_id,
            expected_revision,
            {"profile_version": business.profile_version},
            self.now(),
        )

    def _reserve_edit(
        self, actor_id: int, post_id: str, expected_revision: int, feedback: str
    ) -> tuple[Business, PostView]:
        post = self._post(actor_id, post_id)
        business = self._business(actor_id, post.business_id)
        self._ready(business)
        if type(expected_revision) is not int or post.revision != expected_revision:
            raise Conflict("Откройте актуальную версию поста.")
        if post.status not in {
            PostStatus.REVIEW,
            PostStatus.APPROVED,
            PostStatus.REJECTED,
            PostStatus.FAILED,
        }:
            raise Conflict("Пост уже изменяется, отправляется или отправлен.")
        if post.channel_id != business.channel_id:
            raise Conflict("Сначала обновите канал в предпросмотре поста.")
        self._plain_text(feedback, 4000, "Укажите правку длиной до 4000 символов.")
        # This durable reservation invalidates old buttons before any paid call.
        return business, self.repo.begin_edit(post_id, expected_revision, self.now())

    def _fail_edit_safely(self, business: Business, editing: PostView, message: str) -> PostView | None:
        """Preserve a committed success; otherwise fail only our reserved revision."""
        try:
            current = self.repo.get_post(editing.id)
            if current.status == PostStatus.REVIEW and current.revision == editing.revision + 1:
                return current
            if current.status == PostStatus.EDITING and current.revision == editing.revision:
                self.repo.fail_edit(
                    editing.id,
                    editing.revision,
                    message,
                    self.now(),
                    notification=self._notification(business, "edit_failed", message, editing.id),
                )
        except Exception as exc:
            # An unavailable database leaves EDITING for durable startup recovery.
            log.warning("Edit recovery persistence failed: %s", type(exc).__name__)
        return None

    async def revise_text(
        self,
        actor_id: int,
        post_id: str,
        expected_revision: int,
        feedback: str,
        *,
        request_id: str | None = None,
    ) -> PostView:
        post = self._post(actor_id, post_id)
        self._plain_text(feedback, 4000, "Укажите правку длиной до 4000 символов.")
        await self._ai_operation(
            self._business(actor_id, post.business_id),
            "revise_text",
            {"post_id": post_id, "revision": expected_revision, "feedback": feedback},
            PostView,
            lambda: self._revise_text(actor_id, post_id, expected_revision, feedback),
            request_id=request_id,
        )
        return self._post(actor_id, post_id)

    async def _revise_text(
        self,
        actor_id: int,
        post_id: str,
        expected_revision: int,
        feedback: str,
    ) -> PostView:
        business, editing = self._reserve_edit(actor_id, post_id, expected_revision, feedback)
        try:
            draft = self._provider_result(
                TextDraft,
                await self._external(self.ai.revise_text(business.profile, editing.text, feedback)),
            )
            self._current_context(business)
            known_sources = {source.id for source in self.repo.list_sources(business.id)}
            if set(draft.used_source_ids) - known_sources:
                raise InvalidInput("Текст ссылается на неизвестные источники.")
            text = self._validate_caption(draft.text)
            return self.repo.complete_edit(
                editing.id,
                editing.revision,
                {
                    "text": text,
                    "is_demo": editing.is_demo or self.settings.mode == "demo",
                    "profile_version": business.profile_version,
                    "used_source_ids": draft.used_source_ids,
                    "warnings": draft.warnings,
                },
                self.now(),
                expected_profile_version=business.profile_version,
                expected_channel_id=business.channel_id,
            )
        except asyncio.CancelledError:
            self._fail_edit_safely(business, editing, "Правка прервана. Повторите её отдельным запросом.")
            raise
        except Exception as exc:
            completed = self._fail_edit_safely(
                business,
                editing,
                "Не удалось изменить текст. Пост не будет опубликован без нового согласования.",
            )
            if completed is not None:
                return completed
            if isinstance(exc, AppError):
                raise
            log.warning("Text revision failed: %s", type(exc).__name__)
            raise ProviderUnavailable("Не удалось завершить правку текста.") from None

    async def revise_image(
        self,
        actor_id: int,
        post_id: str,
        expected_revision: int,
        feedback: str,
        *,
        request_id: str | None = None,
    ) -> PostView:
        post = self._post(actor_id, post_id)
        self._plain_text(feedback, 4000, "Укажите правку длиной до 4000 символов.")
        await self._ai_operation(
            self._business(actor_id, post.business_id),
            "revise_image",
            {"post_id": post_id, "revision": expected_revision, "feedback": feedback},
            PostView,
            lambda: self._revise_image(actor_id, post_id, expected_revision, feedback),
            request_id=request_id,
        )
        return self._post(actor_id, post_id)

    async def _revise_image(
        self,
        actor_id: int,
        post_id: str,
        expected_revision: int,
        feedback: str,
    ) -> PostView:
        business, editing = self._reserve_edit(actor_id, post_id, expected_revision, feedback)
        try:
            draft = TextDraft(text=self._validate_caption(editing.text), image_brief=feedback)
            generated = await self._external(self.images.generate(business.profile, draft, feedback))
            self._current_context(business)
            asset = self._store_image(editing.business_id, generated)
            return self.repo.complete_edit(
                editing.id,
                editing.revision,
                {
                    "image_asset_id": asset.id,
                    "is_demo": editing.is_demo or asset.is_demo or self.settings.mode == "demo",
                },
                self.now(),
                expected_profile_version=business.profile_version,
                expected_channel_id=business.channel_id,
            )
        except asyncio.CancelledError:
            self._fail_edit_safely(business, editing, "Правка прервана. Повторите её отдельным запросом.")
            raise
        except Exception as exc:
            completed = self._fail_edit_safely(
                business, editing, "Не удалось изменить изображение. Пост требует нового согласования."
            )
            if completed is not None:
                return completed
            if isinstance(exc, AppError):
                raise
            log.warning("Image revision failed: %s", type(exc).__name__)
            raise ProviderUnavailable("Не удалось завершить правку изображения.") from None

    def approve_post(self, actor_id: int, post_id: str, expected_revision: int) -> PostView:
        post = self._post(actor_id, post_id)
        business = self._business(actor_id, post.business_id)
        self._ready(business)
        asset = self.repo.get_asset(post.image_asset_id)
        if asset.business_id != business.id:
            raise InvalidInput("Некорректное изображение.")
        self.files.read(asset)
        self._validate_caption(post.text)
        if self.settings.mode == "live" and (post.is_demo or asset.is_demo):
            raise InvalidInput("Тестовый материал нельзя публиковать в live-режиме.")
        return self.repo.approve_post(post_id, expected_revision, actor_id, self.now())

    def reject_post(self, actor_id: int, post_id: str, expected_revision: int) -> PostView:
        self._post(actor_id, post_id)
        return self.repo.reject_post(post_id, expected_revision, self.now())

    def configure_automation(
        self,
        actor_id: int,
        business_id: str,
        enabled: bool,
        request: str = "Подготовь один полезный пост о бизнесе",
        interval_hours: int = 24,
        first_run_at: datetime | None = None,
    ) -> Business:
        business = self._business(actor_id, business_id)
        if type(enabled) is not bool:
            raise InvalidInput("Признак автоматизации должен быть логическим значением.")
        if enabled:
            self._ready(business)
        self._plain_text(request, 4000, "Укажите запрос длиной до 4000 символов.")
        if type(interval_hours) is not int or not 1 <= interval_hours <= 720:
            raise InvalidInput("Период должен быть от 1 до 720 часов, запрос — до 4000 символов.")
        next_at = as_utc(first_run_at) if first_run_at else self.now() + timedelta(hours=interval_hours)
        if enabled and next_at < self.now():
            raise InvalidInput("Первый запуск не может быть в прошлом.")
        return self.repo.set_automation(
            business_id, enabled, request, interval_hours, next_at if enabled else None, self.now()
        )

    def schedule_automations(self) -> int:
        return self.repo.schedule_automations(
            self.now(),
            max_pending=self.settings.max_pending_jobs,
            max_per_day=self.settings.max_generation_jobs_per_day,
        )

    async def prepare_next_job(self) -> bool:
        usage = self.repo.ai_usage(self.settings.owner_id, self.now())
        if (
            usage["running"] >= self.settings.max_concurrent_ai_operations
            or usage["used_today"] >= self.settings.max_ai_operations_per_day
        ):
            # Leave queued jobs pending while the owner has no capacity. The
            # transactional operation reservation remains the final race guard.
            return False
        job = self.repo.claim_job(self.now())
        if job is None:
            return False
        business = self.repo.get_business(job.business_id)
        try:
            self._ready(business)
            self.repo.set_job_progress(job.id, "plan", 0, 0, self.now())
            request = f"{job.request}\nПодготовь не более {self.settings.max_posts_per_job} публикаций."
            plan = await self._ai_operation(
                business,
                "plan",
                {"request": request, "profile_version": business.profile_version},
                ContentPlan,
                lambda: self._external(self.ai.make_plan(business.profile, request)),
                request_id=f"job:{job.id}:plan",
            )
            self._current_context(business)
            if len(plan.items) > self.settings.max_posts_per_job:
                raise InvalidInput("AI превысил лимит публикаций на одно задание.")
            self.repo.save_plan(business.id, job.id, plan, self.now())
            drafts = []
            source_ids = {source.id for source in self.repo.list_sources(business.id)}
            total = 1 + 2 * len(plan.items)
            for index, item in enumerate(plan.items):
                self._current_context(business)
                self.repo.set_job_progress(job.id, "text", 1 + 2 * index, total, self.now())
                text = await self._ai_operation(
                    business,
                    "text",
                    {"item": item.model_dump(mode="json"), "profile_version": business.profile_version},
                    TextDraft,
                    lambda: self._external(self.ai.write_post(business.profile, item)),
                    request_id=f"job:{job.id}:text:{index}",
                )
                self._current_context(business)
                self._validate_caption(text.text)
                if set(text.used_source_ids) - source_ids:
                    raise InvalidInput("Текст ссылается на неизвестные источники.")
                self.repo.set_job_progress(job.id, "image", 2 + 2 * index, total, self.now())

                async def generate_image():
                    generated = await self._external(self.images.generate(business.profile, text))
                    self._current_context(business)
                    return self._store_image(business.id, generated)

                asset = await self._ai_operation(
                    business,
                    "image",
                    {"text": text.model_dump(mode="json"), "profile_version": business.profile_version},
                    Asset,
                    generate_image,
                    request_id=f"job:{job.id}:image:{index}",
                )
                self._current_context(business)
                proposed = as_utc(item.suggested_at) if item.suggested_at else None
                drafts.append(
                    {
                        "text": text.text,
                        "image_asset_id": asset.id,
                        "channel_id": business.channel_id,
                        "scheduled_at": proposed if proposed and proposed > self.now() else None,
                        "is_demo": asset.is_demo or self.settings.mode == "demo",
                        "profile_version": business.profile_version,
                        "used_source_ids": text.used_source_ids,
                        "warnings": text.warnings,
                    }
                )
            self.repo.set_job_progress(job.id, "saving", total, total, self.now())
            self.repo.create_posts(
                business.id,
                drafts,
                self.now(),
                job_id=job.id,
                expected_profile_version=business.profile_version,
                expected_channel_id=business.channel_id,
                notification=self._notification(
                    business,
                    "drafts_ready",
                    f"Готово черновиков: {len(drafts)}. Откройте план и согласуйте каждый готовый пост.",
                    job_id=job.id,
                ),
            )
        except asyncio.CancelledError:
            try:
                self._fail_preparation(
                    business,
                    job,
                    "Подготовка прервана. Повторный запуск — только по отдельному запросу.",
                )
            except Exception as exc:
                log.warning("Generation cancellation persistence failed: %s", type(exc).__name__)
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "preparation_failed"
            log.warning("Preparation failed job_id=%s code=%s", job.id, code)
            self._fail_preparation(
                business,
                job,
                f"Не удалось подготовить материалы. Код: {code}. Задание: {job.id}. "
                "Проверьте профиль, лимиты и доступность провайдеров.",
            )
            return True
        return True

    def _fail_preparation(self, business: Business, job: Job, message: str) -> None:
        current = self.repo.get_job(job.id)
        # A transaction may have committed before the caller saw an exception.
        if current.status in {"done", "failed", "cancelled"}:
            return
        self.repo.fail_job(
            job.id,
            message,
            self.now(),
            notification=self._notification(business, "preparation_failed", message, job_id=job.id),
        )

    def _fail_publication(
        self,
        business: Business,
        post: PostView,
        message: str,
        unknown: bool,
        kind: str | None = None,
    ) -> None:
        current = self.repo.get_post(post.id)
        if current.status != PostStatus.PUBLISHING or current.revision != post.revision:
            return
        self.repo.fail_publication(
            post.id,
            post.revision,
            message,
            unknown,
            self.now(),
            notification=self._notification(
                business,
                kind or ("delivery_unknown" if unknown else "publication_failed"),
                message,
                post.id,
            ),
        )

    async def publish_due_once(self) -> bool:
        post = self.repo.claim_due_post(self.now())
        if post is None:
            return False
        business = self.repo.get_business(post.business_id)
        if (
            post.scheduled_at is None
            or (self.now() - as_utc(post.scheduled_at)).total_seconds()
            > self.settings.max_publish_lateness_seconds
        ):
            self._fail_publication(
                business,
                post,
                "Время публикации пропущено. Выберите новое время и согласуйте пост заново.",
                False,
                "missed_time",
            )
            return True
        attempted = False
        try:
            asset = self.repo.get_asset(post.image_asset_id)
            if asset.business_id != business.id:
                raise InvalidInput("Изображение относится к другому бизнесу.")
            image = self.files.read(asset)
            if self.settings.mode == "live" and (post.is_demo or asset.is_demo):
                raise InvalidInput("Публикация тестового материала запрещена.")
            attempted = True
            # Do not wrap ambiguous network errors in a generic provider error here.
            async with asyncio.timeout(self.settings.provider_timeout_seconds):
                result = PublicationResult.model_validate(
                    await self.publisher.publish(post, image, asset.mime_type)
                )
            if result.channel_id != post.channel_id:
                raise DeliveryUnknown("Не удалось подтвердить канал опубликованного сообщения.")
            self.repo.finish_publication(
                post.id,
                post.revision,
                result,
                self.now(),
                notification=self._notification(
                    business,
                    "published",
                    "Пост опубликован." + (f" {result.message_url}" if result.message_url else ""),
                    post.id,
                ),
            )
        except asyncio.CancelledError:
            try:
                self._fail_publication(
                    business,
                    post,
                    "Отправка прервана; проверьте канал перед повторением.",
                    attempted,
                )
            except Exception as exc:
                log.warning("Publication cancellation persistence failed: %s", type(exc).__name__)
            raise
        except DeliveryRejected:
            self._fail_publication(
                business,
                post,
                "Telegram отклонил публикацию. Исправьте причину и согласуйте новое время.",
                False,
            )
        except Exception as exc:
            log.warning("Publication failed: %s", type(exc).__name__)
            message = (
                "Результат отправки неизвестен. Проверьте канал; автоматического повтора не будет."
                if attempted
                else "Не удалось подготовить файл к отправке. Публикация не отправлена."
            )
            self._fail_publication(business, post, message, attempted)
        return True

    def resolve_unknown_delivery(
        self,
        actor_id: int,
        post_id: str,
        expected_revision: int,
        *,
        confirmed_not_sent: bool = False,
        message_id: int | None = None,
    ) -> PostView:
        post = self._post(actor_id, post_id)
        if type(confirmed_not_sent) is not bool:
            raise InvalidInput("Подтверждение отсутствия сообщения должно быть логическим значением.")
        if confirmed_not_sent == (message_id is not None):
            raise InvalidInput("Подтвердите либо отсутствие сообщения, либо его идентификатор.")
        if message_id is not None and (type(message_id) is not int or message_id <= 0):
            raise InvalidInput("Идентификатор сообщения должен быть положительным целым числом.")
        result = (
            PublicationResult(channel_id=post.channel_id, message_id=message_id)
            if message_id is not None
            else None
        )
        return self.repo.resolve_unknown(post_id, expected_revision, result, self.now())

    async def dispatch_notifications(self, limit: int = 20) -> int:
        async with self._notification_lock:
            return await self._dispatch_notification_batch(limit)

    async def _dispatch_notification_batch(self, limit: int) -> int:
        delivered = 0
        for notification in self.repo.pending_notifications(limit, now=self.now()):
            try:
                await self._external(self.notifier.send(notification))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.repo.record_notification_failure(notification.id, self.now())
            else:
                self.repo.acknowledge_notification(notification.id, now=self.now())
                delivered += 1
        return delivered

    def record_reactions(
        self, channel_id: str, message_id: int, total: int, observed_at: datetime, event_id: str
    ) -> bool:
        """Trusted Telegram adapter boundary, not a user command. Pass authenticated updates only."""
        if total < 0 or not event_id or len(event_id) > 200:
            raise InvalidInput("Некорректное событие реакции.")
        for business in self.repo.list_businesses():
            for post in self.repo.list_posts(business.id):
                if (
                    post.channel_id == channel_id
                    and post.message_id == message_id
                    and post.status == PostStatus.PUBLISHED
                ):
                    return self.repo.record_metric(
                        MetricSnapshot(
                            business_id=business.id,
                            post_id=post.id,
                            name="reactions",
                            value=total,
                            observed_at=as_utc(observed_at),
                            source="telegram",
                            event_id=f"reactions:{event_id}",
                        )
                    )
        return False

    async def collect_metrics(self) -> int:
        count = 0
        for business in self.repo.list_businesses():
            if not business.channel_id:
                continue
            try:
                value = await self._external(self.publisher.subscriber_count(business.channel_id))
                now = self.now()
                metric_source = (
                    "demo" if self.settings.mode == "demo" else "telegram"
                ) + f":{business.channel_id}"
                self.repo.record_metric(
                    MetricSnapshot(
                        business_id=business.id,
                        name="subscribers",
                        value=value,
                        observed_at=now,
                        source=metric_source,
                        event_id=f"subscribers:{business.channel_id}:{now.isoformat()}",
                    )
                )
                count += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Metric unavailable: %s", type(exc).__name__)
        return count

    def report_data(self, actor_id: int, business_id: str, start: datetime, end: datetime) -> ReportInput:
        self._business(actor_id, business_id)
        start, end = as_utc(start), as_utc(end)
        if start >= end or end - start > timedelta(days=366):
            raise InvalidInput("Выберите период от начала до конца длиной до 366 дней.")
        return self.repo.report_input(business_id, start, end)

    def build_report(self, actor_id: int, business_id: str, start: datetime, end: datetime) -> str:
        data = self.report_data(actor_id, business_id, start, end)
        if self.reporter is None:
            raise ProviderUnavailable("Модуль отчётов ещё не подключён. Данные доступны через report_data().")
        try:
            result = self.reporter.build(data)
            if asyncio.iscoroutine(result):
                result.close()
            if not isinstance(result, str) or not result.strip():
                raise ValueError("Invalid report result")
            result.encode("utf-16-le")
            return result
        except Exception as exc:
            log.warning("Report provider failed: %s", type(exc).__name__)
            raise ProviderUnavailable("Не удалось подготовить отчёт. Проверьте модуль аналитики.") from None

    def recover_interrupted(self) -> dict:
        return self.repo.recover_interrupted(self.now())
