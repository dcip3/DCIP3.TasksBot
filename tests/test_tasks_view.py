"""Task breakdown: narrow enough for a phone, short enough for Telegram."""

import os
import re
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
    TASKS_PAGE_SIZE,
    _build_tasks_view,
    _parse_tasks_callback,
    _task_duration,
)

# Telegram rejects messages longer than this.
TELEGRAM_LIMIT = 4096
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def task(index: int, first: int, stat: int = 2, prog: str = "0 %", minutes=None) -> dict:
    entry = {
        "TaskID": index,
        "Frames": f"{first}-{first + 4}",
        "Stat": stat,
        "Prog": prog,
    }
    if minutes is not None:
        start = NOW - timedelta(minutes=minutes)
        entry["StartRen"] = start.isoformat()
        if stat == 5:
            entry["Comp"] = NOW.isoformat()
    return entry


def body_lines(text: str) -> list[str]:
    return re.sub(r"</?pre>", "", text).split("\n")


class WidthTests(unittest.TestCase):
    def test_fits_a_phone(self) -> None:
        """The old layout was 43 characters and wrapped by one on mobile."""
        tasks = [task(i, 921 + i * 5, 5, "100 %", 4) for i in range(58)]
        text, _ = _build_tasks_view(tasks, "job1", 0)
        widest = max(len(line) for line in body_lines(text))
        self.assertLessEqual(widest, 32, f"too wide:\n{text}")

    def test_header_lines_up_with_the_rows(self) -> None:
        tasks = [task(0, 921, 5, "100 %", 4), task(1, 1206, 5, "100 %", 83)]
        lines = body_lines(_build_tasks_view(tasks, "job1", 0)[0])
        header, first_row = lines[0], lines[2]
        # "Frames" header starts where the frame column starts (after the icon).
        self.assertTrue(header.startswith("Frames"))
        self.assertIn("921-925", first_row)
        # Prog column right-aligned to the same edge in header and rows.
        self.assertEqual(header.index("Prog") + len("Prog"), first_row.index("%") + 1)

    def test_no_trailing_whitespace(self) -> None:
        tasks = [task(0, 921), task(1, 926, 5, "100 %", 4)]
        for line in body_lines(_build_tasks_view(tasks, "job1", 0)[0]):
            self.assertEqual(line, line.rstrip(), repr(line))


class DurationTests(unittest.TestCase):
    def test_minutes_and_hours(self) -> None:
        self.assertEqual(_task_duration(task(0, 921, 5, "100 %", 3.75), NOW), "3:45")
        self.assertEqual(_task_duration(task(0, 921, 5, "100 %", 83.5), NOW), "1:23:30")

    def test_running_task_counts_up(self) -> None:
        self.assertEqual(_task_duration(task(0, 921, 4, "20 %", 2), NOW), "2:00")

    def test_queued_task_has_none(self) -> None:
        self.assertEqual(_task_duration(task(0, 921), NOW), "")

    def test_unstarted_sentinel_is_ignored(self) -> None:
        entry = task(0, 921, 5, "100 %")
        entry["StartRen"] = "0001-01-01T00:00:00Z"
        self.assertEqual(_task_duration(entry, NOW), "")


class PaginationTests(unittest.TestCase):
    def test_one_page_for_a_typical_job(self) -> None:
        tasks = [task(i, 921 + i * 5, 5, "100 %", 4) for i in range(58)]
        text, keyboard = _build_tasks_view(tasks, "job1", 0)
        self.assertNotIn("Page 1 of", text)
        payloads = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertNotIn("tasks_page:job1:1", payloads)

    def test_splits_a_frame_by_frame_job(self) -> None:
        """290 frames chunked one at a time used to blow the message limit."""
        tasks = [
            {"TaskID": i, "Frames": str(921 + i), "Stat": 2, "Prog": "0 %"}
            for i in range(290)
        ]
        expected_pages = (290 + TASKS_PAGE_SIZE - 1) // TASKS_PAGE_SIZE
        seen = 0
        for page in range(expected_pages):
            text, _ = _build_tasks_view(tasks, "job1", page)
            self.assertLess(len(text), TELEGRAM_LIMIT, f"page {page} too long")
            self.assertIn(f"Page {page+1} of {expected_pages}", text)
            seen += len(body_lines(text)) - 3  # header, separator, page footer
        self.assertEqual(seen, 290, "every task must appear on exactly one page")

    def test_worst_case_row_still_fits(self) -> None:
        """Long frame numbers plus multi-hour times, a full page of them."""
        tasks = [
            {
                "TaskID": i,
                "Frames": f"{100000 + i * 5}-{100004 + i * 5}",
                "Stat": 5,
                "Prog": "100 %",
                "StartRen": (NOW - timedelta(hours=12)).isoformat(),
                "Comp": NOW.isoformat(),
            }
            for i in range(TASKS_PAGE_SIZE)
        ]
        text, _ = _build_tasks_view(tasks, "job1", 0)
        self.assertLess(len(text), TELEGRAM_LIMIT)

    def test_page_out_of_range_is_clamped(self) -> None:
        tasks = [task(i, 921 + i * 5) for i in range(10)]
        text, _ = _build_tasks_view(tasks, "job1", 99)
        self.assertIn("921-925", text)


class KeyboardTests(unittest.TestCase):
    def test_close_and_update_always_present(self) -> None:
        tasks = [task(i, 921 + i * 5) for i in range(5)]
        _, keyboard = _build_tasks_view(tasks, "job1", 0)
        payloads = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn("tasks_close", payloads)
        self.assertIn("tasks_page:job1:0", payloads)  # Update re-renders this page

    def test_navigation_appears_only_when_needed(self) -> None:
        tasks = [task(i, 921 + i * 5) for i in range(TASKS_PAGE_SIZE + 5)]
        _, first = _build_tasks_view(tasks, "job1", 0)
        _, second = _build_tasks_view(tasks, "job1", 1)
        first_payloads = [b.callback_data for row in first.inline_keyboard for b in row]
        second_payloads = [b.callback_data for row in second.inline_keyboard for b in row]
        self.assertIn("tasks_page:job1:1", first_payloads)
        self.assertIn("tasks_page:job1:0", second_payloads)


class CallbackParsingTests(unittest.TestCase):
    def test_old_and_new_payloads(self) -> None:
        """Job cards already in chats emit the page-less form."""
        self.assertEqual(_parse_tasks_callback("tasks_job:abc123"), ("abc123", 0))
        self.assertEqual(_parse_tasks_callback("tasks_page:abc123:3"), ("abc123", 3))

    def test_garbage_page_falls_back_to_the_first(self) -> None:
        self.assertEqual(_parse_tasks_callback("tasks_page:abc123:x"), ("abc123", 0))
        self.assertEqual(_parse_tasks_callback("tasks_page:abc123:-2"), ("abc123", 0))


if __name__ == "__main__":
    unittest.main()
