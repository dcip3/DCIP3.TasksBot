import contextlib
import json
import logging
import time
from typing import Optional

from aiogram import Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.auth import get_deadline_credentials
from app.storage.user_settings import (
    PREVIEW_DEFAULT_WORKER_AUTO,
    get_preview_default_method,
    get_preview_default_worker,
)
from app.core.bot_core import bot, download_states, stop_downloads
from app.core.config import settings
from app.core.path_utils import extract_dropbox_path
from app.core.preview_text import build_preview_caption
from app.core.maintenance import cleanup_old_files, cleanup_temp_and_conv
from app.integrations.dropbox_helpers import (
    count_exr_files,
    get_fresh_access_token,
)
from app.integrations.video_helpers import cleanup_job_files
from app.services.deadline import (
    ALLOWED_WORKER_STATUSES,
    delete_job_by_user_id,
    get_job_info_by_user_id,
    get_workers_list,
    WorkerStatusError,
)
from app.services.dropbox import get_dropbox_session
from app.services.preview.pipeline import render_preview_via_server_pipeline
from app.services.preview.render import (
    PreviewSubmissionError,
    check_video_exists_in_dropbox,
    create_video_from_job,
    download_video_from_dropbox,
)
from app.services.preview.runtime import pop_preview_message, register_preview_message

logger = logging.getLogger(__name__)

router = Router()

_worker_menu_cooldowns: dict[int, float] = {}


def _hit_worker_menu_cooldown(chat_id: int, window_seconds: float = 3.0) -> bool:
    now = time.monotonic()
    last = _worker_menu_cooldowns.get(chat_id)
    if last is not None and (now - last) < window_seconds:
        return True
    _worker_menu_cooldowns[chat_id] = now
    return False


async def _answer_rate_limit(callback_query: CallbackQuery, retry_after: int) -> None:
    try:
        await callback_query.answer(
            f"Too many requests. Please retry in {retry_after} seconds.",
            show_alert=True,
        )
    except Exception:
        pass


