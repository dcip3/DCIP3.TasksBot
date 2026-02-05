"""Backward-compatible utility facade.

This module now re-exports focused helpers from dedicated modules while preserving
existing import paths used across the project.
"""

from __future__ import annotations

from app.core.lifecycle import on_shutdown, on_startup, scheduler
from app.core.maintenance import (
    cleanup_old_files,
    cleanup_temp_and_conv,
    clear_folder,
    ensure_temp_dir,
    force_cleanup_temp_and_conv,
    get_directory_sizes,
    get_video_duration,
    has_enough_space,
    log_directory_sizes,
    make_progress_bar,
)
from app.core.ui_helpers import authorized_only, get_main_keyboard
from app.services.preview.runtime import (
    _notify_preview_job_completion,
    _notify_preview_job_failure,
    _run_auto_preview_for_job,
    pop_preview_message,
    preview_message_registry,
    register_preview_message,
)


def format_progress(completed: int, total: int) -> str:
    """Format progress as percentage string (e.g. "75% 15/20")."""
    percentage = int((completed / total) * 100) if total else 0
    return f"{percentage}% {completed}/{total}"


def get_task_icon(stat: int) -> str:
    """Get icon for task status."""
    if stat == 0:
        return "⏳"
    if stat == 1:
        return "🔄"
    if stat == 2:
        return "⏸️"
    if stat == 3:
        return "✅"
    if stat == 4:
        return "❌"
    return "❓"


def get_job_icon(stat: int) -> str:
    """Get icon for job status."""
    if stat == 0:
        return "❓"
    if stat == 1:
        return "🔄"
    if stat == 2:
        return "⏸️"
    if stat == 3:
        return "✅"
    if stat == 4:
        return "❌"
    if stat == 6:
        return "⏳"
    return "❓"


def get_worker_icon(stat: int) -> str:
    """Get icon for worker status."""
    if stat == 0:
        return "❓"
    if stat == 1:
        return "🔄"
    if stat == 2:
        return "💤"
    if stat == 3:
        return "🔴"
    if stat == 4:
        return "⚠️"
    if stat == 8:
        return "🚀"
    return "❓"


async def job_progress_watcher(bot) -> None:
    """Backward-compatible wrapper around the dedicated watcher service."""
    from app.services.job_watcher import job_progress_watcher as _service_job_progress_watcher

    await _service_job_progress_watcher(bot)


__all__ = [
    "authorized_only",
    "cleanup_old_files",
    "cleanup_temp_and_conv",
    "clear_folder",
    "ensure_temp_dir",
    "force_cleanup_temp_and_conv",
    "format_progress",
    "get_directory_sizes",
    "get_job_icon",
    "get_main_keyboard",
    "get_task_icon",
    "get_video_duration",
    "get_worker_icon",
    "has_enough_space",
    "job_progress_watcher",
    "log_directory_sizes",
    "make_progress_bar",
    "on_shutdown",
    "on_startup",
    "pop_preview_message",
    "preview_message_registry",
    "register_preview_message",
    "scheduler",
    "_notify_preview_job_completion",
    "_notify_preview_job_failure",
    "_run_auto_preview_for_job",
]
