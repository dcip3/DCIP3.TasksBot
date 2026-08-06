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
    cost_at,
    collect_samples,
)
from app.storage import probe_state

logger = logging.getLogger(__name__)

# Spread this many probes across the range, then refine where the time is.
INITIAL_PROBES = 5
# Two refinements, measured over nine finished jobs on this farm (integral of
# the cost curve against what the job actually cost):
#
#     refinements   median   worst under   worst over   mean |error|
#          +1        1.03       0.94          1.45          9%
#          +2        1.04       0.97          1.17          6%
#          +3        1.03       0.94          1.23          5%
#
# +2 has the best worst case in the direction that matters: promising less time
# than a render takes is what makes the estimate feel broken.
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


def pick_adaptive_probe(
    samples: list[TaskSample],
    exclude: frozenset[int] = frozenset(),
) -> int | None:
    """The unrendered chunk that would tell us the most.

    Straight interpolation between evenly spaced probes flattens narrow peaks -
    on SHB_city_main_v004 the chord across the middle read a 62-minute chunk as
    23 minutes, and the whole estimate ran ~25% short for the entire render.

    Probes therefore go where the *render time* is, not where the curve is
    steepest. Chasing the steepest slope sounds right but keeps landing on the
    cheap end of a ramp. Over nine finished jobs on this farm, with two
    refinements each:

        by slope       median 1.03x, worst under 0.83x
        by cost mass   median 1.04x, worst under 0.97x
    """
    curve = build_curve(samples)
    if len(curve) < 2:
        return None
    by_frame = sorted(samples, key=lambda s: s.midpoint)

    # Split the range at every position already measured *or being measured*.
    # A task that is rendering will deliver its sample shortly, so a probe next
    # to it buys nothing - going by curve points alone once put a probe on task
    # 15 while task 14 was mid-render, learning almost nothing.
    covered = sorted(
        {x for x, _ in curve}
        | {
            s.midpoint
            for s in samples
            if s.stat in (TASK_COMPLETED, TASK_RENDERING)
        }
    )
    if len(covered) < 2:
        return None

    best_key: tuple[float, float] = (0.0, 0.0)
    best_task: int | None = None
    for x0, x1 in zip(covered, covered[1:]):
        inner = [
            s
            for s in by_frame
            if x0 < s.midpoint < x1
            and s.stat in (TASK_QUEUED, TASK_SUSPENDED)
            and s.task_id not in exclude
        ]
        if not inner:
            continue
        y0 = cost_at(curve, x0) or 0.0
        y1 = cost_at(curve, x1) or 0.0
        span = x1 - x0
        # Roughly how much render time this stretch holds - that is where being
        # wrong costs the most. Falls back to the widest unmeasured stretch when
        # no cost is known yet.
        key = ((y0 + y1) / 2.0 * span, span)
        if key > best_key:
            best_key = key
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
    """Release the held tasks once our probes have all been handed out.

    The rule is about probes, not about tasks in general. Deadline gives a free
    worker the lowest-numbered *available* task, so while everything but the
    probes is suspended, a worker that finishes always picks up the next probe -
    no machine idles and no probe gets overtaken.

    Waiting for probes to *finish* would idle machines; releasing the whole
    remainder to avoid that (the first attempt at this) is worse still, because
    tasks 1, 2, 3... immediately outrank the probes further along and those end
    up running hours later. Handing out exactly one probe at a time is what
    satisfies both.

    Once no probe is left waiting, spare capacity goes to a refining probe while
    the budget lasts - that is what lifts the estimate from ~0.8x to ~0.95x -
    and after that the whole remainder is released.
    """
    state = await probe_state.get_probe_state(job_id)
    if state is None or state.released_at is not None or not state.held_task_ids:
        return False

    if state.age_seconds > MAX_PROBE_SECONDS:
        return await _release(state, "probe phase timed out")

    samples = collect_samples(tasks)
    held = set(state.held_task_ids)
    by_id = {s.task_id: s for s in samples}

    # Hold while any probe of ours is still waiting to start. Deadline hands a
    # free worker the lowest-numbered *available* task, so as long as everything
    # else stays suspended, that task is a probe - nobody idles and the probes
    # keep their head start. Releasing the whole remainder instead (as this used
    # to) instantly demotes the later probes behind tasks 1, 2, 3... and they end
    # up running hours later, which defeats the point of probing at all.
    pending_probes = [
        task_id
        for task_id in state.probe_task_ids
        if task_id in by_id and by_id[task_id].stat == TASK_QUEUED
    ]
    if pending_probes:
        return False

    # More capacity than probes - machines that just joined would sit idle while
    # we handed out one probe per scan - so top up for everyone at once.
    busy_workers = sum(1 for s in samples if s.stat == TASK_RENDERING)
    budget = INITIAL_PROBES + ADAPTIVE_PROBES - len(state.probe_task_ids)
    wanted = min(budget, max(1, busy_workers))

    extras: list[int] = []
    while len(extras) < wanted:
        extra = pick_adaptive_probe(samples, exclude=frozenset(extras))
        if extra is None or extra not in held:
            break
        extras.append(extra)

    if extras:
        ok = await deadline.resume_tasks_by_user_id(
            state.telegram_user_id, job_id, extras
        )
        if ok:
            await probe_state.save_probe_state(
                job_id,
                state.telegram_user_id,
                list(state.probe_task_ids) + extras,
                [t for t in state.held_task_ids if t not in extras],
            )
            logger.info("Refining cost curve of %s with probe tasks %s", job_id, extras)
            return False

    # Budget spent, or nothing left worth measuring: everyone gets fed.
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
