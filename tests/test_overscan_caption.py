"""Overscan makes the preview wider than the delivered frame - say so.

The caption repeats the ROP's own figure rather than the true per-side margin.
Redshift's "Pixels" value is the total added to each axis, split over both
sides: 100 on a 2160x1440 camera renders 2260x1540, i.e. 50 px all round. The
caption still says 100, because that is the number in the ROP - 50 appears
nowhere in Houdini and would make a mismatch impossible to trace. Parameter
names verified against the live ROP (RS_overscanMode / RS_overscanData).
"""

import importlib.util
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.preview_text import build_preview_caption


def _worker():
    spec = importlib.util.spec_from_file_location(
        "preview_worker",
        Path(__file__).resolve().parents[1] / "scripts" / "deadline_preview_worker.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["preview_worker"] = module
    try:
        spec.loader.exec_module(module)
    except SystemExit:  # pragma: no cover - the script guards on __main__
        pass
    return module


WORKER = _worker()


def sidecar(mode=1, x=100.0, y=100.0) -> dict:
    label = {0: "Disabled", 1: "Pixels", 2: "Percentage"}[mode]
    return {"overscan": {"mode": mode, "mode_label": label, "x": x, "y": y}}


class DescribeOverscanTests(unittest.TestCase):
    def test_reports_the_rop_figure_not_the_per_side_margin(self) -> None:
        """The ROP says 100; the margin is 50 per side. Show 100."""
        self.assertEqual(WORKER._describe_overscan(sidecar()), "Overscan 100 px")

    def test_percentage_mode_keeps_its_unit(self) -> None:
        self.assertEqual(
            WORKER._describe_overscan(sidecar(mode=2, x=5, y=5)), "Overscan 5 %"
        )

    def test_asymmetric_values(self) -> None:
        self.assertEqual(
            WORKER._describe_overscan(sidecar(x=100, y=40)), "Overscan 100x40 px"
        )

    def test_drops_a_trailing_zero(self) -> None:
        self.assertEqual(WORKER._describe_overscan(sidecar(x=100.0, y=100.0)), "Overscan 100 px")

    def test_silent_when_disabled(self) -> None:
        self.assertIsNone(WORKER._describe_overscan(sidecar(mode=0)))
        self.assertIsNone(WORKER._describe_overscan({"overscan": None}))

    def test_silent_when_the_mode_is_on_but_nothing_is_set(self) -> None:
        self.assertIsNone(WORKER._describe_overscan(sidecar(x=0, y=0)))

    def test_silent_for_older_sidecars(self) -> None:
        """Renders submitted before the plugin recorded overscan."""
        self.assertIsNone(WORKER._describe_overscan({"version": 2}))
        self.assertIsNone(WORKER._describe_overscan(None))


class CaptionTests(unittest.TestCase):
    def _plain(self, **kwargs) -> str:
        return re.sub(r"</?code>", "", build_preview_caption("shot_v01.mp4", "/x/y", **kwargs))

    def test_overscan_line_sits_under_the_resolution(self) -> None:
        lines = self._plain(
            resolution="2260x1540", overscan="Overscan 100 px"
        ).split("\n")
        self.assertEqual(lines[2], "📐 2260 × 1540")
        self.assertEqual(lines[3], "🖼 Overscan 100 px")

    def test_px_is_not_mangled_by_dimension_formatting(self) -> None:
        """A blanket x -> × replacement turned "100 px" into "100 p × "."""
        caption = self._plain(overscan="Overscan 100 px")
        self.assertIn("100 px", caption)
        self.assertNotIn("p ×", caption)

    def test_asymmetric_values_still_get_the_multiplication_sign(self) -> None:
        self.assertIn("100 × 40 px", self._plain(overscan="Overscan 100x40 px"))

    def test_absent_when_there_is_no_overscan(self) -> None:
        self.assertNotIn("🖼", self._plain(resolution="1920x1080"))


if __name__ == "__main__":
    unittest.main()
