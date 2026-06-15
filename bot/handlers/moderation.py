import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from bot.services.batch import BatchProcessor

logger = logging.getLogger(__name__)
router = Router()


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_message(message: Message, batch_processor: BatchProcessor) -> None:
    if message.new_chat_members or message.left_chat_member:
        return
    if not message.from_user or message.from_user.is_bot:
        return
    text = message.text or message.caption
    if text and text.startswith("/"):
        return

    batch_processor.store_history(message.chat.id, message)
    await batch_processor.enqueue(message.bot, message)
