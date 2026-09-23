"""An upload token must never become a refusal nobody can clear.

A token's record can say a file has already arrived while that file is no
longer on disk. In that state the record can never move again, so every worker
that picks the task up collects the same 403, "Invalid, expired, or in-progress
token". Worse, each one concludes the machine is at fault and strikes itself
off the job's machine list, until no machine on the farm is allowed to run the
preview.

The record is not the video. When the file behind it is gone, the copy the
worker is holding is the only one left, and it is taken.
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

from app.core import maintenance, preview_upload
from app.core.config import settings
from app.core.preview_upload import (
    STATUS_DELIVERING,
    STATUS_FAILED,
    STATUS_RECEIVED,
    PreviewUploadPayload,
)


class TokenClaimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = settings.sqlite_db_path
        self._old_temp = settings.temp_dir
        settings.sqlite_db_path = str(Path(self._tmp.name) / "tokens.db")
        settings.temp_dir = str(Path(self._tmp.name) / "temp")
        Path(settings.temp_dir).mkdir(parents=True, exist_ok=True)
        # A fresh store per test: the module-level one remembers that it has
        # already created its schema, which is not true of this new database.
        self.store = preview_upload.PreviewUploadTokenStore(ttl_seconds=3600)

    async def asyncTearDown(self) -> None:
        settings.sqlite_db_path = self._old_db
        settings.temp_dir = self._old_temp
        self._tmp.cleanup()

    def _payload(self) -> PreviewUploadPayload:
        return PreviewUploadPayload(
            telegram_user_id=7,
            job_name="Shot",
            expected_dropbox_path=None,
            expected_filename="preview.mp4",
            expected_local_path=None,
        )

    async def _issue(self, _label: str = "") -> str:
        return await self.store.issue(self._payload())

    async def _receive(self, token: str, temp_path: Path) -> None:
        """Walk the token through a real upload, as the endpoint would."""
        await self.store.claim(token)
        await self.store.mark_received(token, temp_path, temp_path.stat().st_size)

    async def test_a_fresh_token_is_claimed(self) -> None:
        token = await self._issue()
        self.assertIsNotNone(await self.store.claim(token))

    async def test_an_upload_that_already_arrived_is_refused_as_a_duplicate(self) -> None:
        token = await self._issue()
        landed = Path(settings.temp_dir) / "landed.mp4"
        landed.write_bytes(b"video")
        await self._receive(token, landed)

        self.assertEqual((await self.store.get_state(token)).status, STATUS_RECEIVED)
        self.assertIsNone(await self.store.claim(token))

    async def test_a_record_whose_file_is_gone_takes_the_upload_again(self) -> None:
        """The dead end: without this the token refuses everyone for ever."""
        token = await self._issue()
        landed = Path(settings.temp_dir) / "landed.mp4"
        landed.write_bytes(b"video")
        await self._receive(token, landed)
        landed.unlink()

        self.assertIsNotNone(await self.store.claim(token))

    async def test_it_is_taken_again_however_far_delivery_had_got(self) -> None:
        for step in ("delivering", "failed"):
            with self.subTest(step=step):
                token = await self._issue()
                landed = Path(settings.temp_dir) / f"landed-{step}.mp4"
                landed.write_bytes(b"video")
                await self._receive(token, landed)
                await self.store.mark_delivery_started(token)
                if step == "failed":
                    await self.store.mark_delivery_failed(token, "telegram said no")
                landed.unlink()

                self.assertIsNotNone(await self.store.claim(token))

    async def test_taking_it_again_starts_delivery_from_scratch(self) -> None:
        """A record that had used up its delivery attempts still gets to run."""
        token = await self._issue()
        landed = Path(settings.temp_dir) / "landed.mp4"
        landed.write_bytes(b"video")
        await self._receive(token, landed)
        for _ in range(settings.preview_upload_delivery_max_attempts):
            await self.store.mark_delivery_started(token)
            await self.store.mark_delivery_failed(token, "telegram said no")
        self.assertTrue((await self.store.get_state(token)).attempts_exhausted)
        landed.unlink()

        self.assertIsNotNone(await self.store.claim(token))
        self.assertEqual((await self.store.get_state(token)).delivery_attempts, 0)

    async def test_an_upload_in_flight_is_still_left_alone(self) -> None:
        token = await self._issue()
        self.assertIsNotNone(await self.store.claim(token))
        self.assertIsNone(await self.store.claim(token))  # lease still held

    async def test_an_unknown_token_is_refused(self) -> None:
        self.assertIsNone(await self.store.claim("never-issued"))


class TempSweepTests(unittest.TestCase):
    """The periodic sweep must not age out an upload a token still owns."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_temp = settings.temp_dir
        self._old_conv = settings.conv_dir
        self._old_enabled = settings.preview_upload_enabled
        settings.temp_dir = str(Path(self._tmp.name) / "temp")
        settings.conv_dir = str(Path(self._tmp.name) / "conv")
        settings.preview_upload_enabled = True
        Path(settings.temp_dir).mkdir(parents=True)
        Path(settings.conv_dir).mkdir(parents=True)

        def restore() -> None:
            settings.temp_dir = self._old_temp
            settings.conv_dir = self._old_conv
            settings.preview_upload_enabled = self._old_enabled

        self.addCleanup(restore)

    def _age(self, path: Path, hours: int) -> None:
        old = time.time() - hours * 3600
        os.utime(path, (old, old))

    def test_a_waiting_upload_survives_the_sweep(self) -> None:
        upload = Path(settings.temp_dir) / "upload_tok"
        upload.mkdir()
        (upload / "preview.mp4").write_bytes(b"video")
        self._age(upload, 48)

        maintenance.cleanup_old_files(24)

        self.assertTrue((upload / "preview.mp4").exists())

    def test_everything_else_old_is_still_swept(self) -> None:
        stale = Path(settings.temp_dir) / "leftover.mp4"
        stale.write_bytes(b"junk")
        self._age(stale, 48)

        maintenance.cleanup_old_files(24)

        self.assertFalse(stale.exists())

    def test_with_uploads_disabled_nothing_is_spared(self) -> None:
        settings.preview_upload_enabled = False
        upload = Path(settings.temp_dir) / "upload_tok"
        upload.mkdir()
        self._age(upload, 48)

        maintenance.cleanup_old_files(24)

        self.assertFalse(upload.exists())


if __name__ == "__main__":
    unittest.main()
