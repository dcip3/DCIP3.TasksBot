import asyncio
import html
import logging
import re
import time
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
from app.core.ui_helpers import (
    authorized_only,
    back_inline_button,
    close_inline_button,
    close_menu,
    inline_button,
)
from app.services import render_cost
from app.services.deadline import (
    delete_job_by_user_id,
    get_job_info_by_user_id,
    get_job_tasks_by_user_id,
    get_jobs_list,
    get_worker_infosettings_by_user_id,
    get_workers_list,
    requeue_job_by_user_id,
    resume_failed_job_by_user_id,
    resume_job_by_user_id,
    save_worker_settings_by_user_id,
    suspend_job_by_user_id,
    take_credentials_rejected_notice,
)

logger = logging.getLogger(__name__)

router = Router()

_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?)")
_FRAMES_RE = re.compile(r"^\s*(-?\d+)(?:\s*-\s*(-?\d+)(?:\s*x\s*(\d+))?)?\s*$")
# Rows per page in the task breakdown. Sized so a typical 58-chunk job stays on
# one page while a job chunked frame-by-frame still fits Telegram's 4096-char
# message limit with room to spare.
TASKS_PAGE_SIZE = 60
_ETA_HISTORY_TTL_SECONDS = 3 * 60 * 60
_ETA_SHORT_WINDOW_SECONDS = 7 * 60
_ETA_LONG_WINDOW_SECONDS = 25 * 60
_ETA_EMA_ALPHA = 0.28
_ETA_MAX_TREND_BOOST = 1.30
_ETA_MIN_TREND_BOOST = 0.78
_ETA_MIN_PREDICTED_RATE = 1e-6
_ETA_MAX_SMOOTHING_GAP_SECONDS = 20 * 60
_ETA_HISTORY: dict[str, deque[tuple[datetime, float, float, int]]] = {}
_ETA_SMOOTHED_SECONDS: dict[str, tuple[datetime, float, int]] = {}
_JOBS_OVERVIEW_CACHE_REFRESH_SECONDS = 15.0
_JOBS_OVERVIEW_CACHE_MAX_STALE_SECONDS = 3 * 60.0
_JOBS_OVERVIEW_CACHE_MAX_ENTRIES = 128
_JOBS_OVERVIEW_CACHE: dict[int, tuple[float, float, list[dict]]] = {}
_JOBS_OVERVIEW_REFRESH_TASKS: dict[int, asyncio.Task] = {}
_WORKERS_PAGE_SIZE = 8
_WORKER_NAME_COLUMN_WIDTH = BATCH_COLUMN_WIDTH - 2


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _prune_jobs_overview_cache(now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    expired_user_ids = [
        user_id
        for user_id, (_, stale_until, _) in _JOBS_OVERVIEW_CACHE.items()
        if stale_until <= current
    ]
    for user_id in expired_user_ids:
        _JOBS_OVERVIEW_CACHE.pop(user_id, None)
        refresh_task = _JOBS_OVERVIEW_REFRESH_TASKS.get(user_id)
        if refresh_task is not None and refresh_task.done():
            _JOBS_OVERVIEW_REFRESH_TASKS.pop(user_id, None)

    if len(_JOBS_OVERVIEW_CACHE) <= _JOBS_OVERVIEW_CACHE_MAX_ENTRIES:
        return

    survivors = sorted(
        _JOBS_OVERVIEW_CACHE.items(),
        key=lambda item: item[1][1],
        reverse=True,
    )[:_JOBS_OVERVIEW_CACHE_MAX_ENTRIES]
    _JOBS_OVERVIEW_CACHE.clear()
    _JOBS_OVERVIEW_CACHE.update(survivors)


def _get_cached_jobs_overview(user_id: int) -> tuple[list[dict], bool] | None:
    now = time.monotonic()
    _prune_jobs_overview_cache(now)
    cached = _JOBS_OVERVIEW_CACHE.get(user_id)
    if cached is None:
        return None
    refresh_at, stale_until, combined_jobs = cached
    if stale_until <= now:
        _JOBS_OVERVIEW_CACHE.pop(user_id, None)
        return None
    return combined_jobs, refresh_at <= now


def _store_jobs_overview(user_id: int, combined_jobs: list[dict]) -> None:
    now = time.monotonic()
    refresh_at = now + _JOBS_OVERVIEW_CACHE_REFRESH_SECONDS
    stale_until = now + _JOBS_OVERVIEW_CACHE_MAX_STALE_SECONDS
    _JOBS_OVERVIEW_CACHE[user_id] = (refresh_at, stale_until, combined_jobs)
    _prune_jobs_overview_cache()


async def _refresh_jobs_overview_cache(user_id: int) -> None:
    started_at = time.monotonic()
    try:
        jobs = await get_jobs_list(user_id)
        if not jobs:
            logger.info(
                "Jobs overview background refresh for user %s returned no jobs in %sms",
                user_id,
                int((time.monotonic() - started_at) * 1000),
            )
            return

        combined_jobs = await group_and_sort_jobs(jobs)
        _store_jobs_overview(user_id, combined_jobs)
        logger.info(
            "Jobs overview background refresh completed for user %s in %sms (jobs=%s)",
            user_id,
            int((time.monotonic() - started_at) * 1000),
            len(combined_jobs),
        )
    except Exception:
        logger.exception("Jobs overview background refresh failed for user %s", user_id)
    finally:
        _JOBS_OVERVIEW_REFRESH_TASKS.pop(user_id, None)


def _ensure_jobs_overview_refresh(user_id: int) -> None:
    refresh_task = _JOBS_OVERVIEW_REFRESH_TASKS.get(user_id)
    if refresh_task is not None and not refresh_task.done():
        return

    task = asyncio.create_task(_refresh_jobs_overview_cache(user_id))
    _JOBS_OVERVIEW_REFRESH_TASKS[user_id] = task


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
    active_renderers: int,
) -> deque[tuple[datetime, float, float, int]]:
    history = _ETA_HISTORY.setdefault(job_id, deque())
    if history:
        _, prev_completed, prev_total, _ = history[-1]
        total_changed = abs(prev_total - total_frames) > 0.01
        progress_reset = completed_frames + 0.5 < prev_completed
        if total_changed or progress_reset:
            history.clear()
            _ETA_SMOOTHED_SECONDS.pop(job_id, None)

    history.append((now_utc, completed_frames, total_frames, max(active_renderers, 0)))
    cutoff = now_utc - timedelta(seconds=_ETA_HISTORY_TTL_SECONDS)
    while history and history[0][0] < cutoff:
        history.popleft()
    return history


