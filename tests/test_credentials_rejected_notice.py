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

from app.services import deadline


class CredentialsRejectedNoticeTests(unittest.IsolatedAsyncioTestCase):
    """A 401 answers "why is this list empty?" better than three guesses do."""

    def setUp(self) -> None:
        deadline._credentials_rejected_notice.clear()
        # The notice is recorded by the same call that sends the warning, so
        # every test here goes through it with Telegram stubbed out.
        self.bot_patcher = mock.patch("app.core.bot_core.bot")
        self.fake_bot = self.bot_patcher.start()
        self.fake_bot.send_message = mock.AsyncMock()
        self.addCleanup(self.bot_patcher.stop)

    async def test_notice_is_recorded_and_read_once(self) -> None:
        await deadline._notify_rejected_credentials(7)
        # The first empty listing after the 401 keeps quiet; a later one, from
        # a farm that simply has no jobs, gets the ordinary explanation.
        self.assertTrue(deadline.take_credentials_rejected_notice(7))
        self.assertFalse(deadline.take_credentials_rejected_notice(7))

    async def test_notice_belongs_to_the_user_who_got_it(self) -> None:
        await deadline._notify_rejected_credentials(7)
        self.assertFalse(deadline.take_credentials_rejected_notice(8))

    async def test_stale_notice_is_ignored(self) -> None:
        await deadline._notify_rejected_credentials(7)
        stamped = deadline._credentials_rejected_notice[7]
        deadline._credentials_rejected_notice[7] = (
            stamped - deadline._CREDENTIALS_NOTICE_TTL_SECONDS - 1
        )
        # An unread notice from an hour ago says nothing about this listing.
        self.assertFalse(deadline.take_credentials_rejected_notice(7))

    async def test_notice_survives_a_failed_send(self) -> None:
        self.fake_bot.send_message = mock.AsyncMock(side_effect=RuntimeError("no chat"))
        await deadline._notify_rejected_credentials(7)
        # Telegram being unreachable is no reason to then guess out loud.
        self.assertTrue(deadline.take_credentials_rejected_notice(7))


class DismissAuthFailureNotificationTests(unittest.TestCase):
    """Someone typing /login has already been asked; do not ask again."""

    def setUp(self) -> None:
        deadline._auth_notify_pending.clear()
        deadline._auth_already_notified.clear()

    def test_pending_warning_is_dropped(self) -> None:
        deadline._auth_notify_pending.add("nodea")
        deadline.dismiss_auth_failure_notification("NodeA")
        self.assertFalse(deadline.pop_auth_failure_notification("nodea"))

    def test_dismissing_nothing_is_harmless(self) -> None:
        deadline.dismiss_auth_failure_notification("nobody")
        self.assertFalse(deadline.pop_auth_failure_notification("nobody"))


if __name__ == "__main__":
    unittest.main()
