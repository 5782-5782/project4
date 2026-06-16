import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner

logger = logging.getLogger(__name__)


@dataclass
class ChatRolesInfo:
    prompt_text: str
    roles_by_user_id: dict[int, str] = field(default_factory=dict)

    @property
    def privileged_user_ids(self) -> set[int]:
        return set(self.roles_by_user_id.keys())


async def get_chat_roles(bot: Bot, chat_id: int) -> ChatRolesInfo:
    try:
        members = await bot.get_chat_administrators(chat_id)
    except Exception as exc:
        logger.warning("Could not fetch chat administrators chat=%s: %s", chat_id, exc)
        return ChatRolesInfo("(список администраторов недоступен)")

    roles: dict[int, str] = {}
    lines: list[str] = []
    for member in members:
        user = member.user
        if user.is_bot:
            continue
        name = f"@{user.username}" if user.username else user.full_name
        if isinstance(member, ChatMemberOwner):
            role = "владелец чата"
        elif isinstance(member, ChatMemberAdministrator):
            role = "администратор"
        else:
            continue
        roles[user.id] = role
        lines.append(f"- user_id={user.id} ({name}) — {role}")

    if not lines:
        return ChatRolesInfo("(администраторы не найдены)", roles)
    return ChatRolesInfo("\n".join(lines), roles)


def format_user_role_tag(user_id: int, roles_by_user_id: dict[int, str]) -> str:
    role = roles_by_user_id.get(user_id)
    if not role:
        return ""
    return f" [{role}]"
