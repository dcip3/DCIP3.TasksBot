import contextlib
import logging
import time
from typing import Optional

from aiogram import Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.auth import get_deadline_credentials
from app.storage.user_settings import (
    PREVIEW_DEFAULT_WORKER_AUTO,
    get_preview_default_worker,
)
from app.core.bot_core import bot
from app.core.ui_helpers import cancel_inline_button
from app.services.deadline import (
    ALLOWED_WORKER_STATUSES,
    delete_job_by_user_id,
    get_job_info_by_user_id,
    get_workers_list,
    WorkerStatusError,
)
from app.services.preview.render import (
    PreviewSubmissionError,
    create_video_from_job,
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


async def _start_deadline_preview(callback_query: CallbackQuery, job_id: str) -> None:
    if callback_query.from_user is None:
        await callback_query.answer("Error: user not found.", show_alert=True)
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
                "Error auto-submitting preview with inherited job machine list: %s",
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

        del credentials

        preview_job_detected = False
        preview_source_id = None
        job_info = await get_job_info_by_user_id(callback_query.from_user.id, job_id)
        if job_info:
            preview_job_detected, preview_source_id = _extract_preview_source(job_info)
            if preview_job_detected and preview_source_id:
                job_id = preview_source_id

        if preview_job_detected and not preview_source_id:
            await callback_query.answer(
                "This is a preview job. Open the source job to recreate previews.",
                show_alert=True,
            )
            return

        await _start_deadline_preview(callback_query, job_id)
        return

    except Exception as exc:
        logger.error("Error handling preview for user %s: %s", callback_query.from_user.id, exc)
        await callback_query.answer("Error occurred while generating preview.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("preview_render_options:"))
async def preview_render_options_callback(callback_query: CallbackQuery) -> None:
    """Start a new Deadline preview for the job."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    await _start_deadline_preview(callback_query, job_id)
    with contextlib.suppress(Exception):
        await callback_query.answer()


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
                        cancel_inline_button(callback_data=f"preview_job_cancel:{preview_id}")
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
                [cancel_inline_button(callback_data="preview_cancel")],
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
    """Handle legacy render-method buttons; everything renders via Deadline."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    parts = callback_query.data.split(":", 2)
    if len(parts) != 3:
        await callback_query.answer("Invalid selection.", show_alert=True)
        return

    _, mode, job_id = parts

    # "server" is a legacy mode from old inline keyboards; route it to Deadline too.
    if mode in {"deadline", "server"}:
        await _start_deadline_preview(callback_query, job_id)
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
        if worker_choice == "auto":
            await create_new_video_process(
                callback_query,
                job_id,
                use_any_machine=False,
                skip_worker_validation=True,
                progress_message=progress_msg,
                specific_worker=None,
            )
        elif worker_choice == "any":
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

        keyboard_rows = [
            [
                InlineKeyboardButton(
                    text="✅ Auto",
                    callback_data=f"preview_submit:{job_id}:auto",
                )
            ]
        ]
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
                cancel_inline_button(callback_data="preview_cancel")
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
    """Legacy button from removed server-side preview generation."""
    await callback_query.answer("Nothing to cancel.", show_alert=False)


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
                        await bot.delete_message(stored_chat_id, stored_message_id)
            if callback_query.message:
                with contextlib.suppress(Exception):
                    await callback_query.message.delete()
            await callback_query.answer("Preview generation cancelled.", show_alert=False)
        else:
            await callback_query.answer("Failed to cancel preview job.", show_alert=True)
    except Exception as exc:
        logger.error("Error cancelling preview job %s: %s", preview_job_id, exc)
        await callback_query.answer("Error cancelling job.", show_alert=True)


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
        text_lines.append("\nDefault: Auto (job machine list)")
    elif default_worker:
        text_lines.append(f"\nDefault: {default_worker}")

    text = "\n".join(text_lines)

    keyboard_rows = [
        [
            InlineKeyboardButton(
                text=(
                    "✅ Auto"
                    if default_worker == PREVIEW_DEFAULT_WORKER_AUTO
                    else "Auto"
                ),
                callback_data=f"preview_submit:{job_id}:auto",
            )
        ]
    ]

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
            cancel_inline_button(callback_data="preview_cancel")
        ]
    )

    await _show_worker_menu(
        callback_query,
        text,
        InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("send_dbx_video:"))
async def send_dbx_video_callback(callback_query: CallbackQuery) -> None:
    """Legacy button from removed Dropbox video delivery."""
    await callback_query.answer(
        "This feature was removed. Use the preview button to render a new one.",
        show_alert=True,
    )
