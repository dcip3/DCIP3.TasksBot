import asyncio
import html
import logging
import re
from collections import deque
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.job_helpers import (
    BATCH_COLUMN_WIDTH,
    JOBS_PAGE_SIZE,
    format_progress_old,
    group_and_sort_jobs,
    truncate_cell,
)
from app.core.config import settings
from app.core.ui_helpers import authorized_only, back_inline_button, cancel_inline_button, inline_button
from app.services.deadline import (
    add_pools_by_user_id,
    add_pools_to_workers_by_user_id,
    delete_job_by_user_id,
    delete_pools_by_user_id,
    delete_pools_from_workers_by_user_id,
    get_job_tasks_by_user_id,
    get_jobs_list,
    get_pool_names_by_user_id,
    get_worker_infosettings_by_user_id,
    get_workers_for_pool_by_user_id,
    get_workers_list,
    requeue_job_by_user_id,
    resume_job_by_user_id,
    save_worker_settings_by_user_id,
    suspend_job_by_user_id,
)
from app.storage.pool_profiles import (
    delete_pool_profile,
    get_pool_profile,
    rename_pool_profile,
    set_pool_profile,
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
_WORKERS_PAGE_SIZE = 8
_WORKER_NAME_COLUMN_WIDTH = BATCH_COLUMN_WIDTH - 2
_POOLS_PAGE_SIZE = 8
_POOL_NAME_COLUMN_WIDTH = BATCH_COLUMN_WIDTH
_POOL_WORKERS_COLUMN_WIDTH = 7
_DISK_INPUT_RE = re.compile(r"^\s*([A-Za-z])(?:\s*:[\\/]?)?\s*$")
_DRIVE_LETTER_RE = re.compile(r"([A-Za-z]):[\\/]")
_POOL_NAME_MAX_LENGTH = 64


class PoolCreateStates(StatesGroup):
    POOL_NAME = State()
    CREATE_DISK_LETTER = State()
    CREATE_MANUAL_SELECT = State()
    RENAME_POOL = State()
    EDIT_DISK_LETTER = State()
    EDIT_MANUAL_SELECT = State()


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
    total_str = (
        str(int(round(total_frames)))
        if abs(total_frames - round(total_frames)) < 0.05
        else f"{total_frames:.1f}"
    )
    return f"{percent}% {done_str}/{total_str}"

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

        keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
        page_info = f"Page {page+1} of {total_pages}"
        text = f"<pre>{header}\n{batch_text}\n{page_info}</pre>\n\nSelect a job for details:"
        return text, keyboard

    return f"<pre>{header}\n{batch_text}</pre>", None


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

    header = f"{'':2}{'Name':<{_WORKER_NAME_COLUMN_WIDTH}}   Status"
    header += f"\n{'-'*40}"
    body = "\n".join(lines) if lines else "No data"
    page_info = f"Page {page+1} of {total_pages}"
    text = f"<pre>{header}\n{body}\n{page_info}</pre>\n\nSelect a worker for details:"
    return text, InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


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


def _sorted_pool_names(pools: list[str]) -> list[str]:
    return sorted(
        (
            str(pool).strip()
            for pool in pools
            if str(pool).strip() and str(pool).strip().lower() != "none"
        ),
        key=str.lower,
    )


def _display_pool_name(pool_name: str) -> str:
    """Normalize pool label for UI while keeping original value for API calls."""
    normalized = str(pool_name).strip()
    if len(normalized) == 1 and normalized.isalpha():
        return normalized.upper()
    return normalized


def _normalize_pools_page(page: int, total_items: int) -> tuple[int, int]:
    total_pages = max(1, (total_items + _POOLS_PAGE_SIZE - 1) // _POOLS_PAGE_SIZE)
    normalized_page = max(0, min(page, total_pages - 1))
    return normalized_page, total_pages


def _pool_names_for_page(pools: list[str], page: int) -> tuple[list[str], int, int]:
    sorted_pools = _sorted_pool_names(pools)
    normalized_page, total_pages = _normalize_pools_page(page, len(sorted_pools))
    start = normalized_page * _POOLS_PAGE_SIZE
    stop = start + _POOLS_PAGE_SIZE
    return sorted_pools[start:stop], normalized_page, total_pages


def _build_pools_overview(
    pools: list[str],
    page: int,
    pool_worker_counts: dict[str, int] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    sorted_pools = _sorted_pool_names(pools)
    pools_slice, page, total_pages = _pool_names_for_page(pools, page)
    start = page * _POOLS_PAGE_SIZE

    lines: list[str] = []
    pool_buttons: list[InlineKeyboardButton] = []
    for idx, pool_name in enumerate(pools_slice, start=start):
        display_name = truncate_cell(_display_pool_name(pool_name), _POOL_NAME_COLUMN_WIDTH)
        workers_count = (
            "-"
            if pool_worker_counts is None or pool_name not in pool_worker_counts
            else str(pool_worker_counts[pool_name])
        )
        lines.append(
            f"{_escape_pre(display_name):<{_POOL_NAME_COLUMN_WIDTH}} {workers_count:^{_POOL_WORKERS_COLUMN_WIDTH}}"
        )
        pool_buttons.append(
            InlineKeyboardButton(
                text=display_name,
                callback_data=f"pool_info:{idx}:{page}",
            )
        )

    inline_keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for idx, button in enumerate(pool_buttons, start=1):
        row.append(button)
        if idx % 2 == 0:
            inline_keyboard.append(row)
            row = []
    if row:
        inline_keyboard.append(row)

    inline_keyboard.append(
        [
            inline_button(
                text="➕ Create Pool",
                callback_data=f"pool_create_start:{page}",
                style="primary",
            )
        ]
    )

    nav_buttons: list[InlineKeyboardButton] = []
    if page > 0:
        nav_buttons.append(back_inline_button(callback_data=f"pools_page:{page-1}"))
    if (page + 1) < total_pages:
        nav_buttons.append(
            back_inline_button(
                callback_data=f"pools_page:{page+1}",
                text="Next ➡️",
            )
        )
    if nav_buttons:
        inline_keyboard.append(nav_buttons)

    header = f"{'Pool Name':<{_POOL_NAME_COLUMN_WIDTH}} {'Workers':^{_POOL_WORKERS_COLUMN_WIDTH}}"
    header += f"\n{'-'*40}"
    body = "\n".join(lines) if lines else "No pools yet"
    page_info = f"Page {page+1} of {total_pages}"
    text = f"<pre>{header}\n{body}\n{page_info}</pre>\n\nSelect a pool for details:"
    return text, InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def _resolve_pool_by_index(pools: list[str], pool_index: int) -> str | None:
    sorted_pools = _sorted_pool_names(pools)
    if pool_index < 0 or pool_index >= len(sorted_pools):
        return None
    return sorted_pools[pool_index]


def _pool_index_by_name(pools: list[str], pool_name: str) -> int | None:
    sorted_pools = _sorted_pool_names(pools)
    target = str(pool_name or "").strip()
    if not target:
        return None
    for idx, item in enumerate(sorted_pools):
        if item == target:
            return idx
    return None


async def _load_pool_worker_counts(
    telegram_user_id: int,
    pool_names: list[str],
) -> dict[str, int]:
    if not pool_names:
        return {}

    semaphore = asyncio.Semaphore(min(6, max(1, len(pool_names))))
    counts: dict[str, int] = {}

    async def _load_one(pool_name: str) -> None:
        async with semaphore:
            workers = await get_workers_for_pool_by_user_id(telegram_user_id, pool_name)
            counts[pool_name] = len([name for name in workers if str(name).strip()])

    await asyncio.gather(*(_load_one(pool_name) for pool_name in pool_names))
    return counts


def _normalize_worker_name_list(worker_names: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for item in worker_names:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append(name)
    return sorted(normalized, key=str.lower)


def _normalize_pool_mode(raw_mode: str | None) -> str:
    return "disk" if str(raw_mode or "").strip().lower() == "disk" else "manual"


def _normalize_disk_letter_value(raw_letter: str | None) -> str | None:
    if not raw_letter:
        return None
    text = str(raw_letter).strip().upper()
    if len(text) == 1 and text.isalpha():
        return text
    return None


def _infer_disk_letter_for_pool_name(pool_name: str) -> str | None:
    display_pool = _display_pool_name(pool_name)
    if len(display_pool) == 1 and display_pool.isalpha():
        return display_pool.upper()
    return None


async def _get_pool_profile_for_ui(pool_name: str, current_workers: list[str]) -> dict:
    profile = await get_pool_profile(pool_name)
    if profile:
        mode = _normalize_pool_mode(profile.get("mode"))
        disk_letter = _normalize_disk_letter_value(profile.get("disk_letter"))
        manual_workers = _normalize_worker_name_list(profile.get("manual_workers", []))
    else:
        mode = "manual"
        disk_letter = None
        manual_workers = _normalize_worker_name_list(current_workers)

    if mode == "disk" and not disk_letter:
        disk_letter = _infer_disk_letter_for_pool_name(pool_name)

    return {
        "mode": mode,
        "disk_letter": disk_letter,
        "manual_workers": manual_workers,
    }


def _pool_mode_text(mode: str, disk_letter: str | None) -> str:
    if mode == "disk":
        if disk_letter:
            return f"Disk ({disk_letter}:)"
        return "Disk"
    return "Manual"


def _build_pool_info_text(pool_name: str, worker_names: list[str], profile: dict) -> str:
    display_pool_name = _display_pool_name(pool_name)
    sorted_workers = _normalize_worker_name_list(worker_names)
    sample = ", ".join(sorted_workers[:8]) if sorted_workers else "-"
    if len(sorted_workers) > 8:
        sample += f" (+{len(sorted_workers) - 8})"

    mode = _normalize_pool_mode(profile.get("mode"))
    disk_letter = _normalize_disk_letter_value(profile.get("disk_letter"))
    return "\n".join(
        [
            "Pool Info:",
            f"🗂️ Name: <code>{html.escape(display_pool_name)}</code>",
            f"🧭 Mode: <code>{html.escape(_pool_mode_text(mode, disk_letter))}</code>",
            f"🖥️ Workers: <code>{len(sorted_workers)}</code>",
            f"📋 Sample: <code>{html.escape(sample)}</code>",
        ]
    )


def _build_pool_mode_keyboard(pool_index: int, page: int, mode: str) -> list[InlineKeyboardButton]:
    mode_disk_selected = mode == "disk"
    disk_text = "✅ By Disk" if mode_disk_selected else "☐ By Disk"
    manual_text = "✅ Manual" if not mode_disk_selected else "☐ Manual"
    return [
        inline_button(
            text=disk_text,
            callback_data=f"pool_mode:{pool_index}:{page}:disk",
            style="success" if mode_disk_selected else None,
        ),
        inline_button(
            text=manual_text,
            callback_data=f"pool_mode:{pool_index}:{page}:manual",
            style="success" if not mode_disk_selected else None,
        ),
    ]


def _build_pool_info_keyboard(page: int, pool_index: int, profile: dict) -> InlineKeyboardMarkup:
    mode = _normalize_pool_mode(profile.get("mode"))
    disk_letter = _normalize_disk_letter_value(profile.get("disk_letter"))
    if mode == "disk":
        disk_suffix = f" ({disk_letter}:)" if disk_letter else ""
        mode_action_button = InlineKeyboardButton(
            text=f"💽 Change Disk{disk_suffix}",
            callback_data=f"pool_disk_start:{pool_index}:{page}",
        )
    else:
        mode_action_button = InlineKeyboardButton(
            text="👥 Select Workers",
            callback_data=f"pool_manual_start:{pool_index}:{page}",
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Rename",
                    callback_data=f"pool_rename_start:{pool_index}:{page}",
                ),
                InlineKeyboardButton(
                    text="🗑️ Delete Pool",
                    callback_data=f"pool_delete:{pool_index}:{page}",
                ),
            ],
            _build_pool_mode_keyboard(pool_index, page, mode),
            [mode_action_button],
            [
                back_inline_button(callback_data=f"pools_back:{page}"),
                inline_button(
                    text="🔄 Update",
                    callback_data=f"pool_update:{pool_index}:{page}",
                    style="primary",
                ),
            ],
        ]
    )


def _parse_disk_letter(raw_text: str | None) -> str | None:
    if raw_text is None:
        return None
    match = _DISK_INPUT_RE.match(raw_text)
    if not match:
        return None
    return match.group(1).upper()


def _parse_pool_name(raw_text: str | None) -> str | None:
    if raw_text is None:
        return None
    value = str(raw_text).strip()
    if not value:
        return None
    if len(value) > _POOL_NAME_MAX_LENGTH:
        return None
    if value.lower() == "none":
        return None
    return value


def _build_pool_create_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text="💽 By Disk",
                    callback_data="pool_create_mode:disk",
                ),
                inline_button(
                    text="👥 Manual",
                    callback_data="pool_create_mode:manual",
                ),
            ],
            [cancel_inline_button(callback_data="pool_create_cancel")],
        ]
    )


