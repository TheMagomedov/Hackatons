import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .shared.ports import AppError


@dataclass(frozen=True)
class Settings:
    owner_id: int
    token: str
    timezone: ZoneInfo
    data_dir: Path
    demo: bool
    api_base: str
    api_key: str
    text_model: str
    image_model: str

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        try:
            owner_id = int(os.getenv("OWNER_ID") or "0")
            timezone = ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Moscow"))
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise AppError("Проверьте OWNER_ID и часовой пояс APP_TIMEZONE.") from exc
        demo = os.getenv("DEMO_MODE", "0") == "1"
        return cls(
            owner_id=owner_id, token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            timezone=timezone, data_dir=Path(os.getenv("APP_DATA_DIR", "var")),
            demo=demo, api_base=os.getenv("AI_API_BASE", "https://api.openai.com/v1").rstrip("/"),
            api_key=os.getenv("AI_API_KEY", "").strip(),
            text_model=os.getenv("AI_TEXT_MODEL", "gpt-4.1-mini"),
            image_model=os.getenv("AI_IMAGE_MODEL", "gpt-image-1"),
        )
