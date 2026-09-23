from pathlib import Path
import subprocess
import os
import logging
import contextlib
from typing import Optional
import asyncio
import shutil
from dataclasses import dataclass

from app.core.config import settings

logger = logging.getLogger(__name__)


def get_file_size_mb(file_path: Path) -> float:
    """
    Get file size in megabytes.
    
    Args:
        file_path (Path): Path to the file
        
    Returns:
        float: File size in MB
    """
    return file_path.stat().st_size / (1024 * 1024)

def compress_video_if_needed(video_path: Path, max_size_mb: float = 45.0) -> Path:
    """
    Compress video if it's larger than max_size_mb.
    
    Args:
        video_path (Path): Path to the video file
        max_size_mb (float): Maximum allowed size in MB
        
    Returns:
        Path: Path to the compressed video (or original if compression not needed)
    """
    current_size = get_file_size_mb(video_path)
    if current_size <= max_size_mb:
        return video_path
        
    # Calculate target bitrate (in kbps) based on desired file size
    # Formula: bitrate = target_size_bytes * 8 / duration_seconds / 1000
    from app.core.maintenance import get_video_duration
    duration = get_video_duration(video_path)

    target_bitrate = None
    min_bitrate = 200
    if duration and duration > 0:
        target_size_bytes = max_size_mb * 1024 * 1024 * 0.92
        target_bitrate = int((target_size_bytes * 8) / duration / 1000)
    ffmpeg_bin = settings.ffmpeg_path or "ffmpeg"

    def _run_two_pass(
        output_path: Path,
        bitrate_k: int,
        passlogfile: Path,
    ) -> Optional[Path]:
        base_cmd = [
            ffmpeg_bin, "-y",
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
        subprocess.run(cmd_pass1, check=True, capture_output=True)
        subprocess.run(cmd_pass2, check=True, capture_output=True)
        return output_path

    def _run_crf(
        output_path: Path,
        crf: int,
        preset: str = "slow",
    ) -> Optional[Path]:
        cmd = [
            ffmpeg_bin, "-y",
            "-i", str(video_path),
            "-c:v", "libx264",
            "-crf", str(crf),
            "-preset", preset,
            "-pix_fmt", "yuv420p",
            "-an",
            str(output_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return output_path

    compressed_path = video_path.parent / f"{video_path.stem}_compressed.mp4"
    best_path: Optional[Path] = None
    try:
        if target_bitrate is not None:
            attempts = [1.0, 0.85, 0.7, 0.55]
            for idx, factor in enumerate(attempts, start=1):
                bitrate_k = max(int(target_bitrate * factor), min_bitrate)
                output_path = (
                    compressed_path
                    if idx == 1
                    else video_path.parent / f"{video_path.stem}_compressed_{idx}.mp4"
                )
                passlogfile = video_path.parent / f"{video_path.stem}_passlog_{idx}"
                try:
                    best_path = _run_two_pass(output_path, bitrate_k, passlogfile)
                finally:
                    for suffix in (".log", ".log.mbtree", "-0.log", "-0.log.mbtree"):
                        path = Path(f"{passlogfile}{suffix}")
                        if path.exists():
                            with contextlib.suppress(Exception):
                                path.unlink()
                if best_path and get_file_size_mb(best_path) <= max_size_mb:
                    best_path.replace(video_path)
                    return video_path

        crf_values = [24, 26, 28, 30, 32, 34]
        for crf in crf_values:
            output_path = video_path.parent / f"{video_path.stem}_compressed_crf{crf}.mp4"
            best_path = _run_crf(output_path, crf=crf)
            if best_path and get_file_size_mb(best_path) <= max_size_mb:
                best_path.replace(video_path)
                return video_path

        if best_path:
            compressed_size = get_file_size_mb(best_path)
            if compressed_size > max_size_mb:
                logger.warning(
                    "Compressed video still too large: %.1fMB > %.0fMB",
                    compressed_size,
                    max_size_mb,
                )
        return video_path
    except Exception as e:
        logger.error(f"Error compressing video {video_path.name}: {type(e).__name__}")
        return video_path
    finally:
        for candidate in video_path.parent.glob(f"{video_path.stem}_compressed*.mp4"):
            if candidate.exists():
                with contextlib.suppress(Exception):
                    candidate.unlink()


@dataclass
class VideoDeliveryPreparation:
    video_path: Path
    size_mb: float
    fallback_message: Optional[str]
    was_compressed: bool = False


async def prepare_video_for_delivery(
    video_path: Path,
    display_path: Optional[str] = None,
    max_size_mb: float = 45.0,
    initial_size_mb: Optional[float] = None,
) -> VideoDeliveryPreparation:
    """
    Ensure a video is ready to be delivered via Telegram by enforcing file-size limits.

    Returns a dataclass with the potentially updated video path, its size, and an optional
    fallback message when the file still exceeds the limit even after compression.
    """
    size_mb = initial_size_mb if initial_size_mb is not None else get_file_size_mb(video_path)
    was_compressed = False

    if size_mb > max_size_mb:
        video_path = await asyncio.to_thread(
            compress_video_if_needed,
            video_path,
            max_size_mb,
        )
        size_mb = get_file_size_mb(video_path)
        was_compressed = True

    fallback_message = None
    if size_mb > max_size_mb:
        location_hint = (
            f"<code>{display_path}</code>"
            if display_path
            else f"<code>{video_path}</code>"
        )
        fallback_message = (
            "⚠️ Preview video is ready but still too large to send via Telegram "
            f"({size_mb:.1f} MB > {max_size_mb:.0f} MB).\n"
            f"Please download it manually:\n{location_hint}"
        )

    return VideoDeliveryPreparation(
        video_path=video_path,
        size_mb=size_mb,
        fallback_message=fallback_message,
        was_compressed=was_compressed,
    )

@dataclass
class VideoMetadata:
    """What Telegram needs in order to lay a video out correctly."""

    width: int
    height: int
    duration: int


def probe_video_metadata(video_path: Path) -> Optional[VideoMetadata]:
    """Read display dimensions and duration straight from the file being sent.

    Uses the *display* aspect ratio, not the coded one: a stream stored with a
    non-square pixel aspect would otherwise report dimensions that make Telegram
    lay the player out wrong.
    """
    ffprobe_bin = "ffprobe"
    if settings.ffmpeg_path and settings.ffmpeg_path != "ffmpeg":
        candidate = Path(settings.ffmpeg_path).with_name("ffprobe")
        if candidate.exists():
            ffprobe_bin = str(candidate)
    try:
        probe = subprocess.run(
            [
                ffprobe_bin,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,sample_aspect_ratio:format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=0",
                str(video_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        logger.warning("ffprobe unavailable for %s: %s", video_path.name, type(exc).__name__)
        return None
    if probe.returncode != 0:
        stderr = (probe.stderr or "").replace(str(video_path), video_path.name)
        logger.warning("ffprobe failed for %s: %s", video_path.name, stderr[:200])
        return None

    values: dict[str, str] = {}
    for line in (probe.stdout or "").splitlines():
        key, _, value = line.partition("=")
        if key:
            values[key.strip()] = value.strip()

    try:
        width = int(values["width"])
        height = int(values["height"])
    except (KeyError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None

    sar = values.get("sample_aspect_ratio") or ""
    if ":" in sar and sar not in ("0:1", "1:1", "N/A"):
        try:
            sar_num, sar_den = (int(part) for part in sar.split(":", 1))
            if sar_num > 0 and sar_den > 0:
                width = max(1, round(width * sar_num / sar_den))
        except ValueError:
            pass

    duration = 0
    try:
        duration = max(0, round(float(values.get("duration") or 0)))
    except ValueError:
        duration = 0

    return VideoMetadata(width=width, height=height, duration=duration)


def make_video_thumbnail(video_path: Path) -> Optional[Path]:
    """Grab a poster frame for the video, matching its aspect ratio.

    Telegram clients size the player from the thumbnail. Without one they pick
    a shape themselves, which is how a 3:2 preview ends up square on phones.
    Telegram requires JPEG, at most 320px on a side and under 200 kB.
    """
    ffmpeg_bin = settings.ffmpeg_path or "ffmpeg"
    thumb_path = video_path.parent / f"{video_path.stem}_thumb.jpg"
    try:
        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                # Fit inside 320x320 without padding, keeping square pixels.
                "-vf",
                "scale='if(gt(a,1),320,-2)':'if(gt(a,1),-2,320)',setsar=1",
                "-q:v",
                "4",
                str(thumb_path),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except Exception as exc:
        logger.warning(
            "Could not build thumbnail for %s: %s", video_path.name, type(exc).__name__
        )
        return None

    if not thumb_path.exists() or thumb_path.stat().st_size == 0:
        return None
    if thumb_path.stat().st_size > 200 * 1024:
        with contextlib.suppress(Exception):
            thumb_path.unlink()
        return None
    return thumb_path


def cleanup_job_files(job_id: str):
    """
    Clean up temporary files for a specific job.
    
    Args:
        job_id (str): Job ID to clean up files for
    """
    try:
        temp_root = Path(settings.temp_dir)
        conv_root = Path(settings.conv_dir)
        
        # Clean up temp directory
        if temp_root.exists():
            for item in temp_root.glob(f"*_{job_id}*"):
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
                    
        # Clean up conv directory
        if conv_root.exists():
            for item in conv_root.glob(f"*_{job_id}*"):
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
                    
    except Exception as e:
        logger.error(f"Error cleaning up files for job {job_id}: {e}")