def _workers_for_page(workers: list[str], page: int, page_size: int = 8) -> tuple[list[str], int, int]:
    sorted_workers = _normalize_worker_name_list(workers)
    total_pages = max(1, (len(sorted_workers) + page_size - 1) // page_size)
    normalized_page = max(0, min(page, total_pages - 1))
    start = normalized_page * page_size
    stop = start + page_size
    return sorted_workers[start:stop], normalized_page, total_pages


def _build_pool_manual_selector(
    *,
    pool_name: str,
    all_workers: list[str],
    selected_workers: list[str],
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    selected_set = set(_normalize_worker_name_list(selected_workers))
    sorted_workers = _normalize_worker_name_list(all_workers)
    workers_slice, page, total_pages = _workers_for_page(sorted_workers, page, page_size=8)
    start_index = page * 8

    lines = [
        f"Pool: <code>{html.escape(_display_pool_name(pool_name))}</code>",
        f"Selected workers: <code>{len(selected_set)}</code>",
        f"Page {page+1} of {total_pages}",
    ]
    text = "\n".join(lines)

    inline_keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for idx, worker_name in enumerate(workers_slice, start=start_index):
        selected = worker_name in selected_set
        label_prefix = "✅ " if selected else "☐ "
        row.append(
            InlineKeyboardButton(
                text=f"{label_prefix}{truncate_cell(worker_name, 24)}",
                callback_data=f"pool_manual_toggle:{idx}:{page}",
            )
        )
        if len(row) == 2:
            inline_keyboard.append(row)
            row = []
    if row:
        inline_keyboard.append(row)

    nav_buttons: list[InlineKeyboardButton] = []
    if page > 0:
        nav_buttons.append(back_inline_button(callback_data=f"pool_manual_page:{page-1}"))
    if (page + 1) < total_pages:
        nav_buttons.append(
            back_inline_button(
                callback_data=f"pool_manual_page:{page+1}",
                text="Next ➡️",
            )
        )
    if nav_buttons:
        inline_keyboard.append(nav_buttons)

    inline_keyboard.append(
        [
            inline_button(
                text="Apply",
                callback_data="pool_manual_apply",
                style="primary",
            )
        ]
    )
    inline_keyboard.append([cancel_inline_button(callback_data="pool_manual_cancel")])
    return text, InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def _worker_has_drive_letter(worker: dict, disk_letter: str) -> bool:
    info = worker.get("Info", {})
    disk_str = str(info.get("DiskStr") or "")
    if not disk_str:
        return False
    for found in _DRIVE_LETTER_RE.findall(disk_str):
        if found.upper() == disk_letter:
            return True
    return False


async def _render_pool_details(
    callback_query: CallbackQuery,
    pool_index: int,
    page: int,
    *,
    answer_text: str | None = None,
) -> bool:
    if callback_query.from_user is None or callback_query.message is None:
        return False

    pools = await get_pool_names_by_user_id(callback_query.from_user.id)
    pool_name = _resolve_pool_by_index(pools, pool_index)
    if pool_name is None:
        await callback_query.answer("Pool not found.", show_alert=True)
        return False

    workers = await get_workers_for_pool_by_user_id(callback_query.from_user.id, pool_name)
    profile = await _get_pool_profile_for_ui(pool_name, workers)
    info_text = _build_pool_info_text(pool_name, workers, profile)
    keyboard = _build_pool_info_keyboard(page, pool_index, profile)
    await _edit_or_send_job_info(callback_query.message, info_text, keyboard)
    if answer_text:
        await callback_query.answer(answer_text)
    else:
        await callback_query.answer()
    return True


async def _sync_pool_from_disk(
    telegram_user_id: int,
    pool_name: str,
    disk_letter: str,
) -> tuple[bool, str, str]:
    normalized_pool_name = str(pool_name or "").strip()
    if not normalized_pool_name:
        return False, "", "Pool name is required."

    normalized_disk = disk_letter.upper()
    workers = await get_workers_list(telegram_user_id)
    matched_workers = sorted(
        {
            _worker_name(worker)
            for worker in workers
            if _worker_has_drive_letter(worker, normalized_disk)
        },
        key=str.lower,
    )

    if not matched_workers:
        return False, normalized_pool_name, f"No workers found with disk {normalized_disk}:\\"

    current_workers = await get_workers_for_pool_by_user_id(telegram_user_id, normalized_pool_name)
    current_set = {name for name in current_workers if name}
    target_set = set(matched_workers)

    created = await add_pools_by_user_id(telegram_user_id, [normalized_pool_name])
    if not created:
        return False, normalized_pool_name, f"Failed to create or ensure pool '{normalized_pool_name}'."

    assigned = await add_pools_to_workers_by_user_id(
        telegram_user_id,
        matched_workers,
        [normalized_pool_name],
        overwrite=False,
    )
    if not assigned:
        return False, normalized_pool_name, f"Pool '{normalized_pool_name}' created, but assigning workers failed."

    stale_workers = sorted(current_set - target_set, key=str.lower)
    if stale_workers:
        removed = await delete_pools_from_workers_by_user_id(
            telegram_user_id,
            [normalized_pool_name],
            stale_workers,
        )
        if not removed:
            return (
                False,
                normalized_pool_name,
                f"Pool '{normalized_pool_name}' updated, but failed to remove stale workers.",
            )

    return (
        True,
        normalized_pool_name,
        f"Pool '{normalized_pool_name}' synced from disk {normalized_disk}:\\ ({len(matched_workers)} workers).",
    )


async def _sync_pool_manual_workers(
    telegram_user_id: int,
    pool_name: str,
    selected_workers: list[str],
) -> tuple[bool, str]:
    normalized_pool_name = str(pool_name or "").strip()
    if not normalized_pool_name:
        return False, "Pool name is required."

    target_workers = _normalize_worker_name_list(selected_workers)
    current_workers = _normalize_worker_name_list(
        await get_workers_for_pool_by_user_id(telegram_user_id, normalized_pool_name)
    )

    created = await add_pools_by_user_id(telegram_user_id, [normalized_pool_name])
    if not created:
        return False, f"Failed to create or ensure pool '{normalized_pool_name}'."

    if target_workers:
        assigned = await add_pools_to_workers_by_user_id(
            telegram_user_id,
            target_workers,
            [normalized_pool_name],
            overwrite=False,
        )
        if not assigned:
            return False, f"Pool '{normalized_pool_name}' created, but assigning workers failed."

    stale_workers = sorted(set(current_workers) - set(target_workers), key=str.lower)
    if stale_workers:
        removed = await delete_pools_from_workers_by_user_id(
            telegram_user_id,
            [normalized_pool_name],
            stale_workers,
        )
        if not removed:
            return False, f"Pool '{normalized_pool_name}' updated, but failed to remove stale workers."

    return True, f"Pool '{normalized_pool_name}' synced manually ({len(target_workers)} workers)."


def _pool_cancel_keyboard(callback_data: str = "pool_create_cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[cancel_inline_button(callback_data=callback_data)]])


def _pool_prompt_parse_page(callback_data: str | None, prefix: str) -> int:
    if not callback_data:
        return 0
    raw = callback_data.split(":", 1)
    if len(raw) != 2 or raw[0] != prefix:
        return 0
    try:
        return int(raw[1])
    except ValueError:
        return 0


def _pool_manual_state_context(state_data: dict) -> str:
    context = str(state_data.get("pool_manual_context") or "").strip().lower()
    if context in {"create", "edit"}:
        return context
    return ""


def _pool_manual_payload_from_state(state_data: dict) -> tuple[str, list[str], list[str], int]:
    pool_name = str(state_data.get("pool_name") or "").strip()
    all_workers = _normalize_worker_name_list(state_data.get("manual_all_workers") or [])
    selected_raw = _normalize_worker_name_list(state_data.get("manual_selected_workers") or [])
    all_workers_set = set(all_workers)
    selected_workers = [name for name in selected_raw if name in all_workers_set]
    try:
        page = int(state_data.get("manual_page", 0) or 0)
    except (TypeError, ValueError):
        page = 0
    return pool_name, all_workers, selected_workers, page


async def _render_pools_overview_by_page(
    message: Message,
    telegram_user_id: int,
    page: int,
) -> None:
    pools = await get_pool_names_by_user_id(telegram_user_id)
    pools_slice, normalized_page, _ = _pool_names_for_page(pools, page=page)
    pool_worker_counts = await _load_pool_worker_counts(telegram_user_id, pools_slice)
    text, keyboard = _build_pools_overview(
        pools,
        page=normalized_page,
        pool_worker_counts=pool_worker_counts,
    )
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


async def _render_pool_details_by_name(
    message: Message,
    telegram_user_id: int,
    pool_name: str,
    page: int,
) -> bool:
    normalized_pool_name = str(pool_name or "").strip()
    if not normalized_pool_name:
        return False

    pools = await get_pool_names_by_user_id(telegram_user_id)
    pool_index = _pool_index_by_name(pools, normalized_pool_name)
    if pool_index is None:
        return False

    workers = await get_workers_for_pool_by_user_id(telegram_user_id, normalized_pool_name)
    profile = await _get_pool_profile_for_ui(normalized_pool_name, workers)
    info_text = _build_pool_info_text(normalized_pool_name, workers, profile)
    keyboard = _build_pool_info_keyboard(page, pool_index, profile)
    await message.answer(info_text, parse_mode="HTML", reply_markup=keyboard)
    return True


async def _update_pool_manual_selector_message(
    callback_query: CallbackQuery,
    state: FSMContext,
    *,
    page: int | None = None,
    answer_text: str | None = None,
) -> bool:
    if callback_query.message is None:
        return False

    state_data = await state.get_data()
    pool_name, all_workers, selected_workers, current_page = _pool_manual_payload_from_state(state_data)
    if not pool_name:
        return False
    normalized_page = current_page if page is None else page
    text, keyboard = _build_pool_manual_selector(
        pool_name=pool_name,
        all_workers=all_workers,
        selected_workers=selected_workers,
        page=normalized_page,
    )
    _, normalized_page, _ = _workers_for_page(all_workers, normalized_page, page_size=8)
    await state.update_data(
        manual_page=normalized_page,
        manual_selected_workers=selected_workers,
    )
    await _edit_or_send_job_info(callback_query.message, text, keyboard)
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
                back_inline_button(callback_data="jobs_back"),
                inline_button(
                    text="🔄 Update",
                    callback_data=f"job_update:{job_id}",
                    style="primary",
                ),
            ]
        )
    else:
        inline_keyboard.append(
            [back_inline_button(callback_data="jobs_back")]
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
    """Display worker list with pagination and inline actions."""
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

        text, keyboard = _build_workers_overview(workers, page=0)
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as exc:
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
        workers = await get_workers_list(callback_query.from_user.id)
        if not workers:
            await callback_query.message.edit_text("No workers found.")
            await callback_query.answer()
            return

        text, keyboard = _build_workers_overview(workers, page=page)
        await callback_query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
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
        workers = await get_workers_list(callback_query.from_user.id)
        if not workers:
            await callback_query.message.edit_text("No workers found.")
            await callback_query.answer()
            return
        text, keyboard = _build_workers_overview(workers, page=page)
        await callback_query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await callback_query.answer()
    except Exception:
        logger.exception(
            "Error handling workers back for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while fetching workers.", show_alert=True)


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


@router.message(F.text == "🗂️ Pools")
@authorized_only
async def handle_pools(message: Message) -> None:
    """Display pools list with pagination and create action."""
    if message.from_user is None:
        await message.answer("Error: User information not available.")
        return

    logger.info("Pools button pressed by user %s", message.from_user.id)

    try:
        pools = await get_pool_names_by_user_id(message.from_user.id)
        pools_slice, normalized_page, _ = _pool_names_for_page(pools, page=0)
        pool_worker_counts = await _load_pool_worker_counts(message.from_user.id, pools_slice)
        text, keyboard = _build_pools_overview(
            pools,
            page=normalized_page,
            pool_worker_counts=pool_worker_counts,
        )
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        logger.exception("Error handling pools for user %s", message.from_user.id)
        await message.answer("Error occurred while fetching pools.")


@router.callback_query(lambda c: c.data and c.data.startswith("pools_page:"))
async def pools_page_callback(callback_query: CallbackQuery) -> None:
    """Handle pagination for pools list."""
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
        pools = await get_pool_names_by_user_id(callback_query.from_user.id)
        pools_slice, normalized_page, _ = _pool_names_for_page(pools, page=page)
        pool_worker_counts = await _load_pool_worker_counts(callback_query.from_user.id, pools_slice)
        text, keyboard = _build_pools_overview(
            pools,
            page=normalized_page,
            pool_worker_counts=pool_worker_counts,
        )
        await callback_query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await callback_query.answer()
    except Exception:
        logger.exception(
            "Error handling pools page for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while fetching pools.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("pools_back:"))
async def pools_back_callback(callback_query: CallbackQuery) -> None:
    """Return from pool details to pools list."""
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
        pools = await get_pool_names_by_user_id(callback_query.from_user.id)
        pools_slice, normalized_page, _ = _pool_names_for_page(pools, page=page)
        pool_worker_counts = await _load_pool_worker_counts(callback_query.from_user.id, pools_slice)
        text, keyboard = _build_pools_overview(
            pools,
            page=normalized_page,
            pool_worker_counts=pool_worker_counts,
        )
        await callback_query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await callback_query.answer()
    except Exception:
        logger.exception(
            "Error handling pools back for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while fetching pools.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("pool_info:"))
async def pool_info_callback(callback_query: CallbackQuery) -> None:
    """Show detailed information for selected pool."""
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
        pool_index = int(parts[1])
        page = int(parts[2])
    except ValueError:
        await callback_query.answer("Invalid pool selection.", show_alert=True)
        return

    try:
        await _render_pool_details(callback_query, pool_index, page)
    except Exception:
        logger.exception(
            "Error handling pool info for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while fetching pool info.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("pool_update:"))
async def pool_update_callback(callback_query: CallbackQuery) -> None:
    """Refresh selected pool details."""
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
        pool_index = int(parts[1])
        page = int(parts[2])
    except ValueError:
        await callback_query.answer("Invalid pool selection.", show_alert=True)
        return

    try:
        await _render_pool_details(
            callback_query,
            pool_index,
            page,
            answer_text="Updated",
        )
    except Exception:
        logger.exception(
            "Error updating pool info for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while updating pool info.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("pool_delete:"))
async def pool_delete_callback(callback_query: CallbackQuery) -> None:
    """Delete selected pool."""
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
        pool_index = int(parts[1])
        page = int(parts[2])
    except ValueError:
        await callback_query.answer("Invalid pool selection.", show_alert=True)
        return

    try:
        pools = await get_pool_names_by_user_id(callback_query.from_user.id)
        pool_name = _resolve_pool_by_index(pools, pool_index)
        if pool_name is None:
            await callback_query.answer("Pool not found.", show_alert=True)
            return

        deleted = await delete_pools_by_user_id(callback_query.from_user.id, [pool_name])
        if not deleted:
            await callback_query.answer("Failed to delete pool.", show_alert=True)
            return

        await delete_pool_profile(pool_name)

        pools_after = await get_pool_names_by_user_id(callback_query.from_user.id)
        pools_slice, normalized_page, _ = _pool_names_for_page(pools_after, page=page)
        pool_worker_counts = await _load_pool_worker_counts(callback_query.from_user.id, pools_slice)
        text, keyboard = _build_pools_overview(
            pools_after,
            page=normalized_page,
            pool_worker_counts=pool_worker_counts,
        )
        await callback_query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await callback_query.answer("Pool deleted.")
    except Exception:
        logger.exception(
            "Error deleting pool for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while deleting pool.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("pool_create_start:"))
async def pool_create_start_callback(callback_query: CallbackQuery, state: FSMContext) -> None:
    """Start pool creation workflow by asking for pool name."""
    if callback_query.from_user is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    page = _pool_prompt_parse_page(callback_query.data, "pool_create_start")
    await state.clear()
    await state.set_state(PoolCreateStates.POOL_NAME)
    await state.update_data(pools_page=page)

    if callback_query.message:
        await callback_query.message.answer(
            "Send <b>Pool Name</b> for the new pool.",
            parse_mode="HTML",
            reply_markup=_pool_cancel_keyboard("pool_create_cancel"),
        )
    await callback_query.answer("Waiting for pool name.")


@router.callback_query(lambda c: c.data and c.data.startswith("pool_create_mode:"))
async def pool_create_mode_callback(callback_query: CallbackQuery, state: FSMContext) -> None:
    """Handle mode selection during pool creation."""
    if callback_query.from_user is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    parts = callback_query.data.split(":", 1)
    if len(parts) != 2:
        await callback_query.answer("Invalid mode.", show_alert=True)
        return
    target_mode = _normalize_pool_mode(parts[1])

    state_data = await state.get_data()
    pool_name = str(state_data.get("pool_name") or "").strip()
    if not pool_name:
        await callback_query.answer("Pool name missing. Start again.", show_alert=True)
        return

    if target_mode == "disk":
        await state.set_state(PoolCreateStates.CREATE_DISK_LETTER)
        if callback_query.message:
            await callback_query.message.answer(
                "Send disk letter for this pool (example: <code>Z</code>).",
                parse_mode="HTML",
                reply_markup=_pool_cancel_keyboard("pool_create_cancel"),
            )
        await callback_query.answer("Waiting for disk letter.")
        return

    workers = await get_workers_list(callback_query.from_user.id)
    all_worker_names = _normalize_worker_name_list([_worker_name(worker) for worker in workers])
    selected_workers = _normalize_worker_name_list(
        await get_workers_for_pool_by_user_id(callback_query.from_user.id, pool_name)
    )
    await state.set_state(PoolCreateStates.CREATE_MANUAL_SELECT)
    await state.update_data(
        pool_manual_context="create",
        manual_all_workers=all_worker_names,
        manual_selected_workers=selected_workers,
        manual_page=0,
    )
    if callback_query.message:
        text, keyboard = _build_pool_manual_selector(
            pool_name=pool_name,
            all_workers=all_worker_names,
            selected_workers=selected_workers,
            page=0,
        )
        await _edit_or_send_job_info(callback_query.message, text, keyboard)
    await callback_query.answer()


@router.callback_query(lambda c: c.data == "pool_create_cancel")
async def pool_create_cancel_callback(callback_query: CallbackQuery, state: FSMContext) -> None:
    """Cancel pool creation workflow."""
    await state.clear()
    if callback_query.message:
        try:
            await callback_query.message.edit_text("Pool flow cancelled.")
        except Exception:
            await callback_query.message.answer("Pool flow cancelled.")
    await callback_query.answer("Cancelled.")


@router.callback_query(lambda c: c.data == "pool_edit_cancel")
async def pool_edit_cancel_callback(callback_query: CallbackQuery, state: FSMContext) -> None:
    """Cancel pool edit flow and return to pool details when possible."""
    if callback_query.from_user is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    state_data = await state.get_data()
    page = int(state_data.get("pools_page", 0) or 0)
    pool_index_raw = state_data.get("pool_index")
    pool_name = str(state_data.get("pool_name") or "").strip()
    await state.clear()

    pool_index: int | None
    try:
        pool_index = int(pool_index_raw)
    except (TypeError, ValueError):
        pool_index = None

    if callback_query.message and pool_index is not None:
        rendered = await _render_pool_details(callback_query, pool_index, page, answer_text="Cancelled.")
        if rendered:
            return

    if callback_query.message and pool_name:
        rendered = await _render_pool_details_by_name(
            callback_query.message,
            callback_query.from_user.id,
            pool_name,
            page,
        )
        if rendered:
            await callback_query.answer("Cancelled.")
            return

    if callback_query.message:
        try:
            await callback_query.message.edit_text("Pool edit cancelled.")
        except Exception:
            await callback_query.message.answer("Pool edit cancelled.")
    await callback_query.answer("Cancelled.")


@router.message(StateFilter(PoolCreateStates.POOL_NAME))
async def pool_create_pool_name_message(message: Message, state: FSMContext) -> None:
    """Handle pool name input for pool creation."""
    if message.from_user is None:
        await message.answer("Error: user not found.")
        return

    pool_name = _parse_pool_name(message.text)
    if pool_name is None:
        await message.answer(
            "Invalid pool name. Use 1-64 symbols, and avoid <code>none</code>.",
            parse_mode="HTML",
            reply_markup=_pool_cancel_keyboard("pool_create_cancel"),
        )
        return

    pools = await get_pool_names_by_user_id(message.from_user.id)
    existing_lut = {str(pool).strip().lower() for pool in pools}
    if pool_name.lower() in existing_lut:
        await message.answer(
            "Pool already exists. Open it in Pools list and edit there.",
            reply_markup=_pool_cancel_keyboard("pool_create_cancel"),
        )
        return

    await state.update_data(pool_name=pool_name)
    await message.answer(
        "\n".join(
            [
                f"Pool Name: <code>{html.escape(_display_pool_name(pool_name))}</code>",
                "Choose creation mode:",
            ]
        ),
        parse_mode="HTML",
        reply_markup=_build_pool_create_mode_keyboard(),
    )


@router.message(StateFilter(PoolCreateStates.CREATE_DISK_LETTER))
async def pool_create_disk_letter_message(message: Message, state: FSMContext) -> None:
    """Handle disk letter input for pool creation."""
    if message.from_user is None:
        await message.answer("Error: user not found.")
        return

    disk_letter = _parse_disk_letter(message.text)
    if disk_letter is None:
        await message.answer(
            "Invalid input. Send a single disk letter (example: <code>Z</code>).",
            parse_mode="HTML",
            reply_markup=_pool_cancel_keyboard("pool_create_cancel"),
        )
        return

    data = await state.get_data()
    pool_name = str(data.get("pool_name") or "").strip()
    page = int(data.get("pools_page", 0) or 0)
    if not pool_name:
        await state.clear()
        await message.answer("Pool name missing. Start again from Pools.")
        return

    success, _, status_message = await _sync_pool_from_disk(
        message.from_user.id,
        pool_name,
        disk_letter,
    )
    if not success:
        await message.answer(f"❌ {status_message}")
        return

    current_workers = await get_workers_for_pool_by_user_id(message.from_user.id, pool_name)
    await set_pool_profile(
        pool_name,
        mode="disk",
        disk_letter=disk_letter,
        manual_workers=current_workers,
    )
    await state.clear()
    await message.answer(f"✅ {status_message}")
    rendered = await _render_pool_details_by_name(message, message.from_user.id, pool_name, page)
    if not rendered:
        await _render_pools_overview_by_page(message, message.from_user.id, page)


@router.message(StateFilter(PoolCreateStates.EDIT_DISK_LETTER))
async def pool_edit_disk_letter_message(message: Message, state: FSMContext) -> None:
    """Handle disk letter input for pool edit mode."""
    if message.from_user is None:
        await message.answer("Error: user not found.")
        return

    disk_letter = _parse_disk_letter(message.text)
    if disk_letter is None:
        await message.answer(
            "Invalid input. Send a single disk letter (example: <code>Z</code>).",
            parse_mode="HTML",
            reply_markup=_pool_cancel_keyboard("pool_edit_cancel"),
        )
        return

    state_data = await state.get_data()
    pool_name = str(state_data.get("pool_name") or "").strip()
    page = int(state_data.get("pools_page", 0) or 0)
    if not pool_name:
        await state.clear()
        await message.answer("Pool not found. Open Pools list and try again.")
        return

    success, _, status_message = await _sync_pool_from_disk(
        message.from_user.id,
        pool_name,
        disk_letter,
    )
    if not success:
        await message.answer(f"❌ {status_message}")
        return

    profile = await get_pool_profile(pool_name)
    manual_workers = _normalize_worker_name_list((profile or {}).get("manual_workers", []))
    await set_pool_profile(
        pool_name,
        mode="disk",
        disk_letter=disk_letter,
        manual_workers=manual_workers,
    )
    await state.clear()
    await message.answer(f"✅ {status_message}")
    rendered = await _render_pool_details_by_name(message, message.from_user.id, pool_name, page)
    if not rendered:
        await _render_pools_overview_by_page(message, message.from_user.id, page)


@router.callback_query(lambda c: c.data and c.data.startswith("pool_mode:"))
async def pool_mode_callback(callback_query: CallbackQuery, state: FSMContext) -> None:
    """Switch pool mode between disk and manual."""
    if callback_query.from_user is None or callback_query.message is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    parts = callback_query.data.split(":", 3)
    if len(parts) != 4:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    try:
        pool_index = int(parts[1])
        page = int(parts[2])
    except ValueError:
        await callback_query.answer("Invalid pool selection.", show_alert=True)
        return
    target_mode = _normalize_pool_mode(parts[3])

    pools = await get_pool_names_by_user_id(callback_query.from_user.id)
    pool_name = _resolve_pool_by_index(pools, pool_index)
    if pool_name is None:
        await callback_query.answer("Pool not found.", show_alert=True)
        return

    workers = await get_workers_for_pool_by_user_id(callback_query.from_user.id, pool_name)
    profile = await _get_pool_profile_for_ui(pool_name, workers)
    current_mode = _normalize_pool_mode(profile.get("mode"))
    if current_mode == target_mode:
        await callback_query.answer("Mode already selected.")
        return

    if target_mode == "manual":
        await set_pool_profile(
            pool_name,
            mode="manual",
            disk_letter=None,
            manual_workers=workers,
        )
        await _render_pool_details(
            callback_query,
            pool_index,
            page,
            answer_text="Mode set to Manual.",
        )
        return

    disk_letter = _normalize_disk_letter_value(profile.get("disk_letter")) or _infer_disk_letter_for_pool_name(
        pool_name
    )
    if disk_letter is None:
        await state.set_state(PoolCreateStates.EDIT_DISK_LETTER)
        await state.update_data(
            pool_name=pool_name,
            pool_index=pool_index,
            pools_page=page,
        )
        await callback_query.message.answer(
            f"Send disk letter for pool <code>{html.escape(_display_pool_name(pool_name))}</code>.",
            parse_mode="HTML",
            reply_markup=_pool_cancel_keyboard("pool_edit_cancel"),
        )
        await callback_query.answer("Waiting for disk letter.")
        return

    success, _, status_message = await _sync_pool_from_disk(
        callback_query.from_user.id,
        pool_name,
        disk_letter,
    )
    if not success:
        await callback_query.answer(status_message, show_alert=True)
        return

    await set_pool_profile(
        pool_name,
        mode="disk",
        disk_letter=disk_letter,
        manual_workers=_normalize_worker_name_list(profile.get("manual_workers", [])),
    )
    await _render_pool_details(
        callback_query,
        pool_index,
        page,
        answer_text=status_message,
    )


@router.callback_query(lambda c: c.data and c.data.startswith("pool_disk_start:"))
async def pool_disk_start_callback(callback_query: CallbackQuery, state: FSMContext) -> None:
    """Start disk letter edit flow for a pool."""
    if callback_query.from_user is None:
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
        pool_index = int(parts[1])
        page = int(parts[2])
    except ValueError:
        await callback_query.answer("Invalid pool selection.", show_alert=True)
        return

    pools = await get_pool_names_by_user_id(callback_query.from_user.id)
    pool_name = _resolve_pool_by_index(pools, pool_index)
    if pool_name is None:
        await callback_query.answer("Pool not found.", show_alert=True)
        return

    await state.set_state(PoolCreateStates.EDIT_DISK_LETTER)
    await state.update_data(
        pool_name=pool_name,
        pool_index=pool_index,
        pools_page=page,
    )
    if callback_query.message:
        await callback_query.message.answer(
            f"Send new disk letter for pool <code>{html.escape(_display_pool_name(pool_name))}</code>.",
            parse_mode="HTML",
            reply_markup=_pool_cancel_keyboard("pool_edit_cancel"),
        )
    await callback_query.answer("Waiting for disk letter.")


@router.callback_query(lambda c: c.data and c.data.startswith("pool_manual_start:"))
async def pool_manual_start_callback(callback_query: CallbackQuery, state: FSMContext) -> None:
    """Open manual worker selector for a pool."""
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
        pool_index = int(parts[1])
        page = int(parts[2])
    except ValueError:
        await callback_query.answer("Invalid pool selection.", show_alert=True)
        return

    pools = await get_pool_names_by_user_id(callback_query.from_user.id)
    pool_name = _resolve_pool_by_index(pools, pool_index)
    if pool_name is None:
        await callback_query.answer("Pool not found.", show_alert=True)
        return

    workers = await get_workers_list(callback_query.from_user.id)
    all_worker_names = _normalize_worker_name_list([_worker_name(worker) for worker in workers])
    current_workers = _normalize_worker_name_list(
        await get_workers_for_pool_by_user_id(callback_query.from_user.id, pool_name)
    )
    profile = await _get_pool_profile_for_ui(pool_name, current_workers)
    selected_workers = _normalize_worker_name_list(profile.get("manual_workers") or current_workers)

    await state.set_state(PoolCreateStates.EDIT_MANUAL_SELECT)
    await state.update_data(
        pool_manual_context="edit",
        pool_name=pool_name,
        pool_index=pool_index,
        pools_page=page,
        manual_all_workers=all_worker_names,
        manual_selected_workers=selected_workers,
        manual_page=0,
    )
    text, keyboard = _build_pool_manual_selector(
        pool_name=pool_name,
        all_workers=all_worker_names,
        selected_workers=selected_workers,
        page=0,
    )
    await _edit_or_send_job_info(callback_query.message, text, keyboard)
    await callback_query.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("pool_manual_toggle:"))
async def pool_manual_toggle_callback(callback_query: CallbackQuery, state: FSMContext) -> None:
    """Toggle worker checkbox in manual selector."""
    if callback_query.message is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    state_data = await state.get_data()
    context = _pool_manual_state_context(state_data)
    if not context:
        await callback_query.answer("Manual selector is not active.", show_alert=True)
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

    _, all_workers, selected_workers, _ = _pool_manual_payload_from_state(state_data)
    if worker_index < 0 or worker_index >= len(all_workers):
        await callback_query.answer("Worker not found.", show_alert=True)
        return

    worker_name = all_workers[worker_index]
    selected_set = set(selected_workers)
    if worker_name in selected_set:
        selected_set.remove(worker_name)
    else:
        selected_set.add(worker_name)
    await state.update_data(manual_selected_workers=sorted(selected_set, key=str.lower))
    await _update_pool_manual_selector_message(callback_query, state, page=page)


@router.callback_query(lambda c: c.data and c.data.startswith("pool_manual_page:"))
async def pool_manual_page_callback(callback_query: CallbackQuery, state: FSMContext) -> None:
    """Handle pagination inside manual selector."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    state_data = await state.get_data()
    context = _pool_manual_state_context(state_data)
    if not context:
        await callback_query.answer("Manual selector is not active.", show_alert=True)
        return

    page = _pool_prompt_parse_page(callback_query.data, "pool_manual_page")
    await _update_pool_manual_selector_message(callback_query, state, page=page)


@router.callback_query(lambda c: c.data == "pool_manual_apply")
async def pool_manual_apply_callback(callback_query: CallbackQuery, state: FSMContext) -> None:
    """Apply selected workers for manual mode."""
    if callback_query.from_user is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    state_data = await state.get_data()
    context = _pool_manual_state_context(state_data)
    if not context:
        await callback_query.answer("Manual selector is not active.", show_alert=True)
        return

    pool_name, _all_workers, selected_workers, _ = _pool_manual_payload_from_state(state_data)
    if not pool_name:
        await callback_query.answer("Pool name missing.", show_alert=True)
        return
    page = int(state_data.get("pools_page", 0) or 0)

    success, status_message = await _sync_pool_manual_workers(
        callback_query.from_user.id,
        pool_name,
        selected_workers,
    )
    if not success:
        await callback_query.answer(status_message, show_alert=True)
        return

    await set_pool_profile(
        pool_name,
        mode="manual",
        disk_letter=None,
        manual_workers=selected_workers,
    )
    await state.clear()

    if callback_query.message:
        if context == "edit":
            pools = await get_pool_names_by_user_id(callback_query.from_user.id)
            pool_index = _pool_index_by_name(pools, pool_name)
            if pool_index is not None:
                rendered = await _render_pool_details(
                    callback_query,
                    pool_index,
                    page,
                    answer_text="Applied.",
                )
                if rendered:
                    return
            await _render_pools_overview_by_page(
                callback_query.message,
                callback_query.from_user.id,
                page,
            )
        else:
            await callback_query.message.answer(f"✅ {status_message}")
            rendered = await _render_pool_details_by_name(
                callback_query.message,
                callback_query.from_user.id,
                pool_name,
                page,
            )
            if not rendered:
                await _render_pools_overview_by_page(
                    callback_query.message,
                    callback_query.from_user.id,
                    page,
                )
    await callback_query.answer("Applied.")


@router.callback_query(lambda c: c.data == "pool_manual_cancel")
async def pool_manual_cancel_callback(callback_query: CallbackQuery, state: FSMContext) -> None:
    """Cancel manual selector."""
    if callback_query.from_user is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    state_data = await state.get_data()
    context = _pool_manual_state_context(state_data)
    pool_name = str(state_data.get("pool_name") or "").strip()
    page = int(state_data.get("pools_page", 0) or 0)
    await state.clear()

    if callback_query.message:
        if context == "edit" and pool_name:
            pools = await get_pool_names_by_user_id(callback_query.from_user.id)
            pool_index = _pool_index_by_name(pools, pool_name)
            if pool_index is not None:
                rendered = await _render_pool_details(
                    callback_query,
                    pool_index,
                    page,
                    answer_text="Cancelled.",
                )
                if rendered:
                    return
        try:
            await callback_query.message.edit_text("Selection cancelled.")
        except Exception:
            await callback_query.message.answer("Selection cancelled.")
    await callback_query.answer("Cancelled.")


@router.callback_query(lambda c: c.data and c.data.startswith("pool_rename_start:"))
async def pool_rename_start_callback(callback_query: CallbackQuery, state: FSMContext) -> None:
    """Start pool rename flow."""
    if callback_query.from_user is None:
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
        pool_index = int(parts[1])
        page = int(parts[2])
    except ValueError:
        await callback_query.answer("Invalid pool selection.", show_alert=True)
        return

    pools = await get_pool_names_by_user_id(callback_query.from_user.id)
    pool_name = _resolve_pool_by_index(pools, pool_index)
    if pool_name is None:
        await callback_query.answer("Pool not found.", show_alert=True)
        return

    await state.set_state(PoolCreateStates.RENAME_POOL)
    await state.update_data(
        pool_name=pool_name,
        pool_index=pool_index,
        pools_page=page,
    )
    if callback_query.message:
        await callback_query.message.answer(
            f"Send new name for pool <code>{html.escape(_display_pool_name(pool_name))}</code>.",
            parse_mode="HTML",
            reply_markup=_pool_cancel_keyboard("pool_edit_cancel"),
        )
    await callback_query.answer("Waiting for new name.")


@router.message(StateFilter(PoolCreateStates.RENAME_POOL))
async def pool_rename_message(message: Message, state: FSMContext) -> None:
    """Handle pool rename input."""
    if message.from_user is None:
        await message.answer("Error: user not found.")
        return

    new_pool_name = _parse_pool_name(message.text)
    if new_pool_name is None:
        await message.answer(
            "Invalid pool name. Use 1-64 symbols, and avoid <code>none</code>.",
            parse_mode="HTML",
            reply_markup=_pool_cancel_keyboard("pool_edit_cancel"),
        )
        return

    state_data = await state.get_data()
    old_pool_name = str(state_data.get("pool_name") or "").strip()
    page = int(state_data.get("pools_page", 0) or 0)
    if not old_pool_name:
        await state.clear()
        await message.answer("Pool not found. Open Pools and retry.")
        return

    if new_pool_name.lower() == old_pool_name.lower():
        await message.answer(
            "New name should differ from current one.",
            reply_markup=_pool_cancel_keyboard("pool_edit_cancel"),
        )
        return

    pools = await get_pool_names_by_user_id(message.from_user.id)
    existing_lut = {str(pool).strip().lower() for pool in pools}
    if new_pool_name.lower() in existing_lut:
        await message.answer(
            "Pool with this name already exists.",
            reply_markup=_pool_cancel_keyboard("pool_edit_cancel"),
        )
        return

    old_workers = await get_workers_for_pool_by_user_id(message.from_user.id, old_pool_name)
    created = await add_pools_by_user_id(message.from_user.id, [new_pool_name])
    if not created:
        await message.answer("Failed to create new pool for rename.")
        return

    if old_workers:
        assigned = await add_pools_to_workers_by_user_id(
            message.from_user.id,
            old_workers,
            [new_pool_name],
            overwrite=False,
        )
        if not assigned:
            await message.answer("Failed to move workers to new pool name.")
            return

    deleted = await delete_pools_by_user_id(message.from_user.id, [old_pool_name])
    if not deleted:
        await message.answer(
            "Workers moved, but deleting old pool failed. Please delete it manually."
        )
        return

    old_profile = await get_pool_profile(old_pool_name)
    if old_profile:
        moved = await rename_pool_profile(old_pool_name, new_pool_name)
        if not moved:
            await set_pool_profile(
                new_pool_name,
                mode=_normalize_pool_mode(old_profile.get("mode")),
                disk_letter=_normalize_disk_letter_value(old_profile.get("disk_letter")),
                manual_workers=_normalize_worker_name_list(old_profile.get("manual_workers", [])),
            )
            await delete_pool_profile(old_pool_name)
    else:
        await delete_pool_profile(old_pool_name)

    await state.clear()
    await message.answer(
        f"✅ Pool renamed: <code>{html.escape(_display_pool_name(old_pool_name))}</code> -> "
        f"<code>{html.escape(_display_pool_name(new_pool_name))}</code>",
        parse_mode="HTML",
    )
    rendered = await _render_pool_details_by_name(message, message.from_user.id, new_pool_name, page)
    if not rendered:
        await _render_pools_overview_by_page(message, message.from_user.id, page)


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
