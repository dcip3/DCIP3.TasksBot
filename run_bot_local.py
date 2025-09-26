#!/usr/bin/env python3
"""
Local bot runner that starts only the Telegram bot without FastAPI or the mini app.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Add project root to module path
sys.path.insert(0, str(Path(__file__).parent))

async def main():
    """Main entry point for starting the bot."""
    try:
        # Import bot modules lazily to avoid side effects during startup
        from app.core.bot_core import dp, bot
        from app.utils import on_startup, on_shutdown
        from app.handlers import register_handlers
        
        logger.info("🚀 Starting TasksBot in local mode...")
        
        # Register handlers
        register_handlers()
        logger.info("✅ Handlers registered")
        
        # Attach startup and shutdown hooks
        dp.startup.register(on_startup)
        dp.shutdown.register(on_shutdown)
        
        logger.info("🤖 Bot is ready!")
        logger.info("📱 Send /start in Telegram to begin")
        
        # Start polling
        await dp.start_polling(bot, skip_updates=True)
        
    except ImportError as e:
        logger.error(f"❌ Import error: {e}")
        logger.error("Ensure all dependencies are installed: pip install -r requirements.txt")
        return 1
    except Exception as e:
        logger.error(f"❌ Bot startup error: {e}")
        return 1

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Critical error: {e}")
        sys.exit(1) 
