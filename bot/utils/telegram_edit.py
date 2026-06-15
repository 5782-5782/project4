import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)


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


async def safe_edit_message(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit message text and markup; ignore Telegram 'message is not modified' errors."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        err = str(exc).lower()
        if "message is not modified" in err:
            return
        await edit_message_text_safe(message, text)
        if reply_markup is not None:
            await edit_message_markup_safe(message, reply_markup)


async def edit_message_status_and_keyboard(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    """Edit text and inline keyboard in separate API calls."""
    await edit_message_text_safe(message, text)
    if reply_markup is not None:
        await edit_message_markup_safe(message, reply_markup)