async def _try_edit_text(
    callback_query: CallbackQuery,
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> tuple[bool, bool]:
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return True, False
    except TelegramRetryAfter as rate_exc:
        logger.warning("Rate limited on edit_text: retry in %s seconds", rate_exc.retry_after)
        await _answer_rate_limit(callback_query, int(rate_exc.retry_after))
        return False, True
    except Exception as exc:
        logger.warning("Error editing message text: %s", exc)
        return False, False


async def _try_send_message(
    callback_query: CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> Message | None:
    try:
        return await callback_query.message.answer(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except TelegramRetryAfter as rate_exc:
        logger.warning("Rate limited on send_message: retry in %s seconds", rate_exc.retry_after)
        await _answer_rate_limit(callback_query, int(rate_exc.retry_after))
        return None
    except Exception as exc:
        logger.error("Error sending message: %s", exc)
        return None

async def _set_progress_message(
    callback_query: CallbackQuery,
    message: Optional[Message],
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> tuple[Optional[Message], bool]:
    """Update a progress message, falling back to sending a new one if needed."""
    if message is None:
        sent = await _try_send_message(
            callback_query, text, reply_markup=reply_markup, parse_mode=parse_mode
        )
        return sent, sent is None

    edited, rate_limited = await _try_edit_text(
        callback_query,
        message,
        text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
    if edited:
        return message, False
    if rate_limited:
        return message, True

    sent = await _try_send_message(
        callback_query, text, reply_markup=reply_markup, parse_mode=parse_mode
    )
    return sent, sent is None


async def _show_worker_menu(
    callback_query: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    """Render worker selection menu with shared cooldown and rate-limit handling."""
    if callback_query.message is None:
        await callback_query.answer("Error: message not found.", show_alert=True)
        return

    if _hit_worker_menu_cooldown(callback_query.message.chat.id):
        await callback_query.answer("Please wait a few seconds and try again.", show_alert=False)
        return

    try:
        await callback_query.message.edit_text(text, reply_markup=keyboard)
        await callback_query.answer()
    except TelegramRetryAfter as rate_exc:
        logger.warning("Worker selection rate limited: retry in %s seconds", rate_exc.retry_after)
        await _answer_rate_limit(callback_query, int(rate_exc.retry_after))
    except Exception as exc:
        logger.warning("Error editing worker selection message: %s", exc)
        try:
            await callback_query.message.answer(text, reply_markup=keyboard)
            await callback_query.answer()
        except TelegramRetryAfter as rate_exc:
            logger.warning("Worker selection send rate limited: retry in %s seconds", rate_exc.retry_after)
            await _answer_rate_limit(callback_query, int(rate_exc.retry_after))
        except Exception as fallback_exc:
            logger.error("Error showing worker selection: %s", fallback_exc)
            await callback_query.answer("Failed to load workers.", show_alert=True)

def _extract_preview_source(job_info: dict) -> tuple[bool, Optional[str]]:
    props = job_info.get("Props", {})
    comment = str(props.get("Cmmt") or "")
    name_raw = str(props.get("Name") or "")
    name_tail = name_raw.split("/")[-1] if "/" in name_raw else name_raw
    extra_dict = props.get("ExDic") or {}
    if not isinstance(extra_dict, dict):
        extra_dict = {}

    preview_job_flag = str(extra_dict.get("PreviewJob") or "").strip() == "1"
    preview_source = extra_dict.get("PreviewSource")

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
        if prefix == "PreviewJob":
            preview_job_flag = preview_job_flag or payload.strip() == "1"
        elif prefix == "PreviewSource":
            preview_source = payload

    is_preview_job = (
        preview_job_flag
        or "Preview job generated by TasksBot" in comment
        or name_tail.endswith(" - Preview")
    )
    source_id = str(preview_source).strip() if preview_source else None
    return is_preview_job, source_id


async def _maybe_send_single_frame_preview(
    callback_query: CallbackQuery,
    job_id: str,
) -> bool:
    if callback_query.from_user is None:
        return False

    job_info = await get_job_info_by_user_id(callback_query.from_user.id, job_id)
    if not job_info:
        return False

    outdirs = job_info.get("OutDir", [])
    dropbox_path = extract_dropbox_path(
        outdirs[0] if outdirs else None,
        settings.dropbox_root_marker,
    )
    if not dropbox_path:
        return False

    headers_dbx = {
        "Authorization": f"Bearer {await get_fresh_access_token()}",
        "Dropbox-API-Select-User": settings.dropbox_team_member_id,
        "Dropbox-API-Path-Root": json.dumps(
            {".tag": "root", "root": settings.dropbox_root_namespace_id}
        ),
        "Content-Type": "application/json",
    }
    session_dbx = await get_dropbox_session()
    try:
        total_files = await count_exr_files(session_dbx, dropbox_path, headers_dbx)
    except Exception as exc:
        logger.warning("Single-frame check failed for job %s: %s", job_id, exc)
        return False

    if total_files != 1:
        return False

    await render_preview_via_server(callback_query, job_id)
    return True


def _build_render_method_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🖥️ Server",
                    callback_data=f"preview_render:server:{job_id}",
                ),
                InlineKeyboardButton(
                    text="☁️ Deadline",
                    callback_data=f"preview_render:deadline:{job_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✖️ Cancel",
                    callback_data="preview_cancel",
                )
            ],
        ]
    )


async def _prompt_render_method(
    callback_query: CallbackQuery,
    job_id: str,
    message_text: Optional[str] = None,
) -> None:
    text = message_text or "Choose how to create the preview."
    keyboard = _build_render_method_keyboard(job_id)
    target_message = callback_query.message
    if target_message:
        await target_message.answer(text, reply_markup=keyboard)
    elif callback_query.from_user:
        await bot.send_message(callback_query.from_user.id, text, reply_markup=keyboard)


async def _start_deadline_preview(callback_query: CallbackQuery, job_id: str) -> None:
    if callback_query.from_user is None:
        await callback_query.answer("Error: user not found.", show_alert=True)
        return

    if await _maybe_send_single_frame_preview(callback_query, job_id):
        return

    default_worker = await get_preview_default_worker(callback_query.from_user.id)
    # Use a fresh progress message to avoid editing the job card message.
    progress_msg: Optional[Message] = None

    if default_worker == PREVIEW_DEFAULT_WORKER_AUTO:
        try:
            await create_new_video_process(
                callback_query,
                job_id,
                use_any_machine=False,
                skip_worker_validation=True,
                progress_message=progress_msg,
                specific_worker=None,
            )
            return
        except Exception as exc:
            logger.error(
                "Error auto-submitting preview with render worker preference: %s",
                exc,
            )
            try:
                await show_worker_selection_for_preview(callback_query, job_id)
                return
            except Exception as fallback_exc:
                logger.error("Error showing worker selection fallback: %s", fallback_exc)
                await callback_query.answer("Failed to submit with auto worker.", show_alert=True)
                return

    if default_worker:
        try:
            await create_new_video_process(
                callback_query,
                job_id,
                use_any_machine=False,
                skip_worker_validation=True,
                progress_message=progress_msg,
                specific_worker=default_worker,
            )
            return
        except Exception as exc:
            logger.error(
                "Error auto-submitting preview to default worker %s: %s",
                default_worker,
                exc,
            )
            try:
                await show_worker_selection_for_preview(callback_query, job_id)
                return
            except Exception as fallback_exc:
                logger.error("Error showing worker selection fallback: %s", fallback_exc)
                await callback_query.answer("Failed to submit with default worker.", show_alert=True)
                return

    await show_worker_selection_for_preview(callback_query, job_id)


@router.callback_query(lambda c: c.data and c.data.startswith("preview_job:"))
async def preview_job_callback(callback_query: CallbackQuery) -> None:
    """Handle preview job button press."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return

    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        credentials = await get_deadline_credentials(callback_query.from_user.id)
        if not credentials:
            await callback_query.answer("No credentials found. Please login again.", show_alert=True)
            return

        login, password = credentials

        preview_job_detected = False
        preview_source_id = None
        job_info = await get_job_info_by_user_id(callback_query.from_user.id, job_id)
        if job_info:
            preview_job_detected, preview_source_id = _extract_preview_source(job_info)
            if preview_job_detected and preview_source_id:
                job_id = preview_source_id

        video_info = await check_video_exists_in_dropbox(login, password, job_id)
        if video_info:
            send_button = InlineKeyboardButton(
                text="📤 Send from Dropbox",
                callback_data=f"send_dbx_video:{job_id}",
            )
            recreate_button = InlineKeyboardButton(
                text="🔄 New render",
                callback_data=f"preview_render_options:{job_id}",
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[send_button, recreate_button]])
            await callback_query.message.answer(
                f"🎬 Video '{video_info['filename']}' already exists on Dropbox.",
                reply_markup=keyboard,
            )
            await callback_query.answer()
            return

        if preview_job_detected and not preview_source_id:
            await callback_query.answer(
                "This is a preview job. Open the source job to recreate previews.",
                show_alert=True,
            )
            return

        default_method = await get_preview_default_method(callback_query.from_user.id)
        if default_method == "server":
            await render_preview_via_server(callback_query, job_id)
            return
        if default_method == "deadline":
            await _start_deadline_preview(callback_query, job_id)
            return

        await _prompt_render_method(
            callback_query,
            job_id,
            "No preview yet. Choose a render method:",
        )
        await callback_query.answer()
        return

    except Exception as exc:
        logger.error("Error handling preview for user %s: %s", callback_query.from_user.id, exc)
        await callback_query.answer("Error occurred while generating preview.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("preview_render_options:"))
async def preview_render_options_callback(callback_query: CallbackQuery) -> None:
    """Show render method choices when user wants to create a new preview."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    default_method = (
        await get_preview_default_method(callback_query.from_user.id)
        if callback_query.from_user
        else None
    )
    if default_method == "server":
        await render_preview_via_server(callback_query, job_id)
        return
    if default_method == "deadline":
        await _start_deadline_preview(callback_query, job_id)
        return

    await _prompt_render_method(
        callback_query,
        job_id,
        "Select a preview render method:",
    )
    try:
        await callback_query.answer()
    except Exception:
        pass


async def create_new_video_process(
    callback_query: CallbackQuery,
    job_id: str,
    *,
    use_any_machine: bool = False,
    skip_worker_validation: bool = False,
    progress_message: Optional[Message] = None,
    specific_worker: Optional[str] = None,
) -> None:
    """Submit a Deadline job that generates a preview video via ffmpeg."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: user not found.", show_alert=True)
        return

    if await _maybe_send_single_frame_preview(callback_query, job_id):
        return

    if specific_worker:
        initial_text = f"🧾 Submitting preview job to worker: {specific_worker}..."
    elif use_any_machine:
        initial_text = "🧾 Submitting preview job without machine restrictions..."
    else:
        initial_text = "🧾 Submitting preview job to Deadline..."

    progress_msg = progress_message
    progress_msg, _ = await _set_progress_message(
        callback_query,
        progress_msg,
        initial_text,
        reply_markup=None,
    )

    try:
        result = await create_video_from_job(
            callback_query.from_user.id,
            job_id,
            skip_worker_validation=skip_worker_validation,
            use_any_machine=use_any_machine,
            specific_worker=specific_worker,
        )
        if not result:
            if progress_msg:
                await _set_progress_message(
                    callback_query,
                    progress_msg,
                    "❌ Failed to submit the job to Deadline.",
                )
            await callback_query.answer("Failed to submit the job.", show_alert=True)
            return

        preview_id = result.get("preview_job_id")
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

        if progress_msg:
            progress_msg, rate_limited = await _set_progress_message(
                callback_query,
                progress_msg,
                "✅ Preview job queued\n□ □ □",
                reply_markup=cancel_keyboard,
            )
            if rate_limited:
                return
            if preview_id and progress_msg:
                register_preview_message(preview_id, progress_msg.chat.id, progress_msg.message_id)
        await callback_query.answer("Preview job queued!", show_alert=False)
    except WorkerStatusError as worker_error:
        status_lines = []
        for item in worker_error.invalid_workers:
            name = item.get("name", "Unknown")
            status_code = item.get("status_code")
            status_text = item.get("status_text") or "Unknown"
            if status_code is None:
                status_lines.append(f"• {name}: {status_text}")
            else:
                status_lines.append(f"• {name}: {status_text} ({status_code})")

        status_block = "\n".join(status_lines) if status_lines else "• No status information"
        message_text = (
            "⚠️ Preview job could not be queued: preferred workers are unavailable.\n"
            f"{status_block}\n\n"
            "Choose an action:"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Check again", callback_data=f"preview_retry:{job_id}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🖥️ Select Worker", callback_data=f"preview_select_worker:{job_id}"
                    ),
                ],
                [InlineKeyboardButton(text="✖️ Cancel", callback_data="preview_cancel")],
            ]
        )
        if progress_msg:
            progress_msg, rate_limited = await _set_progress_message(
                callback_query,
                progress_msg,
                message_text,
                reply_markup=keyboard,
            )
            if rate_limited:
                return
        await callback_query.answer("Preferred workers are unavailable.", show_alert=False)
    except PreviewSubmissionError as exc:
        user_message = exc.user_message
        if progress_msg:
            await _set_progress_message(
                callback_query,
                progress_msg,
                f"❌ {user_message}",
            )
        try:
            await callback_query.answer(user_message, show_alert=True)
        except Exception:
            pass
    except Exception as exc:
        logger.error(
            "Error submitting preview job for user %s: %s",
            callback_query.from_user.id if callback_query.from_user else "unknown",
            exc,
        )
        if progress_msg:
            await _set_progress_message(
                callback_query,
                progress_msg,
                "❌ An error occurred while submitting the job.",
            )
        try:
            await callback_query.answer("Failed to submit the job.", show_alert=True)
        except Exception:
            pass


@router.callback_query(lambda c: c.data and c.data.startswith("preview_render:"))
async def preview_render_callback(callback_query: CallbackQuery) -> None:
    """Handle render method choice for previews."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    parts = callback_query.data.split(":", 2)
    if len(parts) != 3:
        await callback_query.answer("Invalid selection.", show_alert=True)
        return

    _, mode, job_id = parts

    if mode == "deadline":
        await _start_deadline_preview(callback_query, job_id)
        return

    if mode == "server":
        await render_preview_via_server(callback_query, job_id)
        return

    await callback_query.answer("Unknown action.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("preview_submit:"))
async def preview_submit_callback(callback_query: CallbackQuery) -> None:
    """Handle preview submission with selected worker."""
    if callback_query.data is None or callback_query.from_user is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    parts = callback_query.data.split(":", 2)
    if len(parts) != 3:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    job_id = parts[1]
    worker_choice = parts[2]

    progress_msg: Optional[Message] = None
    if callback_query.message and isinstance(callback_query.message, Message):
        progress_msg = callback_query.message

    try:
        if worker_choice == "any":
            await create_new_video_process(
                callback_query,
                job_id,
                use_any_machine=True,
                skip_worker_validation=True,
                progress_message=progress_msg,
            )
        else:
            await create_new_video_process(
                callback_query,
                job_id,
                use_any_machine=False,
                skip_worker_validation=True,
                progress_message=progress_msg,
                specific_worker=worker_choice,
            )
    except Exception as exc:
        logger.error("Error submitting preview with worker %s: %s", worker_choice, exc)
        await callback_query.answer("Failed to submit preview job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("preview_retry:"))
async def preview_retry_callback(callback_query: CallbackQuery) -> None:
    """Retry worker status check before submitting preview."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]
    try:
        await create_new_video_process(
            callback_query,
            job_id,
            progress_message=callback_query.message,
        )
    except Exception as exc:
        logger.error("Error retrying preview submission: %s", exc)
        await callback_query.answer("Retry failed.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("preview_force:"))
async def preview_force_callback(callback_query: CallbackQuery) -> None:
    """Force preview submission without worker whitelist."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]
    try:
        await create_new_video_process(
            callback_query,
            job_id,
            use_any_machine=True,
            skip_worker_validation=True,
            progress_message=callback_query.message,
        )
    except Exception as exc:
        logger.error("Error forcing preview submission: %s", exc)
        await callback_query.answer("Failed to submit without restrictions.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("preview_select_worker:"))
async def preview_select_worker_callback(callback_query: CallbackQuery) -> None:
    """Show worker selection menu for preview job."""
    if callback_query.data is None or callback_query.from_user is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]
    try:
        workers = await get_workers_list(callback_query.from_user.id)
        if not workers:
            await callback_query.answer("No workers available.", show_alert=True)
            return

        available_workers = []
        for worker in workers:
            info = worker.get("Info", {})
            name = info.get("Name")
            status_code = info.get("Stat")
            if name and status_code in ALLOWED_WORKER_STATUSES:
                available_workers.append(name)

        if not available_workers:
            await callback_query.answer("No active workers available.", show_alert=True)
            return

        keyboard_rows = []
        for i in range(0, len(available_workers), 2):
            row = []
            for j in range(i, min(i + 2, len(available_workers))):
                worker_name = available_workers[j]
                row.append(
                    InlineKeyboardButton(
                        text=worker_name,
                        callback_data=f"preview_worker_chosen:{job_id}:{worker_name}",
                    )
                )
            keyboard_rows.append(row)

        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="☁️ Any Worker",
                    callback_data=f"preview_force:{job_id}",
                )
            ]
        )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="✖️ Cancel",
                    callback_data="preview_cancel",
                )
            ]
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
        await _show_worker_menu(
            callback_query,
            "🖥️ Select a worker for preview rendering:",
            keyboard,
        )
    except Exception as exc:
        logger.error("Error showing worker selection: %s", exc)
        await callback_query.answer("Failed to load workers.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("preview_worker_chosen:"))
async def preview_worker_chosen_callback(callback_query: CallbackQuery) -> None:
    """Submit preview job to specific worker."""
    if callback_query.data is None or callback_query.from_user is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    parts = callback_query.data.split(":", 2)
    if len(parts) != 3:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    job_id = parts[1]
    worker_name = parts[2]

    try:
        await create_new_video_process(
            callback_query,
            job_id,
            use_any_machine=False,
            skip_worker_validation=True,
            progress_message=callback_query.message,
            specific_worker=worker_name,
        )
    except Exception as exc:
        logger.error("Error submitting preview to worker %s: %s", worker_name, exc)
        await callback_query.answer("Failed to submit preview job.", show_alert=True)


@router.callback_query(lambda c: c.data == "preview_cancel")
async def preview_cancel_callback(callback_query: CallbackQuery) -> None:
    """Cancel preview submission attempt."""
    await callback_query.answer("Action cancelled.", show_alert=False)
    try:
        await callback_query.message.edit_text("Action cancelled.", reply_markup=None)
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("preview_server_cancel:"))
async def preview_server_cancel_callback(callback_query: CallbackQuery) -> None:
    """Handle cancellation of server-side preview generation."""
    if callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]
    stop_event = stop_downloads.get(job_id)
    state = download_states.get(job_id)

    if stop_event is None or state is None:
        await callback_query.answer("Nothing to cancel.", show_alert=False)
        return

    if not stop_event.is_set():
        stop_event.set()

    state["cancel_requested"] = True
    state["stop_kb"] = None
    progress_msg = state.get("progress_msg")

    if progress_msg:
        with contextlib.suppress(Exception):
            await progress_msg.edit_text("⏹️ Cancelling preview generation...", reply_markup=None)

    await callback_query.answer("Cancelling preview...", show_alert=False)


