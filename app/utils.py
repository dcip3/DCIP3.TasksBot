# app/utils.py
"""
Utility functions, decorators, keyboards, and initialization functions.

This module provides core utilities for the Telegram bot including:
- Authentication decorators
- Keyboard layouts
- File system utilities
- Progress formatting
- Directory management
"""

import asyncio
import logging
import os
import shutil
from functools import wraps
from pathlib import Path
from typing import cast

from aiogram.types import Message, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, MenuButtonWebApp, WebAppInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.bot_core import bot, dp
from app.core.database import init_db, close_db

logger = logging.getLogger(__name__)

# ============================================================================
# === GLOBAL OBJECTS ===
# ============================================================================

# Scheduler for automated tasks
scheduler = AsyncIOScheduler(timezone="Europe/Moscow", job_defaults={'coalesce': True, 'max_instances': 1})

# Track jobs that have been notified about
notified_jobs = set()

# ============================================================================
# === DECORATORS ===
# ============================================================================

def authorized_only(handler):
    """
    Decorator to ensure only authorized users can access handler functions.
    
    Args:
        handler: The handler function to wrap
        
    Returns:
        Wrapped handler that checks authorization before execution
    """
    @wraps(handler)
    async def wrapper(message: Message, *args, **kwargs):
        from app.auth import is_authorized
        if message.from_user is None:
            await message.reply("Access denied. User information not available.")
            return
        if not await is_authorized(message.from_user.id):
            await message.reply("Access denied. Please use /login to authenticate.")
            return
        return await handler(message, *args, **kwargs)
    return wrapper

# ============================================================================
# === KEYBOARD FUNCTIONS ===
# ============================================================================

def get_main_keyboard():
    """
    Create the main ReplyKeyboardMarkup for the bot.
    
    Returns:
        ReplyKeyboardMarkup with main navigation buttons
    """
    kb = [
        [
            KeyboardButton(text="Jobs", request_contact=False, request_location=False),
            KeyboardButton(text="Workers", request_contact=False, request_location=False),
        ],
        [
            KeyboardButton(text="Realtime", request_contact=False, request_location=False),
            KeyboardButton(text="🔔 Уведомления", request_contact=False, request_location=False),
        ],
        [
            KeyboardButton(text="🧹 Очистить", request_contact=False, request_location=False),
        ],
    ]
    return ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=False,
        is_persistent=False,
        input_field_placeholder=""
    )

async def setup_menu_button():
    """
    Setup the menu button for Mini App.
    This creates a button in the chat menu that opens the Mini App.
    """
    try:
        # URL для Mini App - для разработки используем localhost
        # В продакшене замените на ваш домен с HTTPS
        # mini_app_url = "http://localhost:3000"  # Для разработки
        mini_app_url = "https://example.com"  # Для продакшена
        
        logger.info(f"Setting up menu button with URL: {mini_app_url}")
        
        # Используем правильный метод для aiogram 3.x
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Tasks",
                web_app=WebAppInfo(url=mini_app_url)
            )
        )
        logger.info("Menu button setup successfully")
    except Exception as e:
        logger.error(f"Failed to setup menu button: {e}")
        # Попробуем альтернативный способ
        try:
            logger.info("Trying alternative method...")
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Tasks",
                    web_app=WebAppInfo(url=mini_app_url)
                ),
                chat_id=None  # Для всех пользователей
            )
            logger.info("Menu button setup successfully (alternative method)")
        except Exception as e2:
            logger.error(f"Alternative method also failed: {e2}")
            # Попробуем третий способ - через BotFather API
            try:
                logger.info("Trying BotFather API method...")
                # Этот метод может не работать в aiogram 3.x, но попробуем
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text="Tasks",
                        web_app=WebAppInfo(url=mini_app_url)
                    ),
                    chat_id=0  # Глобальная настройка
                )
                logger.info("Menu button setup successfully (BotFather API method)")
            except Exception as e3:
                logger.error(f"All methods failed: {e3}")
                raise e3

# ============================================================================
# === UTILITY FUNCTIONS ===
# ============================================================================

def has_enough_space(path: str, min_free_bytes: int | None = None) -> bool:
    """
    Check if there's enough free space on the disk partition.
    
    Args:
        path: Path to check disk space for
        min_free_bytes: Minimum required free space in bytes
        
    Returns:
        True if enough space is available, False otherwise
    """
    if min_free_bytes is None:
        min_free_bytes = settings.min_free_space_bytes
    
    total, used, free = shutil.disk_usage(path)
    return free >= min_free_bytes


def clear_folder(folder_path: str | Path) -> None:
    """
    Clear contents of a folder without removing the folder itself.
    
    Args:
        folder_path: Path to the folder to clear
    """
    folder = Path(folder_path)
    if folder.exists():
        for item in folder.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink()
            except Exception:
                pass
    else:
        folder.mkdir(parents=True, exist_ok=True)


def cleanup_temp_and_conv() -> None:
    """
    Clear contents of 'temp' and 'conv' directories.
    """
    clear_folder(Path(settings.temp_dir))
    clear_folder(Path(settings.conv_dir))


def ensure_temp_dir() -> Path:
    """
    Ensure temp directory exists and return its path.
    
    Returns:
        Path to the temp directory
    """
    temp_path = Path(settings.temp_dir)
    temp_path.mkdir(exist_ok=True)
    return temp_path


