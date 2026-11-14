from urllib.parse import parse_qs, urlsplit

import pytest

from app.shared.errors import Forbidden
from tests import test_miniapp_manual
from tests.test_miniapp import OWNER

manual = test_miniapp_manual.manual


def campaign(request_id='campaign-1'):
    return {'name':'Архитекторы','url':'https://example.com/page?x=1&utm_source=old',
            'source':'telegram','campaign':'architects','request_id':request_id}


async def test_campaign_utms_dedup_no_fake_clicks_and_lead_workflow(manual):
    response=await manual.client.post(manual.prefix+'/campaigns',json=campaign())
    assert response.status == 200
    c=await response.json()
    assert parse_qs(urlsplit(c['tracked_url']).query)=={
        'x':['1'],'utm_source':['telegram'],'utm_medium':['social'],'utm_campaign':['architects']}
    assert (await (await manual.client.post(manual.prefix+'/campaigns',json=campaign())).json())['id']==c['id']
    body={'title':'Заявка 123','campaign_id':c['id'],'request_id':'lead-1'}
    lead=await (await manual.client.post(manual.prefix+'/leads',json=body)).json()
    assert (await (await manual.client.post(manual.prefix+'/leads',json=body)).json())['id']==lead['id']
    assert (await manual.client.post(manual.prefix+'/leads',json={**body,'title':'Другой'})).status==409
    update={'revision':1,'stage':'won','amount_kopecks':125000}
    assert (await manual.client.post(manual.prefix+'/leads/'+lead['id'],json=update)).status==200
    assert (await manual.client.post(manual.prefix+'/leads/'+lead['id'],json=update)).status==409
    report=await (await manual.client.get(manual.prefix+'/campaigns')).json()
    assert report['total']['won']==1 and report['total']['amount_kopecks']==125000
    assert report['campaigns'][0]['metrics']['total']==1 and report['clicks'] is None
    assert not manual.publisher.calls and manual.service.ai_usage(OWNER)['used_today']==0


async def test_company_isolation_attribution_and_immutable_idempotency(manual):
    c=manual.service.create_campaign(OWNER,manual.business.id,campaign())
    other=manual.service.create_business(OWNER,'Другой')
    with pytest.raises(Forbidden):
        manual.service.create_lead(OWNER,other.id,{'title':'Сделка','campaign_id':c['id'],'request_id':'x'})
    lead=manual.service.create_lead(OWNER,manual.business.id,{'title':'Сделка','request_id':'x'})
    response=await manual.client.post('/mini/api/business/'+other.id+'/leads/'+lead['id'],
                                     json={'revision':1,'stage':'won','amount_kopecks':1})
    assert response.status==404
    report=manual.service.campaign_report(OWNER,manual.business.id)
    assert report['unattributed']['total']==1 and report['total']['amount_kopecks']==0


async def test_external_crm_scope_and_no_ai_or_publication_permission(manual):
    key=manual.repo.issue_api_key(manual.business.id,OWNER,'CRM',['leads','read'],1,manual.now[0])
    headers={'Authorization':'Bearer '+key['token']}
    path='/mini/integration/v1'
    body={'title':'CRM #36','request_id':'external-36'}
    lead=await (await manual.client.post(path+'/leads',json=body,headers=headers)).json()
    assert lead['origin']=='api'
    assert (await (await manual.client.post(path+'/leads',json=body,headers=headers)).json())['id']==lead['id']
    assert (await manual.client.post(path+'/images',json={'request':'Картинка','request_id':'x'},headers=headers)).status==403
    assert (await manual.client.post(path+'/leads/'+lead['id'],json={'revision':1,'stage':'qualified'},headers=headers)).status==200
    assert (await manual.client.post(path+'/leads',json=body)).status==403
    manual.repo.revoke_api_key(manual.business.id,OWNER,key['id'],manual.now[0])
    assert (await manual.client.get(path+'/campaigns',headers=headers)).status==403


@pytest.mark.parametrize('payload',[
    {'title':'','request_id':'x'}, {'title':'X','request_id':'x','amount_kopecks':100},
    {'title':'X','request_id':'x','amount_kopecks':-1,'stage':'won'},
    {'title':'X','request_id':'x','amount_kopecks':1.5,'stage':'won'},
])
async def test_invalid_revenue_never_persisted(manual,payload):
    assert (await manual.client.post(manual.prefix+'/leads',json=payload)).status==400
    assert manual.service.campaign_report(OWNER,manual.business.id)['total']['total']==0
