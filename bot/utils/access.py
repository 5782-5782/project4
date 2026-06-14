"""Access control for owner and sub-admins."""

from bot.config import get_settings
from bot.db.database import Database


async def is_owner(user_id: int) -> bool:
    return user_id == get_settings().owner_id


async def is_sub_admin(db: Database, user_id: int) -> bool:
    admin = await db.get_sub_admin(user_id)
    return admin is not None and admin["active"]


async def can_access_dm(db: Database, user_id: int) -> bool:
    return await is_owner(user_id) or await is_sub_admin(db, user_id)


async def can_manage_chat(db: Database, user_id: int, chat_id: int) -> bool:
    if await is_owner(user_id):
        return True
    chat = await db.get_registered_chat(chat_id)
    if not chat:
        return False
    return chat["owner_admin_id"] == user_id


async def can_use_ai_quota(db: Database, user_id: int) -> tuple[bool, str]:
    if await is_owner(user_id):
        return True, ""
    admin = await db.get_sub_admin(user_id)
    if not admin or not admin["active"]:
        return False, "Нет прав"
    used = await db.get_admin_daily_usage(user_id)
    limit = admin["daily_limit"]
    if used >= limit:
        return False, f"Дневной лимит исчерпан ({used}/{limit})"
    return True, ""


async def get_chat_owner_for_processing(db: Database, chat_id: int) -> int:
    """Returns user_id whose quota to charge for moderation in this chat."""
    chat = await db.get_registered_chat(chat_id)
    if chat and chat["owner_admin_id"]:
        return chat["owner_admin_id"]
    return get_settings().owner_id
