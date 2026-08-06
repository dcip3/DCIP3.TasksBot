import os
import sys
import time
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

from app.services import deadline, probe_scheduler, render_cost
from app.storage import probe_state


def task(task_id: int, first: int, stat: int = render_cost.TASK_QUEUED) -> dict:
    return {"TaskID": task_id, "Frames": f"{first}-{first + 4}", "Stat": stat}


def job_tasks(count: int = 20) -> list[dict]:
    return [task(i, 1 + i * 5) for i in range(count)]


class ProbePlanningTests(unittest.TestCase):
    def test_probes_span_the_whole_range(self) -> None:
        samples = render_cost.collect_samples(job_tasks(58))
        probes = probe_scheduler.plan_initial_probes(samples)
        self.assertEqual(len(probes), probe_scheduler.INITIAL_PROBES)
        self.assertEqual(probes[0], 0)
        self.assertEqual(probes[-1], 57)
        # Evenly spread, not clustered at one end.
        gaps = [b - a for a, b in zip(probes, probes[1:])]
        self.assertTrue(all(gap >= 13 for gap in gaps), probes)

    def test_short_jobs_get_fewer_probes(self) -> None:
        samples = render_cost.collect_samples(job_tasks(6))
        self.assertEqual(len(probe_scheduler.plan_initial_probes(samples)), 2)

    def test_tiny_jobs_are_left_alone(self) -> None:
        samples = render_cost.collect_samples(job_tasks(3))
        self.assertEqual(probe_scheduler.plan_initial_probes(samples), [])

    def test_adaptive_probe_avoids_a_task_already_being_measured(self) -> None:
        """The real slip on SHB_city_main_v004.

        Task 0 was done, 28 was rendering far enough along to count, and 14 was
        rendering but below the confidence threshold, so it was not a curve
        point. The gap 0..28 therefore looked unsampled and the probe landed on
        task 15 - right next to a measurement already on its way.
        """
        tasks = job_tasks(58)
        tasks[0].update(
            {
                "Stat": render_cost.TASK_COMPLETED,
                "StartRen": "2026-08-05T21:53:30+00:00",
                "Comp": "2026-08-05T21:56:30+00:00",
            }
        )
        tasks[14].update(
            {
                "Stat": render_cost.TASK_RENDERING,
                "StartRen": "2026-08-05T21:53:29+00:00",
                "Prog": "5 %",  # too early to trust as a sample
            }
        )
        tasks[28].update(
            {
                "Stat": render_cost.TASK_RENDERING,
                "StartRen": "2026-08-05T21:54:17+00:00",
                "Prog": "60 %",
            }
        )
        samples = render_cost.collect_samples(tasks)
        picked = probe_scheduler.pick_adaptive_probe(samples)

        self.assertIsNotNone(picked)
        self.assertNotIn(picked, (13, 15), "probe placed next to task 14, which is mid-render")
        self.assertNotEqual(picked, 14)

    def test_adaptive_probe_targets_the_stretch_holding_the_render_time(self) -> None:
        """Where being wrong costs the most, not where the curve is steepest.

        Tasks 0-10 ramp steeply from 1 to 50 minutes; tasks 10-19 sit flat but
        expensive at ~50 minutes. Chasing the steepest slope lands in the ramp,
        which is mostly cheap frames. Chasing render time lands in the plateau -
        and on real jobs that was the difference between an integral of 0.82x
        and 0.94x.
        """
        tasks = job_tasks(20)
        for task_id, minutes in ((0, 1), (10, 50), (19, 53)):
            tasks[task_id] = {
                "TaskID": task_id,
                "Frames": f"{1 + task_id * 5}-{5 + task_id * 5}",
                "Stat": render_cost.TASK_COMPLETED,
                "StartRen": "2026-08-05T10:00:00+00:00",
                "Comp": (
                    datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
                    + timedelta(minutes=minutes)
                ).isoformat(),
            }
        samples = render_cost.collect_samples(tasks)
        picked = probe_scheduler.pick_adaptive_probe(samples)

        self.assertIsNotNone(picked)
        self.assertTrue(10 < picked < 19, f"picked {picked}, expected the costly plateau")


