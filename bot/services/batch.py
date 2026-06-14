import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.types import Message

from bot.db.database import Database
from bot.services.context import ContextBuilder
from bot.services.moderation import ModerationService

logger = logging.getLogger(__name__)


@dataclass
class PendingBatch:
    messages: list[Message] = field(default_factory=list)
    history: list[Message] = field(default_factory=list)
    task: asyncio.Task | None = None


class BatchProcessor:
    """Collects group messages and processes them on interval."""

    def __init__(
        self,
        db: Database,
        moderation: ModerationService,
        context_builder: ContextBuilder,
    ) -> None:
        self.db = db
        self.moderation = moderation
        self.context_builder = context_builder
        self._batches: dict[int, PendingBatch] = defaultdict(PendingBatch)
        self._history: dict[int, list[Message]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def store_history(self, chat_id: int, message: Message) -> None:
        history = self._history[chat_id]
        history.append(message)
        if len(history) > 200:
            self._history[chat_id] = history[-200:]

    async def enqueue(self, bot: Bot, message: Message) -> None:
        chat_id = message.chat.id
        settings = await self.db.get_chat_settings(chat_id)
        if not settings.get("moderation_enabled", 1):
            return
        if not message.text and not message.caption:
            return

        interval = settings.get("batch_interval", 30)

        async with self._lock:
            batch = self._batches[chat_id]
            batch.messages.append(message)
            batch.history = list(self._history.get(chat_id, []))

            if interval == 0:
                msgs = batch.messages[:]
                hist = batch.history[:]
                batch.messages.clear()
                await self._process_messages(bot, chat_id, msgs, hist)
                return

            if batch.task and not batch.task.done():
                batch.task.cancel()
            batch.task = asyncio.create_task(self._delayed_process(bot, chat_id, interval))

    async def _delayed_process(self, bot: Bot, chat_id: int, interval: int) -> None:
        await asyncio.sleep(interval)
        async with self._lock:
            batch = self._batches[chat_id]
            if not batch.messages:
                return
            msgs = batch.messages[:]
            hist = batch.history[:]
            batch.messages.clear()
        await self._process_messages(bot, chat_id, msgs, hist)

    async def _process_messages(
        self,
        bot: Bot,
        chat_id: int,
        messages: list[Message],
        history: list[Message],
    ) -> None:
        settings = await self.db.get_chat_settings(chat_id)
        rules = settings.get("rules_text", "")

        for msg in messages:
            try:
                ctx = self.context_builder.build(msg, history)
                decision = await self.moderation.analyze(
                    chat_id, rules, msg.message_id, ctx
                )
                await self.moderation.apply_decision(bot, chat_id, decision)
            except Exception:
                logger.exception("Moderation failed for chat=%s msg=%s", chat_id, msg.message_id)
