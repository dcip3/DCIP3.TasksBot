"""Cost model for render-time estimation.

Frame cost is wildly uneven on real shots - the most expensive chunk can take
dozens of times longer than the cheapest, and the expensive ones may sit in the
middle of the range. Any estimator that assumes "the frames left cost what the
recent frames cost" therefore lowballs the ETA, by as much as ~5x, for hours.

This module models cost as a *curve over frame index* instead: every completed
task is one sample of that curve, unobserved stretches are interpolated between
the samples that bracket them, and the ETA is the integral of the curve over
what is still unrendered. It only works if the samples are spread across the
range, which is what the probe scheduler arranges - see probe_scheduler.py.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_FRAME_RANGE_RE = re.compile(r"^\s*(-?\d+)\s*(?:-\s*(-?\d+))?\s*$")

# Deadline task states we care about.
TASK_QUEUED = 2
TASK_SUSPENDED = 3
TASK_RENDERING = 4
TASK_COMPLETED = 5


@dataclass(frozen=True)
class TaskSample:
    """One task of a render, with whatever timing Deadline has for it."""

    task_id: int
    first_frame: int
    frame_count: int
    stat: int
    seconds: float | None  # wall time, completed tasks only
    progress: float | None  # 0..1, rendering tasks only
    elapsed: float | None  # seconds since it started, rendering tasks only

    @property
    def midpoint(self) -> float:
        return self.first_frame + (self.frame_count - 1) / 2.0

    @property
    def seconds_per_frame(self) -> float | None:
        if self.seconds is None or self.frame_count <= 0 or self.seconds <= 0:
            return None
        return self.seconds / self.frame_count


def parse_frames(value: object) -> tuple[int, int] | None:
    """Return (first_frame, count) for a Deadline frame string like "921-925"."""
    text = str(value or "").strip()
    if not text:
        return None
    first: int | None = None
    count = 0
    for part in text.split(","):
        match = _FRAME_RANGE_RE.match(part)
        if not match:
            return None
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) is not None else start
        if end < start:
            start, end = end, start
        if first is None or start < first:
            first = start
        count += end - start + 1
    if first is None or count <= 0:
        return None
    return first, count


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text or text.startswith("0001-01-01"):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_progress(value: object) -> float | None:
    """Deadline reports task progress as e.g. "40%" or "40% 3 of 5"."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", str(value or ""))
    if not match:
        return None
    return max(0.0, min(1.0, float(match.group(1)) / 100.0))


def in_flight_frames(task: dict) -> float | None:
    """Frames done in a task, counting the one being rendered as a fraction.

    Deadline's own task progress only moves on whole frames - a five-frame
    chunk reports 0/20/40/60/80/100 and nothing between. The plugin status
    carries the missing detail:

        "Rendering Frame 107 -  Progress 92.0 %"

    which, for a task covering 103-107, means four frames done plus 0.92 of the
    fifth. Returns None when the status says nothing we can trust.
    """
    status = str(task.get("RndStat") or "")
    if not status:
        return None
    frame_match = re.search(r"[Ff]rame\s+(-?\d+)", status)
    if not frame_match:
        return None
    percent_match = re.search(r"(\d+(?:\.\d+)?)\s*%", status)
    fraction = float(percent_match.group(1)) / 100.0 if percent_match else 0.0
    fraction = max(0.0, min(1.0, fraction))

    parsed = parse_frames(task.get("Frames"))
    if parsed is None:
        return None
    first_frame, frame_count = parsed
    index = int(frame_match.group(1)) - first_frame
    if index < 0 or index >= frame_count:
        # Frame numbering we don't recognise - better to say nothing.
        return None
    return min(index + fraction, float(frame_count))


def _task_progress(task: dict, frame_count: int) -> float | None:
    """Task completion 0..1, at sub-frame resolution where the plugin reports it."""
    coarse = _parse_progress(task.get("Prog"))
    fine = in_flight_frames(task)
    if fine is not None and frame_count > 0:
        fine_ratio = fine / frame_count
        return fine_ratio if coarse is None else max(coarse, fine_ratio)
    return coarse


