from aiogram.types import Message


def topic_send_kwargs(message: Message | None) -> dict[str, int]:
    if message and message.message_thread_id:
        return {"message_thread_id": message.message_thread_id}
    return {}