@router.callback_query(lambda c: c.data and c.data.startswith("preview_job_cancel:"))
async def preview_job_cancel_callback(callback_query: CallbackQuery) -> None:
    """Cancel a queued preview job."""
    if callback_query.data is None or callback_query.from_user is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    preview_job_id = callback_query.data.split(":", 1)[1]
    try:
        success = await delete_job_by_user_id(callback_query.from_user.id, preview_job_id)
        if success:
            stored_message = pop_preview_message(preview_job_id)
            if stored_message and callback_query.message:
                stored_chat_id, stored_message_id = stored_message
                if (
                    callback_query.message.chat.id != stored_chat_id
                    or callback_query.message.message_id != stored_message_id
                ):
                    with contextlib.suppress(Exception):
                        await bot.edit_message_text(
                            "⏹️ Preview generation cancelled.",
                            chat_id=stored_chat_id,
                            message_id=stored_message_id,
                        )
            if callback_query.message:
                await callback_query.message.edit_text("⏹️ Preview generation cancelled.", reply_markup=None)
            await callback_query.answer("Preview generation cancelled.", show_alert=False)
        else:
            await callback_query.answer("Failed to cancel preview job.", show_alert=True)
    except Exception as exc:
        logger.error("Error cancelling preview job %s: %s", preview_job_id, exc)
        await callback_query.answer("Error cancelling job.", show_alert=True)


