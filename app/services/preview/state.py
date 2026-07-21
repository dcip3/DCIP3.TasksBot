"""Runtime state for preview workflows."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class PreviewRuntimeState:
    message_registry: dict[str, tuple[int, int]] = field(default_factory=dict)
    animation_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    upload_wait_notice_jobs: set[str] = field(default_factory=set)


preview_state = PreviewRuntimeState()

