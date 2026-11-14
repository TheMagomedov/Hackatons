"""Role 2's application boundary. Adapters never approve or publish directly."""

import asyncio
import hashlib
import io
import ipaddress
import json
import logging
import time
import warnings
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from app.access import actor_action
from app.ai.resilience import inference_scope
from app.config import Settings
from app.safety import NoopSafety, SafetyPolicy
from app.safety.input_guard import guard_input, guard_payload, inspect_input
from app.safety.privacy import guard_public_output
from app.shared.errors import (
    AppError,
    Conflict,
    DeliveryRejected,
    DeliveryUnknown,
    Forbidden,
    InvalidInput,
    ProviderUnavailable,
    SafetyBlocked,
    SafetyUnavailable,
)
from app.shared.models import (
    Asset,
    Business,
    BusinessProfile,
    ChannelInfo,
    ContentPlan,
    Fact,
    Job,
    MetricSnapshot,
    Notification,
    PlanItem,
    PostStatus,
    PostView,
    PublicationResult,
    ReportInput,
    Source,
    SourceInput,
    TaskIntent,
    TextDraft,
    VisualDraft,
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
        *,
        safety: SafetyPolicy | None = None,
        knowledge=None,
        telemetry=None,
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
        self.safety = NoopSafety() if safety is None else safety
        self.knowledge = knowledge
        self.telemetry = telemetry
        self._notification_lock = asyncio.Lock()

    def now(self) -> datetime:
        return as_utc(self.clock())

    def access_role(self, actor_id: int) -> str | None:
        return self.repo.access_role(self.settings.owner_id, actor_id)

    def require_access(self, actor_id: int, *, write: bool = False) -> str:
        return self.repo.require_access(self.settings.owner_id, actor_id, write=write)

    def claim_invite(self, actor_id: int, token: str) -> str:
        return self.repo.claim_access_invite(self.settings.owner_id, actor_id, token, self.now())

    def _owner(self, actor_id: int) -> None:
        self.require_access(actor_id, write=True)

    def _business(self, actor_id: int, business_id: str, *, write: bool = True) -> Business:
        self.require_access(actor_id, write=write)
        business = self.repo.get_business(business_id)
        if business.owner_id != self.settings.owner_id:
            raise Forbidden("Нет доступа к этому бизнесу.")
        role = self.repo.business_role(self.settings.owner_id, actor_id, business_id)
        if role is None or (write and role == 'reviewer'):
            raise Forbidden('Нет прав на это действие в выбранной компании.')
        if write and business.archived:
            raise Conflict("Компания в архиве. Сначала восстановите её.")
        return business

    def _post(self, actor_id: int, post_id: str, *, write: bool = True) -> PostView:
        self.require_access(actor_id, write=write)
        post = self.repo.get_post(post_id)
        self._business(actor_id, post.business_id, write=write)
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
            validated = model.model_validate(result)
        except ValidationError:
            raise ProviderUnavailable(
                "Внешний сервис вернул ответ, не соответствующий общему контракту."
            ) from None
        if isinstance(validated, TextDraft):
            for value in [validated.text, validated.image_brief, *validated.warnings]:
                guard_public_output(value)
        if isinstance(validated, ContentPlan):
            for item in validated.items:
                for value in [item.topic, item.goal, item.brief, item.image_brief]:
                    guard_public_output(value)
        return validated

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
        scope = {"business_id": business.id, "operation_id": operation_id, "kind": kind}
        started = time.monotonic()
        self._trace(scope, "started")
        try:
            with inference_scope(business.id, operation_id, kind, business.profile_version):
                result = self._provider_result(model, await operation())
            self.repo.finish_ai_operation(
                operation_id, result.model_dump(mode="json"), self.now(), dialogue_text=dialogue_text
            )
            log.info("AI operation completed id=%s kind=%s", operation_id, kind)
            self._trace(scope, "completed", (time.monotonic() - started) * 1000)
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
            self._trace(scope, "failed", (time.monotonic() - started) * 1000)
            if isinstance(exc, (AppError, asyncio.CancelledError)):
                raise
            if not isinstance(exc, Exception):
                raise
            raise ProviderUnavailable(
                f"Не удалось завершить AI-действие. Код операции: {operation_id}."
            ) from None

    def _trace(self, scope, event, duration_ms=0):
        if self.telemetry is not None:
            try:
                self.telemetry.trace(scope, event, duration_ms=duration_ms)
            except Exception:
                log.warning("AI metadata trace could not be saved")

    def ai_traces(self, actor_id: int, business_id: str, *, limit: int = 30) -> list[dict]:
        self._business(actor_id, business_id, write=False)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInput("Лимит журнала: от 1 до 100.")
        return [] if self.telemetry is None else self.telemetry.recent(business_id, limit=limit)

    def _source_inputs(self, business_id: str) -> list[SourceInput]:
        return [SourceInput(source=source, content=self.files.read(self.repo.get_asset(source.asset_id))
                            if source.asset_id else None)
                for source in self._check_source_limits(business_id)]

    async def search_knowledge(self, actor_id: int, business_id: str, query: str, *, limit: int = 4) -> list[dict]:
        self._business(actor_id, business_id, write=False)
        self._plain_text(query, 4000, "Поисковый запрос должен содержать от 1 до 4000 символов.")
        guard_input(query)
        if type(limit) is not int or not 1 <= limit <= 6:
            raise InvalidInput("Лимит фрагментов: от 1 до 6.")
        if self.knowledge is None:
            return []
        inputs = self._source_inputs(business_id)
        await self.knowledge.index_sources(business_id, inputs)
        self._business(actor_id, business_id, write=False)  # Permission may change while extracting.
        current = {source.id for source in self.repo.list_sources(business_id)}
        if current != {item.source.id for item in inputs}:
            raise Conflict("Источники изменились во время поиска. Повторите запрос.")
        hits = await asyncio.to_thread(self.knowledge.search, business_id, query, limit=limit,
                                     allowed_source_ids=current, max_context_chars=5000)
        self._business(actor_id, business_id, write=False)
        if current != {source.id for source in self.repo.list_sources(business_id)}:
            raise Conflict('Источники изменились во время поиска. Повторите запрос.')
        result = [hit.as_context() for hit in hits]
        guard_payload(result)
        return result

    def knowledge_status(self, actor_id: int, business_id: str) -> dict:
        self._business(actor_id, business_id, write=False)
        if self.knowledge is None:
            return {"enabled": False, "sources": 0, "chunks": 0}
        ids = {source.id for source in self.repo.list_sources(business_id)}
        status = self.knowledge.status(business_id, allowed_source_ids=ids)
        return {"enabled": True, "sources": status.sources, "chunks": status.chunks,
                "fingerprint": status.fingerprint, "method": "local_bm25"}

    async def _knowledge_profile(self, business: Business, query: str) -> BusinessProfile:
        """Enrich an inference snapshot; never silently approve or persist retrieved clauses."""
        hits = await self.search_knowledge(business.owner_id, business.id, query[:4000])
        profile = business.profile
        facts = list(profile.facts)
        for hit in hits:
            fact = Fact(text=hit["text"][:900], source_ids=[hit["source_id"]])
            if fact in facts:
                continue
            candidate = profile.model_copy(update={"facts": [*facts, fact]})
            if len(candidate.model_dump_json()) <= 5900:
                facts.append(fact)
        return profile.model_copy(update={"facts": facts})

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

    def _current_context(self, business: Business, actor_id: int | None = None) -> Business:
        if actor_id is not None:
            self._business(actor_id, business.id)
        current = self.repo.get_business(business.id)
        if (
            current.archived or current.profile_version != business.profile_version
            or current.channel_id != business.channel_id
            or not current.profile_confirmed
        ):
            raise Conflict("Профиль или канал изменились во время подготовки. Повторите запрос.")
        return current

    @actor_action
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
        business = self.repo.create_business(self.settings.owner_id, name, timezone, self.now())
        if not self.repo.business_role(self.settings.owner_id, actor_id, business.id):
            self.repo.set_company_access(self.settings.owner_id, actor_id, business.id, 'operator', 'selected', self.now())
        return business

    def get_business(self, actor_id: int, business_id: str) -> Business:
        return self._business(actor_id, business_id, write=False)

    def list_businesses(self, actor_id: int) -> list[Business]:
        self.require_access(actor_id)
        return [b for b in self.repo.list_businesses() if b.owner_id == self.settings.owner_id and not b.archived
                and self.repo.business_role(self.settings.owner_id, actor_id, b.id)]

    def archived_businesses(self, actor_id: int) -> list[Business]:
        self.require_access(actor_id)
        return [b for b in self.repo.list_businesses() if b.owner_id == self.settings.owner_id and b.archived
                and self.repo.business_role(self.settings.owner_id, actor_id, b.id)]

    def company_team(self, actor_id: int, business_id: str) -> list[dict]:
        if self.require_access(actor_id) != 'owner':
            raise Forbidden('Командой управляет владелец пространства.')
        self._business(actor_id, business_id, write=False)
        return self.repo.company_access(self.settings.owner_id, business_id)

    @actor_action
    def set_company_access(self, actor_id: int, business_id: str, member_id: int, role: str, scope: str):
        self.company_team(actor_id, business_id)
        self.repo.set_company_access(self.settings.owner_id, member_id, business_id, role, scope, self.now())
        return {'saved': True}

    @actor_action
    def invite_company_member(self, actor_id: int, business_id: str, role: str):
        self.company_team(actor_id, business_id)
        invite = self.repo.create_access_invite(self.settings.owner_id, role, 1, self.now()+timedelta(hours=24),
                                                self.now(), business_id=business_id)
        return {'start_parameter': 'join_'+invite['token'], 'expires_at': invite['expires_at'], 'role': role}

    @actor_action
    def archive_business(self, actor_id: int, business_id: str, archived: bool, confirmation: str) -> Business:
        if self.require_access(actor_id, write=True) != "owner":
            raise Forbidden("Архивом компаний управляет владелец пространства.")
        business = self._business(actor_id, business_id, write=False)
        if confirmation.strip() != business.name:
            raise InvalidInput("Введите точное название компании для подтверждения.")
        return self.repo.archive_business(business_id, archived, self.now())

    def capabilities(self, actor_id: int) -> dict:
        """Loaded configuration only; credentials, balance and provider access are never probed."""
        self.require_access(actor_id)
        from app.ai.provider import UnconfiguredAI
        from app.media.runtime import UnconfiguredImages

        def state(provider, unconfigured):
            provider = unwrap(provider)
            if provider is None or isinstance(provider, unconfigured):
                return "unconfigured"
            return "demo" if getattr(provider, "is_demo", False) else "configured_unverified"

        def unwrap(provider):
            for _ in range(4):
                child = getattr(provider, "provider", getattr(provider, "_provider", provider))
                if child is provider:
                    break
                provider = child
            return provider

        return {
            "manual_profile": True, "manual_drafts": True,
            "text_generation": state(self.ai, UnconfiguredAI),
            "image_generation": state(self.images, UnconfiguredImages),
            "image_local": bool(getattr(unwrap(self.images), "_local_flux", False)),
            "image_model": str(getattr(unwrap(self.images), "_model", "")),
            "external_access_checked": False,
            "image_upload": {"formats": ["image/png", "image/jpeg"],
                             "max_bytes": min(self.settings.max_upload_bytes, 10_000_000),
                             "max_dimension": 4096, "max_pixels": 16_777_216},
        }

    def accept_telegram_update(self, actor_id: int, event_key: str, digest: str) -> bool:
        """Persist a receipt before routing, including for non-AI user actions.

        This is at-most-once admission, separate from AI quota. After a process
        interruption the owner can inspect state and send a new explicit action.
        """
        self.require_access(actor_id)
        return self.repo.claim_inbound_event(actor_id, event_key, digest, self.now())

    @actor_action
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

    @actor_action
    def add_text_source(
        self, actor_id: int, business_id: str, text: str, name: str = "Описание владельца"
    ) -> Source:
        self._business(actor_id, business_id)
        if not text.strip() or len(text.encode("utf-8")) > self.settings.max_upload_bytes:
            raise InvalidInput("Пустой или слишком большой источник.")
        guard_input(text, source="document")
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

    @actor_action
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

    @actor_action
    def add_docx_source(self, actor_id: int, business_id: str, filename: str, content: bytes) -> Source:
        from app.ai.documents import DOCX_MIME, extract_docx_text

        self._business(actor_id, business_id)
        text = extract_docx_text(content)
        guard_input(text, source='document')
        self._check_source_limits(business_id, extra_bytes=len(content))
        asset = self.files.write(business_id, filename, content, DOCX_MIME, self.now())
        self.repo.add_asset(asset)
        source = Source(id=str(uuid4()), business_id=business_id, kind='docx',
                        name=filename[:200], asset_id=asset.id, created_at=self.now())
        self.repo.add_source(source)
        return source

    @actor_action
    def add_url_source(self, actor_id: int, business_id: str, url: str) -> Source:
        self._business(actor_id, business_id)
        url = url.strip()
        # This boundary does not fetch URLs. Role 1 must revalidate DNS + every redirect at fetch time.
        try:
            if any(ord(char) < 32 or ord(char) == 127 for char in url):
                raise ValueError
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
        self._business(actor_id, business_id, write=False)
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

    @actor_action
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
        self._business(actor_id, business_id)  # The actor may have been revoked during inference.
        return self.repo.set_profile(
            business_id, profile, self.now(), expected_version=business.profile_version
        )

    @actor_action
    def save_profile(
        self, actor_id: int, business_id: str, expected_version: int, profile: BusinessProfile
    ) -> Business:
        self._business(actor_id, business_id)
        try:
            profile = BusinessProfile.model_validate(profile)
        except ValidationError:
            raise InvalidInput("Профиль бизнеса не соответствует общей схеме.") from None
        return self.repo.set_profile(business_id, profile, self.now(), expected_version=expected_version)

    @actor_action
    def confirm_profile(self, actor_id: int, business_id: str, expected_version: int) -> Business:
        self._business(actor_id, business_id)
        return self.repo.confirm_profile(business_id, expected_version, self.now())

    @actor_action
    def save_manual_profile(
        self, actor_id: int, business_id: str, expected_version: int, *, description: str,
        products: list[str] | None = None, audience: str = "", tone: str = "Дружелюбный, конкретный",
        constraints: list[str] | None = None,
    ) -> Business:
        business = self._business(actor_id, business_id)
        if type(expected_version) is not int or expected_version < 0:
            raise InvalidInput("Укажите актуальную версию профиля.")
        if business.profile_version != expected_version:
            raise Conflict("Версия профиля изменилась. Откройте актуальный профиль.")
        description = self._plain_text(description, 2000, "Опишите бизнес: от 1 до 2000 символов.").strip()
        products = [] if products is None else products
        constraints = [] if constraints is None else constraints
        for items, maximum in ((products, 19), (constraints, 20)):
            if not isinstance(items, list) or len(items) > maximum:
                raise InvalidInput("Допускается до 19 продуктов и до 20 правил бренда.")
            for value in items:
                self._plain_text(value, 500, "Каждый пункт должен содержать от 1 до 500 символов.")
        if not isinstance(audience, str) or len(audience) > 1000:
            raise InvalidInput("Описание аудитории должно быть до 1000 символов.")
        self._plain_text(tone, 400, "Опишите стиль: от 1 до 400 символов.")
        payload = {"description": description, "products": products, "audience": audience,
                   "tone": tone, "constraints": constraints}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        try:
            size = len(encoded.encode("utf-8"))
        except UnicodeEncodeError:
            raise InvalidInput("Проверьте текст профиля.") from None
        if size > self.settings.max_upload_bytes:
            raise InvalidInput("Профиль превышает допустимый размер источника.")
        self._check_source_limits(business.id, extra_bytes=size)
        now = self.now()
        source = Source(id=str(uuid4()), business_id=business.id, kind="text",
                        name=f"Сведения владельца · профиль {expected_version + 1}", text=encoded, created_at=now)
        profile = BusinessProfile(
            name=business.name, description=description, products=products, audience=audience, tone=tone,
            constraints=constraints,
            facts=[Fact(text=description, source_ids=[source.id]),
                   *[Fact(text=item, source_ids=[source.id]) for item in products]],
        )
        return self.repo.set_manual_profile(business.id, expected_version, profile, source, actor_id, now)

    def _ready(self, business: Business, *, require_channel: bool = True) -> None:
        if not business.profile or not business.profile_confirmed:
            raise Conflict("Сначала подтвердите актуальный профиль бизнеса.")
        if require_channel and not business.channel_id:
            raise Conflict("Сначала подключите канал.")

    def conversation_history(self, actor_id: int, business_id: str, limit: int = 20) -> list[dict]:
        self._business(actor_id, business_id, write=False)
        return self.repo.conversation_history(business_id, limit)

    def _conversation_context(self, business: Business, post_id: str | None) -> dict:
        """Only this company's bounded data can reach the language model.

        Conversation entries are evidence of a dialogue, never authority to
        approve/publish. Live job/post state overrides past assistant prose.
        """
        profile = business.profile
        history = self.repo.conversation_history(business.id, limit=8)
        # Quarantine old poisoned memory without deleting it or blocking every future question.
        history = [item for item in history if not inspect_input(
            item["user_text"][:1200] + "\n" + item["assistant_text"][:1200], source="document",
        )]
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

    @actor_action
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
        guard_input(text)
        if post_id and self._post(actor_id, post_id).business_id != business_id:
            raise Forbidden("Пост относится к другому бизнесу.")

        # Intent is a suggestion only; there is deliberately no 'approve/publish' model action.
        async def interpret():
            context = self._conversation_context(business, post_id)
            context["knowledge"] = await self.search_knowledge(actor_id, business_id, text)
            intent = self._provider_result(
                TaskIntent,
                await self._external(
                    self.ai.interpret_request(text, context)
                ),
            )
            if intent.post_id and intent.post_id != post_id:
                raise InvalidInput("Выберите пост явно перед изменением.")
            if intent.action == "answer" and not intent.answer.strip():
                raise ProviderUnavailable("AI вернул пустой ответ. Уточните вопрос.")
            if intent.action == "answer" and context["knowledge"]:
                names = list(dict.fromkeys(hit["source_name"] for hit in context["knowledge"]))[:3]
                evidence = "\n\nИсточники для проверки: " + "; ".join(name[:160] for name in names)
                if len(intent.answer) + len(evidence) <= 4000:
                    intent = intent.model_copy(update={"answer": intent.answer + evidence})
            if any(not question.strip() or len(question) > 1000 for question in intent.questions):
                raise ProviderUnavailable("AI вернул некорректные уточняющие вопросы.")
            for value in [intent.answer, intent.request, *intent.questions]:
                guard_public_output(value)
            self._business(actor_id, business_id)
            return intent

        result = await self._ai_operation(
            business,
            "interpret",
            {"text": text, "post_id": post_id},
            TaskIntent,
            interpret,
            request_id=request_id,
            dialogue_text=text,
        )
        # Replayed durable results must respect the current output policy as well.
        for value in [result.answer, result.request, *result.questions]:
            guard_public_output(value)
        return result

    @actor_action
    def request_preparation(self, actor_id: int, business_id: str, request: str, request_id: str) -> Job:
        business = self._business(actor_id, business_id)
        guard_input(request)
        request = self._plain_text(request, 4000, "Укажите запрос длиной до 4000 символов.").strip()
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise InvalidInput("Укажите запрос и стабильный идентификатор действия.")
        key = f"manual:{business_id}:{request_id}"
        existing = self.repo.find_job(business_id, key)
        if existing is not None:
            if existing.request != request:
                raise Conflict("Ключ задания уже использован для другого запроса.")
            return existing
        self._ready(business, require_channel=False)
        # Same Telegram update/callback ID -> same durable job, including after restart.
        return self.repo.enqueue_job(
            business_id,
            request,
            key,
            self.now(),
            max_pending=self.settings.max_pending_jobs,
            max_per_day=self.settings.max_generation_jobs_per_day,
            requested_by=actor_id,
        )

    async def prepare_with_tools(self, actor_id: int, business_id: str, request: str, request_id: str) -> Job:
        from app.mcp_tools import ToolDispatcher

        tool = ToolDispatcher(self, actor_id, business_ids={business_id}, allow_prepare=True)
        result = await tool.call("studio_request_preparation", {
            "business_id": business_id, "request": request, "request_id": request_id,
        })
        return self.get_job(actor_id, result["job"]["id"])

    def get_job(self, actor_id: int, job_id: str) -> Job:
        self.require_access(actor_id)
        job = self.repo.get_job(job_id)
        self._business(actor_id, job.business_id, write=False)
        return job

    def list_jobs(self, actor_id: int, business_id: str) -> list[Job]:
        self._business(actor_id, business_id, write=False)
        return self.repo.list_jobs(business_id)

    @actor_action
    def cancel_job(self, actor_id: int, job_id: str) -> Job:
        job = self.get_job(actor_id, job_id)
        self._business(actor_id, job.business_id)
        return self.repo.cancel_job(job_id, self.now())

    @actor_action
    def set_publications_paused(self, actor_id: int, business_id: str, paused: bool) -> Business:
        self._business(actor_id, business_id)
        if type(paused) is not bool:
            raise InvalidInput("Признак паузы должен быть логическим значением.")
        return self.repo.set_publications_paused(business_id, paused, self.now())

    def ai_usage(self, actor_id: int) -> dict:
        self.require_access(actor_id)
        return {
            **self.repo.ai_usage(self.settings.owner_id, self.now()),
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
        self.require_access(actor_id)
        if business_id is not None:
            self._business(actor_id, business_id, write=False)
        entries = self.repo.list_ai_operations(self.settings.owner_id, business_id, limit)
        return [row for row in entries if self.repo.business_role(self.settings.owner_id, actor_id, row['business_id'])]

    def failed_notifications(self, actor_id: int, business_id: str, limit: int = 20) -> list[dict]:
        self._business(actor_id, business_id, write=False)
        return self.repo.list_failed_notifications(business_id, limit=limit)

    def get_failed_notification(self, actor_id: int, notification_id: str) -> dict:
        self.require_access(actor_id)
        notification = self.repo.get_failed_notification(notification_id)
        self._business(actor_id, notification["business_id"], write=False)
        return notification

    @actor_action
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
        self._business(actor_id, business_id, write=False)
        return self.repo.list_posts(business_id)

    def get_post(self, actor_id: int, post_id: str) -> PostView:
        return self._post(actor_id, post_id, write=False)

    def read_asset(self, actor_id: int, asset_id: str) -> tuple[Asset, bytes]:
        self.require_access(actor_id)
        asset = self.repo.get_asset(asset_id)
        self._business(actor_id, asset.business_id, write=False)
        return asset, self.files.read(asset)

    @actor_action
    def upload_post_image(self, actor_id: int, business_id: str, filename: str, content: bytes) -> Asset:
        self._business(actor_id, business_id)
        maximum = min(self.settings.max_upload_bytes, 10_000_000)
        if not isinstance(content, bytes) or not 1 <= len(content) <= maximum:
            raise InvalidInput("Изображение пустое или превышает лимит загрузки.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content), formats=["PNG", "JPEG"]) as picture:
                    width, height = picture.size
                    image_format = picture.format
                    if (image_format not in {"PNG", "JPEG"} or not 1 <= width <= 4096 or not 1 <= height <= 4096
                            or width * height > 16_777_216 or max(width, height) > 20 * min(width, height)
                            or getattr(picture, "n_frames", 1) != 1):
                        raise InvalidInput("Нужно статичное изображение до 4096×4096 с отношением сторон не более 20:1.")
                    picture.verify()
                with Image.open(io.BytesIO(content), formats=["PNG", "JPEG"]) as picture:
                    picture.load()
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
                Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise InvalidInput("Файл должен быть исправным PNG или JPEG.") from None
        mime_type = "image/png" if image_format == "PNG" else "image/jpeg"
        self.safety.check_image(content, mime_type)
        self._business(actor_id, business_id)
        asset = self.files.write(business_id, filename, content, mime_type, self.now(),
                                 is_demo=self.settings.mode == "demo")
        self.repo.add_asset(asset)
        return asset

    @actor_action
    def create_manual_draft(
        self, actor_id: int, business_id: str, text: str, image_asset_id: str | None, request_id: str,
        *, scheduled_at: datetime | None = None, source_ids: list[str] | None = None,
    ) -> PostView:
        business = self._business(actor_id, business_id)
        text = self._validate_caption(text)
        self._plain_text(request_id, 100, "Укажите идентификатор черновика до 100 символов.")
        if image_asset_id is not None:
            self._plain_text(image_asset_id, 100, "Загрузите изображение для поста.")
        source_ids = [] if source_ids is None else source_ids
        if (not isinstance(source_ids, list) or len(source_ids) > 100
                or any(not isinstance(item, str) or not item or len(item) > 200 for item in source_ids)):
            raise InvalidInput("Проверьте выбранные источники поста.")
        scheduled_at = as_utc(scheduled_at) if scheduled_at is not None else None
        payload = {"text": text, "image_asset_id": image_asset_id, "source_ids": sorted(set(source_ids)),
                   "scheduled_at": scheduled_at.isoformat() if scheduled_at else None}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        existing = self.repo.find_manual_post(business_id, request_id, digest)
        if existing is not None:
            return existing
        self._ready(business, require_channel=False)
        if scheduled_at is not None and scheduled_at <= self.now():
            raise InvalidInput("Выберите время публикации в будущем.")
        asset = self.repo.get_asset(image_asset_id) if image_asset_id is not None else None
        if asset and (asset.business_id != business_id or asset.mime_type not in {"image/png", "image/jpeg"}):
            raise InvalidInput("Изображение должно принадлежать этому бизнесу.")
        if self.settings.mode == "live" and asset and asset.is_demo:
            raise InvalidInput("Демонстрационное изображение нельзя использовать в live-режиме.")
        if asset:
            self.files.read(asset)
        self.safety.check_text([text])
        self._business(actor_id, business_id)
        return self.repo.create_posts(
            business_id, [{"text": text, "image_asset_id": asset.id if asset else None, "channel_id": business.channel_id or '',
                           "scheduled_at": scheduled_at, "profile_version": business.profile_version,
                           "is_demo": self.settings.mode == "demo", "used_source_ids": source_ids,
                           "warnings": ["Создано вручную: AI не проверял текст и изображение."]}], self.now(),
            expected_profile_version=business.profile_version, expected_channel_id=business.channel_id,
            manual_request_id=request_id, manual_payload_hash=digest, manual_actor_id=actor_id,
            notification=self._notification(business, "draft_ready", "Ручной пост сохранён и ожидает согласования."),
        )[0]

    @actor_action
    def request_visual(self, actor_id: int, business_id: str, request: str, request_id: str,
                       *, caption: str | None = None) -> Job:
        business = self._business(actor_id, business_id)
        request = self._plain_text(request, 4000, 'Опишите изображение.').strip()
        self._plain_text(request_id, 100, 'Укажите идентификатор запроса.')
        guard_input(request)
        if caption is not None:
            self._validate_caption(caption)
            guard_input(caption)
        return self.repo.enqueue_job(business_id, request, f'visual:{actor_id}:{request_id}', self.now(),
                                     kind='visual', requested_by=actor_id, caption=caption,
                                     profile_version=business.profile_version,
                                     max_pending=self.settings.max_pending_jobs,
                                     max_per_day=self.settings.max_generation_jobs_per_day)

    @actor_action
    async def generate_visual(self, actor_id: int, business_id: str, request: str, request_id: str,
                              *, caption: str | None = None, job_id: str | None = None) -> VisualDraft:
        business = self._business(actor_id, business_id)
        self._plain_text(request, 4000, "Опишите изображение: от 1 до 4000 символов.")
        guard_input(request)
        if caption is not None:
            self._validate_caption(caption)
            guard_input(caption)
        await asyncio.to_thread(self.safety.check_text, [request] if caption is None else [request, caption])
        profile = business.profile or BusinessProfile(name=business.name, description="")

        def check_context():
            if job_id and self.repo.get_job(job_id).status != 'running':
                raise Conflict('Создание изображения отменено. Уже начатый расчёт может завершиться без сохранения результата.')
            current = self._business(actor_id, business_id)
            if current.profile_version != business.profile_version:
                raise Conflict("Профиль изменился во время генерации. Повторите запрос.")

        async def generate():
            # The text model derives a scene from the complete user's idea and
            # business style. FLUX receives that scene, never an invented fallback.
            check_context()
            if job_id:
                self.repo.set_job_progress(job_id, 'text', 0, 2, self.now())
            item = PlanItem(topic=request[:300], goal="Создать изображение по идее пользователя и краткую подпись.",
                            brief=request, image_brief=request)
            draft = self._provider_result(TextDraft, await self._external(self.ai.write_post(profile, item)))
            if caption is not None:
                draft = draft.model_copy(update={"text": caption})
            check_context()
            if job_id:
                self.repo.set_job_progress(job_id, 'image', 1, 2, self.now())
            image = await self._external(self.images.generate(profile, draft))
            check_context()
            asset = self._store_image(business_id, image)
            return VisualDraft(asset=asset, draft=draft, request=request, profile_version=business.profile_version)

        result = await self._ai_operation(
            business, "visual", {"request": request, "caption": caption, "profile_version": business.profile_version},
            VisualDraft, generate, request_id=request_id,
        )
        self._business(actor_id, business_id)
        return result

    def list_visuals(self, actor_id: int, business_id: str, *, archived: bool = False) -> list[VisualDraft]:
        self._business(actor_id, business_id, write=False)
        return [VisualDraft.model_validate(row) for row in self.repo.list_visual_results(business_id, archived=archived)]

    @actor_action
    def archive_asset(self, actor_id: int, asset_id: str, archived: bool) -> Asset:
        asset = self.repo.get_asset(asset_id)
        self._business(actor_id, asset.business_id)
        return self.repo.archive_asset(asset_id, archived, actor_id, self.now())

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
        guard_public_output(text)
        if len(text.encode("utf-16-le")) // 2 > 1024:
            raise InvalidInput(message)
        return text

    @actor_action
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
        remove_image: bool = False,
    ) -> PostView:
        post = self._post(actor_id, post_id)
        business = self._business(actor_id, post.business_id)
        changes = {}
        if type(remove_image) is not bool or (remove_image and image_asset_id is not None):
            raise InvalidInput("Выберите удаление или замену изображения.")
        if remove_image:
            changes["image_asset_id"] = None
        if text is not None:
            self._ready(business, require_channel=False)
            changes["text"] = self._validate_caption(text)
            self.safety.check_text([changes["text"]])
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
            if self.settings.mode == "live" and asset.is_demo:
                raise InvalidInput("Демонстрационное изображение нельзя использовать в live-режиме.")
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
        self._post(actor_id, post_id)
        return self.repo.revise_post(post_id, expected_revision, changes, self.now())

    @actor_action
    def refresh_post_context(self, actor_id: int, post_id: str, expected_revision: int) -> PostView:
        """Explicit human review against the current profile; never an approval."""
        post = self._post(actor_id, post_id)
        business = self._business(actor_id, post.business_id)
        self._ready(business, require_channel=False)
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
        self._ready(business, require_channel=False)
        if type(expected_revision) is not int or post.revision != expected_revision:
            raise Conflict("Откройте актуальную версию поста.")
        if post.status not in {
            PostStatus.REVIEW,
            PostStatus.APPROVED,
            PostStatus.REJECTED,
            PostStatus.FAILED,
        }:
            raise Conflict("Пост уже изменяется, отправляется или отправлен.")
        if post.channel_id != (business.channel_id or ''):
            raise Conflict("Сначала обновите канал в предпросмотре поста.")
        self._plain_text(feedback, 4000, "Укажите правку длиной до 4000 символов.")
        guard_input(feedback)
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

    @actor_action
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
        guard_input(feedback)
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
            self._business(actor_id, business.id)
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
            if isinstance(exc, Forbidden):
                raise
            if completed is not None:
                return completed
            if isinstance(exc, AppError):
                raise
            log.warning("Text revision failed: %s", type(exc).__name__)
            raise ProviderUnavailable("Не удалось завершить правку текста.") from None

    @actor_action
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
        guard_input(feedback)
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
            self._business(actor_id, business.id)
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
            if isinstance(exc, Forbidden):
                raise
            if completed is not None:
                return completed
            if isinstance(exc, AppError):
                raise
            log.warning("Image revision failed: %s", type(exc).__name__)
            raise ProviderUnavailable("Не удалось завершить правку изображения.") from None

    @actor_action
    def approve_post(self, actor_id: int, post_id: str, expected_revision: int) -> PostView:
        post = self._post(actor_id, post_id)
        business = self._business(actor_id, post.business_id)
        self._ready(business)
        asset = self.repo.get_asset(post.image_asset_id) if post.image_asset_id is not None else None
        if asset and asset.business_id != business.id:
            raise InvalidInput("Некорректное изображение.")
        if asset:
            self.files.read(asset)
        self._validate_caption(post.text)
        guard_public_output(post.text)
        if self.settings.mode == "live" and (post.is_demo or (asset and asset.is_demo)):
            raise InvalidInput("Тестовый материал нельзя публиковать в live-режиме.")
        return self.repo.approve_post(post_id, expected_revision, actor_id, self.now())

    def post_revisions(self, actor_id: int, post_id: str) -> list[PostView]:
        self.get_post(actor_id, post_id)
        return self.repo.post_revisions(post_id)

    @actor_action
    def reject_post(self, actor_id: int, post_id: str, expected_revision: int) -> PostView:
        self._post(actor_id, post_id)
        return self.repo.reject_post(post_id, expected_revision, self.now())

    @actor_action
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
        guard_input(request)
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
            self._business(job.requested_by or business.owner_id, business.id)
            if job.kind == 'visual':
                if job.profile_version != business.profile_version:
                    raise Conflict('Профиль изменился после постановки изображения в очередь.')
                result = await self.generate_visual(job.requested_by, business.id, job.request,
                                                     f'visual-job:{job.id}', caption=job.caption, job_id=job.id)
                self.repo.finish_visual_job(job.id, result, self.now())
                return True
            self._ready(business, require_channel=False)
            guard_input(job.request)
            profile = await self._knowledge_profile(business, job.request)
            self.repo.set_job_progress(job.id, "plan", 0, 0, self.now())
            request = f"{job.request}\nПодготовь не более {self.settings.max_posts_per_job} публикаций."
            plan = await self._ai_operation(
                business,
                "plan",
                {"request": request, "profile_version": business.profile_version},
                ContentPlan,
                lambda: self._external(self.ai.make_plan(profile, request)),
                request_id=f"job:{job.id}:plan",
            )
            self._current_context(business, job.requested_by or business.owner_id)
            if len(plan.items) > self.settings.max_posts_per_job:
                raise InvalidInput("AI превысил лимит публикаций на одно задание.")
            self.repo.save_plan(business.id, job.id, plan, self.now())
            drafts = []
            source_ids = {source.id for source in self.repo.list_sources(business.id)}
            total = 1 + 2 * len(plan.items)
            for index, item in enumerate(plan.items):
                self._current_context(business, job.requested_by or business.owner_id)
                self.repo.set_job_progress(job.id, "text", 1 + 2 * index, total, self.now())
                text = await self._ai_operation(
                    business,
                    "text",
                    {"item": item.model_dump(mode="json"), "profile_version": business.profile_version},
                    TextDraft,
                    lambda: self._external(self.ai.write_post(profile, item)),
                    request_id=f"job:{job.id}:text:{index}",
                )
                self._current_context(business, job.requested_by or business.owner_id)
                self._validate_caption(text.text)
                if set(text.used_source_ids) - source_ids:
                    raise InvalidInput("Текст ссылается на неизвестные источники.")
                self.repo.set_job_progress(job.id, "image", 2 + 2 * index, total, self.now())

                async def generate_image():
                    generated = await self._external(self.images.generate(business.profile, text))
                    self._current_context(business, job.requested_by or business.owner_id)
                    return self._store_image(business.id, generated)

                asset = await self._ai_operation(
                    business,
                    "image",
                    {"text": text.model_dump(mode="json"), "profile_version": business.profile_version},
                    Asset,
                    generate_image,
                    request_id=f"job:{job.id}:image:{index}",
                )
                self._current_context(business, job.requested_by or business.owner_id)
                proposed = as_utc(item.suggested_at) if item.suggested_at else None
                drafts.append(
                    {
                        "text": text.text,
                        "image_asset_id": asset.id,
                        "channel_id": business.channel_id or '',
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
            asset = self.repo.get_asset(post.image_asset_id) if post.image_asset_id is not None else None
            if asset and asset.business_id != business.id:
                raise InvalidInput("Изображение относится к другому бизнесу.")
            image = self.files.read(asset) if asset else None
            if self.settings.mode == "live" and (post.is_demo or (asset and asset.is_demo)):
                raise InvalidInput("Публикация тестового материала запрещена.")
            guard_public_output(post.text)
            await asyncio.to_thread(self.safety.check_text, [post.text])
            if asset:
                await asyncio.to_thread(self.safety.check_image, image, asset.mime_type)
            try:
                current = self._current_context(business)
            except Conflict:
                self._fail_publication(
                    business, post,
                    "Профиль или канал изменились во время проверки. Обновите пост и согласуйте его заново.",
                    False,
                )
                return True
            if current.publications_paused:
                self._fail_publication(
                    business, post,
                    "Публикации приостановлены во время проверки. Выберите новое время и согласуйте пост заново.",
                    False,
                )
                return True
            if not self.repo.approval_allowed(business.owner_id, post):
                raise Forbidden("Согласование отозвано. Требуется новое согласование действующего оператора.")
            attempted = True
            # Do not wrap ambiguous network errors in a generic provider error here.
            async with asyncio.timeout(self.settings.provider_timeout_seconds):
                result = PublicationResult.model_validate(
                    await self.publisher.publish(post, image, asset.mime_type if asset else None)
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
                    ("Отправка прервана; проверьте канал перед повторением." if attempted
                     else "Проверка перед отправкой прервана. Публикация не отправлена."),
                    attempted,
                )
            except Exception as exc:
                log.warning("Publication cancellation persistence failed: %s", type(exc).__name__)
            raise
        except (SafetyBlocked, SafetyUnavailable) as exc:
            self._fail_publication(business, post, str(exc), False, exc.code)
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

    @actor_action
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
        self._business(actor_id, business_id, write=False)
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
