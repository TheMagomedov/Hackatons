"""Local, bounded MP4 assembly from approved image inputs; no remote video API."""

import asyncio
import io
import os
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import Field

from app.media.branding import ASSETS, _wrap
from app.shared.errors import InvalidInput, ProviderUnavailable
from app.shared.models import Asset, Contract


class VideoFrame(Contract):
    asset_id: str = Field(min_length=1, max_length=100)
    caption: str = Field(default='', max_length=80)


class VideoInput(Contract):
    title: str = Field(min_length=1, max_length=100)
    frames: list[VideoFrame] = Field(min_length=1, max_length=6)
    seconds_per_frame: int = Field(default=5, ge=2, le=10, strict=True)
    portrait: bool = True
    request_id: str = Field(min_length=1, max_length=100)


class VideoResult(Contract):
    asset: Asset
    title: str
    duration: int
    width: int
    height: int
    frames: list[VideoFrame]
    profile_version: int


def _frames(root, frames, company, portrait):
    width, height = (720, 1280) if portrait else (720, 720)
    for index, (content, caption) in enumerate(frames):
        canvas = Image.new('RGB', (width,height), '#DDE1E1')
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0,0,width,100),fill='#285055')
        font = ImageFont.truetype(str(ASSETS/'brand-avenir-demi.ttf'),24)
        lines = _wrap(company,font,width-64)
        if len(lines)>2:
            font = ImageFont.truetype(str(ASSETS/'brand-avenir-demi.ttf'),16)
            lines = _wrap(company,font,width-64)
        if len(lines)>2:
            raise InvalidInput('Название компании слишком длинное для титра видео.')
        for i,line in enumerate(lines):
            draw.text((32,22+i*30),line,font=font,fill='white')
        with Image.open(io.BytesIO(content),formats=['PNG','JPEG']) as original:
            if not 1 <= original.width <= 4096 or not 1 <= original.height <= 4096:
                raise InvalidInput('Слишком большое изображение для видео.')
            photo = ImageOps.contain(original.convert('RGB'),(width-32,height-270),Image.Resampling.LANCZOS)
            canvas.paste(photo,((width-photo.width)//2,110+(height-270-photo.height)//2))
        font = ImageFont.truetype(str(ASSETS/'brand-avenir-demi.ttf'),28)
        lines = _wrap(caption,font,width-64)
        if len(lines)>4:
            raise InvalidInput('Сократите подпись кадра.')
        for i,line in enumerate(lines):
            draw.text((32,height-142+i*31),line,font=font,fill='#285055')
        canvas.save(root/f'frame-{index}.png')
    return width,height


async def render_video(frames, company, seconds, portrait):
    executable = shutil.which('ffmpeg')
    if not executable:
        raise ProviderUnavailable('Для сборки видео установите FFmpeg на сервере.')
    # Callers bound input count, duration and decoded image size before this function.
    if not 1 <= len(frames) <= 6 or not 2 <= seconds <= 10:
        raise InvalidInput('Видео: от 1 до 6 кадров, от 2 до 10 секунд каждый.')
    with tempfile.TemporaryDirectory(prefix='studio-video-') as directory:
        root=Path(directory)
        worker=asyncio.create_task(asyncio.to_thread(_frames,root,frames,company,portrait))
        try:
            width,height=await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Join bounded image work before removing its private directory.
            await worker
            raise
        manifest=''.join(f"file 'frame-{i}.png'\nduration {seconds}\n" for i in range(len(frames)))
        manifest += f"file 'frame-{len(frames)-1}.png'\n"
        (root/'frames.txt').write_text(manifest,encoding='utf-8')
        output=root/'video.mp4'
        arguments=[executable,'-nostdin','-hide_banner','-loglevel','error','-protocol_whitelist','file,pipe',
                   '-f','concat','-safe','1','-i','frames.txt','-t',str(seconds*len(frames)),
                   '-an','-vf','fps=15,tpad=stop_mode=clone:stop_duration=10',
                   '-c:v','libx264','-preset','veryfast','-profile:v','baseline',
                   '-pix_fmt','yuv420p','-b:v','700k','-maxrate','900k','-bufsize','1800k',
                   '-threads','2','-movflags','+faststart',str(output)]
        process=await asyncio.create_subprocess_exec(*arguments,cwd=root,stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,creationflags=0x08000000 if os.name=='nt' else 0)
        try:
            async with asyncio.timeout(90):
                code=await process.wait()
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if code != 0 or not output.is_file() or not 1 <= output.stat().st_size <= 10_000_000:
            raise ProviderUnavailable('Не удалось собрать видео в допустимом размере.')
        return output.read_bytes(),width,height
