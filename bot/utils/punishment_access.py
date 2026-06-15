import json

from aiogram import Bot
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner

from bot.db.database import Database, Punishment
from bot.utils.access import can_manage_chat, is_owner


async def can_manage_punishment(
    db: Database,
    bot: Bot,
    punishment: Punishment,
    user_id: int,
) -> bool:
    if await is_owner(user_id):
        return True
    if await can_manage_chat(db, user_id, punishment.chat_id):
        return True
    try:
        can_unpunish_ids = json.loads(punishment.can_unpunish_ids)
    except (json.JSONDecodeError, TypeError):
        can_unpunish_ids = []
    if user_id in can_unpunish_ids:
        return True
    try:
        member = await bot.get_chat_member(punishment.chat_id, user_id)
    except Exception:
        return False
    return isinstance(member, (ChatMemberOwner, ChatMemberAdministrator))
