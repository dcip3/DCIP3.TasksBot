import asyncio
import html
import logging
import re
from collections import deque
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.job_helpers import (
    BATCH_COLUMN_WIDTH,
    JOBS_PAGE_SIZE,
    format_progress_old,
    group_and_sort_jobs,
    truncate_cell,
)
from app.core.config import settings
from app.core.ui_helpers import authorized_only
from app.services.deadline import (
    delete_job_by_user_id,
    get_job_tasks_by_user_id,
    get_jobs_list,
    get_workers_list,
    requeue_job_by_user_id,
    resume_job_by_user_id,
    suspend_job_by_user_id,
)

logger = logging.getLogger(__name__)

router = Router()

_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?)")
_FRAMES_RE = re.compile(r"^\s*(-?\d+)(?:\s*-\s*(-?\d+)(?:\s*x\s*(\d+))?)?\s*$")
_ETA_HISTORY_TTL_SECONDS = 3 * 60 * 60
_ETA_SHORT_WINDOW_SECONDS = 7 * 60
_ETA_LONG_WINDOW_SECONDS = 25 * 60
_ETA_EMA_ALPHA = 0.28
_ETA_MAX_TREND_BOOST = 1.30
_ETA_MIN_TREND_BOOST = 0.78
_ETA_MIN_PREDICTED_RATE = 1e-6
_ETA_MAX_SMOOTHING_GAP_SECONDS = 20 * 60
_ETA_HISTORY: dict[str, deque[tuple[datetime, float, float]]] = {}
_ETA_SMOOTHED_SECONDS: dict[str, tuple[datetime, float]] = {}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _prune_eta_state(now_utc: datetime) -> None:
    cutoff = now_utc - timedelta(seconds=_ETA_HISTORY_TTL_SECONDS)
    to_delete: list[str] = []
    for job_id, history in _ETA_HISTORY.items():
        while history and history[0][0] < cutoff:
            history.popleft()
        if not history:
            to_delete.append(job_id)
    for job_id in to_delete:
        _ETA_HISTORY.pop(job_id, None)
        _ETA_SMOOTHED_SECONDS.pop(job_id, None)


def _track_eta_history(
    job_id: str,
    now_utc: datetime,
    completed_frames: float,
    total_frames: float,
) -> deque[tuple[datetime, float, float]]:
    history = _ETA_HISTORY.setdefault(job_id, deque())
    if history:
        _, prev_completed, prev_total = history[-1]
        total_changed = abs(prev_total - total_frames) > 0.01
        progress_reset = completed_frames + 0.5 < prev_completed
        if total_changed or progress_reset:
            history.clear()
            _ETA_SMOOTHED_SECONDS.pop(job_id, None)

    history.append((now_utc, completed_frames, total_frames))
    cutoff = now_utc - timedelta(seconds=_ETA_HISTORY_TTL_SECONDS)
    while history and history[0][0] < cutoff:
        history.popleft()
    return history


def _estimate_rate_over_window(
    history: deque[tuple[datetime, float, float]],
    now_utc: datetime,
    window_seconds: int,
    min_duration_seconds: int,
    min_frame_delta: float,
) -> float | None:
    if len(history) < 2:
        return None

    latest_time, latest_completed, _ = history[-1]
    cutoff = now_utc - timedelta(seconds=window_seconds)
    baseline = history[0]
    for sample in reversed(history):
        if sample[0] <= cutoff:
            baseline = sample
            break

    base_time, base_completed, _ = baseline
    elapsed = (latest_time - base_time).total_seconds()
    frame_delta = latest_completed - base_completed
    if elapsed < min_duration_seconds or frame_delta < min_frame_delta:
        return None
    return frame_delta / elapsed


