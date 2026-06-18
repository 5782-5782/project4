import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from aiogram.types import ChatPermissions, Message

from bot.config import get_settings
from bot.db.database import Database, Punishment
from bot.services.context import ContextBuilder, ModerationContext, BatchModerationContext
from bot.services.gemini import GeminiService, parse_moderation_response
from bot.utils.chat_members import (
    LOOKUP_FOLLOWUP_BATCH,
    LOOKUP_FOLLOWUP_SINGLE,
    extract_lookup_requests,
    extract_mentioned_usernames,
    get_chat_members_context,
    is_lookup_response,
    lookup_participant_by_username,
    resolve_user_lookups,
)
from bot.utils.chat_roles import ChatRolesInfo
from bot.utils.forum_topic import topic_send_kwargs
from bot.utils.punishment_time import format_punishment_moment

logger = logging.getLogger(__name__)

MODERATION_SYSTEM = """Ты — AI-модератор Telegram-чата. Анализируй сообщения строго по правилам чата и этическим нормам.

ЭТИЧЕСКИЕ НОРМЫ (приоритет при помиловании):
- Не наказывай за явные шутки между друзьями без злого умысла
- Учитывай контекст и тон беседы
- Различай конструктивную критику и оскорбления
- При сомнении — устное предупреждение вместо жёсткого наказания
- Учитывай повторные нарушения: при рецидиве ужесточай наказание
- Учитывай ДАВНОСТЬ прошлых наказаний (см. дату и «сколько назад»):
  * до 3 дней — свежее нарушение, будь строже при повторе
  * 4–14 дней — учитывай, но не ужесточай без явного рецидива
  * более 2 недель — старые наказания смягчают оценку при мелком нарушении
  * повтор того же типа в течение 7 дней после наказания — рецидив, ужесточай

ПРАВИЛА ЧАТА:
{rules}

ПРОШЛЫЕ НАКАЗАНИЯ УЧАСТНИКОВ (за последние 30 дней, с точным временем):
{past_punishments}

РОЛИ В ЧАТЕ (Telegram):
{chat_roles}

УЧАСТНИКИ ЧАТА:
{chat_members}

ВЛАДЕЛЕЦ, АДМИНИСТРАТОРЫ И КОМАНДЫ МОДЕРАЦИИ:
- В контексте у владельца и администраторов отмечена роль в квадратных скобках.
- Владелец и администраторы могут отдавать команды модерации: «мут 10 минут», «замутить», «бан», «предупреждение» и т.п. (см. также правила чата).
- Если автор — владелец/админ и сообщение явно команда модерации в адрес участника (часто ответ reply на его сообщение):
  * action="punish"
  * violator_user_id = user_id ЦЕЛИ наказания (НЕ автора команды)
  * punishment_type и duration_minutes — по смыслу команды
  * rule_references: «команда модерации» или пункт правил, если указан
  * can_unpunish_user_ids: включи автора команды (владельца/админа)
- Команда модерации по @username (например «мут @nickname 10 мин»):
  * violator_display = "@nickname" (точный username из команды)
  * violator_user_id = user_id ЭТОГО username из раздела УЧАСТНИКИ — копируй, не выдумывай
  * Если нужного @username нет в УЧАСТНИКИ — action="lookup_users" с этим username, не угадывай id
  * violator_user_id и @username в violator_display должны быть одним человеком
- Если владелец/админ сам нарушает правила (оскорбления и т.д.) — оценивай по правилам чата; НЕ игнорируй автоматически. Если правила допускают снисхождение — pardon или none.
- Обычная речь владельца/админа без нарушения и без команды — action="none".
- violator_user_id всегда указывает на того, к кому применяется решение (нарушитель или цель команды).

КОНТЕКСТ ПЕРЕПИСКИ:
{context}

ЗАДАЧА:
Проанализируй ОБРАБАТЫВАЕМОЕ СООБЩЕНИЕ. Определи:
1. Нарушены ли правила чата?
2. Кто нарушитель (user_id)?
3. Если правила требуют наказания, но этика позволяет простить — выдай помилование с устным предупреждением
4. При наказании укажи конкретные пункты/названия правил
5. Укажи user_id тех, кто может снять наказание (пострадавшие, адресаты оскорблений)
6. Укажи affected_users — кому адресовано нарушение (оскорбление, угроза, хамство в ответ и т.д.)

Ответь ТОЛЬКО валидным JSON:
{{
  "action": "none" | "pardon" | "punish",
  "violator_user_id": null или число,
  "violator_display": "имя/username нарушителя или null",
  "violator_username": "@username цели или null (при муте по username)",
  "affected_users": [{{"user_id": число, "display": "имя или @username"}}],
  "rule_references": ["п. 3.2 Запрет оскорблений", "..."],
  "punishment_type": null | "warning" | "mute",
  "duration_minutes": null или число (для mute),
  "warning_text": "текст устного предупреждения при помиловании",
  "explanation": "краткое объяснение решения",
  "can_unpunish_user_ids": [список user_id — обязательно включи пострадавших из affected_users],
  "reply_to_message_id": id сообщения для ответа
}}

Действия:
- "none" — нарушений НЕ обнаружено, правила не нарушены. Ничего не предпринимать.
- "pardon" — формально нарушение есть, но помиловать с устным предупреждением.
- "punish" — выдать наказание (мут/предупреждение).
"""

