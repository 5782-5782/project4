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
from bot.services.chat_history import StoredChatMessage, from_telegram
from bot.services.context import ContextBuilder
from bot.services.moderation import ModerationService
from bot.utils.access import can_use_ai_quota, get_chat_owner_for_processing
from bot.utils.chat_roles import get_chat_roles

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

    async def store_history(self, message: Message) -> None:
        await self.db.save_chat_messages_from_telegram(message)

    async def get_history(self, chat_id: int) -> list[StoredChatMessage]:
        return await self.db.get_chat_messages(chat_id)

    async def _resolve_stored(self, chat_id: int, message: Message) -> StoredChatMessage | None:
        stored = from_telegram(message)
        if stored:
            return stored
        return await self.db.get_chat_message(chat_id, message.message_id)

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
            history = await self.get_history(chat_id)
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
            owner_id = state.owner_id
            state.messages.clear()
            state.open_slot = None

        history = await self.get_history(chat_id)

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

        task = asyncio.create_task(self._process_batch(bot, chat_id, msgs, history, owner_id))
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    async def _process_batch(
        self,
        bot: Bot,
        chat_id: int,
        messages: list[Message],
        history: list[StoredChatMessage],
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
                chat_roles = await get_chat_roles(bot, chat_id)
                pending: list[tuple[Message, StoredChatMessage]] = []
                for msg in chunk:
                    if await self.db.was_message_moderated(chat_id, msg.message_id):
                        logger.info(
                            "Skip already moderated chat=%s msg=%s",
                            chat_id,
                            msg.message_id,
                        )
                        continue
                    stored = await self._resolve_stored(chat_id, msg)
                    if not stored:
                        logger.warning("No stored message chat=%s msg=%s", chat_id, msg.message_id)
                        continue
                    pending.append((msg, stored))

                if not pending:
                    continue

                if len(pending) == 1:
                    msg, stored = pending[0]
                    ctx = self.context_builder.build(stored, history)
                    decision = await self.moderation.analyze(
                        chat_id,
                        rules,
                        msg.message_id,
                        ctx,
                        admin_user_id=owner_id,
                        chat_roles=chat_roles,
                    )
                    decision = self.moderation.enrich_decision(decision, msg, chat_roles)
                    await self.moderation.apply_decision(
                        bot, chat_id, decision, msg.message_id, target_message=msg
                    )
                    continue

                stored_chunk = [s for _, s in pending]
                ctx = self.context_builder.build_batch(stored_chunk, history)
                target_ids = [s.message_id for s in stored_chunk]
                decisions = await self.moderation.analyze_batch(
                    chat_id,
                    rules,
                    target_ids,
                    ctx,
                    admin_user_id=owner_id,
                    chat_roles=chat_roles,
                )
                msg_by_id = {m.message_id: m for m, _ in pending}
                mapped = self.moderation.map_batch_decisions(decisions, [m for m, _ in pending])
                for msg_id in sorted(msg_by_id):
                    msg = msg_by_id[msg_id]
                    decision = mapped.get(msg_id)
                    if not decision:
                        logger.warning(
                            "Batch missing decision chat=%s msg=%s, fallback to single analyze",
                            chat_id,
                            msg_id,
                        )
                        stored = await self._resolve_stored(chat_id, msg)
                        if not stored:
                            continue
                        ctx_single = self.context_builder.build(stored, history)
                        decision = await self.moderation.analyze(
                            chat_id,
                            rules,
                            msg_id,
                            ctx_single,
                            admin_user_id=owner_id,
                            chat_roles=chat_roles,
                        )
                    decision = self.moderation.enrich_decision(decision, msg, chat_roles)
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
