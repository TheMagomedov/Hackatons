import asyncio
import sqlite3
from datetime import timedelta
from unittest.mock import AsyncMock

from app.bot.adapter import TelegramBotAdapter
from app.ops.backup import backup_data, restore_data
from app.shared.errors import ProviderUnavailable
from app.vk.store import VKStore
from tests import test_miniapp_manual
from tests.ops import test_backup
from tests.test_miniapp import OWNER, signed_data

manual = test_miniapp_manual.manual
data = test_backup.data


async def test_archive_requires_confirmation_preserves_data_and_restores_paused(manual):
    route = manual.prefix + '/archive'
    assert (await manual.client.post(route, json={'archived': True, 'confirmation': 'wrong'})).status == 400
    result = await manual.client.post(route, json={'archived': True, 'confirmation': manual.business.name})
    assert result.status == 200
    assert manual.repo.get_business(manual.business.id).archived
    assert manual.service.list_businesses(OWNER) == []
    assert (await (await manual.client.get('/mini/api/business-archive')).json())['items'][0]['id'] == manual.business.id
    assert (await manual.client.post(manual.prefix+'/source', json={'kind':'text','value':'Описание'})).status == 409
    result = await manual.client.post(route, json={'archived': False, 'confirmation': manual.business.name})
    assert result.status == 200
    assert manual.service.list_businesses(OWNER)[0].publications_paused


async def test_archive_rejects_operator_and_pending_generation(manual):
    invitation = manual.repo.create_access_invite(OWNER, 'operator', 1, manual.now[0]+timedelta(hours=1), manual.now[0])
    manual.service.claim_invite(3004, invitation['token'])
    manual.client.session.headers['X-Telegram-Init-Data'] = signed_data(manual.now[0], actor=3004)
    payload = {'archived':True,'confirmation':manual.business.name}
    assert (await manual.client.post(manual.prefix+'/archive',json=payload)).status == 403
    manual.client.session.headers['X-Telegram-Init-Data'] = signed_data(manual.now[0])
    manual.repo.enqueue_job(manual.business.id, 'Пост', 'queue-check', manual.now[0])
    assert (await manual.client.post(manual.prefix+'/archive',json=payload)).status == 409
    assert not manual.repo.get_business(manual.business.id).archived


def test_backup_preserves_vk_links_but_restore_revokes_tickets_and_pending_inbox(data, tmp_path):
    source, _, _ = data
    vk = VKStore(source/'vk.sqlite3', 123)
    with vk.db:
        vk.db.execute('INSERT INTO links(user_id,actor) VALUES(?,?)',(11,22))
        vk.db.execute("INSERT INTO inbox(event_key,user_id,text,created) VALUES('event',11,'private request',1)")
    vk.issue('session',22,11)
    vk.close()
    for name in ('knowledge.sqlite3','ai-runtime.sqlite3'):
        with sqlite3.connect(source/name) as c:
            c.execute('CREATE TABLE evidence(value TEXT)')
            c.execute("INSERT INTO evidence VALUES('preserved')")
    backup_data(source,tmp_path/'snapshot')
    restore_data(tmp_path/'snapshot',tmp_path/'restored')
    restored = VKStore(tmp_path/'restored/vk.sqlite3',123)
    assert restored.actor(11) == 22
    assert restored.db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0] == 0
    assert tuple(restored.db.execute('SELECT state,text FROM inbox').fetchone()) == ('interrupted','')
    restored.close()
    for name in ('knowledge.sqlite3','ai-runtime.sqlite3'):
        with sqlite3.connect(tmp_path/'restored'/name) as c:
            assert c.execute('SELECT value FROM evidence').fetchone()[0] == 'preserved'


async def test_telegram_reconnects_without_terminating_host():
    adapter = object.__new__(TelegramBotAdapter)
    stop = asyncio.Event()
    calls = 0

    async def connection(event):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderUnavailable('temporary network error')
        event.set()

    adapter._run_connected = AsyncMock(side_effect=connection)
    await asyncio.wait_for(adapter.run(stop),timeout=4)
    assert calls == 2
