"""Where a preview is allowed to run, and what happens when it cannot.

A preview inherits its render's machine list, which may be a *deny* list.
Deadline has no "WhitelistFlag" key - the choice between an allow list and a
deny list *is* which key you write - so a deny list submitted under "Whitelist"
with a made-up "WhitelistFlag=False" beside it pins the preview to exactly the
machines the render excluded. When none of them can take work - a disabled
worker, say - the preview sits Queued with no worker and no error.
"""

import os
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.services import job_watcher
from app.services.preview import render


def worker(name: str, stat: int = 2, enabled: bool = True) -> dict:
    """A worker as /slaves?Data=infosettings reports it."""
    return {"Info": {"Name": name, "Stat": stat}, "Settings": {"Name": name.lower(), "Enable": enabled}}


FARM = [
    worker("NodeC", stat=1),
    worker("NodeA"),
    worker("NodeB", enabled=False),  # disabled: reports Idle, never dequeues
    worker("nodee", stat=3),  # offline
]


class ReadingTheSourceListTests(unittest.TestCase):
    def test_a_deny_list_is_read_as_a_deny_list(self) -> None:
        """A render keeping one machine off: White=False, ListedSlaves=[nodeb]."""
        self.assertEqual(
            render._resolve_machine_restrictions({"White": False, "ListedSlaves": ["nodeb"]}),
            (["nodeb"], False),
        )

    def test_an_allow_list_is_read_as_an_allow_list(self) -> None:
        self.assertEqual(
            render._resolve_machine_restrictions({"White": True, "ListedSlaves": ["NodeC", "NodeB"]}),
            (["NodeC", "NodeB"], True),
        )

    def test_no_list_means_no_restriction(self) -> None:
        self.assertEqual(render._resolve_machine_restrictions({"White": False}), ([], None))


class SubmissionFieldTests(unittest.TestCase):
    def test_a_deny_list_is_submitted_as_a_deny_list(self) -> None:
        fields = render._machine_restriction_fields(["nodeb"], False)
        self.assertEqual(fields.get("Blacklist"), "nodeb")
        self.assertNotIn("Whitelist", fields)

    def test_a_deny_list_carries_no_machine_limit(self) -> None:
        """Every other machine may run it, so limiting it to one is nonsense."""
        self.assertEqual(render._machine_restriction_fields(["nodeb"], False)["MachineLimit"], 0)

    def test_an_allow_list_is_submitted_as_an_allow_list(self) -> None:
        fields = render._machine_restriction_fields(["NodeC", "NodeA"], True)
        self.assertEqual(fields.get("Whitelist"), "NodeC,NodeA")
        self.assertNotIn("Blacklist", fields)
        self.assertEqual(fields["MachineLimit"], 2)

    def test_the_made_up_flag_is_gone(self) -> None:
        for flag in (True, False, None):
            self.assertNotIn("WhitelistFlag", render._machine_restriction_fields(["a"], flag))

    def test_no_list_leaves_the_job_unrestricted(self) -> None:
        self.assertEqual(render._machine_restriction_fields([], None), {"MachineLimit": 0})


class WorkerUsabilityTests(unittest.TestCase):
    def test_a_disabled_worker_cannot_take_work_however_idle_it_looks(self) -> None:
        self.assertFalse(render._worker_is_usable(worker("NodeB", stat=2, enabled=False)))

    def test_idle_and_rendering_workers_can(self) -> None:
        self.assertTrue(render._worker_is_usable(worker("NodeA", stat=2)))
        self.assertTrue(render._worker_is_usable(worker("NodeC", stat=1)))

    def test_offline_and_stalled_workers_cannot(self) -> None:
        self.assertFalse(render._worker_is_usable(worker("nodee", stat=3)))
        self.assertFalse(render._worker_is_usable(worker("nodee", stat=4)))

    def test_a_worker_with_no_enable_flag_is_taken_at_its_status(self) -> None:
        """Older Deadline versions may not report the setting at all."""
        self.assertTrue(render._worker_is_usable({"Info": {"Name": "x", "Stat": 2}}))

    def test_unusable_workers_are_reported_with_a_reason(self) -> None:
        found = render._unusable_workers(["NodeB", "NodeC", "ghost"], FARM)
        self.assertEqual(
            [(entry["name"], entry["status_text"]) for entry in found],
            [("NodeB", "Disabled"), ("ghost", "Unknown worker")],
        )


