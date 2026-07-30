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


class InteractiveNotificationTests(unittest.IsolatedAsyncioTestCase):
    """User-initiated requests must report rejected credentials every time."""

    def setUp(self) -> None:
        deadline.clear_auth_suspension("tester")

    async def _run(self, call) -> list:
        sent: list = []

        async def fake_notify(user_id: int) -> None:
            sent.append(user_id)

        with mock.patch.object(
            deadline, "_notify_rejected_credentials", side_effect=fake_notify
        ), mock.patch(
            "app.auth.get_deadline_credentials",
            new=mock.AsyncMock(return_value=("tester", "pw")),
        ):
            await deadline._with_user_credentials(
                42, default=[], operation_name="test op", call=call
            )
        return sent

    async def test_notifies_on_fresh_401(self) -> None:
        async def call(login: str, password: str):
            deadline._record_auth_failure(login)  # simulates a 401 response
            return []

        self.assertEqual(await self._run(call), [42])
        # Every further attempt notifies again, so the user always gets an answer.
        self.assertEqual(await self._run(call), [42])

    async def test_silent_when_request_succeeds(self) -> None:
        async def call(login: str, password: str):
            deadline._record_auth_success(login)
            return [{"_id": "job"}]

        self.assertEqual(await self._run(call), [])


class AuthSuspensionTests(unittest.TestCase):
    def setUp(self) -> None:
        deadline.clear_auth_suspension("tester")

    def test_suspends_after_threshold_and_notifies_once(self) -> None:
        for _ in range(deadline._AUTH_FAILURE_THRESHOLD - 1):
            deadline._record_auth_failure("tester")
            self.assertFalse(deadline.is_auth_suspended("tester"))

        deadline._record_auth_failure("tester")
        self.assertTrue(deadline.is_auth_suspended("Tester"))  # case-insensitive
        self.assertTrue(deadline.pop_auth_failure_notification("tester"))
        self.assertFalse(deadline.pop_auth_failure_notification("tester"))

    def test_success_resets_counter(self) -> None:
        deadline._record_auth_failure("tester")
        deadline._record_auth_failure("tester")
        deadline._record_auth_success("tester")
        deadline._record_auth_failure("tester")
        self.assertFalse(deadline.is_auth_suspended("tester"))

    def test_relogin_clears_suspension(self) -> None:
        for _ in range(deadline._AUTH_FAILURE_THRESHOLD):
            deadline._record_auth_failure("tester")
        self.assertTrue(deadline.is_auth_suspended("tester"))
        deadline.clear_auth_suspension("tester")
        self.assertFalse(deadline.is_auth_suspended("tester"))
        self.assertFalse(deadline.pop_auth_failure_notification("tester"))

    def test_retry_probe_after_expiry_without_renotify(self) -> None:
        for _ in range(deadline._AUTH_FAILURE_THRESHOLD):
            deadline._record_auth_failure("tester")
        self.assertTrue(deadline.is_auth_suspended("tester"))
        self.assertTrue(deadline.pop_auth_failure_notification("tester"))

        # Expire the suspension window: a single probe is allowed again.
        deadline._auth_suspended_until[deadline._auth_key("tester")] = 0.0
        self.assertFalse(deadline.is_auth_suspended("tester"))

        # The probe fails again: re-suspended, but the user is NOT re-notified.
        deadline._record_auth_failure("tester")
        self.assertTrue(deadline.is_auth_suspended("tester"))
        self.assertFalse(deadline.pop_auth_failure_notification("tester"))


if __name__ == "__main__":
    unittest.main()
