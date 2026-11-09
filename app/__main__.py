import argparse
import asyncio
import logging

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.token import TokenValidationError

from .ai.client import ModelAI, Provider
from .ai.fixture import DEMO_NOTES, FixtureAI
from .bot.router import build_router
from .config import Settings
from .core.service import Marketing
from .demo import run_demo
from .media.generator import FixtureImages, ModelImages
from .publishing.telegram import TelegramNotifier, TelegramPublisher
from .scheduler.worker import Worker
from .shared.models import ExtractedSource, Source
from .shared.ports import AppError
from .storage.db import Database


async def probe_ai(settings: Settings, probe_image: bool) -> None:
    if not settings.api_key:
        raise AppError("Для проверки провайдера заполните AI_API_KEY.")
    from datetime import datetime

    async with aiohttp.ClientSession() as session:
        provider = Provider(session, settings.api_base, settings.api_key)
        ai = ModelAI(provider, settings.text_model)
        source = Source(id=1, kind="notes", label="Тестовая кофейня")
        profile = await ai.build_profile([ExtractedSource(source=source, text=DEMO_NOTES)])
        plan = await ai.make_plan(profile, "Завтраки и кофе", 3, datetime.now(settings.timezone))
        draft = await ai.write_post(profile, plan.items[0])
        print(f"AI: {profile.name}. Тем: {len(plan.items)}. Тестовый пост:\n{draft.text}")
        if probe_image:
            generated = await ModelImages(provider, settings.image_model).generate(profile, plan.items[0], draft)
            database = Database(settings.data_dir / "probe", 1)
            asset = await asyncio.to_thread(database.save_asset, generated.data, generated.format)
            print(f"AI-изображение: {database.asset_path(asset)}")


async def run(settings: Settings) -> None:
    if settings.owner_id <= 0 or not settings.token:
        raise AppError("Для запуска бота заполните OWNER_ID и TELEGRAM_BOT_TOKEN в .env.")
    if not settings.demo and not settings.api_key:
        raise AppError("Заполните AI_API_KEY или включите DEMO_MODE=1 для тестового контента.")
    database = Database(settings.data_dir, settings.owner_id)
    async with Bot(settings.token) as bot, aiohttp.ClientSession() as session:
        try:
            await bot.get_me(request_timeout=15)
        except TelegramAPIError as exc:
            raise AppError("Telegram не подтвердил подключение. Проверьте токен и доступ к сети.") from exc
        provider = Provider(session, settings.api_base, settings.api_key)
        ai = FixtureAI() if settings.demo else ModelAI(provider, settings.text_model)
        images = FixtureImages() if settings.demo else ModelImages(provider, settings.image_model)
        app = Marketing(database, ai, images, TelegramPublisher(bot, settings.owner_id),
                        settings.owner_id, settings.timezone)
        worker = Worker(app, TelegramNotifier(bot))
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(app, worker, settings.demo))
        task = asyncio.create_task(worker.run())
        try:
            await dispatcher.start_polling(
                bot, allowed_updates=dispatcher.resolve_used_update_types(), close_bot_session=False,
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Автономный AI-маркетолог")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Проверить загрузку конфигурации")
    mode.add_argument("--demo", action="store_true", help="Пройти сценарий локально без ключей")
    mode.add_argument("--probe-ai", action="store_true", help="Проверить текстовый AI API")
    mode.add_argument("--probe-image", action="store_true", help="Проверить текстовый и графический AI API")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # The polling library can include request details in transport errors.
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    try:
        settings = Settings.load()
        if args.check:
            print("Настройки и общие контракты загружены.")
        elif args.demo:
            asyncio.run(run_demo(settings.timezone))
        elif args.probe_ai or args.probe_image:
            asyncio.run(probe_ai(settings, args.probe_image))
        else:
            asyncio.run(run(settings))
    except AppError as exc:
        parser.exit(1, f"{exc}\n")
    except TokenValidationError:
        parser.exit(1, "TELEGRAM_BOT_TOKEN имеет неверный формат.\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
