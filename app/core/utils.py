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
import contextlib
import html
import logging
import os
import shutil
from functools import wraps
from pathlib import Path
from typing import cast, Optional, Tuple, List, Dict, Any

from aiogram.types import (
    BotCommand,
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    FSInputFile,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.bot_core import bot, dp, init_aiosession, close_aiosession, auto_preview_jobs
from app.core.database import init_db, close_db
from app.integrations.video_helpers import prepare_video_for_delivery, get_file_size_mb

logger = logging.getLogger(__name__)

# ============================================================================
# === GLOBAL OBJECTS ===
# ============================================================================

# Scheduler for automated tasks
scheduler = AsyncIOScheduler(timezone="Europe/Moscow", job_defaults={'coalesce': True, 'max_instances': 1})

# Background task handle for job watcher
job_watcher_task: Optional[asyncio.Task] = None

# Track preview submission progress messages (preview_job_id -> (chat_id, message_id))
preview_message_registry: dict[str, tuple[int, int]] = {}
preview_animation_tasks: dict[str, asyncio.Task] = {}

# ============================================================================
# === PREVIEW HELPERS ===
# ============================================================================


def register_preview_message(preview_job_id: str, chat_id: int, message_id: int) -> None:
    """Store the progress message info for a preview job."""
    preview_message_registry[preview_job_id] = (chat_id, message_id)
    existing = preview_animation_tasks.get(preview_job_id)
    if existing and not existing.done():
        existing.cancel()
    preview_animation_tasks[preview_job_id] = asyncio.create_task(
        _run_preview_animation(preview_job_id, chat_id, message_id)
    )


def pop_preview_message(preview_job_id: str) -> Optional[tuple[int, int]]:
    """Retrieve and remove stored progress message for a preview job."""
    info = preview_message_registry.pop(preview_job_id, None)
    task = preview_animation_tasks.pop(preview_job_id, None)
    if task and not task.done():
        task.cancel()
    return info


async def _run_preview_animation(preview_job_id: str, chat_id: int, message_id: int) -> None:
    """Animate the preview queued message until the job finishes."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    frames = ["□ □ □", "■ □ □", "■ ■ □", "■ ■ ■"]
    index = 1  # start from next frame to avoid "message is not modified"

    # Create cancel keyboard
    cancel_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Cancel", callback_data=f"preview_job_cancel:{preview_job_id}")]
        ]
    )

    try:
        while preview_message_registry.get(preview_job_id) == (chat_id, message_id):
            frame = frames[index % len(frames)]
            text = f"✅ Preview job queued\n{frame}"
            try:
                await bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=cancel_keyboard,
                )
            except Exception as edit_error:
                message = str(edit_error).lower()
                if "message is not modified" in message:
                    index += 1
                    await asyncio.sleep(1)
                    continue
                logger.debug(
                    "Preview animation edit failed for job %s: %s",
                    preview_job_id,
                    edit_error,
                )
                return
            index += 1
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        logger.debug("Preview animation task cancelled for job %s", preview_job_id)
        return

# ============================================================================
# === PREVIEW HELPERS ===
# ============================================================================


def _extract_preview_context(
    props: Dict[str, Any],
    default_user_id: int,
) -> Tuple[str, str, int, Dict[str, Any], Optional[str]]:
    """Extract preview metadata from job properties.

    Returns local/dropbox path hints, resolved target Telegram user ID,
    a sanitized copy of the Extra dictionary, and optional source job ID.
    """

    local_path_hint = props.get("Ex0") or ""
    dropbox_path_hint = props.get("Ex1") or ""

    extra_dict = props.get("ExDic") or {}
    if not isinstance(extra_dict, dict):
        extra_dict = {}

    local_path_hint = extra_dict.get("PreviewLocal", local_path_hint)
    dropbox_path_hint = extra_dict.get("PreviewDropbox", dropbox_path_hint)
    preview_user_id_str = extra_dict.get("PreviewTelegram")
    preview_source_id = extra_dict.get("PreviewSource")

    for key in (
        "ExtraInfoKeyValue0",
        "ExtraInfoKeyValue1",
        "ExtraInfoKeyValue2",
        "ExtraInfoKeyValue3",
        "ExtraInfoKeyValue4",
        "ExtraInfoKeyValue5",
    ):
        value = props.get(key)
        if not value or "=" not in value:
            continue
        prefix, payload = value.split("=", 1)
        if prefix == "PreviewLocal":
            local_path_hint = payload
        elif prefix == "PreviewDropbox":
            dropbox_path_hint = payload
        elif prefix == "PreviewTelegram":
            preview_user_id_str = payload
        elif prefix == "PreviewSource":
            preview_source_id = payload

    target_user_id = default_user_id
    if preview_user_id_str:
        try:
            target_user_id = int(str(preview_user_id_str).strip())
        except (TypeError, ValueError):
            logger.warning(
                "Invalid PreviewTelegram value '%s' in preview job metadata",
                preview_user_id_str,
            )

    source_job_id: Optional[str] = None
    if preview_source_id:
        source_job_id = str(preview_source_id).strip() or None

    return local_path_hint, dropbox_path_hint, target_user_id, extra_dict, source_job_id


async def _notify_preview_job_completion(
    telegram_user_id: int,
    job: dict,
    job_name: str,
    login: str,
    password: str,
) -> Optional[int]:
    """Send ready preview video to the user when the ffmpeg job finishes.

    Returns the Telegram user ID that received the notification, or ``None``
    if the notification could not be delivered.
    """
    props = job.get("Props", {})
    job_id = job.get("_id", "")

    (
        local_path_hint,
        dropbox_path_hint,
        target_user_id,
        extra_dict,
        source_job_id,
    ) = _extract_preview_context(props, telegram_user_id)

    final_path: Optional[Path] = None
    dropbox_path = dropbox_path_hint
    downloaded_temp = False

    if dropbox_path_hint:
        try:
            from app.services import download_video_from_dropbox

            retry_delays = [0, 2, 4, 6, 10, 20, 40, 80, 138]
            for delay in retry_delays:
                if delay:
                    await asyncio.sleep(delay)
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
                    break
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
                target_user_id,
                f"⚠️ Preview for {job_name} is ready, but the file path is missing.",
            )
            logger.warning("Preview job %s has no recorded paths", job_id)
            return None

        if not local_path.exists():
            retry_delays = [0, 2, 4, 6, 10, 15, 20]
            for delay in retry_delays:
                if delay:
                    await asyncio.sleep(delay)
                if local_path.exists():
                    break

        if not local_path.exists():
            retry_markup = None
            if source_job_id:
                retry_markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🔁 Recreate preview",
                                callback_data=f"preview_render_options:{source_job_id}",
                            )
                        ]
                    ]
                )

            message_text = (
                f"⚠️ Preview for {job_name} finished, but the file is still not available at:\n"
                f"{local_path}\n\n"
                "Possible reasons: the preview upload token expired or the path is not accessible "
                "from the bot host. The preview job will be removed."
            )

            stored_message = pop_preview_message(job_id)
            if stored_message:
                chat_id, message_id = stored_message
                try:
                    await bot.edit_message_text(
                        message_text,
                        chat_id=chat_id,
                        message_id=message_id,
                        reply_markup=retry_markup,
                    )
                except Exception as edit_error:
                    logger.warning(
                        "Failed to edit preview progress message for job %s: %s",
                        job_id,
                        edit_error,
                    )
                    await bot.send_message(
                        target_user_id,
                        message_text,
                        reply_markup=retry_markup,
                    )
            else:
                await bot.send_message(
                    target_user_id,
                    message_text,
                    reply_markup=retry_markup,
                )

            try:
                from app.services import delete_job

                deleted = await delete_job(login, password, job_id)
                if deleted:
                    logger.info("Preview job %s deleted after missing output", job_id)
                else:
                    logger.warning("Failed to delete preview job %s after missing output", job_id)
            except Exception as delete_error:
                logger.warning(
                    "Error deleting preview job %s after missing output: %s",
                    job_id,
                    delete_error,
                )

            logger.warning("Preview file %s not found after job %s", local_path, job_id)
            return target_user_id

        final_path = local_path
        dropbox_path = dropbox_path or dropbox_path_hint

    from app.core.path_utils import normalize_display_path

    max_video_size_mb = 45.0
    size_mb = get_file_size_mb(final_path)
    path_hint = dropbox_path if dropbox_path else str(final_path)
    display_path = normalize_display_path(path_hint) or str(final_path)
    fallback_message = None
    if size_mb > max_video_size_mb:
        location_hint = f"<code>{display_path}</code>"
        fallback_message = (
            "⚠️ Preview video is ready but too large to send via Telegram "
            f"({size_mb:.1f} MB > {max_video_size_mb:.0f} MB).\n"
            f"Please download it manually:\n{location_hint}"
        )

    caption_parts = [f"📁 {final_path.name}"]
    if display_path:
        caption_parts.append(f"<code>{display_path}</code>")
    caption = "\n".join(caption_parts)

    ready_text = f"🎬 Preview for {job_name} is ready."
    target_chat_id = target_user_id
    stored_message = pop_preview_message(job_id)
    if stored_message:
        chat_id, message_id = stored_message
        target_chat_id = chat_id
        try:
            await bot.edit_message_text(
                ready_text,
                chat_id=chat_id,
                message_id=message_id,
            )
        except Exception as edit_error:
            logger.warning(
                "Failed to edit preview progress message for job %s: %s",
                job_id,
                edit_error,
            )
            target_chat_id = target_user_id
            await bot.send_message(target_chat_id, ready_text)
    else:
        await bot.send_message(
            target_chat_id,
            ready_text,
        )
    if fallback_message is None:
        await bot.send_video(
            target_chat_id,
            FSInputFile(str(final_path)),
            caption=caption,
            parse_mode="HTML",
        )
    else:
        logger.warning(
            "Preview video %s is %.1f MB; sending fallback message",
            final_path,
            size_mb,
        )
        await bot.send_message(
            target_chat_id,
            fallback_message,
            parse_mode="HTML",
        )

    if downloaded_temp:
        await asyncio.to_thread(final_path.unlink, missing_ok=True)

    try:
        from app.services import delete_job

        deleted = await delete_job(login, password, job_id)
        if deleted:
            logger.info("Preview job %s deleted from Deadline after completion", job_id)
        else:
            logger.warning("Failed to delete preview job %s from Deadline", job_id)
    except Exception as delete_error:
        logger.warning(
            "Error deleting preview job %s from Deadline: %s",
            job_id,
            delete_error,
        )

    logger.info("Preview video sent to user %s for job %s", target_user_id, job_id)
    return target_user_id

async def _notify_preview_job_failure(
    telegram_user_id: int,
    job: dict,
    job_name: str,
    login: str,
    password: str,
) -> Optional[int]:
    """Notify requester about a failed preview job and include worker error details."""

    job_id = job.get("_id", "")
    props = job.get("Props", {})

    (
        _local_hint,
        _dropbox_hint,
        target_user_id,
        _extra_dict,
        source_job_id,
    ) = _extract_preview_context(props, telegram_user_id)

    safe_name = html.escape(job_name)
    failure_title = f"❌ Preview for {safe_name} failed."
    stored_message = pop_preview_message(job_id)
    edited_progress: Optional[Tuple[int, int]] = None
    if stored_message:
        chat_id, message_id = stored_message
        try:
            await bot.edit_message_text(
                failure_title,
                chat_id=chat_id,
                message_id=message_id,
            )
            edited_progress = (chat_id, message_id)
        except Exception as edit_error:
            logger.warning(
                "Failed to edit preview progress message for job %s failure: %s",
                job_id,
                edit_error,
            )
            edited_progress = None

    worker_names: List[str] = []
    try:
        from app.services import get_job_tasks, get_worker_report_contents

        tasks = await get_job_tasks(login, password, job_id)
        for task in tasks:
            if not isinstance(task, dict):
                continue
            worker = task.get("Slave") or task.get("Worker") or task.get("Machine")
            if worker:
                worker_names.append(str(worker))
    except Exception as task_error:
        logger.warning(
            "Could not gather worker names for failed preview job %s: %s",
            job_id,
            task_error,
        )

    if not worker_names:
        fallback_worker = props.get("Mach") or props.get("Slave")
        if fallback_worker:
            worker_names.append(str(fallback_worker))

    worker_names = list(dict.fromkeys(worker_names))

    error_segments: List[str] = []
    try:
        if worker_names:
            reports = await get_worker_report_contents(login, password, worker_names)
            for report in reports:
                report_type = report.get("type")
                if str(report_type) not in {"1", "ErrorReport"}:
                    continue
                contents = report.get("contents") or ""
                if not contents:
                    continue
                worker_label = html.escape(str(report.get("worker") or "Unknown worker"))
                title_raw = report.get("title")
                header = f"• {worker_label}"
                if title_raw:
                    header += f" — {html.escape(str(title_raw))}"
                snippet_lines = contents.strip().splitlines()
                if not snippet_lines:
                    continue
                snippet = "\n".join(snippet_lines[:20])
                escaped_snippet = html.escape(snippet)
                error_segments.append(f"{header}\n<pre>{escaped_snippet}</pre>")
    except Exception as reports_error:
        logger.warning(
            "Failed to fetch worker reports for job %s: %s",
            job_id,
            reports_error,
        )

    if error_segments:
        body = "\n\n".join(error_segments)
    else:
        body = "No worker error logs were retrieved."

    full_message = f"{failure_title}\n\n{body}"
    retry_markup = None
    if source_job_id:
        retry_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔁 Recreate preview",
                        callback_data=f"preview_render_options:{source_job_id}",
                    )
                ]
            ]
        )
    if edited_progress is not None:
        chat_id, message_id = edited_progress
        try:
            await bot.edit_message_text(
                full_message,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="HTML",
                reply_markup=retry_markup,
            )
        except Exception as edit_error:
            logger.warning(
                "Failed to update preview failure message for job %s: %s",
                job_id,
                edit_error,
            )
            await bot.send_message(
                target_user_id,
                full_message,
                parse_mode="HTML",
                reply_markup=retry_markup,
            )
    else:
        await bot.send_message(
            target_user_id,
            full_message,
            parse_mode="HTML",
            reply_markup=retry_markup,
        )

    try:
        from app.services import delete_job

        deleted = await delete_job(login, password, job_id)
        if deleted:
            logger.info("Failed preview job %s deleted from Deadline", job_id)
        else:
            logger.warning("Failed to delete preview job %s from Deadline", job_id)
    except Exception as delete_error:
        logger.warning(
            "Error deleting failed preview job %s: %s",
            job_id,
            delete_error,
        )

    return target_user_id


# ============================================================================
# === DECORATORS ===
# ============================================================================


async def _send_dropbox_video_to_user(
    telegram_user_id: int,
    login: str,
    password: str,
    job_id: str,
    dropbox_path_hint: Optional[str] = None,
) -> bool:
    """Download a preview from Dropbox and deliver it to the user."""
    from app.services import download_video_from_dropbox
    from app.integrations.video_helpers import cleanup_job_files

    progress_msg = await bot.send_message(
        telegram_user_id,
        "📥 Auto preview: downloading existing video from Dropbox...",
    )
    try:
        download_result = await download_video_from_dropbox(
            login,
            password,
            job_id,
            dropbox_path_hint=dropbox_path_hint,
        )
    except Exception as exc:
        logger.error("Auto preview Dropbox download failed for job %s: %s", job_id, exc)
        with contextlib.suppress(Exception):
            await progress_msg.edit_text("❌ Auto preview: failed to download from Dropbox.")
        return False

    if not download_result:
        with contextlib.suppress(Exception):
            await progress_msg.delete()
        return False

    video_path, dropbox_path = download_result
    video_path_obj = Path(video_path)
    try:
        preparation = await prepare_video_for_delivery(
            video_path_obj,
            dropbox_path,
        )
        caption_lines = [f"📁 {video_path_obj.stem}"]
        if dropbox_path:
            caption_lines.append(f"<code>{dropbox_path}</code>")
        else:
            caption_lines.append(f"<code>{video_path}</code>")
        caption = "\n".join(caption_lines)

        if preparation.fallback_message:
            await bot.send_message(
                telegram_user_id,
                preparation.fallback_message,
                parse_mode="HTML",
            )
        else:
            await bot.send_video(
                telegram_user_id,
                FSInputFile(str(preparation.video_path)),
                caption=caption,
                parse_mode="HTML",
            )
        with contextlib.suppress(Exception):
            await progress_msg.delete()
        return True
    except Exception as exc:
        logger.error("Auto preview send failed for job %s: %s", job_id, exc)
        with contextlib.suppress(Exception):
            await progress_msg.edit_text("❌ Auto preview: failed to send video.")
        return False
    finally:
        with contextlib.suppress(Exception):
            video_path_obj.unlink()
        with contextlib.suppress(Exception):
            cleanup_job_files(job_id)
        with contextlib.suppress(Exception):
            cleanup_old_files(max_age_hours=6)


async def _submit_auto_preview_deadline(
    telegram_user_id: int,
    job_id: str,
    job_name: str,
    default_worker: Optional[str],
) -> None:
    """Submit a Deadline preview job and register progress tracking."""
    from app.services import create_video_from_job, WorkerStatusError

    result = None
    fallback_used = False
    try:
        result = await create_video_from_job(
            telegram_user_id,
            job_id,
            specific_worker=default_worker,
        )
    except WorkerStatusError as worker_error:
        logger.warning(
            "Auto preview default worker unavailable for job %s: %s",
            job_id,
            worker_error,
        )
        if default_worker:
            fallback_used = True
        try:
            result = await create_video_from_job(
                telegram_user_id,
                job_id,
                use_any_machine=True,
                skip_worker_validation=True,
            )
        except Exception as exc:
            logger.error("Auto preview fallback submission failed for job %s: %s", job_id, exc)
            result = None
    except Exception as exc:
        logger.error("Auto preview submission failed for job %s: %s", job_id, exc)
        result = None

    if not result:
        await bot.send_message(
            telegram_user_id,
            "❌ Auto preview: failed to submit preview job.",
        )
        return

    preview_id = result.get("preview_job_id")
    header = f"🧾 Auto preview queued for {job_name}"
    if fallback_used:
        header += " (any worker)"
    message_text = f"{header}\n□ □ □"
    cancel_keyboard = (
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✖️ Cancel",
                        callback_data=f"preview_job_cancel:{preview_id}",
                    )
                ]
            ]
        )
        if preview_id
        else None
    )
    progress_msg = await bot.send_message(
        telegram_user_id,
        message_text,
        reply_markup=cancel_keyboard,
    )
    if preview_id:
        register_preview_message(preview_id, progress_msg.chat.id, progress_msg.message_id)


async def _run_auto_preview_for_job(
    telegram_user_id: int,
    job_id: str,
    job_name: str,
    login: str,
    password: str,
    preview_method: Optional[str],
    default_worker: Optional[str],
) -> None:
    """Dispatch auto preview creation based on the user's configured method."""
    if preview_method not in {"server", "deadline"}:
        logger.info(
            "Auto preview skipped for job %s: no default method set for user %s",
            job_id,
            telegram_user_id,
        )
        return

    try:
        sent_existing = await _send_dropbox_video_to_user(
            telegram_user_id,
            login,
            password,
            job_id,
        )
        if sent_existing:
            return
    except Exception as exc:
        logger.warning("Auto preview Dropbox send failed for job %s: %s", job_id, exc)

    try:
        from types import SimpleNamespace
        from app.bot.handlers.preview import render_preview_via_server, _maybe_send_single_frame_preview

        class _AutoCallback:
            def __init__(self, user_id: int) -> None:
                self.from_user = SimpleNamespace(id=user_id)
                self.message = None

            async def answer(self, *args, **kwargs) -> None:
                return None

        auto_callback = _AutoCallback(telegram_user_id)

        if preview_method == "deadline":
            if await _maybe_send_single_frame_preview(auto_callback, job_id):
                return
            await _submit_auto_preview_deadline(
                telegram_user_id,
                job_id,
                job_name,
                default_worker,
            )
            return

        await render_preview_via_server(auto_callback, job_id)
    except Exception as exc:
        logger.error("Auto preview workflow failed for job %s: %s", job_id, exc)

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
            KeyboardButton(text="📂 Jobs", request_contact=False, request_location=False),
            KeyboardButton(text="🖥️ Workers", request_contact=False, request_location=False),
        ],
        [
            KeyboardButton(text="⚙️ Settings", request_contact=False, request_location=False),
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


def _cleanup_preview_temp_dir() -> None:
    preview_dir = settings.preview_temp_dir
    if not preview_dir:
        return

    sentinel_values = {"local", "auto", "default", "system"}
    if preview_dir.strip().lower() in sentinel_values:
        logger.debug("Skipping preview temp cleanup for sentinel value '%s'", preview_dir)
        return

    try:
        expanded = os.path.expandvars(os.path.expanduser(preview_dir))
        # If expansion failed (still contains % or $), skip cleanup to avoid creating bogus paths
        if any(symbol in expanded for symbol in ("%", "$")) and expanded == preview_dir:
            logger.debug("Skipping preview temp cleanup; unresolved env vars in %s", preview_dir)
            return

        preview_path = Path(expanded)
        if not preview_path.exists():
            return

        for item in preview_path.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as preview_error:
        logger.warning(
            "Failed to clean preview temp directory %s: %s",
            preview_dir,
            preview_error,
        )


def cleanup_temp_and_conv() -> None:
    """
    Clear contents of 'temp' and 'conv' directories.
    Removes all files including .mp4 files (videos are stored in Dropbox).
    """
    # Clear temp directory completely
    clear_folder(Path(settings.temp_dir))

    # Clear conv directory completely (including .mp4 files)
    clear_folder(Path(settings.conv_dir))

    # Clear preview temp directory if configured (best effort; intended for deadlines workers sharing storage)
    _cleanup_preview_temp_dir()

    logger.info("Cleaned up temp, conv, and preview directories (where accessible)")


def force_cleanup_temp_and_conv() -> None:
    """
    Force clear ALL contents of 'temp' and 'conv' directories.
    Use this for error recovery.
    """
    # Clear temp directory completely
    clear_folder(Path(settings.temp_dir))
    
    # Clear conv directory completely
    clear_folder(Path(settings.conv_dir))

    # Clear preview temp directory if configured (best effort)
    _cleanup_preview_temp_dir()

    logger.info("Force cleaned up temp, conv, and preview directories (where accessible)")


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
    preview_dir = None
    if settings.preview_temp_dir:
        sentinel_values = {"local", "auto", "default", "system"}
        if settings.preview_temp_dir.strip().lower() not in sentinel_values:
            expanded = os.path.expandvars(os.path.expanduser(settings.preview_temp_dir))
            if not (any(symbol in expanded for symbol in ("%", "$")) and expanded == settings.preview_temp_dir):
                preview_dir = Path(expanded)

    cleaned_count = 0

    directories_to_clean = [temp_dir, conv_dir]
    if preview_dir:
        directories_to_clean.append(preview_dir)

    for directory in directories_to_clean:
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

    from app.core.preview_upload import start_preview_upload_server
    await start_preview_upload_server()

    # Initialize directories
    ensure_temp_dir()
    Path(settings.conv_dir).mkdir(exist_ok=True)
    
    # Clean up any existing files on startup
    cleanup_temp_and_conv()
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

    # Log TTL cache statistics every hour
    def log_cache_stats():
        from app.core.bot_core import notified_jobs
        stats = notified_jobs.get_stats()
        logger.info(f"TTL Cache stats: {stats}")

    scheduler.add_job(
        log_cache_stats,
        CronTrigger(minute=0),  # Every hour at minute 0
        id="log_cache_stats",
        replace_existing=True
    )

    # Clean up expired preview upload tokens every hour
    def cleanup_preview_tokens():
        from app.core.preview_upload import cleanup_preview_upload_tokens
        cleanup_preview_upload_tokens()

    scheduler.add_job(
        cleanup_preview_tokens,
        CronTrigger(minute=0),  # Every hour at minute 0
        id="cleanup_preview_tokens",
        replace_existing=True
    )

    logger.info("Scheduled cleanup tasks added")
    
    # Start job progress watcher
    global job_watcher_task
    if job_watcher_task is None or job_watcher_task.done():
        job_watcher_task = asyncio.create_task(job_progress_watcher(bot))
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

    # Stop job progress watcher
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
    Also monitors preview jobs for ALL users (regardless of notification settings).

    Uses adaptive polling interval:
    - 15 seconds when there are active preview jobs
    - 60 seconds for regular monitoring

    Args:
        bot: Bot instance for sending notifications
    """
    import aiohttp
    from datetime import datetime, timezone, timedelta
    from app.auth import (
        _decrypt_password,
        _normalize_scope,
        VALID_PREVIEW_RENDER_METHODS,
        disable_notifications_for_user,
    )
    from app.core.bot_core import notified_jobs, get_aiosession, auto_preview_jobs
    from app.core.database import get_db_connection

    try:
        while True:
            # Adaptive polling interval based on active preview jobs
            has_active_previews = len(preview_message_registry) > 0
            sleep_interval = settings.job_watcher_interval_preview if has_active_previews else settings.job_watcher_interval_normal

            if has_active_previews:
                logger.debug(f"Active preview jobs: {len(preview_message_registry)}, using fast polling ({sleep_interval}s)")

            await asyncio.sleep(sleep_interval)

            conn = get_db_connection()
            user_rows = []
            if conn:
                try:
                    async with conn.execute(
                        """
                        SELECT telegram_user_id,
                               deadline_login,
                               deadline_password,
                               notifications_enabled,
                               notification_scope,
                               preview_default_method,
                               preview_default_worker,
                               preview_auto_enabled
                        FROM user_sessions
                        """
                    ) as cursor:
                        user_rows = await cursor.fetchall()
                except Exception as e:
                    logger.error("Error fetching users for monitoring: %s", e)

            users = []
            for row in user_rows:
                (
                    user_id,
                    login,
                    password,
                    notifications_enabled,
                    scope_raw,
                    preview_method_raw,
                    preview_worker,
                    preview_auto_enabled,
                ) = row
                try:
                    decrypted_password = _decrypt_password(password)
                except Exception:
                    decrypted_password = password

                scope = _normalize_scope(scope_raw)
                preview_method = (preview_method_raw or "").strip().lower()
                if preview_method not in VALID_PREVIEW_RENDER_METHODS:
                    preview_method = None

                users.append(
                    (
                        user_id,
                        login,
                        decrypted_password,
                        bool(notifications_enabled),
                        scope,
                        preview_method,
                        preview_worker,
                        bool(preview_auto_enabled),
                    )
                )

            notify_count = sum(1 for item in users if item[3])
            auto_preview_count = sum(1 for item in users if item[7])
            logger.info(
                "Job progress watcher: Monitoring %d users (%d notifications, %d auto previews)",
                len(users),
                notify_count,
                auto_preview_count,
            )

            def job_matches_scope(scope_value: str, login_value: str, props: dict, job_entry: dict) -> bool:
                if scope_value != "own":
                    return True
                job_owner = props.get("User") or job_entry.get("UserName") or ""
                if not job_owner:
                    return False
                normalized_login = str(login_value).split("\\")[-1].split("/")[-1].lower()
                normalized_owner = str(job_owner).split("\\")[-1].split("/")[-1].lower()
                return normalized_owner == normalized_login

            for (
                telegram_user_id,
                login,
                decrypted_password,
                has_notifications,
                scope,
                preview_method,
                preview_worker,
                auto_preview_enabled,
            ) in users:

                try:
                    session = await get_aiosession()
                    headers = aiohttp.BasicAuth(login, decrypted_password)
                    async with session.get(f"{settings.deadline_api_url}/jobs", auth=headers, ssl=False) as resp:
                        if resp.status == 200:
                            jobs = await resp.json()
                            for job in jobs:
                                job_id = job.get("_id", "")
                                if not job_id:
                                    continue

                                # Verify job status
                                stat = job.get("Stat", 0)

                                props = job.get("Props", {})
                                name = props.get("Name", "").split("/")[-1]
                                comment = props.get("Cmmt", "")
                                extra_dict = props.get("ExDic") or {}
                                if not isinstance(extra_dict, dict):
                                    extra_dict = {}

                                preview_owner_str = extra_dict.get("PreviewTelegram")
                                for key in (
                                    "ExtraInfoKeyValue0",
                                    "ExtraInfoKeyValue1",
                                    "ExtraInfoKeyValue2",
                                    "ExtraInfoKeyValue3",
                                    "ExtraInfoKeyValue4",
                                ):
                                    value = props.get(key)
                                    if not value or "=" not in value:
                                        continue
                                    prefix, payload = value.split("=", 1)
                                    if prefix == "PreviewTelegram":
                                        preview_owner_str = payload

                                preview_owner_id: Optional[int] = None
                                if preview_owner_str:
                                    try:
                                        preview_owner_id = int(str(preview_owner_str).strip())
                                    except (TypeError, ValueError):
                                        logger.warning(
                                            "Invalid PreviewTelegram value '%s' for job %s",
                                            preview_owner_str,
                                            job_id,
                                        )

                                is_preview_job = (
                                    "Preview job generated by TasksBot" in comment
                                    or name.endswith(" - Preview")
                                    or extra_dict.get("PreviewJob") == "1"
                                )

                                if is_preview_job and preview_owner_id is not None and preview_owner_id != telegram_user_id:
                                    continue

                                target_user_for_cache = (
                                    preview_owner_id if preview_owner_id is not None else telegram_user_id
                                )
                                notified_key = (job_id, target_user_for_cache)

                                if stat == 4 and is_preview_job:
                                    if notified_key in notified_jobs:
                                        continue

                                    notified_user_id = await _notify_preview_job_failure(
                                        telegram_user_id,
                                        job,
                                        name,
                                        login,
                                        decrypted_password,
                                    )
                                    resolved_user_id = (
                                        notified_user_id or preview_owner_id or telegram_user_id
                                    )
                                    notified_jobs.add((job_id, resolved_user_id))
                                    continue

                                if stat != 3:
                                    continue

                                date_comp_str = job.get("DateComp") or props.get("DateComp")
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

                                if is_preview_job:
                                    if notified_key in notified_jobs:
                                        continue
                                    notified_user_id = await _notify_preview_job_completion(
                                        telegram_user_id,
                                        job,
                                        name,
                                        login,
                                        decrypted_password,
                                    )
                                    resolved_user_id = (
                                        notified_user_id or preview_owner_id or telegram_user_id
                                    )
                                    notified_jobs.add((job_id, resolved_user_id))
                                    continue

                                if not job_matches_scope(scope, login, props, job):
                                    continue

                                if auto_preview_enabled and preview_method in {"server", "deadline"}:
                                    auto_key = (job_id, telegram_user_id)
                                    if auto_key not in auto_preview_jobs:
                                        auto_preview_jobs.add(auto_key)
                                        asyncio.create_task(
                                            _run_auto_preview_for_job(
                                                telegram_user_id,
                                                job_id,
                                                name,
                                                login,
                                                decrypted_password,
                                                preview_method,
                                                preview_worker,
                                            )
                                        )

                                if not has_notifications:
                                    continue

                                if notified_key in notified_jobs:
                                    continue

                                batch = props.get("Batch") or "No Batch"
                                message_text = (
                                    "✅ Job completed:\n"
                                    f"• Batch: {batch}\n"
                                    f"• Name: {name}"
                                )

                                preview_markup = InlineKeyboardMarkup(
                                    inline_keyboard=[
                                        [
                                            InlineKeyboardButton(
                                                text="🔍 Preview",
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
                                logger.info(
                                    "Completion notification sent to user %s for job %s (%s)",
                                    telegram_user_id,
                                    job_id,
                                    name,
                                )
                                notified_jobs.add((job_id, telegram_user_id))
                        elif resp.status == 401:
                            logger.warning(
                                "Watcher: Unauthorized for user %s. Disabling notifications and requesting re-login.",
                                telegram_user_id
                            )
                            await disable_notifications_for_user(telegram_user_id)
                            await bot.send_message(
                                telegram_user_id,
                                "⚠️ Authorization expired. Please run /login again to keep receiving notifications."
                            )
                        else:
                            logger.error(f"Watcher: Error requesting jobs for user {telegram_user_id}: {resp.status}")
                except Exception as e:
                    logger.error(f"Watcher: Error monitoring jobs for user {telegram_user_id}: {e}", exc_info=True)
    except asyncio.CancelledError:
        logger.info("Job progress watcher cancelled")
