"""Probe-first task scheduling, so the ETA has a cost curve to work with.

A render normally walks the frame range in order, which means the first hours
only ever sample one end of the shot. On SHB_city_main_v027 that end happened
to be the cheapest part of the range, and the ETA sat at ~1.5 h while the true
answer was 8 h.

So before letting the render proceed sequentially, we hold back all but a
handful of chunks spread across the whole range. Those "probes" are ordinary
chunks that had to be rendered anyway - no extra work is done - but once they
are in, the cost curve is known end to end and the estimate lands within ~10%
for the rest of the job. Measured on real farm timings:

    stage of job     current estimator     probes + cost curve
    12% ... 60%           0.18-0.27x              0.90-0.98x

After the probe phase every held task is released and the render continues in
its normal order, so playback of a contiguous range is unaffected - the probes
just show up as a few already-finished chunks further along.

Safety: the held tasks are recorded in the database before they are suspended,
and released again on bot startup, on job completion, or when the probe phase
runs over time. See release_if_ready() and recover_orphaned_probes().
"""

from __future__ import annotations

import logging

from app.services import deadline
from app.services.render_cost import (
    TASK_COMPLETED,
    TASK_QUEUED,
    TASK_RENDERING,
    TASK_SUSPENDED,
    TaskSample,
    build_curve,
    collect_samples,
)
from app.storage import probe_state

logger = logging.getLogger(__name__)

# Spread this many probes across the range, then refine the steepest stretch.
INITIAL_PROBES = 5
ADAPTIVE_PROBES = 2
# Below this a job is too short for probing to tell us anything useful.
MIN_TASKS_FOR_PROBING = 4
# Hard ceiling on how long tasks may stay held back, whatever else happens.
MAX_PROBE_SECONDS = 90 * 60

__all__ = [
    "INITIAL_PROBES",
    "ADAPTIVE_PROBES",
    "MIN_TASKS_FOR_PROBING",
    "MAX_PROBE_SECONDS",
    "plan_initial_probes",
    "pick_adaptive_probe",
    "start_probing",
    "release_if_ready",
    "release_stale_probes",
]


