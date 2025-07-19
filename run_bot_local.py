#!/usr/bin/env python3
"""
Local bot runner - запуск только Telegram бота без FastAPI и miniapp
"""

import asyncio
import logging
import sys
from pathlib import Path

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Добавляем путь к модулям приложения
sys.path.insert(0, str(Path(__file__).parent))

async def main():
    """Основная функция запуска бота"""
    try:
        # Импортируем модули бота
        from app.core.bot_core import dp, bot
        from app.utils import on_startup, on_shutdown
        from app.handlers import register_handlers
        
        logger.info("🚀 Запуск TasksBot в локальном режиме...")
        
        # Регистрируем обработчики
        register_handlers()
        logger.info("✅ Обработчики зарегистрированы")
        
        # Настраиваем события запуска и остановки
        dp.startup.register(on_startup)
        dp.shutdown.register(on_shutdown)
        
        logger.info("🤖 Бот готов к работе!")
        logger.info("📱 Отправьте /start в Telegram для начала работы")
        
        # Запускаем бота
        await dp.start_polling(bot, skip_updates=True)
        
    except ImportError as e:
        logger.error(f"❌ Ошибка импорта: {e}")
        logger.error("Убедитесь, что все зависимости установлены: pip install -r requirements.txt")
        return 1
    except Exception as e:
        logger.error(f"❌ Ошибка запуска бота: {e}")
        return 1

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
        sys.exit(1) 