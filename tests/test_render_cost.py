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

from app.services import render_cost

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def task(
    task_id: int,
    first: int,
    count: int = 5,
    stat: int = render_cost.TASK_QUEUED,
    seconds: float | None = None,
    progress: str | None = None,
    started_ago: float | None = None,
) -> dict:
    entry: dict = {
        "TaskID": task_id,
        "Frames": f"{first}-{first + count - 1}",
        "Stat": stat,
    }
    if seconds is not None:
        start = NOW - timedelta(seconds=seconds + 600)
        entry["StartRen"] = start.isoformat()
        entry["Comp"] = (start + timedelta(seconds=seconds)).isoformat()
    elif started_ago is not None:
        entry["StartRen"] = (NOW - timedelta(seconds=started_ago)).isoformat()
    if progress is not None:
        entry["Prog"] = progress
    return entry


class FrameParsingTests(unittest.TestCase):
    def test_parses_ranges_and_singles(self) -> None:
        self.assertEqual(render_cost.parse_frames("921-925"), (921, 5))
        self.assertEqual(render_cost.parse_frames("42"), (42, 1))
        self.assertEqual(render_cost.parse_frames("1-2,10-12"), (1, 5))

    def test_rejects_garbage(self) -> None:
        self.assertIsNone(render_cost.parse_frames(""))
        self.assertIsNone(render_cost.parse_frames("every other frame"))


class CostCurveTests(unittest.TestCase):
    def test_interpolates_between_observations(self) -> None:
        curve = [(0.0, 10.0), (100.0, 20.0)]
        self.assertEqual(render_cost.cost_at(curve, 50.0), 15.0)

    def test_holds_flat_outside_observed_span(self) -> None:
        """Extrapolating a steep trend produced 150h estimates; don't do it."""
        curve = [(10.0, 5.0), (20.0, 50.0)]
        self.assertEqual(render_cost.cost_at(curve, 0.0), 5.0)
        self.assertEqual(render_cost.cost_at(curve, 9999.0), 50.0)

    def test_ignores_barely_started_tasks(self) -> None:
        """A task at 5% would claim an absurd per-frame cost."""
        samples = render_cost.collect_samples(
            [task(0, 1, stat=render_cost.TASK_RENDERING, progress="5%", started_ago=60)],
            NOW,
        )
        self.assertEqual(render_cost.build_curve(samples), [])


class RemainingEstimateTests(unittest.TestCase):
    def test_expensive_middle_is_not_flattened_away(self) -> None:
        """The real failure mode: cheap ends, expensive middle."""
        tasks = [
            task(0, 1, stat=render_cost.TASK_COMPLETED, seconds=100),   # 20 s/frame
            task(5, 51, stat=render_cost.TASK_COMPLETED, seconds=2000),  # 400 s/frame
            task(9, 91, stat=render_cost.TASK_COMPLETED, seconds=100),
            task(2, 21),
            task(7, 71),
        ]
        samples = render_cost.collect_samples(tasks, NOW)
        remaining = render_cost.estimate_remaining_seconds(samples, active_workers=1)
        # Both queued chunks sit on the slopes toward the expensive middle. A
        # throughput estimator sees only the 20 s/frame ends and would predict
        # ~200 s for the two of them; the curve must charge several times that.
        self.assertIsNotNone(remaining)
        naive = 2 * 5 * 20.0
        self.assertGreater(remaining, 5 * naive)

    def test_returns_none_without_enough_spread(self) -> None:
        samples = render_cost.collect_samples(
            [task(0, 1, stat=render_cost.TASK_COMPLETED, seconds=100), task(1, 6)], NOW
        )
        self.assertIsNone(render_cost.estimate_remaining_seconds(samples, 1))

    def test_never_faster_than_its_slowest_remaining_chunk(self) -> None:
        tasks = [
            task(0, 1, stat=render_cost.TASK_COMPLETED, seconds=60),
            task(9, 91, stat=render_cost.TASK_COMPLETED, seconds=60),
            task(5, 51),
        ]
        samples = render_cost.collect_samples(tasks, NOW)
        remaining = render_cost.estimate_remaining_seconds(samples, active_workers=8)
        self.assertAlmostEqual(remaining, 60.0, delta=1.0)

    def test_partly_rendered_task_only_counts_what_is_left(self) -> None:
        tasks = [
            task(0, 1, stat=render_cost.TASK_COMPLETED, seconds=100),
            task(9, 91, stat=render_cost.TASK_COMPLETED, seconds=100),
            task(5, 51, stat=render_cost.TASK_RENDERING, progress="80%", started_ago=80),
        ]
        samples = render_cost.collect_samples(tasks, NOW)
        remaining = render_cost.estimate_remaining_seconds(samples, active_workers=1)
        self.assertLess(remaining, 100)


