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
        logger.error(f"Error compressing video: {e}")
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
