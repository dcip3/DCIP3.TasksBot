import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

import aiosqlite

from app.storage import user_settings


class AuthFailureNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.conn = await aiosqlite.connect(":memory:")
        await self.conn.execute(
            """
            CREATE TABLE user_sessions (
                telegram_user_id INTEGER UNIQUE,
                auth_failure_notified_at INTEGER
            )
            """
        )
        await self.conn.execute("INSERT INTO user_sessions (telegram_user_id) VALUES (1)")
        await self.conn.commit()
        self.patcher = mock.patch.object(
            user_settings, "get_db_connection", return_value=self.conn
        )
        self.patcher.start()

    async def asyncTearDown(self) -> None:
        self.patcher.stop()
        await self.conn.close()

    async def test_first_warning_allowed_then_suppressed(self) -> None:
        self.assertTrue(await user_settings.claim_auth_failure_notice(1))
        # A bot restart would ask again immediately: must stay silent.
        self.assertFalse(await user_settings.claim_auth_failure_notice(1))
        self.assertFalse(await user_settings.claim_auth_failure_notice(1))

    async def test_never_reminded_again_without_login(self) -> None:
        self.assertTrue(await user_settings.claim_auth_failure_notice(1))
        # Even much later (and after restarts) the background warning stays silent.
        ancient = int(time.time()) - 30 * 24 * 60 * 60
        await self.conn.execute(
            "UPDATE user_sessions SET auth_failure_notified_at = ? WHERE telegram_user_id = 1",
            (ancient,),
        )
        await self.conn.commit()
        self.assertFalse(await user_settings.claim_auth_failure_notice(1))

    async def test_successful_login_clears_notice(self) -> None:
        self.assertTrue(await user_settings.claim_auth_failure_notice(1))
        self.assertFalse(await user_settings.claim_auth_failure_notice(1))

        await user_settings.clear_auth_failure_notice(1)
        self.assertTrue(await user_settings.claim_auth_failure_notice(1))

    async def test_db_failure_does_not_spam(self) -> None:
        with mock.patch.object(user_settings, "get_db_connection", return_value=None):
            # No database at all: allow the warning (nothing to remember).
            self.assertTrue(await user_settings.claim_auth_failure_notice(1))

        broken = mock.MagicMock()
        broken.execute.side_effect = RuntimeError("db down")
        with mock.patch.object(user_settings, "get_db_connection", return_value=broken):
            self.assertFalse(await user_settings.claim_auth_failure_notice(1))


if __name__ == "__main__":
    unittest.main()