def collect_samples(tasks: list[dict], now_utc: datetime | None = None) -> list[TaskSample]:
    """Turn Deadline's task dicts into cost samples, skipping unusable ones."""
    now = now_utc or datetime.now(timezone.utc)
    samples: list[TaskSample] = []
    for task in tasks:
        frames = parse_frames(task.get("Frames"))
        if frames is None:
            continue
        first_frame, frame_count = frames
        try:
            task_id = int(task.get("TaskID", task.get("_id")))
        except (TypeError, ValueError):
            continue

        stat = int(task.get("Stat", 0) or 0)
        seconds: float | None = None
        progress: float | None = None
        elapsed: float | None = None

        start = _parse_datetime(task.get("StartRen") or task.get("Start"))
        if stat == TASK_COMPLETED:
            completed = _parse_datetime(task.get("Comp"))
            if start is not None and completed is not None and completed > start:
                seconds = (completed - start).total_seconds()
        elif stat == TASK_RENDERING and start is not None:
            progress = _task_progress(task, frame_count)
            elapsed = max((now - start).total_seconds(), 0.0)

        samples.append(
            TaskSample(
                task_id=task_id,
                first_frame=first_frame,
                frame_count=frame_count,
                stat=stat,
                seconds=seconds,
                progress=progress,
                elapsed=elapsed,
            )
        )
    return samples


def build_curve(samples: list[TaskSample]) -> list[tuple[float, float]]:
    """Cost curve as sorted (frame midpoint, seconds per frame) observations.

    A task that is still rendering contributes only once it is far enough along
    to say something useful - an early task at 5% would claim an absurd cost.
    """
    points: dict[float, float] = {}
    for sample in samples:
        per_frame = sample.seconds_per_frame
        if per_frame is None and sample.stat == TASK_RENDERING:
            if (
                sample.progress is not None
                and sample.progress >= 0.25
                and sample.elapsed
                and sample.elapsed > 0
            ):
                per_frame = (sample.elapsed / sample.progress) / sample.frame_count
        if per_frame is None or per_frame <= 0:
            continue
        points[sample.midpoint] = per_frame
    return sorted(points.items())


def cost_at(curve: list[tuple[float, float]], frame: float) -> float | None:
    """Seconds per frame at a frame index, interpolated between observations.

    Outside the observed span the nearest observation is held flat rather than
    extrapolated: extrapolating a steep cost trend produced estimates of 150+
    hours in testing.
    """
    if not curve:
        return None
    if frame <= curve[0][0]:
        return curve[0][1]
    if frame >= curve[-1][0]:
        return curve[-1][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x0 <= frame <= x1:
            if x1 == x0:
                return y0
            return y0 + (y1 - y0) * (frame - x0) / (x1 - x0)
    return curve[-1][1]


def estimate_remaining_seconds(
    samples: list[TaskSample],
    active_workers: int,
) -> float | None:
    """Seconds of wall time left, from the cost curve and how much is unrendered.

    Returns None when there is not enough spread in the observations to say
    anything - the caller should then fall back to a coarser estimate rather
    than show a confident number.
    """
    if active_workers <= 0:
        return None
    curve = build_curve(samples)
    if len(curve) < 2:
        return None

    remaining_costs: list[float] = []
    for sample in samples:
        if sample.stat == TASK_COMPLETED:
            continue
        per_frame = cost_at(curve, sample.midpoint)
        if per_frame is None:
            continue
        cost = per_frame * sample.frame_count
        if sample.stat == TASK_RENDERING and sample.progress is not None:
            cost *= max(1.0 - sample.progress, 0.0)
        if cost > 0:
            remaining_costs.append(cost)

    if not remaining_costs:
        return 0.0

    total = sum(remaining_costs)
    # Perfect parallelism is the optimistic bound; a single chunk still cannot
    # finish faster than itself, which matters at the tail of a job.
    return max(total / active_workers, max(remaining_costs))


def coverage(samples: list[TaskSample]) -> float:
    """How much of the frame range the observations actually span, 0..1.

    Used to decide whether the estimate deserves to be shown as a number.
    """
    if not samples:
        return 0.0
    lows = [s.first_frame for s in samples]
    highs = [s.first_frame + s.frame_count - 1 for s in samples]
    span = max(highs) - min(lows)
    if span <= 0:
        return 1.0
    curve = build_curve(samples)
    if len(curve) < 2:
        return 0.0
    return max(0.0, min(1.0, (curve[-1][0] - curve[0][0]) / span))
