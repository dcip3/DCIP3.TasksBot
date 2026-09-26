"""Read DWA-compressed EXRs with an ffmpeg that can, installing one if need be.

YACE_0250's preview came back captioned "0 x 0": the worker's ffprobe was
older than FFmpeg 4.4, the first release that decodes DWAA/DWAB, so it read
the header and answered with no size. The same ffmpeg fails on every frame
whenever a preview has it read the EXRs itself. worker_setup.ps1 installed
Python and its packages but only told whoever ran it to install ffmpeg.
"""

import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts import deadline_preview_worker as worker

GYAN_7 = (
    "ffmpeg version 7.1.1-full_build-www.gyan.dev Copyright (c) 2000-2025\n"
    "libavutil      59. 39.100 / 59. 39.100\n"
    "libavcodec     61. 19.101 / 61. 19.101\n"
)
FFMPEG_4_2 = (
    "ffmpeg version 4.2.2 Copyright (c) 2000-2019 the FFmpeg developers\n"
    "libavcodec     58. 54.100 / 58. 54.100\n"
)
GIT_BUILD = (
    "ffmpeg version 2021-05-02-git-a3a8e7d2e1-full_build-www.gyan.dev\n"
    "libavcodec     59.  1.100 / 59.  1.100\n"
)


def quiet(test: unittest.TestCase) -> None:
    logging.disable(logging.CRITICAL)
    test.addCleanup(logging.disable, logging.NOTSET)


class LibavcodecVersionTests(unittest.TestCase):
    def _version(self, stdout: str = "", returncode: int = 0):
        answer = mock.Mock(returncode=returncode, stdout=stdout)
        with mock.patch.object(worker.subprocess, "run", return_value=answer):
            return worker._libavcodec_version("ffmpeg")

    def test_a_release_build(self) -> None:
        self.assertEqual(self._version(GYAN_7), (61, 19))

    def test_a_build_from_git_has_no_release_number_to_read(self) -> None:
        self.assertEqual(self._version(GIT_BUILD), (59, 1))

    def test_the_line_4_4_is_drawn_at(self) -> None:
        self.assertFalse(self._capable(FFMPEG_4_2))
        self.assertFalse(self._capable("libavcodec     58.133.100 / 58.133.100\n"))
        self.assertTrue(self._capable("libavcodec     58.134.100 / 58.134.100\n"))
        self.assertTrue(self._capable(GYAN_7))

    def _capable(self, stdout: str) -> bool:
        answer = mock.Mock(returncode=0, stdout=stdout)
        with mock.patch.object(worker.subprocess, "run", return_value=answer):
            return worker._decodes_dwa_exr("ffmpeg")

    def test_an_ffmpeg_that_does_not_run_has_no_version(self) -> None:
        self.assertIsNone(self._version(returncode=1))
        with mock.patch.object(worker.subprocess, "run", side_effect=OSError("gone")):
            self.assertIsNone(worker._libavcodec_version("ffmpeg"))


