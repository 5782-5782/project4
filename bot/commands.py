"""Telegram bot command menu and help texts."""

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats

from bot.ui.emoji import E


async def setup_bot_commands(bot: Bot) -> None:
    """Register commands visible in Telegram menu."""
    await bot.set_my_commands(
        [BotCommand(command="help", description="Справка по командам")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [
            BotCommand(command="help", description="Справка"),
            BotCommand(command="punishments", description="Активные наказания"),
            BotCommand(command="linkchat", description="Привязать чат"),
            BotCommand(command="setrules", description="Загрузить правила"),
            BotCommand(command="setinterval", description="Интервал батчинга"),
            BotCommand(command="mod", description="Вкл/выкл модерацию"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )


def build_help_text(is_owner: bool) -> str:
    lines = [
        f"{E['info']} <b>Справка</b>",
        "",
        f"<b>{E['crown']} Личные сообщения</b>",
        "/admin — панель управления",
        "/help — эта справка",
        "/allpunishments — наказания",
    ]
    if is_owner:
        lines.append("/addadmin &lt;id&gt; &lt;лимит&gt; — добавить суб-админа")
    lines += [
        "",
        f"<b>{E['shield']} В группе</b>",
        "/linkchat — привязать чат к вашему аккаунту",
        "/setrules — загрузить правила (ответом на сообщение бота)",
        "/setinterval &lt;сек&gt; — интервал батчинга (0 = сразу)",
        "/mod on|off — включить/выключить модерацию",
        "/punishments — активные наказания чата",
        "",
        f"<i>Управление чатами также доступно через /admin → Мои чаты</i>",
    ]
    if is_owner:
        lines += [
            "",
            f"<b>{E['chart']} Только владелец</b>",
            "Панель → Лимиты API, Суб-админы",
        ]
    else:
        lines += [
            "",
            f"<i>Ваш дневной лимит запросов отображается в /admin</i>",
        ]
    return "\n".join(lines)
