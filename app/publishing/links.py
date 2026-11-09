from app.shared.models import Channel


def message_link(channel: Channel, message_id: int) -> str:
    if channel.username:
        return f"https://t.me/{channel.username}/{message_id}"
    return f"https://t.me/c/{str(channel.id).removeprefix('-100')}/{message_id}"
