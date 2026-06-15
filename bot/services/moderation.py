import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from aiogram.types import ChatPermissions

from bot.config import get_settings
from bot.db.database import Database, Punishment
from bot.services.context import ContextBuilder, ModerationContext
from bot.services.gemini import GeminiService, parse_moderation_response

logger = logging.getLogger(__name__)

MODERATION_SYSTEM = """Ты — AI-модератор Telegram-чата. Анализируй сообщения строго по правилам чата и этическим нормам.

ЭТИЧЕСКИЕ НОРМЫ (приоритет при помиловании):
- Не наказывай за явные шутки между друзьями без злого умысла
- Учитывай контекст и тон беседы
- Различай конструктивную критику и оскорбления
- При сомнении — устное предупреждение вместо жёсткого наказания
- Учитывай повторные нарушения: при рецидиве ужесточай наказание

ПРАВИЛА ЧАТА:
{rules}

ПРОШЛЫЕ НАКАЗАНИЯ УЧАСТНИКОВ (за последний месяц):
{past_punishments}

КОНТЕКСТ ПЕРЕПИСКИ:
{context}

ЗАДАЧА:
Проанализируй ОБРАБАТЫВАЕМОЕ СООБЩЕНИЕ. Определи:
1. Нарушены ли правила чата?
2. Кто нарушитель (user_id)?
3. Если правила требуют наказания, но этика позволяет простить — выдай помилование с устным предупреждением
4. При наказании укажи конкретные пункты/названия правил
5. Укажи user_id тех, кто может снять наказание (пострадавшие, адресаты оскорблений)

Ответь ТОЛЬКО валидным JSON:
{{
  "action": "none" | "pardon" | "punish",
  "violator_user_id": null или число,
  "violator_display": "имя/username нарушителя или null",
  "rule_references": ["п. 3.2 Запрет оскорблений", "..."],
  "punishment_type": null | "warning" | "mute",
  "duration_minutes": null или число (для mute),
  "warning_text": "текст устного предупреждения при помиловании",
  "explanation": "краткое объяснение решения",
  "can_unpunish_user_ids": [список user_id],
  "reply_to_message_id": id сообщения для ответа
}}

Действия:
- "none" — нарушений НЕ обнаружено, правила не нарушены. Ничего не предпринимать.
- "pardon" — формально нарушение есть, но помиловать с устным предупреждением.
- "punish" — выдать наказание (мут/предупреждение).
"""


