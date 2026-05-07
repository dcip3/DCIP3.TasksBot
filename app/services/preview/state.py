"""Runtime state for preview workflows."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PreviewRuntimeState:
    download_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    stop_downloads: dict[str, asyncio.Event] = field(default_factory=dict)
    message_registry: dict[str, tuple[int, int]] = field(default_factory=dict)
    animation_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    upload_wait_notice_jobs: set[str] = field(default_factory=set)

    def get_or_create_stop_event(self, job_id: str) -> asyncio.Event:
        stop_event = self.stop_downloads.get(job_id)
        if stop_event is None:
            stop_event = asyncio.Event()
            self.stop_downloads[job_id] = stop_event
        return stop_event

    def clear_download(self, job_id: str) -> None:
        self.download_states.pop(job_id, None)
        self.stop_downloads.pop(job_id, None)


preview_state = PreviewRuntimeState()

