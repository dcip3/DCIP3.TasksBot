"""Per-account probe scope: who is allowed to hold back which render.

Probing is a write on somebody's job, so it is opt-in per account. The default
("own") reproduces the behaviour that existed before the setting; "all" lets an
account with farm-wide rights probe renders whose submitter cannot - which is
the case that used to leave those renders without a usable ETA for hours.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.config import settings
from app.services import job_watcher
from app.storage import database, user_settings


def _user(user_id: int, login: str, probe_scope: str) -> job_watcher._WatcherUser:
    return job_watcher._WatcherUser(
        telegram_user_id=user_id,
        login=login,
        password="secret",
        notifications_enabled=False,
        notification_scope="own",
        auto_scope="own",
        preview_worker=None,
        auto_preview_enabled=False,
        probe_scope=probe_scope,
    )


def _job(job_id: str, owner: str) -> dict:
    return {
        "_id": job_id,
        "Stat": 1,
        "OutDir": ["Y:/renders"],
        "Props": {"User": owner, "Name": f"shot/{job_id}"},
    }


class ProbeScopeStorageTests(unittest.IsolatedAsyncioTestCase):
    """Against a real database, so the column and its default are exercised."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_path = settings.sqlite_db_path
        settings.sqlite_db_path = str(Path(self._tmp.name) / "test.db")
        await database.init_db()
        conn = database.get_db_connection()
        await conn.execute(
            """
            INSERT INTO user_sessions (telegram_user_id, deadline_login, deadline_password)
            VALUES (1, 'nodea', 'pw'), (2, 'artist', 'pw')
            """
        )
        await conn.commit()

    async def asyncTearDown(self) -> None:
        await database.close_db()
        settings.sqlite_db_path = self._old_path
        self._tmp.cleanup()

    async def test_defaults_to_own_jobs(self) -> None:
        """An upgrade must not start probing other people's renders by itself."""
        self.assertEqual(await user_settings.get_probe_scope(1), "own")

    async def test_round_trip(self) -> None:
        self.assertEqual(await user_settings.set_probe_scope(1, "all"), "all")
        self.assertEqual(await user_settings.get_probe_scope(1), "all")
        self.assertEqual(await user_settings.set_probe_scope(1, "off"), "off")
        self.assertEqual(await user_settings.get_probe_scope(1), "off")
        # The other account is untouched.
        self.assertEqual(await user_settings.get_probe_scope(2), "own")

    async def test_garbage_scope_falls_back_to_own(self) -> None:
        await user_settings.set_probe_scope(1, "everything")
        self.assertEqual(await user_settings.get_probe_scope(1), "own")

    async def test_unknown_user_is_own(self) -> None:
        self.assertEqual(await user_settings.get_probe_scope(999), "own")

    async def test_watcher_reads_the_scope(self) -> None:
        await user_settings.set_probe_scope(1, "all")
        users = {u.telegram_user_id: u for u in await job_watcher._load_watcher_users()}
        self.assertEqual(users[1].probe_scope, "all")
        self.assertEqual(users[2].probe_scope, "own")

    async def test_release_candidates_prefer_farm_wide_accounts(self) -> None:
        await user_settings.set_probe_scope(2, "all")
        self.assertEqual(await user_settings.list_probe_release_candidates(), [2, 1])
        self.assertEqual(await user_settings.list_probe_release_candidates(2), [1])

    async def test_release_candidates_skip_logged_out_accounts(self) -> None:
        conn = database.get_db_connection()
        await conn.execute(
            "UPDATE user_sessions SET deadline_password = '' WHERE telegram_user_id = 2"
        )
        await conn.commit()
        self.assertEqual(await user_settings.list_probe_release_candidates(), [1])


class ProbePermissionTests(unittest.TestCase):
    def test_own_scope_only_matches_the_submitter(self) -> None:
        owner = _user(1, "artist", "own")
        job = _job("j1", "artist")
        self.assertTrue(job_watcher._may_probe(job, job["Props"], owner))

        foreign = _user(2, "nodea", "own")
        self.assertFalse(job_watcher._may_probe(job, job["Props"], foreign))

    def test_all_scope_matches_foreign_jobs(self) -> None:
        admin = _user(2, "nodea", "all")
        job = _job("j1", "artist")
        self.assertTrue(job_watcher._may_probe(job, job["Props"], admin))

    def test_off_scope_matches_nothing(self) -> None:
        muted = _user(1, "artist", "off")
        job = _job("j1", "artist")
        self.assertFalse(job_watcher._may_probe(job, job["Props"], muted))

    def test_domain_qualified_owner_still_matches(self) -> None:
        owner = _user(1, "artist", "own")
        job = _job("j1", "STUDIO\\Artist")
        self.assertTrue(job_watcher._may_probe(job, job["Props"], owner))

    def test_owner_is_scanned_before_farm_wide_accounts(self) -> None:
        """Whoever runs first claims the job; the submitter is the better holder."""
        admin = _user(2, "nodea", "all")
        owner = _user(1, "artist", "own")
        muted = _user(3, "third", "off")
        order = job_watcher._probe_scan_order([admin, muted, owner])
        self.assertEqual([u.telegram_user_id for u in order], [1, 2])


