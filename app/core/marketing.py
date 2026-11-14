"""Create a bounded, attributable document inside one durable AI operation."""

import hashlib

from app.ai.sources import _public_url, fetch_public_page
from app.safety.input_guard import guard_input, guard_payload
from app.safety.privacy import guard_public_output, redact_text
from app.shared.errors import Conflict, InvalidInput, ProviderUnavailable
from app.shared.marketing import MarketingDocument, MarketingEvidence, MarketingReport


async def create_document(service, actor, business_id, kind, request, urls, request_id):
    business = service._business(actor, business_id)
    if business.profile is None or not business.profile_confirmed:
        raise Conflict('Сначала заполните и подтвердите профиль компании.')
    if kind not in {'strategy', 'competitors', 'video_script'}:
        raise InvalidInput('Выберите стратегию, сравнение конкурентов или сценарий видео.')
    service._plain_text(request, 4000, 'Опишите задачу: от 1 до 4000 символов.')
    guard_input(request)
    if not isinstance(urls, list) or len(urls) > 3 or any(not isinstance(u, str) or len(u) > 2000 for u in urls):
        raise InvalidInput('Можно сравнить до трёх публичных страниц.')
    if (kind == 'competitors' and not urls) or (kind != 'competitors' and urls):
        raise InvalidInput('Ссылки нужны только для сравнения конкурентов: от 1 до 3.')
    urls = list(dict.fromkeys(str(_public_url(url)) for url in urls))
    for url in urls:
        guard_public_output(url)
        # Do not send supplied credentials or personal contact data in a request URL.
        if redact_text(url) != url:
            raise InvalidInput('Удалите личные данные и секреты из ссылки.')
    method = getattr(service.ai, 'marketing_report', None)
    if method is None:
        raise ProviderUnavailable('Подключённый AI не поддерживает маркетинговые документы.')

    async def generate():
        profile = await service._knowledge_profile(business, request)
        service._current_context(business, actor)
        sources = {s.id: s for s in service.repo.list_sources(business_id)}
        evidence = []
        for sid in sorted({sid for fact in profile.facts for sid in fact.source_ids}):
            if sid not in sources:
                raise Conflict('Источники профиля изменились. Пересоберите профиль.')
            source = sources[sid]
            evidence.append(MarketingEvidence(id=sid, name=source.name, url=source.url,
                excerpt=redact_text('\n'.join(f.text for f in profile.facts if sid in f.source_ids)[:3000]),
                captured_at=service.now()))
        for url in urls:
            text = await fetch_public_page(url)
            guard_input(text)
            evidence.append(MarketingEvidence(id='web:' + hashlib.sha256(url.encode()).hexdigest()[:16],
                name=_public_url(url).host, url=url, excerpt=redact_text(text[:3000]), captured_at=service.now()))
            service._current_context(business, actor)
        guard_payload([e.model_dump(mode='json') for e in evidence])
        result = service._provider_result(MarketingReport, await service._external(method(
            profile, kind, request, [e.model_dump(mode='json') for e in evidence])))
        known = {e.id for e in evidence}
        if any(not set(section.source_ids) <= known for section in result.sections):
            raise ProviderUnavailable('AI сослался на источник, которого нет в материалах.')
        if bool(result.scenes) != (kind == 'video_script'):
            raise ProviderUnavailable('AI вернул неверный формат сценария.')
        for text in result.texts():
            guard_public_output(text)
        service._current_context(business, actor)
        if set(sources) != {s.id for s in service.repo.list_sources(business_id)}:
            raise Conflict('Источники изменились во время подготовки. Повторите запрос.')
        return MarketingDocument(kind=kind, request=request, profile_version=business.profile_version,
            created_at=service.now(), report=result, evidence=evidence)

    result = await service._ai_operation(business, 'marketing_' + kind,
        {'kind': kind, 'request': request, 'urls': urls, 'profile_version': business.profile_version},
        MarketingDocument, generate, request_id=request_id)
    service._current_context(business, actor)
    return result
