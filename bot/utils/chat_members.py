import logging
import re
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import Message, User

from bot.config import get_settings
from bot.db.database import ChatParticipant, Database
from bot.services.context import BatchModerationContext, ModerationContext

logger = logging.getLogger(__name__)

MAX_LOOKUP_REQUESTS = 10


@dataclass
class ChatMembersInfo:
    member_count: int
    is_full_list: bool
    prompt_text: str


def format_participant_line(
    user_id: int,
    username: str | None,
    full_name: str,
) -> str:
    parts = [f"user_id={user_id}"]
    if username:
        parts.append(f"@{username}")
    nick = (full_name or "").strip() or "без имени"
    parts.append(f'ник «{nick}»')
    return ", ".join(parts)


async def upsert_participant_from_user(
    db: Database,
    chat_id: int,
    user: User | None,
    *,
    in_chat: bool = True,
) -> None:
    if not user or user.is_bot:
        return
    await db.upsert_chat_participant(
        chat_id,
        user.id,
        user.username,
        user.full_name or "",
        in_chat=in_chat,
    )


async def track_participants_from_message(db: Database, message: Message) -> None:
    chat_id = message.chat.id
    await upsert_participant_from_user(db, chat_id, message.from_user)
    if message.reply_to_message:
        await upsert_participant_from_user(db, chat_id, message.reply_to_message.from_user)
    for entity in message.entities or []:
        if entity.type == "text_mention" and entity.user:
            await upsert_participant_from_user(db, chat_id, entity.user)
    for entity in message.caption_entities or []:
        if entity.type == "text_mention" and entity.user:
            await upsert_participant_from_user(db, chat_id, entity.user)


async def ensure_participants_registry(db: Database, chat_id: int) -> None:
    if await db.count_chat_participants(chat_id) == 0:
        seeded = await db.seed_participants_from_chat_messages(chat_id)
        if seeded:
            logger.info("Seeded %s chat participants from history chat=%s", seeded, chat_id)


async def get_chat_member_count(bot: Bot, chat_id: int) -> int:
    try:
        return await bot.get_chat_member_count(chat_id)
    except Exception as exc:
        logger.warning("Could not fetch chat member count chat=%s: %s", chat_id, exc)
        return 0


def collect_context_profiles(
    context: ModerationContext | BatchModerationContext,
) -> dict[int, tuple[str | None, str]]:
    profiles: dict[int, tuple[str | None, str]] = {}
    for msg in context.messages:
        profiles[msg.user_id] = (msg.username, msg.full_name)
    if isinstance(context, ModerationContext):
        t = context.target_message
        profiles[t.user_id] = (t.username, t.full_name)
    else:
        for msg in context.target_messages:
            profiles[msg.user_id] = (msg.username, msg.full_name)
    return profiles


async def _enrich_context_profiles(
    bot: Bot,
    db: Database,
    chat_id: int,
    profiles: dict[int, tuple[str | None, str]],
) -> dict[int, tuple[str | None, str]]:
    enriched = dict(profiles)
    for user_id in profiles:
        stored = await db.get_chat_participant(chat_id, user_id)
        if stored:
            enriched[user_id] = (stored.username or profiles[user_id][0], stored.full_name or profiles[user_id][1])
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            user = member.user
            if user and not user.is_bot:
                await upsert_participant_from_user(db, chat_id, user)
                enriched[user_id] = (user.username, user.full_name or profiles[user_id][1])
        except Exception:
            pass
    return enriched


def _format_context_participants_section(
    profiles: dict[int, tuple[str | None, str]],
) -> str:
    if not profiles:
        return "УЧАСТНИКИ ИЗ ТЕКУЩЕГО КОНТЕКСТА: (нет)"
    lines = ["УЧАСТНИКИ ИЗ ТЕКУЩЕГО КОНТЕКСТА (user_id, username, ник):"]
    for user_id in sorted(profiles):
        username, full_name = profiles[user_id]
        lines.append(f"- {format_participant_line(user_id, username, full_name)}")
    return "\n".join(lines)


def _format_full_participants_list(participants: list[ChatParticipant], member_count: int) -> str:
    if not participants:
        return (
            f"В чате около {member_count} участников, но в реестре бота пока никого нет "
            "(участники добавляются по сообщениям и входу в чат)."
        )
    lines = [f"УЧАСТНИКИ ЧАТА (всего {member_count}, известно боту {len(participants)}):"]
    for p in participants:
        lines.append(f"- {format_participant_line(p.user_id, p.username, p.full_name)}")
    if len(participants) < member_count:
        lines.append(
            f"(остальные {member_count - len(participants)} не писали в чат после добавления бота)"
        )
    return "\n".join(lines)


def _format_large_chat_members_hint(member_count: int) -> str:
    threshold = get_settings().chat_members_full_list_threshold
    return (
        f"УЧАСТНИКИ ЧАТА: всего {member_count} человек (порог полного списка: {threshold}).\n"
        "Полный список всех участников не передаётся из-за размера чата.\n"
        "Участники из текущего контекста перечислены выше — их данные всегда доступны.\n"
        "Если для решения нужны данные о другом человеке вне контекста, ответь JSON:\n"
        '{{\n'
        '  "action": "lookup_users",\n'
        '  "lookup_requests": [\n'
        '    {{"user_id": 123456789}},\n'
        '    {{"username": "nickname"}}\n'
        "  ],\n"
        '  "explanation": "зачем нужна информация"\n'
        "}}\n"
        "username можно указывать с @ или без. После этого ты получишь данные и вынесешь финальное решение."
    )