def _estimate_efficiency_over_window(
    history: deque[tuple[datetime, float, float, int]],
    now_utc: datetime,
    window_seconds: int,
    min_duration_seconds: int,
    min_frame_delta: float,
) -> float | None:
    if len(history) < 2:
        return None

    samples = list(history)
    latest_time, latest_completed, _, _ = samples[-1]
    cutoff = now_utc - timedelta(seconds=window_seconds)
    baseline_idx = 0
    for idx in range(len(samples) - 1, -1, -1):
        sample = samples[idx]
        if sample[0] <= cutoff:
            baseline_idx = idx
            break

    relevant_samples = samples[baseline_idx:]
    if len(relevant_samples) < 2:
        return None

    base_time, base_completed, _, _ = relevant_samples[0]
    elapsed = (latest_time - base_time).total_seconds()
    frame_delta = latest_completed - base_completed
    if elapsed < min_duration_seconds or frame_delta < min_frame_delta:
        return None
    worker_seconds = 0.0
    for prev_sample, next_sample in zip(relevant_samples, relevant_samples[1:]):
        prev_time, _, _, prev_active = prev_sample
        next_time, _, _, next_active = next_sample
        gap = (next_time - prev_time).total_seconds()
        if gap <= 0:
            continue
        average_active = (max(prev_active, 0) + max(next_active, 0)) / 2.0
        worker_seconds += gap * average_active
    if worker_seconds <= 0:
        return None
    return frame_delta / worker_seconds


def _smooth_eta_seconds(
    job_id: str,
    now_utc: datetime,
    eta_seconds: float,
    active_renderers: int,
) -> float:
    prev = _ETA_SMOOTHED_SECONDS.get(job_id)
    if prev is None:
        _ETA_SMOOTHED_SECONDS[job_id] = (now_utc, eta_seconds, active_renderers)
        return eta_seconds

    prev_time, prev_eta, prev_active = prev
    gap_seconds = (now_utc - prev_time).total_seconds()
    if (
        gap_seconds < 0
        or gap_seconds > _ETA_MAX_SMOOTHING_GAP_SECONDS
        or prev_active != active_renderers
    ):
        _ETA_SMOOTHED_SECONDS[job_id] = (now_utc, eta_seconds, active_renderers)
        return eta_seconds

    smoothed = (_ETA_EMA_ALPHA * eta_seconds) + ((1.0 - _ETA_EMA_ALPHA) * prev_eta)
    _ETA_SMOOTHED_SECONDS[job_id] = (now_utc, smoothed, active_renderers)
    return smoothed

def _escape_pre(value: object) -> str:
    return html.escape(str(value))


def _extract_job_errors(job: dict) -> int:
    try:
        return max(0, int(job.get("Errs", 0) or 0))
    except (TypeError, ValueError):
        return 0


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


def _task_worker_key(task: dict, fallback_index: int) -> str:
    for key in ("Slave", "Worker", "Machine", "WorkerName"):
        raw_value = task.get(key)
        if raw_value is None:
            continue
        value = str(raw_value).strip()
        if value:
            return value.casefold()

    task_id = task.get("TaskId") or task.get("TaskID") or task.get("_id")
    if task_id is not None:
        return f"task:{task_id}"
    return f"anonymous:{fallback_index}"


def _count_active_renderers(tasks: list[dict]) -> int:
    active_workers: set[str] = set()
    for idx, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        if task.get("Stat", 1) != 4:
            continue
        active_workers.add(_task_worker_key(task, idx))
    return len(active_workers)

_in_flight_frames = render_cost.in_flight_frames


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
        if stat not in {3, 4}:
            continue
        prog_ratio = _parse_progress_ratio(task.get("Prog"))
        done = frames * prog_ratio if prog_ratio is not None else 0.0
        # Deadline's task progress only ticks over on whole frames (1 of 5 =
        # 20%), so the bar would jump 20/40/60. The plugin status carries the
        # frame in flight and how far into it we are - fold that in.
        in_flight = _in_flight_frames(task)
        if in_flight is not None:
            done = max(done, in_flight)
        completed_frames += min(done, float(frames))

    if completed_frames > total_frames:
        completed_frames = total_frames
    return total_frames, completed_frames

def _compute_progress_from_tasks(tasks: list[dict], total_tasks: int) -> str | None:
    metrics = _get_frame_progress_metrics(tasks)
    if metrics is None:
        return None
    total_frames, completed = metrics
    percent = int((completed / total_frames) * 100) if total_frames else 0
    # The percentage carries the partially rendered frame so the bar creeps
    # instead of jumping a whole chunk at a time; the frame counter stays whole,
    # since "156.9/290 frames" reads as noise rather than detail.
    done_str = str(int(completed))
    total_str = (
        str(int(round(total_frames)))
        if abs(total_frames - round(total_frames)) < 0.05
        else f"{total_frames:.1f}"
    )
    return f"{percent}% {done_str}/{total_str}"

def _jobs_action_row(page: int) -> list[InlineKeyboardButton]:
    """Close and Update, kept on their own row away from the paging arrows."""
    return [
        close_inline_button(callback_data="jobs_close"),
        inline_button(
            text="🔄 Update",
            callback_data=f"jobs_update:{page}",
            style="primary",
        ),
    ]


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

        # Paging on its own row, then the actions. Update used to sit between
        # Back and Next, so the two halves of one control were split apart by a
        # button that does something else entirely.
        nav_buttons = []
        if page > 0:
            nav_buttons.append(back_inline_button(callback_data=f"jobs_page:{page-1}"))
        if (page + 1) < total_pages:
            nav_buttons.append(
                back_inline_button(
                    callback_data=f"jobs_page:{page+1}",
                    text="Next ➡️",
                )
            )
        if nav_buttons:
            inline_keyboard.append(nav_buttons)
        inline_keyboard.append(_jobs_action_row(page))

        keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
        page_info = f"Page {page+1} of {total_pages}"
        text = f"<pre>{header}\n{batch_text}\n{page_info}</pre>\n\nSelect a job for details:"
        return text, keyboard

    # No jobs to pick from, but the message still deserves a way out.
    return (
        f"<pre>{header}\n{batch_text}</pre>",
        InlineKeyboardMarkup(inline_keyboard=[_jobs_action_row(page)]),
    )


def _worker_name(worker: dict) -> str:
    info = worker.get("Info", {})
    name = str(info.get("Name") or "").strip()
    return name or "Unknown"


def _worker_status(worker: dict) -> str:
    info = worker.get("Info", {})
    stat_num = info.get("Stat", 0)
    base_status = settings.worker_status_map.get(stat_num, f"Unknown ({stat_num})")
    worker_settings = worker.get("Settings", {})
    if isinstance(worker_settings, dict):
        enabled_raw = worker_settings.get("Enable")
        if isinstance(enabled_raw, bool) and not enabled_raw:
            return f"Dis ({base_status})"
    return base_status


