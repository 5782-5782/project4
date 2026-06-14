import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from bot.db.database import Database
from bot.services.batch import BatchProcessor
from bot.services.gemini import GeminiService
from bot.services.moderation import ModerationService


class ServicesMiddleware(BaseMiddleware):
    def __init__(
        self,
        db: Database,
        gemini: GeminiService,
        moderation: ModerationService,
        batch_processor: BatchProcessor,
    ) -> None:
        self.db = db
        self.gemini = gemini
        self.moderation = moderation
        self.batch_processor = batch_processor

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["db"] = self.db
        data["gemini"] = self.gemini
        data["moderation"] = self.moderation
        data["batch_processor"] = self.batch_processor
        return await handler(event, data)
