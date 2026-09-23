"""One preview per run, and a clock that counts rendering rather than waiting.

Delivery deletes the preview job. If the watcher took the vanished job for a
lost preview and cleared the run records, the next scan would see a recent
completion with nothing on record and make another preview - which finds the
video already rendered and uploads it again, so the same preview arrives twice
with an identical caption.

A job card's render time counts only the time a task was running. A job can
wait while the farm is busy with other work, and counting the whole span can
make it look three times longer than the time it spent rendering.
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.bot.handlers import jobs as jobs_handler
from app.services import job_watcher
from app.services.job_state import auto_preview_jobs, notified_jobs
from app.services.preview import runtime

USER = 7
PREVIEW = "prev1"
SOURCE = "src1"


class VanishedPreviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        notified_jobs.clear()
        auto_preview_jobs.clear()
        runtime.preview_tracked_jobs.clear()
        runtime.preview_missing_strikes.clear()

    def _user(self):
        return job_watcher._WatcherUser(
            telegram_user_id=USER,
            login="artist2",
            password="pw",
            notifications_enabled=True,
            notification_scope="all",
            auto_scope="all",
            preview_worker=None,
            auto_preview_enabled=True,
        )

    async def _vanish(self) -> mock.AsyncMock:
        """Report the preview missing often enough to be believed."""
        runtime.track_preview_job(PREVIEW, USER, SOURCE)
        auto_preview_jobs.add((SOURCE, USER))
        forget = mock.AsyncMock()
        with mock.patch.object(job_watcher, "_unregister_auto_preview_history", new=forget):
            for _ in range(job_watcher._MISSING_PREVIEW_STRIKES):
                await job_watcher._handle_missing_preview_job(PREVIEW, self._user())
        return forget

    async def test_a_preview_deleted_before_delivering_frees_its_run(self) -> None:
        forget = await self._vanish()
        forget.assert_awaited_once_with(USER, SOURCE)
        self.assertNotIn((SOURCE, USER), auto_preview_jobs)

    async def test_a_delivered_preview_leaves_its_run_on_record(self) -> None:
        """The duplicate: the video was already in the chat."""
        notified_jobs.add((PREVIEW, USER))
        forget = await self._vanish()
        forget.assert_not_awaited()
        self.assertIn((SOURCE, USER), auto_preview_jobs)

    async def test_a_few_misses_are_tolerated_first(self) -> None:
        runtime.track_preview_job(PREVIEW, USER, SOURCE)
        forget = mock.AsyncMock()
        with mock.patch.object(job_watcher, "_unregister_auto_preview_history", new=forget):
            await job_watcher._handle_missing_preview_job(PREVIEW, self._user())
        forget.assert_not_awaited()
        self.assertIn(PREVIEW, runtime.preview_tracked_jobs)


NOW = datetime(2026, 8, 20, 12, 4, tzinfo=timezone.utc)


def task(task_id: int, start: str, comp: str | None, stat: int = 5) -> dict:
    def stamp(value: str | None) -> str:
        return f"2026-08-{value}:00+00:00" if value else "0001-01-01T00:00:00Z"

    return {
        "TaskID": task_id,
        "StartRen": stamp(start),
        "Comp": stamp(comp),
        "Stat": stat,
    }


class RenderClockTests(unittest.TestCase):
    def test_waiting_between_tasks_is_not_render_time(self) -> None:
        """The farm was busy elsewhere from 23:32 until 08:38."""
        job = {"DateStart": "2026-08-19T22:30:48+00:00"}
        tasks = [
            task(0, "19T22:30", "19T23:32"),
            task(1, "20T08:38", "20T12:04", stat=4),
        ]
        seconds = jobs_handler._job_render_seconds(job, NOW, tasks=tasks)
        self.assertEqual(jobs_handler._humanize_duration(seconds), "4 h 28 min")

        wall = jobs_handler._job_render_seconds(job, NOW, tasks=[])
        self.assertEqual(jobs_handler._humanize_duration(wall), "13 h 33 min")

    def test_machines_working_side_by_side_count_once(self) -> None:
        """Two workers on one job make it finish sooner, not take longer."""
        tasks = [task(0, "20T10:00", "20T11:00"), task(1, "20T10:00", "20T11:00")]
        seconds = jobs_handler._job_render_seconds({}, NOW, tasks=tasks)
        self.assertEqual(jobs_handler._humanize_duration(seconds), "1 h 00 min")

    def test_overlapping_runs_are_merged(self) -> None:
        tasks = [task(0, "20T10:00", "20T11:00"), task(1, "20T10:30", "20T11:30")]
        seconds = jobs_handler._job_render_seconds({}, NOW, tasks=tasks)
        self.assertEqual(jobs_handler._humanize_duration(seconds), "1 h 30 min")

    def test_a_task_still_rendering_counts_up_to_now(self) -> None:
        tasks = [task(0, "20T11:04", None, stat=4)]
        seconds = jobs_handler._job_render_seconds({}, NOW, tasks=tasks)
        self.assertEqual(jobs_handler._humanize_duration(seconds), "1 h 00 min")

    def test_a_queued_task_has_not_rendered_anything(self) -> None:
        tasks = [task(0, "20T11:04", None, stat=1)]
        self.assertIsNone(jobs_handler._job_render_seconds({}, NOW, tasks=tasks))

    def test_without_task_details_the_wall_clock_stands(self) -> None:
        job = {"DateStart": "2026-08-20T10:04:00+00:00"}
        seconds = jobs_handler._job_render_seconds(job, NOW, tasks=None)
        self.assertEqual(jobs_handler._humanize_duration(seconds), "2 h 00 min")

    def test_a_finished_job_keeps_the_time_it_took(self) -> None:
        job = {
            "DateStart": "2026-08-20T08:00:00+00:00",
            "DateComp": "2026-08-20T09:00:00+00:00",
        }
        self.assertEqual(
            jobs_handler._humanize_duration(jobs_handler._job_render_seconds(job, NOW, tasks=[])),
            "1 h 00 min",
        )

    def test_a_job_that_never_started_has_no_clock(self) -> None:
        self.assertIsNone(jobs_handler._job_render_seconds({}, NOW, tasks=[]))
        self.assertIsNone(jobs_handler._job_render_seconds({"DateStart": ""}, NOW, tasks=[]))


if __name__ == "__main__":
    unittest.main()
