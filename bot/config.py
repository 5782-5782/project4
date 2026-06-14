from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    owner_id: int = 1776244625

    gemini_api_key_1: str
    gemini_api_key_2: str = ""

    rpm_limit: int = 15
    rpd_per_model: int = 1000
    default_batch_interval: int = 30

    database_path: str = "data/bot.db"

    # Models from smartest to simplest (free-tier friendly IDs)
    gemini_models: list[str] = [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-3-flash-preview",
        "gemini-3.1-flash-lite-preview",
    ]

    spam_ban_days: int = 7
    spam_threshold: int = 5
    spam_window_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