class ProbeSuspensionSafetyTests(unittest.IsolatedAsyncioTestCase):
    """The blast radius of a wrong task list here is a stalled render."""

    async def test_task_command_uses_tasklist_key(self) -> None:
        """With any other key Deadline silently applies the command to ALL tasks."""
        captured: dict = {}

        class _Resp:
            status = 200

            async def text(self):
                return "Success"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class _Session:
            def put(self, url, json=None, **kwargs):
                captured["url"] = url
                captured["json"] = json
                return _Resp()

        with mock.patch.object(
            deadline, "get_aiosession", new=mock.AsyncMock(return_value=_Session())
        ), mock.patch.object(deadline, "_invalidate_jobs_cache"):
            ok = await deadline._put_task_command("u", "p", "suspend", "job1", [2, 3])

        self.assertTrue(ok)
        self.assertIn("/tasks", captured["url"])
        self.assertEqual(captured["json"]["TaskList"], ["2", "3"])
        self.assertNotIn("TaskIDs", captured["json"])

    async def test_empty_task_list_is_refused(self) -> None:
        """Deadline treats an empty selection as 'everything'."""
        with mock.patch.object(deadline, "get_aiosession") as session_mock:
            ok = await deadline._put_task_command("u", "p", "suspend", "job1", [])
        self.assertFalse(ok)
        session_mock.assert_not_called()

    async def test_unknown_command_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await deadline._put_task_command("u", "p", "delete", "job1", [1])


class ProbeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.saved: dict = {}
        self.suspended: list = []
        self.resumed: list = []

    def _patches(self, state=None):
        async def save(job_id, user_id, probes, held):
            self.saved = {
                "job_id": job_id,
                "user_id": user_id,
                "probes": probes,
                "held": held,
            }

        async def suspend(user_id, job_id, ids):
            self.suspended.append(list(ids))
            return True

        async def resume(user_id, job_id, ids):
            self.resumed.append(list(ids))
            return True

        return (
            mock.patch.object(probe_state, "save_probe_state", new=save),
            mock.patch.object(
                probe_state, "get_probe_state", new=mock.AsyncMock(return_value=state)
            ),
            mock.patch.object(probe_state, "mark_probe_released", new=mock.AsyncMock()),
            mock.patch.object(probe_state, "delete_probe_state", new=mock.AsyncMock()),
            mock.patch.object(
                deadline, "suspend_tasks_by_user_id", new=mock.AsyncMock(side_effect=suspend)
            ),
            mock.patch.object(
                deadline, "resume_tasks_by_user_id", new=mock.AsyncMock(side_effect=resume)
            ),
        )

    async def test_holds_back_everything_except_probes(self) -> None:
        tasks = job_tasks(20)
        patches = self._patches()
        for patch in patches:
            patch.start()
        try:
            started = await probe_scheduler.start_probing(42, "job1", tasks)
        finally:
            for patch in patches:
                patch.stop()

        self.assertTrue(started)
        probes = self.saved["probes"]
        held = self.saved["held"]
        self.assertEqual(sorted(probes + held), list(range(20)))
        self.assertEqual(self.suspended, [held])
        # State is recorded before the suspend call, so a crash in between
        # still leaves something for recovery to release.
        self.assertEqual(self.saved["job_id"], "job1")

    async def test_never_suspends_a_rendering_task(self) -> None:
        tasks = job_tasks(20)
        tasks[7]["Stat"] = render_cost.TASK_RENDERING
        patches = self._patches()
        for patch in patches:
            patch.start()
        try:
            await probe_scheduler.start_probing(42, "job1", tasks)
        finally:
            for patch in patches:
                patch.stop()
        self.assertNotIn(7, self.saved["held"])
        self.assertNotIn(7, self.suspended[0])

    async def test_skips_jobs_already_underway(self) -> None:
        tasks = job_tasks(20)
        tasks[0]["Stat"] = render_cost.TASK_COMPLETED
        patches = self._patches()
        for patch in patches:
            patch.start()
        try:
            started = await probe_scheduler.start_probing(42, "job1", tasks)
        finally:
            for patch in patches:
                patch.stop()
        self.assertFalse(started)
        self.assertEqual(self.suspended, [])

    async def test_leaves_foreign_suspensions_alone(self) -> None:
        """Someone suspended tasks by hand - not ours to reshuffle."""
        tasks = job_tasks(20)
        tasks[3]["Stat"] = render_cost.TASK_SUSPENDED
        patches = self._patches()
        for patch in patches:
            patch.start()
        try:
            started = await probe_scheduler.start_probing(42, "job1", tasks)
        finally:
            for patch in patches:
                patch.stop()
        self.assertFalse(started)
        self.assertEqual(self.suspended, [])

    async def test_releases_once_probes_are_done(self) -> None:
        held = [1, 2, 3]
        state = probe_state.ProbeState(
            job_id="job1",
            telegram_user_id=42,
            probe_task_ids=[0, 4],
            held_task_ids=held,
            started_at=int(time.time()),
            released_at=None,
        )
        tasks = job_tasks(5)
        for i in (0, 4):
            tasks[i]["Stat"] = render_cost.TASK_COMPLETED
        patches = self._patches(state)
        for patch in patches:
            patch.start()
        try:
            with mock.patch.object(
                probe_scheduler, "ADAPTIVE_PROBES", 0
            ):
                released = await probe_scheduler.release_if_ready("job1", tasks)
        finally:
            for patch in patches:
                patch.stop()
        self.assertTrue(released)
        self.assertEqual(self.resumed, [held])

    async def test_holds_while_probes_are_still_queued(self) -> None:
        """One worker busy, two probes still waiting: nothing would idle."""
        state = probe_state.ProbeState(
            job_id="job1",
            telegram_user_id=42,
            probe_task_ids=[0, 5, 9],
            held_task_ids=[1, 2, 3, 4, 6, 7, 8],
            started_at=int(time.time()),
            released_at=None,
        )
        tasks = job_tasks(10)
        tasks[0]["Stat"] = render_cost.TASK_RENDERING
        patches = self._patches(state)
        for patch in patches:
            patch.start()
        try:
            released = await probe_scheduler.release_if_ready("job1", tasks)
        finally:
            for patch in patches:
                patch.stop()
        self.assertFalse(released)
        self.assertEqual(self.resumed, [])

    async def test_three_machines_do_not_wait_for_the_last_probes(self) -> None:
        """Three workers, every probe claimed: the queue must be topped up now.

        This is the case that used to strand a machine - it would finish its
        probe with nothing dispatchable left and sit idle until the other two
        probes came in.
        """
        state = probe_state.ProbeState(
            job_id="job1",
            telegram_user_id=42,
            probe_task_ids=[0, 4, 9],
            held_task_ids=[1, 2, 3, 5, 6, 7, 8],
            started_at=int(time.time()),
            released_at=None,
        )
        tasks = job_tasks(10)
        for i in (0, 4, 9):
            tasks[i]["Stat"] = render_cost.TASK_RENDERING
        patches = self._patches(state)
        for patch in patches:
            patch.start()
        try:
            # No cost curve yet (nothing completed), so refinement cannot help
            # and the whole remainder is handed over rather than stalling.
            released = await probe_scheduler.release_if_ready("job1", tasks)
        finally:
            for patch in patches:
                patch.stop()
        self.assertTrue(released)
        self.assertEqual(self.resumed, [[1, 2, 3, 5, 6, 7, 8]])

    async def test_a_queued_probe_is_never_overtaken(self) -> None:
        """The failure seen on SHB_city_main_v004.

        This is the exact state at 22:00 that day: both refining probes spent,
        two workers busy (7 and 28), and exactly two dispatchable tasks left -
        probes 43 and 57. The old rule compared dispatchable tasks against busy
        workers, saw 2 against 2, found the refinement budget exhausted and
        released all 51 held tasks. Deadline then handed the next free worker
        task 1, because it dispatches the lowest available id, so probes 43 and
        57 were left for hours and the far half of the range went unmeasured.
        """
        probes = [0, 14, 28, 43, 57, 15, 7]  # five spread, two refining
        tasks = job_tasks(58)
        for i in (0, 14, 15):
            tasks[i]["Stat"] = render_cost.TASK_COMPLETED
        for i in (7, 28):
            tasks[i]["Stat"] = render_cost.TASK_RENDERING
        state = probe_state.ProbeState(
            job_id="job1",
            telegram_user_id=42,
            probe_task_ids=probes,
            held_task_ids=[i for i in range(58) if i not in probes],
            started_at=int(time.time()),
            released_at=None,
        )
        patches = self._patches(state)
        for patch in patches:
            patch.start()
        try:
            released = await probe_scheduler.release_if_ready("job1", tasks)
        finally:
            for patch in patches:
                patch.stop()

        self.assertFalse(released, "probes 43/57 still queued - nothing may be released")
        self.assertEqual(self.resumed, [])

    async def test_moves_on_once_every_probe_has_started(self) -> None:
        """No probe left waiting means a freeing worker needs something else."""
        tasks = job_tasks(58)
        for i in (0, 14, 28, 43):
            tasks[i]["Stat"] = render_cost.TASK_COMPLETED
        tasks[57]["Stat"] = render_cost.TASK_RENDERING
        state = probe_state.ProbeState(
            job_id="job1",
            telegram_user_id=42,
            probe_task_ids=[0, 14, 28, 43, 57],
            held_task_ids=[i for i in range(58) if i not in (0, 14, 28, 43, 57)],
            started_at=int(time.time()),
            released_at=None,
        )
        patches = self._patches(state)
        for patch in patches:
            patch.start()
        try:
            released = await probe_scheduler.release_if_ready("job1", tasks)
        finally:
            for patch in patches:
                patch.stop()

        # Either a refining probe went out, or the remainder did - but the
        # scheduler did not just sit there.
        self.assertTrue(released or self.resumed, "a free worker would have idled")

    async def test_a_burst_of_machines_is_fed_in_one_go(self) -> None:
        """Machines can join mid-render; one probe per scan would strand them.

        Six workers are busy on probes and none is left queued. Handing out a
        single refining probe per 10-second scan would leave several machines
        with nothing for the better part of a minute.
        """
        tasks = job_tasks(58)
        probes = [0, 14, 28, 43, 57]
        for i in probes[:1]:
            tasks[i]["Stat"] = render_cost.TASK_COMPLETED
            tasks[i]["StartRen"] = "2026-08-05T10:00:00+00:00"
            tasks[i]["Comp"] = "2026-08-05T10:05:00+00:00"
        for i in probes[1:]:
            tasks[i]["Stat"] = render_cost.TASK_RENDERING
            tasks[i]["StartRen"] = "2026-08-05T10:00:00+00:00"
            tasks[i]["Prog"] = "60 %"
        state = probe_state.ProbeState(
            job_id="job1",
            telegram_user_id=42,
            probe_task_ids=probes,
            held_task_ids=[i for i in range(58) if i not in probes],
            started_at=int(time.time()),
            released_at=None,
        )
        patches = self._patches(state)
        for patch in patches:
            patch.start()
        try:
            released = await probe_scheduler.release_if_ready("job1", tasks)
        finally:
            for patch in patches:
                patch.stop()

        self.assertFalse(released)
        self.assertEqual(len(self.resumed), 1, "should be a single batched call")
        batch = self.resumed[0]
        self.assertGreater(len(batch), 1, "one probe per scan strands the rest")
        self.assertEqual(len(batch), len(set(batch)), "same task handed out twice")

    async def test_spare_slot_goes_to_a_refining_probe_first(self) -> None:
        """When the queue needs topping up, spend it on the least-certain gap."""
        tasks = job_tasks(20)
        for task_id, comp in ((0, "10:01:00"), (10, "10:01:00"), (19, "11:23:20")):
            tasks[task_id].update(
                {
                    "Stat": render_cost.TASK_COMPLETED,
                    "StartRen": "2026-08-05T10:00:00+00:00",
                    "Comp": f"2026-08-05T{comp}+00:00",
                }
            )
        state = probe_state.ProbeState(
            job_id="job1",
            telegram_user_id=42,
            probe_task_ids=[0, 10, 19],
            held_task_ids=[i for i in range(20) if i not in (0, 10, 19)],
            started_at=int(time.time()),
            released_at=None,
        )
        patches = self._patches(state)
        for patch in patches:
            patch.start()
        try:
            released = await probe_scheduler.release_if_ready("job1", tasks)
        finally:
            for patch in patches:
                patch.stop()
        self.assertFalse(released)
        self.assertEqual(len(self.resumed), 1)
        self.assertEqual(len(self.resumed[0]), 1)
        # Between task 10 and 19 is where the cost jumps; refine in there.
        self.assertTrue(10 < self.resumed[0][0] < 19, self.resumed)

    async def test_a_failed_probe_does_not_hold_the_render(self) -> None:
        """Waiting for Completed on a failed probe would stall until timeout."""
        state = probe_state.ProbeState(
            job_id="job1",
            telegram_user_id=42,
            probe_task_ids=[0, 4],
            held_task_ids=[1, 2, 3],
            started_at=int(time.time()),
            released_at=None,
        )
        tasks = job_tasks(5)
        tasks[0]["Stat"] = render_cost.TASK_COMPLETED
        tasks[4]["Stat"] = 6  # failed
        patches = self._patches(state)
        for patch in patches:
            patch.start()
        try:
            with mock.patch.object(probe_scheduler, "ADAPTIVE_PROBES", 0):
                released = await probe_scheduler.release_if_ready("job1", tasks)
        finally:
            for patch in patches:
                patch.stop()
        self.assertTrue(released)


class StaleProbeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_overrunning_probe_phase_is_released(self) -> None:
        stale = probe_state.ProbeState(
            job_id="job1",
            telegram_user_id=42,
            probe_task_ids=[0],
            held_task_ids=[1, 2],
            started_at=int(time.time()) - probe_scheduler.MAX_PROBE_SECONDS - 60,
            released_at=None,
        )
        fresh = probe_state.ProbeState(
            job_id="job2",
            telegram_user_id=42,
            probe_task_ids=[0],
            held_task_ids=[1, 2],
            started_at=int(time.time()),
            released_at=None,
        )
        resumed: list = []

        async def resume(user_id, job_id, ids):
            resumed.append(job_id)
            return True

        with mock.patch.object(
            probe_state,
            "list_unreleased_probes",
            new=mock.AsyncMock(return_value=[stale, fresh]),
        ), mock.patch.object(
            probe_state, "mark_probe_released", new=mock.AsyncMock()
        ), mock.patch.object(
            deadline, "resume_tasks_by_user_id", new=mock.AsyncMock(side_effect=resume)
        ):
            released = await probe_scheduler.release_stale_probes()

        self.assertEqual(released, 1)
        # A redeploy must not throw away a probe phase that is still on time.
        self.assertEqual(resumed, ["job1"])

    async def test_gives_up_on_a_vanished_job(self) -> None:
        ancient = probe_state.ProbeState(
            job_id="gone",
            telegram_user_id=42,
            probe_task_ids=[0],
            held_task_ids=[1],
            started_at=int(time.time()) - 3 * probe_scheduler.MAX_PROBE_SECONDS,
            released_at=None,
        )
        with mock.patch.object(
            probe_state,
            "list_unreleased_probes",
            new=mock.AsyncMock(return_value=[ancient]),
        ), mock.patch.object(
            probe_state, "mark_probe_released", new=mock.AsyncMock()
        ) as mark_mock, mock.patch.object(
            deadline, "resume_tasks_by_user_id", new=mock.AsyncMock(return_value=False)
        ):
            await probe_scheduler.release_stale_probes()

        mark_mock.assert_awaited_once_with("gone")


if __name__ == "__main__":
    unittest.main()