async def get_chat_members_context(
    bot: Bot,
    db: Database,
    chat_id: int,
    context: ModerationContext | BatchModerationContext | None = None,
) -> ChatMembersInfo:
    await ensure_participants_registry(db, chat_id)
    member_count = await get_chat_member_count(bot, chat_id)
    threshold = get_settings().chat_members_full_list_threshold
    participants = await db.get_chat_participants(chat_id)

    context_profiles: dict[int, tuple[str | None, str]] = {}
    if context is not None:
        context_profiles = await _enrich_context_profiles(
            bot, db, chat_id, collect_context_profiles(context)
        )

    sections = [_format_context_participants_section(context_profiles)]

    effective_count = member_count or len(participants)
    is_full_list = effective_count > 0 and effective_count < threshold
    if is_full_list:
        sections.append(_format_full_participants_list(participants, effective_count))
    else:
        sections.append(_format_large_chat_members_hint(effective_count))

    return ChatMembersInfo(
        member_count=effective_count,
        is_full_list=is_full_list,
        prompt_text="\n\n".join(sections),
    )


async def _resolve_user_id(
    bot: Bot,
    db: Database,
    chat_id: int,
    user_id: int,
) -> str:
    stored = await db.get_chat_participant(chat_id, user_id)
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        user = member.user
        await upsert_participant_from_user(db, chat_id, user)
        return format_participant_line(user.id, user.username, user.full_name)
    except Exception:
        if stored:
            status = "в чате" if stored.in_chat else "вышел из чата"
            return (
                f"{format_participant_line(stored.user_id, stored.username, stored.full_name)} "
                f"({status}, данные из реестра бота)"
            )
        return f"user_id={user_id} — не найден в чате и в реестре бота"


async def _resolve_username(
    bot: Bot,
    db: Database,
    chat_id: int,
    username: str,
) -> str:
    uname = username.lstrip("@")
    stored = await db.find_chat_participant_by_username(chat_id, uname)
    if stored:
        try:
            member = await bot.get_chat_member(chat_id, stored.user_id)
            user = member.user
            await upsert_participant_from_user(db, chat_id, user)
            return format_participant_line(user.id, user.username, user.full_name)
        except Exception:
            status = "в чате" if stored.in_chat else "вышел из чата"
            return (
                f"{format_participant_line(stored.user_id, stored.username, stored.full_name)} "
                f"({status}, данные из реестра бота)"
            )

    mention = f"@{uname}"
    for participant in await db.get_chat_participants(chat_id, in_chat_only=False):
        if participant.username and participant.username.lower() == uname.lower():
            return await _resolve_user_id(bot, db, chat_id, participant.user_id)

    return f"{mention} — не найден в реестре бота (возможно, человек не писал в чат)"


async def lookup_participant_by_username(
    bot: Bot,
    db: Database,
    chat_id: int,
    username: str,
) -> ChatParticipant | None:
    uname = username.lstrip("@").lower()
    if not uname:
        return None

    stored = await db.find_chat_participant_by_username(chat_id, uname)
    if not stored:
        for participant in await db.get_chat_participants(chat_id, in_chat_only=False):
            if participant.username and participant.username.lower() == uname:
                stored = participant
                break
    if not stored:
        return None

    try:
        member = await bot.get_chat_member(chat_id, stored.user_id)
        user = member.user
        if user.is_bot:
            return None
        await upsert_participant_from_user(db, chat_id, user)
        return await db.get_chat_participant(chat_id, user.id)
    except Exception:
        return stored if stored.in_chat else None


async def resolve_user_lookups(
    bot: Bot,
    db: Database,
    chat_id: int,
    lookup_requests: list,
) -> str:
    if not lookup_requests:
        return "(запросы на поиск пользователей пусты)"

    lines: list[str] = []
    for raw in lookup_requests[:MAX_LOOKUP_REQUESTS]:
        if not isinstance(raw, dict):
            continue
        if raw.get("user_id") is not None:
            try:
                user_id = int(raw["user_id"])
            except (TypeError, ValueError):
                lines.append(f"некорректный user_id: {raw.get('user_id')!r}")
                continue
            lines.append(f"- {await _resolve_user_id(bot, db, chat_id, user_id)}")
            continue
        username = raw.get("username")
        if username:
            lines.append(f"- {await _resolve_username(bot, db, chat_id, str(username))}")
            continue
        lines.append(f"- непонятный запрос: {raw!r}")

    if not lines:
        return "(не удалось разобрать запросы на поиск пользователей)"
    return "\n".join(lines)


def is_lookup_response(data: dict) -> bool:
    return data.get("action") == "lookup_users"


def extract_lookup_requests(data: dict) -> list:
    requests = data.get("lookup_requests") or data.get("user_lookups") or []
    return requests if isinstance(requests, list) else []


LOOKUP_FOLLOWUP_SINGLE = """

ДАННЫЕ ПО ЗАПРОСУ УЧАСТНИКОВ:
{lookup_results}

Теперь вынеси ФИНАЛЬНОЕ решение по модерации в обычном формате JSON (action: none | warning | punish).
Не используй action="lookup_users" повторно — работай с имеющимися данными.
"""

LOOKUP_FOLLOWUP_BATCH = """

ДАННЫЕ ПО ЗАПРОСУ УЧАСТНИКОВ:
{lookup_results}

Теперь вынеси ФИНАЛЬНЫЕ решения по модерации для всех сообщений батча в обычном формате JSON с массивом decisions.
Не используй action="lookup_users" повторно — работай с имеющимися данными.
"""

_MENTION_RE = re.compile(r"@([A-Za-z0-9_]{4,32})")


def extract_mentioned_usernames(text: str) -> list[str]:
    return list(dict.fromkeys(_MENTION_RE.findall(text or "")))
