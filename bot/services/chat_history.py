from dataclasses import dataclass

from aiogram.types import Message


@dataclass
class StoredChatMessage:
    chat_id: int
    message_id: int
    user_id: int
    username: str | None
    full_name: str
    text: str
    reply_to_message_id: int | None = None


def message_text(message: Message) -> str | None:
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    return None


def from_telegram(message: Message, chat_id: int | None = None) -> StoredChatMessage | None:
    text = message_text(message)
    if not text or not message.from_user:
        return None
    cid = chat_id if chat_id is not None else message.chat.id
    reply_id = None
    if message.reply_to_message:
        reply_id = message.reply_to_message.message_id
    return StoredChatMessage(
        chat_id=cid,
        message_id=message.message_id,
        user_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
        text=text,
        reply_to_message_id=reply_id,
    )


def from_row(row) -> StoredChatMessage:
    return StoredChatMessage(
        chat_id=row["chat_id"],
        message_id=row["message_id"],
        user_id=row["user_id"],
        username=row["username"],
        full_name=row["full_name"],
        text=row["text"],
        reply_to_message_id=row["reply_to_message_id"],
    )