BATCH_MODERATION_SYSTEM = """Ты — AI-модератор Telegram-чата. Анализируй сообщения строго по правилам чата и этическим нормам.

ЭТИЧЕСКИЕ НОРМЫ (приоритет при помиловании):
- Не наказывай за явные шутки между друзьями без злого умысла
- Учитывай контекст и тон беседы
- Различай конструктивную критику и оскорбления
- При сомнении — устное предупреждение вместо жёсткого наказания
- Учитывай повторные нарушения: при рецидиве ужесточай наказание
- Учитывай ДАВНОСТЬ прошлых наказаний (см. дату и «сколько назад»):
  * до 3 дней — свежее нарушение, будь строже при повторе
  * 4–14 дней — учитывай, но не ужесточай без явного рецидива
  * более 2 недель — старые наказания смягчают оценку при мелком нарушении
  * повтор того же типа в течение 7 дней после наказания — рецидив, ужесточай

ПРАВИЛА ЧАТА:
{rules}

ПРОШЛЫЕ НАКАЗАНИЯ УЧАСТНИКОВ (за последние 30 дней, с точным временем):
{past_punishments}

РОЛИ В ЧАТЕ (Telegram):
{chat_roles}

УЧАСТНИКИ ЧАТА:
{chat_members}

ВЛАДЕЛЕЦ, АДМИНИСТРАТОРЫ И КОМАНДЫ МОДЕРАЦИИ:
- В контексте у владельца и администраторов отмечена роль в квадратных скобках.
- Владелец и администраторы могут отдавать команды модерации: «мут 10 минут», «замутить», «бан», «предупреждение» и т.п. (см. также правила чата).
- Если автор — владелец/админ и сообщение явно команда модерации в адрес участника (часто ответ reply на его сообщение):
  * action="punish"
  * violator_user_id = user_id ЦЕЛИ наказания (НЕ автора команды)
  * punishment_type и duration_minutes — по смыслу команды
  * rule_references: «команда модерации» или пункт правил, если указан
  * can_unpunish_user_ids: включи автора команды (владельца/админа)
- Команда модерации по @username (например «мут @nickname 10 мин»):
  * violator_display = "@nickname" (точный username из команды)
  * violator_user_id = user_id ЭТОГО username из раздела УЧАСТНИКИ — копируй, не выдумывай
  * Если нужного @username нет в УЧАСТНИКИ — action="lookup_users" с этим username, не угадывай id
  * violator_user_id и @username в violator_display должны быть одним человеком
- Если владелец/админ сам нарушает правила (оскорбления и т.д.) — оценивай по правилам чата; НЕ игнорируй автоматически. Если правила допускают снисхождение — pardon или none.
- Обычная речь владельца/админа без нарушения и без команды — action="none".
- violator_user_id всегда указывает на того, к кому применяется решение (нарушитель или цель команды).

КОНТЕКСТ ПЕРЕПИСКИ:
{context}

ЗАДАЧА:
Проанализируй КАЖДОЕ сообщение из раздела «СООБЩЕНИЯ ДЛЯ АНАЛИЗА (батч)» отдельно.
Для каждого message_id определи:
1. Нарушены ли правила чата?
2. Кто нарушитель (user_id)?
3. Если правила требуют наказания, но этика позволяет простить — выдай помилование с устным предупреждением
4. При наказании укажи конкретные пункты/названия правил
5. Укажи user_id тех, кто может снять наказание (пострадавшие, адресаты оскорблений)
6. Укажи affected_users — кому адресовано нарушение (оскорбление, угроза, хамство в ответ и т.д.)

Ответь ТОЛЬКО валидным JSON:
{{
  "decisions": [
    {{
      "message_id": число,
      "action": "none" | "pardon" | "punish",
      "violator_user_id": null или число,
      "violator_display": "имя/username нарушителя или null",
      "violator_username": "@username цели или null (при муте по username)",
      "affected_users": [{{"user_id": число, "display": "имя или @username"}}],
      "rule_references": ["п. 3.2 Запрет оскорблений", "..."],
      "punishment_type": null | "warning" | "mute",
      "duration_minutes": null или число (для mute),
      "warning_text": "текст устного предупреждения при помиловании",
      "explanation": "краткое объяснение решения",
      "can_unpunish_user_ids": [список user_id — обязательно включи пострадавших из affected_users],
      "reply_to_message_id": id сообщения для ответа
    }}
  ]
}}

Действия:
- "none" — нарушений НЕ обнаружено, правила не нарушены. Ничего не предпринимать.
- "pardon" — формально нарушение есть, но помиловать с устным предупреждением.
- "punish" — выдать наказание (мут/предупреждение).

В массиве decisions должен быть ровно один объект на каждое сообщение из батча.
Для каждого решения поле message_id ОБЯЗАТЕЛЬНО должно совпадать с id из раздела «СООБЩЕНИЯ ДЛЯ АНАЛИЗА».
"""


