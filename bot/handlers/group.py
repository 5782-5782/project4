import json
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, ChatMemberAdministrator, ChatMemberOwner, Message

from bot.db.database import Database
from bot.keyboards.punishment import unpunish_keyboard
from bot.services.batch import BatchProcessor
from bot.services.context import ContextBuilder
from bot.services.gemini import GeminiAuthError, RateLimitExhausted
from bot.services.moderation import ModerationService, format_decision_preview
from bot.ui.emoji import E
from bot.utils.access import can_access_dm, can_manage_chat, is_owner

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
        batch_processor.store_history(message.chat.id, target)
        history = batch_processor.get_history(message.chat.id)
        ctx = ContextBuilder().build(target, history)
        decision = await moderation.analyze(
            message.chat.id,
            settings.get("rules_text", ""),
            target.message_id,
            ctx,
            admin_user_id=message.from_user.id,
        )
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
        await message.answer(f"Текущий интервал: <b>{settings['batch_interval']}</b> сек.\nИспользование: /setinterval 30")
        return
    try:
        interval = int(parts[1])
        if interval < 0:
            raise ValueError
    except ValueError:
        await message.answer("Укажите число секунд (0 или больше).")
        return
    await db.update_batch_interval(message.chat.id, interval)
    desc = "каждое сообщение отдельно" if interval == 0 else f"каждые {interval} сек"
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
        await callback.answer("Наказание уже снято", show_alert=True)
        return

    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    can_unpunish_ids = json.loads(punishment.can_unpunish_ids)
    allowed = user_id in can_unpunish_ids or await is_owner(user_id)

    if not allowed:
        member = await bot.get_chat_member(chat_id, user_id)
        if isinstance(member, (ChatMemberOwner, ChatMemberAdministrator)):
            allowed = True

    if not allowed:
        await callback.answer(
            "Снять наказание может только пострадавший или администратор чата",
            show_alert=True,
        )
        return

    if punishment.punishment_type == "mute":
        from aiogram.types import ChatPermissions

        await bot.restrict_chat_member(
            chat_id,
            punishment.user_id,
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

    await db.deactivate_punishment(punishment_id)
    original = callback.message.text or callback.message.caption or ""
    await callback.message.edit_text(
        f"{original}\n\n{E['pardon']} <b>Наказание снято</b> пользователем {callback.from_user.full_name}",
        reply_markup=None,
    )
    await callback.answer("Наказание снято!")


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
            f"Правила: {', '.join(refs)}"
        )
    return "\n".join(lines)
