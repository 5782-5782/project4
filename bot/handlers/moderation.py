import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from bot.db.database import Database
from bot.services.batch import BatchProcessor
from bot.utils.chat_members import track_participants_from_message

logger = logging.getLogger(__name__)
router = Router()


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.new_chat_members,
)
async def on_members_joined(message: Message, db: Database) -> None:
    for user in message.new_chat_members or []:
        if user.is_bot:
            continue
        await db.upsert_chat_participant(
            message.chat.id,
            user.id,
            user.username,
            user.full_name or "",
            in_chat=True,
        )


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.left_chat_member,
)
async def on_member_left(message: Message, db: Database) -> None:
    user = message.left_chat_member
    if not user or user.is_bot:
        return
    await db.mark_chat_participant_left(message.chat.id, user.id)


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    ~F.text.startswith("/"),
    ~(F.caption.startswith("/")),
)
async def group_message(
    message: Message,
    db: Database,
    batch_processor: BatchProcessor,
) -> None:
    if message.new_chat_members or message.left_chat_member:
        return
    if not message.from_user or message.from_user.is_bot:
        return
    if not message.text and not message.caption:
        return

    await track_participants_from_message(db, message)

    logger.info(
        "Group message for moderation chat=%s msg=%s user=%s",
        message.chat.id,
        message.message_id,
        message.from_user.id,
    )
    await batch_processor.store_history(message)
    await batch_processor.enqueue(message.bot, message)
