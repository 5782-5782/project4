from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def admin_main_keyboard(is_owner: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="💬 Мои чаты", callback_data="admin:chats"),
            InlineKeyboardButton(text="📊 Лимиты API", callback_data="admin:limits"),
        ],
        [
            InlineKeyboardButton(text="🚫 Наказания", callback_data="admin:all_punishments"),
            InlineKeyboardButton(text="📈 Статистика", callback_data="admin:stats"),
        ],
    ]
    if is_owner:
        rows.append([
            InlineKeyboardButton(text="👥 Суб-админы", callback_data="admin:subadmins"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chats_list_keyboard(chats: list[dict], page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    start = page * per_page
    chunk = chats[start : start + per_page]
    rows = []
    for chat in chunk:
        title = (chat.get("title") or f"Chat {chat['chat_id']}")[:30]
        rows.append([
            InlineKeyboardButton(
                text=f"💬 {title}",
                callback_data=f"chat:{chat['chat_id']}",
            )
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="«", callback_data=f"chats_page:{page - 1}"))
    if start + per_page < len(chats):
        nav.append(InlineKeyboardButton(text="»", callback_data=f"chats_page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="« В меню", callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chat_detail_keyboard(chat_id: int, mod_enabled: bool) -> InlineKeyboardMarkup:
    mod_label = "🔴 Выкл модерацию" if mod_enabled else "🟢 Вкл модерацию"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📜 Правила", callback_data=f"chat_rules:{chat_id}"),
                InlineKeyboardButton(text="⏱ Интервал", callback_data=f"chat_interval:{chat_id}"),
            ],
            [
                InlineKeyboardButton(text=mod_label, callback_data=f"chat_mod_toggle:{chat_id}"),
                InlineKeyboardButton(text="🚫 Наказания", callback_data=f"chat_punish:{chat_id}"),
            ],
            [InlineKeyboardButton(text="« К чатам", callback_data="admin:chats")],
        ]
    )


def chat_interval_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="0с", callback_data=f"cint:{chat_id}:0"),
                InlineKeyboardButton(text="15с", callback_data=f"cint:{chat_id}:15"),
            ],
            [
                InlineKeyboardButton(text="30с", callback_data=f"cint:{chat_id}:30"),
                InlineKeyboardButton(text="60с", callback_data=f"cint:{chat_id}:60"),
            ],
            [InlineKeyboardButton(text="« Назад", callback_data=f"chat:{chat_id}")],
        ]
    )


def subadmins_keyboard(admins: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for a in admins:
        name = a.get("display_name") or str(a["user_id"])
        rows.append([
            InlineKeyboardButton(
                text=f"👤 {name} ({a['daily_limit']}/день)",
                callback_data=f"subadmin:{a['user_id']}",
            )
        ])
    rows.append([
        InlineKeyboardButton(text="➕ Добавить", callback_data="subadmin_add"),
    ])
    rows.append([InlineKeyboardButton(text="« В меню", callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subadmin_detail_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Лимит", callback_data=f"subadmin_limit:{user_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"subadmin_del:{user_id}"),
            ],
            [InlineKeyboardButton(text="« Назад", callback_data="admin:subadmins")],
        ]
    )


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« В меню", callback_data="admin:back")]]
    )
