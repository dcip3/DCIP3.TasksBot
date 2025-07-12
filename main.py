# main.py
"""
Main application entry point for the TasksBot.

This module initializes the bot, sets up logging, and starts the polling process.
"""

import asyncio
import logging

# Import initialization modules
from app.core.bot_core import dp, bot
from app.utils import on_startup, on_shutdown

# Import handlers for registration
from app.handlers import register_handlers

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    """Application entry point"""
    logger.info("Starting bot...")
    
    # Register handlers
    register_handlers()
    
    # Setup startup and shutdown handlers
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    # Start polling
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main()) 