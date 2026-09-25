"""A delivered preview's job is deleted once its worker is done with it.

The worker uploads the video just before its process exits, so the task is
still rendering in Deadline when the video is in the chat. The bot deleted the
job right then, and the worker, reporting a task of a job that was gone, logged
a NullReferenceException and waited twenty seconds before it took other work -
on every preview. The deletion now waits for the task, and the upload token is
kept, marked delivered, until the job is gone, so a restart in between neither
sends the video again nor reports it missing.
"""

import json
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

from app.core import preview_upload
from app.core.config import settings
from app.services import deadline
from app.services.preview import runtime


class DeleteOnceFinishedTests(unittest.IsolatedAsyncioTestCase):
    """The worker reports its task after its process exits; delete after that."""

    async def _run(self, lookups: list, *, deleted=(True,), **kwargs):
        lookup = mock.AsyncMock(side_effect=lookups)
        delete = mock.AsyncMock(side_effect=list(deleted))
        with mock.patch.object(deadline, "_lookup_job", new=lookup), mock.patch.object(
            deadline, "delete_job", new=delete
        ), mock.patch.object(deadline.asyncio, "sleep", new=mock.AsyncMock()):
            result = await deadline.delete_job_once_finished("nodeb", "pw", "prev1", **kwargs)
        return result, lookup, delete

    async def test_waits_while_the_task_is_still_rendering(self) -> None:
        rendering = (True, {"Stat": 1, "RenderingChunks": 1})
        done = (True, {"Stat": 3, "RenderingChunks": 0})
        result, lookup, delete = await self._run([rendering, rendering, done])
        self.assertTrue(result)
        self.assertEqual(lookup.await_count, 3)
        delete.assert_awaited_once_with("nodeb", "pw", "prev1")

    async def test_a_job_already_gone_is_not_deleted_again(self) -> None:
        result, _, delete = await self._run([(False, None)])
        self.assertTrue(result)
        delete.assert_not_awaited()

    async def test_a_failed_lookup_is_not_taken_for_a_deleted_job(self) -> None:
        done = (True, {"Stat": 3, "RenderingChunks": 0})
        result, lookup, delete = await self._run([(None, None), (None, None), done])
        self.assertEqual(lookup.await_count, 3)
        delete.assert_awaited_once()
        self.assertTrue(result)

    async def test_a_worker_that_never_reports_does_not_keep_the_job(self) -> None:
        rendering = (True, {"Stat": 1, "RenderingChunks": 1})
        _, _, delete = await self._run([rendering] * 5, wait_seconds=0)
        delete.assert_awaited_once()

    async def test_a_refused_delete_is_tried_again(self) -> None:
        done = (True, {"Stat": 3})
        result, _, delete = await self._run([done], deleted=(False, False, True))
        self.assertTrue(result)
        self.assertEqual(delete.await_count, 3)


class LookupTests(unittest.IsolatedAsyncioTestCase):
    """Deadline answers an unknown JobID with 200 and an empty body."""

    async def _lookup(self, status: int, body: str):
        response = mock.MagicMock()
        response.status = status
        response.text = mock.AsyncMock(return_value=body)
        context = mock.MagicMock()
        context.__aenter__ = mock.AsyncMock(return_value=response)
        context.__aexit__ = mock.AsyncMock(return_value=False)
        session = mock.Mock(get=mock.Mock(return_value=context))
        with mock.patch.object(deadline, "get_aiosession", new=mock.AsyncMock(return_value=session)):
            return await deadline._lookup_job("nodeb", "pw", "prev1")

    async def test_an_empty_answer_means_gone(self) -> None:
        self.assertEqual(await self._lookup(200, ""), (False, None))

    async def test_the_job_is_found(self) -> None:
        found, job = await self._lookup(200, json.dumps([{"_id": "prev1", "Stat": 1}]))
        self.assertTrue(found)
        self.assertEqual(job["Stat"], 1)

    async def test_an_error_says_nothing(self) -> None:
        self.assertEqual(await self._lookup(401, "Unauthorized"), (None, None))
        self.assertEqual(await self._lookup(500, ""), (None, None))


