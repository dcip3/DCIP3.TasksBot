"""/login must always be a way back in.

The bot tells users to /login when Deadline starts rejecting their stored
credentials. Answering "You are already authorized." to that left them with a
warning they could not act on and monitoring stuck paused.
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


class LoginStartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.message = mock.Mock()
        self.message.from_user = mock.Mock(id=42)
        self.message.answer = mock.AsyncMock()
        self.state = mock.Mock()
        self.state.clear = mock.AsyncMock()
        self.state.set_state = mock.AsyncMock()

    async def _run(self, credentials, suspended: bool) -> str:
        with mock.patch.object(
            auth, "get_deadline_credentials", new=mock.AsyncMock(return_value=credentials)
        ), mock.patch.object(auth, "is_auth_suspended", return_value=suspended):
            await auth.cmd_login_start(self.message, self.state)
        return self.message.answer.await_args.args[0]

    def _assert_flow_started(self) -> None:
        self.state.set_state.assert_awaited_once_with(auth.LoginStates.USERNAME)
        keyboard = self.message.answer.await_args.kwargs.get("reply_markup")
        self.assertIsNotNone(keyboard, "user needs a way to cancel")

    async def test_rejected_credentials_can_be_replaced(self) -> None:
        text = await self._run(("tester", "pw"), suspended=True)
        self.assertNotIn("already authorized", text.lower())
        self.assertIn("rejecting", text.lower())
        self.assertIn("tester", text)
        self._assert_flow_started()

    async def test_healthy_session_can_still_re_login(self) -> None:
        text = await self._run(("tester", "pw"), suspended=False)
        self.assertNotIn("already authorized", text.lower())
        self.assertIn("tester", text)
        self._assert_flow_started()

    async def test_fresh_user_gets_the_plain_prompt(self) -> None:
        text = await self._run(None, suspended=False)
        self.assertEqual(text, "Enter your Deadline login:")
        self._assert_flow_started()

    async def test_missing_user_info_is_handled(self) -> None:
        self.message.from_user = None
        with mock.patch.object(
            auth, "get_deadline_credentials", new=mock.AsyncMock()
        ) as creds_mock:
            await auth.cmd_login_start(self.message, self.state)
        creds_mock.assert_not_awaited()
        self.state.set_state.assert_not_awaited()


class LoginClearsSuspensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_auth_lifts_the_pause(self) -> None:
        """Otherwise monitoring stays paused after a correct re-login."""
        from app.auth import service

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class _Session:
            def get(self, *args, **kwargs):
                return _Resp()

        with mock.patch.object(
            service, "get_aiosession", new=mock.AsyncMock(return_value=_Session())
        ), mock.patch(
            "app.services.deadline.clear_auth_suspension"
        ) as clear_mock, mock.patch(
            "app.storage.user_settings.clear_auth_failure_notice", new=mock.AsyncMock()
        ) as notice_mock:
            ok = await service.authenticate_user("tester", "pw", 42)

        self.assertTrue(ok)
        clear_mock.assert_called_once_with("tester")
        notice_mock.assert_awaited_once_with(42)


if __name__ == "__main__":
    unittest.main()