class InFlightFrameTests(unittest.TestCase):
    """Deadline's task progress only moves on whole frames; the plugin status
    carries the frame being rendered and how far into it we are."""

    def test_reads_the_frame_in_flight(self) -> None:
        task = {
            "Frames": "103-107",
            "RndStat": "Rendering Frame 107 -  Progress 92.0 %",
        }
        self.assertAlmostEqual(render_cost.in_flight_frames(task), 4.92)

    def test_first_frame_of_a_chunk(self) -> None:
        task = {"Frames": "103-107", "RndStat": "Rendering Frame 103 - Progress 10.0 %"}
        self.assertAlmostEqual(render_cost.in_flight_frames(task), 0.1)

    def test_ignores_a_frame_outside_the_chunk(self) -> None:
        task = {"Frames": "103-107", "RndStat": "Rendering Frame 900 - Progress 50 %"}
        self.assertIsNone(render_cost.in_flight_frames(task))

    def test_ignores_unparseable_status(self) -> None:
        self.assertIsNone(render_cost.in_flight_frames({"Frames": "1-5"}))
        self.assertIsNone(
            render_cost.in_flight_frames({"Frames": "1-5", "RndStat": "Loading scene"})
        )

    def test_never_exceeds_the_chunk(self) -> None:
        task = {"Frames": "1-5", "RndStat": "Rendering Frame 5 - Progress 100 %"}
        self.assertEqual(render_cost.in_flight_frames(task), 5.0)

    def test_feeds_finer_progress_into_the_cost_model(self) -> None:
        """A task at 80% + 92% of the next frame is nearly done, not 80% done."""
        sample = render_cost.collect_samples(
            [
                {
                    "TaskID": 0,
                    "Frames": "103-107",
                    "Stat": render_cost.TASK_RENDERING,
                    "Prog": "80 %",
                    "RndStat": "Rendering Frame 107 -  Progress 92.0 %",
                    "StartRen": (NOW - timedelta(seconds=100)).isoformat(),
                }
            ],
            NOW,
        )[0]
        self.assertAlmostEqual(sample.progress, 0.984, places=3)


class CoverageTests(unittest.TestCase):
    def test_sequential_start_has_poor_coverage(self) -> None:
        tasks = [task(i, 1 + i * 5, stat=render_cost.TASK_COMPLETED, seconds=60) for i in range(3)]
        tasks += [task(i, 1 + i * 5) for i in range(3, 20)]
        samples = render_cost.collect_samples(tasks, NOW)
        self.assertLess(render_cost.coverage(samples), 0.2)

    def test_spread_probes_give_good_coverage(self) -> None:
        tasks = [task(i, 1 + i * 5) for i in range(20)]
        for i in (0, 6, 12, 19):
            tasks[i] = task(i, 1 + i * 5, stat=render_cost.TASK_COMPLETED, seconds=60)
        samples = render_cost.collect_samples(tasks, NOW)
        self.assertGreater(render_cost.coverage(samples), 0.9)


if __name__ == "__main__":
    unittest.main()
