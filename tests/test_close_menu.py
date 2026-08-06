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

from app.core.ui_helpers import close_menu


def _callback(delete_ok: bool = True, edit_ok: bool = True):
    message = mock.Mock()
    message.delete = mock.AsyncMock(
        side_effect=None if delete_ok else Exception("message can't be deleted")
    )
    message.edit_text = mock.AsyncMock(
        side_effect=None if edit_ok else Exception("message to edit not found")
    )
    query = mock.Mock()
    query.message = message
    query.answer = mock.AsyncMock()
    return query, message


class CloseMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_without_leaving_a_notice(self) -> None:
        """Closing a menu should leave nothing behind in the chat."""
        query, message = _callback()
        await close_menu(query, "Settings closed.")
        message.delete.assert_awaited_once()
        message.edit_text.assert_not_awaited()
        query.answer.assert_awaited_once()

    async def test_falls_back_to_text_when_delete_is_refused(self) -> None:
        """Telegram won't delete messages older than 48h - don't leave a dead menu."""
        query, message = _callback(delete_ok=False)
        await close_menu(query, "Settings closed.")
        message.edit_text.assert_awaited_once()
        self.assertEqual(message.edit_text.await_args.args[0], "Settings closed.")
        self.assertIsNone(message.edit_text.await_args.kwargs["reply_markup"])
        query.answer.assert_awaited_once()

    async def test_survives_both_calls_failing(self) -> None:
        query, _ = _callback(delete_ok=False, edit_ok=False)
        await close_menu(query)
        query.answer.assert_awaited_once()

    async def test_handles_a_callback_without_a_message(self) -> None:
        query = mock.Mock()
        query.message = None
        query.answer = mock.AsyncMock()
        await close_menu(query)
        query.answer.assert_awaited_once()


class CloseButtonWiringTests(unittest.TestCase):
    """The buttons must point at handlers that actually exist."""

    def test_job_card_and_workers_list_close(self) -> None:
        from app.bot.handlers import jobs

        for job_id in ("job1", None):
            keyboard = jobs._build_job_info_keyboard([], job_id)
            payloads = [
                b.callback_data for row in keyboard.inline_keyboard for b in row
            ]
            self.assertIn("job_close", payloads, job_id)
            self.assertNotIn("jobs_back", payloads, job_id)

    def _jobs_rows(self, count: int, page: int) -> list[list[str]]:
        from app.bot.handlers import jobs

        combined = [
            {
                "_id": f"j{i}",
                "Stat": 3,
                "CompletedChunks": 58,
                "Props": {"Name": f"batch_{i}", "Tasks": 58, "BatchName": f"batch_{i}"},
            }
            for i in range(count)
        ]
        _, keyboard = jobs._build_jobs_overview(combined, page)
        self.assertIsNotNone(keyboard, "every jobs message needs a way out")
        return [[b.text for b in row] for row in keyboard.inline_keyboard]

    def test_paging_and_actions_are_on_separate_rows(self) -> None:
        """Update used to sit between Back and Next, splitting one control."""
        rows = self._jobs_rows(18, 1)
        self.assertEqual(rows[-1], ["✖️ Close", "🔄 Update"])
        nav = rows[-2]
        self.assertIn("⬅️ Back", nav)
        self.assertIn("Next ➡️", nav)
        self.assertNotIn("🔄 Update", nav)

    def test_actions_present_without_paging(self) -> None:
        for count in (4, 0):
            with self.subTest(jobs=count):
                self.assertEqual(
                    self._jobs_rows(count, 0)[-1], ["✖️ Close", "🔄 Update"]
                )

    def test_close_handlers_are_registered(self) -> None:
        from app.bot.handlers import jobs

        for data in ("job_close", "jobs_close", "workers_close", "tasks_close"):
            matched = any(
                any(f.callback(mock.Mock(data=data)) for f in handler.filters or [])
                for handler in jobs.router.callback_query.handlers
            )
            self.assertTrue(matched, f"no handler matches {data}")


if __name__ == "__main__":
    unittest.main()
