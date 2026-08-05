"""Probe state against a real database.

The earlier probe tests mocked this module out entirely, so nothing exercised
the actual SQL - and in production every call raised "threads can only be
started once", because `get_db_connection()` was being awaited. aiosqlite's
Connection subclasses Thread, so awaiting a live one restarts it.

These tests talk to a real (temporary) database, which is the only way that
class of mistake shows up.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.config import settings
from app.storage import database, probe_state


class ProbeStateStorageTests(unittest.IsolatedAsyncioTestCase):
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
        await probe_state.save_probe_state("job1", 42, [0, 14, 28], [1, 2, 3])
        state = await probe_state.get_probe_state("job1")

        self.assertIsNotNone(state)
        self.assertEqual(state.job_id, "job1")
        self.assertEqual(state.telegram_user_id, 42)
        self.assertEqual(state.probe_task_ids, [0, 14, 28])
        self.assertEqual(state.held_task_ids, [1, 2, 3])
        self.assertIsNone(state.released_at)
        self.assertLess(state.age_seconds, 5)

    async def test_unknown_job(self) -> None:
        self.assertIsNone(await probe_state.get_probe_state("nope"))

    async def test_lists_only_unreleased(self) -> None:
        await probe_state.save_probe_state("held", 42, [0], [1, 2])
        await probe_state.save_probe_state("done", 42, [0], [3, 4])
        await probe_state.mark_probe_released("done")

        pending = await probe_state.list_unreleased_probes()
        self.assertEqual([s.job_id for s in pending], ["held"])

    async def test_release_forgets_the_held_tasks(self) -> None:
        """Once released, nothing is still being held back."""
        await probe_state.save_probe_state("job1", 42, [0], [1, 2, 3])
        await probe_state.mark_probe_released("job1")

        state = await probe_state.get_probe_state("job1")
        self.assertIsNotNone(state.released_at)
        self.assertEqual(state.held_task_ids, [])

    async def test_saving_twice_updates_in_place(self) -> None:
        """Adaptive probes re-save the row; it must not collide on the key."""
        await probe_state.save_probe_state("job1", 42, [0, 9], [1, 2, 3])
        await probe_state.save_probe_state("job1", 42, [0, 9, 2], [1, 3])

        state = await probe_state.get_probe_state("job1")
        self.assertEqual(state.probe_task_ids, [0, 9, 2])
        self.assertEqual(state.held_task_ids, [1, 3])
        self.assertEqual(len(await probe_state.list_unreleased_probes()), 1)

    async def test_delete_and_cleanup(self) -> None:
        await probe_state.save_probe_state("job1", 42, [0], [1])
        await probe_state.delete_probe_state("job1")
        self.assertIsNone(await probe_state.get_probe_state("job1"))

        await probe_state.save_probe_state("job2", 42, [0], [1])
        await probe_state.mark_probe_released("job2")
        await probe_state.cleanup_probe_state(max_age_seconds=-1)
        self.assertIsNone(await probe_state.get_probe_state("job2"))

    async def test_stale_sweep_reads_a_real_database(self) -> None:
        """The exact call that was failing on every watcher tick."""
        from app.services import probe_scheduler

        await probe_state.save_probe_state("job1", 42, [0], [1, 2])
        released = await probe_scheduler.release_stale_probes()
        # Nothing has overrun yet, but the read itself must succeed.
        self.assertEqual(released, 0)
        self.assertEqual(len(await probe_state.list_unreleased_probes()), 1)


if __name__ == "__main__":
    unittest.main()
