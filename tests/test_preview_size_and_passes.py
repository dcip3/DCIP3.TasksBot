"""Name every pass, and send a frame size players can actually decode.

Both from one preview: its caption read "diffuse, specular, reflection,
refraction, beauty_aux, ao, V, P +3 more" - the three that mattered could well
have been the missing ones - and the video itself, 3556x2404, downloaded fine
but would not play in the chat. Nothing in the preview path had ever scaled a
render down; previews had simply been small enough until now.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core import preview_text
from scripts import deadline_preview_worker as worker


def sidecar(*names: str) -> dict:
    return {"aovs": {"list": [{"name": name, "enabled": True} for name in names]}}


class PassListTests(unittest.TestCase):
    def test_every_pass_is_named(self) -> None:
        """The nine from the caption that stopped at eight."""
        names = [
            "diffuse", "specular", "reflection", "refraction", "beauty_aux",
            "ao", "V", "P", "N", "Z", "cryptomatte",
        ]
        self.assertEqual(worker._describe_passes(sidecar(*names)), ", ".join(names))

    def test_disabled_passes_are_still_left_out(self) -> None:
        data = sidecar("diffuse", "specular")
        data["aovs"]["list"][1]["enabled"] = False
        self.assertEqual(worker._describe_passes(data), "diffuse")

    def test_a_render_without_extra_passes_says_nothing(self) -> None:
        self.assertIsNone(worker._describe_passes({"aovs": {"list": []}}))
        self.assertIsNone(worker._describe_passes({"aovs": {"all_disabled": True}}))


class CaptionLengthTests(unittest.TestCase):
    def _caption(self, passes: str) -> str:
        return preview_text.build_preview_caption(
            "shb_mount_0025_lighting_acescg_v003",
            "/projects/proj_a/render/SHB_mount/shb_mount_0025",
            resolution="3556x2404",
            passes=passes,
        )

    def test_a_long_list_is_kept_whole(self) -> None:
        passes = ", ".join(f"aov_{index}" for index in range(20))
        caption = self._caption(passes)
        self.assertIn(passes, caption)
        self.assertNotIn("more", caption)

    def test_only_a_caption_telegram_would_reject_gives_names_up(self) -> None:
        passes = ", ".join(f"an_extremely_long_pass_name_{index}" for index in range(60))
        caption = self._caption(passes)
        self.assertLessEqual(len(caption), preview_text._CAPTION_LIMIT)
        self.assertIn("more", caption)
        self.assertIn("an_extremely_long_pass_name_0", caption)


class ScaleFilterTests(unittest.TestCase):
    def test_a_frame_larger_than_the_box_is_fitted_into_it(self) -> None:
        self.assertIn("min(iw,1920)", worker._scale_filter(1920))
        self.assertIn("force_original_aspect_ratio=decrease", worker._scale_filter(1920))

    def test_dimensions_are_rounded_to_even_numbers(self) -> None:
        """yuv420p cannot store an odd width or height."""
        self.assertIn("trunc(iw/2)*2:trunc(ih/2)*2", worker._scale_filter(1920))

    def test_a_zero_box_leaves_the_render_size_alone(self) -> None:
        self.assertIsNone(worker._scale_filter(0))
        self.assertIsNone(worker._scale_filter(-1))


class EncodeCommandTests(unittest.TestCase):
    def _command(self, **kwargs) -> list:
        command, _ = worker.build_ffmpeg_command(
            ffmpeg_path="ffmpeg",
            start_number=1,
            frame_rate=25.0,
            input_pattern="shot.%04d.exr",
            output_path="preview.mp4",
            video_encoder="libx264",
            preset="fast",
            crf=24,
            **kwargs,
        )
        return command

    def test_the_encode_carries_the_scale_filter(self) -> None:
        command = self._command()
        self.assertIn("-vf", command)
        self.assertIn("min(iw,1920)", command[command.index("-vf") + 1])

    def test_the_cap_can_be_lifted(self) -> None:
        self.assertNotIn("-vf", self._command(max_dimension=0))

    def test_the_cap_is_whatever_it_is_told(self) -> None:
        command = self._command(max_dimension=2560)
        self.assertIn("min(iw,2560)", command[command.index("-vf") + 1])

    def test_the_filter_comes_before_the_output(self) -> None:
        command = self._command()
        self.assertLess(command.index("-vf"), command.index("preview.mp4"))


class ReuseTests(unittest.TestCase):
    """A preview built before the cap must not be handed out again.

    Finished previews sit in the render folder, and a worker that finds one
    newer than every frame simply uploads it. Without this, every shot whose
    preview already exists keeps delivering the video that would not play.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.frame = root / "shot.0001.exr"
        self.frame.write_bytes(b"frame")
        self.output = root / "shot.mp4"
        self.output.write_bytes(b"x" * 4096)
        os.utime(self.output, (time.time() + 60, time.time() + 60))

    def _reusable(self, resolution: str | None, **kwargs) -> bool:
        with mock.patch.object(
            worker, "_probe_output_resolution", return_value=resolution
        ), mock.patch.object(
            worker, "_validate_preview_output", return_value=(True, "looks fine")
        ):
            return worker._reusable_existing_output(
                self.output, [self.frame], "ffmpeg", 1, **kwargs
            )

    def test_a_preview_within_the_box_is_reused(self) -> None:
        self.assertTrue(self._reusable("1920x1298"))

    def test_the_oversized_one_from_the_farm_is_rebuilt(self) -> None:
        self.assertFalse(self._reusable("3556x2404"))

    def test_a_square_render_is_measured_on_both_sides(self) -> None:
        self.assertFalse(self._reusable("2560x2560"))

    def test_with_the_cap_lifted_anything_is_reused(self) -> None:
        self.assertTrue(self._reusable("3556x2404", max_dimension=0))

    def test_an_unreadable_resolution_does_not_block_reuse(self) -> None:
        self.assertTrue(self._reusable(None))


