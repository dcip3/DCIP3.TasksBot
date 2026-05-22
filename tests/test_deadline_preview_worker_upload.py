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

    def test_single_frame_preview_writes_png_without_video_encode(self) -> None:
        source_frame = self.video_path.with_name("preview.0001.jpg")
        source_frame.write_bytes(b"jpg")
        output_path = self.video_path.with_suffix(".png")

        def write_png(**kwargs) -> None:
            kwargs["output_path"].write_bytes(b"png")

        with mock.patch.object(
            deadline_preview_worker,
            "_convert_single_frame_to_png",
            side_effect=write_png,
        ) as convert_mock, mock.patch.object(
            deadline_preview_worker,
            "_maybe_upload_preview",
            return_value=True,
        ) as upload_mock, mock.patch.object(
            deadline_preview_worker,
            "run_ffmpeg",
        ) as ffmpeg_mock:
            result = deadline_preview_worker.main(
                [
                    "--input-pattern",
                    str(source_frame.with_name("preview.%04d.jpg")),
                    "--output-path",
                    str(output_path),
                    "--start-number",
                    "1",
                    "--expected-frames",
                    "1",
                    "--disable-color",
                ]
            )

        self.assertEqual(result, 0)
        convert_mock.assert_called_once()
        self.assertEqual(convert_mock.call_args.kwargs["input_path"], source_frame)
        self.assertEqual(convert_mock.call_args.kwargs["output_path"], output_path)
        upload_mock.assert_called_once_with(output_path)
        ffmpeg_mock.assert_not_called()
