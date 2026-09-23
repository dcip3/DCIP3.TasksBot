"""Runtime state for preview workflows."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class PreviewRuntimeState:
    message_registry: dict[str, tuple[int, int]] = field(default_factory=dict)
    animation_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    upload_wait_notice_jobs: set[str] = field(default_factory=set)
    # Preview jobs the watcher follows without a progress message in chat
    # (auto previews): preview_job_id -> (telegram_user_id, source_job_id).
    tracked_previews: dict[str, tuple[int, str | None]] = field(default_factory=dict)
    # Consecutive lookups where a followed preview job was not found in Deadline.
    missing_preview_strikes: dict[str, int] = field(default_factory=dict)


preview_state = PreviewRuntimeState()
