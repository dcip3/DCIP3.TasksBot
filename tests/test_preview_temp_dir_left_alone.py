"""The bot's cleanup never touches PREVIEW_TEMP_DIR.

That setting names a folder on the render workers, which the preview script
cleans up itself. On the bot host the same path can be anything, the user's own
temp folder included, so emptying it there would delete files that belong to
someone else.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core import maintenance
from app.core.config import settings


class PreviewTempDirLeftAloneTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        saved = (settings.temp_dir, settings.conv_dir, settings.preview_temp_dir)

        def restore() -> None:
            settings.temp_dir, settings.conv_dir, settings.preview_temp_dir = saved

        self.addCleanup(restore)
        settings.temp_dir = str(root / "temp")
        settings.conv_dir = str(root / "conv")
        settings.preview_temp_dir = str(root / "worker_temp")

        worker_temp = root / "worker_temp"
        (worker_temp / "preview_cpu_1").mkdir(parents=True)
        self.someone_elses = worker_temp / "notes.txt"
        self.someone_elses.write_text("keep me", encoding="utf-8")
        old = time.time() - 48 * 3600
        os.utime(self.someone_elses, (old, old))

    def test_startup_and_shutdown_sweep(self) -> None:
        maintenance.cleanup_temp_and_conv()
        self.assertTrue(self.someone_elses.exists())
        self.assertTrue((self.someone_elses.parent / "preview_cpu_1").is_dir())

    def test_periodic_sweep(self) -> None:
        maintenance.cleanup_old_files(24)
        self.assertTrue(self.someone_elses.exists())


if __name__ == "__main__":
    unittest.main()
