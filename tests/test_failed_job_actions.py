"""Failed jobs need their own command.

Verified against the live farm: sending "requeue" or "resume" to a Failed job
returns HTTP 200 Success and leaves the job Failed. Only "resumefailed" puts it
back to work. That silent no-op is what made the Requeue button look broken.
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

from app.bot.handlers import jobs
from app.services import deadline

JOB_FAILED = 4
JOB_ACTIVE = 1
JOB_SUSPENDED = 2
JOB_COMPLETED = 3


def _payloads(stat: int) -> list[str]:
    buttons = jobs._build_job_action_buttons("job1", stat, False, None)
    return [b.callback_data.split(":", 1)[0] for b in buttons]


class FailedJobKeyboardTests(unittest.TestCase):
    def test_failed_job_offers_resume_failed(self) -> None:
        payloads = _payloads(JOB_FAILED)
        self.assertIn("resume_failed_job", payloads)

    def test_failed_job_does_not_offer_the_no_op_actions(self) -> None:
        payloads = _payloads(JOB_FAILED)
        self.assertNotIn("requeue_job", payloads)
        self.assertNotIn("suspend_job", payloads)

    def test_other_states_are_unchanged(self) -> None:
        self.assertIn("suspend_job", _payloads(JOB_ACTIVE))
        self.assertIn("requeue_job", _payloads(JOB_ACTIVE))
        self.assertIn("resume_job", _payloads(JOB_SUSPENDED))
        self.assertNotIn("resume_failed_job", _payloads(JOB_ACTIVE))
        self.assertNotIn("requeue_job", _payloads(JOB_COMPLETED))


class ResumeFailedCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_the_resumefailed_command(self) -> None:
        self.assertIn("resumefailed", deadline._PUT_JOB_COMMANDS)

        with mock.patch.object(
            deadline, "_put_job_command", new=mock.AsyncMock(return_value=True)
        ) as put_mock, mock.patch(
            "app.auth.get_deadline_credentials",
            new=mock.AsyncMock(return_value=("tester", "pw")),
        ):
            ok = await deadline.resume_failed_job_by_user_id(42, "job1")

        self.assertTrue(ok)
        self.assertEqual(put_mock.await_args.args[2], "resumefailed")


class LegacyRequeueButtonTests(unittest.IsolatedAsyncioTestCase):
    """Cards already in chats still show Requeue on failed jobs."""

    def _query(self):
        query = mock.Mock()
        query.from_user = mock.Mock(id=42)
        query.data = "requeue_job:job1"
        query.answer = mock.AsyncMock()
        return query

    async def test_requeue_on_a_failed_job_is_rerouted(self) -> None:
        query = self._query()
        with mock.patch.object(
            jobs,
            "get_job_info_by_user_id",
            new=mock.AsyncMock(return_value={"Stat": JOB_FAILED}),
        ), mock.patch.object(
            jobs, "resume_failed_job_by_user_id", new=mock.AsyncMock(return_value=True)
        ) as resume_mock, mock.patch.object(
            jobs, "requeue_job_by_user_id", new=mock.AsyncMock(return_value=True)
        ) as requeue_mock:
            await jobs.requeue_job_callback(query)

        resume_mock.assert_awaited_once_with(42, "job1")
        requeue_mock.assert_not_awaited()

    async def test_requeue_on_a_healthy_job_still_requeues(self) -> None:
        query = self._query()
        with mock.patch.object(
            jobs,
            "get_job_info_by_user_id",
            new=mock.AsyncMock(return_value={"Stat": JOB_ACTIVE}),
        ), mock.patch.object(
            jobs, "resume_failed_job_by_user_id", new=mock.AsyncMock(return_value=True)
        ) as resume_mock, mock.patch.object(
            jobs, "requeue_job_by_user_id", new=mock.AsyncMock(return_value=True)
        ) as requeue_mock:
            await jobs.requeue_job_callback(query)

        requeue_mock.assert_awaited_once_with(42, "job1")
        resume_mock.assert_not_awaited()

    async def test_unreadable_job_falls_back_to_requeue(self) -> None:
        query = self._query()
        with mock.patch.object(
            jobs, "get_job_info_by_user_id", new=mock.AsyncMock(return_value=None)
        ), mock.patch.object(
            jobs, "requeue_job_by_user_id", new=mock.AsyncMock(return_value=True)
        ) as requeue_mock:
            await jobs.requeue_job_callback(query)
        requeue_mock.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
