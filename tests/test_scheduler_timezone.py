"""The maintenance jobs run in a configurable time zone.

SCHEDULER_TIMEZONE defaults to UTC. A name zoneinfo does not know has to stop
the bot at startup with an error that names the setting. Every job follows the
setting, not only the scheduler: a trigger built without a zone would run in
the host's local zone.
"""

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

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import ValidationError

from app.core import lifecycle
from app.core.config import Settings, settings


class SchedulerTimezoneTests(unittest.TestCase):
    def test_defaults_to_utc(self) -> None:
        self.assertEqual(Settings.model_fields["scheduler_timezone"].default, "UTC")

    def test_accepts_an_iana_zone(self) -> None:
        configured = Settings(scheduler_timezone=" Europe/Berlin ")
        self.assertEqual(configured.scheduler_timezone, "Europe/Berlin")

    def test_refuses_an_unknown_zone(self) -> None:
        for name in ("Mars/Olympus", "", "../etc/passwd"):
            with self.subTest(name=name):
                with self.assertRaises(ValidationError) as ctx:
                    Settings(scheduler_timezone=name)
                self.assertIn("scheduler_timezone", str(ctx.exception))

    def test_scheduler_uses_the_setting(self) -> None:
        self.assertEqual(str(lifecycle.scheduler.timezone), settings.scheduler_timezone)

    def test_every_job_follows_the_setting(self) -> None:
        # Neither UTC nor a likely host zone, so a trigger that fell back to
        # the host's zone cannot pass by coincidence.
        with mock.patch.object(
            settings, "scheduler_timezone", "Pacific/Chatham"
        ), mock.patch.object(settings, "preview_upload_enabled", True):
            scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)
            with mock.patch.object(lifecycle, "scheduler", scheduler):
                lifecycle._schedule_maintenance_jobs()
            jobs = scheduler.get_jobs()

            ids = {job.id for job in jobs}
            self.assertIn("cleanup_old_files", ids)
            self.assertIn("recover_preview_uploads", ids)
            for job in jobs:
                with self.subTest(job=job.id):
                    self.assertEqual(
                        str(job.trigger.timezone), settings.scheduler_timezone
                    )


if __name__ == "__main__":
    unittest.main()
