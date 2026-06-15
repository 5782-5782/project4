import logging

from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

from bot.db.database import Database
from bot.keyboards.punishment import punishments_history_keyboard
from bot.utils.access import is_owner

logger = logging.getLogger(__name__)


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


async def edit_message_text_safe(message: Message, text: str) -> bool:
    try:
        await message.edit_text(text)
        return True
    except TelegramBadRequest as exc:
        err = str(exc).lower()
        if "message is not modified" in err:
            return False
        if message.caption is not None:
            try:
                await message.edit_caption(caption=text)
                return True
            except TelegramBadRequest as cap_exc:
                if "message is not modified" in str(cap_exc).lower():
                    return False
        logger.warning("Failed to edit message text chat=%s: %s", message.chat.id, exc)
        return False


async def edit_message_markup_safe(message: Message, reply_markup: InlineKeyboardMarkup | None) -> bool:
    try:
        await message.edit_reply_markup(reply_markup=reply_markup)
        return True
    except TelegramBadRequest as exc:
        err = str(exc).lower()
        if "message is not modified" in err:
            return True
        logger.warning("Failed to edit message markup chat=%s: %s", message.chat.id, exc)
        return False


async def edit_message_status_and_keyboard(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    """Edit text and inline keyboard in separate API calls (Telegram is flaky when combined)."""
    await edit_message_text_safe(message, text)
    if reply_markup is not None:
        await edit_message_markup_safe(message, reply_markup)


async def safe_edit_message(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    await edit_message_status_and_keyboard(message, text, reply_markup)


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


def is_private_punishments_list(message: Message) -> bool:
    return (
        message.chat.type == ChatType.PRIVATE
        and get_punishments_list_back(message.reply_markup) is not None
    )
