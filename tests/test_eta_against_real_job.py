"""Regression test on real render timings.

The numbers below are one production render's actual per-chunk render times,
taken from Deadline: 58 chunks of 5 frames, 8 h 22 min on two workers, and
per-chunk cost ranging from 72 s to 4851 s with the expensive stretch sitting
in the middle of the range. On a render like this a throughput estimator reads
"about 1.5 hours left" for most of the render.

The test replays the render and checks that the cost model tracks the truth
where the old approach could not.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.services import probe_scheduler, render_cost

WORKERS = 2
EPOCH = datetime(2026, 8, 4, 11, 32, 43, tzinfo=timezone.utc)

# (first frame of chunk, seconds it actually took)
V027 = [
    (921, 223), (926, 209), (931, 205), (936, 202), (941, 351), (946, 201),
    (951, 201), (956, 301), (961, 203), (966, 204), (971, 300), (976, 202),
    (981, 303), (986, 204), (991, 205), (996, 308), (1001, 72), (1006, 315),
    (1011, 214), (1016, 331), (1021, 222), (1026, 229), (1031, 368), (1036, 268),
    (1041, 443), (1046, 310), (1051, 355), (1056, 693), (1061, 615), (1066, 944),
    (1071, 751), (1076, 855), (1081, 1183), (1086, 980), (1091, 1682), (1096, 1575),
    (1101, 3279), (1106, 2109), (1111, 2741), (1116, 2776), (1121, 4080), (1126, 4851),
    (1131, 2612), (1136, 1714), (1141, 1902), (1146, 1405), (1151, 1321), (1156, 1441),
    (1161, 1241), (1166, 1416), (1171, 1314), (1176, 1439), (1181, 1275), (1186, 1287),
    (1191, 998), (1196, 934), (1201, 790), (1206, 469),
]


def _schedule(order: list[int]) -> tuple[dict[int, tuple[float, float]], float]:
    """Greedy two-worker scheduling; returns {task_id: (start, end)} and makespan."""
    free = [0.0] * WORKERS
    spans: dict[int, tuple[float, float]] = {}
    for task_id in order:
        worker = free.index(min(free))
        start = free[worker]
        free[worker] = start + V027[task_id][1]
        spans[task_id] = (start, free[worker])
    return spans, max(free)


def _tasks_at(spans: dict[int, tuple[float, float]], now: float) -> list[dict]:
    """The task list Deadline would report at `now` seconds into the render."""
    tasks = []
    for task_id, (first_frame, seconds) in enumerate(V027):
        start, end = spans[task_id]
        entry: dict = {"TaskID": task_id, "Frames": f"{first_frame}-{first_frame + 4}"}
        if end <= now:
            entry["Stat"] = render_cost.TASK_COMPLETED
            entry["StartRen"] = (EPOCH + timedelta(seconds=start)).isoformat()
            entry["Comp"] = (EPOCH + timedelta(seconds=end)).isoformat()
        elif start <= now:
            entry["Stat"] = render_cost.TASK_RENDERING
            entry["StartRen"] = (EPOCH + timedelta(seconds=start)).isoformat()
            entry["Prog"] = "%d%%" % int(100 * (now - start) / seconds)
        else:
            entry["Stat"] = render_cost.TASK_QUEUED
        tasks.append(entry)
    return tasks


def _naive_eta(tasks: list[dict], now: float) -> float | None:
    """What a frames-per-second estimator would say: the old behaviour."""
    done = sum(1 for t in tasks if t["Stat"] == render_cost.TASK_COMPLETED)
    if not done:
        return None
    fraction = done / len(tasks)
    return now * (1 - fraction) / fraction


def _as_tasks(completed: list[int]) -> list[dict]:
    """Task list where exactly `completed` are done, with their real durations."""
    tasks = []
    for task_id, (first_frame, seconds) in enumerate(V027):
        entry: dict = {"TaskID": task_id, "Frames": f"{first_frame}-{first_frame + 4}"}
        if task_id in completed:
            entry["Stat"] = render_cost.TASK_COMPLETED
            entry["StartRen"] = EPOCH.isoformat()
            entry["Comp"] = (EPOCH + timedelta(seconds=seconds)).isoformat()
        else:
            entry["Stat"] = render_cost.TASK_QUEUED
        tasks.append(entry)
    return tasks


def _probe_order() -> list[int]:
    """The order the scheduler actually produces: spread probes, then refinement."""
    probes = probe_scheduler.plan_initial_probes(
        render_cost.collect_samples(_as_tasks([]))
    )
    for _ in range(probe_scheduler.ADAPTIVE_PROBES):
        extra = probe_scheduler.pick_adaptive_probe(
            render_cost.collect_samples(_as_tasks(probes))
        )
        if extra is None or extra in probes:
            break
        probes.append(extra)
    return probes + [i for i in range(len(V027)) if i not in probes]


class RealJobEtaTests(unittest.TestCase):
    def test_probe_order_keeps_the_estimate_honest(self) -> None:
        """With probes first, the estimate stays in the right order of magnitude."""
        spans, total = _schedule(_probe_order())
        errors = []
        for step in range(3, 12):  # skip the very start, before any probe lands
            now = total * step / 13.0
            tasks = _tasks_at(spans, now)
            samples = render_cost.collect_samples(
                tasks, EPOCH + timedelta(seconds=now)
            )
            estimate = render_cost.estimate_remaining_seconds(samples, WORKERS)
            self.assertIsNotNone(estimate, f"no estimate at {now / 3600:.1f} h")
            errors.append(estimate / (total - now))

        # Measured range on this job is 0.79x - 1.03x; the bounds leave room for
        # tuning without letting the old 0.2x behaviour back in.
        self.assertGreater(min(errors), 0.70, f"underestimates badly: {errors}")
        self.assertLess(max(errors), 1.40, f"overestimates badly: {errors}")

    def test_old_estimator_was_off_by_several_times(self) -> None:
        """Guards the premise: this job really does defeat throughput estimates."""
        spans, total = _schedule(list(range(len(V027))))
        now = total * 0.25
        tasks = _tasks_at(spans, now)
        naive = _naive_eta(tasks, now)
        self.assertIsNotNone(naive)
        # A quarter of the way in, it claimed under half an hour per hour left.
        self.assertLess(naive / (total - now), 0.35)

    def test_sequential_order_is_not_trusted_early(self) -> None:
        """Without probes the samples cover one end only - refuse to answer."""
        spans, total = _schedule(list(range(len(V027))))
        tasks = _tasks_at(spans, total * 0.15)
        samples = render_cost.collect_samples(
            tasks, EPOCH + timedelta(seconds=total * 0.15)
        )
        self.assertLess(render_cost.coverage(samples), 0.6)


if __name__ == "__main__":
    unittest.main()
