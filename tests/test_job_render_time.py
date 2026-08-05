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

from app.bot.handlers.jobs import (
    _build_job_info_text,
    _humanize_duration,
    _job_render_seconds,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


class DurationFormatTests(unittest.TestCase):
    def test_formats(self) -> None:
        self.assertEqual(_humanize_duration(38), "38 s")
        self.assertEqual(_humanize_duration(47 * 60), "47 min")
        self.assertEqual(_humanize_duration(8 * 3600 + 21 * 60 + 37), "8 h 21 min")
        # Long renders keep counting in hours rather than rolling into days.
        self.assertEqual(_humanize_duration(36 * 3600), "36 h 00 min")


class RenderSecondsTests(unittest.TestCase):
    def test_completed_job_uses_its_own_end_time(self) -> None:
        job = {
            "DateStart": "2026-07-14T04:09:42.859+00:00",
            "DateComp": "2026-07-14T05:03:30.441+00:00",
        }
        self.assertAlmostEqual(_job_render_seconds(job, NOW), 3227.582, places=2)

    def test_running_job_counts_up_to_now(self) -> None:
        job = {"DateStart": (NOW - timedelta(hours=2)).isoformat()}
        self.assertAlmostEqual(_job_render_seconds(job, NOW), 7200, delta=1)

    def test_queue_time_is_not_render_time(self) -> None:
        """A job can sit queued for hours; that is not time spent rendering."""
        job = {
            "Date": "2026-07-14T00:52:09+00:00",  # submitted
            "DateStart": "2026-07-14T04:09:42+00:00",
            "DateComp": "2026-07-14T05:03:30+00:00",
        }
        self.assertLess(_job_render_seconds(job, NOW), 3600)

    def test_never_started(self) -> None:
        self.assertIsNone(_job_render_seconds({}, NOW))
        self.assertIsNone(_job_render_seconds({"DateStart": "0001-01-01T00:00:00Z"}, NOW))


class JobCardTests(unittest.TestCase):
    def _card(self, **kwargs) -> str:
        base = dict(
            batch_name="SHA_0080_EB_v019",
            name="SHA_0080_crypto_v05",
            stat=3,
            stat_name="Completed",
            progress_str="100% 107/107",
            errors_count=0,
            eta_str="N/A",
        )
        base.update(kwargs)
        return _build_job_info_text(**base)

    def test_completed_job_keeps_showing_how_long_it_took(self) -> None:
        card = self._card(render_seconds=8057)
        self.assertIn("⏱️ 2 h 14 min", card)
        self.assertNotIn("left", card)

    def test_spent_and_remaining_share_one_clock_line(self) -> None:
        """Two clock icons read as two unrelated metrics - keep it to one."""
        card = self._card(
            stat=1, stat_name="Active", eta_str="1:47:09", render_seconds=15132
        )
        self.assertIn("⏱️ 4 h 12 min · 1 h 47 min left", card)
        self.assertEqual(card.count("⏱️"), 1)
        self.assertNotIn("⌛", card)

    def test_running_job_without_an_eta_still_shows_elapsed(self) -> None:
        card = self._card(stat=1, stat_name="Active", eta_str="N/A", render_seconds=95)
        self.assertIn("⏱️ 1 min", card)
        self.assertNotIn("left", card)

    def test_omitted_when_unknown(self) -> None:
        self.assertNotIn("⏱️", self._card())


if __name__ == "__main__":
    unittest.main()