class ModerationService:
    def __init__(self, db: Database, gemini: GeminiService) -> None:
        self.db = db
        self.gemini = gemini
        self.context_builder = ContextBuilder()

    async def analyze(
        self,
        chat_id: int,
        rules_text: str,
        target_message_id: int,
        context: ModerationContext,
        admin_user_id: int | None = None,
    ) -> dict[str, Any]:
        past = await self.db.get_punishments_for_users(
            chat_id, list(context.participant_ids), days=30
        )
        past_text = _format_past_punishments(past)
        prompt = MODERATION_SYSTEM.format(
            rules=rules_text or "(правила не заданы — используй этические нормы)",
            past_punishments=past_text,
            context=self.context_builder.format_for_prompt(context),
        )
        raw = await self.gemini.generate(prompt, admin_user_id=admin_user_id)
        result = parse_moderation_response(raw)
        result["reply_to_message_id"] = result.get("reply_to_message_id") or target_message_id
        return result

    async def apply_decision(
        self,
        bot: Bot,
        chat_id: int,
        decision: dict[str, Any],
        message_id: int | None = None,
    ) -> Punishment | None:
        action = decision.get("action", "none")
        explanation = decision.get("explanation", "")
        rule_refs = decision.get("rule_references") or []

        await self.db.log_moderation(
            chat_id=chat_id,
            message_id=message_id or decision.get("reply_to_message_id"),
            action=action,
            explanation=explanation,
            rule_references=rule_refs,
        )

        if action == "none":
            settings = get_settings()
            if settings.log_clean_checks:
                logger.info("Chat %s: no violation (msg %s) — %s", chat_id, message_id, explanation)
            return None
        can_unpunish = decision.get("can_unpunish_user_ids") or []
        reply_id = decision.get("reply_to_message_id")
        violator_id = decision.get("violator_user_id")

        if action == "pardon":
            warning = decision.get("warning_text") or "Вы нарушили правила, но вас решили помиловать. Впредь будьте осторожны."
            rules_str = "; ".join(rule_refs) if rule_refs else "общие нормы"
            text = (
                f"🕊 <b>Устное предупреждение</b>\n\n"
                f"{warning}\n\n"
                f"📜 <b>Правила:</b> {rules_str}\n"
                f"💬 {explanation}"
            )
            await bot.send_message(chat_id, text, reply_to_message_id=reply_id)
            return None

        if action == "punish" and violator_id:
            ptype = decision.get("punishment_type", "mute")
            duration = decision.get("duration_minutes") or 30
            expires = None
            if ptype == "mute":
                expires = datetime.now(timezone.utc) + timedelta(minutes=duration)
                await bot.restrict_chat_member(
                    chat_id,
                    violator_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=expires,
                )

            display = decision.get("violator_display", str(violator_id))
            rules_str = "\n".join(f"• {r}" for r in rule_refs) if rule_refs else "• нарушение правил чата"
            ptype_label = "Мут" if ptype == "mute" else "Предупреждение"
            duration_str = f" на <b>{duration} мин</b>" if ptype == "mute" else ""

            punishment_id = await self.db.add_punishment(
                chat_id=chat_id,
                user_id=violator_id,
                username=display if display.startswith("@") else None,
                punishment_type=ptype,
                duration_minutes=duration if ptype == "mute" else None,
                rule_references=rule_refs,
                explanation=explanation,
                message_id=reply_id,
                can_unpunish_ids=[int(x) for x in can_unpunish if x],
                expires_at=expires,
            )

            from bot.keyboards.punishment import unpunish_keyboard

            text = (
                f"🚫 <b>{ptype_label}{duration_str}</b>\n\n"
                f"👤 Нарушитель: <b>{display}</b> (<code>{violator_id}</code>)\n"
                f"📜 <b>Правила:</b>\n{rules_str}\n\n"
                f"💬 {explanation}"
            )
            sent = await bot.send_message(
                chat_id,
                text,
                reply_to_message_id=reply_id,
                reply_markup=unpunish_keyboard(punishment_id),
            )
            return await self.db.get_punishment(punishment_id)

        return None


def format_decision_preview(decision: dict[str, Any]) -> str:
    action = decision.get("action", "none")
    explanation = decision.get("explanation", "")
    rule_refs = decision.get("rule_references") or []
    rules_str = "; ".join(rule_refs) if rule_refs else "—"

    if action == "none":
        return (
            f"✅ <b>Нарушений нет</b>\n\n"
            f"💬 {explanation or 'Сообщение соответствует правилам.'}"
        )
    if action == "pardon":
        warning = decision.get("warning_text") or "Устное предупреждение"
        return (
            f"🕊 <b>Помилование</b>\n\n"
            f"📜 Правила: {rules_str}\n"
            f"⚠️ {warning}\n\n"
            f"💬 {explanation}"
        )
    if action == "punish":
        ptype = decision.get("punishment_type", "mute")
        duration = decision.get("duration_minutes")
        violator = decision.get("violator_display") or decision.get("violator_user_id")
        duration_str = f" на {duration} мин" if duration else ""
        return (
            f"🚫 <b>Наказание: {ptype}{duration_str}</b>\n\n"
            f"👤 Нарушитель: <b>{violator}</b>\n"
            f"📜 Правила: {rules_str}\n\n"
            f"💬 {explanation}"
        )
    return f"❓ Неизвестное действие: <code>{action}</code>\n\n💬 {explanation}"


def _format_past_punishments(punishments: list[Punishment]) -> str:
    if not punishments:
        return "(нет наказаний за последний месяц)"
    lines = []
    for p in punishments[:20]:
        refs = json.loads(p.rule_references) if p.rule_references.startswith("[") else [p.rule_references]
        refs_str = ", ".join(refs)
        active = "активно" if p.active else "снято"
        lines.append(
            f"- user_id={p.user_id}, тип={p.punishment_type}, "
            f"правила: {refs_str}, статус: {active}, дата: {p.created_at[:10]}"
        )
    return "\n".join(lines)
