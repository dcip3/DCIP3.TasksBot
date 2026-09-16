"""A render that is put back to work earns another preview.

Requeueing a few tasks of a finished job - the usual fix for a bug spotted in
the frames - starts a new render run, and the repaired frames are exactly what
the user wants to see. Auto previews used to be deduplicated per (user, job)
forever, so that second run silently got nothing. They are now deduplicated per
*run*: a row remembers which completion it stands for, and whether a preview is
already queued for the run in flight.

These tests talk to a real (temporary) database, because the whole mechanism is
the SQL.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core import farm_events
from app.core.config import settings
from app.services import job_watcher
from app.services.job_state import auto_preview_jobs
from app.storage import database

USER = 4242
JOB = "job-a"


def _stamp(seconds_ago: int) -> str:
    """A completion time the watcher still considers recent."""
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return moment.isoformat().replace("+00:00", "Z")


FIRST_RUN = _stamp(300)
SECOND_RUN = _stamp(30)


class _DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_path = settings.sqlite_db_path
        settings.sqlite_db_path = str(Path(self._tmp.name) / "test.db")
        await database.init_db()
        auto_preview_jobs.clear()

    async def asyncTearDown(self) -> None:
        await database.close_db()
        settings.sqlite_db_path = self._old_path
        self._tmp.cleanup()
        auto_preview_jobs.clear()

    async def _rows(self) -> list:
        conn = database.get_db_connection()
        async with conn.execute(
            "SELECT telegram_user_id, job_id, run_state, completed_at"
            " FROM auto_preview_history ORDER BY telegram_user_id"
        ) as cursor:
            return list(await cursor.fetchall())


class CompletedRunClaimTests(_DatabaseTestCase):
    async def test_the_first_completion_is_previewed(self) -> None:
        self.assertEqual(
            await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN),
            job_watcher._CLAIM_NEW,
        )

    async def test_the_same_completion_is_not_previewed_twice(self) -> None:
        await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN)
        self.assertEqual(
            await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN),
            job_watcher._CLAIM_KNOWN,
        )

    async def test_a_second_completion_earns_its_own_preview(self) -> None:
        """The bug: requeued tasks finish, and nothing was ever sent."""
        await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN)
        self.assertEqual(
            await job_watcher._claim_completed_run(USER, JOB, SECOND_RUN),
            job_watcher._CLAIM_NEW,
        )
        self.assertEqual(
            await job_watcher._claim_completed_run(USER, JOB, SECOND_RUN),
            job_watcher._CLAIM_KNOWN,
        )

    async def test_an_armed_run_is_covered_by_the_preview_already_queued(self) -> None:
        await job_watcher._claim_new_run(USER, JOB)
        self.assertEqual(
            await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN),
            job_watcher._CLAIM_KNOWN,
        )
        # ...but the completion is now on record, so the next one is a new run.
        self.assertEqual(
            await job_watcher._claim_completed_run(USER, JOB, SECOND_RUN),
            job_watcher._CLAIM_NEW,
        )

    async def test_a_row_from_before_run_tracking_adopts_the_completion(self) -> None:
        """Upgrading the bot must not resend a preview the user has seen."""
        conn = database.get_db_connection()
        await conn.execute(
            "INSERT INTO auto_preview_history (telegram_user_id, job_id, created_at)"
            " VALUES (?, ?, 0)",
            (USER, JOB),
        )
        await conn.commit()

        self.assertEqual(
            await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN),
            job_watcher._CLAIM_KNOWN,
        )
        self.assertEqual(
            await job_watcher._claim_completed_run(USER, JOB, SECOND_RUN),
            job_watcher._CLAIM_NEW,
        )

    async def test_a_completion_without_a_timestamp_tells_us_nothing(self) -> None:
        await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN)
        self.assertEqual(
            await job_watcher._claim_completed_run(USER, JOB, ""),
            job_watcher._CLAIM_KNOWN,
        )

    async def test_users_are_tracked_apart(self) -> None:
        await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN)
        self.assertEqual(
            await job_watcher._claim_completed_run(USER + 1, JOB, FIRST_RUN),
            job_watcher._CLAIM_NEW,
        )


class NewRunClaimTests(_DatabaseTestCase):
    async def test_a_running_render_is_armed_once(self) -> None:
        self.assertEqual(
            await job_watcher._claim_new_run(USER, JOB), job_watcher._CLAIM_NEW
        )
        self.assertEqual(
            await job_watcher._claim_new_run(USER, JOB), job_watcher._CLAIM_KNOWN
        )

    async def test_a_render_back_at_work_after_delivery_is_armed_again(self) -> None:
        await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN)
        self.assertEqual(
            await job_watcher._claim_new_run(USER, JOB), job_watcher._CLAIM_NEW
        )
        rows = await self._rows()
        self.assertEqual(rows[0][2], job_watcher._RUN_ARMED)
        self.assertEqual(rows[0][3], "")


class ForgetRunTests(_DatabaseTestCase):
    async def test_a_requeue_clears_every_watcher_of_that_render(self) -> None:
        await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN)
        await job_watcher._claim_completed_run(USER + 1, JOB, FIRST_RUN)
        await job_watcher._claim_completed_run(USER, "job-b", FIRST_RUN)
        auto_preview_jobs.add((JOB, USER))
        auto_preview_jobs.add((JOB, USER + 1))
        auto_preview_jobs.add(("job-b", USER))

        cleared = await job_watcher.forget_auto_preview_run(JOB)

        self.assertEqual(cleared, 2)
        self.assertEqual([row[1] for row in await self._rows()], ["job-b"])
        self.assertNotIn((JOB, USER), auto_preview_jobs)
        self.assertNotIn((JOB, USER + 1), auto_preview_jobs)
        self.assertIn(("job-b", USER), auto_preview_jobs)

    async def test_clearing_one_watcher_leaves_the_others(self) -> None:
        await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN)
        await job_watcher._claim_completed_run(USER + 1, JOB, FIRST_RUN)

        await job_watcher.forget_auto_preview_run(JOB, USER)

        self.assertEqual([row[0] for row in await self._rows()], [USER + 1])

    async def test_a_cleared_render_is_previewed_again(self) -> None:
        await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN)
        await job_watcher.forget_auto_preview_run(JOB)
        self.assertEqual(
            await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN),
            job_watcher._CLAIM_NEW,
        )

    async def test_an_unknown_job_is_not_an_error(self) -> None:
        self.assertEqual(await job_watcher.forget_auto_preview_run("nope"), 0)
        self.assertEqual(await job_watcher.forget_auto_preview_run(""), 0)


class NoDatabaseTests(unittest.IsolatedAsyncioTestCase):
    """Without a database the in-memory dedupe is all there is."""

    async def test_claims_admit_they_cannot_answer(self) -> None:
        with mock.patch.object(job_watcher, "get_db_connection", return_value=None):
            self.assertEqual(
                await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN),
                job_watcher._CLAIM_UNKNOWN,
            )
            self.assertEqual(
                await job_watcher._claim_new_run(USER, JOB),
                job_watcher._CLAIM_UNKNOWN,
            )


def preview_job(source_id: str, stat: int, owner: int = USER) -> dict:
    return {
        "_id": f"preview-of-{source_id}",
        "Stat": stat,
        "Props": {
            "Name": "Shot - Preview",
            "ExDic": {
                "PreviewJob": "1",
                "PreviewSource": source_id,
                "PreviewTelegram": str(owner),
            },
        },
    }


class LivePreviewGuardTests(unittest.TestCase):
    def test_pending_queued_and_paused_previews_all_count(self) -> None:
        jobs = [
            preview_job("src-pending", 6),
            preview_job("src-running", 1),
            preview_job("src-paused", 2),
        ]
        self.assertEqual(
            job_watcher._sources_with_live_previews(jobs, USER),
            {"src-pending", "src-running", "src-paused"},
        )

    def test_a_finished_preview_no_longer_holds_its_render(self) -> None:
        self.assertEqual(
            job_watcher._sources_with_live_previews([preview_job("src", 3)], USER),
            set(),
        )

    def test_another_users_preview_does_not_block_mine(self) -> None:
        jobs = [preview_job("src", 6, owner=USER + 1)]
        self.assertEqual(job_watcher._sources_with_live_previews(jobs, USER), set())

    def test_renders_are_not_mistaken_for_previews(self) -> None:
        render = {"_id": "src", "Stat": 1, "Props": {"Name": "Shot v01"}}
        self.assertEqual(job_watcher._sources_with_live_previews([render], USER), set())


def render_job(stat: int, *, date_comp: str = "", completed_chunks: int = 0) -> dict:
    return {
        "_id": JOB,
        "Stat": stat,
        "OutDir": "Y:/render/shot",
        "DateComp": date_comp,
        "RenderingChunks": 0,
        "QueuedChunks": 0,
        "CompletedChunks": completed_chunks,
        "FailedChunks": 0,
        "Props": {"Name": "Shot v01", "User": "artist2"},
    }


class ScanTests(_DatabaseTestCase):
    """The scan itself - where the missing preview actually went missing."""

    def _user(self) -> job_watcher._WatcherUser:
        return job_watcher._WatcherUser(
            telegram_user_id=USER,
            login="artist2",
            password="secret",
            notifications_enabled=True,
            notification_scope="all",
            auto_scope="all",
            preview_worker=None,
            auto_preview_enabled=True,
        )

    async def _scan(self, jobs: list) -> tuple[list, list]:
        """Run one scan pass, returning what it asked to be previewed."""
        presubmitted: list = []
        completed: list = []

        async def _noop(*args, **kwargs) -> None:
            return None

        with mock.patch(
            "app.services.deadline.get_jobs_by_credentials",
            new=mock.AsyncMock(return_value=jobs),
        ), mock.patch.object(
            job_watcher,
            "_run_auto_preview_presubmit",
            new=lambda *args, **kwargs: (presubmitted.append(args), _noop())[1],
        ), mock.patch.object(
            job_watcher,
            "_run_auto_preview_for_job",
            new=lambda *args, **kwargs: (completed.append(args), _noop())[1],
        ), mock.patch.object(
            job_watcher,
            "_reconcile_previews",
            new=mock.AsyncMock(return_value=None),
        ):
            await job_watcher._scan_auto_preview_candidates([self._user()])
            # The scan fires the preview flows as background tasks.
            await job_watcher.asyncio.sleep(0)
        return presubmitted, completed

    async def test_a_running_render_gets_one_preview_queued(self) -> None:
        jobs = [render_job(1, completed_chunks=3)]
        presubmitted, _ = await self._scan(jobs)
        self.assertEqual(len(presubmitted), 1)

        # Same run on the next pass: nothing more.
        presubmitted, _ = await self._scan(jobs)
        self.assertEqual(presubmitted, [])

    async def test_requeued_tasks_get_a_new_preview_queued(self) -> None:
        """The reported bug, end to end."""
        await self._scan([render_job(1, completed_chunks=3)])
        await self._scan([render_job(3, date_comp=FIRST_RUN)])

        # The artist requeues a few tasks in Monitor: the render is at work
        # again, and its earlier preview has been delivered and removed.
        presubmitted, _ = await self._scan([render_job(1, completed_chunks=3)])
        self.assertEqual(len(presubmitted), 1)

    async def test_a_requeue_during_the_previous_preview_is_not_forgotten(self) -> None:
        """Found against the live farm: the run was armed while a preview from
        the previous run was still rendering, so it was marked as handled and
        the repaired frames never got a preview of their own."""
        preview = preview_job(JOB, 1)
        await self._scan([render_job(1, completed_chunks=3), preview])
        await self._scan([render_job(3, date_comp=FIRST_RUN), preview])

        # Tasks are requeued while that preview is still on the farm.
        presubmitted, _ = await self._scan([render_job(1, completed_chunks=3), preview])
        self.assertEqual(presubmitted, [])

        # It delivers and is removed; the waiting run gets its preview.
        presubmitted, _ = await self._scan([render_job(1, completed_chunks=3)])
        self.assertEqual(len(presubmitted), 1)

    async def test_a_short_requeue_outruns_the_previous_preview(self) -> None:
        """Three repaired frames render faster than a 240-frame preview encodes.

        The new run then completes while the previous preview is still on the
        farm, and holding the completion back on that account would lose it.
        """
        preview = preview_job(JOB, 1)
        await self._scan([render_job(1, completed_chunks=3), preview])
        await self._scan([render_job(3, date_comp=FIRST_RUN), preview])

        _, completed = await self._scan([render_job(3, date_comp=SECOND_RUN), preview])
        self.assertEqual(len(completed), 1)

    async def test_no_second_preview_beside_the_one_still_pending(self) -> None:
        jobs = [render_job(1, completed_chunks=3), preview_job(JOB, 6)]
        await self._scan(jobs)
        await job_watcher.forget_auto_preview_run(JOB)

        presubmitted, _ = await self._scan(jobs)
        self.assertEqual(presubmitted, [])

    async def test_a_second_completion_is_previewed_without_presubmit(self) -> None:
        with mock.patch.object(job_watcher.settings, "preview_presubmit_enabled", False):
            _, completed = await self._scan([render_job(3, date_comp=FIRST_RUN)])
            self.assertEqual(len(completed), 1)

            _, completed = await self._scan([render_job(3, date_comp=FIRST_RUN)])
            self.assertEqual(completed, [])

            _, completed = await self._scan([render_job(3, date_comp=SECOND_RUN)])
            self.assertEqual(len(completed), 1)


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    """An existing database must gain the run columns without losing rows."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_path = settings.sqlite_db_path
        settings.sqlite_db_path = str(Path(self._tmp.name) / "legacy.db")

        # The table exactly as it shipped before run tracking.
        conn = sqlite3.connect(settings.sqlite_db_path)
        conn.execute(
            """
            CREATE TABLE auto_preview_history (
                telegram_user_id INTEGER NOT NULL,
                job_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (telegram_user_id, job_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO auto_preview_history VALUES (?, ?, ?)",
            (USER, JOB, int(datetime.now(timezone.utc).timestamp())),
        )
        conn.commit()
        conn.close()

    async def asyncTearDown(self) -> None:
        await database.close_db()
        settings.sqlite_db_path = self._old_path
        self._tmp.cleanup()

    async def test_old_rows_survive_as_delivered_runs(self) -> None:
        await database.init_db()

        conn = database.get_db_connection()
        async with conn.execute(
            "SELECT run_state, completed_at FROM auto_preview_history"
        ) as cursor:
            rows = list(await cursor.fetchall())

        self.assertEqual(rows, [(job_watcher._RUN_DELIVERED, None)])
        # And that row still stands for a completion nobody recorded, so the
        # next one is adopted rather than resent.
        self.assertEqual(
            await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN),
            job_watcher._CLAIM_KNOWN,
        )


class FarmEventTests(_DatabaseTestCase):
    async def test_a_requeue_event_clears_the_run_records(self) -> None:
        await job_watcher._claim_completed_run(USER, JOB, FIRST_RUN)
        await farm_events.forget_previewed_runs_of(JOB)
        self.assertEqual(await self._rows(), [])

    async def test_an_unknown_job_is_quietly_ignored(self) -> None:
        await farm_events.forget_previewed_runs_of("never-seen")


if __name__ == "__main__":
    unittest.main()
