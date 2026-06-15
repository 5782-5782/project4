from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def _reason_button(punishment_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="💬 Причина",
        callback_data=f"punish_reason:{punishment_id}",
    )


def history_only_keyboard(punishment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Убрать из истории",
                    callback_data=f"punish_del:{punishment_id}",
                )
            ],
            [_reason_button(punishment_id)],
        ]
    )


def punishment_done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Причина",
                    callback_data="punish_done:reason",
                )
            ]
        ]
    )


def unpunish_keyboard(punishment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🕊 Снять наказание",
                    callback_data=f"unpunish:{punishment_id}",
                ),
                InlineKeyboardButton(
                    text="🗑 Из истории",
                    callback_data=f"punish_del:{punishment_id}",
                ),
            ],
            [_reason_button(punishment_id)],
        ]
    )


def punishments_history_keyboard(punishments: list, back_data: str = "admin:back") -> InlineKeyboardMarkup:
    rows = []
    for p in punishments[:12]:
        status = "🟢" if p.active else "⚫"
        label = f"{status} #{p.id} {p.punishment_type}"
        rows.append([
            InlineKeyboardButton(text=f"🗑 {label}", callback_data=f"punish_del:{p.id}"),
        ])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data=back_data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Лимиты API", callback_data="admin:limits"),
                InlineKeyboardButton(text="📜 Правила чата", callback_data="admin:rules"),
            ],
            [
                InlineKeyboardButton(text="⏱ Интервал батча", callback_data="admin:interval"),
                InlineKeyboardButton(text="🛡 Модерация вкл/выкл", callback_data="admin:toggle_mod"),
            ],
            [
                InlineKeyboardButton(text="🚫 Все наказания", callback_data="admin:all_punishments"),
            ],
        ]
    )


def interval_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="0 сек (сразу)", callback_data="interval:0"),
                InlineKeyboardButton(text="15 сек", callback_data="interval:15"),
            ],
            [
                InlineKeyboardButton(text="30 сек", callback_data="interval:30"),
                InlineKeyboardButton(text="60 сек", callback_data="interval:60"),
            ],
            [InlineKeyboardButton(text="« Назад", callback_data="admin:back")],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« Назад", callback_data="admin:back")]]
    )
