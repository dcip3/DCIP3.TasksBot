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

    if event == "job_finished" and job_id:
        # Do not make the farm wait for the HTTP response.
        asyncio.create_task(release_previews_waiting_on(job_id))

    if event == "job_requeued" and job_id:
        # The render is going to produce new frames, so whatever was previewed
        # before is out of date. Forget the run records and the scan woken
        # below queues a preview for the new run.
        asyncio.create_task(forget_previewed_runs_of(job_id))

    request_watcher_wakeup(f"{event} {job_id}")
    return web.Response(status=200, text="ok")


async def forget_previewed_runs_of(source_job_id: str) -> None:
    """Let a requeued render be previewed again.

    Auto previews are deduplicated per render run, and a requeue starts a new
    one. The watcher notices a requeue by itself on its next pass; doing it
    here as well only makes it immediate.
    """
    from app.services.job_watcher import forget_auto_preview_run

    try:
        owners = await forget_auto_preview_run(source_job_id)
    except Exception as exc:
        logger.warning(
            "Could not clear preview run records for requeued job %s: %s",
            source_job_id,
            exc,
        )
        return
    if owners:
        logger.info(
            "Job %s was requeued; %s watcher(s) may get a new preview",
            source_job_id,
            owners,
        )


async def release_previews_waiting_on(source_job_id: str) -> int:
    """Release preview jobs held Pending by the render that just finished.

    The worker asks for its next task within seconds of finishing, so a preview
    still sitting in Pending loses the machine to an unrelated job. The farm
    event plugin does this locally too; doing it here as well keeps the timing
    right even if the plugin on the farm is outdated or was not reloaded.
    """
    from app.services.deadline import release_pending_job_by_user_id
    from app.services.preview.runtime import preview_tracked_jobs

    waiting = [
        (preview_id, owner_id)
        for preview_id, (owner_id, tracked_source) in preview_tracked_jobs.items()
        if tracked_source == source_job_id
    ]
    released = 0
    for preview_id, owner_id in waiting:
        try:
            if await release_pending_job_by_user_id(owner_id, preview_id):
                released += 1
                logger.info(
                    "Released preview %s waiting on finished render %s",
                    preview_id,
                    source_job_id,
                )
        except Exception as exc:
            logger.warning("Could not release preview %s: %s", preview_id, exc)
    return released
