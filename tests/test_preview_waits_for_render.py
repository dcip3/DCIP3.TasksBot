"""A preview asked for before its render has a single frame.

22 Sep 2026: nodeb previewed SHC_0260_ID_v011 and SHD_0270_ID_v009 from the
chat while both renders were still queued. The previews went straight into the
queue at a higher priority than the renders, waited two minutes for frames,
found none, failed, and Deadline handed them back. NodeB and NodeC did nothing
else for two hours - about fifty failures each - while the render they were
keeping from the machines starved. The farm itself never stops such a loop:
task failure detection is off there, and a job only fails at 100 errors.

A preview from the chat is for checking a render while it runs, so it still
starts at once whenever there is something to show. When there is nothing,
the user is told so, and may have the preview sent when the render finishes.
"""

import base64
import json
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

from app.bot.handlers import preview as preview_handlers
from app.core.config import settings
from app.services import job_watcher
from app.services.preview import render, runtime


def render_job(stat: int, *, completed: int = 0, rendering: int = 0, queued: int = 8) -> dict:
    """The render as the REST job list reports it."""
    return {
        "_id": "src1",
        "Stat": stat,
        "CompletedChunks": completed,
        "RenderingChunks": rendering,
        "QueuedChunks": queued,
        "Props": {"Name": "SHC_0260_ID_v011 - /obj/ropnet1/SHD_0260_v002", "User": "nodea"},
        "OutDir": [r"Y:\projects\proj_c\render\SHC_0260\SHC_0260_v011\SHD_0260_v002"],
        "OutFile": ["SHC_0260_ID_v011_SHD_0260_v002_####.exr"],
    }


class NoFramesYetTests(unittest.TestCase):
    def test_a_queued_render_has_nothing_to_preview(self) -> None:
        """Both renders from the incident: queued, nothing started."""
        self.assertTrue(render.render_has_no_frames_yet(render_job(1)))

    def test_a_render_on_its_first_task_has_nothing_yet(self) -> None:
        self.assertTrue(render.render_has_no_frames_yet(render_job(1, rendering=1, queued=7)))

    def test_a_render_with_finished_tasks_can_be_previewed_now(self) -> None:
        """SHD_0010 at 29% got a useful preview of what was there."""
        self.assertFalse(render.render_has_no_frames_yet(render_job(1, completed=5, rendering=3)))

    def test_a_finished_render_can_be_previewed(self) -> None:
        self.assertFalse(render.render_has_no_frames_yet(render_job(3, completed=8, queued=0)))

    def test_suspended_and_pending_renders_without_frames_count(self) -> None:
        self.assertTrue(render.render_has_no_frames_yet(render_job(2)))
        self.assertTrue(render.render_has_no_frames_yet(render_job(6)))

    def test_a_render_in_an_unknown_state_is_not_guessed_at(self) -> None:
        job = render_job(1)
        del job["Stat"]
        self.assertFalse(render.render_has_no_frames_yet(job))


class SubmissionTestCase(unittest.IsolatedAsyncioTestCase):
    async def _submit(self, job_info: dict, **kwargs):
        captured: dict = {}
        self.submitted = captured

        async def _submit_job(**call):
            captured.update(call["job_info"])
            return {"job_id": "prev1"}

        with mock.patch(
            "app.auth.get_deadline_credentials",
            new=mock.AsyncMock(return_value=("nodeb", "pw")),
        ), mock.patch.object(
            render, "get_job_info", new=mock.AsyncMock(return_value=job_info)
        ), mock.patch.object(
            render, "get_workers_by_credentials", new=mock.AsyncMock(return_value=[])
        ), mock.patch.object(
            render, "submit_deadline_job", new=_submit_job
        ):
            result = await render.create_video_from_job(7, "src1", **kwargs)
        return captured, result

    @staticmethod
    def _worker_argv(job_info: dict) -> list:
        for key, value in job_info.items():
            if key.startswith("EnvironmentKeyValue") and value.startswith("PREVIEW_ARGV_B64="):
                return json.loads(base64.b64decode(value.split("=", 1)[1]))
        raise AssertionError("worker arguments missing")


