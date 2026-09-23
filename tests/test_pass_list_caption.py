"""A render usually writes more than the beauty - the caption names the passes.

The plugin records the ROP's AOV list in the sidecar, and the names shown here
are the artist's own AOV names, because that is what ends up as the EXR layer
and what the AOV tab in Houdini shows. Verified against a production scene:
the ROP delegates its list via "Get AOVs Config. From" to
/obj/ropnet1/Redshift_AOVs1, whose five AOVs (beauty_aux, N, P, Z,
cryptomatte) have only beauty_aux and cryptomatte switched on - matching the
frames on disk, a beauty EXR plus a .cryptomatte.exr.
"""

import importlib.util
import os
import re
import sys
import unittest
import urllib.parse
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


def aov(name, type_name="CUSTOM", type_label="Custom", enabled=True) -> dict:
    return {
        "name": name,
        "type": type_name,
        "type_label": type_label,
        "enabled": enabled,
    }


def sidecar(*entries, **kwargs) -> dict:
    return {
        "version": 4,
        "aovs": {
            "all_disabled": kwargs.get("all_disabled", False),
            "source": "/obj/ropnet1/Redshift_AOVs1",
            "list": list(entries),
        },
    }


LIVE_SCENE = sidecar(
    aov("beauty_aux", "BEAUTY", "Beauty"),
    aov("N", "NORMALS", "Normals", enabled=False),
    aov("P", "World", "World Position", enabled=False),
    aov("Z", "Depth", "Z Depth", enabled=False),
    aov("cryptomatte", "CRYPTOMATTE", "Cryptomatte"),
)


class DescribePassesTests(unittest.TestCase):
    def test_the_scene_that_produced_this_feature(self) -> None:
        self.assertEqual(
            WORKER._describe_passes(LIVE_SCENE), "beauty_aux, cryptomatte"
        )

    def test_disabled_aovs_are_not_rendered_so_they_are_not_listed(self) -> None:
        self.assertEqual(
            WORKER._describe_passes(sidecar(aov("Z", enabled=False), aov("N"))), "N"
        )

    def test_an_unnamed_aov_falls_back_to_its_type(self) -> None:
        self.assertEqual(
            WORKER._describe_passes(sidecar(aov("", "CRYPTOMATTE", "Cryptomatte"))),
            "Cryptomatte",
        )

    def test_and_to_the_raw_type_when_the_menu_label_is_missing(self) -> None:
        """Older Redshift builds may not resolve a label for every AOV type."""
        self.assertEqual(
            WORKER._describe_passes(sidecar(aov("", "CRYPTOMATTE", ""))), "CRYPTOMATTE"
        )

    def test_the_same_name_twice_is_shown_once(self) -> None:
        self.assertEqual(WORKER._describe_passes(sidecar(aov("Z"), aov("Z"))), "Z")

    def test_a_long_list_is_named_in_full(self) -> None:
        """It used to stop at eight and count the rest, which hid the ones the
        artist was looking for."""
        entries = [aov("aov%d" % index) for index in range(1, 12)]
        self.assertEqual(
            WORKER._describe_passes(sidecar(*entries)),
            ", ".join("aov%d" % index for index in range(1, 12)),
        )

    def test_silent_when_the_rop_disables_every_aov(self) -> None:
        self.assertIsNone(
            WORKER._describe_passes(sidecar(aov("Z"), all_disabled=True))
        )

    def test_silent_when_nothing_is_switched_on(self) -> None:
        self.assertIsNone(WORKER._describe_passes(sidecar(aov("Z", enabled=False))))
        self.assertIsNone(WORKER._describe_passes(sidecar()))

    def test_silent_for_older_sidecars(self) -> None:
        """Renders submitted before the plugin recorded the AOV list."""
        self.assertIsNone(WORKER._describe_passes({"version": 3}))
        self.assertIsNone(WORKER._describe_passes({"aovs": None}))
        self.assertIsNone(WORKER._describe_passes(None))


class UploadHeaderTests(unittest.TestCase):
    def test_passes_travel_to_the_bot_url_encoded(self) -> None:
        headers = WORKER._upload_metadata_headers(
            None, Path("no-such-preview.mp4"), "ffmpeg", LIVE_SCENE
        )
        self.assertEqual(
            urllib.parse.unquote(headers["X-Preview-Passes"]),
            "beauty_aux, cryptomatte",
        )

    def test_no_header_when_the_render_has_no_extra_passes(self) -> None:
        headers = WORKER._upload_metadata_headers(
            None, Path("no-such-preview.mp4"), "ffmpeg", {"version": 3}
        )
        self.assertNotIn("X-Preview-Passes", headers)


class CaptionTests(unittest.TestCase):
    def _plain(self, **kwargs) -> str:
        return re.sub(
            r"</?code>", "", build_preview_caption("shot_v01.mp4", "/x/y", **kwargs)
        )

    def test_passes_sit_between_the_frame_facts_and_the_look(self) -> None:
        lines = self._plain(
            resolution="2160x1440",
            overscan="Overscan 100 px",
            passes="beauty_aux, cryptomatte",
            lut="show.cube",
        ).split("\n")
        self.assertEqual(lines[2], "📐 2160 × 1440")
        self.assertEqual(lines[3], "🖼 Overscan 100 px")
        self.assertEqual(lines[4], "🧩 beauty_aux, cryptomatte")
        self.assertEqual(lines[5], "🎨 show.cube")

    def test_an_aov_named_like_a_dimension_is_left_alone(self) -> None:
        """Only resolutions get the × treatment; AOV names are names."""
        self.assertIn("mask_2x2", self._plain(passes="mask_2x2"))

    def test_absent_when_the_render_had_no_extra_passes(self) -> None:
        self.assertNotIn("🧩", self._plain(resolution="1920x1080"))


if __name__ == "__main__":
    unittest.main()
