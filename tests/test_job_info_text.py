import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.bot.handlers.jobs import (
    _build_job_info_text,
    _humanize_eta,
    _progress_bar,
)


def card(**overrides) -> str:
    params = dict(
        batch_name="SHB_city_ID_v019",
        name="SHB_0000_main_v02",
        stat=1,
        stat_name="Active",
        progress_str="53% 156/290",
        errors_count=0,
        eta_str="1:47:09",
    )
    params.update(overrides)
    return _build_job_info_text(**params)


class ProgressBarTests(unittest.TestCase):
    def test_bar_endpoints_and_width(self) -> None:
        self.assertEqual(_progress_bar(0, width=10), "░" * 10)
        self.assertEqual(_progress_bar(100, width=10), "█" * 10)
        self.assertEqual(len(_progress_bar(37, width=10)), 10)

    def test_bar_clamps_out_of_range(self) -> None:
        self.assertEqual(_progress_bar(-5, width=5), "░" * 5)
        self.assertEqual(_progress_bar(150, width=5), "█" * 5)


class EtaTests(unittest.TestCase):
    def test_humanized_forms(self) -> None:
        self.assertEqual(_humanize_eta("1:47:09"), "1 h 47 min")
        self.assertEqual(_humanize_eta("0:04:31"), "4 min")
        self.assertEqual(_humanize_eta("2:00:00"), "2 h")
        self.assertEqual(_humanize_eta("0:00:12"), "less than a minute")
        self.assertEqual(_humanize_eta("0:00:00"), "finishing")

    def test_unknown_value_passes_through(self) -> None:
        self.assertEqual(_humanize_eta("N/A"), "N/A")


class JobCardTests(unittest.TestCase):
    def test_active_job_layout(self) -> None:
        text = card()
        lines = text.split("\n")
        self.assertEqual(lines[0], "<b>SHB_0000_main_v02</b>")  # name is the title
        self.assertIn("SHB_city_ID_v019", lines[1])
        self.assertEqual(lines[2], "")  # blank line separates identity from state
        self.assertIn("🟢 Active", lines[3])
        self.assertIn("53% · 156/290", lines[4])
        self.assertIn("1 h 47 min left", text)

    def test_zero_errors_are_not_shown(self) -> None:
        self.assertNotIn("error", card())

    def test_errors_are_shown_with_plural(self) -> None:
        self.assertIn("❌ 1 error", card(errors_count=1))
        self.assertIn("❌ 3 errors", card(errors_count=3))

    def test_eta_only_for_running_jobs(self) -> None:
        self.assertNotIn("left", card(stat=3, stat_name="Completed", eta_str="N/A"))
        self.assertNotIn("N/A", card(stat=1, stat_name="Active", eta_str="N/A"))

    def test_status_icons(self) -> None:
        self.assertIn("✅ Completed", card(stat=3, stat_name="Completed"))
        self.assertIn("🔴 Failed", card(stat=4, stat_name="Failed"))
        self.assertIn("⏸️ Suspended", card(stat=2, stat_name="Suspended"))
        self.assertIn("⏳ Pending", card(stat=6, stat_name="Pending"))

    def test_unparsable_progress_falls_back(self) -> None:
        self.assertIn("waiting for tasks", card(progress_str="waiting for tasks"))

    def test_html_is_escaped(self) -> None:
        text = card(name="shot <b>x</b>", batch_name="batch & co")
        self.assertIn("shot &lt;b&gt;x&lt;/b&gt;", text)
        self.assertIn("batch &amp; co", text)


if __name__ == "__main__":
    unittest.main()