class StrandedPreviewTests(unittest.TestCase):
    def _props(self, listed, white) -> dict:
        return {"Name": "Shot - Preview", "ListedSlaves": listed, "White": white}

    def test_a_preview_pinned_to_a_disabled_worker_can_never_run(self) -> None:
        self.assertTrue(render.preview_cannot_run_anywhere(self._props(["NodeB"], True), FARM))

    def test_one_usable_worker_in_the_list_is_enough(self) -> None:
        self.assertFalse(
            render.preview_cannot_run_anywhere(self._props(["NodeB", "NodeC"], True), FARM)
        )

    def test_a_deny_list_leaves_a_preview_alone_while_a_machine_remains(self) -> None:
        self.assertFalse(render.preview_cannot_run_anywhere(self._props(["NodeB"], False), FARM))

    def test_a_deny_list_that_grew_over_every_working_machine_strands_it(self) -> None:
        """Workers strike themselves off a preview they cannot deliver; two of
        them in a row leave nothing that can run it."""
        self.assertTrue(
            render.preview_cannot_run_anywhere(self._props(["NodeA", "NodeC"], False), FARM)
        )

    def test_denying_only_machines_that_could_not_work_anyway_changes_nothing(self) -> None:
        self.assertFalse(
            render.preview_cannot_run_anywhere(self._props(["NodeB", "nodee"], False), FARM)
        )

    def test_the_deny_list_is_matched_whatever_the_case(self) -> None:
        self.assertTrue(
            render.preview_cannot_run_anywhere(self._props(["nodea", "nodec"], False), FARM)
        )

    def test_an_unrestricted_preview_is_only_ever_waiting_its_turn(self) -> None:
        self.assertFalse(render.preview_cannot_run_anywhere({"Name": "Shot - Preview"}, FARM))


class SubmittedJobTests(unittest.IsolatedAsyncioTestCase):
    """What actually reaches Deadline for a render with a machine list."""

    async def _submit(self, source_props: dict, **kwargs) -> dict:
        job_info = {
            "Props": dict({"Name": "SHA_0100_ID_v029 - /obj/ropnet1/main", "User": "nodea"}, **source_props),
            "OutDir": [r"Y:\projects\render\SHA_0100\main"],
            "OutFile": ["SHA_0100_main_####.exr"],
        }
        captured: dict = {}

        async def _submit_job(**call):
            captured.update(call["job_info"])
            return {"job_id": "prev1"}

        with mock.patch(
            "app.auth.get_deadline_credentials",
            new=mock.AsyncMock(return_value=("nodea", "pw")),
        ), mock.patch.object(
            render, "get_job_info", new=mock.AsyncMock(return_value=job_info)
        ), mock.patch.object(
            render, "get_workers_by_credentials", new=mock.AsyncMock(return_value=FARM)
        ), mock.patch.object(
            render, "submit_deadline_job", new=_submit_job
        ):
            await render.create_video_from_job(7, "src1", **kwargs)
        return captured

    async def test_a_denied_worker_stays_denied(self) -> None:
        """The machine the render keeps off stays off the preview too."""
        submitted = await self._submit({"White": False, "ListedSlaves": ["nodeb"]})
        self.assertEqual(submitted.get("Blacklist"), "nodeb")
        self.assertIsNone(submitted.get("Whitelist"))
        self.assertEqual(submitted["MachineLimit"], 0)

    async def test_an_allow_list_survives_when_its_workers_can_work(self) -> None:
        submitted = await self._submit({"White": True, "ListedSlaves": ["NodeC", "NodeA"]})
        self.assertEqual(submitted.get("Whitelist"), "NodeC,NodeA")

    async def test_workers_that_cannot_take_it_are_dropped(self) -> None:
        """Auto previews have nobody to ask, so they route around the problem."""
        submitted = await self._submit(
            {"White": True, "ListedSlaves": ["NodeB", "NodeC"]}, skip_worker_validation=True
        )
        self.assertEqual(submitted.get("Whitelist"), "NodeC")

    async def test_an_allow_list_nobody_can_serve_is_dropped_entirely(self) -> None:
        """Better anywhere than nowhere: a list nobody can serve strands it."""
        submitted = await self._submit(
            {"White": True, "ListedSlaves": ["NodeB"]}, skip_worker_validation=True
        )
        self.assertIsNone(submitted.get("Whitelist"))
        self.assertIsNone(submitted.get("Blacklist"))
        self.assertEqual(submitted["MachineLimit"], 0)

    async def test_a_named_worker_that_cannot_run_it_still_gets_a_preview(self) -> None:
        submitted = await self._submit({}, specific_worker="NodeB", skip_worker_validation=True)
        self.assertIsNone(submitted.get("Whitelist"))

    async def test_a_watching_user_is_told_instead(self) -> None:
        """Manual previews ask rather than silently going elsewhere."""
        from app.services.deadline import WorkerStatusError

        with self.assertRaises(WorkerStatusError):
            await self._submit({}, specific_worker="NodeB")


