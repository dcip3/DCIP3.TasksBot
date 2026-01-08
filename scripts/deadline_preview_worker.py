#!/usr/bin/env python3
"""
Helper script executed on Deadline workers to build preview videos with proper OCIO color management.

Steps:
1. Optionally bake a temporary LUT using the supplied OCIO config / display / view.
2. Invoke ffmpeg to convert the image sequence to an MP4 using the baked LUT.

The script expects that PyOpenColorIO, OpenEXR and NumPy (indirectly via PyOpenColorIO) are installed
in the Python environment available on the worker.
"""

from __future__ import annotations

import argparse
import atexit
import gc
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import contextlib
from pathlib import Path
from typing import Optional, List, Tuple, Set, Union


ACTIVE_TEMP_PATHS: Set[Path] = set()


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


def _register_temp_path(path: Path) -> None:
    try:
        ACTIVE_TEMP_PATHS.add(path)
    except TypeError:
        # Path may not be hashable or valid; ignore to avoid blocking processing
        pass


def _unregister_temp_path(path: Optional[Path]) -> None:
    if path is None:
        return
    ACTIVE_TEMP_PATHS.discard(path)


def _cleanup_registered_temp_paths() -> None:
    for temp_path in list(ACTIVE_TEMP_PATHS):
        shutil.rmtree(temp_path, ignore_errors=True)
        if temp_path.exists():
            logging.warning("Temporary preview folder persists after cleanup: %s", temp_path)
        else:
            ACTIVE_TEMP_PATHS.discard(temp_path)


def _resolve_base_temp_dir(raw_dir: Optional[Union[str, Path]]) -> Path:
    if raw_dir is not None:
        raw_str = str(raw_dir).strip()
        if raw_str.lower() in {"", "auto", "default", "system", "local"}:
            raw_str = ""
    else:
        raw_str = ""

    if raw_str:
        expanded = os.path.expandvars(os.path.expanduser(raw_str))
        base_path = Path(expanded)
    else:
        base_path = Path(tempfile.gettempdir()) / "PreviewTemp"

    try:
        base_path.mkdir(parents=True, exist_ok=True)
    except Exception as mkdir_error:
        logging.warning("Could not create preview temp dir %s: %s", base_path, mkdir_error)
    return base_path


def _handle_termination(signum, frame) -> None:  # pragma: no cover - Deadline signal handling
    logging.warning("Received signal %s; cleaning up preview temp folders", signum)
    _cleanup_registered_temp_paths()
    try:
        signal.signal(signum, signal.SIG_DFL)
    except Exception:
        pass
    os.kill(os.getpid(), signum)


def _install_signal_handlers() -> None:
    possible_signals = [getattr(signal, name, None) for name in ("SIGTERM", "SIGINT", "SIGBREAK")]
    for sig in possible_signals:
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_termination)
        except (ValueError, OSError):  # pragma: no cover - platform specific behavior
            continue


atexit.register(_cleanup_registered_temp_paths)


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


def _load_cpu_processor(
    config_path: Path,
    input_space: str,
    display: str,
    view: str,
):
    try:
        import PyOpenColorIO as ocio
    except ImportError as exc:  # pragma: no cover - depends on worker environment
        raise RuntimeError("PyOpenColorIO is required for CPU color mode") from exc

    config = ocio.Config.CreateFromFile(str(config_path))
    transform = ocio.DisplayViewTransform()
    transform.setSrc(input_space)
    transform.setDisplay(display)
    transform.setView(view)
    transform.setDirection(ocio.TRANSFORM_DIR_FORWARD)
    processor = config.getProcessor(transform)
    cpu_processor = processor.getDefaultCPUProcessor()
    if cpu_processor is None:
        raise RuntimeError("Failed to create OCIO CPU processor")
    return cpu_processor


