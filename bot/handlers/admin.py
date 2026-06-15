import json
import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, Message

from bot.commands import build_help_text, setup_bot_commands
from bot.db.database import Database
from bot.handlers.chats import _admin_panel_text
from bot.keyboards.admin_kb import admin_main_keyboard
from bot.services.gemini import GeminiService
from bot.ui.emoji import E
from bot.utils.access import can_access_dm, is_owner

logger = logging.getLogger(__name__)
router = Router()


class PrivateNonAdmin(BaseFilter):
    """Only match private messages from users who are NOT bot admins."""

    async def __call__(self, message: Message, db: Database) -> bool:
        if not message.from_user:
            return False
        return not await can_access_dm(db, message.from_user.id)


@router.message(Command("start"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, db: Database, gemini: GeminiService) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    if await can_access_dm(db, uid):
        owner = await is_owner(uid)
        role = "владелец" if owner else "суб-админ"
        await message.answer(
            f"{E['crown']} <b>Добро пожаловать, {role}!</b>\n\n"
            f"/admin — панель управления\n"
            f"Добавьте бота в группу → /linkchat",
            reply_markup=admin_main_keyboard(owner),
        )
        return

    if await db.is_dm_banned(uid):
        return

    await db.record_dm_message(uid)
    await message.answer(f"{E['block']} Не лезь, бот не для вас.")


@router.message(F.chat.type == ChatType.PRIVATE, PrivateNonAdmin())
async def private_messages(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    if await db.is_dm_banned(uid):
        return
    banned = await db.record_dm_message(uid)
    if banned:
        await message.answer(f"{E['ban']} Вы заблокированы на неделю за спам.")
        return
    await message.answer(f"{E['block']} Не лезь, бот не для вас.")


@router.message(Command("help"), F.chat.type == ChatType.PRIVATE)
async def cmd_help(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    if not await can_access_dm(db, uid):
        if await db.is_dm_banned(uid):
            return
        await db.record_dm_message(uid)
        await message.answer(f"{E['block']} Не лезь, бот не для вас.")
        return
    owner = await is_owner(uid)
    await message.answer(build_help_text(owner))


@router.message(Command("help"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def cmd_help_group(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    if not await can_access_dm(db, uid):
        return
    owner = await is_owner(uid)
    await message.answer(build_help_text(owner))


@router.message(Command("admin"), F.chat.type == ChatType.PRIVATE)
async def cmd_admin(message: Message, db: Database, gemini: GeminiService) -> None:
    if not message.from_user or not await can_access_dm(db, message.from_user.id):
        return
    owner = await is_owner(message.from_user.id)
    text = await _admin_panel_text(db, gemini, message.from_user.id)
    await message.answer(text, reply_markup=admin_main_keyboard(owner))


@router.callback_query(F.data == "admin:limits")
async def cb_limits(callback: CallbackQuery, gemini: GeminiService, db: Database) -> None:
    if not callback.from_user or not await can_access_dm(db, callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if not await is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    text = await gemini.get_limits_dashboard()
    await callback.message.edit_text(text, reply_markup=admin_main_keyboard(True))
    await callback.answer()


@router.callback_query(F.data == "admin:back")
async def cb_back(callback: CallbackQuery, db: Database, gemini: GeminiService) -> None:
    if not callback.from_user or not await can_access_dm(db, callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    owner = await is_owner(callback.from_user.id)
    text = await _admin_panel_text(db, gemini, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=admin_main_keyboard(owner))
    await callback.answer()


@router.message(Command("allpunishments"), F.chat.type == ChatType.PRIVATE)
async def cmd_all_punishments(message: Message, db: Database) -> None:
    if not message.from_user or not await can_access_dm(db, message.from_user.id):
        return
    owner = await is_owner(message.from_user.id)
    if owner:
        punishments = await db.get_all_punishments(limit=30)
    else:
        chats = await db.list_chats_for_admin(message.from_user.id, False)
        punishments = []
        for c in chats:
            punishments.extend(await db.get_active_punishments(c["chat_id"]))
    text = _format_punishments_list(punishments, title="Наказания")
    await message.answer(text, reply_markup=admin_main_keyboard(owner))


@router.callback_query(F.data == "admin:all_punishments")
async def cb_all_punishments(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user or not await can_access_dm(db, callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    owner = await is_owner(callback.from_user.id)
    if owner:
        punishments = await db.get_all_punishments(limit=30)
    else:
        chats = await db.list_chats_for_admin(callback.from_user.id, False)
        punishments = []
        for c in chats:
            punishments.extend(await db.get_active_punishments(c["chat_id"]))
    text = _format_punishments_list(punishments, title="Наказания")
    await callback.message.edit_text(text, reply_markup=admin_main_keyboard(owner))
    await callback.answer()


@router.message(Command("addadmin"), F.chat.type == ChatType.PRIVATE)
async def cmd_addadmin(message: Message, db: Database) -> None:
    if not message.from_user or not await is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Использование: /addadmin <user_id> <daily_limit>")
        return
    user_id, limit = int(parts[1]), int(parts[2])
    await db.add_sub_admin(user_id, limit)
    await message.answer(f"{E['check']} Суб-админ <code>{user_id}</code> — лимит <b>{limit}</b>/день")


def _format_punishments_list(punishments, title: str) -> str:
    if not punishments:
        return f"{E['check']} <b>{title}</b>\n\nНаказаний нет."
    lines = [f"{E['ban']} <b>{title}</b>\n"]
    for p in punishments:
        refs = json.loads(p.rule_references) if p.rule_references.startswith("[") else [p.rule_references]
        status = "🟢 активно" if p.active else "⚫ снято"
        lines.append(
            f"\n<b>#{p.id}</b> чат <code>{p.chat_id}</code> | user <code>{p.user_id}</code>\n"
            f"Тип: {p.punishment_type} | {status}\n"
            f"Правила: {', '.join(refs)}\n"
            f"<i>{p.explanation[:100]}</i>"
        )
    return "\n".join(lines)