class WatcherRescueTests(unittest.IsolatedAsyncioTestCase):
    """The watcher moves a stranded preview instead of letting it wait."""

    def setUp(self) -> None:
        job_watcher._stranded_preview_since.clear()

    def _user(self):
        return job_watcher._WatcherUser(
            telegram_user_id=7,
            login="artist2",
            password="pw",
            notifications_enabled=True,
            notification_scope="all",
            auto_scope="all",
            preview_worker=None,
            auto_preview_enabled=True,
        )

    def _preview(self, stat: int = 1, rendering: int = 0) -> tuple:
        props = {
            "Name": "Shot - Preview",
            "ListedSlaves": ["NodeB"],
            "White": True,
            "ExDic": {
                "PreviewJob": "1",
                "PreviewPresubmit": "1",
                "PreviewSource": "src1",
                "PreviewTelegram": "7",
            },
        }
        return ("prev1", {"_id": "prev1", "Stat": stat, "RenderingChunks": rendering}, props)

    async def _check(self, preview) -> set:
        with mock.patch(
            "app.services.deadline.get_workers_by_credentials",
            new=mock.AsyncMock(return_value=FARM),
        ):
            return await job_watcher._strand_check(self._user(), [preview])

    async def test_a_worker_restart_is_given_time_to_come_back(self) -> None:
        self.assertEqual(await self._check(self._preview()), set())

    async def test_it_is_moved_once_the_grace_period_is_up(self) -> None:
        await self._check(self._preview())
        job_watcher._stranded_preview_since["prev1"] -= (
            job_watcher._STRANDED_PREVIEW_GRACE_SECONDS + 1
        )
        self.assertEqual(await self._check(self._preview()), {"prev1"})

    async def test_a_preview_that_is_already_rendering_is_left_alone(self) -> None:
        self.assertEqual(await self._check(self._preview(rendering=1)), set())

    async def test_a_pending_preview_is_not_stranded_it_is_waiting(self) -> None:
        self.assertEqual(await self._check(self._preview(stat=6)), set())

    async def test_rescue_replaces_the_job(self) -> None:
        _, _, props = self._preview()
        submit = mock.AsyncMock(return_value=True)
        with mock.patch(
            "app.services.deadline.delete_job", new=mock.AsyncMock(return_value=True)
        ) as delete, mock.patch(
            "app.services.preview.runtime._submit_auto_preview_deadline", new=submit
        ):
            await job_watcher._rescue_stranded_preview(self._user(), "prev1", props)

        delete.assert_awaited_once()
        submit.assert_awaited_once()
        self.assertEqual(submit.await_args.args[1], "src1")  # the source render

    async def test_nothing_is_deleted_when_the_source_is_unknown(self) -> None:
        with mock.patch(
            "app.services.deadline.delete_job", new=mock.AsyncMock(return_value=True)
        ) as delete:
            await job_watcher._rescue_stranded_preview(
                self._user(), "prev1", {"Name": "Shot - Preview"}
            )
        delete.assert_not_awaited()


class FailingPreviewTests(unittest.IsolatedAsyncioTestCase):
    """A preview that keeps failing is replaced once, then reported.

    A preview can fail on every run, for example when the bot refuses its
    upload token. Deadline hands a failed task straight back to the queue, so
    without this nothing stops it and nobody is told.
    """

    def setUp(self) -> None:
        job_watcher._recently_replaced_previews.clear()

    def _user(self):
        return job_watcher._WatcherUser(
            telegram_user_id=7,
            login="artist2",
            password="pw",
            notifications_enabled=True,
            notification_scope="all",
            auto_scope="all",
            preview_worker=None,
            auto_preview_enabled=True,
        )

    def _props(self) -> dict:
        return {
            "Name": "Shot - Preview",
            "ExDic": {
                "PreviewJob": "1",
                "PreviewPresubmit": "1",
                "PreviewSource": "src1",
                "PreviewTelegram": "7",
            },
        }

    async def _handle(self, errors: int, *, deleted: bool = True):
        rescue = mock.AsyncMock()
        delete = mock.AsyncMock(return_value=deleted)
        send = mock.AsyncMock()
        with mock.patch.object(job_watcher, "_rescue_stranded_preview", new=rescue),              mock.patch("app.services.deadline.delete_job", new=delete),              mock.patch.object(job_watcher.bot, "send_message", new=send):
            await job_watcher._rescue_failing_preview(
                self._user(), "prev1", {"_id": "prev1", "Errs": errors}, self._props()
            )
        return rescue, delete, send

    async def test_the_first_run_of_bad_luck_earns_a_fresh_preview(self) -> None:
        rescue, delete, send = await self._handle(job_watcher._PREVIEW_ERROR_LIMIT)
        rescue.assert_awaited_once()
        send.assert_not_awaited()

    async def test_a_replacement_that_fails_the_same_way_is_not_replaced_again(self) -> None:
        await self._handle(3)
        rescue, delete, send = await self._handle(3)

        rescue.assert_not_awaited()
        delete.assert_awaited_once()
        send.assert_awaited_once()
        self.assertIn("keeps failing", send.await_args.args[1])

    async def test_a_preview_that_would_not_go_is_not_reported_every_pass(self) -> None:
        """Still on the farm, it comes round again in thirty seconds; the user
        hears about it once it is gone, not once per pass."""
        await self._handle(3)
        rescue, delete, send = await self._handle(3, deleted=False)

        delete.assert_awaited_once()
        send.assert_not_awaited()

    async def test_a_preview_belonging_to_someone_else_is_left_alone(self) -> None:
        props = self._props()
        props["ExDic"]["PreviewTelegram"] = "999"
        rescue = mock.AsyncMock()
        with mock.patch.object(job_watcher, "_rescue_stranded_preview", new=rescue):
            await job_watcher._rescue_failing_preview(
                self._user(), "prev1", {"Errs": 9}, props
            )
        rescue.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
