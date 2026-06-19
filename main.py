"""Точка входа aiogram-бота — мультипользовательский режим."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers import router
from bot.middlewares import AuthThrottleMiddleware, ThrottlingMiddleware
from bot.session_manager import sessions
from config import get_settings
from storage import close_db, init_db
from storage.crypto import master_key

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("giftbot")


async def main() -> None:
    settings = get_settings()
    if not settings.bot_token:
        logger.error("BOT_TOKEN не задан в .env")
        sys.exit(1)

    try:
        master_key()
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    await init_db()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(ThrottlingMiddleware())
    dp.callback_query.middleware(ThrottlingMiddleware())
    dp.message.middleware(AuthThrottleMiddleware())

    dp.include_router(router)

    try:
        logger.info("Gift Bot запущен (multi-user, encrypted sessions)")
        await dp.start_polling(bot)
    finally:
        await sessions.shutdown()
        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