class ReportedResolutionTests(unittest.TestCase):
    """The caption reports the render, not the video that was encoded from it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.frame = root / "shot.0001.exr"
        self.frame.write_bytes(b"frame")
        self.output = root / "shot.mp4"
        self.output.write_bytes(b"video")
        self.pattern = str(root / "shot.%04d.exr")

    def _headers(self, sizes: dict, **kwargs) -> dict:
        """sizes maps a file suffix to what ffprobe would report for it."""

        def probe(path, _ffmpeg):
            return sizes.get(Path(path).suffix)

        with mock.patch.object(worker, "_probe_output_resolution", side_effect=probe):
            return worker._upload_metadata_headers(
                None, self.output, "ffmpeg", input_pattern=self.pattern, **kwargs
            )

    def test_the_render_size_is_reported_not_the_preview_size(self) -> None:
        """The regression: a capped preview started reporting its own 1920x1298."""
        headers = self._headers({".exr": "3556x2404", ".mp4": "1920x1298"})
        self.assertEqual(headers["X-Preview-Resolution"], "3556x2404")

    def test_without_a_frame_to_read_the_encoded_file_still_answers(self) -> None:
        headers = self._headers({".mp4": "1920x1298"})
        self.assertEqual(headers["X-Preview-Resolution"], "1920x1298")

    def test_overscan_still_reports_the_delivered_frame(self) -> None:
        """Overscan makes the rendered image larger than what is delivered."""
        sidecar = {
            "resolution": {"camera": [2560, 1440]},
            "overscan": {"mode": 1, "mode_label": "Pixels", "x": 100, "y": 100},
        }
        headers = self._headers(
            {".exr": "2760x1640", ".mp4": "1920x1141"}, sidecar=sidecar
        )
        self.assertEqual(headers["X-Preview-Resolution"], "2560x1440")


if __name__ == "__main__":
    unittest.main()
