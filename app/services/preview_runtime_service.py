"""Preview runtime workflows: progress registry, delivery notifications, and auto-preview orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.core.bot_core import bot
from app.integrations.video_helpers import get_file_size_mb, prepare_video_for_delivery
from app.core.maintenance import cleanup_old_files

logger = logging.getLogger(__name__)

# Track preview submission progress messages (preview_job_id -> (chat_id, message_id))
preview_message_registry: dict[str, tuple[int, int]] = {}
preview_animation_tasks: dict[str, asyncio.Task] = {}


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
    frames = ["□ □ □", "■ □ □", "■ ■ □", "■ ■ ■"]
    index = 1
    sleep_seconds = 4

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
            except TelegramRetryAfter as rate_exc:
                logger.warning(
                    "Preview animation rate limited for job %s: retry in %s seconds",
                    preview_job_id,
                    rate_exc.retry_after,
                )
                await asyncio.sleep(rate_exc.retry_after)
                continue
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
            await asyncio.sleep(sleep_seconds)
    except asyncio.CancelledError:
        logger.debug("Preview animation task cancelled for job %s", preview_job_id)


def _extract_preview_context(
    props: Dict[str, Any],
    default_user_id: int,
) -> Tuple[str, str, int, Dict[str, Any], Optional[str]]:
    """Extract preview metadata from job properties."""
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
    """Send ready preview video to the user when the ffmpeg job finishes."""
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

    from app.core.preview_text import build_preview_caption

    caption = build_preview_caption(final_path.name, display_path)

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
        await bot.send_message(target_chat_id, ready_text)

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

    body = "\n\n".join(error_segments) if error_segments else "No worker error logs were retrieved."
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


async def _send_dropbox_video_to_user(
    telegram_user_id: int,
    login: str,
    password: str,
    job_id: str,
    dropbox_path_hint: Optional[str] = None,
) -> bool:
    """Download a preview from Dropbox and deliver it to the user."""
    from app.integrations.video_helpers import cleanup_job_files
    from app.services import download_video_from_dropbox

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
        from app.core.error_text import describe_error

        logger.error("Auto preview Dropbox download failed for job %s: %s", job_id, exc)
        user_message = describe_error(exc) or "Auto preview: failed to download from Dropbox."
        with contextlib.suppress(Exception):
            await progress_msg.edit_text(f"❌ {user_message}")
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
        from app.core.preview_text import build_preview_caption

        caption = build_preview_caption(video_path_obj.name, dropbox_path or video_path)

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
        from app.core.error_text import describe_error

        logger.error("Auto preview send failed for job %s: %s", job_id, exc)
        user_message = describe_error(exc) or "Auto preview: failed to send the video."
        with contextlib.suppress(Exception):
            await progress_msg.edit_text(f"❌ {user_message}")
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
    from app.services import PreviewSubmissionError, WorkerStatusError, create_video_from_job
    from app.user_settings import PREVIEW_DEFAULT_WORKER_AUTO

    result = None
    fallback_used = False
    try:
        if default_worker == PREVIEW_DEFAULT_WORKER_AUTO:
            default_worker = None
        result = await create_video_from_job(
            telegram_user_id,
            job_id,
            specific_worker=default_worker,
        )
    except PreviewSubmissionError as exc:
        await bot.send_message(
            telegram_user_id,
            f"❌ Auto preview: {exc.user_message}",
        )
        return
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

        from app.bot.handlers.preview import _maybe_send_single_frame_preview, render_preview_via_server

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