def _smooth_eta_seconds(job_id: str, now_utc: datetime, eta_seconds: float) -> float:
    prev = _ETA_SMOOTHED_SECONDS.get(job_id)
    if prev is None:
        _ETA_SMOOTHED_SECONDS[job_id] = (now_utc, eta_seconds)
        return eta_seconds

    prev_time, prev_eta = prev
    gap_seconds = (now_utc - prev_time).total_seconds()
    if gap_seconds < 0 or gap_seconds > _ETA_MAX_SMOOTHING_GAP_SECONDS:
        _ETA_SMOOTHED_SECONDS[job_id] = (now_utc, eta_seconds)
        return eta_seconds

    smoothed = (_ETA_EMA_ALPHA * eta_seconds) + ((1.0 - _ETA_EMA_ALPHA) * prev_eta)
    _ETA_SMOOTHED_SECONDS[job_id] = (now_utc, smoothed)
    return smoothed

def _escape_pre(value: object) -> str:
    return html.escape(str(value))

def _parse_progress_ratio(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        pct = float(value)
    else:
        match = _PROGRESS_RE.search(str(value))
        if not match:
            return None
        try:
            pct = float(match.group(1))
        except ValueError:
            return None
    if pct > 1.0:
        pct = pct / 100.0
    return max(0.0, min(pct, 1.0))

def _count_frames(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    total = 0
    for segment in text.split(","):
        seg = segment.strip()
        if not seg:
            continue
        match = _FRAMES_RE.match(seg)
        if not match:
            return None
        start = int(match.group(1))
        end_raw = match.group(2)
        step_raw = match.group(3)
        if end_raw is None:
            total += 1
            continue
        end = int(end_raw)
        step = int(step_raw) if step_raw else 1
        if step <= 0:
            return None
        if end < start:
            start, end = end, start
        total += ((end - start) // step) + 1
    return total or None

def _parse_task_datetime(value: object) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw or raw == "0001-01-01T00:00:00Z":
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def _get_frame_progress_metrics(tasks: list[dict]) -> tuple[float, float] | None:
    if not tasks:
        return None
    frame_counts: list[int] = []
    for task in tasks:
        frames = _count_frames(task.get("Frames"))
        if frames is None:
            return None
        frame_counts.append(frames)
    total_frames = float(sum(frame_counts))
    if total_frames <= 0:
        return None

    completed_frames = 0.0
    for task, frames in zip(tasks, frame_counts):
        stat = task.get("Stat", 1)
        if stat == 5:
            completed_frames += frames
            continue
        prog_ratio = _parse_progress_ratio(task.get("Prog"))
        if prog_ratio is None:
            continue
        if stat in {3, 4}:
            completed_frames += frames * prog_ratio

    if completed_frames > total_frames:
        completed_frames = total_frames
    return total_frames, completed_frames

def _compute_progress_from_tasks(tasks: list[dict], total_tasks: int) -> str | None:
    metrics = _get_frame_progress_metrics(tasks)
    if metrics is None:
        return None
    total_frames, completed = metrics
    percent = int((completed / total_frames) * 100) if total_frames else 0
    done_str = (
        str(int(round(completed)))
        if abs(completed - round(completed)) < 0.05
        else f"{completed:.1f}"
    )
    return f"{percent}% {done_str}/{total_frames}"

def _build_jobs_overview(
    combined_jobs: list[dict],
    page: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    jobs_slice = combined_jobs[page * JOBS_PAGE_SIZE : page * JOBS_PAGE_SIZE + JOBS_PAGE_SIZE]

    messages: list[str] = []
    buttons: list[InlineKeyboardButton] = []

    def get_job_icon(stat: int) -> str:
        if stat == 3:
            return "✅"
        if stat == 1:
            return "▶️"
        if stat == 6:
            return "⏳"
        if stat == 2:
            return "⏸️"
        if stat == 4:
            return "❌"
        return "❓"

    for job in jobs_slice:
        props = job.get("Props", {})
        batch = _resolve_batch_label(props)
        display_batch = truncate_cell(batch)
        total_tasks = props.get("Tasks", 0)
        completed_chunks = job.get("CompletedChunks", 0)
        progress_str = format_progress_old(completed_chunks, total_tasks)
        stat = job.get("Stat", 0)
        icon = get_job_icon(stat)

        safe_batch = _escape_pre(display_batch)
        safe_progress = _escape_pre(progress_str)
        messages.append(
            f"{icon} {safe_batch:<{BATCH_COLUMN_WIDTH}} {safe_progress:^16}\n{'-'*40}"
        )

        job_id = job.get("_id")
        if job_id:
            buttons.append(
                InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}")
            )

    header = f"{'Batch':<{BATCH_COLUMN_WIDTH + 2}} {'Progress':^16}"
    header += f"\n{'-'*40}"
    batch_text = "\n".join(messages) if messages else "No data"

    if buttons:
        inline_keyboard = []
        row = []
        for idx, button in enumerate(buttons, 1):
            row.append(button)
            if idx % 2 == 0:
                inline_keyboard.append(row)
                row = []
        if row:
            inline_keyboard.append(row)

        total_items = len(combined_jobs)
        total_pages = (total_items + JOBS_PAGE_SIZE - 1) // JOBS_PAGE_SIZE
        nav_buttons = []
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(text="⬅️ Back", callback_data=f"jobs_page:{page-1}")
            )
        if (page + 1) < total_pages:
            nav_buttons.append(
                InlineKeyboardButton(text="Next ➡️", callback_data=f"jobs_page:{page+1}")
            )
        if nav_buttons:
            inline_keyboard.append(nav_buttons)

        keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
        page_info = f"Page {page+1} of {total_pages}"
        text = f"<pre>{header}\n{batch_text}\n{page_info}</pre>\n\nSelect a job for details:"
        return text, keyboard

    return f"<pre>{header}\n{batch_text}</pre>", None

def _resolve_batch_label(props: dict) -> str:
    batch = (props.get("Batch") or "").strip()
    if batch:
        return batch
    name = (props.get("Name") or "Untitled").strip()
    return name or "Untitled"


def _extract_preview_meta(props: dict) -> tuple[bool, str | None]:
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


def _calculate_eta(
    job_id: str | None,
    tasks: list[dict],
    total_tasks: int,
    completed_chunks: int,
) -> str:
    eta_str = "N/A"
    try:
        now_utc = datetime.now(timezone.utc)
        _prune_eta_state(now_utc)
        if not tasks:
            return eta_str

        metrics = _get_frame_progress_metrics(tasks)
        if metrics is not None:
            total_frames, completed_frames = metrics
            remaining_frames = max(total_frames - completed_frames, 0.0)
            if remaining_frames <= 0:
                return "0:00:00"

            history: deque[tuple[datetime, float, float]] | None = None
            if job_id:
                history = _track_eta_history(
                    job_id=job_id,
                    now_utc=now_utc,
                    completed_frames=completed_frames,
                    total_frames=total_frames,
                )

            finished_seconds = 0.0
            finished_frames = 0.0
            running_seconds = 0.0
            running_frames = 0.0

            for task in tasks:
                frames = _count_frames(task.get("Frames"))
                if not frames:
                    continue
                stat = task.get("Stat", 1)
                start_time = _parse_task_datetime(task.get("StartRen"))
                if start_time is None:
                    continue

                if stat == 5:
                    comp_time = _parse_task_datetime(task.get("Comp"))
                    if comp_time is None or comp_time <= start_time:
                        continue
                    finished_seconds += (comp_time - start_time).total_seconds()
                    finished_frames += float(frames)
                    continue

                if stat == 4:
                    prog_ratio = _parse_progress_ratio(task.get("Prog"))
                    if prog_ratio is None or prog_ratio <= 0:
                        continue
                    rendered_frames = float(frames) * prog_ratio
                    # Show ETA as soon as we can infer at least ~1 rendered frame.
                    if rendered_frames < 1.0:
                        continue
                    elapsed = (now_utc - start_time).total_seconds()
                    if elapsed <= 0:
                        continue
                    running_seconds += elapsed
                    running_frames += rendered_frames

            bootstrap_rate: float | None = None
            if finished_frames > 0 and finished_seconds > 0:
                bootstrap_rate = finished_frames / finished_seconds
            elif running_frames > 0 and running_seconds > 0:
                bootstrap_rate = running_frames / running_seconds

            predicted_rate: float | None = bootstrap_rate
            if history is not None:
                short_rate = _estimate_rate_over_window(
                    history=history,
                    now_utc=now_utc,
                    window_seconds=_ETA_SHORT_WINDOW_SECONDS,
                    min_duration_seconds=90,
                    min_frame_delta=0.75,
                )
                long_rate = _estimate_rate_over_window(
                    history=history,
                    now_utc=now_utc,
                    window_seconds=_ETA_LONG_WINDOW_SECONDS,
                    min_duration_seconds=5 * 60,
                    min_frame_delta=2.5,
                )
                if short_rate is not None and long_rate is not None:
                    blended_rate = (0.70 * short_rate) + (0.30 * long_rate)
                    trend_ratio = short_rate / max(long_rate, _ETA_MIN_PREDICTED_RATE)
                    trend_boost = _clamp(
                        trend_ratio ** 0.35,
                        _ETA_MIN_TREND_BOOST,
                        _ETA_MAX_TREND_BOOST,
                    )
                    predicted_rate = blended_rate * trend_boost
                elif short_rate is not None:
                    predicted_rate = short_rate
                elif long_rate is not None:
                    predicted_rate = long_rate

            if predicted_rate is not None and predicted_rate > _ETA_MIN_PREDICTED_RATE:
                total_eta_seconds = remaining_frames / predicted_rate
                if total_eta_seconds > 0:
                    if job_id:
                        total_eta_seconds = _smooth_eta_seconds(
                            job_id=job_id,
                            now_utc=now_utc,
                            eta_seconds=total_eta_seconds,
                        )
                    eta_td = timedelta(seconds=int(total_eta_seconds))
                    eta_str = str(eta_td)
                return eta_str

        # Fallback to task-level estimation if frame-based metrics are unavailable.
        durations = []
        for task in tasks:
            if task.get("Stat") == 5:
                start_time = _parse_task_datetime(task.get("StartRen"))
                comp_time = _parse_task_datetime(task.get("Comp"))
                if start_time is None or comp_time is None or comp_time <= start_time:
                    continue
                duration_val = (comp_time - start_time).total_seconds()
                durations.append(duration_val)

        if durations:
            avg_duration = sum(durations) / len(durations)
            remaining = total_tasks - completed_chunks
            total_eta_seconds = avg_duration * remaining
            if total_eta_seconds > 0:
                eta_td = timedelta(seconds=int(total_eta_seconds))
                eta_str = str(eta_td)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Error calculating ETA: %s", exc)
    return eta_str


def _build_job_action_buttons(
    job_id: str | None,
    stat: int,
    is_preview_job: bool,
    preview_source_id: str | None,
) -> list[InlineKeyboardButton]:
    buttons: list[InlineKeyboardButton] = []
    if not job_id:
        return buttons
    if stat != 3:
        if stat == 2:
            buttons.append(
                InlineKeyboardButton(text="▶️ Resume", callback_data=f"resume_job:{job_id}")
            )
        else:
            buttons.append(
                InlineKeyboardButton(text="⏸️ Suspend", callback_data=f"suspend_job:{job_id}")
            )
        buttons.append(
            InlineKeyboardButton(text="🔄 Requeue", callback_data=f"requeue_job:{job_id}")
        )

    if is_preview_job and preview_source_id:
        buttons.append(
            InlineKeyboardButton(
                text="↩️ Source job",
                callback_data=f"job_info:{preview_source_id}",
            )
        )
        buttons.append(
            InlineKeyboardButton(
                text="🔁 Recreate preview",
                callback_data=f"preview_render_options:{preview_source_id}",
            )
        )
    elif is_preview_job:
        buttons.append(
            InlineKeyboardButton(
                text="ℹ️ Preview job",
                callback_data=f"preview_job:{job_id}",
            )
        )
    else:
        buttons.append(
            InlineKeyboardButton(text="🔍 Preview", callback_data=f"preview_job:{job_id}")
        )
    buttons.append(
        InlineKeyboardButton(text="🗑️ Delete", callback_data=f"delete_job:{job_id}")
    )
    buttons.append(
        InlineKeyboardButton(text="📋 Tasks", callback_data=f"tasks_job:{job_id}")
    )
    return buttons


def _build_job_info_keyboard(
    buttons: list[InlineKeyboardButton],
    job_id: str | None,
) -> InlineKeyboardMarkup:
    inline_keyboard = []
    row = []
    for idx, button in enumerate(buttons, 1):
        row.append(button)
        if idx % 2 == 0:
            inline_keyboard.append(row)
            row = []
    if row:
        inline_keyboard.append(row)

    if job_id:
        inline_keyboard.append(
            [
                InlineKeyboardButton(text="⬅️ Back", callback_data="jobs_back"),
                InlineKeyboardButton(text="🔄 Update", callback_data=f"job_update:{job_id}"),
            ]
        )
    else:
        inline_keyboard.append(
            [InlineKeyboardButton(text="⬅️ Back", callback_data="jobs_back")]
        )

    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


async def _edit_or_send_job_info(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        error_text = str(exc).lower()
        if "message is not modified" in error_text:
            return
        if (
            "message can't be edited" in error_text
            or "message to edit not found" in error_text
            or "message is too old" in error_text
        ):
            await message.answer(text, parse_mode="HTML", reply_markup=reply_markup)
            return
        raise


@router.message(F.text == "📂 Jobs")
@authorized_only
async def handle_jobs(message: Message, page: int = 0) -> None:
    """Display list of jobs with pagination."""
    if message.from_user is None:
        await message.answer("Error: User information not available.")
        return

    logger.info("Jobs button pressed by user %s", message.from_user.id)

    try:
        jobs = await get_jobs_list(message.from_user.id)
        if not jobs:
            await message.answer(
                "No jobs found. This could mean:\n"
                "• There are no active jobs in Deadline\n"
                "• Your user doesn't have access to jobs\n"
                "• There was an API error (check logs)"
            )
            return

        combined_jobs = await group_and_sort_jobs(jobs)
        text, keyboard = _build_jobs_overview(combined_jobs, page)
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as exc:
        logger.exception("Error handling jobs for user %s", message.from_user.id)
        await message.answer("Error occurred while fetching jobs.")


@router.callback_query(lambda c: c.data and c.data.startswith("jobs_page:"))
async def jobs_page_callback(callback_query: CallbackQuery) -> None:
    """Handle pagination for the jobs list."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    page_str = callback_query.data.split(":", 1)[1]
    try:
        page = int(page_str)
    except ValueError:
        await callback_query.answer("Invalid page number.", show_alert=True)
        return

    await callback_query.answer()

    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return

    if callback_query.message is None:
        await callback_query.answer("Error: Message not available.", show_alert=True)
        return

    try:
        jobs = await get_jobs_list(callback_query.from_user.id)
        if not jobs:
            await callback_query.message.edit_text("No jobs found.")
            return

        combined_jobs = await group_and_sort_jobs(jobs)
        text, keyboard = _build_jobs_overview(combined_jobs, page)
        if keyboard:
            await callback_query.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await callback_query.message.edit_text(text, parse_mode="HTML")

    except Exception as exc:
        logger.exception(
            "Error handling jobs page for user %s", callback_query.from_user.id
        )
        await callback_query.message.edit_text("Error occurred while fetching jobs.")


@router.callback_query(lambda c: c.data == "jobs_back")
async def jobs_back_callback(callback_query: CallbackQuery) -> None:
    """Return from job info view back to the first jobs page."""
    await callback_query.answer()

    if callback_query.from_user is None or callback_query.message is None:
        return

    try:
        jobs = await get_jobs_list(callback_query.from_user.id)
        if not jobs:
            await callback_query.message.edit_text("No jobs found.")
            return

        combined_jobs = await group_and_sort_jobs(jobs)
        text, keyboard = _build_jobs_overview(combined_jobs, 0)
        if keyboard:
            await callback_query.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await callback_query.message.edit_text(text, parse_mode="HTML")

    except Exception as exc:
        logger.exception(
            "Error handling jobs back for user %s", callback_query.from_user.id
        )
        await callback_query.message.edit_text("Error occurred while fetching jobs.")


@router.callback_query(lambda c: c.data and c.data.startswith("job_info:"))
async def job_info_callback(callback_query: CallbackQuery) -> None:
    """Show detailed information for the selected job."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return

    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        from app.services.deadline import get_jobs_list as fetch_jobs_list

        all_jobs = await fetch_jobs_list(callback_query.from_user.id)
        if not all_jobs:
            await callback_query.answer("Failed to get jobs list.", show_alert=True)
            return

        selected_job = next((j for j in all_jobs if j.get("_id") == job_id), None)
        if not selected_job:
            await callback_query.answer("Job not found.", show_alert=True)
            return

        props = selected_job.get("Props", {})
        batch_name = _resolve_batch_label(props)

        batch_jobs = [
            j for j in all_jobs
            if _resolve_batch_label(j.get("Props", {})) == batch_name
        ]

        await callback_query.message.delete()

        job_tasks_map = {}
        tasks_futures = []
        for job in batch_jobs:
            batch_job_id = job.get("_id")
            if batch_job_id:
                tasks_futures.append(
                    (
                        batch_job_id,
                        get_job_tasks_by_user_id(callback_query.from_user.id, batch_job_id),
                    )
                )

        if tasks_futures:
            results = await asyncio.gather(
                *[future for _, future in tasks_futures], return_exceptions=True
            )
            for (batch_job_id, _), tasks in zip(tasks_futures, results):
                if isinstance(tasks, Exception):
                    logger.error("Error loading tasks for job %s: %s", batch_job_id, tasks)
                    job_tasks_map[batch_job_id] = []
                else:
                    job_tasks_map[batch_job_id] = tasks or []

        for job in batch_jobs:
            props = job.get("Props", {})
            total_tasks = props.get("Tasks", 0)
            completed_chunks = job.get("CompletedChunks", 0)
            tasks = job_tasks_map.get(job.get("_id"), [])
            progress_str = (
                _compute_progress_from_tasks(tasks or [], total_tasks)
                or format_progress_old(completed_chunks, total_tasks)
            )
            full_name = props.get("Name", "Untitled")
            name = full_name.split("/")[-1] if "/" in full_name else full_name
            stat = job.get("Stat", 0)
            stat_name = settings.job_status_map.get(stat, "Unknown")
            current_job_id = job.get("_id")

            eta_str = "N/A"
            if stat in {1, 6}:
                eta_str = _calculate_eta(
                    current_job_id,
                    tasks or [],
                    total_tasks,
                    completed_chunks,
                )

            indent = ""
            info_lines = [
                "Job Info:",
                f"{indent}🗂️ Batch: <code>{html.escape(str(batch_name))}</code>",
                f"{indent}🏷️ Name: <code>{html.escape(str(name))}</code>",
                f"{indent}⚙️ Status: <code>{html.escape(str(stat_name))}</code>",
                f"{indent}⏳ Progress: <code>{html.escape(str(progress_str))}</code>",
            ]
            if stat in {1, 6}:
                info_lines.append(f"{indent}⏱️ ETA: <code>{html.escape(str(eta_str))}</code>")
            info_text = "\n".join(info_lines)

            is_preview_job, preview_source_id = _extract_preview_meta(props)
            buttons = _build_job_action_buttons(
                current_job_id,
                stat,
                is_preview_job,
                preview_source_id,
            )
            keyboard = _build_job_info_keyboard(buttons, current_job_id)

            await callback_query.message.answer(
                info_text, parse_mode="HTML", reply_markup=keyboard
            )

        await callback_query.answer()

    except Exception as exc:
        logger.exception(
            "Error handling job info for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while fetching job info.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("job_update:"))
async def job_update_callback(callback_query: CallbackQuery) -> None:
    """Refresh job info message."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    if callback_query.message is None:
        await callback_query.answer("Message not available.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]
    try:
        from app.services.deadline import get_jobs_list as fetch_jobs_list

        all_jobs = await fetch_jobs_list(callback_query.from_user.id)
        if not all_jobs:
            await callback_query.answer("Failed to get jobs list.", show_alert=True)
            return

        selected_job = next((j for j in all_jobs if j.get("_id") == job_id), None)
        if not selected_job:
            await callback_query.answer("Job not found.", show_alert=True)
            return

        props = selected_job.get("Props", {})
        batch_name = _resolve_batch_label(props)
        total_tasks = props.get("Tasks", 0)
        completed_chunks = selected_job.get("CompletedChunks", 0)
        tasks = await get_job_tasks_by_user_id(callback_query.from_user.id, job_id)
        progress_str = (
            _compute_progress_from_tasks(tasks or [], total_tasks)
            or format_progress_old(completed_chunks, total_tasks)
        )
        full_name = props.get("Name", "Untitled")
        name = full_name.split("/")[-1] if "/" in full_name else full_name
        stat = selected_job.get("Stat", 0)
        stat_name = settings.job_status_map.get(stat, "Unknown")

        eta_str = "N/A"
        if stat in {1, 6}:
            eta_str = _calculate_eta(
                job_id,
                tasks or [],
                total_tasks,
                completed_chunks,
            )

        info_lines = [
            "Job Info:",
            f"🗂️ Batch: <code>{html.escape(str(batch_name))}</code>",
            f"🏷️ Name: <code>{html.escape(str(name))}</code>",
            f"⚙️ Status: <code>{html.escape(str(stat_name))}</code>",
            f"⏳ Progress: <code>{html.escape(str(progress_str))}</code>",
        ]
        if stat in {1, 6}:
            info_lines.append(f"⏱️ ETA: <code>{html.escape(str(eta_str))}</code>")
        info_text = "\n".join(info_lines)

        is_preview_job, preview_source_id = _extract_preview_meta(props)
        buttons = _build_job_action_buttons(
            job_id,
            stat,
            is_preview_job,
            preview_source_id,
        )
        keyboard = _build_job_info_keyboard(buttons, job_id)

        await _edit_or_send_job_info(callback_query.message, info_text, keyboard)
        await callback_query.answer("Updated")

    except Exception as exc:
        logger.exception(
            "Error updating job info for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while updating job info.", show_alert=True)


@router.message(F.text == "🖥️ Workers")
@authorized_only
async def handle_workers(message: Message) -> None:
    """Display worker list."""
    if message.from_user is None:
        await message.answer("Error: User information not available.")
        return

    logger.info("Workers button pressed by user %s", message.from_user.id)

    try:
        workers = await get_workers_list(message.from_user.id)
        if not workers:
            await message.answer(
                "No workers (slaves) found. This could mean:\n"
                "• There are no active workers in Deadline\n"
                "• Your user doesn't have access to workers\n"
                "• There was an API error (check logs)"
            )
            return

        lines = []
        for worker in workers:
            info = worker.get("Info", {})
            name = info.get("Name", "Unknown")
            stat_num = info.get("Stat", 0)
            stat = settings.worker_status_map.get(stat_num, f"Unknown ({stat_num})")
            lines.append(f"{name:<24} {stat}")

        header = f"{'Name':<24} Status"
        header += f"\n{'-'*40}"
        body = "\n".join(lines) if lines else "No data"

        await message.answer(f"<pre>{header}\n{body}</pre>", parse_mode="HTML")

    except Exception as exc:
        logger.exception(
            "Error handling workers for user %s", message.from_user.id
        )
        await message.answer("Error occurred while fetching workers.")


@router.callback_query(lambda c: c.data and c.data.startswith("requeue_job:"))
async def requeue_job_callback(callback_query: CallbackQuery) -> None:
    """Handle requeue job button press."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        success = await requeue_job_by_user_id(callback_query.from_user.id, job_id)
        if success:
            await callback_query.answer("Job requeued successfully!")
        else:
            await callback_query.answer("Failed to requeue job.", show_alert=True)
    except Exception as exc:
        logger.exception(
            "Error requeuing job for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while requeuing job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("resume_job:"))
async def resume_job_callback(callback_query: CallbackQuery) -> None:
    """Handle resume job button press."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        success = await resume_job_by_user_id(callback_query.from_user.id, job_id)
        if success:
            await callback_query.answer("Job resumed successfully!")
        else:
            await callback_query.answer("Failed to resume job.", show_alert=True)
    except Exception as exc:
        logger.exception(
            "Error resuming job for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while resuming job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("suspend_job:"))
async def suspend_job_callback(callback_query: CallbackQuery) -> None:
    """Handle suspend job button press."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        success = await suspend_job_by_user_id(callback_query.from_user.id, job_id)
        if success:
            await callback_query.answer("Job suspended successfully!")
        else:
            await callback_query.answer("Failed to suspend job.", show_alert=True)
    except Exception as exc:
        logger.exception(
            "Error suspending job for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while suspending job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("delete_job:"))
async def delete_job_callback(callback_query: CallbackQuery) -> None:
    """Handle delete job button press."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        success = await delete_job_by_user_id(callback_query.from_user.id, job_id)
        if success:
            await callback_query.answer("Job deleted successfully!")
            if callback_query.message:
                await callback_query.message.edit_text("Job has been deleted.")
        else:
            await callback_query.answer("Failed to delete job.", show_alert=True)
    except Exception as exc:
        logger.exception(
            "Error deleting job for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while deleting job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("tasks_job:"))
async def tasks_job_callback(callback_query: CallbackQuery) -> None:
    """Show task breakdown for a job."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        tasks = await get_job_tasks_by_user_id(callback_query.from_user.id, job_id)
        if not tasks:
            if callback_query.message:
                await callback_query.message.answer("No tasks found for this job.")
            await callback_query.answer()
            return

        lines = []
        header = f"{'Frames':<18} {'Prog':^10} {'Time':^12}"
        header += f"\n{'-'*42}"
        lines.append(header)

        def get_task_icon(stat: int) -> str:
            if stat == 5:
                return "✅"
            if stat == 4:
                return "▶️"
            if stat == 3:
                return "⏸️"
            if stat == 6:
                return "❌"
            if stat in (2, 8):
                return "⏳"
            return "❓"

        for task in tasks:
            frames = _escape_pre(task.get("Frames", ""))
            prog = _escape_pre(task.get("Prog", ""))
            stat = task.get("Stat", 1)
            icon = get_task_icon(stat)
            rendertime_str = ""
            start_str = task.get("StartRen")
            if start_str and start_str != "0001-01-01T00:00:00Z":
                try:
                    start_time = datetime.fromisoformat(start_str)
                    if stat == 5:
                        comp_str = task.get("Comp")
                        if comp_str and comp_str != "0001-01-01T00:00:00Z":
                            comp_time = datetime.fromisoformat(comp_str)
                            duration = comp_time.astimezone(timezone.utc) - start_time.astimezone(
                                timezone.utc
                            )
                            rendertime_str = str(duration).split(".")[0]
                    elif stat == 4:
                        now_utc = datetime.now(timezone.utc)
                        duration = now_utc - start_time.astimezone(timezone.utc)
                        rendertime_str = str(duration).split(".")[0]
                except Exception:  # pragma: no cover - defensive
                    pass
            safe_rendertime = _escape_pre(rendertime_str)
            line = f"{icon} {frames:<16} {prog:^10} {safe_rendertime:^12}"
            lines.append(line)

        message_text = "<pre>" + "\n".join(lines) + "</pre>"
        if callback_query.message:
            await callback_query.message.answer(message_text, parse_mode="HTML")
        await callback_query.answer()

    except Exception as exc:
        logger.exception(
            "Error getting tasks for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while fetching tasks.", show_alert=True)