class ProbeScanSelectionTests(unittest.IsolatedAsyncioTestCase):
    """What _scan_render_probes actually decides to probe."""

    def setUp(self) -> None:
        self.jobs = [_job("own-job", "artist"), _job("foreign-job", "someone")]
        self.started: list[tuple[int, str]] = []

    async def _scan(self, users: list) -> list[tuple[int, str]]:
        from app.services import probe_scheduler

        async def start_probing(user_id, job_id, tasks):
            self.started.append((user_id, job_id))
            return True

        async def get_jobs(login, password, **kwargs):
            return self.jobs

        async def get_tasks(user_id, job_id):
            return [{"TaskID": i, "Frames": str(i), "Stat": 2} for i in range(10)]

        with (
            mock.patch.object(
                probe_scheduler, "release_stale_probes", new=mock.AsyncMock(return_value=0)
            ),
            mock.patch.object(
                probe_scheduler, "start_probing", new=mock.AsyncMock(side_effect=start_probing)
            ),
            mock.patch.object(
                probe_scheduler, "release_if_ready", new=mock.AsyncMock(return_value=False)
            ),
            mock.patch.object(
                job_watcher.probe_state,
                "get_probe_state",
                new=mock.AsyncMock(return_value=None),
            ),
            mock.patch(
                "app.services.deadline.get_jobs_by_credentials",
                new=mock.AsyncMock(side_effect=get_jobs),
            ),
            mock.patch(
                "app.services.deadline.get_job_tasks_by_user_id",
                new=mock.AsyncMock(side_effect=get_tasks),
            ),
        ):
            await job_watcher._scan_render_probes(users)
        return self.started

    async def test_own_scope_probes_only_its_own_job(self) -> None:
        started = await self._scan([_user(1, "artist", "own")])
        self.assertEqual(started, [(1, "own-job")])

    async def test_all_scope_probes_everything_active(self) -> None:
        started = await self._scan([_user(2, "nodea", "all")])
        self.assertEqual(
            sorted(started), [(2, "foreign-job"), (2, "own-job")]
        )

    async def test_off_scope_probes_nothing(self) -> None:
        started = await self._scan([_user(1, "artist", "off")])
        self.assertEqual(started, [])

    async def test_a_job_is_claimed_once_even_when_two_accounts_may_probe_it(self) -> None:
        """The owner claims their own job; the farm-wide account gets the rest."""
        from app.services import probe_scheduler

        claimed: dict[str, int] = {}

        async def start_probing(user_id, job_id, tasks):
            if job_id in claimed:
                raise AssertionError(f"{job_id} probed twice")
            claimed[job_id] = user_id
            return True

        async def get_probe_state(job_id):
            if job_id not in claimed:
                return None
            from app.storage.probe_state import ProbeState

            return ProbeState(
                job_id=job_id,
                telegram_user_id=claimed[job_id],
                probe_task_ids=[0],
                held_task_ids=[1, 2],
                started_at=0,
                released_at=None,
            )

        async def get_jobs(login, password, **kwargs):
            return self.jobs

        async def get_tasks(user_id, job_id):
            return [{"TaskID": i, "Frames": str(i), "Stat": 2} for i in range(10)]

        with (
            mock.patch.object(
                probe_scheduler, "release_stale_probes", new=mock.AsyncMock(return_value=0)
            ),
            mock.patch.object(
                probe_scheduler, "start_probing", new=mock.AsyncMock(side_effect=start_probing)
            ),
            mock.patch.object(
                probe_scheduler, "release_if_ready", new=mock.AsyncMock(return_value=False)
            ),
            mock.patch.object(
                job_watcher.probe_state,
                "get_probe_state",
                new=mock.AsyncMock(side_effect=get_probe_state),
            ),
            mock.patch(
                "app.services.deadline.get_jobs_by_credentials",
                new=mock.AsyncMock(side_effect=get_jobs),
            ),
            mock.patch(
                "app.services.deadline.get_job_tasks_by_user_id",
                new=mock.AsyncMock(side_effect=get_tasks),
            ),
        ):
            await job_watcher._scan_render_probes(
                [_user(2, "nodea", "all"), _user(1, "artist", "own")]
            )

        self.assertEqual(claimed, {"own-job": 1, "foreign-job": 2})


if __name__ == "__main__":
    unittest.main()