async def render_preview_via_server(callback_query: CallbackQuery, job_id: str) -> None:
    """Thin handler wrapper delegating heavy server pipeline to service layer."""
    await render_preview_via_server_pipeline(callback_query, job_id)


async def show_worker_selection_for_preview(callback_query: CallbackQuery, job_id: str) -> None:
    """Show worker selection menu for preview rendering."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: user not found.", show_alert=True)
        return

    if callback_query.message is None:
        await callback_query.answer("Error: message not found.", show_alert=True)
        return

    user_id = callback_query.from_user.id
    default_worker = await get_preview_default_worker(user_id)
    workers = await get_workers_list(user_id)

    text_lines = ["🎬 Select worker for preview rendering:"]
    if default_worker == PREVIEW_DEFAULT_WORKER_AUTO:
        text_lines.append("\nDefault: Auto (render worker)")
    elif default_worker:
        text_lines.append(f"\nDefault: {default_worker}")

    text = "\n".join(text_lines)

    keyboard_rows = []

    if workers:
        for i in range(0, len(workers), 2):
            row = []
            for j in range(i, min(i + 2, len(workers))):
                worker = workers[j]
                info = worker.get("Info", {})
                worker_name = info.get("Name", "Unknown")
                display_name = (
                    f"✅ {worker_name}" if worker_name == default_worker else worker_name
                )
                row.append(
                    InlineKeyboardButton(
                        text=display_name,
                        callback_data=f"preview_submit:{job_id}:{worker_name}",
                    )
                )
            keyboard_rows.append(row)

    keyboard_rows.append(
        [
            InlineKeyboardButton(
                text="☁️ Any Worker",
                callback_data=f"preview_submit:{job_id}:any",
            )
        ]
    )
    keyboard_rows.append(
        [
            InlineKeyboardButton(
                text="✖️ Cancel",
                callback_data="preview_cancel",
            )
        ]
    )

    await _show_worker_menu(
        callback_query,
        text,
        InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("send_dbx_video:"))
async def send_dbx_video_callback(callback_query: CallbackQuery) -> None:
    """Handle send video from Dropbox button press."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        credentials = await get_deadline_credentials(callback_query.from_user.id)
        if not credentials:
            await callback_query.answer("No credentials found. Please login again.", show_alert=True)
            return

        login, password = credentials
        progress_msg = await callback_query.message.answer("📥 Downloading existing video from Dropbox...")

        try:
            download_result = await download_video_from_dropbox(login, password, job_id)
        except Exception as exc:
            from app.core.error_text import describe_error

            logger.error("Error downloading video from Dropbox: %s", exc)
            user_message = describe_error(exc) or "Failed to download the video from Dropbox."
            await progress_msg.edit_text(f"❌ {user_message}")
            await callback_query.answer(user_message, show_alert=True)
            return

        if not download_result:
            await progress_msg.edit_text("⚠️ Video not found in Dropbox.")
            await callback_query.answer("No video available on Dropbox.", show_alert=True)
            return

        if isinstance(download_result, tuple):
            video_path, dropbox_path = download_result
        else:
            video_path = download_result
            dropbox_path = None

        try:
            video_file = FSInputFile(video_path)
            project_name = Path(video_path).stem
            caption = build_preview_caption(
                project_name,
                dropbox_path or video_path,
            )
            await callback_query.message.answer_video(
                video=video_file,
                caption=caption,
                parse_mode="HTML",
            )

            cleanup_job_files(job_id)
            cleanup_old_files(max_age_hours=6)
            cleanup_temp_and_conv()

            with contextlib.suppress(Exception):
                Path(video_path).unlink()

            await progress_msg.delete()

            try:
                await callback_query.answer("Video sent successfully!")
            except Exception as answer_error:
                logger.warning("Could not answer callback query: %s", answer_error)
        except Exception as exc:
            from app.core.error_text import describe_error

            logger.error("Error sending Dropbox video: %s", exc)
            user_message = describe_error(exc) or "Failed to send the video."
            await progress_msg.edit_text(f"❌ {user_message}")
            await callback_query.answer(user_message, show_alert=True)

    except Exception as exc:
        logger.error("Error handling send_dbx_video for user %s: %s", callback_query.from_user.id, exc)
        await callback_query.answer("Error occurred while downloading video.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("create_new_video:"))
async def create_new_video_callback(callback_query: CallbackQuery) -> None:
    """Handle create new video button press."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return

    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        await _prompt_render_method(
            callback_query,
            job_id,
            "Select a preview render method:",
        )
        with contextlib.suppress(Exception):
            await callback_query.answer()
    except Exception as exc:
        logger.error("Error handling create_new_video for user %s: %s", callback_query.from_user.id, exc)
        await callback_query.answer("Error occurred while creating video.", show_alert=True)