def _convert_exr_to_png_cpu(
    exr_path: Path,
    output_path: Path,
    cpu_processor,
    width_chunk: int = 256,
):
    try:
        import OpenEXR
        import Imath
        import numpy as np
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "CPU color mode requires OpenEXR, Imath, numpy, and Pillow"
        ) from exc

    FLOAT = Imath.PixelType(Imath.PixelType.FLOAT)
    exr = OpenEXR.InputFile(str(exr_path))
    try:
        header = exr.header()
        dw = header["dataWindow"]
        width = dw.max.x - dw.min.x + 1
        height = dw.max.y - dw.min.y + 1

        channels = exr.channels(["R", "G", "B"], FLOAT)

        processed_chunks = []
        for y_start in range(0, height, width_chunk):
            y_end = min(y_start + width_chunk, height)
            chunk_height = y_end - y_start

            r_chunk = np.frombuffer(
                channels[0][y_start * width * 4 : y_end * width * 4],
                dtype=np.float32,
            ).reshape((chunk_height, width))
            g_chunk = np.frombuffer(
                channels[1][y_start * width * 4 : y_end * width * 4],
                dtype=np.float32,
            ).reshape((chunk_height, width))
            b_chunk = np.frombuffer(
                channels[2][y_start * width * 4 : y_end * width * 4],
                dtype=np.float32,
            ).reshape((chunk_height, width))

            rgb_chunk = np.stack([r_chunk, g_chunk, b_chunk], axis=-1)
            flat_chunk = rgb_chunk.reshape(-1, 3).astype(np.float32)
            from PyOpenColorIO import PackedImageDesc  # type: ignore

            img_desc = PackedImageDesc(flat_chunk, width, chunk_height, 3)
            cpu_processor.apply(img_desc)
            processed_chunk = flat_chunk.reshape(chunk_height, width, 3)
            processed_chunks.append(processed_chunk)

            del r_chunk, g_chunk, b_chunk, rgb_chunk, flat_chunk
            gc.collect()

        img = (
            np.vstack(processed_chunks)
            if processed_chunks
            else np.zeros((height, width, 3), dtype=np.float32)
        )
        img = np.clip(img, 0.0, 1.0)
        img8 = (img * 255.0 + 0.5).astype(np.uint8)

        pil_img = Image.fromarray(img8, mode="RGB")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pil_img.save(str(output_path), "PNG")
    finally:
        try:
            exr.close()
        except Exception:
            pass
        gc.collect()


def _expand_sequence(pattern: str) -> Tuple[Path, str, int, List[Path]]:
    pattern_path = Path(pattern)
    directory = pattern_path.parent
    template = pattern_path.name
    match = re.search(r"%0(\d+)d", template)
    if not match:
        raise RuntimeError("Input pattern must contain %0Nd placeholder")
    digits = int(match.group(1))
    glob_pattern = re.sub(r"%0\d+d", "?" * digits, template)
    files = sorted(directory.glob(glob_pattern))
    frame_regex = re.compile(r"(\d+)(?=\.[^.]+$)")

    def sort_key(path: Path) -> int:
        m = frame_regex.search(path.name)
        if not m:
            raise RuntimeError(f"Could not extract frame number from {path.name}")
        return int(m.group(1))

    files.sort(key=sort_key)
    if not files:
        raise RuntimeError(f"No frames found matching pattern {pattern}")
    return directory, template, digits, files


def convert_sequence_cpu(
    *,
    input_pattern: str,
    start_number: int,
    config_path: Path,
    input_space: str,
    display: str,
    view: str,
    temp_dir: Optional[Union[str, Path]],
) -> Tuple[str, int, Optional[tempfile.TemporaryDirectory[str]]]:
    directory, template, digits, files = _expand_sequence(input_pattern)

    if config_path is None or not config_path.exists():
        raise RuntimeError("OCIO config is required for CPU color mode")

    cpu_processor = _load_cpu_processor(config_path, input_space, display, view)

    temp_dir_obj: Optional[tempfile.TemporaryDirectory[str]]
    base_dir: Optional[Path]
    if temp_dir is not None and str(temp_dir).strip():
        base_dir = Path(str(temp_dir).strip())
        base_dir.mkdir(parents=True, exist_ok=True)
        base_dir_str = str(base_dir)
    else:
        base_dir = _resolve_base_temp_dir(None)
        base_dir_str = str(base_dir)

    temp_dir_obj = tempfile.TemporaryDirectory(prefix="preview_cpu_", dir=base_dir_str)
    cpu_dir = Path(temp_dir_obj.name)
    _register_temp_path(cpu_dir)

    try:
        frame_regex = re.compile(r"(\d+)(?=\.[^.]+$)")
        frame_numbers: List[int] = []
        for exr_path in files:
            match = frame_regex.search(exr_path.name)
            if not match:
                raise RuntimeError(f"Could not extract frame number from {exr_path.name}")
            frame_num = int(match.group(1))
            frame_numbers.append(frame_num)
            output_filename = exr_path.with_suffix(".png").name
            output_path = cpu_dir / output_filename
            logging.debug("Converting %s -> %s", exr_path, output_path)
            _convert_exr_to_png_cpu(exr_path, output_path, cpu_processor)

        first_frame = frame_numbers[0] if frame_numbers else start_number
        png_template = template
        if png_template.lower().endswith(".exr"):
            png_template = png_template[:-4] + ".png"
        else:
            png_template = png_template + ".png"

        converted_pattern = str(cpu_dir / png_template)
        return converted_pattern, first_frame, temp_dir_obj
    except Exception:
        try:
            temp_dir_obj.cleanup()
        finally:
            _unregister_temp_path(cpu_dir)
        raise

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

