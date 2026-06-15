import asyncio
import logging

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.types import InlineKeyboardMarkup, Message

from bot.db.database import Database

logger = logging.getLogger(__name__)

# Telegram Bot API: answerCallbackQuery text max length when show_alert=True
CALLBACK_ALERT_TEXT_LIMIT = 200
from bot.keyboards.punishment import punishments_history_keyboard
from bot.utils.access import is_owner


def get_punishments_list_back(markup: InlineKeyboardMarkup | None) -> str | None:
    if not markup:
        return None
    for row in markup.inline_keyboard:
        for btn in row:
            data = btn.callback_data or ""
            if data == "admin:back" or data.startswith("chat:"):
                return data
    return None


def append_status_line(text: str, status: str) -> str:
    if status in text:
        return text
    return f"{text}\n\n{status}"


async def build_punishments_list_view(
    db: Database,
    user_id: int,
    back_data: str,
) -> tuple[str, InlineKeyboardMarkup]:
    from bot.handlers.admin import _format_punishments_list

    if back_data == "admin:back":
        owner = await is_owner(user_id)
        if owner:
            punishments = await db.get_all_punishments(limit=30)
        else:
            chats = await db.list_chats_for_admin(user_id, False)
            punishments = []
            for chat in chats:
                punishments.extend(await db.get_chat_punishment_history(chat["chat_id"], limit=15))
        title = "История наказаний"
    elif back_data.startswith("chat:"):
        chat_id = int(back_data.split(":")[1])
        punishments = await db.get_chat_punishment_history(chat_id)
        title = f"История чата {chat_id}"
    else:
        raise ValueError(f"Unknown punishments list back_data: {back_data}")

    text = _format_punishments_list(punishments, title=title)
    markup = punishments_history_keyboard(punishments, back_data)
    return text, markup


async def deliver_punishment_reason(
    bot: Bot,
    chat_id: int,
    explanation: str,
    callback_answer,
) -> None:
    reason = (explanation or "").strip() or "Причина не указана."
    if len(reason) <= CALLBACK_ALERT_TEXT_LIMIT:
        await callback_answer(reason, show_alert=True)
        return

    await callback_answer()
    sent = await bot.send_message(chat_id, f"💬 {reason}")
    asyncio.create_task(_delete_message_later(sent, delay_seconds=10))


async def _delete_message_later(message: Message, delay_seconds: int) -> None:
    await asyncio.sleep(delay_seconds)
    try:
        await message.delete()
    except Exception as exc:
        logger.debug("Could not delete temporary reason message: %s", exc)


def is_private_punishments_list(message: Message) -> bool:
    return (
        message.chat.type == ChatType.PRIVATE
        and get_punishments_list_back(message.reply_markup) is not None
    )
