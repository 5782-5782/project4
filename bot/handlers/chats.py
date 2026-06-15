import html
import json
import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.db.database import Database
from bot.keyboards.admin_kb import (
    admin_main_keyboard,
    chat_detail_keyboard,
    chat_interval_keyboard,
    chats_list_keyboard,
    subadmin_detail_keyboard,
    subadmins_keyboard,
)
from bot.services.gemini import GeminiService
from bot.states.admin import AdminStates
from bot.ui.emoji import E
from bot.utils.access import can_access_dm, can_manage_chat, is_owner

logger = logging.getLogger(__name__)
router = Router()


class PendingRulesFilter(BaseFilter):
    """Private message from admin who is editing chat rules."""

    async def __call__(self, message: Message, db: Database) -> bool:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return False
        if not await can_access_dm(db, message.from_user.id):
            return False
        return await db.get_pending_rules_input(message.from_user.id) is not None


async def _cancel_rules_input(state: FSMContext, db: Database, user_id: int) -> None:
    await db.clear_pending_rules_input(user_id)
    current = await state.get_state()
    if current == AdminStates.waiting_rules.state:
        await state.clear()


async def _extract_rules_text(message: Message) -> str | None:
    if message.document:
        if not message.document.file_name or not message.document.file_name.endswith(".txt"):
            return None
        file = await message.bot.get_file(message.document.file_id)
        data = await message.bot.download_file(file.file_path)
        return data.read().decode("utf-8", errors="replace")
    if message.text:
        return message.text
    return None


async def _admin_panel_text(db: Database, gemini: GeminiService, user_id: int) -> str:
    owner = await is_owner(user_id)
    stats = await db.get_moderation_stats(days=1)
    punish_cnt = stats.get("punish", 0)
    pardon_cnt = stats.get("pardon", 0)
    lines = [
        f"{E['crown']} <b>Панель управления</b>",
        "",
        f"📈 <b>Сегодня:</b>",
        f"  🕊 Помилован: <b>{pardon_cnt}</b>",
        f"  🚫 Наказан: <b>{punish_cnt}</b>",
    ]
    if owner:
        chats = await db.list_chats_for_admin(user_id, True)
        lines.append(f"\n💬 Чатов: <b>{len(chats)}</b> | Нажмите «Лимиты API» для деталей")
    else:
        admin = await db.get_sub_admin(user_id)
        if admin:
            used = await db.get_admin_daily_usage(user_id)
            chats = await db.list_chats_for_admin(user_id, False)
            lines.append(f"\n{E['clock']} Лимит: <b>{used}</b>/{admin['daily_limit']} | Чатов: <b>{len(chats)}</b>")
    return "\n".join(lines)