def _resolve_ffprobe_path(ffmpeg_path: str) -> str:
    if not ffmpeg_path or ffmpeg_path == "ffmpeg":
        return "ffprobe"
    ffmpeg_path_obj = Path(ffmpeg_path)
    if ffmpeg_path_obj.name.lower().endswith("ffmpeg"):
        return str(ffmpeg_path_obj.with_name("ffprobe"))
    return "ffprobe"

def _get_video_duration_seconds(video_path: Path, ffprobe_path: str) -> Optional[float]:
    try:
        cmd = [
            ffprobe_path,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return float(result.stdout.strip())
    except Exception as exc:
        logging.warning("Failed to read duration via ffprobe: %s", exc)
        return None

def _get_file_size_mb(video_path: Path) -> float:
    return video_path.stat().st_size / (1024 * 1024)

def _compress_if_needed(
    video_path: Path,
    ffmpeg_path: str,
    max_size_mb: float,
) -> None:
    current_size = _get_file_size_mb(video_path)
    if current_size <= max_size_mb:
        return

    ffprobe_path = _resolve_ffprobe_path(ffmpeg_path)
    duration = _get_video_duration_seconds(video_path, ffprobe_path)
    target_bitrate = None
    min_bitrate = 200
    if duration and duration > 0:
        target_size_bytes = max_size_mb * 1024 * 1024 * 0.92
        target_bitrate = int((target_size_bytes * 8) / duration / 1000)

    def run_two_pass(output_path: Path, bitrate_k: int, passlogfile: Path) -> None:
        base_cmd = [
            ffmpeg_path, "-y",
            "-i", str(video_path),
            "-c:v", "libx264",
            "-b:v", f"{bitrate_k}k",
            "-maxrate", f"{int(bitrate_k * 1.2)}k",
            "-bufsize", f"{int(bitrate_k * 2)}k",
            "-preset", "medium",
            "-pix_fmt", "yuv420p",
            "-an",
        ]
        passlog_arg = str(passlogfile)
        cmd_pass1 = base_cmd + ["-pass", "1", "-passlogfile", passlog_arg, "-f", "mp4", os.devnull]
        cmd_pass2 = base_cmd + ["-pass", "2", "-passlogfile", passlog_arg, str(output_path)]
        run_ffmpeg(cmd_pass1)
        run_ffmpeg(cmd_pass2)

    def run_crf(output_path: Path, crf: int) -> None:
        cmd = [
            ffmpeg_path, "-y",
            "-i", str(video_path),
            "-c:v", "libx264",
            "-crf", str(crf),
            "-preset", "slow",
            "-pix_fmt", "yuv420p",
            "-an",
            str(output_path),
        ]
        run_ffmpeg(cmd)

    attempts = [1.0, 0.85, 0.7, 0.55]
    best_path: Optional[Path] = None
    try:
        if target_bitrate is not None:
            for idx, factor in enumerate(attempts, start=1):
                bitrate_k = max(int(target_bitrate * factor), min_bitrate)
                output_path = video_path.with_name(f"{video_path.stem}_compressed_{idx}.mp4")
                passlogfile = video_path.with_name(f"{video_path.stem}_passlog_{idx}")
                try:
                    run_two_pass(output_path, bitrate_k, passlogfile)
                finally:
                    for suffix in (".log", ".log.mbtree", "-0.log", "-0.log.mbtree"):
                        path = Path(f"{passlogfile}{suffix}")
                        if path.exists():
                            with contextlib.suppress(Exception):
                                path.unlink()
                best_path = output_path
                if _get_file_size_mb(best_path) <= max_size_mb:
                    break

        if best_path is None or _get_file_size_mb(best_path) > max_size_mb:
            for crf in (24, 26, 28, 30, 32, 34):
                output_path = video_path.with_name(f"{video_path.stem}_compressed_crf{crf}.mp4")
                run_crf(output_path, crf)
                best_path = output_path
                if _get_file_size_mb(best_path) <= max_size_mb:
                    break

        if best_path and best_path.exists():
            best_path.replace(video_path)
            logging.info(
                "Compressed preview to %.1f MB and replaced output at %s",
                _get_file_size_mb(video_path),
                video_path,
            )
        else:
            logging.warning(
                "Compression did not produce a replacement file for %s",
                video_path,
            )
    except Exception as exc:
        logging.warning("Compression attempt failed: %s", exc)


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
    parser.add_argument("--input-pattern", required=True, help="Sequence pattern, e.g. path/to/shot.%04d.exr")
    parser.add_argument("--output-path", required=True, help="Destination MP4 path")
    parser.add_argument("--start-number", type=int, default=0, help="First frame number in the sequence")
    parser.add_argument("--frame-rate", type=float, default=25.0, help="Playback frame rate")
    parser.add_argument("--ffmpeg-path", default="ffmpeg", help="ffmpeg executable available on the worker")
    parser.add_argument("--preset", default="medium", help="ffmpeg libx264 preset")
    parser.add_argument("--crf", type=int, default=20, help="ffmpeg CRF value")
    parser.add_argument("--max-size-mb", type=float, default=45.0, help="Max MP4 size in MB for delivery")
    parser.add_argument(
        "--temp-dir",
        default=None,
        help="Optional directory for temporary files (uses system temp if omitted)",
    )
    parser.add_argument(
        "--color-mode",
        choices=["lut", "cpu"],
        default="lut",
        help="Color transform mode: 'lut' (default) or 'cpu' to run OCIO on CPU",
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
    _install_signal_handlers()

    try:
        cleanup_resources: List[tempfile.TemporaryDirectory[str]] = []
        base_temp_dir = _resolve_base_temp_dir(args.temp_dir)

        color_mode = args.color_mode.lower()
        apply_color = not args.disable_color
        input_ext = Path(args.input_pattern).suffix.lower()
        is_exr_input = input_ext == ".exr"
        if not is_exr_input and apply_color:
            logging.warning("Non-EXR input detected; disabling color transform.")
            apply_color = False
        config_path: Optional[Path] = None
        if apply_color:
            config_path_optional = resolve_config_path(args)
            config_path = Path(config_path_optional) if config_path_optional else None

        ffmpeg_input_pattern = args.input_pattern
        start_number = args.start_number
        lut_path: Optional[Path] = None

        if apply_color and color_mode == "cpu":
            if config_path is None:
                raise RuntimeError("OCIO config required for CPU color mode")
            converted_pattern, start_number, cpu_temp_dir = convert_sequence_cpu(
                input_pattern=args.input_pattern,
                start_number=args.start_number,
                config_path=config_path,
                input_space=args.input_space,
                display=args.display,
                view=args.view,
                temp_dir=base_temp_dir,
            )
            ffmpeg_input_pattern = converted_pattern
            if cpu_temp_dir is not None:
                cleanup_resources.append(cpu_temp_dir)
        elif apply_color:
            if config_path is None:
                raise RuntimeError("OCIO config required for LUT color mode")
            if args.lut_path:
                lut_path = Path(args.lut_path)
            else:
                lut_temp_dir = tempfile.TemporaryDirectory(prefix="preview_lut_", dir=str(base_temp_dir))
                cleanup_resources.append(lut_temp_dir)
                _register_temp_path(Path(lut_temp_dir.name))
                lut_path = Path(lut_temp_dir.name) / "preview_lut.cube"
            bake_preview_lut(
                config_path=config_path,
                input_space=args.input_space,
                display=args.display,
                view=args.view,
                lut_size=args.lut_size,
                destination=lut_path,
            )

        command = build_ffmpeg_command(
            ffmpeg_path=args.ffmpeg_path,
            start_number=start_number,
            frame_rate=args.frame_rate,
            input_pattern=ffmpeg_input_pattern,
            output_path=args.output_path,
            lut_path=lut_path if apply_color and color_mode == "lut" else None,
            preset=args.preset,
            crf=args.crf,
        )
        run_ffmpeg(command)
        _compress_if_needed(Path(args.output_path), args.ffmpeg_path, args.max_size_mb)
        logging.info("Preview video successfully written to %s", args.output_path)

        if apply_color and color_mode == "lut" and args.keep_lut:
            logging.info("LUT kept at %s", lut_path)
    except Exception as exc:  # pragma: no cover - Deadline handles logging
        logging.error("Preview conversion failed: %s", exc, exc_info=True)
        return 1
    finally:
        for temp_dir_cm in cleanup_resources:
            temp_dir_path: Optional[Path]
            try:
                temp_dir_path = Path(temp_dir_cm.name)
            except Exception:
                temp_dir_path = None

            try:
                temp_dir_cm.cleanup()
            except Exception as cleanup_error:
                logging.warning("Failed to cleanup temp dir via context manager: %s", cleanup_error)

            if temp_dir_path and temp_dir_path.exists():
                shutil.rmtree(temp_dir_path, ignore_errors=True)
                if temp_dir_path.exists():
                    logging.warning("Temporary preview folder persists after cleanup: %s", temp_dir_path)
            _unregister_temp_path(temp_dir_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
