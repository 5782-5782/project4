import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aiogram.types import Message

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


class ContextBuilder:
    """Builds moderation context from chat history."""

    def __init__(self, history_limit: int = 50, reply_context_above: int = 10) -> None:
        self.history_limit = history_limit
        self.reply_context_above = reply_context_above

    def build(self, target: Message, history: list[Message]) -> ModerationContext:
        # Filter out messages without text/caption and build index
        valid_history: list[Message] = []
        for msg in history:
            if msg.message_id == target.message_id:
                continue
            text = _message_text(msg)
            if text is None:
                continue
            valid_history.append(msg)

        # Take last N valid messages before target
        valid_history = valid_history[-self.history_limit :]
        included_ids: set[int] = {m.message_id for m in valid_history}

        # Add reply chain context for target
        extra: list[Message] = []
        if target.reply_to_message:
            replied = target.reply_to_message
            if replied.message_id not in included_ids and _message_text(replied):
                extra.append(replied)
                included_ids.add(replied.message_id)
            # Find messages above replied in history
            for msg in history:
                if msg.message_id >= replied.message_id:
                    break
            idx = next((i for i, m in enumerate(history) if m.message_id == replied.message_id), -1)
            if idx >= 0:
                start = max(0, idx - self.reply_context_above)
                for msg in history[start:idx]:
                    if msg.message_id not in included_ids and _message_text(msg):
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


def _message_text(msg: Message) -> str | None:
      if msg.text:
          return msg.text
      if msg.caption:
          return msg.caption
      return None


def _to_context(msg: Message, is_target: bool = False) -> ContextMessage:
      user = msg.from_user
      return ContextMessage(
          message_id=msg.message_id,
          user_id=user.id if user else 0,
          username=user.username if user else None,
          full_name=user.full_name if user else "Unknown",
          text=_message_text(msg) or "",
          reply_to_message_id=msg.reply_to_message.message_id if msg.reply_to_message else None,
          is_target=is_target,
      )
