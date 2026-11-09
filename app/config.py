"""Explicit configuration. Offline demo is the safe default."""

import os
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.shared.errors import InvalidInput


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["demo", "live"] = "demo"
    data_dir: Path = Path("var")
    owner_id: int = Field(default=1, gt=0)
    timezone: str = "Europe/Moscow"
    telegram_bot_token: SecretStr | None = None
    ai_factory: str | None = None
    image_factory: str | None = None
    report_factory: str | None = None
    bot_factory: str | None = None
    provider_timeout_seconds: float = Field(default=120, gt=0, le=600)
    max_upload_bytes: int = Field(default=10_000_000, gt=0, le=20_000_000)
    max_posts_per_job: int = Field(default=3, ge=1, le=5)
    max_pending_jobs: int = Field(default=5, ge=1, le=50)
    max_generation_jobs_per_day: int = Field(default=10, ge=1, le=100)
    max_publish_lateness_seconds: int = Field(default=900, ge=0, le=86400)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def live_credentials(self) -> "Settings":
        if self.mode == "live":
            if not self.telegram_bot_token or not self.telegram_bot_token.get_secret_value().strip():
                raise ValueError("TELEGRAM_BOT_TOKEN is required in live mode")
            if not self.ai_factory or not self.image_factory:
                raise ValueError("AI_PROVIDER_FACTORY and IMAGE_PROVIDER_FACTORY are required in live mode")
        return self

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        load_dotenv(env_file, override=False)
        if (
            os.environ.get("APP_MODE", "demo") == "live"
            and not os.environ.get("OWNER_TELEGRAM_ID", "").strip()
        ):
            raise InvalidInput("Укажите OWNER_TELEGRAM_ID явно для live-режима.")
        names = {
            "APP_MODE": "mode",
            "DATA_DIR": "data_dir",
            "OWNER_TELEGRAM_ID": "owner_id",
            "APP_TIMEZONE": "timezone",
            "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
            "AI_PROVIDER_FACTORY": "ai_factory",
            "IMAGE_PROVIDER_FACTORY": "image_factory",
            "REPORT_PROVIDER_FACTORY": "report_factory",
            "BOT_FACTORY": "bot_factory",
            "PROVIDER_TIMEOUT_SECONDS": "provider_timeout_seconds",
            "MAX_UPLOAD_BYTES": "max_upload_bytes",
            "MAX_POSTS_PER_JOB": "max_posts_per_job",
            "MAX_PENDING_JOBS": "max_pending_jobs",
            "MAX_GENERATION_JOBS_PER_DAY": "max_generation_jobs_per_day",
            "MAX_PUBLISH_LATENESS_SECONDS": "max_publish_lateness_seconds",
        }
        return cls(
            **{field: os.environ[key] for key, field in names.items() if os.environ.get(key, "").strip()}
        )
