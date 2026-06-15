import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import Message

from bot.config import get_settings
from bot.db.database import Database
from bot.services.context import ContextBuilder
from bot.services.moderation import ModerationService
from bot.utils.access import can_use_ai_quota, get_chat_owner_for_processing

logger = logging.getLogger(__name__)


def current_slot_start(ts: float, interval: int) -> int:
    """UTC wall-clock slot start (interval must divide 60: 15, 30, 60)."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    minute_start = int(dt.replace(second=0, microsecond=0).timestamp())
    slot_offset = (dt.second // interval) * interval
    return minute_start + slot_offset


@dataclass
class ChatBatchState:
    messages: list[Message] = field(default_factory=list)
    open_slot: int | None = None
    owner_id: int | None = None


class BatchProcessor:
    """Collects group messages and flushes them on fixed wall-clock slots."""

    def __init__(
        self,
        db: Database,
        moderation: ModerationService,
        context_builder: ContextBuilder,
    ) -> None:
        self.db = db
        self.moderation = moderation
        self.context_builder = context_builder
        self.settings = get_settings()
        self._states: dict[int, ChatBatchState] = defaultdict(ChatBatchState)
        self._history: dict[int, list[Message]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._bot: Bot | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._flush_tasks: set[asyncio.Task] = set()

    def start(self, bot: Bot) -> None:
        self._bot = bot
        if self._scheduler_task is None:
            self._scheduler_task = asyncio.create_task(self._slot_scheduler())

    async def stop(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None
        if self._flush_tasks:
            await asyncio.gather(*self._flush_tasks, return_exceptions=True)
            self._flush_tasks.clear()

    def store_history(self, chat_id: int, message: Message) -> None:
        history = self._history[chat_id]
        history.append(message)
        if len(history) > 200:
            self._history[chat_id] = history[-200:]

    def get_history(self, chat_id: int) -> list[Message]:
        return list(self._history.get(chat_id, []))

    async def enqueue(self, bot: Bot, message: Message) -> None:
        self._bot = bot
        chat_id = message.chat.id
        settings = await self.db.get_chat_settings(chat_id)
        if not settings.get("moderation_enabled", 1):
            return
        if not message.text and not message.caption:
            return

        owner_id = await get_chat_owner_for_processing(self.db, chat_id)
        allowed, reason = await can_use_ai_quota(self.db, owner_id)
        if not allowed:
            logger.warning("Skipping moderation chat=%s: %s", chat_id, reason)
            return

        interval = int(settings.get("batch_interval", self.settings.default_batch_interval))
        logger.info(
            "Queued moderation chat=%s msg=%s interval=%ss slot_mode=%s",
            chat_id,
            message.message_id,
            interval,
            interval > 0,
        )

        if interval == 0:
            history = list(self._history.get(chat_id, []))
            await self._process_batch(bot, chat_id, [message], history, owner_id)
            return

        now = time.time()
        slot = current_slot_start(now, interval)
        async with self._lock:
            state = self._states[chat_id]
            if not state.messages:
                state.open_slot = slot
            state.messages.append(message)
            state.owner_id = owner_id

        await self._maybe_flush_chat(chat_id, now)

    async def _slot_scheduler(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            if not self._bot:
                continue
            now = time.time()
            async with self._lock:
                chat_ids = list(self._states.keys())
            for chat_id in chat_ids:
                try:
                    await self._maybe_flush_chat(chat_id, now)
                except Exception:
                    logger.exception("Slot flush check failed chat=%s", chat_id)

    async def _maybe_flush_chat(self, chat_id: int, now: float) -> None:
        settings = await self.db.get_chat_settings(chat_id)
        interval = int(settings.get("batch_interval", self.settings.default_batch_interval))
        if interval <= 0:
            return

        slot = current_slot_start(now, interval)
        async with self._lock:
            state = self._states.get(chat_id)
            if not state or not state.messages or state.open_slot is None:
                return
            if slot == state.open_slot:
                return
            msgs = state.messages[:]
            hist = list(self._history.get(chat_id, []))
            owner_id = state.owner_id
            state.messages.clear()
            state.open_slot = None

        logger.info(
            "Flushing moderation slot chat=%s slot=%s messages=%s",
            chat_id,
            slot,
            len(msgs),
        )

        if owner_id is None:
            owner_id = await get_chat_owner_for_processing(self.db, chat_id)

        bot = self._bot
        if not bot:
            return

        task = asyncio.create_task(self._process_batch(bot, chat_id, msgs, hist, owner_id))
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    async def _process_batch(
        self,
        bot: Bot,
        chat_id: int,
        messages: list[Message],
        history: list[Message],
        owner_id: int,
    ) -> None:
        if not messages:
            return

        settings = await self.db.get_chat_settings(chat_id)
        rules = settings.get("rules_text", "")
        max_batch = self.settings.batch_max_messages

        ordered = sorted(messages, key=lambda m: m.message_id)
        chunks = [ordered[i : i + max_batch] for i in range(0, len(ordered), max_batch)]

        logger.info(
            "Processing moderation batch chat=%s messages=%s chunks=%s rules_len=%s",
            chat_id,
            len(ordered),
            len(chunks),
            len(rules or ""),
        )

        for chunk in chunks:
            allowed, reason = await can_use_ai_quota(self.db, owner_id)
            if not allowed:
                logger.warning("Quota exhausted for chat=%s: %s", chat_id, reason)
                break
            try:
                if len(chunk) == 1:
                    msg = chunk[0]
                    ctx = self.context_builder.build(msg, history)
                    decision = await self.moderation.analyze(
                        chat_id,
                        rules,
                        msg.message_id,
                        ctx,
                        admin_user_id=owner_id,
                    )
                    await self.moderation.apply_decision(
                        bot, chat_id, decision, msg.message_id, target_message=msg
                    )
                    continue

                ctx = self.context_builder.build_batch(chunk, history)
                target_ids = [m.message_id for m in chunk]
                decisions = await self.moderation.analyze_batch(
                    chat_id,
                    rules,
                    target_ids,
                    ctx,
                    admin_user_id=owner_id,
                )
                msg_by_id = {m.message_id: m for m in chunk}
                mapped = self.moderation.map_batch_decisions(decisions, chunk)
                for msg_id in sorted(msg_by_id):
                    msg = msg_by_id[msg_id]
                    decision = mapped.get(msg_id)
                    if not decision:
                        logger.warning(
                            "Batch missing decision chat=%s msg=%s, fallback to single analyze",
                            chat_id,
                            msg_id,
                        )
                        ctx_single = self.context_builder.build(msg, history)
                        decision = await self.moderation.analyze(
                            chat_id,
                            rules,
                            msg_id,
                            ctx_single,
                            admin_user_id=owner_id,
                        )
                    decision = self.moderation.enrich_decision(decision, msg)
                    await self.moderation.apply_decision(
                        bot,
                        chat_id,
                        decision,
                        msg_id,
                        target_message=msg,
                    )
            except Exception:
                logger.exception(
                    "Moderation batch failed for chat=%s chunk_size=%s",
                    chat_id,
                    len(chunk),
                )
