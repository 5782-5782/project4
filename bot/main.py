import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.commands import setup_bot_commands
from bot.config import get_settings
from bot.db.database import Database
from bot.handlers import admin, chat_register, chats, group, moderation
from bot.middlewares.services import ServicesMiddleware
from bot.services.batch import BatchProcessor
from bot.services.context import ContextBuilder
from bot.services.gemini import GeminiService
from bot.services.moderation import ModerationService
from bot.utils.proxy import create_aiohttp_session, create_bot_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    if not settings.bot_token or "YOUR_" in settings.bot_token:
        logger.error("Set bot_token in config/secrets.json")
        sys.exit(1)

    db = Database()
    await db.init()

    proxy_url = settings.proxy_url
    http_session = create_aiohttp_session(proxy_url)
    bot_session = create_bot_session(proxy_url)

    gemini = GeminiService(db, http_session=http_session)
    gemini.start()

    moderation_svc = ModerationService(db, gemini)
    batch_processor = BatchProcessor(db, moderation_svc, ContextBuilder())

    bot = Bot(
        token=settings.bot_token,
        session=bot_session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.update.middleware(
        ServicesMiddleware(db, gemini, moderation_svc, batch_processor)
    )

    dp.include_router(moderation.router)
    dp.include_router(chats.router)
    dp.include_router(admin.router)
    dp.include_router(chat_register.router)
    dp.include_router(group.router)

    logger.info("Bot starting... Owner ID: %s", settings.owner_id)
    await setup_bot_commands(bot)
    try:
        await dp.start_polling(bot)
    finally:
        await gemini.stop()
        await bot.session.close()
        if not http_session.closed:
            await http_session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