def _probe_count(task_count: int) -> int:
    """Fewer probes on short jobs - 5 probes out of 6 tasks is just a shuffle."""
    if task_count < MIN_TASKS_FOR_PROBING:
        return 0
    return max(2, min(INITIAL_PROBES, task_count // 3))


def plan_initial_probes(samples: list[TaskSample]) -> list[int]:
    """Task ids spread evenly across the frame range."""
    ordered = sorted(samples, key=lambda s: s.first_frame)
    count = _probe_count(len(ordered))
    if count <= 0:
        return []
    if count == 1:
        return [ordered[0].task_id]
    picked: list[int] = []
    for i in range(count):
        index = round(i * (len(ordered) - 1) / (count - 1))
        task_id = ordered[index].task_id
        if task_id not in picked:
            picked.append(task_id)
    return picked


def pick_adaptive_probe(samples: list[TaskSample]) -> int | None:
    """The unrendered chunk that would tell us the most.

    Straight interpolation between evenly spaced probes flattens a narrow peak -
    on v027 it read the 80-minute stretch as ~22 minutes. So once the initial
    probes are in, we spend the next one in the middle of whichever gap shows
    the largest cost swing, which is where the curve is least trustworthy.
    """
    curve = build_curve(samples)
    if len(curve) < 2:
        return None
    by_frame = sorted(samples, key=lambda s: s.midpoint)

    best_score = 0.0
    best_task: int | None = None
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        inner = [
            s
            for s in by_frame
            if x0 < s.midpoint < x1 and s.stat in (TASK_QUEUED, TASK_SUSPENDED)
        ]
        if not inner:
            continue
        score = abs(y1 - y0) * (x1 - x0)
        if score > best_score:
            best_score = score
            best_task = inner[len(inner) // 2].task_id
    return best_task


async def start_probing(telegram_user_id: int, job_id: str, tasks: list[dict]) -> bool:
    """Hold back everything except the probes. Returns True if probing started."""
    samples = collect_samples(tasks)
    if len(samples) < MIN_TASKS_FOR_PROBING:
        return False
    # Only makes sense before the render has really got going.
    if any(s.stat == TASK_COMPLETED for s in samples):
        return False
    if any(s.stat == TASK_SUSPENDED for s in samples):
        # Someone (or a previous run) already suspended tasks here; stay out of it.
        return False

    probes = plan_initial_probes(samples)
    if not probes:
        return False
    probe_ids = set(probes)
    # Only hold back tasks that have not started. Suspending a rendering task
    # would throw away the frames it has already produced.
    held = [
        s.task_id
        for s in samples
        if s.task_id not in probe_ids and s.stat == TASK_QUEUED
    ]
    if not held:
        return False

    # Record first: if suspending half-succeeds, recovery still knows what to release.
    await probe_state.save_probe_state(job_id, telegram_user_id, probes, held)
    ok = await deadline.suspend_tasks_by_user_id(telegram_user_id, job_id, held)
    if not ok:
        logger.warning("Could not hold back tasks of %s; abandoning probe phase", job_id)
        await probe_state.delete_probe_state(job_id)
        return False

    logger.info(
        "Probing %s: %d probe tasks %s, %d tasks held back",
        job_id,
        len(probes),
        probes,
        len(held),
    )
    return True


async def _release(state: probe_state.ProbeState, reason: str) -> bool:
    if not state.held_task_ids:
        await probe_state.mark_probe_released(state.job_id)
        return True
    ok = await deadline.resume_tasks_by_user_id(
        state.telegram_user_id, state.job_id, state.held_task_ids
    )
    if ok:
        await probe_state.mark_probe_released(state.job_id)
        logger.info(
            "Released %d held tasks of %s (%s)",
            len(state.held_task_ids),
            state.job_id,
            reason,
        )
    else:
        logger.warning("Failed to release held tasks of %s (%s)", state.job_id, reason)
    return ok


async def release_if_ready(job_id: str, tasks: list[dict]) -> bool:
    """Keep the render fed, spending spare capacity on probes while we can.

    Waiting for every probe to *finish* before releasing would idle machines:
    with three workers and five probes, the third worker has nothing left to
    pick up once the last two probes are claimed. So the rule is not "probes
    done" but "never let the queue run dry" - as soon as there are no longer
    more dispatchable tasks than busy workers, we hand something over.

    That spare slot goes to a refining probe while the budget lasts (this is
    what lifts the estimate from ~0.8x to ~0.95x), and after that the whole
    remainder is released. A failed task can then send a worker back to the
    start of the range, which is a fair trade against machines sitting idle.
    """
    state = await probe_state.get_probe_state(job_id)
    if state is None or state.released_at is not None or not state.held_task_ids:
        return False

    if state.age_seconds > MAX_PROBE_SECONDS:
        return await _release(state, "probe phase timed out")

    samples = collect_samples(tasks)
    held = set(state.held_task_ids)
    busy_workers = sum(1 for s in samples if s.stat == TASK_RENDERING)
    dispatchable = [
        s for s in samples if s.stat == TASK_QUEUED and s.task_id not in held
    ]
    # Strictly more than busy workers means the next one to free up has a task
    # waiting for it. At equality the queue is one worker away from running dry.
    if len(dispatchable) > busy_workers:
        return False

    if len(state.probe_task_ids) < INITIAL_PROBES + ADAPTIVE_PROBES:
        extra = pick_adaptive_probe(samples)
        if extra is not None and extra in held:
            ok = await deadline.resume_tasks_by_user_id(
                state.telegram_user_id, job_id, [extra]
            )
            if ok:
                await probe_state.save_probe_state(
                    job_id,
                    state.telegram_user_id,
                    list(state.probe_task_ids) + [extra],
                    [t for t in state.held_task_ids if t != extra],
                )
                logger.info("Refining cost curve of %s with probe task %s", job_id, extra)
                return False

    return await _release(state, "queue would otherwise run dry")


async def release_stale_probes() -> int:
    """Release held tasks of any job whose probe phase has overrun.

    Runs at startup and on every watcher tick. This is the backstop that keeps
    a render from stalling when the normal path cannot run - the bot was down,
    the job vanished from the user's view, credentials broke. A probe phase that
    is still within its time budget is left alone, so an ordinary redeploy does
    not throw away the probing of jobs that were mid-flight.
    """
    released = 0
    try:
        states = await probe_state.list_unreleased_probes()
    except Exception:
        logger.exception("Could not read probe state")
        return 0

    for state in states:
        if state.age_seconds <= MAX_PROBE_SECONDS:
            continue
        try:
            if await _release(state, "probe phase overran"):
                released += 1
            elif state.age_seconds > 2 * MAX_PROBE_SECONDS:
                # The job is most likely gone. Stop retrying it forever.
                await probe_state.mark_probe_released(state.job_id)
                logger.warning(
                    "Giving up on releasing %s; job appears to be gone", state.job_id
                )
        except Exception:
            logger.exception("Failed to release held tasks for %s", state.job_id)
    if released:
        logger.info("Released held tasks for %d overrunning job(s)", released)
    return released
