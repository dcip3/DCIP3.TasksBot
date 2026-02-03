import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

from aiogram import Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.auth import (
    PREVIEW_DEFAULT_WORKER_AUTO,
    get_deadline_credentials,
    get_preview_default_method,
    get_preview_default_worker,
)
from app.core.bot_core import bot, download_states, stop_downloads
from app.core.config import settings
from app.core.path_utils import extract_dropbox_path
from app.core.utils import (
    cleanup_old_files,
    cleanup_temp_and_conv,
    register_preview_message,
    pop_preview_message,
)
from app.integrations.dropbox_helpers import (
    download_exr_folder,
    count_exr_files,
    fetch_dropbox_metadata,
    get_fresh_access_token,
    list_folder_all,
    upload_video_to_dropbox,
)
from app.integrations.video_helpers import (
    assemble_video_from_jpg,
    cleanup_job_files,
    get_file_size_mb,
    prepare_video_for_delivery,
)
from app.services import (
    ALLOWED_WORKER_STATUSES,
    check_video_exists_in_dropbox,
    create_video_from_job,
    delete_job_by_user_id,
    download_video_from_dropbox,
    get_dropbox_session,
    get_job_info_by_user_id,
    get_workers_list,
    WorkerStatusError,
)

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


def _build_server_cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """Inline keyboard with a cancel button for server-side preview generation."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✖️ Cancel",
                    callback_data=f"preview_server_cancel:{job_id}",
                )
            ]
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
    """Replicate the optimized local render pipeline: download frames, convert if needed, assemble, and deliver."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: user not found.", show_alert=True)
        return

    progress_msg: Optional[Message] = None
    cancel_keyboard: Optional[InlineKeyboardMarkup] = None
    stop_event: Optional[asyncio.Event] = None
    state: Optional[dict] = None
    try:
        base_message = callback_query.message
        if base_message:
            progress_msg = await base_message.answer("🔍 Starting preview generation...")
        else:
            progress_msg = await bot.send_message(
                callback_query.from_user.id, "🔍 Starting preview generation..."
            )

        cancel_keyboard = _build_server_cancel_keyboard(job_id)
        stop_event = stop_downloads.get(job_id)
        if stop_event is None:
            stop_event = asyncio.Event()
            stop_downloads[job_id] = stop_event

        download_states[job_id] = {
            "progress_msg": progress_msg,
            "total_files": 0,
            "stop_kb": cancel_keyboard,
            "last_progress_ts": 0.0,
            "last_progress_percent": -1,
        }
        state = download_states[job_id]

        async def finalize_cancellation() -> bool:
            if stop_event is None or state is None:
                return False
            if not (stop_event.is_set() or state.get("cancel_requested")):
                return False
            if not state.get("cancel_finalized"):
                state["cancel_finalized"] = True
                state["stop_kb"] = None
                progress = state.get("progress_msg") or progress_msg
                if progress:
                    with contextlib.suppress(Exception):
                        await progress.edit_text("⏹️ Preview generation cancelled.", reply_markup=None)
                try:
                    cleanup_job_files(job_id)
                    cleanup_temp_and_conv()
                    cleanup_old_files(max_age_hours=6)
                except Exception as cleanup_error:
                    logger.error("Error cleaning up after cancellation: %s", cleanup_error)
                with contextlib.suppress(Exception):
                    await callback_query.answer("Preview generation cancelled.", show_alert=False)
            return True

        await progress_msg.edit_text(
            "📥 Step 1: Downloading files from Dropbox...",
            reply_markup=cancel_keyboard,
        )
        if await finalize_cancellation():
            return

        job_info = await get_job_info_by_user_id(callback_query.from_user.id, job_id)
        if not job_info:
            await progress_msg.edit_text("❌ Failed to get job info")
            await callback_query.answer("Failed to get job info.", show_alert=True)
            return

        props = job_info.get("Props", {}) or {}
        job_name = props.get("Name") or props.get("Batch") or job_id

        outdirs = job_info.get("OutDir", [])
        if not outdirs:
            await progress_msg.edit_text("❌ No output directory found.")
            await callback_query.answer("Render path is missing.", show_alert=True)
            return

        fullpath = outdirs[0]
        idx = fullpath.find(settings.dropbox_root_marker)
        if idx == -1:
            await progress_msg.edit_text("❌ Dropbox root marker not found in path.")
            await callback_query.answer("Could not determine Dropbox path.", show_alert=True)
            return

        dropbox_path = extract_dropbox_path(fullpath, settings.dropbox_root_marker)
        if not dropbox_path:
            await progress_msg.edit_text("❌ Could not normalize Dropbox path.")
            await callback_query.answer("Could not determine Dropbox path.", show_alert=True)
            return

        temp_dir = Path(settings.temp_dir)
        temp_dir.mkdir(exist_ok=True)
        exr_folder_name = Path(dropbox_path).parts[-1] or job_id
        local_root = temp_dir / f"{exr_folder_name}_{job_id}"
        local_root.mkdir(parents=True, exist_ok=True)

        headers_dbx = {
            "Authorization": f"Bearer {await get_fresh_access_token()}",
            "Dropbox-API-Select-User": settings.dropbox_team_member_id,
            "Dropbox-API-Path-Root": json.dumps(
                {".tag": "root", "root": settings.dropbox_root_namespace_id}
            ),
            "Content-Type": "application/json",
        }
        session_dbx = await get_dropbox_session()

        list_result = await list_folder_all(session_dbx, dropbox_path, headers_dbx)
        if not list_result:
            await progress_msg.edit_text("❌ Failed to list folder.")
            await callback_query.answer("Could not list files in Dropbox.", show_alert=True)
            return

        preview_exts = (".exr", ".jpg", ".jpeg", ".png")
        total_files = sum(
            1
            for entry in list_result.get("entries", [])
            if entry.get(".tag") == "file"
            and entry["name"].lower().endswith(preview_exts)
            and "cryptomatte" not in entry["name"].lower()
            and "conflicted copy" not in entry["name"].lower()
        )
        if total_files <= 0:
            await progress_msg.edit_text("⚠️ No usable image files found for conversion.")
            await callback_query.answer("No frames available for preview build.", show_alert=True)
            return

        if state is not None:
            state["total_files"] = total_files
            state["stop_kb"] = cancel_keyboard

        await download_exr_folder(
            session_dbx,
            "https://content.dropboxapi.com/2/files/download",
            headers_dbx,
            dropbox_path,
            local_root,
            job_id,
            download_states,
            stop_downloads,
        )
        if await finalize_cancellation():
            return

        conv_dir = Path(settings.conv_dir) / f"{exr_folder_name}_{job_id}"
        image_files = []
        for pattern in ("*.jpg", "*.jpeg", "*.png"):
            image_files.extend(conv_dir.rglob(pattern))
        if len(image_files) == 1:
            await progress_msg.edit_text(
                "🖼️ Step 2: Sending single frame...",
                reply_markup=cancel_keyboard,
            )
            if await finalize_cancellation():
                return

            image_path = image_files[0]
            project_name = image_path.stem
            try:
                path_parts = (dropbox_path or "").split("/")
                for idx_part, part in enumerate(path_parts):
                    if part == "render" and idx_part + 1 < len(path_parts):
                        project_name = path_parts[idx_part + 1]
                        break
            except Exception:  # pragma: no cover - defensive
                pass

            caption_lines = [f"🖼️ {project_name}"]
            if dropbox_path:
                caption_lines.append(f"<code>{dropbox_path}</code>")
            caption = "\n".join(caption_lines)

            if callback_query.message:
                await callback_query.message.answer_photo(
                    photo=FSInputFile(str(image_path)),
                    caption=caption,
                    parse_mode="HTML",
                )
            else:
                await bot.send_photo(
                    callback_query.from_user.id,
                    FSInputFile(str(image_path)),
                    caption=caption,
                    parse_mode="HTML",
                )

            with contextlib.suppress(Exception):
                await progress_msg.delete()
            try:
                await callback_query.answer("Frame sent successfully!")
            except Exception as answer_error:  # pragma: no cover - telegram timing
                logger.warning("Could not answer callback query: %s", answer_error)
            return

        await progress_msg.edit_text(
            "🎬 Step 2: Processing frames and creating video...",
            reply_markup=cancel_keyboard,
        )
        if await finalize_cancellation():
            return

        video_path = await asyncio.to_thread(assemble_video_from_jpg, conv_dir, str(exr_folder_name))
        if await finalize_cancellation():
            return

        try:
            metadata = await fetch_dropbox_metadata(session_dbx, dropbox_path, headers_dbx)
            dropbox_video_path = await upload_video_to_dropbox(Path(video_path), metadata, job_id)
        except Exception as upload_error:
            logger.error("Error uploading video to Dropbox: %s", upload_error)
            dropbox_video_path = dropbox_path

        if await finalize_cancellation():
            return

        await progress_msg.edit_text(
            "📏 Step 3: Checking file size...",
            reply_markup=cancel_keyboard,
        )
        video_path_obj = Path(video_path)
        max_video_size_mb = 45.0
        video_size_mb = get_file_size_mb(video_path_obj)

        if video_size_mb > max_video_size_mb:
            await progress_msg.edit_text(
                f"🗜️ Step 3.5: Compressing video ({video_size_mb:.1f} MB → target <{max_video_size_mb:.0f} MB)...",
                reply_markup=cancel_keyboard,
            )
            if await finalize_cancellation():
                return

        preparation = await prepare_video_for_delivery(
            video_path_obj,
            dropbox_video_path,
            max_size_mb=max_video_size_mb,
            initial_size_mb=video_size_mb,
        )
        final_video_path = preparation.video_path
        final_size_mb = preparation.size_mb
        fallback_message = preparation.fallback_message

        if await finalize_cancellation():
            return

        await progress_msg.edit_text(
            f"📤 Step 4: Sending video ({final_size_mb:.1f} MB)...",
            reply_markup=cancel_keyboard,
        )
        if await finalize_cancellation():
            return

        video_filename = final_video_path.name
        project_name = video_filename.replace(".mp4", "")
        try:
            path_parts = (dropbox_video_path or "").split("/")
            for idx_part, part in enumerate(path_parts):
                if part == "render" and idx_part + 1 < len(path_parts):
                    project_name = path_parts[idx_part + 1]
                    break
        except Exception:  # pragma: no cover - defensive
            pass

        caption = f"📁 {project_name}\n<code>{dropbox_video_path or ''}</code>"
        if fallback_message:
            if callback_query.message:
                await callback_query.message.answer(
                    fallback_message,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    callback_query.from_user.id,
                    fallback_message,
                    parse_mode="HTML",
                )
        else:
            if callback_query.message:
                await callback_query.message.answer_video(
                    video=FSInputFile(str(final_video_path)),
                    caption=caption,
                    parse_mode="HTML",
                )
            else:
                await bot.send_video(
                    callback_query.from_user.id,
                    FSInputFile(str(final_video_path)),
                    caption=caption,
                    parse_mode="HTML",
                )

        with contextlib.suppress(Exception):
            await progress_msg.delete()

        try:
            await callback_query.answer("Video created successfully!")
        except Exception as answer_error:  # pragma: no cover - telegram timing
            logger.warning("Could not answer callback query: %s", answer_error)

    except Exception as exc:
        logger.error(
            "Error in server-side preview generation for user %s job %s: %s",
            callback_query.from_user.id if callback_query.from_user else "unknown",
            job_id,
            exc,
        )
        if progress_msg:
            with contextlib.suppress(Exception):
                await progress_msg.edit_text(f"❌ Error during preview generation: {exc}")
        try:
            await callback_query.answer("Error occurred while creating video.", show_alert=True)
        except Exception as answer_error:
            logger.warning("Could not answer callback query after failure: %s", answer_error)
            if callback_query.message:
                await callback_query.message.answer("❌ Error occurred while creating video.")
        try:
            cleanup_job_files(job_id)
        except Exception as cleanup_error:
            logger.error("Error cleaning up job files after failure: %s", cleanup_error)
    finally:
        try:
            cleanup_temp_and_conv()
            cleanup_old_files(max_age_hours=6)
        except Exception as cleanup_error:
            logger.error("Error cleaning up directories after preview workflow: %s", cleanup_error)
        download_states.pop(job_id, None)
        stop_downloads.pop(job_id, None)


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
            logger.error("Error downloading video from Dropbox: %s", exc)
            await progress_msg.edit_text(f"❌ Error downloading video: {exc}")
            await callback_query.answer("Error occurred while downloading video.", show_alert=True)
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
            caption_lines = [f"📁 {project_name}"]
            if dropbox_path:
                caption_lines.append(f"<code>{dropbox_path}</code>")
            else:
                caption_lines.append(f"<code>{video_path}</code>")
            caption = "\n".join(caption_lines)
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
            logger.error("Error sending Dropbox video: %s", exc)
            await progress_msg.edit_text(f"❌ Error sending video: {exc}")
            await callback_query.answer("Error occurred while sending video.", show_alert=True)

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