class ModerationService:
    def __init__(self, db: Database, gemini: GeminiService) -> None:
        self.db = db
        self.gemini = gemini
        self.context_builder = ContextBuilder()

    async def analyze(
        self,
        bot: Bot,
        chat_id: int,
        rules_text: str,
        target_message_id: int,
        context: ModerationContext,
        admin_user_id: int | None = None,
        chat_roles: ChatRolesInfo | None = None,
    ) -> dict[str, Any]:
        past = await self.db.get_punishments_for_users(
            chat_id, list(context.participant_ids)
        )
        past_text = _format_past_punishments(past)
        roles_map = chat_roles.roles_by_user_id if chat_roles else {}
        members_info = await get_chat_members_context(bot, self.db, chat_id, context)
        prompt = MODERATION_SYSTEM.format(
            rules=rules_text or "(правила не заданы — используй этические нормы)",
            past_punishments=past_text,
            chat_roles=chat_roles.prompt_text if chat_roles else "(неизвестно)",
            chat_members=members_info.prompt_text,
            context=self.context_builder.format_for_prompt(context, roles_map),
        )
        raw = await self.gemini.generate(prompt, admin_user_id=admin_user_id)
        result = parse_moderation_response(raw)
        if is_lookup_response(result):
            lookup_text = await resolve_user_lookups(
                bot, self.db, chat_id, extract_lookup_requests(result)
            )
            raw = await self.gemini.generate(
                prompt + LOOKUP_FOLLOWUP_SINGLE.format(lookup_results=lookup_text),
                admin_user_id=admin_user_id,
            )
            result = parse_moderation_response(raw)
        result["reply_to_message_id"] = result.get("reply_to_message_id") or target_message_id
        return result

    async def analyze_batch(
        self,
        bot: Bot,
        chat_id: int,
        rules_text: str,
        target_message_ids: list[int],
        context: BatchModerationContext,
        admin_user_id: int | None = None,
        chat_roles: ChatRolesInfo | None = None,
    ) -> dict[int, dict[str, Any]]:
        past = await self.db.get_punishments_for_users(
            chat_id, list(context.participant_ids)
        )
        past_text = _format_past_punishments(past)
        roles_map = chat_roles.roles_by_user_id if chat_roles else {}
        members_info = await get_chat_members_context(bot, self.db, chat_id, context)
        prompt = BATCH_MODERATION_SYSTEM.format(
            rules=rules_text or "(правила не заданы — используй этические нормы)",
            past_punishments=past_text,
            chat_roles=chat_roles.prompt_text if chat_roles else "(неизвестно)",
            chat_members=members_info.prompt_text,
            context=self.context_builder.format_batch_for_prompt(context, roles_map),
        )
        raw = await self.gemini.generate(prompt, admin_user_id=admin_user_id)
        parsed = parse_moderation_response(raw)
        if is_lookup_response(parsed):
            lookup_text = await resolve_user_lookups(
                bot, self.db, chat_id, extract_lookup_requests(parsed)
            )
            raw = await self.gemini.generate(
                prompt + LOOKUP_FOLLOWUP_BATCH.format(lookup_results=lookup_text),
                admin_user_id=admin_user_id,
            )
        decisions = parse_batch_moderation_response(raw)
        return self._index_batch_decisions(decisions, target_message_ids)

    def map_batch_decisions(
        self,
        by_id: dict[int, dict[str, Any]],
        messages: list[Message],
    ) -> dict[int, dict[str, Any]]:
        ordered = sorted(messages, key=lambda m: m.message_id)
        mapped: dict[int, dict[str, Any]] = {}
        used: set[int] = set()

        for msg in ordered:
            decision = by_id.get(msg.message_id)
            if decision:
                mapped[msg.message_id] = decision
                used.add(msg.message_id)

        leftovers = [d for mid, d in sorted(by_id.items()) if mid not in used]
        for msg, decision in zip(
            [m for m in ordered if m.message_id not in mapped],
            leftovers,
        ):
            decision = dict(decision)
            decision["message_id"] = msg.message_id
            decision["reply_to_message_id"] = decision.get("reply_to_message_id") or msg.message_id
            mapped[msg.message_id] = decision

        return mapped

    def enrich_decision(
        self,
        decision: dict[str, Any],
        message: Message,
        chat_roles: ChatRolesInfo | None = None,
    ) -> dict[str, Any]:
        result = dict(decision)
        msg_id = message.message_id
        result["message_id"] = msg_id
        result["reply_to_message_id"] = result.get("reply_to_message_id") or msg_id
        privileged = chat_roles.privileged_user_ids if chat_roles else set()
        if message.from_user and not result.get("violator_user_id"):
            author_id = message.from_user.id
            reply_user = (
                message.reply_to_message.from_user
                if message.reply_to_message
                else None
            )
            if (
                result.get("action") == "punish"
                and author_id in privileged
                and reply_user
                and not reply_user.is_bot
            ):
                result["violator_user_id"] = reply_user.id
                if not result.get("violator_display"):
                    result["violator_display"] = (
                        f"@{reply_user.username}"
                        if reply_user.username
                        else reply_user.full_name
                    )
            elif result.get("action") in ("punish", "pardon"):
                result["violator_user_id"] = author_id
                if not result.get("violator_display"):
                    result["violator_display"] = (
                        f"@{message.from_user.username}"
                        if message.from_user.username
                        else message.from_user.full_name
                    )
        if (
            chat_roles
            and message.from_user
            and result.get("action") == "punish"
            and message.from_user.id in privileged
            and result.get("violator_user_id") != message.from_user.id
        ):
            can_unpunish = list(result.get("can_unpunish_user_ids") or [])
            author_id = message.from_user.id
            if author_id not in can_unpunish:
                can_unpunish.append(author_id)
            result["can_unpunish_user_ids"] = can_unpunish
        return result

    async def finalize_decision(
        self,
        bot: Bot,
        chat_id: int,
        decision: dict[str, Any],
        message: Message,
        chat_roles: ChatRolesInfo | None = None,
    ) -> dict[str, Any]:
        result = self.enrich_decision(decision, message, chat_roles)
        if result.get("action") != "punish":
            return result

        privileged = chat_roles.privileged_user_ids if chat_roles else set()
        author_id = message.from_user.id if message.from_user else None
        target_username = _extract_mute_target_username(result, message)

        if target_username:
            participant = await lookup_participant_by_username(bot, self.db, chat_id, target_username)
            if participant:
                old_id = result.get("violator_user_id")
                if old_id != participant.user_id:
                    logger.info(
                        "Resolved violator by @%s: user_id %s -> %s chat=%s",
                        target_username,
                        old_id,
                        participant.user_id,
                        chat_id,
                    )
                result["violator_user_id"] = participant.user_id
                result["violator_display"] = (
                    f"@{participant.username}" if participant.username else participant.full_name
                )
                result["violator_username"] = (
                    f"@{participant.username}" if participant.username else None
                )
            elif author_id in privileged:
                logger.warning(
                    "Could not resolve @%s for mute command chat=%s msg=%s",
                    target_username,
                    chat_id,
                    message.message_id,
                )
        elif (
            author_id in privileged
            and message.reply_to_message
            and message.reply_to_message.from_user
            and not message.reply_to_message.from_user.is_bot
        ):
            reply_user = message.reply_to_message.from_user
            result["violator_user_id"] = reply_user.id
            result["violator_display"] = (
                f"@{reply_user.username}" if reply_user.username else reply_user.full_name
            )

        return result

    def _index_batch_decisions(
        self,
        decisions: list[dict[str, Any]],
        target_message_ids: list[int],
    ) -> dict[int, dict[str, Any]]:
        by_id: dict[int, dict[str, Any]] = {}
        for decision in decisions:
            msg_id = _extract_decision_message_id(decision)
            if msg_id is None:
                continue
            decision["reply_to_message_id"] = decision.get("reply_to_message_id") or msg_id
            by_id[msg_id] = decision
        for msg_id in target_message_ids:
            if msg_id not in by_id:
                logger.warning(
                    "Batch moderation missing decision for message_id=%s (have=%s)",
                    msg_id,
                    sorted(by_id.keys()),
                )
        return by_id

    async def apply_decision(
        self,
        bot: Bot,
        chat_id: int,
        decision: dict[str, Any],
        message_id: int | None = None,
        target_message: Message | None = None,
    ) -> Punishment | None:
        action = decision.get("action", "none")
        explanation = decision.get("explanation", "")
        rule_refs = decision.get("rule_references") or []
        violator_id = decision.get("violator_user_id")
        reply_id = decision.get("reply_to_message_id")
        check_id = message_id or reply_id

        if check_id and await self.db.was_message_moderated(chat_id, check_id):
            logger.info(
                "Skip duplicate moderation chat=%s msg=%s action=%s",
                chat_id,
                check_id,
                action,
            )
            return None

        await self.db.log_moderation(
            chat_id=chat_id,
            message_id=message_id or reply_id,
            action=action,
            explanation=explanation,
            rule_references=rule_refs,
        )

        if action == "none":
            settings = get_settings()
            if settings.log_clean_checks:
                logger.info("Chat %s: no violation (msg %s) — %s", chat_id, message_id, explanation)
            return None
        affected_ids = _extract_affected_user_ids(decision, target_message, violator_id)
        can_unpunish = _merge_unpunish_ids(decision.get("can_unpunish_user_ids") or [], affected_ids)

        if action == "pardon":
            warning = decision.get("warning_text") or "Вы нарушили правила, но вас решили помиловать. Впредь будьте осторожны."
            rules_str = "; ".join(rule_refs) if rule_refs else "общие нормы"
            text = (
                f"🕊 <b>Устное предупреждение</b>\n\n"
                f"{warning}\n\n"
                f"📜 <b>Правила:</b> {rules_str}\n"
                f"💬 {explanation}"
            )
            record_uid = violator_id
            record_display = decision.get("violator_display")
            if not record_uid and target_message and target_message.from_user:
                record_uid = target_message.from_user.id
                record_display = record_display or target_message.from_user.full_name
            warning_id = None
            if record_uid:
                warning_id = await self._record_warning_entry(
                    chat_id,
                    record_uid,
                    record_display or str(record_uid),
                    rule_refs,
                    explanation,
                    reply_id,
                    can_unpunish,
                    punishment_type="warning",
                )
            await bot.send_message(
                chat_id,
                text,
                reply_to_message_id=reply_id,
                reply_markup=history_only_keyboard(warning_id) if warning_id else None,
                **topic_send_kwargs(target_message),
            )
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
                can_unpunish_ids=can_unpunish,
                expires_at=expires,
            )

            from bot.keyboards.punishment import unpunish_keyboard

            affected_line = await _format_affected_line(bot, chat_id, affected_ids)
            unpunish_line = await _format_unpunish_line(bot, chat_id, can_unpunish, violator_id)

            text = (
                f"🚫 <b>{ptype_label}{duration_str}</b>\n\n"
                f"👤 Нарушитель: <b>{display}</b> (<code>{violator_id}</code>)\n"
            )
            if affected_line:
                text += f"{affected_line}\n"
            text += f"📜 <b>Правила:</b>\n{rules_str}"
            if unpunish_line:
                text += f"\n\n{unpunish_line}"
            sent = await bot.send_message(
                chat_id,
                text,
                reply_to_message_id=reply_id,
                reply_markup=unpunish_keyboard(punishment_id),
                **topic_send_kwargs(target_message),
            )
            return await self.db.get_punishment(punishment_id)

        return None

    async def _record_warning_entry(
        self,
        chat_id: int,
        user_id: int,
        display: str | None,
        rule_refs: list[str],
        explanation: str,
        message_id: int | None,
        can_unpunish: list[int],
        punishment_type: str = "warning",
    ) -> int:
        return await self.db.add_punishment(
            chat_id=chat_id,
            user_id=user_id,
            username=display if display and display.startswith("@") else None,
            punishment_type=punishment_type,
            duration_minutes=None,
            rule_references=rule_refs,
            explanation=explanation,
            message_id=message_id,
            can_unpunish_ids=can_unpunish,
            expires_at=None,
            active=False,
        )


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
        affected = _format_affected_preview(decision.get("affected_users") or [])
        lines = [
            f"🚫 <b>Наказание: {ptype}{duration_str}</b>\n",
            f"👤 Нарушитель: <b>{violator}</b>",
        ]
        if affected:
            lines.append(f"🎯 В отношении: {affected}")
        can_unpunish = decision.get("can_unpunish_user_ids") or []
        if can_unpunish:
            lines.append(f"🕊 Снять могут user_id: {', '.join(str(x) for x in can_unpunish)}")
        lines.append(f"\n💬 {explanation}")
        return "\n".join(lines)
    return f"❓ Неизвестное действие: <code>{action}</code>\n\n💬 {explanation}"