@router.callback_query(F.data == "admin:chats")
async def cb_chats(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if not callback.from_user:
        return
    uid = callback.from_user.id
    await _cancel_rules_input(state, db, uid)
    owner = await is_owner(uid)
    chats = await db.list_chats_for_admin(uid, owner)
    if not chats:
        await callback.message.edit_text(
            f"{E['info']} <b>Нет привязанных чатов</b>\n\n"
            "Добавьте бота в группу и отправьте /linkchat",
            reply_markup=admin_main_keyboard(owner),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        f"{E['shield']} <b>Ваши чаты</b> ({len(chats)})",
        reply_markup=chats_list_keyboard(chats),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("chats_page:"))
async def cb_chats_page(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    page = int(callback.data.split(":")[1])
    uid = callback.from_user.id
    owner = await is_owner(uid)
    chats = await db.list_chats_for_admin(uid, owner)
    await callback.message.edit_text(
        f"{E['shield']} <b>Ваши чаты</b> ({len(chats)})",
        reply_markup=chats_list_keyboard(chats, page=page),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^chat:-?\d+$"))
async def cb_chat_detail(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if not callback.from_user:
        return
    await _cancel_rules_input(state, db, callback.from_user.id)
    chat_id = int(callback.data.split(":")[1])
    uid = callback.from_user.id
    if not await can_manage_chat(db, uid, chat_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await _show_chat_detail(callback.message, db, uid, chat_id)
    await callback.answer()


@router.callback_query(F.data.startswith("chat_mod_toggle:"))
async def cb_chat_mod_toggle(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    chat_id = int(callback.data.split(":")[1])
    uid = callback.from_user.id
    if not await can_manage_chat(db, uid, chat_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    settings = await db.get_chat_settings(chat_id)
    new_val = not bool(settings.get("moderation_enabled", 1))
    await db.set_moderation_enabled(chat_id, new_val)
    await _show_chat_detail(callback.message, db, uid, chat_id)
    await callback.answer()


@router.callback_query(F.data.startswith("chat_interval:"))
async def cb_chat_interval_menu(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    chat_id = int(callback.data.split(":")[1])
    if not await can_manage_chat(db, callback.from_user.id, chat_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        f"{E['clock']} <b>Интервал батчинга</b>\n\n0 = каждое сообщение отдельно",
        reply_markup=chat_interval_keyboard(chat_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cint:"))
async def cb_set_chat_interval(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    _, chat_id_s, interval_s = callback.data.split(":")
    chat_id, interval = int(chat_id_s), int(interval_s)
    if not await can_manage_chat(db, callback.from_user.id, chat_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await db.update_batch_interval(chat_id, interval)
    await callback.answer(f"Интервал: {interval}с")
    await _show_chat_detail(callback.message, db, callback.from_user.id, chat_id)


async def _show_chat_detail(message, db: Database, user_id: int, chat_id: int) -> None:
    reg = await db.get_registered_chat(chat_id)
    settings = await db.get_chat_settings(chat_id)
    stats = await db.get_moderation_stats(chat_id, days=1)
    title = reg["title"] if reg else str(chat_id)
    mod_on = bool(settings.get("moderation_enabled", 1))
    rules_len = len(settings.get("rules_text", ""))
    text = (
        f"💬 <b>{title}</b>\n"
        f"<code>{chat_id}</code>\n\n"
        f"🛡 Модерация: {'🟢 вкл' if mod_on else '🔴 выкл'}\n"
        f"⏱ Интервал: <b>{settings['batch_interval']}</b>с\n"
        f"📜 Правила: <b>{rules_len}</b> символов\n\n"
        f"📈 Сегодня: 🕊{stats.get('pardon', 0)} | 🚫{stats.get('punish', 0)}"
    )
    await message.edit_text(text, reply_markup=chat_detail_keyboard(chat_id, mod_on))


@router.callback_query(F.data.startswith("chat_rules:"))
async def cb_chat_rules(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.from_user:
        return
    chat_id = int(callback.data.split(":")[1])
    uid = callback.from_user.id
    if not await can_manage_chat(db, uid, chat_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_rules)
    await state.update_data(chat_id=chat_id)
    await db.set_pending_rules_input(uid, chat_id)
    settings = await db.get_chat_settings(chat_id)
    preview = html.escape((settings.get("rules_text") or "")[:500])
    await callback.message.edit_text(
        f"{E['rules']} <b>Правила чата</b>\n\n"
        f"Отправьте новый текст правил следующим сообщением или .txt файлом.\n"
        f"Отмена: /cancel\n\n"
        f"<b>Текущие:</b>\n<pre>{preview or '(пусто)'}</pre>",
    )
    await callback.answer()


@router.message(
    PendingRulesFilter(),
    F.chat.type == ChatType.PRIVATE,
    ~(F.text.startswith("/") & ~F.text.startswith("/cancel")),
)
async def receive_rules(message: Message, state: FSMContext, db: Database) -> None:
    if not message.from_user:
        return

    if message.text and message.text.strip().lower() == "/cancel":
        await _cancel_rules_input(state, db, message.from_user.id)
        owner = await is_owner(message.from_user.id)
        await message.answer("Редактирование правил отменено.", reply_markup=admin_main_keyboard(owner))
        return

    rules_text = await _extract_rules_text(message)
    if rules_text is None:
        if message.document:
            await message.answer("Нужен файл .txt с правилами.")
        else:
            await message.answer("Отправьте текст правил или .txt файл. Отмена: /cancel")
        return

    chat_id = await db.get_pending_rules_input(message.from_user.id)
    if not chat_id or not await can_manage_chat(db, message.from_user.id, chat_id):
        await _cancel_rules_input(state, db, message.from_user.id)
        await message.answer("Не удалось определить чат. Откройте чат снова и нажмите «Правила».")
        return

    await db.update_chat_rules(chat_id, rules_text)
    await _cancel_rules_input(state, db, message.from_user.id)
    owner = await is_owner(message.from_user.id)
    logger.info(
        "Rules updated for chat %s by user %s (%s chars)",
        chat_id,
        message.from_user.id,
        len(rules_text),
    )
    await message.answer(
        f"{E['check']} Правила обновлены ({len(rules_text)} символов)",
        reply_markup=admin_main_keyboard(owner),
    )


@router.callback_query(F.data.startswith("chat_punish:"))
async def cb_chat_punishments(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    chat_id = int(callback.data.split(":")[1])
    if not await can_manage_chat(db, callback.from_user.id, chat_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    punishments = await db.get_chat_punishment_history(chat_id)
    from bot.handlers.admin import _format_punishments_list
    from bot.keyboards.punishment import punishments_history_keyboard

    text = _format_punishments_list(punishments, title=f"История чата {chat_id}")
    await callback.message.edit_text(
        text,
        reply_markup=punishments_history_keyboard(punishments, f"chat:{chat_id}"),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:stats")
async def cb_stats(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    stats = await db.get_moderation_stats(days=7)
    owner = await is_owner(callback.from_user.id)
    text = (
        f"{E['chart']} <b>Статистика за 7 дней</b>\n\n"
        f"🕊 Помилован: <b>{stats.get('pardon', 0)}</b>\n"
        f"🚫 Наказан: <b>{stats.get('punish', 0)}</b>"
    )
    await callback.message.edit_text(text, reply_markup=admin_main_keyboard(owner))
    await callback.answer()


# --- Sub-admins (owner only) ---

@router.callback_query(F.data == "admin:subadmins")
async def cb_subadmins(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user or not await is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    admins = await db.list_sub_admins()
    await callback.message.edit_text(
        f"{E['crown']} <b>Суб-админы</b>\n\nУправляют только своими чатами.",
        reply_markup=subadmins_keyboard(admins),
    )
    await callback.answer()


@router.callback_query(F.data == "subadmin_add")
async def cb_subadmin_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_subadmin_id)
    await callback.message.edit_text(
        "👤 Отправьте Telegram ID нового суб-админа:\n\n<i>Узнать ID: @userinfobot</i>"
    )
    await callback.answer()


@router.message(StateFilter(AdminStates.waiting_subadmin_id), F.chat.type == ChatType.PRIVATE)
async def receive_subadmin_id(message: Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Отправьте числовой Telegram ID")
        return
    await state.update_data(new_admin_id=int(message.text.strip()))
    await state.set_state(AdminStates.waiting_subadmin_limit)
    await message.answer("📊 Укажите дневной лимит запросов (например 500):")


@router.message(StateFilter(AdminStates.waiting_subadmin_limit), F.chat.type == ChatType.PRIVATE)
async def receive_subadmin_limit(message: Message, state: FSMContext, db: Database) -> None:
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Отправьте число")
        return
    data = await state.get_data()
    admin_id = data["new_admin_id"]
    limit = int(message.text.strip())
    await db.add_sub_admin(admin_id, limit)
    await state.clear()
    await message.answer(
        f"{E['check']} Суб-админ <code>{admin_id}</code> добавлен. Лимит: <b>{limit}</b>/день",
        reply_markup=admin_main_keyboard(True),
    )


@router.callback_query(F.data.startswith("subadmin:"))
async def cb_subadmin_detail(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user or not await is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    user_id = int(callback.data.split(":")[1])
    admin = await db.get_sub_admin(user_id)
    if not admin:
        await callback.answer("Не найден", show_alert=True)
        return
    used = await db.get_admin_daily_usage(user_id)
    chats = await db.list_chats_for_admin(user_id, False)
    text = (
        f"👤 <b>{admin.get('display_name') or user_id}</b>\n"
        f"ID: <code>{user_id}</code>\n"
        f"Лимит: <b>{used}</b>/{admin['daily_limit']} сегодня\n"
        f"Чатов: <b>{len(chats)}</b>"
    )
    await callback.message.edit_text(text, reply_markup=subadmin_detail_keyboard(user_id))
    await callback.answer()


@router.callback_query(F.data.startswith("subadmin_del:"))
async def cb_subadmin_del(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user or not await is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    user_id = int(callback.data.split(":")[1])
    await db.remove_sub_admin(user_id)
    await callback.answer("Удалён")
    await cb_subadmins(callback, db)


@router.callback_query(F.data.startswith("subadmin_limit:"))
async def cb_subadmin_limit(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    user_id = int(callback.data.split(":")[1])
    await state.set_state(AdminStates.editing_subadmin_limit)
    await state.update_data(edit_admin_id=user_id)
    await callback.message.edit_text(f"📊 Новый дневной лимит для <code>{user_id}</code>:")
    await callback.answer()


@router.message(StateFilter(AdminStates.editing_subadmin_limit), F.chat.type == ChatType.PRIVATE)
async def receive_subadmin_new_limit(message: Message, state: FSMContext, db: Database) -> None:
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Отправьте число")
        return
    data = await state.get_data()
    user_id = data["edit_admin_id"]
    limit = int(message.text.strip())
    await db.update_sub_admin_limit(user_id, limit)
    await state.clear()
    await message.answer(f"{E['check']} Лимит обновлён: <b>{limit}</b>/день", reply_markup=admin_main_keyboard(True))
