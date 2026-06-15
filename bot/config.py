import json
from functools import lru_cache
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class Settings:
    def __init__(self) -> None:
        settings_path = CONFIG_DIR / "settings.json"
        secrets_path = CONFIG_DIR / "secrets.json"

        settings: dict[str, Any] = {}
        secrets: dict[str, Any] = {}

        if settings_path.exists():
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if secrets_path.exists():
            secrets = json.loads(secrets_path.read_text(encoding="utf-8"))

        self.bot_token: str = secrets.get("bot_token", "")
        self.owner_id: int = int(settings.get("owner_id", 1776244625))

        keys = secrets.get("gemini_api_keys", [])
        if isinstance(keys, str):
            keys = [keys]
        self.gemini_api_keys: list[str] = [k for k in keys if k and "YOUR_" not in k]

        self.rpm_limit: int = int(settings.get("rpm_limit", 15))
        self.rpd_per_model: int = int(settings.get("rpd_per_model", 1000))
        self.default_batch_interval: int = int(settings.get("default_batch_interval", 30))
        self.batch_max_messages: int = int(settings.get("batch_max_messages", 50))
        self.database_path: str = settings.get("database_path", "data/bot.db")
        self.gemini_models: list[str] = settings.get("gemini_models", [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite-preview",
        ])
        self.spam_ban_days: int = int(settings.get("spam_ban_days", 7))
        self.spam_threshold: int = int(settings.get("spam_threshold", 5))
        self.spam_window_seconds: int = int(settings.get("spam_window_seconds", 60))
        self.log_clean_checks: bool = bool(settings.get("log_clean_checks", False))
        self.limits_refresh_minutes: int = int(settings.get("limits_refresh_minutes", 30))

        self.proxy: dict[str, Any] = secrets.get("proxy", {})

    @property
    def proxy_url(self) -> str | None:
        from bot.utils.proxy import build_proxy_url, parse_proxy_config

        cfg = parse_proxy_config(self.proxy)
        if not cfg:
            return None
        return build_proxy_url(cfg)

    @property
    def gemini_api_key_1(self) -> str:
        return self.gemini_api_keys[0] if self.gemini_api_keys else ""

    @property
    def gemini_api_key_2(self) -> str:
        return self.gemini_api_keys[1] if len(self.gemini_api_keys) > 1 else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
