import logging
from dataclasses import dataclass, field

from bot.services.chat_history import StoredChatMessage

logger = logging.getLogger(__name__)


@dataclass
class ContextMessage:
    message_id: int
    user_id: int
    username: str | None
    full_name: str
    text: str
    reply_to_message_id: int | None
    is_target: bool = False


@dataclass
class ModerationContext:
    target_message: ContextMessage
    messages: list[ContextMessage] = field(default_factory=list)
    participant_ids: set[int] = field(default_factory=set)


@dataclass
class BatchModerationContext:
    target_messages: list[ContextMessage]
    messages: list[ContextMessage] = field(default_factory=list)
    participant_ids: set[int] = field(default_factory=set)


class ContextBuilder:
    """Builds moderation context from persisted chat history."""

    def __init__(self, history_limit: int = 50, reply_context_above: int = 10) -> None:
        self.history_limit = history_limit
        self.reply_context_above = reply_context_above

    def build(self, target: StoredChatMessage, history: list[StoredChatMessage]) -> ModerationContext:
        by_id = {m.message_id: m for m in history}
        by_id[target.message_id] = target

        valid_history: list[StoredChatMessage] = []
        for msg in history:
            if msg.message_id == target.message_id:
                continue
            if not msg.text:
                continue
            valid_history.append(msg)

        valid_history = valid_history[-self.history_limit :]
        included_ids: set[int] = {m.message_id for m in valid_history}

        extra: list[StoredChatMessage] = []
        if target.reply_to_message_id:
            replied = by_id.get(target.reply_to_message_id)
            if replied and replied.message_id not in included_ids:
                extra.append(replied)
                included_ids.add(replied.message_id)
            ordered = sorted(history, key=lambda m: m.message_id)
            idx = next(
                (i for i, m in enumerate(ordered) if m.message_id == target.reply_to_message_id),
                -1,
            )
            if idx >= 0:
                start = max(0, idx - self.reply_context_above)
                for msg in ordered[start:idx]:
                    if msg.message_id not in included_ids and msg.text:
                        extra.append(msg)
                        included_ids.add(msg.message_id)

        all_msgs = sorted(extra + valid_history, key=lambda m: m.message_id)
        ctx_messages: list[ContextMessage] = []
        participants: set[int] = set()

        for msg in all_msgs:
            cm = _to_context(msg)
            ctx_messages.append(cm)
            participants.add(cm.user_id)

        target_ctx = _to_context(target, is_target=True)
        participants.add(target_ctx.user_id)

        return ModerationContext(
            target_message=target_ctx,
            messages=ctx_messages,
            participant_ids=participants,
        )

    def build_batch(
        self,
        targets: list[StoredChatMessage],
        history: list[StoredChatMessage],
    ) -> BatchModerationContext:
        if not targets:
            raise ValueError("Batch must contain at least one message")

        ordered = sorted(targets, key=lambda m: m.message_id)
        earliest_id = ordered[0].message_id
        by_id = {m.message_id: m for m in history}
        for t in ordered:
            by_id[t.message_id] = t

        valid_history: list[StoredChatMessage] = []
        for msg in history:
            if msg.message_id >= earliest_id:
                continue
            if not msg.text:
                continue
            valid_history.append(msg)
        valid_history = valid_history[-self.history_limit :]
        included_ids: set[int] = {m.message_id for m in valid_history}

        extra: list[StoredChatMessage] = []
        sorted_history = sorted(history, key=lambda m: m.message_id)
        for target in ordered:
            if not target.reply_to_message_id:
                continue
            replied = by_id.get(target.reply_to_message_id)
            if replied and replied.message_id not in included_ids:
                extra.append(replied)
                included_ids.add(replied.message_id)
            idx = next(
                (i for i, m in enumerate(sorted_history) if m.message_id == target.reply_to_message_id),
                -1,
            )
            if idx >= 0:
                start = max(0, idx - self.reply_context_above)
                for msg in sorted_history[start:idx]:
                    if msg.message_id not in included_ids and msg.text:
                        extra.append(msg)
                        included_ids.add(msg.message_id)

        background = sorted(extra + valid_history, key=lambda m: m.message_id)
        ctx_messages: list[ContextMessage] = []
        participants: set[int] = set()

        for msg in background:
            cm = _to_context(msg)
            ctx_messages.append(cm)
            participants.add(cm.user_id)

        target_ctxs: list[ContextMessage] = []
        for target in ordered:
            cm = _to_context(target, is_target=True)
            target_ctxs.append(cm)
            participants.add(cm.user_id)

        return BatchModerationContext(
            target_messages=target_ctxs,
            messages=ctx_messages,
            participant_ids=participants,
        )

    def format_for_prompt(self, ctx: ModerationContext) -> str:
        lines = ["=== ИСТОРИЯ СООБЩЕНИЙ (от старых к новым) ==="]
        for msg in ctx.messages:
            reply_note = f" [reply_to:{msg.reply_to_message_id}]" if msg.reply_to_message_id else ""
            user = f"@{msg.username}" if msg.username else msg.full_name
            lines.append(
                f"[id:{msg.message_id}] [user_id:{msg.user_id}] [{user}]{reply_note}: {msg.text}"
            )
        lines.append("")
        lines.append("=== ОБРАБАТЫВАЕМОЕ СООБЩЕНИЕ ===")
        t = ctx.target_message
        user = f"@{t.username}" if t.username else t.full_name
        reply_note = f" [reply_to:{t.reply_to_message_id}]" if t.reply_to_message_id else ""
        lines.append(f"[id:{t.message_id}] [user_id:{t.user_id}] [{user}]{reply_note}: {t.text}")
        lines.append("")
        lines.append(f"Участники контекста (user_id): {', '.join(str(x) for x in sorted(ctx.participant_ids))}")
        return "\n".join(lines)

    def format_batch_for_prompt(self, ctx: BatchModerationContext) -> str:
        lines = ["=== ИСТОРИЯ ДО ПАЧКИ (от старых к новым) ==="]
        for msg in ctx.messages:
            reply_note = f" [reply_to:{msg.reply_to_message_id}]" if msg.reply_to_message_id else ""
            user = f"@{msg.username}" if msg.username else msg.full_name
            lines.append(
                f"[id:{msg.message_id}] [user_id:{msg.user_id}] [{user}]{reply_note}: {msg.text}"
            )
        lines.append("")
        lines.append("=== СООБЩЕНИЯ ДЛЯ АНАЛИЗА (батч) ===")
        for msg in ctx.target_messages:
            user = f"@{msg.username}" if msg.username else msg.full_name
            reply_note = f" [reply_to:{msg.reply_to_message_id}]" if msg.reply_to_message_id else ""
            lines.append(
                f"[id:{msg.message_id}] [user_id:{msg.user_id}] [{user}]{reply_note}: {msg.text}"
            )
        lines.append("")
        lines.append(f"Участники контекста (user_id): {', '.join(str(x) for x in sorted(ctx.participant_ids))}")
        return "\n".join(lines)


def _to_context(msg: StoredChatMessage, is_target: bool = False) -> ContextMessage:
    return ContextMessage(
        message_id=msg.message_id,
        user_id=msg.user_id,
        username=msg.username,
        full_name=msg.full_name,
        text=msg.text,
        reply_to_message_id=msg.reply_to_message_id,
        is_target=is_target,
    )
