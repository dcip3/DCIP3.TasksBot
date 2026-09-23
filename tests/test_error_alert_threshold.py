"""The alert threshold, driven through the real scan.

The matcher and the dedupe id are unit-tested elsewhere; what matters here is
the behaviour they add up to: a job that trips over a syncing scene a few times
says nothing, and a job that keeps doing it says something exactly once.
"""

import os
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.services import job_watcher

SCENE = "Y:/projects/proj_a/scenes/SHA_0110/SHA_0110_v018.hip"
JOB_ID = "000000000000000000000001"
CONTENTS = (
    "0: STDOUT: Error: Caught exception: The attempted operation failed.\n"
    "0: STDOUT: Unable to open file: " + SCENE + "\n"
)


def _user() -> job_watcher._WatcherUser:
    return job_watcher._WatcherUser(
        telegram_user_id=100000002,
        login="nodeb",
        password="pw",
        notifications_enabled=True,
        notification_scope="own",
        auto_scope="own",
        preview_worker=None,
        auto_preview_enabled=False,
    )


def _job() -> dict:
    return {
        "_id": JOB_ID,
        "Stat": 1,
        "Props": {"User": "nodeb", "Name": "SHA_0110_v018 - /obj/ropnet1/SHA_0081"},
    }


def _reports(count: int) -> list[dict]:
    return [
        {
            "_id": f"report{i}",
            "Title": "Error: Caught exception: The attempted operation failed.",
            "Slave": "NodeA",
            "JobUser": "nodeb",
            "JobName": "SHA_0110_v018 - /obj/ropnet1/SHA_0081",
            "Task": str(i),
            "Date": None,  # no date: treated as recent
        }
        for i in range(count)
    ]


class SceneNotReadyThresholdScanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        job_watcher._error_alert_cache.clear()
        self.sent: list[tuple[int, str]] = []

    async def _scan(self, report_count: int) -> list[tuple[int, str]]:
        async def send_message(user_id, text, **kwargs):
            self.sent.append((user_id, text))

        with (
            mock.patch(
                "app.services.deadline.get_jobs_by_credentials",
                new=mock.AsyncMock(return_value=[_job()]),
            ),
            mock.patch(
                "app.services.deadline.get_job_error_reports",
                new=mock.AsyncMock(return_value=_reports(report_count)),
            ),
            mock.patch(
                "app.services.deadline.get_job_report_contents",
                new=mock.AsyncMock(return_value=CONTENTS),
            ),
            mock.patch.object(
                job_watcher.bot, "send_message", new=mock.AsyncMock(side_effect=send_message)
            ),
        ):
            await job_watcher._scan_error_reports_candidates([_user()])
        return self.sent

    async def test_a_handful_of_failures_says_nothing(self) -> None:
        """The scene still syncing: the render recovers on its own."""
        self.assertEqual(await self._scan(9), [])

    async def test_a_job_that_keeps_failing_sends_one_message(self) -> None:
        sent = await self._scan(40)
        self.assertEqual(len(sent), 1)
        recipient, text = sent[0]
        self.assertEqual(recipient, 100000002)
        self.assertIn(SCENE, text)

    async def test_a_second_scan_does_not_repeat_the_message(self) -> None:
        await self._scan(40)
        self.sent.clear()
        self.assertEqual(await self._scan(40), [])

    async def test_the_message_counts_the_failures_so_far(self) -> None:
        _, text = (await self._scan(40))[0]
        # Reported when the threshold is crossed, not after the whole list.
        self.assertIn(str(job_watcher._SCENE_NOT_READY_MIN_OCCURRENCES), text)


if __name__ == "__main__":
    unittest.main()
