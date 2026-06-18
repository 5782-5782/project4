import json
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, ChatMemberAdministrator, ChatMemberOwner, Message

from bot.db.database import Database
from bot.keyboards.punishment import (
    history_only_keyboard,
    reason_only_keyboard,
    unpunish_only_keyboard,
)
from bot.services.batch import BatchProcessor
from bot.services.chat_history import from_telegram
from bot.services.context import ContextBuilder
from bot.services.gemini import GeminiAuthError, RateLimitExhausted
from bot.services.moderation import ModerationService, format_decision_preview
from bot.ui.emoji import E
from bot.utils.access import can_access_dm, can_manage_chat, is_owner
from bot.utils.punishment_access import can_manage_punishment
from bot.utils.chat_roles import get_chat_roles
from bot.utils.punishment_button_spam import format_callback_user, guard_reason_button
from bot.utils.punishment_message import (
    append_status_line,
    build_punishments_list_view,
    deliver_punishment_reason,
    extract_reason_from_message,
    get_punishments_list_back,
    is_private_punishments_list,
)
from bot.utils.telegram_edit import (
    edit_message_markup_safe,
    edit_message_status_and_keyboard,
    safe_edit_message,
)
from bot.utils.punishment_time import format_punishment_moment

logger = logging.getLogger(__name__)
router = Router()


class ReplyToSetrulesFilter(BaseFilter):
    """Only match replies to the bot's /setrules prompt."""

    async def __call__(self, message: Message) -> bool:
        reply = message.reply_to_message
        if not reply or not reply.text:
            return False
        return "/setrules" in reply.text


async def _can_manage(message: Message, db: Database) -> bool:
    if not message.from_user:
        return False
    uid = message.from_user.id
    if await is_owner(uid):
        return True
    if await can_access_dm(db, uid):
        return await can_manage_chat(db, uid, message.chat.id)
    member = await message.bot.get_chat_member(message.chat.id, uid)
    return isinstance(member, (ChatMemberOwner, ChatMemberAdministrator))