def format_progress(completed: int, total: int) -> str:
    """
    Format progress as percentage string.
    
    Args:
        completed: Number of completed items
        total: Total number of items
        
    Returns:
        Formatted progress string (e.g., "75% 15/20")
    """
    percentage = int((completed / total) * 100) if total else 0
    return f"{percentage}% {completed}/{total}"


def get_task_icon(stat: int) -> str:
    """
    Get icon for task status.
    
    Args:
        stat: Task status number
        
    Returns:
        Unicode icon for the status
    """
    if stat == 0:
        return "⏳"  # Pending
    elif stat == 1:
        return "🔄"  # Active
    elif stat == 2:
        return "⏸️"  # Suspended
    elif stat == 3:
        return "✅"  # Completed
    elif stat == 4:
        return "❌"  # Failed
    else:
        return "❓"  # Unknown


def get_job_icon(stat: int) -> str:
    """
    Get icon for job status.
    
    Args:
        stat: Job status number
        
    Returns:
        Unicode icon for the status
    """
    if stat == 0:
        return "❓"  # Unknown
    elif stat == 1:
        return "🔄"  # Active
    elif stat == 2:
        return "⏸️"  # Suspended
    elif stat == 3:
        return "✅"  # Completed
    elif stat == 4:
        return "❌"  # Failed
    elif stat == 6:
        return "⏳"  # Pending
    else:
        return "❓"  # Unknown


def get_worker_icon(stat: int) -> str:
    """
    Get icon for worker status.
    
    Args:
        stat: Worker status number
        
    Returns:
        Unicode icon for the status
    """
    if stat == 0:
        return "❓"  # Unknown
    elif stat == 1:
        return "🔄"  # Rendering
    elif stat == 2:
        return "💤"  # Idle
    elif stat == 3:
        return "🔴"  # Offline
    elif stat == 4:
        return "⚠️"  # Stalled
    elif stat == 8:
        return "🚀"  # StartingJob
    else:
        return "❓"  # Unknown


# ============================================================================
# === INITIALIZATION FUNCTIONS ===
# ============================================================================

async def on_startup(bot):
    """
    Application startup handler.
    
    Args:
        bot: Bot instance
    """
    logger.info("Starting TasksBot...")
    
    # Initialize database
    await init_db()
    logger.info("Database initialized")
    
    # Initialize directories
    ensure_temp_dir()
    Path(settings.conv_dir).mkdir(exist_ok=True)
    
    # Setup menu button for Mini App
    await setup_menu_button()
    logger.info("Menu button setup completed")
    
    # Start scheduler
    scheduler.start()
    logger.info("Scheduler started")
    
    # Start job progress watcher
    asyncio.create_task(job_progress_watcher())
    logger.info("Job progress watcher started")


async def on_shutdown(bot):
    """
    Application shutdown handler.
    
    Args:
        bot: Bot instance
    """
    logger.info("Shutting down TasksBot...")
    
    # Shutdown scheduler
    scheduler.shutdown()
    logger.info("Scheduler shutdown")
    
    # Close database connection
    await close_db()
    logger.info("Database connection closed")
    
    # Cleanup temp files
    cleanup_temp_and_conv()
    logger.info("Temp files cleaned up")


async def job_progress_watcher():
    """
    Monitor job completion and notify users about finished jobs.
    """
    import aiohttp
    from datetime import datetime, timezone, timedelta
    from app.auth import get_all_users_with_notifications
    
    while True:
        await asyncio.sleep(60)
        
        users_with_notifications = await get_all_users_with_notifications()
        for telegram_user_id, login, password in users_with_notifications:
                
            try:
                async with aiohttp.ClientSession() as session:
                    headers = aiohttp.BasicAuth(login, password)
                    async with session.get(f"{settings.base_api_url}/jobs", auth=headers, ssl=False) as resp:
                        if resp.status == 200:
                            jobs = await resp.json()
                            for job in jobs:
                                job_id = job.get("JobId") or job.get("_id") or job.get("Props", {}).get("JobId")
                                if not job_id:
                                    continue
                                    
                                # Remove job_id from notified_jobs if status is no longer Completed
                                stat_num = job.get("Stat", 0)
                                if job_id in notified_jobs and stat_num != 3:
                                    notified_jobs.remove(job_id)
                                    
                                total_tasks = job.get("Props", {}).get("Tasks", 0)
                                completed_chunks = job.get("CompletedChunks", 0)
                                progress = 0
                                if total_tasks:
                                    progress = int((completed_chunks / total_tasks) * 100)
                                    
                                if progress == 100 and job_id not in notified_jobs:
                                    date_comp_str = job.get("DateComp") or job.get("Props", {}).get("DateComp")
                                    if not date_comp_str or date_comp_str == "0001-01-01T00:00:00Z":
                                        continue
                                        
                                    try:
                                        date_comp = datetime.fromisoformat(date_comp_str.replace("Z", "+00:00"))
                                        now = datetime.now(timezone.utc)
                                        diff = now - date_comp
                                        if diff > timedelta(minutes=10):
                                            continue
                                    except Exception:
                                        continue
                                        
                                    batch = job.get("Props", {}).get("Batch", "Без имени")
                                    message_text = f"✅ Задача '{batch}' завершена (100%)."
                                    await bot.send_message(telegram_user_id, message_text)
                                    notified_jobs.add(job_id)
                        else:
                            logger.error(f"Watcher: Error requesting jobs for user {telegram_user_id}: {resp.status}")
            except Exception as e:
                logger.error(f"Watcher: Error monitoring jobs for user {telegram_user_id}: {e}", exc_info=True) 