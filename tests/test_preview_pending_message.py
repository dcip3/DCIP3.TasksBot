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
        self.assertEqual(runtime.preview_tracked_jobs, {"prev1": (42, "src1")})
        self.assertNotIn("prev1", runtime.preview_message_registry)
        self.assertFalse(runtime.preview_animation_tasks)

    async def test_manual_preview_keeps_progress_message(self) -> None:
        with mock.patch.object(runtime, "_run_preview_animation", new=mock.AsyncMock()):
            runtime.register_preview_message("prev2", 100, 200)
            await asyncio.sleep(0)
        self.assertIn("prev2", runtime.preview_message_registry)
        self.assertIn("prev2", runtime.preview_animation_tasks)

    async def _targets(self, user, farm_jobs: list):
        with mock.patch(
            "app.services.deadline.get_jobs_by_credentials",
            new=mock.AsyncMock(return_value=farm_jobs),
        ):
            return await job_watcher._collect_active_preview_targets([user])

    async def test_watcher_follows_silent_previews(self) -> None:
        runtime.track_preview_job("prev3", 42, "src1")
        user = mock.Mock(telegram_user_id=42, login="tester", password="pw")
        targets = await self._targets(user, [])
        self.assertEqual(targets, [("prev3", user)])

    async def test_watcher_does_not_duplicate_targets(self) -> None:
        runtime.track_preview_job("prev4", 42, "src1")
        runtime.preview_message_registry["prev4"] = (42, 7)
        user = mock.Mock(telegram_user_id=42, login="tester", password="pw")
        targets = await self._targets(user, [])
        self.assertEqual(len(targets), 1)

    async def test_watcher_recovers_previews_after_restart(self) -> None:
        """Nothing in memory (as after a restart): previews come from Deadline."""
        user = mock.Mock(telegram_user_id=42, login="tester", password="pw")
        farm_jobs = [
            {
                "_id": "prevA",
                "Stat": 6,  # pending on its render
                "Props": {
                    "Name": "Shot - Preview",
                    "ExDic": {
                        "PreviewJob": "1",
                        "PreviewTelegram": "42",
                        "PreviewSource": "render1",
                    },
                },
            },
            {"_id": "render1", "Stat": 1, "Props": {"Name": "Shot"}},
        ]
        targets = await self._targets(user, farm_jobs)
        self.assertEqual(targets, [("prevA", user)])
        # The link back to the render is re-learned, so the bot can release the
        # preview itself when that render finishes.
        self.assertEqual(runtime.preview_tracked_jobs.get("prevA"), (42, "render1"))

    async def test_other_users_previews_are_ignored(self) -> None:
        user = mock.Mock(telegram_user_id=42, login="tester", password="pw")
        farm_jobs = [
            {
                "_id": "prevB",
                "Stat": 1,
                "Props": {
                    "Name": "Shot - Preview",
                    "ExDic": {"PreviewJob": "1", "PreviewTelegram": "999"},
                },
            }
        ]
        self.assertEqual(await self._targets(user, farm_jobs), [])

    async def test_deleted_preview_stops_being_followed(self) -> None:
        runtime.track_preview_job("prevC", 42, "srcC")
        user = mock.Mock(telegram_user_id=42, login="tester", password="pw")

        with mock.patch(
            "app.services.deadline.get_job_info_direct",
            new=mock.AsyncMock(return_value=None),
        ), mock.patch.object(
            job_watcher, "_unregister_auto_preview_history", new=mock.AsyncMock()
        ) as unregister_mock:
            for _ in range(job_watcher._MISSING_PREVIEW_STRIKES - 1):
                await job_watcher._process_active_preview_job("prevC", user)
                self.assertIn("prevC", runtime.preview_tracked_jobs)  # transient misses

            await job_watcher._process_active_preview_job("prevC", user)

        self.assertNotIn("prevC", runtime.preview_tracked_jobs)
        # Dedupe cleared so the render can get a fresh preview.
        unregister_mock.assert_awaited_once_with(42, "srcC")

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
