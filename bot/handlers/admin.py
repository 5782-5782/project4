import json
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.config import get_settings
from bot.db.database import Database
from bot.keyboards.punishment import admin_main_keyboard, interval_keyboard
from bot.services.gemini import GeminiService
from bot.ui.emoji import E

logger = logging.getLogger(__name__)
router = Router()


def is_owner(user_id: int) -> bool:
    return user_id == get_settings().owner_id


@router.message(Command("start"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    if is_owner(uid):
        await message.answer(
            f"{E['crown']} <b>Добро пожаловать, администратор!</b>\n\n"
            f"Используйте /admin для панели управления.\n"
            f"Добавьте бота в группу и назначьте администратором с правом ограничивать участников.",
            reply_markup=admin_main_keyboard(),
        )
        return

    if await db.is_dm_banned(uid):
        return

    await db.record_dm_message(uid)
    await message.answer(f"{E['block']} Не лезь, бот не для вас.")


@router.message(F.chat.type == ChatType.PRIVATE)
async def private_messages(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    if is_owner(uid):
        return
    if await db.is_dm_banned(uid):
        return
    banned = await db.record_dm_message(uid)
    if banned:
        await message.answer(f"{E['ban']} Вы заблокированы на неделю за спам.")
        return
    await message.answer(f"{E['block']} Не лезь, бот не для вас.")


@router.message(Command("admin"), F.chat.type == ChatType.PRIVATE)
async def cmd_admin(message: Message, gemini: GeminiService) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        return
    text = await gemini.get_limits_dashboard()
    await message.answer(text, reply_markup=admin_main_keyboard())


@router.callback_query(F.data == "admin:limits")
async def cb_limits(callback: CallbackQuery, gemini: GeminiService) -> None:
    if not callback.from_user or not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    text = await gemini.get_limits_dashboard()
    await callback.message.edit_text(text, reply_markup=admin_main_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:back")
async def cb_back(callback: CallbackQuery, gemini: GeminiService) -> None:
    if not callback.from_user or not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    text = await gemini.get_limits_dashboard()
    await callback.message.edit_text(text, reply_markup=admin_main_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:interval")
async def cb_interval_menu(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        f"{E['clock']} <b>Интервал батчинга</b>\n\n"
        "Выберите интервал для группы (применяется к чату, из которого вызвано):\n"
        "<i>0 = каждое сообщение отдельно</i>",
        reply_markup=interval_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("interval:"))
async def cb_set_interval(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user or not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    interval = int(callback.data.split(":")[1])
    # For DM admin, store for a "default" chat or ask - we'll use a sentinel
    # Owner sets via /setinterval in group; here show confirmation
    await callback.answer(f"Интервал {interval}с. Используйте /setinterval {interval} в группе.", show_alert=True)


@router.callback_query(F.data == "admin:rules")
async def cb_rules_help(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        f"{E['rules']} <b>Загрузка правил</b>\n\n"
        "В группе отправьте команду:\n"
        "<code>/setrules</code> — затем ответьте на это сообщение текстом правил\n\n"
        "Или отправьте документ .txt в ответ на <code>/setrules</code>",
        reply_markup=admin_main_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:toggle_mod")
async def cb_toggle_help(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        f"{E['shield']} <b>Модерация</b>\n\n"
        "В группе: <code>/mod on</code> или <code>/mod off</code>",
        reply_markup=admin_main_keyboard(),
    )
    await callback.answer()


@router.message(Command("allpunishments"), F.chat.type == ChatType.PRIVATE)
async def cmd_all_punishments(message: Message, db: Database) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        return
    punishments = await db.get_all_punishments(limit=30)
    text = _format_punishments_list(punishments, title="Все наказания (последние 30)")
    await message.answer(text, reply_markup=admin_main_keyboard())


@router.callback_query(F.data == "admin:all_punishments")
async def cb_all_punishments(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user or not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    punishments = await db.get_all_punishments(limit=30)
    text = _format_punishments_list(punishments, title="Все наказания (последние 30)")
    await callback.message.edit_text(text, reply_markup=admin_main_keyboard())
    await callback.answer()


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
