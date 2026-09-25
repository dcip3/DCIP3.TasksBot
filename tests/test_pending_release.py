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

from app.core import farm_events
from app.services import deadline
from app.services.preview import runtime


class PendingReleaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        runtime.preview_tracked_jobs.clear()

    def tearDown(self) -> None:
        runtime.preview_tracked_jobs.clear()

    async def test_releases_only_previews_of_that_render(self) -> None:
        runtime.track_preview_job("prevA", 42, "renderX")
        runtime.track_preview_job("prevB", 42, "renderY")

        with mock.patch(
            "app.services.deadline.get_job_info_by_user_id",
            new=mock.AsyncMock(return_value={"_id": "prevA", "Stat": 6, "Props": {}}),
        ), mock.patch(
            "app.services.deadline.release_pending_job_by_user_id",
            new=mock.AsyncMock(return_value=True),
        ) as release_mock:
            released = await farm_events.release_previews_waiting_on("renderX")

        self.assertEqual(released, 1)
        release_mock.assert_awaited_once_with(42, "prevA")

    async def test_no_previews_for_render_is_a_noop(self) -> None:
        runtime.track_preview_job("prevA", 42, "renderX")
        with mock.patch(
            "app.services.deadline.release_pending_job_by_user_id",
            new=mock.AsyncMock(return_value=True),
        ) as release_mock:
            released = await farm_events.release_previews_waiting_on("otherRender")
        self.assertEqual(released, 0)
        release_mock.assert_not_awaited()

    async def test_failure_to_release_is_survivable(self) -> None:
        runtime.track_preview_job("prevA", 42, "renderX")
        with mock.patch(
            "app.services.deadline.release_pending_job_by_user_id",
            new=mock.AsyncMock(side_effect=RuntimeError("rcs down")),
        ):
            released = await farm_events.release_previews_waiting_on("renderX")
        self.assertEqual(released, 0)

    async def test_release_command_is_allowed(self) -> None:
        """The REST layer must accept the verified 'releasepending' command."""
        self.assertIn("releasepending", deadline._PUT_JOB_COMMANDS)

        with mock.patch.object(
            deadline, "_put_job_command", new=mock.AsyncMock(return_value=True)
        ) as put_mock, mock.patch(
            "app.auth.get_deadline_credentials",
            new=mock.AsyncMock(return_value=("tester", "pw")),
        ):
            ok = await deadline.release_pending_job_by_user_id(42, "prevA")

        self.assertTrue(ok)
        self.assertEqual(put_mock.await_args.args[2], "releasepending")


if __name__ == "__main__":
    unittest.main()