class ChatPreviewSubmissionTests(SubmissionTestCase):
    """What reaches Deadline when 🔍 Preview is pressed in the chat."""

    async def test_nothing_is_submitted_for_a_render_without_frames(self) -> None:
        """The incident: this preview used to start at once and loop."""
        with self.assertRaises(render.NoFramesYetError) as caught:
            await self._submit(render_job(1), if_no_frames="refuse")
        self.assertIn("Nothing to preview yet", caught.exception.user_message)
        self.assertEqual(self.submitted, {})

    async def test_a_render_with_frames_is_previewed_now(self) -> None:
        submitted, result = await self._submit(
            render_job(1, completed=5, rendering=3), if_no_frames="refuse"
        )
        self.assertNotIn("JobDependencies", submitted)
        self.assertNotIn("ExtraInfoKeyValue6", submitted)
        self.assertFalse(result["waits_for_render"])

    async def test_a_finished_render_is_previewed_now(self) -> None:
        submitted, result = await self._submit(
            render_job(3, completed=8, queued=0), if_no_frames="refuse"
        )
        self.assertNotIn("JobDependencies", submitted)
        self.assertFalse(result["waits_for_render"])

    async def test_asked_to_wait_it_waits_whatever_is_rendered(self) -> None:
        submitted, result = await self._submit(
            render_job(1),
            depends_on="src1",
            presubmitted=True,
            input_wait_seconds=settings.preview_presubmit_input_wait,
            if_no_frames="refuse",
        )
        self.assertEqual(submitted.get("JobDependencies"), "src1")
        self.assertEqual(submitted.get("ExtraInfoKeyValue6"), "PreviewPresubmit=1")
        self.assertTrue(result["waits_for_render"])

    async def test_a_render_that_failed_before_any_frame_is_refused(self) -> None:
        """Nothing will ever be there, so there is nothing to wait for either."""
        with self.assertRaises(render.PreviewSubmissionError) as caught:
            await self._submit(render_job(4), if_no_frames="refuse")
        self.assertNotIsInstance(caught.exception, render.NoFramesYetError)
        self.assertIn("failed before finishing a single frame", caught.exception.user_message)


class BotQueuedPreviewSubmissionTests(SubmissionTestCase):
    """Previews the bot queues itself - replacements above all - never loop."""

    async def test_a_replacement_for_a_render_without_frames_waits_for_it(self) -> None:
        submitted, result = await self._submit(render_job(1))
        self.assertEqual(submitted.get("JobDependencies"), "src1")
        self.assertEqual(submitted.get("ResumeOnCompleteDependencies"), "true")
        self.assertEqual(submitted.get("ExtraInfoKeyValue6"), "PreviewPresubmit=1")
        self.assertTrue(result["waits_for_render"])

    async def test_a_held_preview_gives_the_frames_time_to_arrive(self) -> None:
        """Released the moment the render ends, like an automatic one, so it
        needs the same patience for frames still syncing from other machines."""
        submitted, _ = await self._submit(render_job(1))
        argv = self._worker_argv(submitted)
        wait = argv[argv.index("--input-wait-seconds") + 1]
        self.assertEqual(int(wait), settings.preview_presubmit_input_wait)

    async def test_an_automatic_preview_keeps_its_own_dependency(self) -> None:
        submitted, result = await self._submit(
            render_job(1, rendering=2), depends_on="src1", presubmitted=True, input_wait_seconds=900
        )
        self.assertEqual(submitted.get("JobDependencies"), "src1")
        argv = self._worker_argv(submitted)
        self.assertEqual(argv[argv.index("--input-wait-seconds") + 1], "900")
        self.assertTrue(result["waits_for_render"])

    async def test_no_dependency_on_a_render_that_has_already_finished(self) -> None:
        """It would only sit Pending until the farm's next pending scan."""
        submitted, result = await self._submit(
            render_job(3, completed=8, queued=0), depends_on="src1", presubmitted=True
        )
        self.assertNotIn("JobDependencies", submitted)
        self.assertFalse(result["waits_for_render"])

    async def test_deadline_stops_a_preview_that_keeps_failing(self) -> None:
        """This farm never fails a task by itself; every preview brings a limit."""
        for kwargs in ({}, {"if_no_frames": "refuse"}):
            submitted, _ = await self._submit(render_job(3, completed=8, queued=0), **kwargs)
            self.assertEqual(submitted.get("OverrideTaskFailureDetection"), "true")
            self.assertEqual(
                submitted.get("FailureDetectionTaskErrors"), render.PREVIEW_TASK_ERROR_LIMIT
            )

    def test_the_watcher_gets_its_turn_before_deadline_gives_up(self) -> None:
        """The watcher replaces a failing preview once (a fresh upload token is
        the failure worth retrying); Deadline failing it first would skip that."""
        self.assertGreater(render.PREVIEW_TASK_ERROR_LIMIT, job_watcher._PREVIEW_ERROR_LIMIT)


