#!/usr/bin/env python3
"""
Helper script executed on Deadline workers to build preview videos with proper OCIO color management.

Steps:
1. Optionally bake a temporary LUT using the supplied OCIO config / display / view.
2. Invoke ffmpeg to convert the EXR sequence to an MP4 using the baked LUT.

The script expects that PyOpenColorIO, OpenEXR and NumPy (indirectly via PyOpenColorIO) are installed
in the Python environment available on the worker.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


def configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - preview_worker - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )


def bake_preview_lut(
    *,
    config_path: Path,
    input_space: str,
    display: str,
    view: str,
    lut_size: int,
    destination: Path,
) -> None:
    try:
        import PyOpenColorIO as ocio
    except ImportError as exc:
        raise RuntimeError("PyOpenColorIO is required on Deadline workers to bake preview LUTs") from exc

    logging.info("Baking preview LUT using config %s", config_path)
    config = ocio.Config.CreateFromFile(str(config_path))
    baker = ocio.Baker()
    baker.setConfig(config)
    baker.setFormat("iridas_cube")
    baker.setCubeSize(max(2, int(lut_size)))
    baker.setInputSpace(input_space)
    try:
        baker.setDisplayView(display, view)
    except Exception as display_error:  # pragma: no cover - defensive fallback for misconfigured displays
        logging.warning(
            "Failed to apply display/view (%s). Falling back to target space sRGB.",
            display_error,
        )
        baker.setTargetSpace("sRGB")

    destination.parent.mkdir(parents=True, exist_ok=True)
    baked = baker.bake()
    destination.write_text(baked)
    logging.info("Preview LUT written to %s", destination)


def build_ffmpeg_command(
    *,
    ffmpeg_path: str,
    start_number: int,
    frame_rate: float,
    input_pattern: str,
    output_path: str,
    lut_path: Optional[Path],
    preset: str,
    crf: int,
) -> list[str]:
    command: list[str] = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-start_number",
        str(start_number),
        "-framerate",
        f"{frame_rate:g}",
        "-i",
        input_pattern,
    ]

    if lut_path is not None:
        lut_posix = lut_path.as_posix().replace(":", r"\:")
        lut_arg = f"lut3d=file='{lut_posix}'"
        command.extend(["-vf", lut_arg])

    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            output_path,
        ]
    )
    return command



def run_ffmpeg(command: list[str]) -> None:
    logging.info("Running ffmpeg: %s", " ".join(command))
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with status {completed.returncode}")


def resolve_config_path(args: argparse.Namespace) -> Optional[Path]:
    if args.disable_color:
        return None
    if args.ocio_config:
        return Path(args.ocio_config)
    env_config = os.environ.get("OCIO")
    if env_config:
        return Path(env_config)
    raise RuntimeError(
        "Color transform requested but no OCIO config supplied. "
        "Pass --ocio-config or set OCIO environment variable."
    )


def parse_arguments(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deadline preview conversion helper.")
    parser.add_argument("--input-pattern", required=True, help="EXR sequence pattern, e.g. path/to/shot.%04d.exr")
    parser.add_argument("--output-path", required=True, help="Destination MP4 path")
    parser.add_argument("--start-number", type=int, default=0, help="First frame number in the sequence")
    parser.add_argument("--frame-rate", type=float, default=25.0, help="Playback frame rate")
    parser.add_argument("--ffmpeg-path", default="ffmpeg", help="ffmpeg executable available on the worker")
    parser.add_argument("--preset", default="medium", help="ffmpeg libx264 preset")
    parser.add_argument("--crf", type=int, default=20, help="ffmpeg CRF value")
    parser.add_argument(
        "--temp-dir",
        default=None,
        help="Optional directory for temporary files (uses system temp if omitted)",
    )

    color_group = parser.add_argument_group("color management")
    color_group.add_argument("--disable-color", action="store_true", help="Disable OCIO color transform")
    color_group.add_argument("--ocio-config", help="Path to OCIO config file")
    color_group.add_argument("--input-space", default="ACEScg", help="OCIO input space")
    color_group.add_argument("--display", default="sRGB", help="OCIO display")
    color_group.add_argument("--view", default="ACES 1.0 SDR-video", help="OCIO view")
    color_group.add_argument("--lut-size", type=int, default=65, help="Preview LUT cube size")
    color_group.add_argument(
        "--keep-lut",
        action="store_true",
        help="Leave the baked LUT on disk for debugging",
    )
    color_group.add_argument(
        "--lut-path",
        help="Explicit LUT output path. Uses temp file if omitted.",
    )

    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase logging verbosity")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_arguments(argv)
    configure_logging(args.verbose)

    try:
        config_path = resolve_config_path(args)

        lut_path: Optional[Path] = None
        temp_dir_cm: Optional[tempfile.TemporaryDirectory[str]] = None
        if not args.disable_color:
            if args.lut_path:
                lut_path = Path(args.lut_path)
            else:
                base_temp_dir = args.temp_dir
                if base_temp_dir:
                    Path(base_temp_dir).mkdir(parents=True, exist_ok=True)
                temp_dir_cm = tempfile.TemporaryDirectory(prefix="preview_lut_", dir=base_temp_dir)
                lut_path = Path(temp_dir_cm.name) / "preview_lut.cube"
            bake_preview_lut(
                config_path=config_path,  # type: ignore[arg-type]
                input_space=args.input_space,
                display=args.display,
                view=args.view,
                lut_size=args.lut_size,
                destination=lut_path,  # type: ignore[arg-type]
            )

        command = build_ffmpeg_command(
            ffmpeg_path=args.ffmpeg_path,
            start_number=args.start_number,
            frame_rate=args.frame_rate,
            input_pattern=args.input_pattern,
            output_path=args.output_path,
            lut_path=lut_path,
            preset=args.preset,
            crf=args.crf,
        )
        run_ffmpeg(command)
        logging.info("Preview video successfully written to %s", args.output_path)

        if temp_dir_cm is not None and args.keep_lut:
            logging.info("LUT kept at %s", lut_path)
    except Exception as exc:  # pragma: no cover - Deadline handles logging
        logging.error("Preview conversion failed: %s", exc, exc_info=True)
        return 1
    finally:
        if "temp_dir_cm" in locals() and isinstance(temp_dir_cm, tempfile.TemporaryDirectory):
            if args.keep_lut:
                temp_dir_cm.cleanup = lambda: None  # type: ignore[assignment]
            else:
                temp_dir_cm.cleanup()

    return 0


if __name__ == "__main__":
    sys.exit(main())
