"""/login must not report success when the credentials could not be stored.

Deadline can accept a password the bot then fails to save, for example with a
broken database or ENCRYPTION_KEY. Every later request reads the stored
credentials, so the user has to hear that the login did not stick.
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

from app.bot.handlers import auth


class LoginCredentialsNotStoredTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.message = mock.Mock()
        self.message.text = "secret-pw"
        self.message.from_user = mock.Mock(id=42)
        self.message.answer = mock.AsyncMock()
        self.message.delete = mock.AsyncMock()
        self.state = mock.Mock()
        self.state.get_data = mock.AsyncMock(return_value={"username": "tester"})
        self.state.clear = mock.AsyncMock()

    async def _run(self, saved: bool) -> None:
        with mock.patch.object(
            auth, "authenticate_user", new=mock.AsyncMock(return_value=True)
        ), mock.patch.object(
            auth, "save_deadline_credentials", new=mock.AsyncMock(return_value=saved)
        ), mock.patch.object(auth, "get_main_keyboard", return_value=None):
            await auth.process_login_password(self.message, self.state)

    async def test_failed_save_is_reported(self) -> None:
        await self._run(saved=False)

        self.message.answer.assert_awaited_once()
        text = self.message.answer.await_args.args[0]
        self.assertNotIn("Successfully authorized", text)
        self.assertIn("could not store", text)
        self.assertIn("bot admin", text)
        self.state.clear.assert_awaited_once()

    async def test_stored_login_still_succeeds(self) -> None:
        await self._run(saved=True)

        text = self.message.answer.await_args.args[0]
        self.assertIn("Successfully authorized", text)
        self.state.clear.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
