from aiogram.types import CallbackQuery, User

from bot.config import get_settings
from bot.db.database import Database


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


async def guard_punishment_button(callback: CallbackQuery, db: Database) -> bool:
    """Return True if the click may proceed. Otherwise answer with spam alert."""
    if not callback.from_user or not callback.message:
        return True

    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
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
