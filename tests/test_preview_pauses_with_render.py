"""What a paused render does to the preview waiting on it.

On 2026-09-28 two renders were paused and their previews stayed Pending beside
them. The preview stays Pending on purpose. Checked on the farm with throwaway
jobs: "resume" on a suspended preview queues it at once, past its unfinished
render, while "resume" leaves a Pending job alone - so a suspended preview
would start early the moment someone resumed the render's batch in Monitor.
What changed:
- the hour a paused render keeps its preview is timed from the database, so a
  restart no longer starts it over (every deploy did);
- a preview already building its video outlives the render being deleted;
- the chat's Resume hands a waiting preview back to its render instead of
  queueing it past it;
- the job list shows a paused render with its waiting preview as Suspended.
"""

import os
import sys
import tempfile
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

from app.bot import job_helpers
from app.core.config import settings
from app.services import deadline, job_watcher
from app.storage import database, preview_pause

ACTIVE, SUSPENDED, COMPLETED, PENDING = 1, 2, 3, 6
GRACE = job_watcher._SUSPENDED_SOURCE_GRACE_SECONDS


def render(stat: int, job_id: str = "src1") -> dict:
    return {"_id": job_id, "Stat": stat, "Props": {"Name": "Shot", "Batch": "Shot"}}


def preview(stat: int, job_id: str = "prev1", source: str = "src1") -> dict:
    return {
        "_id": job_id,
        "Stat": stat,
        "Props": {
            "Name": "Shot - Preview",
            "Batch": "Shot",
            "ExDic": {"PreviewJob": "1", "PreviewSource": source, "PreviewPresubmit": "1"},
        },
    }


def _context(props, telegram_user_id):
    return ("", "", "", 42, {}, props["ExDic"]["PreviewSource"])


def paused(preview_id: str = "prev1", seconds_ago: float = 0) -> preview_pause.PreviewPause:
    return preview_pause.PreviewPause(preview_id, "src1", 42, int(time.time() - seconds_ago))


def _never(name: str):
    return mock.AsyncMock(side_effect=AssertionError(f"{name} must not be sent to a preview"))


class _Farm:
    """Deadline and the pause table, as the reconcile sees them."""

    def __init__(self) -> None:
        self.records: dict[str, preview_pause.PreviewPause] = {}
        self.delete = mock.AsyncMock(return_value=True)
        self.readable = True
        self.writable = True

    async def _list(self, telegram_user_id):
        if not self.readable:
            raise RuntimeError("Database is not initialised")
        return dict(self.records)

    async def _record(self, preview_id, source_id, telegram_user_id):
        if not self.writable:
            raise RuntimeError("database is locked")
        self.records.setdefault(preview_id, paused(preview_id))

    async def _drop(self, preview_id):
        if not self.writable:
            raise RuntimeError("database is locked")
        self.records.pop(preview_id, None)

    async def reconcile(self, jobs: list) -> None:
        user = mock.Mock(telegram_user_id=42, login="tester", password="pw")
        with mock.patch.object(
            job_watcher, "_strand_check", new=mock.AsyncMock(return_value=set())
        ), mock.patch(
            "app.services.preview.runtime._extract_preview_context", side_effect=_context
        ), mock.patch.object(
            job_watcher, "pop_preview_message", return_value=None
        ), mock.patch.object(deadline, "delete_job", new=self.delete), mock.patch.object(
            deadline, "suspend_job", new=_never("suspend")
        ), mock.patch.object(deadline, "resume_job", new=_never("resume")), mock.patch.object(
            deadline, "pend_job", new=_never("pend")
        ), mock.patch.object(preview_pause, "list_pauses", new=self._list), mock.patch.object(
            preview_pause, "record_pause", new=self._record
        ), mock.patch.object(preview_pause, "drop_pause", new=self._drop), mock.patch.object(
            job_watcher, "_unregister_auto_preview_history", new=mock.AsyncMock()
        ):
            await job_watcher._reconcile_previews(user, jobs)

    def deleted(self) -> list[str]:
        return [call.args[2] for call in self.delete.await_args_list]


class PausedRenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_new_pause_is_noted_and_the_preview_kept(self) -> None:
        farm = _Farm()
        await farm.reconcile([render(SUSPENDED), preview(PENDING)])
        self.assertIn("prev1", farm.records)
        self.assertEqual(farm.deleted(), [])

    async def test_a_short_pause_keeps_the_preview(self) -> None:
        farm = _Farm()
        farm.records["prev1"] = paused(seconds_ago=GRACE - 120)
        await farm.reconcile([render(SUSPENDED), preview(PENDING)])
        self.assertEqual(farm.deleted(), [])

    async def test_a_render_left_paused_lets_its_preview_go(self) -> None:
        farm = _Farm()
        farm.records["prev1"] = paused(seconds_ago=GRACE + 1)
        await farm.reconcile([render(SUSPENDED), preview(PENDING)])
        self.assertEqual(farm.deleted(), ["prev1"])
        self.assertEqual(farm.records, {})

    async def test_a_restart_does_not_start_the_hour_over(self) -> None:
        """The clock is the record in the database; nothing in memory is needed."""
        farm = _Farm()
        farm.records["prev1"] = paused(seconds_ago=GRACE + 1)
        # A fresh process: the very first pass already knows how long it has been.
        await farm.reconcile([render(SUSPENDED), preview(PENDING)])
        self.assertEqual(farm.deleted(), ["prev1"])

    async def test_each_preview_keeps_its_own_clock(self) -> None:
        """One account's deletion used to restart another account's hour."""
        farm = _Farm()
        farm.records["prev1"] = paused("prev1", seconds_ago=GRACE + 1)
        await farm.reconcile(
            [render(SUSPENDED), preview(PENDING, "prev1"), preview(PENDING, "prev2")]
        )
        self.assertEqual(farm.deleted(), ["prev1"])
        self.assertIn("prev2", farm.records)

    async def test_resuming_the_render_clears_the_pause(self) -> None:
        farm = _Farm()
        farm.records["prev1"] = paused(seconds_ago=GRACE + 1)
        await farm.reconcile([render(ACTIVE), preview(PENDING)])
        self.assertEqual(farm.records, {})
        self.assertEqual(farm.deleted(), [])

    async def test_the_preview_is_never_suspended_or_resumed(self) -> None:
        """Pending is the one state Resume in Monitor cannot release early."""
        farm = _Farm()
        for stat in (SUSPENDED, ACTIVE, SUSPENDED, COMPLETED):
            await farm.reconcile([render(stat), preview(PENDING)])

    async def test_deleting_a_render_deletes_its_waiting_preview(self) -> None:
        farm = _Farm()
        await farm.reconcile([preview(PENDING)])
        self.assertEqual(farm.deleted(), ["prev1"])

    async def test_a_preview_already_building_outlives_its_render(self) -> None:
        """Deleting a finished render must not kill the preview of its frames."""
        farm = _Farm()
        await farm.reconcile([preview(ACTIVE)])
        self.assertEqual(farm.deleted(), [])

    async def test_records_of_vanished_previews_are_dropped(self) -> None:
        farm = _Farm()
        farm.records["gone"] = paused("gone")
        await farm.reconcile([render(ACTIVE)])
        self.assertEqual(farm.records, {})

    async def test_an_unread_listing_drops_nothing(self) -> None:
        farm = _Farm()
        farm.records["prev1"] = paused()
        await farm.reconcile([])
        self.assertIn("prev1", farm.records)

    async def test_without_the_table_a_paused_render_keeps_its_preview(self) -> None:
        farm = _Farm()
        farm.readable = False
        await farm.reconcile([render(SUSPENDED), preview(PENDING)])
        self.assertEqual(farm.deleted(), [])

    async def test_a_failing_write_does_not_stop_the_rest_of_the_pass(self) -> None:
        farm = _Farm()
        farm.writable = False
        farm.records["gone"] = paused("gone")
        await farm.reconcile(
            [render(SUSPENDED), preview(PENDING), preview(PENDING, "prev2", source="deleted")]
        )
        # The later preview, whose render is gone, is still dealt with.
        self.assertEqual(farm.deleted(), ["prev2"])


class RefusalTests(unittest.IsolatedAsyncioTestCase):
    """Deadline refuses some commands with 200 and a body starting "Error"."""

    async def _send(self, body: str, call):
        class _Resp:
            status = 200

            async def text(self):
                return body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class _Session:
            def put(self, url, **kwargs):
                return _Resp()

            def delete(self, url, **kwargs):
                return _Resp()

        with mock.patch.object(
            deadline, "get_aiosession", new=mock.AsyncMock(return_value=_Session())
        ), mock.patch.object(deadline, "_invalidate_jobs_cache"):
            return await call()

    async def test_an_error_body_is_a_refusal(self) -> None:
        self.assertFalse(await self._send("Error: access denied", lambda: deadline.pend_job("u", "p", "j")))
        self.assertFalse(await self._send("Error: no such job", lambda: deadline.delete_job("u", "p", "j")))

    async def test_success_is_success(self) -> None:
        self.assertTrue(await self._send("Success", lambda: deadline.pend_job("u", "p", "j")))
        self.assertTrue(await self._send("Success", lambda: deadline.delete_job("u", "p", "j")))


