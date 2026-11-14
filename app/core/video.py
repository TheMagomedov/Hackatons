import asyncio

from app.media.video import VideoInput, VideoResult, render_video
from app.safety.input_guard import guard_input
from app.safety.privacy import guard_public_output
from app.shared.errors import InvalidInput


async def create(service, actor, business_id, body):
    business=service._business(actor,business_id)
    body=VideoInput.model_validate(body)
    service._current_context(business,actor)
    for text in [body.title,*[f.caption for f in body.frames]]:
        guard_input(text)
        guard_public_output(text)

    async def generate():
        async with service._video_lock:
            service._current_context(business,actor)
            await asyncio.to_thread(service.safety.check_text,[body.title,*[f.caption for f in body.frames]])
            frames=[]
            demo=False
            for frame in body.frames:
                asset=service.repo.get_asset(frame.asset_id)
                if asset.business_id != business_id or asset.archived or asset.mime_type not in {'image/png','image/jpeg'}:
                    raise InvalidInput('Выберите доступное изображение этой компании.')
                content=service.files.read(asset)
                await asyncio.to_thread(service.safety.check_image,content,asset.mime_type)
                demo |= asset.is_demo
                frames.append((content,frame.caption))
            content,width,height=await render_video(frames,business.name,body.seconds_per_frame,body.portrait)
            service._current_context(business,actor)
            asset=service.files.write(business_id,'video.mp4',content,'video/mp4',service.now(),is_demo=demo)
            service.repo.add_asset(asset)
            return VideoResult(asset=asset,title=body.title,duration=len(frames)*body.seconds_per_frame,
                               width=width,height=height,frames=body.frames,profile_version=business.profile_version)
    result=await service._ai_operation(business,'video_render',
        {**body.model_dump(exclude={'request_id'}),'profile_version':business.profile_version},
        VideoResult,generate,request_id=body.request_id)
    service._business(actor,business_id)
    return result