class DeliveredTokenTests(unittest.IsolatedAsyncioTestCase):
    """A delivered preview's token outlives delivery until its job is deleted."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = mock.patch.object(settings, "sqlite_db_path", str(Path(self.temp.name) / "t.db"))
        self.db.start()
        self.store = preview_upload.PreviewUploadTokenStore(ttl_seconds=3600)

    async def asyncTearDown(self) -> None:
        self.db.stop()
        self.temp.cleanup()

    async def test_a_delivered_token_is_kept_but_never_taken_again(self) -> None:
        payload = preview_upload.PreviewUploadPayload(
            telegram_user_id=42, job_name="Shot", expected_dropbox_path=None,
            expected_filename="p.mp4", expected_local_path=None, preview_job_id="prev1",
        )
        token = await self.store.issue(payload)
        await self.store.mark_delivered(token)
        self.assertIsNone(await self.store.claim(token))
        state = await self.store.get_by_preview_job("prev1")
        self.assertEqual(state.status, preview_upload.STATUS_DELIVERED)
        self.assertIn(token, [s.token for s in await self.store.list_recoverable()])

    async def test_delivery_marks_it_and_cleans_up_after_the_worker(self) -> None:
        payload = preview_upload.PreviewUploadPayload(
            telegram_user_id=42, job_name="Shot", expected_dropbox_path=None,
            expected_filename="p.png", expected_local_path=None, preview_job_id="prev1",
        )
        state = mock.Mock(payload=payload)
        store = mock.Mock(
            mark_delivery_started=mock.AsyncMock(return_value=state),
            mark_delivered=mock.AsyncMock(),
            consume_claimed=mock.AsyncMock(),
        )
        with mock.patch.object(preview_upload, "_token_store", store), mock.patch.object(
            preview_upload, "_resolve_upload_temp_path", return_value=Path("p.png")
        ), mock.patch.object(preview_upload, "_deliver_preview", new=mock.AsyncMock()), mock.patch.object(
            preview_upload, "finish_delivered_preview"
        ) as finish:
            await preview_upload._deliver_received_upload("tok")
        store.mark_delivered.assert_awaited_once_with("tok")
        store.consume_claimed.assert_not_awaited()
        finish.assert_called_once_with("tok", 42, "prev1")

    async def test_the_job_is_deleted_and_then_the_token_dropped(self) -> None:
        store = mock.Mock(drop=mock.AsyncMock())
        with mock.patch.object(preview_upload, "_token_store", store), mock.patch.object(
            deadline, "delete_job_once_finished_by_user_id", new=mock.AsyncMock(return_value=True)
        ):
            await preview_upload._delete_delivered_preview_job("tok", 42, "prev1")
        store.drop.assert_awaited_once_with("tok")

    async def test_a_restart_resumes_the_cleanup(self) -> None:
        state = mock.Mock(
            status=preview_upload.STATUS_DELIVERED, token="tok", preview_job_id="prev1",
            payload=mock.Mock(telegram_user_id=42),
        )
        store = mock.Mock(cleanup=mock.AsyncMock(), list_recoverable=mock.AsyncMock(return_value=[state]))
        with mock.patch.object(settings, "preview_upload_enabled", True), mock.patch.object(
            preview_upload, "_token_store", store
        ), mock.patch.object(preview_upload, "finish_delivered_preview") as finish, mock.patch.object(
            preview_upload, "_start_delivery_task"
        ) as deliver:
            await preview_upload.recover_preview_uploads()
        finish.assert_called_once_with("tok", 42, "prev1")
        deliver.assert_not_called()

    async def test_after_a_restart_the_watcher_does_not_report_it_missing(self) -> None:
        """The job completed after its video went out; no 'file not available'."""
        state = mock.Mock(status=preview_upload.STATUS_DELIVERED, token="tok")
        job = {"_id": "prev1", "Stat": 3, "Props": {"ExDic": {"PreviewLocal": r"Y:\r\p.mp4", "PreviewTelegram": "42"}}}
        with mock.patch(
            "app.core.preview_upload.get_preview_upload_state_for_job", new=mock.AsyncMock(return_value=state)
        ), mock.patch("app.core.preview_upload.finish_delivered_preview") as finish, mock.patch.object(
            runtime.bot, "send_message", new=mock.AsyncMock()
        ) as send:
            result = await runtime._notify_preview_job_completion(42, job, "Shot", "nodeb", "pw")
        self.assertEqual(result.status, "notified")
        finish.assert_called_once_with("tok", 42, "prev1")
        send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
