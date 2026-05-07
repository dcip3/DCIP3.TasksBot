import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import deadline_preview_worker


class DeadlinePreviewWorkerUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "PREVIEW_UPLOAD_URL": "http://127.0.0.1:8081/preview-upload",
                "PREVIEW_UPLOAD_TOKEN": "token",
            },
            clear=False,
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.video_path = Path(temp_dir.name) / "preview.mp4"
        self.video_path.write_bytes(b"video")

    def test_upload_success_after_retry(self) -> None:
        with mock.patch.object(
            deadline_preview_worker,
            "_upload_preview_file",
            side_effect=[False, False, True],
        ) as upload_mock, mock.patch.object(
            deadline_preview_worker.time,
            "sleep",
        ) as sleep_mock:
            self.assertTrue(deadline_preview_worker._maybe_upload_preview(self.video_path))

        self.assertEqual(upload_mock.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [5, 15])

    def test_upload_failure_returns_false_after_retries(self) -> None:
        with mock.patch.object(
            deadline_preview_worker,
            "_upload_preview_file",
            return_value=False,
        ) as upload_mock, mock.patch.object(
            deadline_preview_worker.time,
            "sleep",
        ) as sleep_mock:
            self.assertFalse(deadline_preview_worker._maybe_upload_preview(self.video_path))

        self.assertEqual(upload_mock.call_count, 9)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list],
            [5, 15, 30, 60, 120, 240, 480, 480],
        )

    def test_upload_not_configured_is_success(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"PREVIEW_UPLOAD_URL": "", "PREVIEW_UPLOAD_TOKEN": ""},
        ), mock.patch.object(
            deadline_preview_worker,
            "_upload_preview_file",
        ) as upload_mock:
            self.assertTrue(deadline_preview_worker._maybe_upload_preview(self.video_path))

        upload_mock.assert_not_called()
