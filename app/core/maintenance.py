"""Filesystem/media maintenance helpers used by preview and lifecycle flows."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


def clear_folder(folder_path: str | Path, preserve_prefixes: tuple[str, ...] = ()) -> None:
    """Clear folder contents without deleting the folder itself."""
    folder = Path(folder_path)
    if folder.exists():
        for item in folder.iterdir():
            if preserve_prefixes and item.name.startswith(preserve_prefixes):
                continue
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink()
            except Exception:
                pass
    else:
        folder.mkdir(parents=True, exist_ok=True)


def cleanup_temp_and_conv() -> None:
    """Clear the temp and conv directories.

    PREVIEW_TEMP_DIR is left alone: it names a folder on the render workers,
    where the preview script cleans up after itself, and on the bot host the
    same path can be a folder the bot has no business emptying.
    """
    preserve_prefixes = ("upload_",) if settings.preview_upload_enabled else ()
    clear_folder(Path(settings.temp_dir), preserve_prefixes=preserve_prefixes)
    clear_folder(Path(settings.conv_dir))
    logger.info("Cleaned up temp and conv directories")


def force_cleanup_temp_and_conv() -> None:
    """Force cleanup for error recovery; keeps directories in place."""
    preserve_prefixes = ("upload_",) if settings.preview_upload_enabled else ()
    clear_folder(Path(settings.temp_dir), preserve_prefixes=preserve_prefixes)
    clear_folder(Path(settings.conv_dir))
    logger.info("Force cleaned up temp and conv directories")


def cleanup_old_files(max_age_hours: int = 24) -> None:
    """Delete files older than max_age_hours from managed directories."""
    import time

    current_time = time.time()
    cutoff_time = current_time - (max_age_hours * 3600)

    temp_dir = Path(settings.temp_dir)
    conv_dir = Path(settings.conv_dir)

    cleaned_count = 0
    directories_to_clean = [temp_dir, conv_dir]

    # An upload that has landed but not yet reached the chat belongs to the
    # token that is waiting to deliver it, and that token lives for days. Aging
    # its file out from under it leaves a record pointing at nothing, which no
    # upload can ever satisfy again - the token store clears both together when
    # the token itself expires. The startup sweep already spares these.
    preserve_prefixes = ("upload_",) if settings.preview_upload_enabled else ()

    for directory in directories_to_clean:
        if not directory.exists():
            continue

        for item in directory.iterdir():
            if preserve_prefixes and item.name.startswith(preserve_prefixes):
                continue
            try:
                if item.stat().st_mtime < cutoff_time:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink()
                    cleaned_count += 1
                    logger.debug("Cleaned up old file: %s", item)
            except Exception as exc:
                logger.warning("Failed to clean up %s: %s", item, exc)

    if cleaned_count > 0:
        logger.info("Cleaned up %s old files (older than %s hours)", cleaned_count, max_age_hours)


def get_directory_sizes() -> dict:
    """Return size statistics for temp and conv directories in MB."""
    temp_dir = Path(settings.temp_dir)
    conv_dir = Path(settings.conv_dir)

    def get_dir_size(path: Path) -> float:
        if not path.exists():
            return 0.0
        total_size = 0
        for item in path.rglob("*"):
            if item.is_file():
                total_size += item.stat().st_size
        return total_size / (1024 * 1024)

    temp_mb = get_dir_size(temp_dir)
    conv_mb = get_dir_size(conv_dir)
    return {
        "temp_mb": temp_mb,
        "conv_mb": conv_mb,
        "total_mb": temp_mb + conv_mb,
    }


def log_directory_sizes() -> None:
    """Log current directory sizes and warn on very large usage."""
    sizes = get_directory_sizes()
    logger.info(
        "Directory sizes - Temp: %.1fMB, Conv: %.1fMB, Total: %.1fMB",
        sizes["temp_mb"],
        sizes["conv_mb"],
        sizes["total_mb"],
    )

    if sizes["total_mb"] > 1000:
        logger.warning("Large directory size detected: %.1fMB total", sizes["total_mb"])


def ensure_temp_dir() -> Path:
    """Ensure temp directory exists and return it."""
    temp_path = Path(settings.temp_dir)
    temp_path.mkdir(exist_ok=True)
    return temp_path


def get_video_duration(video_path: Path) -> Optional[float]:
    """Get media duration in seconds via ffprobe."""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as exc:
        logger.error(
            "Error getting video duration for %s: %s", video_path.name, type(exc).__name__
        )
        return None


def make_progress_bar(percent: int, width: int = 10) -> str:
    """Return a simple unicode progress bar string."""
    filled = int(width * percent / 100)
    empty = width - filled
    return "█" * filled + "░" * empty