class ChoosingAnFfmpegTests(unittest.TestCase):
    OLD = os.path.join("C:", "ProgramData", "ffmpeg", "bin", "ffmpeg.exe")
    NEW = os.path.join("C:", "WinGet", "Links", "ffmpeg.exe")

    def setUp(self) -> None:
        quiet(self)

    def _choose(self, found, versions, installs=False, after_install=None):
        """found: ffmpegs on the machine; versions: their libavcodec."""
        scans = [found, after_install if after_install is not None else found]
        with mock.patch.object(
            worker, "_ffmpeg_candidates", side_effect=scans
        ), mock.patch.object(
            worker, "_libavcodec_version", side_effect=lambda path: versions.get(path)
        ), mock.patch.object(
            worker.shutil, "which", return_value=found[0] if found else None
        ), mock.patch.object(
            worker, "_install_ffmpeg", return_value=installs
        ) as install:
            return worker._capable_ffmpeg("ffmpeg"), install

    def test_a_current_ffmpeg_is_kept(self) -> None:
        chosen, install = self._choose([self.NEW], {self.NEW: (61, 19)})
        self.assertEqual(chosen, self.NEW)
        install.assert_not_called()

    def test_a_newer_one_already_on_the_machine_wins(self) -> None:
        chosen, install = self._choose(
            [self.OLD, self.NEW], {self.OLD: (58, 54), self.NEW: (61, 19)}
        )
        self.assertEqual(chosen, self.NEW)
        install.assert_not_called()

    def test_with_none_on_the_machine_one_is_installed(self) -> None:
        chosen, install = self._choose(
            [self.OLD],
            {self.OLD: (58, 54), self.NEW: (62, 3)},
            installs=True,
            after_install=[self.OLD, self.NEW],
        )
        self.assertEqual(chosen, self.NEW)
        install.assert_called_once()

    def test_when_nothing_can_be_installed_the_configured_one_stays(self) -> None:
        """The color path only encodes PNGs with it, which any version does."""
        chosen, install = self._choose([self.OLD], {self.OLD: (58, 54)}, installs=False)
        self.assertEqual(chosen, "ffmpeg")
        install.assert_called_once()

    def test_no_ffmpeg_at_all_is_installed_too(self) -> None:
        chosen, _ = self._choose(
            [], {self.NEW: (62, 3)}, installs=True, after_install=[self.NEW]
        )
        self.assertEqual(chosen, self.NEW)


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        quiet(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stamp = Path(self._tmp.name) / "tasksbot_ffmpeg_install.stamp"
        patcher = mock.patch.object(worker.tempfile, "gettempdir", return_value=self._tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _install(self, returncode: int = 0, winget: str | None = "winget"):
        answer = mock.Mock(returncode=returncode, stdout="", stderr="")
        with mock.patch.object(worker, "_find_winget", return_value=winget), mock.patch.object(
            worker.subprocess, "run", return_value=answer
        ) as run:
            return worker._install_ffmpeg(), run

    def test_winget_installs_it_unattended(self) -> None:
        installed, run = self._install()
        self.assertTrue(installed)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["winget", "install"])
        self.assertIn(worker._FFMPEG_WINGET_ID, command)
        self.assertIn("--silent", command)
        self.assertIs(run.call_args.kwargs["stdin"], worker.subprocess.DEVNULL)

    def test_a_failed_install_says_so(self) -> None:
        installed, _ = self._install(returncode=1)
        self.assertFalse(installed)

    def test_a_recent_attempt_is_not_repeated(self) -> None:
        """A winget that fails must not add minutes to every preview."""
        self._install(returncode=1)
        installed, run = self._install()
        self.assertFalse(installed)
        run.assert_not_called()

    def test_it_is_tried_again_later(self) -> None:
        self._install(returncode=1)
        old = time.time() - worker._FFMPEG_INSTALL_RETRY_SECONDS - 60
        os.utime(self.stamp, (old, old))
        installed, run = self._install()
        self.assertTrue(installed)
        run.assert_called_once()

    def test_without_winget_nothing_is_tried(self) -> None:
        installed, run = self._install(winget=None)
        self.assertFalse(installed)
        run.assert_not_called()
        self.assertFalse(self.stamp.exists())


class CandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

    def _ffmpeg(self, folder: str) -> Path:
        path = self.root / folder / self.exe
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")
        path.chmod(0o755)
        return path

    def test_every_ffmpeg_on_the_path_is_offered_once_configured_first(self) -> None:
        first, second = self._ffmpeg("old"), self._ffmpeg("new")
        path_value = os.pathsep.join([str(first.parent), str(second.parent), str(first.parent)])
        with mock.patch.dict(os.environ, {"PATH": path_value}):
            candidates = worker._ffmpeg_candidates(str(second))
        self.assertEqual(candidates[:2], [str(second), str(first)])
        self.assertEqual(len(candidates), len(set(os.path.normcase(c) for c in candidates)))


class FfprobePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bin = Path(self._tmp.name)

    def test_a_windows_ffmpeg_gets_the_ffprobe_beside_it(self) -> None:
        """It used to fall back to whatever ffprobe PATH had, often an older one."""
        (self.bin / "ffmpeg.exe").write_bytes(b"")
        (self.bin / "ffprobe.exe").write_bytes(b"")
        self.assertEqual(
            worker._resolve_ffprobe_path(str(self.bin / "ffmpeg.exe")),
            str(self.bin / "ffprobe.exe"),
        )

    def test_with_no_ffprobe_beside_it_path_answers(self) -> None:
        (self.bin / "ffmpeg.exe").write_bytes(b"")
        self.assertEqual(worker._resolve_ffprobe_path(str(self.bin / "ffmpeg.exe")), "ffprobe")

    def test_a_bare_name_still_means_path(self) -> None:
        self.assertEqual(worker._resolve_ffprobe_path("ffmpeg"), "ffprobe")

    def test_a_configured_path_on_another_machine_is_taken_at_its_word(self) -> None:
        missing = Path("/opt/ffmpeg/bin/ffmpeg")
        self.assertEqual(worker._resolve_ffprobe_path(str(missing)), str(missing.with_name("ffprobe")))


if __name__ == "__main__":
    unittest.main()