def _worker_status_icon(worker: dict) -> str:
    worker_settings = worker.get("Settings", {})
    if isinstance(worker_settings, dict):
        enabled_raw = worker_settings.get("Enable")
        if isinstance(enabled_raw, bool) and not enabled_raw:
            return "🚫"

    info = worker.get("Info", {})
    stat_num = info.get("Stat", 0)
    if stat_num == 1:  # Rendering
        return "▶️"
    if stat_num == 2:  # Idle
        return "🟢"
    if stat_num == 3:  # Offline
        return "⚫"
    if stat_num == 4:  # Stalled
        return "⚠️"
    if stat_num == 8:  # StartingJob
        return "⏳"
    return "❓"


def _sorted_workers(workers: list[dict]) -> list[dict]:
    return sorted(workers, key=lambda item: _worker_name(item).lower())


def _normalize_workers_page(page: int, total_items: int) -> tuple[int, int]:
    total_pages = max(1, (total_items + _WORKERS_PAGE_SIZE - 1) // _WORKERS_PAGE_SIZE)
    normalized_page = max(0, min(page, total_pages - 1))
    return normalized_page, total_pages


def _build_workers_overview(
    workers: list[dict],
    page: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    sorted_workers = _sorted_workers(workers)
    if not sorted_workers:
        return "<pre>No workers found.</pre>", None

    page, total_pages = _normalize_workers_page(page, len(sorted_workers))
    start = page * _WORKERS_PAGE_SIZE
    stop = start + _WORKERS_PAGE_SIZE
    workers_slice = sorted_workers[start:stop]

    lines: list[str] = []
    worker_buttons: list[InlineKeyboardButton] = []
    for idx, worker in enumerate(workers_slice, start=start):
        name = _worker_name(worker)
        status = _worker_status(worker)
        icon = _worker_status_icon(worker)
        display_name = truncate_cell(name, _WORKER_NAME_COLUMN_WIDTH)
        lines.append(
            f"{icon} {_escape_pre(display_name):<{_WORKER_NAME_COLUMN_WIDTH}}  {_escape_pre(status)}"
        )
        worker_buttons.append(
            InlineKeyboardButton(
                text=display_name,
                callback_data=f"worker_info:{idx}:{page}",
            )
        )

    inline_keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for idx, button in enumerate(worker_buttons, start=1):
        row.append(button)
        if idx % 2 == 0:
            inline_keyboard.append(row)
            row = []
    if row:
        inline_keyboard.append(row)

    nav_buttons: list[InlineKeyboardButton] = []
    if page > 0:
        nav_buttons.append(back_inline_button(callback_data=f"workers_page:{page-1}"))
    if (page + 1) < total_pages:
        nav_buttons.append(
            back_inline_button(
                callback_data=f"workers_page:{page+1}",
                text="Next ➡️",
            )
        )
    if nav_buttons:
        inline_keyboard.append(nav_buttons)

    inline_keyboard.append(
        [
            close_inline_button(callback_data="workers_close"),
            inline_button(
                text="🔄 Update",
                callback_data=f"workers_update:{page}",
                style="primary",
            ),
        ]
    )

    header = f"{'':2}{'Name':<{_WORKER_NAME_COLUMN_WIDTH}}   Status"
    header += f"\n{'-'*40}"
    body = "\n".join(lines) if lines else "No data"
    page_info = f"Page {page+1} of {total_pages}"
    text = f"<pre>{header}\n{body}\n{page_info}</pre>\n\nSelect a worker for details:"
    return text, InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


_PROGRESS_RE = re.compile(r"^\s*(\d+)%\s+(\S+)\s*$")
_ETA_RE = re.compile(r"^\s*(\d+):(\d{2}):(\d{2})\s*$")
_JOB_STATUS_ICONS = {
    "Active": "🟢",
    "Completed": "✅",
    "Failed": "🔴",
    "Suspended": "⏸️",
    "Pending": "⏳",
    "Unknown": "❔",
}


def _progress_bar(percent: int, width: int = 12) -> str:
    """Render a compact progress bar for a 0-100 percentage."""
    percent = max(0, min(100, percent))
    filled = round(percent / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _humanize_eta(eta_str: str) -> str:
    """Turn Deadline's H:MM:SS estimate into something readable at a glance."""
    match = _ETA_RE.match(str(eta_str or ""))
    if not match:
        return str(eta_str or "").strip()
    hours, minutes, seconds = (int(part) for part in match.groups())
    if hours == 0 and minutes == 0:
        return "less than a minute" if seconds else "finishing"
    if hours == 0:
        return f"{minutes} min"
    if minutes == 0:
        return f"{hours} h"
    return f"{hours} h {minutes} min"


def _render_busy_seconds(tasks: list, now_utc: datetime) -> float | None:
    """How long this job was actually rendering, waiting not counted.

    A job does not render for every minute between its first task and its last:
    the farm takes its machines away for other work, probing suspends most of
    its tasks, a requeue puts it back in the queue. Counting the whole span can
    make a job look three times longer than the time it had a task running -
    the rest of that time it sat waiting its turn.

    So this measures the union of the intervals in which at least one task was
    rendering. Overlaps count once: two machines working in parallel make a job
    finish sooner, not take longer.
    """
    spans: list[list[datetime]] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        started = _parse_task_datetime(task.get("StartRen")) or _parse_task_datetime(
            task.get("Start")
        )
        if started is None:
            continue
        finished = _parse_task_datetime(task.get("Comp"))
        if finished is None:
            # 4 = rendering right now; anything else without a completion time
            # was requeued or suspended, and its earlier run cannot be measured.
            if task.get("Stat") != 4:
                continue
            finished = now_utc
        if finished <= started:
            continue
        spans.append([started, finished])

    if not spans:
        return None

    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    seconds = sum((end - start).total_seconds() for start, end in merged)
    return seconds if seconds > 0 else None


def _job_render_seconds(
    job: dict,
    now_utc: datetime | None = None,
    *,
    tasks: list | None = None,
) -> float | None:
    """Time the render has taken, counting only the time it was rendering.

    Falls back to the wall time since DateStart when there are no task details
    to go on - still better than measuring from submission, since a job can sit
    queued for hours before it starts.
    """
    if not isinstance(job, dict):
        return None
    now = now_utc or datetime.now(timezone.utc)

    busy = _render_busy_seconds(tasks or [], now)
    if busy is not None:
        return busy

    started = render_cost._parse_datetime(job.get("DateStart"))
    if started is None:
        return None
    finished = render_cost._parse_datetime(job.get("DateComp"))
    end = finished or now
    seconds = (end - started).total_seconds()
    return seconds if seconds > 0 else None


def _humanize_duration(seconds: float) -> str:
    """Short, readable duration: "8 h 21 min", "47 min", "38 s"."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min"
    return f"{secs} s"


def _build_job_info_text(
    *,
    batch_name: str,
    name: str,
    stat: int,
    stat_name: str,
    progress_str: str,
    errors_count: int,
    eta_str: str,
    render_seconds: float | None = None,
) -> str:
    """Job card: name first, then status, a progress bar and anything notable."""
    icon = _JOB_STATUS_ICONS.get(str(stat_name), "❔")
    lines = [
        f"<b>{html.escape(str(name))}</b>",
        f"🗂️ <code>{html.escape(str(batch_name))}</code>",
        "",
        f"{icon} {html.escape(str(stat_name))}",
    ]

    match = _PROGRESS_RE.match(str(progress_str or ""))
    if match:
        percent = int(match.group(1))
        counts = match.group(2)
        # Bar and percentage read as one thing; the frame counter gets its own
        # line so neither has to be hunted for inside a long row.
        lines.append(f"<code>{_progress_bar(percent)}</code> {percent}%")
        lines.append(f"🎞️ {html.escape(counts)}")
    elif progress_str:
        lines.append(f"⏳ {html.escape(str(progress_str))}")

    # One clock, read left to right: time already spent, then time still to go.
    # Two separate clock lines looked like two unrelated metrics. The elapsed
    # half stays after the render ends, when the ETA half has nothing to say.
    time_parts: list[str] = []
    if render_seconds:
        time_parts.append(html.escape(_humanize_duration(render_seconds)))
    if stat in {1, 6}:
        eta_human = _humanize_eta(eta_str)
        if eta_human and eta_human.upper() != "N/A":
            time_parts.append(f"{html.escape(eta_human)} left")
    if time_parts:
        lines.append("⏱️ " + " · ".join(time_parts))

    if errors_count:
        suffix = "" if int(errors_count) == 1 else "s"
        lines.append(f"❌ {errors_count} error{suffix}")

    return "\n".join(lines)


def _build_worker_info_text(worker_entry: dict) -> str:
    info = worker_entry.get("Info", {}) if isinstance(worker_entry, dict) else {}
    name = str(info.get("Name") or "Unknown")
    status_line = _worker_status(worker_entry)
    current_job = str(info.get("JobName") or "").strip() or "Idle"

    return "\n".join(
        [
            "Worker Info:",
            f"🖥️ Name: <code>{html.escape(name)}</code>",
            f"⚙️ Status: <code>{html.escape(str(status_line))}</code>",
            f"🎬 Current Job: <code>{html.escape(current_job)}</code>",
        ]
    )


def _worker_enable_value(worker_entry: dict) -> bool | None:
    if not isinstance(worker_entry, dict):
        return None
    worker_settings = worker_entry.get("Settings", {})
    if not isinstance(worker_settings, dict):
        return None
    enabled_raw = worker_settings.get("Enable")
    return enabled_raw if isinstance(enabled_raw, bool) else None


def _build_worker_info_keyboard(
    page: int,
    worker_index: int,
    enabled: bool | None,
) -> InlineKeyboardMarkup:
    if enabled is True:
        toggle_text = "🚫 Disable"
        target_value = 0
    else:
        toggle_text = "✅ Enable"
        target_value = 1

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=toggle_text,
                    callback_data=f"worker_toggle_enable:{worker_index}:{page}:{target_value}",
                )
            ],
            [
                back_inline_button(callback_data=f"workers_back:{page}"),
                inline_button(
                    text="🔄 Update",
                    callback_data=f"worker_update:{worker_index}:{page}",
                    style="primary",
                ),
            ],
        ]
    )


def _resolve_worker_by_index(workers: list[dict], worker_index: int) -> dict | None:
    sorted_workers = _sorted_workers(workers)
    if worker_index < 0 or worker_index >= len(sorted_workers):
        return None
    return sorted_workers[worker_index]


async def _render_worker_details(
    callback_query: CallbackQuery,
    worker_index: int,
    page: int,
    *,
    answer_text: str | None = None,
) -> bool:
    if callback_query.from_user is None or callback_query.message is None:
        return False

    workers = await get_workers_list(callback_query.from_user.id)
    selected_worker = _resolve_worker_by_index(workers, worker_index)
    if selected_worker is None:
        await callback_query.answer("Worker not found.", show_alert=True)
        return False

    worker_name = _worker_name(selected_worker)
    worker_entry = await get_worker_infosettings_by_user_id(
        callback_query.from_user.id,
        worker_name,
    )
    details = worker_entry or selected_worker
    info_text = _build_worker_info_text(details)
    keyboard = _build_worker_info_keyboard(
        page,
        worker_index,
        _worker_enable_value(details),
    )
    await _edit_or_send_job_info(callback_query.message, info_text, keyboard)
    if answer_text:
        await callback_query.answer(answer_text)
    else:
        await callback_query.answer()
    return True


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


# How much of the frame range the samples must span before the cost curve is
# allowed to speak. Below this it is interpolating over stretches it has never
# seen, which is exactly the guesswork the curve exists to avoid.
_COST_MODEL_MIN_COVERAGE = 0.60


def _format_eta_clock(seconds: float) -> str:
    """H:MM:SS, with hours running past 24 instead of rolling into days.

    str(timedelta) renders a day and a half as "1 day, 12:00:00", which the
    display formatter cannot parse - and long renders are exactly the case this
    estimator exists for.
    """
    total = max(int(seconds), 0)
    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _cost_model_eta(
    tasks: list[dict],
    active_renderers: int,
    now_utc: datetime,
) -> float | None:
    """ETA from the frame-cost curve, or None if the samples don't support one."""
    if active_renderers <= 0:
        return None
    try:
        samples = render_cost.collect_samples(tasks, now_utc)
        if not samples:
            return None
        if render_cost.coverage(samples) < _COST_MODEL_MIN_COVERAGE:
            return None
        return render_cost.estimate_remaining_seconds(samples, active_renderers)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Cost-model ETA failed, falling back: %s", exc)
        return None


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

        active_renderers = _count_active_renderers(tasks)

        # Preferred path: model cost as a curve over the frame range. Only
        # trustworthy once the observations span most of the range, which is
        # what the probe scheduler arranges; otherwise fall through to the
        # throughput estimate below.
        cost_eta = _cost_model_eta(tasks, active_renderers, now_utc)
        if cost_eta is not None:
            return _format_eta_clock(cost_eta)

        metrics = _get_frame_progress_metrics(tasks)
        if metrics is not None:
            total_frames, completed_frames = metrics
            remaining_frames = max(total_frames - completed_frames, 0.0)
            if remaining_frames <= 0:
                return "0:00:00"

            history: deque[tuple[datetime, float, float, int]] | None = None
            if job_id:
                history = _track_eta_history(
                    job_id=job_id,
                    now_utc=now_utc,
                    completed_frames=completed_frames,
                    total_frames=total_frames,
                    active_renderers=active_renderers,
                )

            if active_renderers <= 0:
                return eta_str

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

            bootstrap_efficiency: float | None = None
            if finished_frames > 0 and finished_seconds > 0:
                bootstrap_efficiency = finished_frames / finished_seconds
            elif running_frames > 0 and running_seconds > 0:
                bootstrap_efficiency = running_frames / running_seconds

            predicted_rate: float | None = (
                bootstrap_efficiency * active_renderers
                if bootstrap_efficiency is not None
                else None
            )
            if history is not None:
                short_efficiency = _estimate_efficiency_over_window(
                    history=history,
                    now_utc=now_utc,
                    window_seconds=_ETA_SHORT_WINDOW_SECONDS,
                    min_duration_seconds=90,
                    min_frame_delta=0.75,
                )
                long_efficiency = _estimate_efficiency_over_window(
                    history=history,
                    now_utc=now_utc,
                    window_seconds=_ETA_LONG_WINDOW_SECONDS,
                    min_duration_seconds=5 * 60,
                    min_frame_delta=2.5,
                )
                if short_efficiency is not None and long_efficiency is not None:
                    blended_efficiency = (0.70 * short_efficiency) + (0.30 * long_efficiency)
                    trend_ratio = short_efficiency / max(long_efficiency, _ETA_MIN_PREDICTED_RATE)
                    trend_boost = _clamp(
                        trend_ratio ** 0.35,
                        _ETA_MIN_TREND_BOOST,
                        _ETA_MAX_TREND_BOOST,
                    )
                    predicted_rate = blended_efficiency * trend_boost * active_renderers
                elif short_efficiency is not None:
                    predicted_rate = short_efficiency * active_renderers
                elif long_efficiency is not None:
                    predicted_rate = long_efficiency * active_renderers

            if predicted_rate is not None and predicted_rate > _ETA_MIN_PREDICTED_RATE:
                total_eta_seconds = remaining_frames / predicted_rate
                if total_eta_seconds > 0:
                    if job_id:
                        total_eta_seconds = _smooth_eta_seconds(
                            job_id=job_id,
                            now_utc=now_utc,
                            eta_seconds=total_eta_seconds,
                            active_renderers=active_renderers,
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

        if durations and active_renderers > 0:
            avg_duration = sum(durations) / len(durations)
            remaining_task_equivalents = 0.0
            for task in tasks:
                stat = task.get("Stat", 1)
                if stat == 5:
                    continue
                if stat == 4:
                    prog_ratio = _parse_progress_ratio(task.get("Prog"))
                    remaining_task_equivalents += max(1.0 - (prog_ratio or 0.0), 0.0)
                    continue
                remaining_task_equivalents += 1.0

            if remaining_task_equivalents <= 0:
                remaining_task_equivalents = max(total_tasks - completed_chunks, 0)

            total_eta_seconds = (avg_duration * remaining_task_equivalents) / active_renderers
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
    if stat == 4:
        # A failed job takes its own command - Requeue and Suspend answer
        # Success on it but leave it Failed, so they are not offered here.
        buttons.append(
            InlineKeyboardButton(
                text="▶️ Resume failed",
                callback_data=f"resume_failed_job:{job_id}",
            )
        )
    elif stat != 3:
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
                close_inline_button(callback_data="job_close"),
                inline_button(
                    text="🔄 Update",
                    callback_data=f"job_update:{job_id}",
                    style="primary",
                ),
            ]
        )
    else:
        inline_keyboard.append(
            [close_inline_button(callback_data="job_close")]
        )

    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


async def _edit_or_send_message(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    parse_mode: str | None = "HTML",
) -> str:
    try:
        await message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return "edited"
    except TelegramBadRequest as exc:
        error_text = str(exc).lower()
        if "message is not modified" in error_text:
            return "not_modified"
        if (
            "message can't be edited" in error_text
            or "message to edit not found" in error_text
            or "message is too old" in error_text
        ):
            await message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
            return "sent_new"
        raise


async def _edit_or_send_job_info(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    await _edit_or_send_message(
        message,
        text,
        reply_markup,
        parse_mode="HTML",
    )


async def _render_jobs_overview_message(
    message: Message,
    user_id: int,
    page: int,
    *,
    force_refresh: bool = False,
) -> bool:
    flow_started_at = time.monotonic()
    cache_state = "miss"
    fetch_ms = 0
    sort_ms = 0

    cached_entry = None if force_refresh else _get_cached_jobs_overview(user_id)
    if cached_entry is not None:
        combined_jobs, needs_refresh = cached_entry
        cache_state = "stale_hit" if needs_refresh else "hit"
        if needs_refresh:
            _ensure_jobs_overview_refresh(user_id)
    else:
        fetch_started_at = time.monotonic()
        jobs = await get_jobs_list(user_id)
        fetch_ms = int((time.monotonic() - fetch_started_at) * 1000)
        if not jobs:
            telegram_started_at = time.monotonic()
            action = await _edit_or_send_message(message, "No jobs found.", parse_mode=None)
            telegram_ms = int((time.monotonic() - telegram_started_at) * 1000)
            total_ms = int((time.monotonic() - flow_started_at) * 1000)
            logger.info(
                "Jobs overview timings for user %s page %s: cache=%s fetch=%sms telegram=%sms total=%sms action=%s empty=1",
                user_id,
                page,
                "force_refresh" if force_refresh else cache_state,
                fetch_ms,
                telegram_ms,
                total_ms,
                action,
            )
            return False

        sort_started_at = time.monotonic()
        combined_jobs = await group_and_sort_jobs(jobs)
        sort_ms = int((time.monotonic() - sort_started_at) * 1000)
        _store_jobs_overview(user_id, combined_jobs)
        cache_state = "force_refresh" if force_refresh else "miss"

    build_started_at = time.monotonic()
    text, keyboard = _build_jobs_overview(combined_jobs, page)
    build_ms = int((time.monotonic() - build_started_at) * 1000)
    telegram_started_at = time.monotonic()
    action = await _edit_or_send_message(
        message,
        text,
        keyboard,
        parse_mode="HTML",
    )
    telegram_ms = int((time.monotonic() - telegram_started_at) * 1000)
    total_ms = int((time.monotonic() - flow_started_at) * 1000)
    logger.info(
        "Jobs overview timings for user %s page %s: cache=%s fetch=%sms sort=%sms build=%sms telegram=%sms total=%sms action=%s jobs=%s",
        user_id,
        page,
        cache_state,
        fetch_ms,
        sort_ms,
        build_ms,
        telegram_ms,
        total_ms,
        action,
        len(combined_jobs),
    )
    return True


async def _render_workers_overview_message(
    message: Message,
    user_id: int,
    page: int,
) -> bool:
    workers = await get_workers_list(user_id)
    if not workers:
        await _edit_or_send_message(message, "No workers found.", parse_mode=None)
        return False

    text, keyboard = _build_workers_overview(workers, page=page)
    await _edit_or_send_message(
        message,
        text,
        keyboard,
        parse_mode="HTML",
    )
    return True


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
            if take_credentials_rejected_notice(message.from_user.id):
                # Deadline refused the credentials a moment ago and the user
                # was told so. Three guesses about why the list is empty, on
                # top of an answer they already have, only make them doubt it.
                return
            await message.answer(
                "No jobs found. This could mean:\n"
                "• There are no active jobs in Deadline\n"
                "• Your user doesn't have access to jobs\n"
                "• There was an API error (check logs)"
            )
            return

        combined_jobs = await group_and_sort_jobs(jobs)
        _store_jobs_overview(message.from_user.id, combined_jobs)
        text, keyboard = _build_jobs_overview(combined_jobs, page)
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception:
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
        await _render_jobs_overview_message(
            callback_query.message,
            callback_query.from_user.id,
            page,
        )

    except Exception:
        logger.exception(
            "Error handling jobs page for user %s", callback_query.from_user.id
        )
        await _edit_or_send_message(
            callback_query.message,
            "Error occurred while fetching jobs.",
            parse_mode=None,
        )


@router.callback_query(lambda c: c.data == "jobs_back")
async def jobs_back_callback(callback_query: CallbackQuery) -> None:
    """Return from job info view back to the first jobs page.

    No current keyboard emits this any more - the job card closes instead - but
    job cards already sitting in chats still carry the old Back button, and a
    button that does nothing is worse than one that still works.
    """
    await callback_query.answer()

    if callback_query.from_user is None or callback_query.message is None:
        return

    try:
        await _render_jobs_overview_message(
            callback_query.message,
            callback_query.from_user.id,
            0,
        )

    except Exception:
        logger.exception(
            "Error handling jobs back for user %s", callback_query.from_user.id
        )
        await _edit_or_send_message(
            callback_query.message,
            "Error occurred while fetching jobs.",
            parse_mode=None,
        )


@router.callback_query(lambda c: c.data == "job_close")
async def job_close_callback(callback_query: CallbackQuery) -> None:
    """Dismiss a job card, removing it from the chat."""
    await close_menu(callback_query, "Job info closed.")


@router.callback_query(lambda c: c.data == "jobs_close")
async def jobs_close_callback(callback_query: CallbackQuery) -> None:
    """Dismiss the jobs list, removing it from the chat."""
    await close_menu(callback_query, "Jobs closed.")


@router.callback_query(lambda c: c.data == "workers_close")
async def workers_close_callback(callback_query: CallbackQuery) -> None:
    """Dismiss the workers list, removing it from the chat."""
    await close_menu(callback_query, "Workers closed.")


@router.callback_query(lambda c: c.data and c.data.startswith("jobs_update:"))
async def jobs_update_callback(callback_query: CallbackQuery) -> None:
    """Refresh jobs list on the current page."""
    if callback_query.from_user is None or callback_query.message is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    page_str = callback_query.data.split(":", 1)[1]
    try:
        page = int(page_str)
    except ValueError:
        await callback_query.answer("Invalid page number.", show_alert=True)
        return

    try:
        await _render_jobs_overview_message(
            callback_query.message,
            callback_query.from_user.id,
            page,
            force_refresh=True,
        )
        await callback_query.answer("Updated")
    except Exception:
        logger.exception(
            "Error updating jobs list for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while updating jobs.", show_alert=True)


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
            errors_count = _extract_job_errors(job)

            eta_str = "N/A"
            if stat in {1, 6}:
                eta_str = _calculate_eta(
                    current_job_id,
                    tasks or [],
                    total_tasks,
                    completed_chunks,
                )

            info_text = _build_job_info_text(
                batch_name=batch_name,
                name=name,
                stat=stat,
                stat_name=stat_name,
                progress_str=progress_str,
                errors_count=errors_count,
                eta_str=eta_str,
                render_seconds=_job_render_seconds(job, tasks=tasks),
            )

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

    except Exception:
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
        errors_count = _extract_job_errors(selected_job)

        eta_str = "N/A"
        if stat in {1, 6}:
            eta_str = _calculate_eta(
                job_id,
                tasks or [],
                total_tasks,
                completed_chunks,
            )

        info_text = _build_job_info_text(
            batch_name=batch_name,
            name=name,
            stat=stat,
            stat_name=stat_name,
            progress_str=progress_str,
            errors_count=errors_count,
            eta_str=eta_str,
            render_seconds=_job_render_seconds(selected_job, tasks=tasks),
        )

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

    except Exception:
        logger.exception(
            "Error updating job info for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while updating job info.", show_alert=True)


@router.message(F.text == "🖥️ Workers")
@authorized_only
async def handle_workers(message: Message) -> None:
    """Display worker list with pagination and inline actions."""
    if message.from_user is None:
        await message.answer("Error: User information not available.")
        return

    logger.info("Workers button pressed by user %s", message.from_user.id)

    try:
        workers = await get_workers_list(message.from_user.id)
        if not workers:
            if take_credentials_rejected_notice(message.from_user.id):
                return
            await message.answer(
                "No workers (slaves) found. This could mean:\n"
                "• There are no active workers in Deadline\n"
                "• Your user doesn't have access to workers\n"
                "• There was an API error (check logs)"
            )
            return

        text, keyboard = _build_workers_overview(workers, page=0)
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception:
        logger.exception(
            "Error handling workers for user %s", message.from_user.id
        )
        await message.answer("Error occurred while fetching workers.")


@router.callback_query(lambda c: c.data and c.data.startswith("workers_page:"))
async def workers_page_callback(callback_query: CallbackQuery) -> None:
    """Handle pagination for workers list."""
    if callback_query.from_user is None or callback_query.message is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    page_str = callback_query.data.split(":", 1)[1]
    try:
        page = int(page_str)
    except ValueError:
        await callback_query.answer("Invalid page number.", show_alert=True)
        return

    try:
        await _render_workers_overview_message(
            callback_query.message,
            callback_query.from_user.id,
            page,
        )
        await callback_query.answer()
    except Exception:
        logger.exception(
            "Error handling workers page for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while fetching workers.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("workers_back:"))
async def workers_back_callback(callback_query: CallbackQuery) -> None:
    """Return from worker details to workers list."""
    if callback_query.from_user is None or callback_query.message is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    page_str = callback_query.data.split(":", 1)[1]
    try:
        page = int(page_str)
    except ValueError:
        page = 0

    try:
        await _render_workers_overview_message(
            callback_query.message,
            callback_query.from_user.id,
            page,
        )
        await callback_query.answer()
    except Exception:
        logger.exception(
            "Error handling workers back for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while fetching workers.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("workers_update:"))
async def workers_update_callback(callback_query: CallbackQuery) -> None:
    """Refresh workers list on the current page."""
    if callback_query.from_user is None or callback_query.message is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    page_str = callback_query.data.split(":", 1)[1]
    try:
        page = int(page_str)
    except ValueError:
        await callback_query.answer("Invalid page number.", show_alert=True)
        return

    try:
        await _render_workers_overview_message(
            callback_query.message,
            callback_query.from_user.id,
            page,
        )
        await callback_query.answer("Updated")
    except Exception:
        logger.exception(
            "Error updating workers list for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while updating workers.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("worker_info:"))
async def worker_info_callback(callback_query: CallbackQuery) -> None:
    """Show detailed information for selected worker."""
    if callback_query.from_user is None or callback_query.message is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    parts = callback_query.data.split(":", 2)
    if len(parts) != 3:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    try:
        worker_index = int(parts[1])
        page = int(parts[2])
    except ValueError:
        await callback_query.answer("Invalid worker selection.", show_alert=True)
        return

    try:
        await _render_worker_details(callback_query, worker_index, page)
    except Exception:
        logger.exception(
            "Error handling worker info for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while fetching worker info.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("worker_update:"))
async def worker_update_callback(callback_query: CallbackQuery) -> None:
    """Refresh selected worker details."""
    if callback_query.from_user is None or callback_query.message is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    parts = callback_query.data.split(":", 2)
    if len(parts) != 3:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    try:
        worker_index = int(parts[1])
        page = int(parts[2])
    except ValueError:
        await callback_query.answer("Invalid worker selection.", show_alert=True)
        return

    try:
        await _render_worker_details(
            callback_query,
            worker_index,
            page,
            answer_text="Updated",
        )
    except Exception:
        logger.exception(
            "Error updating worker info for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while updating worker info.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("worker_toggle_enable:"))
async def worker_toggle_enable_callback(callback_query: CallbackQuery) -> None:
    """Toggle worker Enable flag via Deadline savesettings."""
    if callback_query.from_user is None or callback_query.message is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    parts = callback_query.data.split(":", 4)
    if len(parts) != 4:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    try:
        worker_index = int(parts[1])
        page = int(parts[2])
        target_enable = bool(int(parts[3]))
    except ValueError:
        await callback_query.answer("Invalid action.", show_alert=True)
        return

    try:
        workers = await get_workers_list(callback_query.from_user.id)
        selected_worker = _resolve_worker_by_index(workers, worker_index)
        if selected_worker is None:
            await callback_query.answer("Worker not found.", show_alert=True)
            return

        worker_name = _worker_name(selected_worker)
        worker_entry = await get_worker_infosettings_by_user_id(
            callback_query.from_user.id,
            worker_name,
        )
        if not worker_entry:
            await callback_query.answer("Worker settings unavailable.", show_alert=True)
            return

        settings_payload = worker_entry.get("Settings", {})
        if not isinstance(settings_payload, dict):
            await callback_query.answer("Worker settings unavailable.", show_alert=True)
            return

        settings_payload = dict(settings_payload)
        settings_payload["Name"] = str(settings_payload.get("Name") or worker_name)
        settings_payload["Enable"] = target_enable

        saved = await save_worker_settings_by_user_id(
            callback_query.from_user.id,
            settings_payload,
        )
        if not saved:
            await callback_query.answer("Failed to update worker status.", show_alert=True)
            return

        toast_text = "Worker enabled." if target_enable else "Worker disabled."
        await _render_worker_details(
            callback_query,
            worker_index,
            page,
            answer_text=toast_text,
        )
    except Exception:
        logger.exception(
            "Error toggling worker enable for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error updating worker status.", show_alert=True)


async def _forget_previewed_runs(job_id: str) -> None:
    """A requeue from chat starts a new render run, same as one from Monitor."""
    from app.services.job_watcher import forget_auto_preview_run

    try:
        await forget_auto_preview_run(job_id)
    except Exception as exc:
        logger.warning(
            "Could not clear preview run records for requeued job %s: %s", job_id, exc
        )


@router.callback_query(lambda c: c.data and c.data.startswith("requeue_job:"))
async def requeue_job_callback(callback_query: CallbackQuery) -> None:
    """Handle requeue job button press."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        # Job cards already sitting in chats still offer Requeue on failed jobs,
        # where Deadline reports Success and changes nothing. Route those to the
        # command that actually works.
        job_info = await get_job_info_by_user_id(callback_query.from_user.id, job_id)
        if isinstance(job_info, dict) and job_info.get("Stat") == 4:
            success = await resume_failed_job_by_user_id(
                callback_query.from_user.id, job_id
            )
            if success:
                await _forget_previewed_runs(job_id)
            await callback_query.answer(
                "Failed tasks requeued!" if success else "Failed to resume job.",
                show_alert=not success,
            )
            return

        success = await requeue_job_by_user_id(callback_query.from_user.id, job_id)
        if success:
            await _forget_previewed_runs(job_id)
            await callback_query.answer("Job requeued successfully!")
        else:
            await callback_query.answer("Failed to requeue job.", show_alert=True)
    except Exception:
        logger.exception(
            "Error requeuing job for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while requeuing job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("resume_failed_job:"))
async def resume_failed_job_callback(callback_query: CallbackQuery) -> None:
    """Put a failed job back to work by requeueing its failed tasks."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        success = await resume_failed_job_by_user_id(callback_query.from_user.id, job_id)
        if success:
            await _forget_previewed_runs(job_id)
            await callback_query.answer("Failed tasks requeued!")
        else:
            await callback_query.answer("Failed to resume job.", show_alert=True)
    except Exception:
        logger.exception(
            "Error resuming failed job for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while resuming job.", show_alert=True)


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
    except Exception:
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
    except Exception:
        logger.exception(
            "Error suspending job for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while suspending job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("delete_job:"))
async def delete_job_callback(callback_query: CallbackQuery) -> None:
    """Ask for confirmation before deleting a job."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    if callback_query.message is None:
        await callback_query.answer("Message not available.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Confirm delete",
                    callback_data=f"delete_job_confirm:{job_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Cancel",
                    callback_data=f"delete_job_cancel:{job_id}",
                )
            ],
        ]
    )

    await callback_query.message.answer(
        f"Delete this job?\n<code>{html.escape(job_id)}</code>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await callback_query.answer("Please confirm deletion.", show_alert=False)


@router.callback_query(lambda c: c.data and c.data.startswith("delete_job_confirm:"))
async def delete_job_confirm_callback(callback_query: CallbackQuery) -> None:
    """Delete a job after explicit confirmation."""
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
    except Exception:
        logger.exception(
            "Error deleting job for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while deleting job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("delete_job_cancel:"))
async def delete_job_cancel_callback(callback_query: CallbackQuery) -> None:
    """Cancel a pending job deletion confirmation."""
    if callback_query.message:
        try:
            await callback_query.message.edit_text("Job deletion cancelled.", reply_markup=None)
        except Exception:
            await callback_query.message.answer("Job deletion cancelled.")
    await callback_query.answer("Deletion cancelled.", show_alert=False)


def _task_icon(stat: int) -> str:
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


def _task_duration(task: dict, now_utc: datetime) -> str:
    """How long the task ran, compact: "3:45", or "1:04:12" past the hour."""
    stat = task.get("Stat", 1)
    start_str = task.get("StartRen")
    if not start_str or str(start_str).startswith("0001-01-01"):
        return ""
    try:
        start_time = datetime.fromisoformat(str(start_str)).astimezone(timezone.utc)
        if stat == 5:
            comp_str = task.get("Comp")
            if not comp_str or str(comp_str).startswith("0001-01-01"):
                return ""
            end = datetime.fromisoformat(str(comp_str)).astimezone(timezone.utc)
        elif stat == 4:
            end = now_utc
        else:
            return ""
    except Exception:  # pragma: no cover - defensive
        return ""

    total = int((end - start_time).total_seconds())
    if total < 0:
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _build_tasks_view(
    tasks: list[dict],
    job_id: str,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Task breakdown, sized to fit a phone and split into pages.

    Columns are measured from the data rather than fixed: the old layout was 43
    characters wide, which wrapped by a single character on mobile. Paging keeps
    the message well inside Telegram's 4096-character limit even for a job
    chunked one frame at a time.
    """
    now_utc = datetime.now(timezone.utc)
    total_pages = max(1, (len(tasks) + TASKS_PAGE_SIZE - 1) // TASKS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_tasks = tasks[page * TASKS_PAGE_SIZE : (page + 1) * TASKS_PAGE_SIZE]

    rows: list[tuple[str, str, str, str]] = []
    for task in page_tasks:
        frames = str(task.get("Frames") or "")
        progress = re.sub(r"\s+", "", str(task.get("Prog") or "")) or "-"
        rows.append(
            (
                _task_icon(task.get("Stat", 1)),
                frames,
                progress,
                _task_duration(task, now_utc),
            )
        )

    frame_width = max([len("Frames")] + [len(r[1]) for r in rows])
    prog_width = max([len("Prog")] + [len(r[2]) for r in rows])
    time_width = max([len("Time")] + [len(r[3]) for r in rows])

    # Rows are prefixed with a status icon, so the header shifts by that much
    # to keep the columns lined up underneath it.
    label_width = frame_width + 2
    total_width = label_width + prog_width + time_width + 4
    lines = [
        f"{'Frames':<{label_width}}  {'Prog':>{prog_width}}  {'Time':>{time_width}}".rstrip(),
        "-" * total_width,
    ]
    for icon, frames, progress, duration in rows:
        lines.append(
            (
                f"{icon} {_escape_pre(frames):<{frame_width}}  "
                f"{_escape_pre(progress):>{prog_width}}  "
                f"{_escape_pre(duration):>{time_width}}"
            ).rstrip()
        )

    text = "<pre>" + "\n".join(lines) + "</pre>"
    if total_pages > 1:
        text += f"\nPage {page+1} of {total_pages}"

    keyboard: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(back_inline_button(callback_data=f"tasks_page:{job_id}:{page-1}"))
    if page + 1 < total_pages:
        nav.append(
            back_inline_button(
                callback_data=f"tasks_page:{job_id}:{page+1}",
                text="Next ➡️",
            )
        )
    if nav:
        keyboard.append(nav)
    keyboard.append(
        [
            close_inline_button(callback_data="tasks_close"),
            inline_button(
                text="🔄 Update",
                callback_data=f"tasks_page:{job_id}:{page}",
                style="primary",
            ),
        ]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


def _parse_tasks_callback(data: str) -> tuple[str, int]:
    """"tasks_job:<id>" and "tasks_page:<id>:<page>" both land here."""
    parts = data.split(":")
    job_id = parts[1] if len(parts) > 1 else ""
    page = 0
    if len(parts) > 2:
        try:
            page = max(0, int(parts[2]))
        except ValueError:
            page = 0
    return job_id, page


@router.callback_query(lambda c: c.data == "tasks_close")
async def tasks_close_callback(callback_query: CallbackQuery) -> None:
    """Dismiss the task list, removing it from the chat."""
    await close_menu(callback_query, "Tasks closed.")


@router.callback_query(lambda c: c.data and c.data.startswith("tasks_page:"))
async def tasks_page_callback(callback_query: CallbackQuery) -> None:
    """Paging and refresh both re-render in place."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id, page = _parse_tasks_callback(callback_query.data)
    try:
        tasks = await get_job_tasks_by_user_id(callback_query.from_user.id, job_id)
        if not tasks:
            await callback_query.answer("No tasks found for this job.", show_alert=True)
            return
        text, keyboard = _build_tasks_view(tasks, job_id, page)
        if callback_query.message:
            try:
                await callback_query.message.edit_text(
                    text, parse_mode="HTML", reply_markup=keyboard
                )
            except Exception as edit_error:
                # Telegram rejects an edit that changes nothing.
                logger.debug("Tasks view unchanged for %s: %s", job_id, edit_error)
        await callback_query.answer()
    except Exception:
        logger.exception("Error paging tasks for job %s", job_id)
        await callback_query.answer("Error occurred while fetching tasks.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("tasks_job:"))
async def tasks_job_callback(callback_query: CallbackQuery) -> None:
    """Show task breakdown for a job."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id, page = _parse_tasks_callback(callback_query.data)

    try:
        tasks = await get_job_tasks_by_user_id(callback_query.from_user.id, job_id)
        if not tasks:
            if callback_query.message:
                await callback_query.message.answer("No tasks found for this job.")
            await callback_query.answer()
            return

        text, keyboard = _build_tasks_view(tasks, job_id, page)
        if callback_query.message:
            await callback_query.message.answer(
                text, parse_mode="HTML", reply_markup=keyboard
            )
        await callback_query.answer()

    except Exception:
        logger.exception(
            "Error getting tasks for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while fetching tasks.", show_alert=True)
