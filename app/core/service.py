"""Role 2's application boundary. Adapters never approve or publish directly."""

import asyncio
import ipaddress
import logging
from collections.abc import Callable
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
        source = Source(
            id=str(uuid4()), business_id=business_id, kind="url", name=url, url=url, created_at=self.now()
        )
        self.repo.add_source(source)
        return source

    def list_sources(self, actor_id: int, business_id: str) -> list[Source]:
        self._business(actor_id, business_id)
        return self.repo.list_sources(business_id)

    async def analyze_business(self, actor_id: int, business_id: str) -> Business:
        business = self._business(actor_id, business_id)
        sources = self.repo.list_sources(business_id)
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

    async def interpret_request(
        self, actor_id: int, business_id: str, text: str, post_id: str | None = None
    ) -> TaskIntent:
        business = self._business(actor_id, business_id)
        if not text.strip() or len(text) > 4000:
            raise InvalidInput("Запрос должен содержать от 1 до 4000 символов.")
        if post_id and self._post(actor_id, post_id).business_id != business_id:
            raise Forbidden("Пост относится к другому бизнесу.")
        # Intent is a suggestion only; there is deliberately no 'approve/publish' model action.
        intent = self._provider_result(
            TaskIntent,
            await self._external(
                self.ai.interpret_request(text, {"business_id": business.id, "selected_post_id": post_id})
            ),
        )
        if intent.post_id and intent.post_id != post_id:
            raise InvalidInput("Выберите пост явно перед изменением.")
        return intent

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
            changes["text"] = self._validate_caption(text)
            changes["is_demo"] = post.is_demo or self.settings.mode == "demo"
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
        self, actor_id: int, post_id: str, expected_revision: int, feedback: str
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
                {"text": text, "is_demo": editing.is_demo or self.settings.mode == "demo"},
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
        self, actor_id: int, post_id: str, expected_revision: int, feedback: str
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
        job = self.repo.claim_job(self.now())
        if job is None:
            return False
        business = self.repo.get_business(job.business_id)
        try:
            self._ready(business)
            request = f"{job.request}\nПодготовь не более {self.settings.max_posts_per_job} публикаций."
            plan = self._provider_result(
                ContentPlan, await self._external(self.ai.make_plan(business.profile, request))
            )
            self._current_context(business)
            if len(plan.items) > self.settings.max_posts_per_job:
                raise InvalidInput("AI превысил лимит публикаций на одно задание.")
            self.repo.save_plan(business.id, job.id, plan, self.now())
            drafts = []
            source_ids = {source.id for source in self.repo.list_sources(business.id)}
            for item in plan.items:
                self._current_context(business)
                text = self._provider_result(
                    TextDraft, await self._external(self.ai.write_post(business.profile, item))
                )
                self._current_context(business)
                self._validate_caption(text.text)
                if set(text.used_source_ids) - source_ids:
                    raise InvalidInput("Текст ссылается на неизвестные источники.")
                generated = await self._external(self.images.generate(business.profile, text))
                self._current_context(business)
                asset = self._store_image(business.id, generated)
                proposed = as_utc(item.suggested_at) if item.suggested_at else None
                drafts.append(
                    {
                        "text": text.text,
                        "image_asset_id": asset.id,
                        "channel_id": business.channel_id,
                        "scheduled_at": proposed if proposed and proposed > self.now() else None,
                        "is_demo": asset.is_demo or self.settings.mode == "demo",
                    }
                )
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
            log.warning("Preparation failed: %s", type(exc).__name__)
            self._fail_preparation(
                business,
                job,
                str(exc) if isinstance(exc, AppError)
                else "Не удалось подготовить материалы. Проверьте профиль и доступность провайдеров.",
            )
            return True
        return True

    def _fail_preparation(self, business: Business, job: Job, message: str) -> None:
        current = self.repo.get_job(job.id)
        # A transaction may have committed before the caller saw an exception.
        if current.status in {"done", "failed"}:
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
