# main.py
"""
Main application entry point for running the Telegram bot only.
"""

import asyncio
import logging
import sys

from app.core.bot_core import dp, bot
from app.core.utils import on_startup, on_shutdown
from app.bot.handlers import register_handlers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> int:
    """Start the bot via polling."""
    try:
        register_handlers()
        dp.startup.register(on_startup)
        dp.shutdown.register(on_shutdown)
        logger.info("Starting TasksBot...")
        await dp.start_polling(bot, skip_updates=True)
        return 0
    except Exception as exc:
        logger.error("Bot startup error: %s", exc)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        sys.exit(0)
