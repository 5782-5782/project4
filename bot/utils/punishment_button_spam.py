from aiogram import Bot
from aiogram.types import CallbackQuery, ChatMemberAdministrator, ChatMemberOwner, User

from bot.config import get_settings
from bot.db.database import Database
from bot.utils.access import can_manage_chat, is_owner


def format_callback_user(user: User) -> str:
    if user.username:
        return f"@{user.username}"
    return user.full_name


def format_remaining_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    if minutes and secs:
        return f"{minutes} мин {secs} сек"
    if minutes:
        return f"{minutes} мин"
    return f"{secs} сек"


async def is_reason_spam_exempt(db: Database, bot: Bot, user_id: int, chat_id: int) -> bool:
    if await is_owner(user_id):
        return True
    if chat_id < 0 and await can_manage_chat(db, user_id, chat_id):
        return True
    if chat_id >= 0:
        return False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return isinstance(member, (ChatMemberOwner, ChatMemberAdministrator))


async def guard_reason_button(callback: CallbackQuery, db: Database, bot: Bot) -> bool:
    """Anti-spam for the «Причина» button only."""
    if not callback.from_user or not callback.message:
        return True

    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    if await is_reason_spam_exempt(db, bot, user_id, chat_id):
        return True
    remaining = await db.get_punishment_button_ban_remaining(user_id, chat_id)
    if remaining is not None and remaining > 0:
        await callback.answer(
            f"Вы спамер. Сможете через: {format_remaining_seconds(remaining)}",
            show_alert=True,
        )
        return False

    just_banned = await db.record_punishment_button_click(user_id, chat_id)
    if just_banned:
        ban_seconds = get_settings().punishment_button_spam_ban_minutes * 60
        await callback.answer(
            f"Вы спамер. Сможете через: {format_remaining_seconds(ban_seconds)}",
            show_alert=True,
        )
        return False

    return True
