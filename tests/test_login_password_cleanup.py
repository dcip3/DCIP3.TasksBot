"""/login must not leave the password in the chat.

The password step reads the password from an ordinary message, so the bot
deletes that message once it has it. The delete is best effort: when Telegram
refuses it the login still has to go through.
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

from aiogram.exceptions import TelegramBadRequest

from app.bot.handlers import auth


class LoginPasswordCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.message = mock.Mock()
        self.message.text = " secret-pw "
        self.message.from_user = mock.Mock(id=42)
        self.message.answer = mock.AsyncMock()
        self.message.delete = mock.AsyncMock()
        self.state = mock.Mock()
        self.state.get_data = mock.AsyncMock(return_value={"username": "tester"})
        self.state.clear = mock.AsyncMock()

    async def _run(self, auth_ok: bool = True) -> mock.AsyncMock:
        save_mock = mock.AsyncMock()
        with mock.patch.object(
            auth, "authenticate_user", new=mock.AsyncMock(return_value=auth_ok)
        ), mock.patch.object(
            auth, "save_deadline_credentials", new=save_mock
        ), mock.patch.object(auth, "get_main_keyboard", return_value=None):
            await auth.process_login_password(self.message, self.state)
        return save_mock

    async def test_password_message_is_deleted(self) -> None:
        save_mock = await self._run()
        self.message.delete.assert_awaited_once()
        save_mock.assert_awaited_once_with(42, "tester", "secret-pw")

    async def test_failed_delete_does_not_break_login(self) -> None:
        self.message.delete.side_effect = TelegramBadRequest(
            method=mock.Mock(), message="Bad Request: message can't be deleted"
        )
        with self.assertLogs(auth.logger, level="WARNING"):
            save_mock = await self._run()
        save_mock.assert_awaited_once_with(42, "tester", "secret-pw")
        text = self.message.answer.await_args.args[0]
        self.assertIn("Successfully authorized", text)
        self.state.clear.assert_awaited_once()

    async def test_rejected_password_is_deleted_too(self) -> None:
        save_mock = await self._run(auth_ok=False)
        self.message.delete.assert_awaited_once()
        save_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