class ChatResumeTests(unittest.IsolatedAsyncioTestCase):
    async def _resume(self, jobs: dict, job_id: str):
        from app.bot.handlers import jobs as handlers

        async def lookup(user_id, wanted):
            return jobs.get(wanted)

        pend = mock.AsyncMock(return_value=True)
        resume = mock.AsyncMock(return_value=True)
        with mock.patch.object(
            handlers, "get_job_info_by_user_id", new=mock.AsyncMock(side_effect=lookup)
        ), mock.patch.object(handlers, "pend_job_by_user_id", new=pend), mock.patch.object(
            handlers, "resume_job_by_user_id", new=resume
        ), mock.patch(
            "app.services.preview.runtime._extract_preview_context",
            return_value=("", "", "", 42, {}, "src1"),
        ):
            outcome = await handlers._resume_job(42, job_id)
        return outcome, pend, resume

    async def test_a_preview_of_an_unfinished_render_goes_back_to_waiting(self) -> None:
        outcome, pend, resume = await self._resume(
            {"prev1": preview(SUSPENDED), "src1": render(SUSPENDED)}, "prev1"
        )
        pend.assert_awaited_once_with(42, "prev1")
        resume.assert_not_awaited()
        self.assertEqual(outcome, (True, "Preview waits for its render again."))

    async def test_a_preview_of_a_finished_render_is_resumed(self) -> None:
        """Its frames are there; pending it would send it down the overdue path."""
        _, pend, resume = await self._resume(
            {"prev1": preview(SUSPENDED), "src1": render(COMPLETED)}, "prev1"
        )
        resume.assert_awaited_once_with(42, "prev1")
        pend.assert_not_awaited()

    async def test_a_render_is_resumed_as_before(self) -> None:
        _, pend, resume = await self._resume({"src1": render(SUSPENDED)}, "src1")
        resume.assert_awaited_once_with(42, "src1")
        pend.assert_not_awaited()

    async def test_nothing_is_resumed_blind(self) -> None:
        outcome, pend, resume = await self._resume({}, "prev1")
        self.assertIsNone(outcome)
        pend.assert_not_awaited()
        resume.assert_not_awaited()


class JobListTests(unittest.TestCase):
    def _batch_stat(self, *jobs: dict) -> int:
        (combined,) = job_helpers.group_and_combine_jobs(list(jobs))
        return combined["Stat"]

    def test_a_paused_render_with_its_waiting_preview_reads_suspended(self) -> None:
        self.assertEqual(self._batch_stat(render(SUSPENDED), preview(PENDING)), SUSPENDED)

    def test_a_running_render_still_reads_as_running_or_pending(self) -> None:
        self.assertIn(self._batch_stat(render(ACTIVE), preview(PENDING)), (ACTIVE, PENDING))

    def test_a_finished_render_waiting_for_its_preview_reads_pending(self) -> None:
        self.assertEqual(self._batch_stat(render(COMPLETED), preview(PENDING)), PENDING)


class PauseStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_path = settings.sqlite_db_path
        settings.sqlite_db_path = str(Path(self._tmp.name) / "test.db")
        await database.init_db()

    async def asyncTearDown(self) -> None:
        await database.close_db()
        settings.sqlite_db_path = self._old_path
        self._tmp.cleanup()

    async def test_round_trip(self) -> None:
        await preview_pause.record_pause("prev1", "src1", 42)
        await preview_pause.record_pause("prev2", "src2", 7)
        pauses = await preview_pause.list_pauses(42)
        self.assertEqual(list(pauses), ["prev1"])
        self.assertEqual(pauses["prev1"].source_job_id, "src1")
        await preview_pause.drop_pause("prev1")
        self.assertEqual(await preview_pause.list_pauses(42), {})
        self.assertIn("prev2", await preview_pause.list_pauses(7))

    async def test_noting_a_pause_again_keeps_when_it_began(self) -> None:
        await preview_pause.record_pause("prev1", "src1", 42)
        first = (await preview_pause.list_pauses(42))["prev1"].paused_at
        conn = database.get_db_connection()
        await conn.execute("UPDATE preview_pauses SET paused_at = paused_at - 5000")
        await conn.commit()
        await preview_pause.record_pause("prev1", "src1", 42)
        self.assertEqual((await preview_pause.list_pauses(42))["prev1"].paused_at, first - 5000)


if __name__ == "__main__":
    unittest.main()
