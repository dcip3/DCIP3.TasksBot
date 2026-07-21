"""Push notifications from the Deadline farm (event plugin -> bot).

The TasksBot Deadline event plugin POSTs job state transitions here. Instead of
re-implementing reaction logic, the handler invalidates the jobs cache and wakes
the job watcher immediately, so all existing scans (auto preview, presubmit,
preview completion, error reports) run right away instead of on the next poll
tick. Polling stays in place as a fallback for lost events.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from aiohttp import web

from app.core.config import settings

logger = logging.getLogger(__name__)

EVENT_PATH = "/deadline-event"

_watcher_wake = asyncio.Event()
_last_event: Optional[Dict[str, Any]] = None


def request_watcher_wakeup(reason: str) -> None:
    """Wake the job watcher loop immediately."""
    logger.debug("Watcher wakeup requested: %s", reason)
    _watcher_wake.set()


async def wait_for_wake(timeout: float) -> bool:
    """Sleep until a farm event arrives or the timeout elapses.

    Returns True when woken by an event, False on timeout.
    """
    try:
        await asyncio.wait_for(_watcher_wake.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return False
    _watcher_wake.clear()
    return True


async def handle_deadline_event(request: web.Request) -> web.Response:
    secret = (settings.deadline_event_secret or "").strip()
    if not secret:
        return web.Response(status=503, text="Farm events are not configured")

    provided = (request.headers.get("X-Deadline-Event-Secret") or "").strip()
    if provided != secret:
        return web.Response(status=403, text="Invalid secret")

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    event = str(payload.get("event") or "unknown")
    job_id = str(payload.get("job_id") or "")
    job_name = str(payload.get("job_name") or "")
    logger.info("Farm event: %s job=%s (%s)", event, job_id, job_name)

    global _last_event
    _last_event = payload

    try:
        from app.services.deadline import invalidate_all_jobs_cache

        invalidate_all_jobs_cache()
    except Exception as exc:
        logger.warning("Could not invalidate jobs cache on farm event: %s", exc)

    request_watcher_wakeup(f"{event} {job_id}")
    return web.Response(status=200, text="ok")
