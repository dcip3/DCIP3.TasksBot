"""A preview queued before its render finished waits for the frames, not a worker.

Frames rendered on other machines reach the preview's machine through
Dropbox. Heavy EXRs (45-110 MB) arrived 20-45 minutes after they were written,
and a preview released when its render finished spent that time on a worker,
waiting for them - one sat on a machine for over half an hour while renders
queued behind it. These previews now list the frames as required assets and
stay Pending until they are there. Deadline checks such files on its Linux
server, where the Y: drive does not exist, so the farm's event plugin releases
them from a worker; the bot only has to leave them alone and catch the ones
whose frames never come.
"""

import base64
import json
import os
import sys
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

from app.bot.handlers import preview as preview_handlers
from app.core import farm_events
from app.core.config import settings
from app.services import deadline, job_watcher
from app.services.preview import render, runtime

OUT_DIR = r"Y:\projects\proj_c\render\SHC_0071\SHC_0071_v04\SHD_0071_v3"


def iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def render_job(
    stat: int = 3,
    *,
    frames: str = "1-30",
    out_file: str = "SHC_0071_v04_SHD_0071_v3_####.exr",
    out_dir: str = OUT_DIR,
    finished_minutes_ago: float = 1,
) -> dict:
    return {
        "_id": "src1",
        "Stat": stat,
        "CompletedChunks": 6 if stat == 3 else 2,
        "DateComp": iso(finished_minutes_ago) if stat == 3 else "0001-01-01T00:00:00Z",
        "Props": {"Name": "SHC_0071_v04 - /out/SHD_0071_v3", "User": "nodeb", "Frames": frames},
        "OutDir": [out_dir],
        "OutFile": [out_file] if out_file else [],
    }


class FramePathTests(unittest.TestCase):
    def test_every_frame_of_the_sequence_is_named(self) -> None:
        paths = render.expected_frame_paths(OUT_DIR + r"\SHC_%04d.exr", "1-3")
        self.assertEqual(
            paths,
            [OUT_DIR + r"\SHC_0001.exr", OUT_DIR + r"\SHC_0002.exr", OUT_DIR + r"\SHC_0003.exr"],
        )

    def test_steps_and_lists_follow_the_frames_spec(self) -> None:
        paths = render.expected_frame_paths(r"C:\r\f_%03d.exr", "1-9x4,20")
        self.assertEqual(paths, [r"C:\r\f_001.exr", r"C:\r\f_005.exr", r"C:\r\f_009.exr", r"C:\r\f_020.exr"])

    def test_a_reversed_range_is_walked_from_its_first_frame(self) -> None:
        """Deadline renders 100-1x2 as 100, 98, ... 2 - not 1, 3, ... 99."""
        paths = render.expected_frame_paths(r"C:\r\f_%04d.exr", "100-1x2")
        self.assertEqual(paths[0], r"C:\r\f_0100.exr")
        self.assertEqual(paths[-1], r"C:\r\f_0002.exr")
        self.assertEqual(render.expected_frame_paths(r"C:\r\f_%04d.exr", "5-1x10"), [r"C:\r\f_0005.exr"])

    def test_the_count_matches_what_the_worker_expects(self) -> None:
        spec = "1-100x3,150-160,7,40-10x4"
        self.assertEqual(
            len(render.expected_frame_paths(r"C:\r\f_%04d.exr", spec)),
            render._count_expected_frames(spec),
        )

    def test_a_still_without_a_frame_number_is_its_own_file(self) -> None:
        self.assertEqual(render.expected_frame_paths(r"C:\r\still.exr", "1"), [r"C:\r\still.exr"])

    def test_output_that_cannot_be_named_is_not_waited_on(self) -> None:
        """No OutFile on the job: the worker globs *.exr, and so cannot Deadline."""
        self.assertEqual(render.expected_frame_paths(r"C:\r\*.exr", "1-30"), [])

    def test_a_path_with_a_comma_is_not_listed(self) -> None:
        """Deadline's asset list is comma-separated; the path would fall apart."""
        self.assertEqual(render.expected_frame_paths(r"Y:\Clients\Smith, Jones\f_%04d.exr", "1-3"), [])

    def test_a_long_sequence_lists_its_last_frames_in_full(self) -> None:
        paths = render.expected_frame_paths(r"C:\r\f_%04d.exr", "1-2000")
        self.assertEqual(len(paths), render.MAX_FRAME_ASSETS)
        self.assertEqual(paths[0], r"C:\r\f_0001.exr")
        tail = render.MAX_FRAME_ASSETS // 2
        self.assertEqual(paths[-tail:], [rf"C:\r\f_{n:04d}.exr" for n in range(2001 - tail, 2001)])


class SubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def _submit(self, job_info: dict, **kwargs):
        captured: dict = {}

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
    def _input_wait(job_info: dict) -> int:
        for key, value in job_info.items():
            if key.startswith("EnvironmentKeyValue") and value.startswith("PREVIEW_ARGV_B64="):
                argv = json.loads(base64.b64decode(value.split("=", 1)[1]))
                return int(argv[argv.index("--input-wait-seconds") + 1]) if "--input-wait-seconds" in argv else 0
        raise AssertionError("worker arguments missing")

    GATED = {"wait_for_frames": True, "depends_on": "src1", "presubmitted": True}

    async def test_a_preview_of_the_whole_render_waits_for_its_frames(self) -> None:
        submitted, result = await self._submit(render_job(1), **self.GATED)
        assets = submitted["RequiredAssets"].split(",")
        self.assertEqual(len(assets), 30)
        self.assertEqual(assets[0], OUT_DIR + r"\SHC_0071_v04_SHD_0071_v3_0001.exr")
        self.assertEqual(assets[-1], OUT_DIR + r"\SHC_0071_v04_SHD_0071_v3_0030.exr")
        self.assertTrue(result["waits_for_frames"])

    async def test_the_assets_are_the_files_the_worker_reads(self) -> None:
        submitted, _ = await self._submit(render_job(1), **self.GATED)
        env = next(v for k, v in submitted.items() if k.startswith("EnvironmentKeyValue") and v.startswith("PREVIEW_ARGV_B64="))
        argv = json.loads(base64.b64decode(env.split("=", 1)[1]))
        pattern = argv[argv.index("--input-pattern") + 1]
        self.assertEqual(submitted["RequiredAssets"].split(",")[0], pattern % 1)

    async def test_released_early_it_still_waits_for_its_frames_on_the_worker(self) -> None:
        """The machine that checks sees its own frames before anyone else; the
        worker's wait is what keeps an early release from a partial preview."""
        submitted, _ = await self._submit(render_job(1), **self.GATED, input_wait_seconds=900)
        self.assertEqual(self._input_wait(submitted), settings.preview_presubmit_input_wait)

    async def test_a_preview_queued_after_the_render_finished_is_not_held(self) -> None:
        """Nothing on a quiet farm would release it: no worker event is left.
        It waits for its frames on its worker, as before."""
        submitted, result = await self._submit(render_job(), wait_for_frames=True)
        self.assertNotIn("RequiredAssets", submitted)
        self.assertFalse(result["waits_for_frames"])
        # The render finished while the preview was being queued: no dependency,
        # so no frame list either.
        submitted, result = await self._submit(render_job(), **self.GATED)
        self.assertNotIn("JobDependencies", submitted)
        self.assertNotIn("RequiredAssets", submitted)

    async def test_a_preview_from_the_chat_shows_what_is_there_now(self) -> None:
        submitted, result = await self._submit(render_job(), if_no_frames="refuse")
        self.assertNotIn("RequiredAssets", submitted)
        self.assertFalse(result["waits_for_frames"])

    async def test_output_that_cannot_be_listed_keeps_the_callers_wait(self) -> None:
        for job in (render_job(out_file=""), render_job(out_dir=r"Y:\Clients\Smith, Jones\render")):
            job["Stat"] = 1
            submitted, result = await self._submit(job, **self.GATED, input_wait_seconds=900)
            self.assertNotIn("RequiredAssets", submitted)
            self.assertEqual(self._input_wait(submitted), 900)
            self.assertFalse(result["waits_for_frames"])


class WhoWaitsForFramesTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_automatic_preview_waits_for_frames(self) -> None:
        create = mock.AsyncMock(return_value={"preview_job_id": "prev1"})
        with mock.patch("app.services.preview.render.create_video_from_job", new=create):
            await runtime._submit_auto_preview_deadline(42, "src1", "Shot", None, notify_on_failure=False)
        runtime.untrack_preview_job("prev1")
        self.assertTrue(create.await_args.kwargs["wait_for_frames"])

    async def _press(self, **kwargs):
        message = mock.Mock()
        message.chat.id = 5
        message.message_id = 6
        message.edit_text = mock.AsyncMock()
        callback = mock.Mock()
        callback.from_user.id = 5
        callback.message.answer = mock.AsyncMock(return_value=message)
        callback.answer = mock.AsyncMock()
        create = mock.AsyncMock(return_value={"preview_job_id": "prev9", "waits_for_render": False})
        with mock.patch.object(preview_handlers, "create_video_from_job", new=create), mock.patch.object(
            runtime, "_run_preview_animation", new=mock.AsyncMock()
        ):
            await preview_handlers.create_new_video_process(callback, "src1", **kwargs)
        runtime.pop_preview_message("prev9")
        return create.await_args.kwargs

    async def test_a_preview_from_the_chat_does_not(self) -> None:
        self.assertFalse((await self._press())["wait_for_frames"])

    async def test_one_sent_when_the_render_finishes_does(self) -> None:
        self.assertTrue((await self._press(wait_for_render=True))["wait_for_frames"])

    async def _replace(self, props: dict) -> dict:
        user = mock.Mock(telegram_user_id=42, login="nodeb", password="pw", preview_worker=None)
        submit = mock.AsyncMock(return_value=True)
        with mock.patch.object(deadline, "delete_job", new=mock.AsyncMock(return_value=True)), mock.patch(
            "app.services.preview.runtime._submit_auto_preview_deadline", new=submit
        ):
            await job_watcher._rescue_stranded_preview(user, "prev1", props)
        return submit.await_args.kwargs

    async def test_a_replacement_waits_as_the_preview_it_replaces_did(self) -> None:
        """A chat preview of the frames so far is replaced by one that runs now."""
        chat_now = preview_job(assets=False, presubmitted=False)["Props"]
        gated = preview_job(assets=True, presubmitted=False)["Props"]
        held = preview_job(assets=False, presubmitted=True)["Props"]
        self.assertFalse((await self._replace(chat_now))["wait_for_frames"])
        self.assertTrue((await self._replace(gated))["wait_for_frames"])
        self.assertTrue((await self._replace(held))["wait_for_frames"])


def preview_job(*, assets: bool, stat: int = 6, owner: int = 42, presubmitted: bool = True, date: str = "") -> dict:
    extra = {"PreviewJob": "1", "PreviewTelegram": str(owner), "PreviewSource": "src1"}
    if presubmitted:
        extra["PreviewPresubmit"] = "1"
    props = {"Name": "SHC_0071_v04 - /out/SHD_0071_v3 - Preview", "ExDic": extra}
    if assets:
        props["ReqAss"] = [{"FileName": OUT_DIR + r"\SHC_0001.exr"}]
    return {"_id": "prev1", "Stat": stat, "Errs": 0, "Date": date, "Props": props}


class BotReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def _release_on_finish(self, preview):
        runtime.track_preview_job("prev1", 42, "src1")
        release = mock.AsyncMock(return_value=True)
        try:
            with mock.patch.object(
                deadline, "get_job_info_by_user_id", new=mock.AsyncMock(return_value=preview)
            ), mock.patch.object(deadline, "release_pending_job_by_user_id", new=release):
                released = await farm_events.release_previews_waiting_on("src1")
        finally:
            runtime.untrack_preview_job("prev1")
        return released, release

    async def test_a_preview_waiting_for_frames_is_left_to_the_farm(self) -> None:
        """A release from the bot would skip Deadline's check for the files."""
        released, release = await self._release_on_finish(preview_job(assets=True))
        self.assertEqual(released, 0)
        release.assert_not_awaited()

    async def test_a_preview_it_could_not_look_up_is_left_to_the_farm_too(self) -> None:
        released, release = await self._release_on_finish(None)
        self.assertEqual(released, 0)
        release.assert_not_awaited()

    async def test_a_preview_without_assets_is_released_at_once(self) -> None:
        released, release = await self._release_on_finish(preview_job(assets=False))
        self.assertEqual(released, 1)
        release.assert_awaited_once_with(42, "prev1")


class OverdueFramesTests(unittest.IsolatedAsyncioTestCase):
    """Frames that never arrive must not leave a preview Pending for ever."""

    OVERDUE = job_watcher._FRAMES_OVERDUE_SECONDS / 60 + 1

    async def _reconcile(self, jobs: list, owner: int = 42):
        user = mock.Mock(telegram_user_id=owner, login="nodeb", password="pw")
        replace = mock.AsyncMock()
        with mock.patch.object(
            job_watcher, "_strand_check", new=mock.AsyncMock(return_value=set())
        ), mock.patch.object(job_watcher, "_rescue_stranded_preview", new=replace), mock.patch.object(
            deadline, "delete_job", new=mock.AsyncMock(return_value=True)
        ), mock.patch.object(job_watcher, "_unregister_auto_preview_history", new=mock.AsyncMock()):
            await job_watcher._reconcile_previews(user, jobs)
        return replace

    def _render(self, *, stat: int = 3, finished_minutes_ago: float = 0) -> dict:
        return {"_id": "src1", "Stat": stat, "DateComp": iso(finished_minutes_ago), "Props": {"Name": "SHC_0071_v04"}}

    async def test_frames_missing_long_after_the_render_finished(self) -> None:
        """Replaced by a preview that runs now with what is there, briefly waiting."""
        replace = await self._reconcile([self._render(finished_minutes_ago=self.OVERDUE), preview_job(assets=True)])
        replace.assert_awaited_once()
        self.assertFalse(replace.await_args.kwargs["wait_for_frames"])
        self.assertEqual(replace.await_args.kwargs["input_wait_seconds"], job_watcher._OVERDUE_INPUT_WAIT_SECONDS)

    async def test_frames_still_on_their_way_are_waited_for(self) -> None:
        replace = await self._reconcile([self._render(finished_minutes_ago=40), preview_job(assets=True)])
        replace.assert_not_awaited()

    async def test_a_render_still_running_is_not_overdue(self) -> None:
        replace = await self._reconcile([self._render(stat=1), preview_job(assets=True)])
        replace.assert_not_awaited()

    async def test_someone_elses_preview_is_left_alone(self) -> None:
        replace = await self._reconcile(
            [self._render(finished_minutes_ago=self.OVERDUE), preview_job(assets=True)], owner=7
        )
        replace.assert_not_awaited()

    async def test_a_completion_time_preview_of_a_render_gone_or_failed(self) -> None:
        """No finished render to date it by, so its own submission does."""
        for jobs in (
            [preview_job(assets=True, presubmitted=False, date=iso(self.OVERDUE))],
            [self._render(stat=4), preview_job(assets=True, presubmitted=False, date=iso(self.OVERDUE))],
        ):
            replace = await self._reconcile(jobs)
            replace.assert_awaited_once()

    async def test_a_preview_without_assets_is_not_touched_here(self) -> None:
        replace = await self._reconcile([self._render(finished_minutes_ago=self.OVERDUE), preview_job(assets=False)])
        replace.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
