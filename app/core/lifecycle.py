"""Application lifecycle: startup, shutdown, scheduler, and watcher orchestration."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.bot_core import close_aiosession, init_aiosession
from app.core.config import settings
from app.storage.database import close_db, init_db
from app.core.maintenance import (
    cleanup_old_files,
    cleanup_temp_and_conv,
    ensure_temp_dir,
    log_directory_sizes,
)

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(
    timezone=settings.scheduler_timezone,
    job_defaults={"coalesce": True, "max_instances": 1},
)
job_watcher_task: Optional[asyncio.Task] = None


async def on_startup(bot) -> None:
    """Application startup handler."""
    logger.info("Starting TasksBot...")

    await init_aiosession()
    logger.info("aiohttp session initialized")

    await init_db()
    logger.info("Database initialized")

    from app.core.preview_upload import recover_preview_uploads, start_preview_upload_server

    await start_preview_upload_server()

    ensure_temp_dir()
    cleanup_temp_and_conv()
    recovered = await recover_preview_uploads()
    if recovered:
        logger.info("Scheduled %s pending preview upload recovery task(s)", recovered)
    logger.info("Startup cleanup completed")

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Start the bot"),
            BotCommand(command="login", description="Authenticate to the bot"),
            BotCommand(command="logout", description="End the current session"),
            BotCommand(command="help", description="Show help"),
        ]
    )
    logger.info("Bot commands registered")

    scheduler.start()
    logger.info("Scheduler started")

    _schedule_maintenance_jobs()
    logger.info("Scheduled cleanup tasks added")

    global job_watcher_task
    if job_watcher_task is None or job_watcher_task.done():
        from app.services.job_watcher import job_progress_watcher

        job_watcher_task = asyncio.create_task(job_progress_watcher(bot))
        logger.info("Job progress watcher started")


def _schedule_maintenance_jobs() -> None:
    """Add the periodic cleanup and housekeeping jobs to the scheduler.

    Each trigger is given the scheduler's zone explicitly. A trigger object
    passed to add_job keeps the zone it was built with, and one built without
    a zone uses the host's local zone, not the scheduler's.
    """
    zone = scheduler.timezone

    scheduler.add_job(
        cleanup_old_files,
        CronTrigger(hour="*/6", timezone=zone),
        args=[24],
        id="cleanup_old_files",
        replace_existing=True,
    )

    scheduler.add_job(
        log_directory_sizes,
        CronTrigger(minute=0, timezone=zone),
        id="log_directory_sizes",
        replace_existing=True,
    )

    def log_cache_stats() -> None:
        from app.services.job_state import notified_jobs

        stats = notified_jobs.get_stats()
        logger.info("TTL Cache stats: %s", stats)

    scheduler.add_job(
        log_cache_stats,
        CronTrigger(minute=0, timezone=zone),
        id="log_cache_stats",
        replace_existing=True,
    )

    async def cleanup_preview_tokens() -> None:
        from app.core.preview_upload import cleanup_preview_upload_tokens

        await cleanup_preview_upload_tokens()

    scheduler.add_job(
        cleanup_preview_tokens,
        CronTrigger(minute=0, timezone=zone),
        id="cleanup_preview_tokens",
        replace_existing=True,
    )

    async def recover_preview_upload_tokens() -> None:
        from app.core.preview_upload import recover_preview_uploads

        recovered_count = await recover_preview_uploads()
        if recovered_count:
            logger.info(
                "Scheduled %s preview upload delivery retry task(s)",
                recovered_count,
            )

    if settings.preview_upload_enabled:
        scheduler.add_job(
            recover_preview_upload_tokens,
            IntervalTrigger(
                seconds=settings.preview_upload_recovery_interval_seconds,
                timezone=zone,
            ),
            id="recover_preview_uploads",
            replace_existing=True,
        )


async def on_shutdown(bot) -> None:
    """Application shutdown handler."""
    logger.info("Shutting down TasksBot...")

    scheduler.shutdown()
    logger.info("Scheduler shutdown")

    global job_watcher_task
    if job_watcher_task and not job_watcher_task.done():
        job_watcher_task.cancel()
        try:
            await job_watcher_task
        except asyncio.CancelledError:
            logger.info("Job progress watcher cancelled")
        except Exception as exc:
            logger.warning("Job progress watcher failed during shutdown: %s", exc)
    job_watcher_task = None

    from app.core.preview_upload import stop_preview_upload_server

    await stop_preview_upload_server()

    await close_db()
    logger.info("Database connection closed")

    cleanup_temp_and_conv()
    logger.info("Temp files cleaned up")

    await close_aiosession()
    logger.info("aiohttp session closed")
