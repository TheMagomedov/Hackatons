import io
import json
import sqlite3
import zipfile
from datetime import timedelta

import httpx
import pytest
from aiohttp import FormData

from app.ai.documents import extract_docx_text
from app.ai.sources import extract_source
from app.publishing import TelegramPublisher
from app.shared.errors import DeliveryUnknown, InvalidInput
from app.shared.models import PostStatus, SourceInput
from app.storage import Repository
from tests import test_miniapp_manual
from tests.publishing.test_telegram import CHANNEL, TOKEN, post, response, success_message
from tests.test_miniapp import OWNER

manual = test_miniapp_manual.manual


async def test_text_only_create_approve_publish_and_revision_history(manual):
    await test_miniapp_manual.setup_profile(manual)
    result = await test_miniapp_manual.draft(manual, None,
        scheduled_at=(manual.now[0]+timedelta(minutes=1)).isoformat())
    assert result.status == 200
    item = await result.json()
    assert item['image_asset_id'] is None and not manual.publisher.calls
    base = '/mini/api/post/'+item['id']
    assert (await manual.client.post(base+'/approve',json={'revision':1})).status == 200
    result = await manual.client.post(base+'/edit',json={'revision':1,'text':'Обновлённый текст'})
    updated = await result.json()
    assert updated['revision'] == 2 and updated['approved_revision'] is None
    versions = await (await manual.client.get(base+'/revisions')).json()
    assert [r['revision'] for r in versions['items']] == [2,1]
    assert (await manual.client.post(base+'/approve',json={'revision':1})).status == 409
    assert (await manual.client.post(base+'/approve',json={'revision':2})).status == 200
    manual.now[0] += timedelta(minutes=1)
    assert await manual.service.publish_due_once()
    assert manual.repo.get_post(item['id']).status == PostStatus.PUBLISHED
    assert len(manual.publisher.calls) == 1
    assert not await manual.service.publish_due_once()


async def test_remove_image_revokes_approval_and_keeps_original_revision(manual):
    await test_miniapp_manual.setup_profile(manual)
    asset = await (await test_miniapp_manual.upload(manual)).json()
    item = await (await test_miniapp_manual.draft(manual,asset['id'],
        scheduled_at=(manual.now[0]+timedelta(minutes=2)).isoformat())).json()
    manual.service.approve_post(OWNER,item['id'],1)
    changed = manual.service.edit_post(OWNER,item['id'],1,remove_image=True)
    assert changed.image_asset_id is None and changed.approved_by is None
    assert manual.repo.post_revisions(item['id'])[-1].image_asset_id == asset['id']


@pytest.mark.parametrize('timeout',[False,True])
async def test_telegram_text_delivery_uses_message_once_and_handles_ambiguity(timeout):
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.url.path.endswith('/sendMessage')
        assert json.loads(request.content) == {'chat_id':CHANNEL,'text':'A useful post',
                                               'link_preview_options':{'is_disabled':True}}
        if timeout:
            raise httpx.ReadTimeout('private response',request=request)
        return response(success_message())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher=TelegramPublisher(TOKEN,client)
        try:
            if timeout:
                with pytest.raises(DeliveryUnknown):
                    await publisher.publish(post(image_asset_id=None),None,None)
            else:
                assert (await publisher.publish(post(image_asset_id=None),None,None)).message_id == 12
        finally:
            await publisher.close()
    assert len(calls) == 1


async def test_schema_rebuild_preserves_posts_revision_foreign_keys_and_receipts(manual):
    await test_miniapp_manual.setup_profile(manual)
    asset=await (await test_miniapp_manual.upload(manual)).json()
    item=await (await test_miniapp_manual.draft(manual,asset['id'])).json()
    with sqlite3.connect(manual.repo.db_path) as conn:
        before=conn.execute('SELECT data FROM post_revisions').fetchall()
        conn.execute('PRAGMA user_version=6')
    reopened=Repository(manual.repo.db_path)
    reopened.initialize()
    assert reopened.get_post(item['id']).image_asset_id == asset['id']
    with reopened._reader() as conn:
        assert conn.execute('SELECT data FROM post_revisions').fetchall()[0][0] == before[0][0]
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(manual.repo.db_path) as conn:
            conn.execute('DELETE FROM post_revisions')
    assert (await (await test_miniapp_manual.draft(manual,asset['id'])).json())['id'] == item['id']


def docx(xml):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as archive:
        archive.writestr('[Content_Types].xml','<Types/>')
        archive.writestr('word/document.xml',xml)
    return out.getvalue()


async def test_docx_upload_and_local_extraction_keep_source_provenance(manual):
    content=docx('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>Фирменный цвет 196C7F</w:t></w:r></w:p></w:body></w:document>')
    data=FormData()
    data.add_field('file',content,filename='Регламент.docx')
    result=await manual.client.post(manual.prefix+'/pdf',data=data)
    assert result.status == 200
    saved=await result.json()
    source=manual.repo.get_source(saved['id']) if hasattr(manual.repo,'get_source') else manual.repo.list_sources(manual.business.id)[0]
    assert source.kind == 'docx' and source.asset_id
    extracted=await extract_source(SourceInput(source=source,content=content))
    assert extracted.id == source.id and '196C7F' in extracted.text


@pytest.mark.parametrize('xml',[
    '<!DOCTYPE x [<!ENTITY x "boom">]><x>&x;</x>',
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
    '<invalid',
])
def test_docx_rejects_unsafe_empty_and_broken_documents(xml):
    with pytest.raises(InvalidInput):
        extract_docx_text(docx(xml))


async def test_extended_form_autosave_and_gallery_archive(manual):
    from tests.test_visuals import configure

    route = manual.prefix+'/form-draft/manual-profile'
    fields = {'description':'Настоящее описание', 'audience':'Архитекторы', 'products':'Листовое стекло'}
    assert (await manual.client.post(route,json={'revision':0,'data':{'fields':fields,'context_revision':3}})).status == 200
    saved = await (await manual.client.get(route)).json()
    assert saved['data']['fields'] == fields and saved['data']['context_revision'] == 3
    assert (await manual.client.post(route,json={'revision':1,'data':{'fields':{'unknown':'x'}}})).status == 400
    configure(manual)
    visual = await manual.service.generate_visual(OWNER,manual.business.id,'Стекло','gallery-archive')
    route = '/mini/api/asset/'+visual.asset.id+'/archive'
    assert (await manual.client.post(route,json={'archived':True})).status == 200
    assert not manual.service.list_visuals(OWNER,manual.business.id)
    assert manual.service.list_visuals(OWNER,manual.business.id,archived=True)[0].asset.archived
    assert manual.service.read_asset(OWNER,visual.asset.id)[1], 'Archiving must not break existing posts'
    assert (await manual.client.post(route,json={'archived':False})).status == 200
    assert len(manual.service.list_visuals(OWNER,manual.business.id)) == 1