def _format_past_punishments(punishments: list[Punishment]) -> str:
    if not punishments:
        return "(нет наказаний за последние 30 дней)"
    lines = []
    for p in punishments[:20]:
        refs = json.loads(p.rule_references) if p.rule_references.startswith("[") else [p.rule_references]
        refs_str = ", ".join(refs)
        active = "активно" if p.active else "снято"
        when = format_punishment_moment(p.created_at)
        lines.append(
            f"- user_id={p.user_id}, тип={p.punishment_type}, "
            f"правила: {refs_str}, статус: {active}, когда: {when}"
        )
    return "\n".join(lines)


def parse_batch_moderation_response(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    data = json.loads(text)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("decisions", "results", "messages"):
            items = data.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    raise ValueError("Batch response is not a JSON object with decisions array")


def _extract_decision_message_id(decision: dict[str, Any]) -> int | None:
    for key in ("message_id", "reply_to_message_id", "target_message_id"):
        raw = decision.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def _extract_affected_user_ids(
    decision: dict[str, Any],
    target_message: Message | None,
    violator_id: int | None,
) -> list[int]:
    ids: list[int] = []
    for item in decision.get("affected_users") or []:
        if isinstance(item, dict) and item.get("user_id"):
            ids.append(int(item["user_id"]))
        elif isinstance(item, int):
            ids.append(item)

    if not ids and target_message and target_message.reply_to_message:
        reply_user = target_message.reply_to_message.from_user
        if reply_user and not reply_user.is_bot:
            ids.append(reply_user.id)

    if violator_id:
        ids = [uid for uid in ids if uid != violator_id]
    return list(dict.fromkeys(ids))


def _merge_unpunish_ids(can_unpunish: list, affected_ids: list[int]) -> list[int]:
    merged: list[int] = []
    for raw in list(can_unpunish) + affected_ids:
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            continue
        if uid not in merged:
            merged.append(uid)
    return merged


def _mentions_from_message(message: Message) -> list[str]:
    text = message.text or message.caption or ""
    mentions: list[str] = []
    for entities in (message.entities, message.caption_entities):
        if not entities:
            continue
        for entity in entities:
            if entity.type == "mention":
                mention = text[entity.offset : entity.offset + entity.length].lstrip("@")
                if mention:
                    mentions.append(mention)
    for mention in extract_mentioned_usernames(text):
        if mention not in mentions:
            mentions.append(mention)
    return list(dict.fromkeys(mentions))


def _extract_mute_target_username(decision: dict[str, Any], message: Message) -> str | None:
    for key in ("violator_username",):
        raw = decision.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lstrip("@")

    display = decision.get("violator_display")
    if isinstance(display, str) and display.strip().startswith("@"):
        return display.strip().lstrip("@")

    author_username = (
        message.from_user.username.lower()
        if message.from_user and message.from_user.username
        else ""
    )
    mentions = [
        m for m in _mentions_from_message(message) if m.lower() != author_username
    ]
    if len(mentions) == 1:
        return mentions[0]
    if mentions and isinstance(display, str) and display.startswith("@"):
        wanted = display.lstrip("@").lower()
        for mention in mentions:
            if mention.lower() == wanted:
                return mention
    return None


def _format_affected_preview(affected_users: list) -> str:
    parts = []
    for item in affected_users:
        if isinstance(item, dict):
            display = item.get("display") or item.get("user_id")
            parts.append(str(display))
        else:
            parts.append(str(item))
    return ", ".join(parts)


async def _user_mention(bot: Bot, chat_id: int, user_id: int) -> str:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        user = member.user
        label = f"@{user.username}" if user.username else user.full_name
    except Exception:
        label = "пользователь"
    return f'<a href="tg://user?id={user_id}">{label}</a>'


async def _format_affected_line(bot: Bot, chat_id: int, user_ids: list[int]) -> str:
    if not user_ids:
        return ""
    mentions = [await _user_mention(bot, chat_id, uid) for uid in user_ids]
    return f"🎯 <b>В отношении:</b> {', '.join(mentions)}"


async def _format_unpunish_line(
    bot: Bot, chat_id: int, user_ids: list[int], violator_id: int | None
) -> str:
    allowed = [uid for uid in user_ids if uid != violator_id]
    if not allowed:
        return "🕊 <b>Снять наказание</b> могут администраторы чата (кнопка ниже)"
    mentions = [await _user_mention(bot, chat_id, uid) for uid in allowed]
    return f"🕊 <b>Снять наказание</b> могут: {', '.join(mentions)} (кнопка ниже)"
