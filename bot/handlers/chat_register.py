import logging

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message

from bot.config import get_settings
from bot.db.database import Database
from bot.utils.access import can_access_dm, is_owner

logger = logging.getLogger(__name__)
router = Router()


@router.my_chat_member()
async def on_bot_added(event: ChatMemberUpdated, db: Database) -> None:
    if event.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    old = event.old_chat_member.status
    new = event.new_chat_member.status
    if old in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and new == ChatMemberStatus.MEMBER:
        await _try_register_chat(db, event.chat.id, event.chat.title or "", event.from_user.id if event.from_user else get_settings().owner_id)
    if new == ChatMemberStatus.ADMINISTRATOR:
        inviter = event.from_user.id if event.from_user else get_settings().owner_id
        await _try_register_chat(db, event.chat.id, event.chat.title or "", inviter)


@router.message(Command("linkchat"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def cmd_linkchat(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    if not await can_access_dm(db, uid):
        return
    await db.register_chat(message.chat.id, message.chat.title or "", uid)
    await message.answer("✅ Чат привязан к вашему аккаунту. Управляйте им в ЛС бота через /admin")


async def _try_register_chat(db: Database, chat_id: int, title: str, user_id: int) -> None:
    if not await can_access_dm(db, user_id):
        user_id = get_settings().owner_id
    existing = await db.get_registered_chat(chat_id)
    if existing:
        return
    await db.register_chat(chat_id, title, user_id)
    logger.info("Registered chat %s (%s) for admin %s", chat_id, title, user_id)
