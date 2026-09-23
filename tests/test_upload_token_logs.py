"""Upload tokens stay out of the logs.

Whoever holds a live upload token can post a file in the worker's place. A
received upload sits in a directory named after its full token, so a log line
that prints the file's path, or an error that carries it, prints the token too.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core import preview_upload
from app.integrations import video_helpers

TOKEN = "Tk3nW1thEnoughCharsToBeARealUploadToken0123"
UPLOAD_PATH = Path("data") / "temp" / f"upload_{TOKEN}" / "preview.mp4"


class UploadTokenLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_delivery_logs_only_the_hint(self) -> None:
        state = SimpleNamespace(payload=object(), attempts_exhausted=False)
        store = mock.Mock(
            mark_delivery_started=mock.AsyncMock(return_value=state),
            mark_delivery_failed=mock.AsyncMock(),
            get_state=mock.AsyncMock(return_value=state),
        )
        missing = FileNotFoundError(2, "No such file or directory", str(UPLOAD_PATH))
        with mock.patch.object(preview_upload, "_token_store", store), mock.patch.object(
            preview_upload, "_resolve_upload_temp_path", return_value=UPLOAD_PATH
        ), mock.patch.object(
            preview_upload, "_deliver_preview", new=mock.AsyncMock(side_effect=missing)
        ), self.assertLogs(preview_upload.logger, level="ERROR") as logs:
            await preview_upload._deliver_received_upload(TOKEN)

        output = "\n".join(logs.output)
        self.assertNotIn(TOKEN, output)
        self.assertIn(f"upload_{TOKEN[:8]}...", output)

    def test_ffprobe_failure_logs_the_file_name(self) -> None:
        failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=f"{UPLOAD_PATH}: Invalid data"
        )
        with mock.patch.object(
            video_helpers.subprocess, "run", return_value=failed
        ), self.assertLogs(video_helpers.logger, level="WARNING") as logs:
            self.assertIsNone(video_helpers.probe_video_metadata(UPLOAD_PATH))

        output = "\n".join(logs.output)
        self.assertNotIn(TOKEN, output)
        self.assertIn("preview.mp4: Invalid data", output)

    def test_thumbnail_failure_logs_the_file_name(self) -> None:
        # The exception text of a failed command repeats the whole command line.
        failed = subprocess.CalledProcessError(1, ["ffmpeg", "-i", str(UPLOAD_PATH)])
        with mock.patch.object(
            video_helpers.subprocess, "run", side_effect=failed
        ), self.assertLogs(video_helpers.logger, level="WARNING") as logs:
            self.assertIsNone(video_helpers.make_video_thumbnail(UPLOAD_PATH))

        output = "\n".join(logs.output)
        self.assertNotIn(TOKEN, output)
        self.assertIn("preview.mp4", output)


if __name__ == "__main__":
    unittest.main()
