from datetime import timedelta

import pytest

from app.shared.errors import Conflict
from tests import test_miniapp_manual
from tests.test_critical_release import grant
from tests.test_miniapp import OWNER, signed_data

manual = test_miniapp_manual.manual
BASE = '/mini/integration/v1'


async def issue(ctx, scopes=None):
    result = await ctx.client.post(ctx.prefix+'/api-keys', json={
        'name': 'Интеграция', 'days': 1, 'scopes': scopes or ['read', 'generate']})
    assert result.status == 200
    return await result.json()


async def test_api_scope_company_dedup_and_expiry(manual):
    await test_miniapp_manual.setup_profile(manual)
    key = await issue(manual)
    headers = {'Authorization': 'Bearer '+key['token']}
    assert (await manual.client.get(BASE+'/me')).status == 403
    result = await manual.client.get(BASE+'/me', headers=headers)
    assert (await result.json())['company']['id'] == manual.business.id
    payload = {'request': 'Стеклянный фасад', 'request_id': 'a'*100}
    first = await manual.client.post(BASE+'/images', json=payload, headers=headers)
    assert first.status == 202
    job = await first.json()
    assert (await (await manual.client.post(BASE+'/images', json=payload, headers=headers)).json())['id'] == job['id']
    assert (await manual.client.post(BASE+'/images', json={**payload,'request':'Другой запрос'}, headers=headers)).status == 409
    assert (await manual.client.get(BASE+'/jobs/'+job['id'], headers=headers)).status == 200
    other = manual.service.create_business(OWNER,'Чужая компания')
    other_job = manual.repo.enqueue_job(other.id,'Описание','other',manual.now[0])
    assert (await manual.client.get(BASE+'/jobs/'+other_job.id, headers=headers)).status == 403
    asset = await (await test_miniapp_manual.upload(manual, business_id=other.id)).json()
    assert (await manual.client.get(BASE+'/assets/'+asset['id'], headers=headers)).status == 403
    assert (await manual.client.post(BASE+'/prepare',json={'request':'Новость','request_id':'post'},headers=headers)).status == 202
    assert (await manual.client.post(BASE+'/approve',json={},headers=headers)).status == 404
    manual.now[0] += timedelta(days=1)
    assert (await manual.client.get(BASE+'/me',headers=headers)).status == 403


async def test_key_is_hash_only_owner_only_and_revocable(manual):
    key = await issue(manual, ['read'])
    listing = await (await manual.client.get(manual.prefix+'/api-keys')).json()
    assert key['token'] not in str(listing) and 'token_hash' not in str(listing)
    with manual.repo._reader() as conn:
        row = dict(conn.execute('SELECT * FROM api_keys').fetchone())
        assert key['token'] not in str(row)
    headers = {'Authorization':'Bearer '+key['token']}
    assert (await manual.client.post(BASE+'/images',json={'request':'Дом','request_id':'a'},headers=headers)).status == 403
    actor = grant(manual)
    manual.client.session.headers['X-Telegram-Init-Data']=signed_data(manual.now[0],actor=actor)
    assert (await manual.client.get(manual.prefix+'/api-keys')).status == 403
    manual.client.session.headers['X-Telegram-Init-Data']=signed_data(manual.now[0])
    assert (await manual.client.post(manual.prefix+'/api-keys/'+key['id']+'/revoke',json={})).status == 200
    assert (await manual.client.get(BASE+'/me',headers=headers)).status == 403


async def test_rate_limit_and_archived_company(manual):
    key=await issue(manual)
    for _ in range(60):
        manual.repo.authenticate_api_key(key['token'],'read',manual.now[0])
    with pytest.raises(Conflict):
        manual.repo.authenticate_api_key(key['token'],'read',manual.now[0])
    manual.now[0]+=timedelta(minutes=1)
    assert manual.repo.authenticate_api_key(key['token'],'read',manual.now[0])
    manual.service.archive_business(OWNER,manual.business.id,True,manual.business.name)
    assert (await manual.client.get(BASE+'/me',headers={'Authorization':'Bearer '+key['token']})).status == 403


async def test_company_limit_is_atomic_and_distinct_from_owner_budget(manual):
    result = await manual.client.post(manual.prefix+'/ai-limit', json={'limit': 1})
    assert result.status == 200 and (await result.json())['daily_limit'] == 1
    repo, now = manual.repo, manual.now[0]
    first = repo.begin_ai_operation(OWNER,manual.business.id,'interpret','first','a'*64,now,
                                    daily_limit=100,max_parallel=10)
    with pytest.raises(Conflict,match='компании'):
        repo.begin_ai_operation(OWNER,manual.business.id,'interpret','second','b'*64,now,
                                daily_limit=100,max_parallel=10)
    assert repo.begin_ai_operation(OWNER,manual.business.id,'interpret','first','a'*64,now,
                                   daily_limit=100,max_parallel=10)['id'] == first['id']
    other = manual.service.create_business(OWNER,'Другая компания')
    assert repo.begin_ai_operation(OWNER,other.id,'interpret','other','c'*64,now,daily_limit=100,max_parallel=10)
    assert (await (await manual.client.get(manual.prefix+'/ai-limit')).json())['used_today'] == 1
