import asyncio
import json
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from app.media.video import render_video
from tests import test_miniapp_manual
from tests.test_miniapp import OWNER

manual = test_miniapp_manual.manual


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='Local FFmpeg required')
async def test_real_mp4_is_playable_and_has_expected_duration(tmp_path):
    content,width,height=await render_video([(test_miniapp_manual.image_bytes(),'Стекло и свет')]*2,
                                            'САЛАВАТСТЕКЛО',2,True)
    target=tmp_path/'video.mp4'
    target.write_bytes(content)
    data=json.loads(subprocess.check_output([shutil.which('ffprobe'),'-v','error','-show_format','-show_streams',
                                            '-of','json',str(target)]))
    stream=data['streams'][0]
    assert stream['codec_name']=='h264' and (stream['width'],stream['height'])==(width,height)==(720,1280)
    assert abs(float(data['format']['duration'])-4)<0.1
    assert len(content)<10_000_000


async def test_video_receipt_scope_and_gallery(manual,monkeypatch):
    await test_miniapp_manual.setup_profile(manual)
    asset=await (await test_miniapp_manual.upload(manual)).json()
    render=AsyncMock(return_value=(b'test-mp4',720,1280))
    monkeypatch.setattr('app.core.video.render_video',render)
    payload={'title':'Ролик','frames':[{'asset_id':asset['id'],'caption':'Свет'}],'request_id':'video-1'}
    response=await manual.client.post(manual.prefix+'/videos',json=payload)
    assert response.status==200,await response.text()
    result=await response.json()
    assert result['asset']['mime_type']=='video/mp4'
    assert (await manual.client.post(manual.prefix+'/videos',json=payload)).status==200
    assert render.await_count==1
    gallery=await (await manual.client.get(manual.prefix+'/videos')).json()
    assert len(gallery['items'])==1
    video=await manual.client.get('/mini/api/video/'+result['asset']['id'])
    assert video.status==200 and await video.read()==b'test-mp4'
    assert (await manual.client.get('/mini/api/asset/'+result['asset']['id'])).status==404
    other=manual.service.create_business(OWNER,'Вторая')
    assert manual.service.videos(OWNER,other.id)==[]
    assert not manual.publisher.calls


async def test_foreign_image_is_blocked_before_renderer(manual,monkeypatch):
    await test_miniapp_manual.setup_profile(manual)
    other=manual.service.create_business(OWNER,'Другая')
    asset=await (await test_miniapp_manual.upload(manual,business_id=other.id)).json()
    render=AsyncMock()
    monkeypatch.setattr('app.core.video.render_video',render)
    response=await manual.client.post(manual.prefix+'/videos',json={
        'title':'Ролик','frames':[{'asset_id':asset['id']}],'request_id':'video'})
    assert response.status==400 and not render.called


async def test_ffmpeg_cancel_kills_process_before_temp_cleanup(monkeypatch):
    process=AsyncMock()
    process.returncode=None
    process.kill=lambda:setattr(process,'returncode',-9)
    started=asyncio.Event()
    async def wait():
        if process.returncode is None:
            started.set()
            await asyncio.Event().wait()
        return process.returncode
    process.wait=wait
    monkeypatch.setattr('app.media.video.shutil.which',lambda _: 'ffmpeg')
    monkeypatch.setattr('app.media.video.asyncio.create_subprocess_exec',AsyncMock(return_value=process))
    task=asyncio.create_task(render_video([(test_miniapp_manual.image_bytes(),'Тест')],'Компания',2,False))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode==-9
