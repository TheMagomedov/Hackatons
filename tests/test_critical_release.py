import asyncio
from datetime import timedelta

import pytest

from app.shared.errors import Forbidden
from app.storage import Repository
from tests import test_miniapp_manual
from tests.test_miniapp import OWNER, signed_data
from tests.test_visuals import configure

manual = test_miniapp_manual.manual


def grant(ctx, actor=3003):
    invitation = ctx.repo.create_access_invite(OWNER, 'operator', 1, ctx.now[0]+timedelta(hours=1), ctx.now[0])
    ctx.service.claim_invite(actor, invitation['token'])
    return actor


async def test_company_invitation_grants_only_named_company(manual):
    other = manual.service.create_business(OWNER, 'Не доступна приглашённому')
    response = await manual.client.post(manual.prefix+'/team/invite',json={'role':'operator'})
    assert response.status == 200
    invite = await response.json()
    manual.service.claim_invite(3333, invite['start_parameter'].removeprefix('join_'))
    assert [b.id for b in manual.service.list_businesses(3333)] == [manual.business.id]
    with pytest.raises(Forbidden):
        manual.service.get_business(3333,other.id)
    manual.service.set_company_access(OWNER,manual.business.id,3333,'none','selected')
    with pytest.raises(Forbidden):
        manual.service.claim_invite(3333, invite['start_parameter'].removeprefix('join_'))


async def test_company_scope_enforced_for_lists_direct_reads_writes_and_owner_admin(manual):
    actor = grant(manual)
    other = manual.service.create_business(OWNER, 'Другая компания')
    manual.service.set_company_access(OWNER, manual.business.id, actor, 'reviewer', 'selected')
    manual.client.session.headers['X-Telegram-Init-Data'] = signed_data(manual.now[0], actor=actor)
    state = await (await manual.client.get('/mini/api/state')).json()
    assert [b['id'] for b in state['businesses']] == [manual.business.id]
    assert state['access'] == {'role': 'reviewer', 'can_write': False}
    assert (await manual.client.get('/mini/api/state?business='+other.id)).status == 403
    assert (await manual.client.post(manual.prefix+'/source', json={'kind':'text','value':'Описание'})).status == 403
    assert (await manual.client.get(manual.prefix+'/team')).status == 403
    job = manual.repo.enqueue_job(manual.business.id, 'Текст', 'queue', manual.now[0])
    assert (await manual.client.post('/mini/api/job/'+job.id+'/cancel', json={})).status == 403
    manual.service.set_company_access(OWNER, manual.business.id, actor, 'operator', 'selected')
    assert (await manual.client.post('/mini/api/job/'+job.id+'/cancel', json={})).status == 200
    new = manual.service.create_business(actor, 'Компания участника')
    assert manual.service.get_business(actor, new.id).id == new.id
    with pytest.raises(Forbidden):
        manual.service.get_business(actor, other.id)


async def test_visual_queue_durable_dedup_cancel_and_recovery(manual):
    configure(manual)
    payload = {'request':'Стеклянный дом','caption':'Наш новый пост','request_id':'queue-one'}
    first = await manual.client.post(manual.prefix+'/visual-jobs', json=payload)
    assert first.status == 202
    job = await first.json()
    assert job['status'] == 'pending' and job['kind'] == 'visual'
    assert (await (await manual.client.post(manual.prefix+'/visual-jobs', json=payload)).json())['id'] == job['id']
    assert (await manual.client.post(manual.prefix+'/visual-jobs', json={**payload,'caption':'Иной текст'})).status == 409
    assert manual.service.ai.write_post.await_count == 0
    reopened = Repository(manual.repo.db_path)
    try:
        manual.service.repo = reopened
        manual.service.recover_interrupted()
        assert reopened.get_job(job['id']).status == 'pending'
        assert await manual.service.prepare_next_job()
        result = reopened.get_job(job['id'])
        assert result.status == 'done' and result.visual_result.draft.text == payload['caption']
        assert len(manual.service.list_visuals(OWNER, manual.business.id)) == 1
        assert not await manual.service.prepare_next_job()
        assert manual.service.images.generate.await_count == 1
    finally:
        manual.service.repo = manual.repo
        reopened.close()
    second = manual.service.request_visual(OWNER, manual.business.id, 'Другое стекло', 'cancel')
    retry_route='/mini/api/job/'+second.id+'/retry-visual'
    assert (await manual.client.post(retry_route,json={'request_id':'explicit-retry'})).status == 409
    manual.service.cancel_job(OWNER, second.id)
    assert not await manual.service.prepare_next_job()
    response=await manual.client.post(retry_route,json={'request_id':'explicit-retry'})
    assert response.status == 202
    retry=await response.json()
    assert retry['id'] != second.id and retry['status'] == 'pending'
    assert (await (await manual.client.post(retry_route,json={'request_id':'explicit-retry'})).json())['id'] == retry['id']
    assert await manual.service.prepare_next_job()
    assert manual.repo.get_job(retry['id']).status == 'done'


