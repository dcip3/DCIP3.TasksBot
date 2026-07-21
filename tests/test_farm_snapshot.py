import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.services import deadline


def _seed_jobs(login: str, data, age_seconds: float) -> None:
    deadline._jobs_cache[deadline._jobs_cache_key(login)] = (
        time.monotonic() - age_seconds,
        data,
    )


class FarmSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        deadline._jobs_cache.clear()
        deadline._refresh_tasks.clear()

    async def test_fresh_snapshot_hits_cache_without_fetch(self) -> None:
        _seed_jobs("tester", [{"_id": "cached"}], age_seconds=1.0)
        with mock.patch.object(
            deadline, "_fetch_jobs_by_credentials", new=mock.AsyncMock()
        ) as fetch_mock:
            jobs = await deadline.get_jobs_snapshot("tester", "pw")
        self.assertEqual(jobs, [{"_id": "cached"}])
        fetch_mock.assert_not_awaited()

    async def test_stale_snapshot_returns_instantly_and_refreshes(self) -> None:
        _seed_jobs("tester", [{"_id": "stale"}], age_seconds=deadline._JOBS_FRESH_SECONDS + 5)
        with mock.patch.object(
            deadline,
            "_fetch_jobs_by_credentials",
            new=mock.AsyncMock(return_value=[{"_id": "fresh"}]),
        ) as fetch_mock:
            jobs = await deadline.get_jobs_snapshot("tester", "pw")
            self.assertEqual(jobs, [{"_id": "stale"}])  # instant stale answer

            second = await deadline.get_jobs_snapshot("tester", "pw")
            self.assertEqual(second, [{"_id": "stale"}])

            await asyncio.sleep(0.05)  # let the background refresh run
        # Single-flight: two stale reads spawn only one refresh.
        self.assertEqual(fetch_mock.await_count, 1)

    async def test_missing_snapshot_blocks_on_fetch(self) -> None:
        with mock.patch.object(
            deadline,
            "_fetch_jobs_by_credentials",
            new=mock.AsyncMock(return_value=[{"_id": "live"}]),
        ) as fetch_mock:
            jobs = await deadline.get_jobs_snapshot("tester", "pw")
        self.assertEqual(jobs, [{"_id": "live"}])
        fetch_mock.assert_awaited_once()

    async def test_invalidate_marks_stale_but_keeps_data(self) -> None:
        _seed_jobs("tester", [{"_id": "cached"}], age_seconds=1.0)
        deadline.invalidate_all_jobs_cache()

        key = deadline._jobs_cache_key("tester")
        self.assertIn(key, deadline._jobs_cache)  # data preserved for instant answers
        age = time.monotonic() - deadline._jobs_cache[key][0]
        self.assertGreater(age, deadline._JOBS_FRESH_SECONDS)

        with mock.patch.object(
            deadline,
            "_fetch_jobs_by_credentials",
            new=mock.AsyncMock(return_value=[{"_id": "fresh"}]),
        ) as fetch_mock:
            jobs = await deadline.get_jobs_snapshot("tester", "pw")
            self.assertEqual(jobs, [{"_id": "cached"}])  # instant, then refresh
            await asyncio.sleep(0.05)
        fetch_mock.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
