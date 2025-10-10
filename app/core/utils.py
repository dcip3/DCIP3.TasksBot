# app/core/utils.py
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
from typing import cast, Optional

from aiogram.types import (
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    WebAppInfo,
    FSInputFile,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.bot_core import bot, dp, init_aiosession, close_aiosession
from app.core.database import init_db, close_db
from app.integrations.video_helpers import compress_video_if_needed, get_file_size_mb

logger = logging.getLogger(__name__)

# ============================================================================
# === GLOBAL OBJECTS ===
# ============================================================================

# Scheduler for automated tasks
scheduler = AsyncIOScheduler(timezone="Europe/Moscow", job_defaults={'coalesce': True, 'max_instances': 1})

# Track jobs that have been notified about
notified_jobs = set()

# ============================================================================
# === PREVIEW HELPERS ===
# ============================================================================


async def _notify_preview_job_completion(
    telegram_user_id: int,
    job: dict,
    job_name: str,
    login: str,
    password: str,
) -> None:
    """Send ready preview video to the user when the ffmpeg job finishes."""
    props = job.get("Props", {})
    job_id = job.get("_id", "")

    local_path_hint = props.get("Ex0") or ""
    dropbox_path_hint = props.get("Ex1") or ""

    extra_dict = props.get("ExDic") or {}
    if not isinstance(extra_dict, dict):
        extra_dict = {}
    local_path_hint = extra_dict.get("PreviewLocal", local_path_hint)
    dropbox_path_hint = extra_dict.get("PreviewDropbox", dropbox_path_hint)

    for key in ("ExtraInfoKeyValue0", "ExtraInfoKeyValue1", "ExtraInfoKeyValue2"):
        value = props.get(key)
        if not value or "=" not in value:
            continue
        prefix, payload = value.split("=", 1)
        if prefix == "PreviewLocal":
            local_path_hint = payload
        elif prefix == "PreviewDropbox":
            dropbox_path_hint = payload

    final_path: Optional[Path] = None
    dropbox_path = dropbox_path_hint
    downloaded_temp = False

    if dropbox_path_hint:
        try:
            from app.services import download_video_from_dropbox

            result = await download_video_from_dropbox(
                login,
                password,
                job_id,
                dropbox_path_hint=dropbox_path_hint,
            )
            if result:
                final_path = Path(result[0])
                dropbox_path = result[1]
                downloaded_temp = True
        except Exception as download_error:
            logger.warning(
                "Failed to download preview video from Dropbox for job %s: %s",
                job_id,
                download_error,
            )

    if final_path is None:
        local_path = Path(local_path_hint) if local_path_hint else None
        if local_path is None:
            await bot.send_message(
                telegram_user_id,
                f"⚠️ Превью для задачи {job_name} создано, но путь к файлу не указан.",
            )
            logger.warning("Preview job %s has no recorded paths", job_id)
            return

        for _ in range(6):
            if local_path.exists():
                break
            await asyncio.sleep(5)

        if not local_path.exists():
            await bot.send_message(
                telegram_user_id,
                (
                    f"⚠️ Превью для задачи {job_name} завершено, но файл пока не найден по пути:\n"
                    f"{local_path}"
                ),
            )
            logger.warning("Preview file %s not found after job %s", local_path, job_id)
            return

        final_path = local_path
        dropbox_path = dropbox_path or dropbox_path_hint

    size_mb = get_file_size_mb(final_path)
    if size_mb > 45.0:
        final_path = await asyncio.to_thread(compress_video_if_needed, final_path, 45.0)
        size_mb = get_file_size_mb(final_path)

    caption_parts = [f"📁 {final_path.name}"]
    if dropbox_path:
        caption_parts.append(f"<code>{dropbox_path}</code>")
    caption = "\n".join(caption_parts)

    await bot.send_message(
        telegram_user_id,
        f"🎬 Превью для задачи {job_name} готово.",
    )
    await bot.send_video(
        telegram_user_id,
        FSInputFile(str(final_path)),
        caption=caption,
        parse_mode="HTML",
    )

    if downloaded_temp:
        await asyncio.to_thread(final_path.unlink, missing_ok=True)

    logger.info("Preview video sent to user %s for job %s", telegram_user_id, job_id)

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
            KeyboardButton(text="🔔 Notifications", request_contact=False, request_location=False),
        ],
        [
            KeyboardButton(text="🧹 Clear", request_contact=False, request_location=False),
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
        # Mini App URL: use localhost for development, HTTPS domain for production
        # mini_app_url defaults to the value provided in settings
        mini_app_url = settings.mini_app_url

        # Skip setup if URL is not HTTPS (Telegram requires HTTPS for menu buttons)
        from urllib.parse import urlparse
        parsed_url = urlparse(mini_app_url)
        if parsed_url.scheme.lower() != "https":
            logger.warning(
                "Skipping menu button setup because MINI_APP_URL is not HTTPS: %s",
                mini_app_url,
            )
            return
        
        logger.info(f"Setting up menu button with URL: {mini_app_url}")
        
        # Use the default aiogram 3.x method
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Tasks",
                web_app=WebAppInfo(url=mini_app_url)
            )
        )
        logger.info("Menu button setup successfully")
    except Exception as e:
        logger.error(f"Failed to setup menu button: {e}")
        # Try an alternative approach
        try:
            logger.info("Trying alternative method...")
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Tasks",
                    web_app=WebAppInfo(url=mini_app_url)
                ),
                chat_id=None  # Apply for all users
            )
            logger.info("Menu button setup successfully (alternative method)")
        except Exception as e2:
            logger.error(f"Alternative method also failed: {e2}")
            # Try a third approach using the BotFather API
            try:
                logger.info("Trying BotFather API method...")
                # This method might not work in aiogram 3.x, but it is worth a try
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text="Tasks",
                        web_app=WebAppInfo(url=mini_app_url)
                    ),
                    chat_id=0  # Global setting
                )
                logger.info("Menu button setup successfully (BotFather API method)")
            except Exception as e3:
                logger.error(f"All methods failed: {e3}")
                return

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
    Removes all files including .mp4 files (videos are stored in Dropbox).
    """
    # Clear temp directory completely
    clear_folder(Path(settings.temp_dir))
    
    # Clear conv directory completely (including .mp4 files)
    clear_folder(Path(settings.conv_dir))
    
    logger.info("Cleaned up temp and conv directories (removed all files)")


def force_cleanup_temp_and_conv() -> None:
    """
    Force clear ALL contents of 'temp' and 'conv' directories.
    Use this for error recovery.
    """
    # Clear temp directory completely
    clear_folder(Path(settings.temp_dir))
    
    # Clear conv directory completely
    clear_folder(Path(settings.conv_dir))
    
    logger.info("Force cleaned up temp and conv directories (removed all files)")


def cleanup_old_files(max_age_hours: int = 24) -> None:
    """
    Clean up old files in temp and conv directories.
    
    Args:
        max_age_hours: Maximum age of files in hours before deletion
    """
    import time
    from datetime import datetime, timezone, timedelta
    
    current_time = time.time()
    cutoff_time = current_time - (max_age_hours * 3600)
    
    temp_dir = Path(settings.temp_dir)
    conv_dir = Path(settings.conv_dir)
    
    cleaned_count = 0
    
    for directory in [temp_dir, conv_dir]:
        if not directory.exists():
            continue
            
        for item in directory.iterdir():
            try:
                # Check file age
                if item.stat().st_mtime < cutoff_time:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink()
                    cleaned_count += 1
                    logger.debug(f"Cleaned up old file: {item}")
            except Exception as e:
                logger.warning(f"Failed to clean up {item}: {e}")
    
    if cleaned_count > 0:
        logger.info(f"Cleaned up {cleaned_count} old files (older than {max_age_hours} hours)")


def get_directory_sizes() -> dict:
    """
    Get sizes of temp and conv directories.
    
    Returns:
        Dictionary with directory sizes in MB
    """
    temp_dir = Path(settings.temp_dir)
    conv_dir = Path(settings.conv_dir)
    
    def get_dir_size(path: Path) -> float:
        if not path.exists():
            return 0.0
        total_size = 0
        for item in path.rglob('*'):
            if item.is_file():
                total_size += item.stat().st_size
        return total_size / (1024 * 1024)  # Convert to MB
    
    return {
        'temp_mb': get_dir_size(temp_dir),
        'conv_mb': get_dir_size(conv_dir),
        'total_mb': get_dir_size(temp_dir) + get_dir_size(conv_dir)
    }


def log_directory_sizes() -> None:
    """
    Log the current sizes of temp and conv directories.
    """
    sizes = get_directory_sizes()
    logger.info(f"Directory sizes - Temp: {sizes['temp_mb']:.1f}MB, Conv: {sizes['conv_mb']:.1f}MB, Total: {sizes['total_mb']:.1f}MB")
    
    # Warning if total size is too large
    if sizes['total_mb'] > 1000:  # More than 1GB
        logger.warning(f"Large directory size detected: {sizes['total_mb']:.1f}MB total")


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


def get_video_duration(video_path: Path) -> Optional[float]:
    """
    Get video duration in seconds using ffprobe.
    
    Args:
        video_path (Path): Path to the video file
        
    Returns:
        Optional[float]: Duration in seconds or None if failed
    """
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as e:
        logger.error(f"Error getting video duration: {e}")
        return None


def make_progress_bar(percent: int, width: int = 10) -> str:
    """Return a simple unicode progress bar string."""
    filled = int(width * percent / 100)
    empty = width - filled
    return '█' * filled + '░' * empty


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
    
    await init_aiosession()
    logger.info("aiohttp session initialized")
    
    # Initialize database
    await init_db()
    logger.info("Database initialized")
    
    # Initialize directories
    ensure_temp_dir()
    Path(settings.conv_dir).mkdir(exist_ok=True)
    
    # Clean up any existing files on startup
    cleanup_temp_and_conv()
    logger.info("Startup cleanup completed")
    
    # Setup menu button for Mini App
    await setup_menu_button()
    logger.info("Menu button setup completed")
    
    # Start scheduler
    scheduler.start()
    logger.info("Scheduler started")
    
    # Schedule automatic cleanup tasks
    # Clean up old files every 6 hours
    scheduler.add_job(
        cleanup_old_files,
        CronTrigger(hour="*/6"),  # Every 6 hours
        args=[24],  # Remove files older than 24 hours
        id="cleanup_old_files",
        replace_existing=True
    )
    
    # Log directory sizes every hour
    scheduler.add_job(
        log_directory_sizes,
        CronTrigger(minute=0),  # Every hour at minute 0
        id="log_directory_sizes",
        replace_existing=True
    )
    
    logger.info("Scheduled cleanup tasks added")
    
    # Start job progress watcher
    asyncio.create_task(job_progress_watcher(bot))
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

    await close_aiosession()
    logger.info("aiohttp session closed")


async def job_progress_watcher(bot):
    """
    Monitor job completion and notify users about finished jobs.
    
    Args:
        bot: Bot instance for sending notifications
    """
    import aiohttp
    from datetime import datetime, timezone, timedelta
    from app.auth import (
        get_all_users_with_notifications,
        disable_notifications_for_user,
    )
    from app.core.bot_core import notified_jobs
    
    while True:
        await asyncio.sleep(60)
        
        users_with_notifications = await get_all_users_with_notifications()
        logger.info(f"Job progress watcher: Found {len(users_with_notifications)} users with notifications enabled")
        
        for telegram_user_id, login, password in users_with_notifications:
            try:
                async with aiohttp.ClientSession() as session:
                    headers = aiohttp.BasicAuth(login, password)
                    async with session.get(f"{settings.base_api_url}/jobs", auth=headers, ssl=False) as resp:
                        if resp.status == 200:
                            jobs = await resp.json()
                            for job in jobs:
                                job_id = job.get("_id", "")
                                if not job_id:
                                    continue

                                # Verify job status
                                stat = job.get("Stat", 0)
                                if stat == 3 and (job_id, telegram_user_id) not in notified_jobs:  # Completed
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
                                    props = job.get("Props", {})
                                    name = props.get("Name", "").split("/")[-1]
                                    comment = props.get("Cmmt", "")
                                    extra_dict = props.get("ExDic") or {}
                                    if not isinstance(extra_dict, dict):
                                        extra_dict = {}
                                    is_preview_job = (
                                        "Preview job generated by TasksBot" in comment
                                        or name.endswith(" - Preview")
                                        or extra_dict.get("PreviewJob") == "1"
                                    )

                                    if is_preview_job:
                                        await _notify_preview_job_completion(
                                            telegram_user_id,
                                            job,
                                            name,
                                            login,
                                            password,
                                        )
                                        notified_jobs.add((job_id, telegram_user_id))
                                        continue

                                    batch = props.get("Batch", "Без серии")
                                    message_text = (
                                        "✅ Задача завершена:\n"
                                        f"• Серия: {batch}\n"
                                        f"• Имя: {name}"
                                    )

                                    preview_markup = InlineKeyboardMarkup(
                                        inline_keyboard=[
                                            [
                                                InlineKeyboardButton(
                                                    text="🔍 Превью",
                                                    callback_data=f"preview_job:{job_id}"
                                                )
                                            ]
                                        ]
                                    )

                                    await bot.send_message(
                                        telegram_user_id,
                                        message_text,
                                        reply_markup=preview_markup
                                    )
                                    logger.info(f"Уведомление отправлено пользователю {telegram_user_id} по задаче {job_id} ({name})")
                                    notified_jobs.add((job_id, telegram_user_id))
                        elif resp.status == 401:
                            logger.warning(
                                "Watcher: Unauthorized for user %s. Disabling notifications and requesting re-login.",
                                telegram_user_id
                            )
                            await disable_notifications_for_user(telegram_user_id)
                            await bot.send_message(
                                telegram_user_id,
                                "⚠️ Авторизация истекла. Пожалуйста, выполните /login заново, чтобы продолжить получать уведомления."
                            )
                        else:
                            logger.error(f"Watcher: Error requesting jobs for user {telegram_user_id}: {resp.status}")
            except Exception as e:
                logger.error(f"Watcher: Error monitoring jobs for user {telegram_user_id}: {e}", exc_info=True) 