async def test_queued_visual_rechecks_membership_before_any_provider_call(manual):
    configure(manual)
    actor = grant(manual)
    job = manual.service.request_visual(actor, manual.business.id, 'Стекло', 'denied')
    manual.service.set_company_access(OWNER, manual.business.id, actor, 'none', 'selected')
    assert await manual.service.prepare_next_job()
    assert manual.repo.get_job(job.id).status == 'failed'
    assert manual.service.ai.write_post.await_count == manual.service.images.generate.await_count == 0


async def test_completed_visual_operation_is_recovered_without_generating_again(manual):
    configure(manual)
    job = manual.service.request_visual(OWNER, manual.business.id, 'Дом', 'finished-before-crash')
    manual.repo.claim_job(manual.now[0])
    visual = await manual.service.generate_visual(OWNER, manual.business.id, job.request, f'visual-job:{job.id}')
    manual.service.recover_interrupted()
    recovered = manual.repo.get_job(job.id)
    assert recovered.status == 'done' and recovered.visual_result.asset.id == visual.asset.id
    assert manual.service.images.generate.await_count == 1
    assert not await manual.service.prepare_next_job()


async def test_running_visual_cancel_does_not_store_late_result(manual):
    configure(manual)
    job = manual.service.request_visual(OWNER,manual.business.id,'Стекло','cancel-running')
    entered, release = asyncio.Event(), asyncio.Event()
    original = manual.service.images.generate

    async def slow_image(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    manual.service.images.generate=slow_image
    task=asyncio.create_task(manual.service.prepare_next_job())
    await asyncio.wait_for(entered.wait(),3)
    manual.service.cancel_job(OWNER,job.id)
    release.set()
    await task
    assert manual.repo.get_job(job.id).status == 'cancelled'
    assert manual.service.list_visuals(OWNER,manual.business.id) == []
    assert not await manual.service.prepare_next_job()


async def test_form_autosave_is_actor_scoped_compare_and_swap_and_cleared(manual):
    route = manual.prefix+'/form-draft/manual-post'
    assert (await (await manual.client.get(route)).json()) == {'revision':0,'data':None}
    saved = await manual.client.post(route,json={'revision':0,'data':{'text':'Не потерять этот текст'}})
    assert saved.status == 200
    assert (await manual.client.post(route,json={'revision':0,'data':{'text':'Другое окно'}})).status == 409
    actor = grant(manual)
    manual.client.session.headers['X-Telegram-Init-Data'] = signed_data(manual.now[0],actor=actor)
    assert (await (await manual.client.get(route)).json())['data'] is None
    manual.client.session.headers['X-Telegram-Init-Data'] = signed_data(manual.now[0])
    assert (await (await manual.client.get(route)).json())['data']['text'] == 'Не потерять этот текст'
    assert (await manual.client.post(route,json={'revision':1,'data':None})).status == 200
    assert (await (await manual.client.get(route)).json()) == {'revision':2,'data':None}
    assert (await manual.client.post(route,json={'revision':1,'data':{'text':'Старый запрос'}})).status == 409


async def test_draft_before_channel_then_explicit_destination_and_approval(manual):
    s=manual.service
    b=s.save_manual_profile(OWNER, manual.business.id, 0, description='Стекло', products=['Окна'],audience='Архитекторы',tone='Деловой',constraints=[])
    s.confirm_profile(OWNER,b.id,b.profile_version)
    asset=await (await test_miniapp_manual.upload(manual)).json()
    response=await test_miniapp_manual.draft(manual,asset['id'],scheduled_at=(manual.now[0]+timedelta(minutes=3)).isoformat())
    assert response.status == 200
    post=await response.json()
    assert post['channel_id'] == ''
    assert (await manual.client.post('/mini/api/post/'+post['id']+'/approve',json={'revision':post['revision']})).status == 409
    await s.connect_channel(OWNER,b.id,'-100bakery')
    response=await manual.client.post('/mini/api/post/'+post['id']+'/edit',json={'revision':post['revision'],'channel_id':'-100bakery'})
    assert response.status == 200
    updated=await response.json()
    assert updated['revision'] == post['revision']+1
    assert (await manual.client.post('/mini/api/post/'+post['id']+'/approve',json={'revision':updated['revision']})).status == 200
    assert not manual.publisher.calls