@router.message(Command("modtest"))
async def cmd_modtest(
    message: Message,
    db: Database,
    moderation: ModerationService,
    batch_processor: BatchProcessor,
) -> None:
    """Force AI check on a replied message and show the result (owner/admins only)."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not message.from_user or not await _can_manage(message, db):
        await message.answer("Только администратор чата или владелец бота.")
        return
    if not message.reply_to_message:
        await message.answer(
            f"{E['info']} Ответьте <b>/modtest</b> на сообщение, которое нужно проверить ИИ."
        )
        return
    target = message.reply_to_message
    if not target.from_user or target.from_user.is_bot:
        await message.answer("Нужно ответить на сообщение пользователя.")
        return
    if not target.text and not target.caption:
        await message.answer("У сообщения должен быть текст.")
        return

    settings = await db.get_chat_settings(message.chat.id)
    if not settings.get("moderation_enabled", 1):
        await message.answer("Модерация выключена. Включите: /mod on")
        return

    status = await message.answer(f"{E['robot']} Проверяю сообщение через ИИ…")
    try:
        await batch_processor.store_history(message)
        await batch_processor.store_history(target)
        history = await batch_processor.get_history(message.chat.id)
        stored = from_telegram(target) or await db.get_chat_message(
            message.chat.id, target.message_id
        )
        if not stored:
            await status.edit_text(f"{E['ban']} Не удалось загрузить сообщение для контекста.")
            return
        ctx = ContextBuilder().build(stored, history)
        chat_roles = await get_chat_roles(message.bot, message.chat.id)
        decision = await moderation.analyze(
            message.bot,
            message.chat.id,
            settings.get("rules_text", ""),
            target.message_id,
            ctx,
            admin_user_id=message.from_user.id,
            chat_roles=chat_roles,
        )
        decision = moderation.enrich_decision(decision, target, chat_roles)
        preview = format_decision_preview(decision)
        await status.edit_text(
            f"{E['robot']} <b>Тест ИИ-модерации</b> (наказание не применяется)\n\n{preview}"
        )
    except GeminiAuthError as exc:
        logger.warning("modtest auth failed chat=%s: %s", message.chat.id, exc)
        await status.edit_text(
            f"{E['ban']} <b>Ключ Gemini не работает</b>\n\n"
            f"{exc}\n\n"
            f"<i>После замены ключа: sudo bot restart mod</i>"
        )
    except RateLimitExhausted as exc:
        logger.warning("modtest quota failed chat=%s: %s", message.chat.id, exc)
        await status.edit_text(
            f"{E['ban']} <b>Лимит Gemini исчерпан</b>\n\n"
            f"Попробуйте позже или добавьте второй API-ключ в secrets.json."
        )
    except Exception as exc:
        logger.exception("modtest failed chat=%s", message.chat.id)
        await status.edit_text(
            f"{E['ban']} Ошибка ИИ: <code>{type(exc).__name__}</code>\n\n"
            f"<i>Проверьте /admin → Лимиты API и ключи Gemini.</i>"
        )


@router.message(Command("punishments"))
async def cmd_punishments(message: Message, db: Database) -> None:
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Команда работает только в группах.")
        return
    punishments = await db.get_active_punishments(message.chat.id)
    text = _format_active(punishments)
    await message.answer(text)


@router.message(Command("setrules"))
async def cmd_setrules(message: Message, db: Database) -> None:
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not await _can_manage(message, db):
        await message.answer("Только администратор чата или владелец бота.")
        return
    await message.answer(
        f"{E['rules']} Ответьте на это сообщение текстом правил или прикрепите .txt файл."
    )


@router.message(ReplyToSetrulesFilter(), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def rules_reply(message: Message, db: Database) -> None:
    if not await _can_manage(message, db):
        return

    rules_text = ""
    if message.document:
        if not message.document.file_name or not message.document.file_name.endswith(".txt"):
            await message.answer("Нужен файл .txt")
            return
        file = await message.bot.get_file(message.document.file_id)
        data = await message.bot.download_file(file.file_path)
        rules_text = data.read().decode("utf-8", errors="replace")
    elif message.text:
        rules_text = message.text
    else:
        await message.answer("Отправьте текст или .txt файл.")
        return

    await db.update_chat_rules(message.chat.id, rules_text)
    await message.answer(f"{E['check']} Правила чата обновлены ({len(rules_text)} символов).")


@router.message(Command("setinterval"))
async def cmd_setinterval(message: Message, db: Database) -> None:
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not await _can_manage(message, db):
        await message.answer("Только администратор чата или владелец бота.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        settings = await db.get_chat_settings(message.chat.id)
        await message.answer(
            f"Текущий интервал: <b>{settings['batch_interval']}</b> сек.\n"
            f"Слоты по UTC: :00, :30 (при 30с) — пачка сообщений → 1 запрос Gemini.\n"
            f"Использование: /setinterval 30"
        )
        return
    try:
        interval = int(parts[1])
        if interval < 0:
            raise ValueError
    except ValueError:
        await message.answer("Укажите число секунд (0 или больше).")
        return
    await db.update_batch_interval(message.chat.id, interval)
    if interval == 0:
        desc = "каждое сообщение отдельно (1 запрос)"
    else:
        desc = f"слоты каждые {interval} сек (пачка → 1 запрос Gemini)"
    await message.answer(f"{E['check']} Интервал батчинга: <b>{desc}</b>")


@router.message(Command("mod"))
async def cmd_mod(message: Message, db: Database) -> None:
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not await _can_manage(message, db):
        await message.answer("Только администратор чата или владелец бота.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        settings = await db.get_chat_settings(message.chat.id)
        status = "включена" if settings.get("moderation_enabled") else "выключена"
        await message.answer(f"Модерация {status}. /mod on | /mod off")
        return
    enabled = parts[1].lower() == "on"
    await db.set_moderation_enabled(message.chat.id, enabled)
    await message.answer(f"{E['shield']} Модерация {'включена' if enabled else 'выключена'}.")


@router.callback_query(F.data.startswith("unpunish:"))
async def cb_unpunish(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer("Ошибка", show_alert=True)
        return

    punishment_id = int(callback.data.split(":")[1])
    punishment = await db.get_punishment(punishment_id)
    if not punishment:
        await callback.answer("Наказание не найдено", show_alert=True)
        return
    if not punishment.active:
        await edit_message_markup_safe(callback.message, history_only_keyboard(punishment_id))
        await callback.answer("Наказание уже снято", show_alert=True)
        return

    if not await can_manage_punishment(db, bot, punishment, callback.from_user.id):
        await callback.answer(
            "Снять наказание может только пострадавший или администратор чата",
            show_alert=True,
        )
        return

    await _lift_mute(bot, punishment.chat_id, punishment.user_id, punishment.punishment_type)
    await db.deactivate_punishment(punishment_id)
    original = callback.message.text or callback.message.caption or ""
    status = f"{E['pardon']} <b>Наказание снято</b> пользователем {callback.from_user.full_name}"
    await edit_message_status_and_keyboard(
        callback.message,
        append_status_line(original, status),
        history_only_keyboard(punishment_id),
    )
    await callback.answer("Наказание снято!")


@router.callback_query(F.data.startswith("punish_reason:"))
async def cb_punish_reason(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer("Ошибка", show_alert=True)
        return
    if not await guard_reason_button(callback, db, bot):
        return

    punishment_id = int(callback.data.split(":")[1])
    punishment = await db.get_punishment(punishment_id)
    if not punishment:
        await callback.answer("Запись не найдена", show_alert=True)
        return

    await deliver_punishment_reason(
        bot,
        callback.message.chat.id,
        punishment.explanation,
        callback.answer,
        source_message=callback.message,
        clicked_by=format_callback_user(callback.from_user),
    )


@router.callback_query(F.data.startswith("punish_del:"))
async def cb_punish_del(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer("Ошибка", show_alert=True)
        return

    punishment_id = int(callback.data.split(":")[1])
    punishment = await db.get_punishment(punishment_id)
    if not punishment:
        await callback.answer("Запись не найдена", show_alert=True)
        return

    if not await can_manage_punishment(db, bot, punishment, callback.from_user.id):
        await callback.answer(
            "Удалить из истории может пострадавший или администратор чата",
            show_alert=True,
        )
        return

    if punishment.hidden_from_history:
        await callback.answer("Уже удалено из истории", show_alert=True)
        return

    await db.hide_punishment_from_history(punishment_id)

    if is_private_punishments_list(callback.message):
        back_data = get_punishments_list_back(callback.message.reply_markup)
        assert back_data is not None
        text, markup = await build_punishments_list_view(db, callback.from_user.id, back_data)
        await safe_edit_message(callback.message, text, markup)
        await callback.answer("Удалено из истории")
        return

    if punishment.active:
        await edit_message_markup_safe(callback.message, unpunish_only_keyboard(punishment_id))
    else:
        await edit_message_markup_safe(callback.message, reason_only_keyboard(punishment_id))
    await callback.answer("Удалено из истории")


@router.callback_query(F.data == "punish_done:reason")
async def cb_punish_done_reason(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer("Ошибка", show_alert=True)
        return
    if not await guard_reason_button(callback, db, bot):
        return

    text = callback.message.text or callback.message.caption or ""
    explanation = extract_reason_from_message(text)
    if not explanation:
        await callback.answer("Причина не найдена", show_alert=True)
        return

    await deliver_punishment_reason(
        bot,
        callback.message.chat.id,
        explanation,
        callback.answer,
        source_message=callback.message,
        clicked_by=format_callback_user(callback.from_user),
    )


@router.callback_query(F.data.startswith("punish_done:"))
async def cb_punish_done(callback: CallbackQuery) -> None:
    await callback.answer("Запись уже обработана")


async def _lift_mute(bot: Bot, chat_id: int, user_id: int, punishment_type: str) -> None:
    if punishment_type != "mute":
        return
    from aiogram.types import ChatPermissions

    await bot.restrict_chat_member(
        chat_id,
        user_id,
        permissions=ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        ),
    )


def _format_active(punishments) -> str:
    if not punishments:
        return f"{E['check']} <b>Активные наказания</b>\n\nНет действующих наказаний."
    lines = [f"{E['ban']} <b>Активные наказания</b>\n"]
    for p in punishments:
        refs = json.loads(p.rule_references) if p.rule_references.startswith("[") else [p.rule_references]
        dur = f" ({p.duration_minutes} мин)" if p.duration_minutes else ""
        lines.append(
            f"\n<b>#{p.id}</b> — <code>{p.user_id}</code>\n"
            f"Тип: {p.punishment_type}{dur}\n"
            f"🕐 {format_punishment_moment(p.created_at)}\n"
            f"Правила: {', '.join(refs)}"
        )
    return "\n".join(lines)
