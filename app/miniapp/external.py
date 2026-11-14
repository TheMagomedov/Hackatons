"""Versioned integration API. Keys cannot approve or publish content."""

import hashlib

from aiohttp import web
from pydantic import Field

from app.shared.errors import Forbidden, InvalidInput
from app.shared.models import Contract


class KeyInput(Contract):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(min_length=1, max_length=2)
    days: int = Field(default=30, strict=True, ge=1, le=90)


class LimitInput(Contract):
    limit: int | None = Field(default=None, strict=True, ge=0, le=10000)


def register(app, service, context, json_body):
    from app.miniapp.server import NewJob, VisualInput

    def request_key(key, value):
        return f"api:{key['id']}:{hashlib.sha256(value.encode()).hexdigest()[:32]}"

    async def keys(request):
        svc, actor = context(request)
        business = request.match_info['id']
        svc.get_business(actor, business)
        if request.method == 'GET':
            return web.json_response({'items': svc.repo.list_api_keys(business, actor)})
        body = KeyInput.model_validate(await json_body(request))
        return web.json_response(svc.repo.issue_api_key(business, actor, body.name, body.scopes, body.days, svc.now()))

    async def revoke(request):
        svc, actor = context(request)
        business = request.match_info['id']
        svc.get_business(actor, business)
        svc.repo.revoke_api_key(business, actor, request.match_info['key'], svc.now())
        return web.json_response({'revoked': True})

    async def company_limit(request):
        svc, actor = context(request)
        business = request.match_info['id']
        svc.get_business(actor, business)
        if request.method == 'POST':
            body = LimitInput.model_validate(await json_body(request))
            svc.repo.set_company_ai_limit(business, actor, body.limit, svc.now())
        return web.json_response(svc.repo.company_ai_usage(business, svc.now()))

    def authorize(request, scope):
        if service is None:
            raise Forbidden('Интеграционный API недоступен в предпросмотре.')
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer ') or len(header) > 100:
            raise Forbidden('Передайте API-ключ в Authorization: Bearer.')
        return service.repo.authenticate_api_key(header[7:], scope, service.now())

    async def me(request):
        key = authorize(request, 'read')
        business = service.get_business(key['actor_id'], key['business_id'])
        return web.json_response({'company': {'id': business.id, 'name': business.name}, 'version': 1})

    async def images(request):
        key = authorize(request, 'generate')
        body = VisualInput.model_validate(await json_body(request))
        job = service.request_visual(key['actor_id'], key['business_id'], body.request,
            request_key(key, body.request_id), caption=body.caption)
        return web.json_response(job.model_dump(mode='json'), status=202)

    async def prepare(request):
        key = authorize(request, 'generate')
        body = NewJob.model_validate(await json_body(request))
        job = service.request_preparation(key['actor_id'], key['business_id'], body.request,
                                          request_key(key, body.request_id))
        return web.json_response(job.model_dump(mode='json'), status=202)

    async def job(request):
        key = authorize(request, 'read')
        result = service.get_job(key['actor_id'], request.match_info['id'])
        if result.business_id != key['business_id']:
            raise Forbidden('API-ключ относится к другой компании.')
        return web.json_response(result.model_dump(mode='json'))

    async def posts(request):
        key = authorize(request, 'read')
        return web.json_response({'items': [item.model_dump(mode='json') for item in
            service.list_posts(key['actor_id'], key['business_id'])[-100:]]})

    async def asset(request):
        key = authorize(request, 'read')
        item = service.repo.get_asset(request.match_info['id'])
        if item.business_id != key['business_id']:
            raise Forbidden('API-ключ относится к другой компании.')
        if item.mime_type not in {'image/png', 'image/jpeg'}:
            raise InvalidInput('По этому адресу доступны только изображения.')
        _, data = service.read_asset(key['actor_id'], item.id)
        return web.Response(body=data, content_type=item.mime_type)

    app.add_routes([
        web.get('/mini/api/business/{id}/api-keys', keys),
        web.post('/mini/api/business/{id}/api-keys', keys),
        web.post('/mini/api/business/{id}/api-keys/{key}/revoke', revoke),
        web.get('/mini/api/business/{id}/ai-limit', company_limit),
        web.post('/mini/api/business/{id}/ai-limit', company_limit),
        web.get('/mini/integration/v1/me', me), web.post('/mini/integration/v1/images', images),
        web.post('/mini/integration/v1/prepare', prepare), web.get('/mini/integration/v1/jobs/{id}', job),
        web.get('/mini/integration/v1/posts', posts), web.get('/mini/integration/v1/assets/{id}', asset),
    ])