class RequestFromChatTests(unittest.IsolatedAsyncioTestCase):
    """What the person who pressed 🔍 Preview sees."""

    def setUp(self) -> None:
        runtime.preview_message_registry.clear()
        runtime.preview_tracked_jobs.clear()

    def tearDown(self) -> None:
        for task in list(runtime.preview_animation_tasks.values()):
            task.cancel()
        runtime.preview_animation_tasks.clear()
        runtime.preview_message_registry.clear()
        runtime.preview_tracked_jobs.clear()

    @staticmethod
    def _callback(data: str = "preview_job:src1"):
        message = mock.Mock()
        message.chat.id = 100000002
        message.message_id = 55
        message.edit_text = mock.AsyncMock()
        callback = mock.Mock()
        callback.data = data
        callback.from_user.id = 100000002
        callback.message = message
        callback.message.answer = mock.AsyncMock(return_value=message)
        callback.answer = mock.AsyncMock()
        return callback, message

    async def _press(self, submit: mock.AsyncMock, **kwargs):
        callback, message = self._callback()
        with mock.patch.object(
            preview_handlers, "create_video_from_job", new=submit
        ), mock.patch.object(
            runtime, "_run_preview_animation", new=mock.AsyncMock()
        ):
            await preview_handlers.create_new_video_process(callback, "src1", **kwargs)
        return message

    async def test_a_preview_asks_for_what_is_there_now(self) -> None:
        submit = mock.AsyncMock(return_value={"preview_job_id": "prev1", "waits_for_render": False})
        await self._press(submit)
        self.assertEqual(submit.await_args.kwargs["if_no_frames"], "refuse")
        self.assertIsNone(submit.await_args.kwargs["depends_on"])

    async def test_nothing_rendered_yet_is_said_at_once(self) -> None:
        """No job on the farm, and a way to get the preview later."""
        submit = mock.AsyncMock(side_effect=preview_handlers.NoFramesYetError("Nothing to preview yet: …"))
        message = await self._press(submit)
        text = message.edit_text.await_args.args[0]
        self.assertTrue(text.startswith("⚠️ Nothing to preview yet"))
        buttons = [
            button.callback_data
            for row in message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("preview_after_render:src1", buttons)
        self.assertFalse(runtime.preview_message_registry)
        self.assertFalse(runtime.preview_tracked_jobs)

    async def test_send_it_when_the_render_finishes(self) -> None:
        submit = mock.AsyncMock(return_value={"preview_job_id": "prev1", "waits_for_render": True})
        callback, message = self._callback("preview_after_render:src1")
        with mock.patch.object(
            preview_handlers, "create_video_from_job", new=submit
        ), mock.patch.object(
            preview_handlers, "get_preview_default_worker", new=mock.AsyncMock(return_value=None)
        ):
            await preview_handlers.preview_after_render_callback(callback)

        kwargs = submit.await_args.kwargs
        self.assertEqual(kwargs["depends_on"], "src1")
        self.assertTrue(kwargs["presubmitted"])
        self.assertEqual(kwargs["input_wait_seconds"], settings.preview_presubmit_input_wait)
        self.assertIn("starts as soon as the render completes", message.edit_text.await_args.args[0])
        # Hours of waiting are not animated into Telegram's rate limits.
        self.assertNotIn("prev1", runtime.preview_animation_tasks)
        self.assertEqual(runtime.preview_message_registry.get("prev1"), (100000002, 55))
        # The bot can release it itself when the render finishes.
        self.assertEqual(runtime.preview_tracked_jobs.get("prev1"), (100000002, "src1"))

    async def test_a_named_default_worker_is_kept_for_the_wait(self) -> None:
        submit = mock.AsyncMock(return_value={"preview_job_id": "prev1", "waits_for_render": True})
        callback, _ = self._callback("preview_after_render:src1")
        with mock.patch.object(
            preview_handlers, "create_video_from_job", new=submit
        ), mock.patch.object(
            preview_handlers, "get_preview_default_worker", new=mock.AsyncMock(return_value="NodeC")
        ):
            await preview_handlers.preview_after_render_callback(callback)
        self.assertEqual(submit.await_args.kwargs["specific_worker"], "NodeC")

    async def test_a_preview_that_runs_now_is_animated_as_before(self) -> None:
        submit = mock.AsyncMock(return_value={"preview_job_id": "prev2", "waits_for_render": False})
        message = await self._press(submit)
        self.assertIn("Preview job queued", message.edit_text.await_args.args[0])
        self.assertIn("prev2", runtime.preview_animation_tasks)
        self.assertNotIn("prev2", runtime.preview_tracked_jobs)


if __name__ == "__main__":
    unittest.main()
