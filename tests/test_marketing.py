from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ai.provider import ModelAI
from app.safety.wrappers import ModeratedAI
from app.shared.marketing import MarketingReport, MarketingSection, VideoScene
from tests import test_miniapp_manual
from tests.test_miniapp import OWNER

manual = test_miniapp_manual.manual


def document(evidence, scenes=None):
    return MarketingReport(title='План на 90 дней', summary='Рекомендации требуют проверки.',
        sections=[MarketingSection(heading='Предложение', kind='observation',
            text='Компания печёт хлеб.', source_ids=[evidence[0]['id']]),
            MarketingSection(heading='Эксперимент', kind='hypothesis', text='Проверить интерес соседей.')],
        next_steps=['Подготовить две темы и согласовать.'], measurement_plan=['Считать обращения по меткам.'],
        scenes=scenes or [])


async def configured(manual):
    await test_miniapp_manual.setup_profile(manual)
    calls = []
    async def generate(profile, kind, request, evidence):
        calls.append((kind, evidence))
        return document(evidence, [VideoScene(seconds=5, visual='Хлеб на столе')] if kind == 'video_script' else [])
    manual.service.ai = SimpleNamespace(marketing_report=generate)
    return calls


async def create(manual, **changes):
    return await manual.client.post(manual.prefix+'/marketing', json={
        'kind':'strategy','request':'План на 90 дней для соседей', 'request_id':'report-1',**changes})


async def test_report_is_saved_deduplicated_and_never_published(manual):
    calls = await configured(manual)
    first = await create(manual)
    assert first.status == 200, await first.text()
    result = await first.json()
    assert result['report']['sections'][0]['source_ids'] == [result['evidence'][0]['id']]
    assert (await create(manual)).status == 200
    assert len(calls) == 1
    assert (await create(manual,request='Другой запрос')).status == 409
    items = await (await manual.client.get(manual.prefix+'/marketing')).json()
    assert len(items['items']) == 1
    assert manual.service.ai_usage(OWNER)['used_today'] == 1
    assert not manual.publisher.calls and not manual.service.list_posts(OWNER,manual.business.id)


async def test_competitor_fetch_is_inside_dedup_and_unknown_citations_fail(manual, monkeypatch):
    await configured(manual)
    fetch = AsyncMock(return_value='Конкурент предлагает булочки и кофе.')
    monkeypatch.setattr('app.core.marketing.fetch_public_page',fetch)
    for _ in range(2):
        response = await create(manual,kind='competitors',urls=['https://competitor.example'])
        assert response.status == 200, await response.text()
    assert fetch.await_count == 1
    result = await response.json()
    assert result['evidence'][-1]['url'] == 'https://competitor.example'
    assert result['evidence'][-1]['id'].startswith('web:')
    async def bad(*_):
        return document([{'id':'invented-source'}])
    manual.service.ai.marketing_report = bad
    assert (await create(manual,request_id='bad')).status == 502
    assert len(manual.service.marketing_documents(OWNER,manual.business.id)) == 1


@pytest.mark.parametrize('urls', [['http://127.0.0.1'],['file:///etc/passwd'],['https://x.example:444'],[]])
async def test_invalid_competitor_sources_never_spend_ai_quota(manual, urls):
    calls = await configured(manual)
    assert (await create(manual,kind='competitors',urls=urls)).status == 400
    assert not calls and manual.service.ai_usage(OWNER)['used_today'] == 0


async def test_profile_race_blocks_result(manual):
    await configured(manual)
    async def race(profile, kind, request, evidence):
        manual.service.save_manual_profile(OWNER, manual.business.id, 1, description='Другое описание')
        return document(evidence)
    manual.service.ai.marketing_report = race
    response = await create(manual)
    assert response.status == 409, await response.text()
    assert manual.service.marketing_documents(OWNER,manual.business.id) == []


async def test_company_scope_history_and_write(manual):
    await configured(manual)
    await create(manual)
    other = manual.service.create_business(OWNER,'Другая компания')
    assert not manual.service.marketing_documents(OWNER,other.id)
    invite = manual.repo.create_access_invite(OWNER,'reviewer',1,manual.now[0]+timedelta(days=1),manual.now[0])
    manual.service.claim_invite(900,invite['token'])
    from app.shared.errors import Forbidden
    with pytest.raises(Forbidden):
        await manual.service.create_marketing_document(900,manual.business.id,'strategy','Новый план',[],request_id='r')
    assert len(manual.service.marketing_documents(900,manual.business.id)) == 1


async def test_unconfigured_and_video_script(manual):
    await test_miniapp_manual.setup_profile(manual)
    assert (await create(manual)).status == 502
    calls=[]
    async def generate(profile,kind,request,evidence):
        calls.append(kind)
        return document(evidence,[VideoScene(seconds=5,visual='Фасад здания')])
    manual.service.ai = SimpleNamespace(marketing_report=generate)
    assert (await create(manual,kind='video_script',request_id='video')).status == 200
    assert calls == ['video_script']


async def test_model_passes_grounding_validator_and_moderates_output():
    class Client:
        async def complete(self, schema, instruction, data, validate):
            good = document(data['evidence'])
            assert validate(good)
            assert not validate(document([{'id':'fake'}]))
            return good
    policy=SimpleNamespace(require_configured=lambda:None,check_text=lambda texts:None)
    from app.shared.models import BusinessProfile
    report = await ModeratedAI(ModelAI(Client()),policy).marketing_report(
        BusinessProfile(name='Компания',description='Описание'),'strategy','План',[{'id':'fact','text':'Хлеб'}])
    assert report.title
