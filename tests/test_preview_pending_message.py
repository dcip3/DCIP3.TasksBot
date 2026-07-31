import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.services import job_watcher
from app.services.preview import delivery, runtime


class SilentAutoPreviewTests(unittest.IsolatedAsyncioTestCase):
    """Auto previews are followed without posting anything to the chat."""

    def setUp(self) -> None:
        runtime.preview_message_registry.clear()
        runtime.preview_animation_tasks.clear()
        runtime.preview_tracked_jobs.clear()

    async def asyncTearDown(self) -> None:
        for task in list(runtime.preview_animation_tasks.values()):
            task.cancel()
        runtime.preview_message_registry.clear()
        runtime.preview_animation_tasks.clear()
        runtime.preview_tracked_jobs.clear()

    async def test_auto_preview_submits_without_message(self) -> None:
        with mock.patch.object(
            runtime.bot, "send_message", new=mock.AsyncMock()
        ) as send_mock, mock.patch(
            "app.services.preview.render.create_video_from_job",
            new=mock.AsyncMock(return_value={"preview_job_id": "prev1"}),
        ):
            submitted = await runtime._submit_auto_preview_deadline(
                42, "src1", "Shot", None, waiting_for_render=True, depends_on="src1"
            )

        self.assertTrue(submitted)
        send_mock.assert_not_awaited()  # nothing posted while the render runs
        self.assertEqual(runtime.preview_tracked_jobs, {"prev1": 42})
        self.assertNotIn("prev1", runtime.preview_message_registry)
        self.assertFalse(runtime.preview_animation_tasks)

    async def test_manual_preview_keeps_progress_message(self) -> None:
        with mock.patch.object(runtime, "_run_preview_animation", new=mock.AsyncMock()):
            runtime.register_preview_message("prev2", 100, 200)
            await asyncio.sleep(0)
        self.assertIn("prev2", runtime.preview_message_registry)
        self.assertIn("prev2", runtime.preview_animation_tasks)

    async def test_watcher_follows_silent_previews(self) -> None:
        runtime.track_preview_job("prev3", 42)
        user = mock.Mock(telegram_user_id=42, login="tester", password="pw")
        targets = job_watcher._collect_active_preview_targets([user])
        self.assertEqual(targets, [("prev3", user)])

    async def test_watcher_does_not_duplicate_targets(self) -> None:
        runtime.track_preview_job("prev4", 42)
        runtime.preview_message_registry["prev4"] = (42, 7)
        user = mock.Mock(telegram_user_id=42, login="tester", password="pw")
        targets = job_watcher._collect_active_preview_targets([user])
        self.assertEqual(len(targets), 1)

    async def test_delivery_stops_tracking(self) -> None:
        runtime.track_preview_job("prev5", 42)
        delivery._pop_message("prev5")
        self.assertNotIn("prev5", runtime.preview_tracked_jobs)

    async def test_pop_message_stops_tracking(self) -> None:
        runtime.track_preview_job("prev6", 42)
        runtime.pop_preview_message("prev6")
        self.assertNotIn("prev6", runtime.preview_tracked_jobs)


if __name__ == "__main__":
    unittest.main()
