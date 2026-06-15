import json
import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.commands import build_help_text, setup_bot_commands
from bot.db.database import Database
from bot.handlers.chats import _admin_panel_text, _cancel_rules_input
from bot.keyboards.admin_kb import admin_main_keyboard
from bot.keyboards.punishment import punishments_history_keyboard
from bot.services.gemini import GeminiService
from bot.ui.emoji import E
from bot.utils.punishment_time import format_punishment_moment
from bot.utils.access import can_access_dm, is_owner
from bot.utils.telegram_edit import safe_edit_message

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
    await safe_edit_message(callback.message, text, admin_main_keyboard(True))
    await callback.answer()


@router.callback_query(F.data == "admin:back")
async def cb_back(callback: CallbackQuery, db: Database, gemini: GeminiService, state: FSMContext) -> None:
    if not callback.from_user or not await can_access_dm(db, callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await _cancel_rules_input(state, db, callback.from_user.id)
    owner = await is_owner(callback.from_user.id)
    text = await _admin_panel_text(db, gemini, callback.from_user.id)
    await safe_edit_message(callback.message, text, admin_main_keyboard(owner))
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
            punishments.extend(await db.get_chat_punishment_history(c["chat_id"], limit=15))
    text = _format_punishments_list(punishments, title="История наказаний")
    await message.answer(
        text,
        reply_markup=punishments_history_keyboard(punishments, "admin:back"),
    )


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
            punishments.extend(await db.get_chat_punishment_history(c["chat_id"], limit=15))
    text = _format_punishments_list(punishments, title="История наказаний")
    await safe_edit_message(
        callback.message,
        text,
        punishments_history_keyboard(punishments, "admin:back"),
    )
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


@router.message(Command("cleardb"), F.chat.type == ChatType.PRIVATE)
async def cmd_cleardb(message: Message, db: Database, gemini: GeminiService) -> None:
    """Owner only: clear test data and Gemini usage counters."""
    if not message.from_user or not await is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("yes", "all"):
        await message.answer(
            f"{E['warn']} <b>Очистка базы после тестов</b>\n\n"
            f"<code>/cleardb yes</code> — наказания, логи, лимиты Gemini, спам-баны\n"
            f"(чаты и суб-админы остаются)\n\n"
            f"<code>/cleardb all</code> — всё, включая привязки чатов и суб-админов"
        )
        return

    wipe_all = parts[1].lower() == "all"
    counts = await db.clear_test_data(keep_chats=not wipe_all, keep_sub_admins=not wipe_all)
    gemini.reset_runtime_state()
    total = sum(counts.values())
    lines = [f"{E['check']} <b>База очищена</b> — удалено строк: <b>{total}</b>\n"]
    for table, n in sorted(counts.items()):
        if n:
            lines.append(f"• <code>{table}</code>: {n}")
    lines.append("\n<i>Лимиты Gemini в памяти сброшены.</i>")
    await message.answer("\n".join(lines))


def _format_punishments_list(punishments, title: str) -> str:
    if not punishments:
        return f"{E['check']} <b>{title}</b>\n\nНаказаний нет."
    lines = [f"{E['ban']} <b>{title}</b>\n"]
    for p in punishments:
        refs = json.loads(p.rule_references) if p.rule_references.startswith("[") else [p.rule_references]
        status = "🟢 активно" if p.active else "⚫ в истории"
        type_label = p.punishment_type
        if type_label == "warning":
            type_label = "предупреждение"
        elif type_label == "admin_warning":
            type_label = "предупр. админу"
        lines.append(
            f"\n<b>#{p.id}</b> чат <code>{p.chat_id}</code> | user <code>{p.user_id}</code>\n"
            f"Тип: {type_label} | {status}\n"
            f"🕐 {format_punishment_moment(p.created_at)}\n"
            f"Правила: {', '.join(refs)}\n"
            f"<i>{p.explanation[:100]}</i>"
        )
    return "\n".join(lines)
