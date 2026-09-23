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

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.core import farm_events
from app.core.config import settings


class FarmEventsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        app = web.Application()
        app.router.add_post(farm_events.EVENT_PATH, farm_events.handle_deadline_event)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        farm_events._watcher_wake.clear()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_disabled_without_secret(self) -> None:
        with mock.patch.object(settings, "deadline_event_secret", None):
            response = await self.client.post(farm_events.EVENT_PATH, json={})
        self.assertEqual(response.status, 503)

    async def test_rejects_wrong_secret(self) -> None:
        with mock.patch.object(settings, "deadline_event_secret", "right"):
            response = await self.client.post(
                farm_events.EVENT_PATH,
                json={"event": "job_finished"},
                headers={"X-Deadline-Event-Secret": "wrong"},
            )
        self.assertEqual(response.status, 403)
        self.assertFalse(farm_events._watcher_wake.is_set())

    async def test_rejects_near_miss_and_missing_secret(self) -> None:
        wrong_headers = [
            {"X-Deadline-Event-Secret": "righ"},
            {"X-Deadline-Event-Secret": "right-and-more"},
            {"X-Deadline-Event-Secret": "r\u00efght"},
            {"X-Deadline-Event-Secret": ""},
            {},
        ]
        for headers in wrong_headers:
            with self.subTest(headers=headers), mock.patch.object(
                settings, "deadline_event_secret", "right"
            ):
                response = await self.client.post(
                    farm_events.EVENT_PATH,
                    json={"event": "job_finished"},
                    headers=headers,
                )
                self.assertEqual(response.status, 403)
                self.assertFalse(farm_events._watcher_wake.is_set())

    async def test_accepts_event_and_wakes_watcher(self) -> None:
        with mock.patch.object(settings, "deadline_event_secret", "right"), mock.patch(
            "app.services.deadline.invalidate_all_jobs_cache"
        ) as invalidate_mock:
            response = await self.client.post(
                farm_events.EVENT_PATH,
                json={"event": "job_finished", "job_id": "abc", "job_name": "Shot"},
                headers={"X-Deadline-Event-Secret": "right"},
            )
        self.assertEqual(response.status, 200)
        invalidate_mock.assert_called_once()
        self.assertTrue(farm_events._watcher_wake.is_set())

        woken = await farm_events.wait_for_wake(0.1)
        self.assertTrue(woken)
        self.assertFalse(farm_events._watcher_wake.is_set())

    async def test_wait_for_wake_times_out(self) -> None:
        started = asyncio.get_event_loop().time()
        woken = await farm_events.wait_for_wake(0.05)
        elapsed = asyncio.get_event_loop().time() - started
        self.assertFalse(woken)
        self.assertGreaterEqual(elapsed, 0.04)


if __name__ == "__main__":
    unittest.main()
