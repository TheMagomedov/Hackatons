from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.ai.sources import _public_url
from app.safety.privacy import guard_public_output, redact_text
from app.shared.campaigns import CampaignInput, LeadInput
from app.shared.errors import InvalidInput


def create_campaign(service, actor, business_id, body):
    service._business(actor, business_id)
    body = CampaignInput.model_validate(body)
    url = str(_public_url(body.url))
    if redact_text(url) != url:
        raise InvalidInput('Не используйте секреты или личные данные в ссылке.')
    guard_public_output(body.name)
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith('utm_')]
    query.extend([('utm_source',body.source),('utm_medium',body.medium),('utm_campaign',body.campaign)])
    tracked = urlunsplit(parts._replace(query=urlencode(query)))
    return service.repo.save_campaign_item(business_id,actor,'campaigns',
        {**body.model_dump(exclude={'request_id'}),'tracked_url':tracked},body.request_id,service.now())


def create_lead(service, actor, business_id, body, origin='manual'):
    service._business(actor, business_id)
    body = LeadInput.model_validate(body)
    guard_public_output(body.title)
    guard_public_output(body.note)
    return service.repo.save_campaign_item(business_id,actor,'leads',
        {**body.model_dump(exclude={'request_id'}),'origin':origin},body.request_id,service.now())


def report(service, actor, business_id, offset=0):
    service._business(actor, business_id, write=False)
    if type(offset) is not int or not 0 <= offset <= 5000:
        raise InvalidInput('Недопустимая страница обращений.')
    campaigns = service.repo.campaign_items(business_id,'campaigns')
    leads = service.repo.campaign_items(business_id,'leads')
    def counts(items):
        won = [item for item in items if item['stage']=='won']
        return {'total':len(items),'new':sum(i['stage']=='new' for i in items),
                'qualified':sum(i['stage']=='qualified' for i in items),'won':len(won),
                'lost':sum(i['stage']=='lost' for i in items),'amount_kopecks':sum(i['amount_kopecks'] for i in won)}
    return {'campaigns':[dict(c,metrics=counts([lead for lead in leads if lead['campaign_id']==c['id']])) for c in campaigns],
            'leads':leads[offset:offset+100],'total':counts(leads),
            'unattributed':counts([lead for lead in leads if not lead['campaign_id']]),
            'offset':offset,'more':offset+100<len(leads),
            'leads_total':len(leads),'currency':'RUB','attribution':'explicit','clicks':None}
