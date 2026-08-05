"""Telegram must be told the shape of a preview video.

Without explicit width/height and a matching thumbnail, clients pick the player
geometry themselves - phones tend to pick square, which squashed 3:2 renders
while desktop looked fine.
"""

import os
import shutil
import subprocess
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

from app.integrations import video_helpers
from app.services.preview import delivery

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _make_video(directory: Path, name: str, size: str, extra: list[str] | None = None) -> Path:
    path = directory / name
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24:duration=2",
            *(extra or []),
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe not available")
class ProbeAndThumbnailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _thumb_size(self, thumb: Path) -> tuple[int, int]:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
                str(thumb),
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        width, height = (int(part) for part in out.split("x"))
        return width, height

    def test_reads_real_dimensions_and_duration(self) -> None:
        video = _make_video(self.dir, "landscape.mp4", "2560x1706")
        meta = video_helpers.probe_video_metadata(video)
        self.assertEqual((meta.width, meta.height), (2560, 1706))
        self.assertEqual(meta.duration, 2)

    def test_non_square_pixels_report_display_size(self) -> None:
        """A stream stored anamorphic must not be described by its coded size."""
        video = _make_video(self.dir, "anamorphic.mp4", "960x1080", ["-vf", "setsar=2/1"])
        meta = video_helpers.probe_video_metadata(video)
        self.assertEqual((meta.width, meta.height), (1920, 1080))

    def _assert_same_shape(self, thumb: Path, expected: float) -> None:
        """Within 1%: scaling rounds to even dimensions, which shifts the ratio
        a fraction of a percent. Anything beyond that is a visible squash."""
        width, height = self._thumb_size(thumb)
        self.assertLess(
            abs((width / height) / expected - 1.0),
            0.01,
            f"thumbnail {width}x{height} does not match {expected:.4f}",
        )
        self.assertLessEqual(max(width, height), 320, "Telegram caps thumbnails at 320px")

    def test_thumbnail_matches_the_video_aspect(self) -> None:
        video = _make_video(self.dir, "thumb_land.mp4", "2560x1706")
        thumb = video_helpers.make_video_thumbnail(video)
        self.assertIsNotNone(thumb)
        self._assert_same_shape(thumb, 2560 / 1706)
        self.assertLess(thumb.stat().st_size, 200 * 1024)

    def test_portrait_thumbnail_is_not_squashed(self) -> None:
        video = _make_video(self.dir, "thumb_port.mp4", "1080x1920")
        thumb = video_helpers.make_video_thumbnail(video)
        self._assert_same_shape(thumb, 1080 / 1920)

    def test_square_video_stays_square(self) -> None:
        video = _make_video(self.dir, "thumb_square.mp4", "1024x1024")
        thumb = video_helpers.make_video_thumbnail(video)
        self._assert_same_shape(thumb, 1.0)

    def test_missing_file_is_survivable(self) -> None:
        self.assertIsNone(video_helpers.probe_video_metadata(self.dir / "nope.mp4"))
        self.assertIsNone(video_helpers.make_video_thumbnail(self.dir / "nope.mp4"))


class SendVideoArgumentTests(unittest.IsolatedAsyncioTestCase):
    async def test_shape_is_passed_to_telegram(self) -> None:
        meta = video_helpers.VideoMetadata(width=2560, height=1706, duration=12)
        thumb = Path("thumb.jpg")

        with mock.patch.object(
            delivery, "probe_video_metadata", return_value=meta
        ), mock.patch.object(
            delivery, "make_video_thumbnail", return_value=thumb
        ), mock.patch.object(
            delivery.bot, "send_video", new=mock.AsyncMock()
        ) as send_mock, mock.patch.object(Path, "unlink"):
            await delivery._send_video_with_shape(42, Path("preview.mp4"), "caption")

        kwargs = send_mock.await_args.kwargs
        self.assertEqual(kwargs["width"], 2560)
        self.assertEqual(kwargs["height"], 1706)
        self.assertEqual(kwargs["duration"], 12)
        self.assertTrue(kwargs["supports_streaming"])
        self.assertIsNotNone(kwargs.get("thumbnail"))

    async def test_still_sends_when_ffmpeg_is_unavailable(self) -> None:
        """A missing ffprobe must not cost the user their preview."""
        with mock.patch.object(
            delivery, "probe_video_metadata", return_value=None
        ), mock.patch.object(
            delivery, "make_video_thumbnail", return_value=None
        ), mock.patch.object(
            delivery.bot, "send_video", new=mock.AsyncMock()
        ) as send_mock:
            await delivery._send_video_with_shape(42, Path("preview.mp4"), "caption")

        send_mock.assert_awaited_once()
        kwargs = send_mock.await_args.kwargs
        self.assertNotIn("width", kwargs)
        self.assertNotIn("thumbnail", kwargs)

    async def test_thumbnail_is_cleaned_up_even_if_sending_fails(self) -> None:
        thumb = Path("thumb.jpg")
        with mock.patch.object(
            delivery, "probe_video_metadata", return_value=None
        ), mock.patch.object(
            delivery, "make_video_thumbnail", return_value=thumb
        ), mock.patch.object(
            delivery.bot, "send_video", new=mock.AsyncMock(side_effect=RuntimeError("nope"))
        ), mock.patch.object(Path, "unlink") as unlink_mock:
            with self.assertRaises(RuntimeError):
                await delivery._send_video_with_shape(42, Path("preview.mp4"), "caption")
        unlink_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
