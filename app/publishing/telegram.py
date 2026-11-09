from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import BufferedInputFile

from app.shared.models import Channel, PostView, PublicationResult
from app.shared.ports import AppError, DeliveryUnknown

from .links import message_link


class TelegramPublisher:
    def __init__(self, bot: Bot, owner_id: int):
        self.bot = bot
        self.owner_id = owner_id

    async def check_channel(self, channel: int | str) -> Channel:
        try:
            chat = await self.bot.get_chat(channel)
            if chat.type != "channel":
                raise AppError("Укажите Telegram-канал.")
            bot_member = await self.bot.get_chat_member(chat.id, self.bot.id)
            owner_member = await self.bot.get_chat_member(chat.id, self.owner_id)
        except TelegramAPIError as exc:
            raise AppError("Канал недоступен. Проверьте ID и права администратора бота.") from exc
        for member, label in ((bot_member, "бота"), (owner_member, "владельца бизнеса")):
            if member.status != "creator" and (
                member.status != "administrator" or not getattr(member, "can_post_messages", False)
            ):
                raise AppError(f"Проверьте права {label} на публикацию в этом канале.")
        return Channel(id=chat.id, title=chat.title or str(chat.id), username=chat.username)

    async def publish(self, post: PostView, image: bytes) -> PublicationResult:
        try:
            message = await self.bot.send_photo(
                post.channel.id, BufferedInputFile(image, filename=post.image_id),
                caption=post.draft.text, request_timeout=25,
            )
        except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
            raise AppError("Telegram отклонил публикацию. Проверьте права, фото и ограничения канала.") from exc
        except (TelegramNetworkError, TelegramAPIError, TimeoutError, OSError) as exc:
            raise DeliveryUnknown("Результат отправки неизвестен. Откройте канал и проверьте пост.") from exc
        return PublicationResult(
            message_id=message.message_id, link=message_link(post.channel, message.message_id),
            published_at=message.date,
        )

    async def subscriber_count(self, channel: Channel) -> int:
        return await self.bot.get_chat_member_count(channel.id)


class TelegramNotifier:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def notify(self, owner_id: int, message: str) -> None:
        await self.bot.send_message(owner_id, message, request_timeout=10)
