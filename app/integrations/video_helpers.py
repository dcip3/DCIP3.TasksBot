import numpy as np
import OpenEXR
import PyOpenColorIO as ocio
import Imath
from pathlib import Path
import subprocess
import os
import sys
import logging
import contextlib
import gc
from typing import Optional
from PIL import Image
import asyncio
import shutil
from functools import lru_cache
from dataclasses import dataclass

from app.core.config import settings

logger = logging.getLogger(__name__)
GC_RSS_THRESHOLD_MB = 1024


def _get_process_rss_mb() -> Optional[float]:
    if not sys.platform.startswith("linux"):
        return None
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as fh:
            parts = fh.read().split()
        if len(parts) < 2:
            return None
        rss_pages = int(parts[1])
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        return (rss_pages * page_size) / (1024 * 1024)
    except Exception:
        return None


def _maybe_collect_gc_for_memory_pressure() -> None:
    rss_mb = _get_process_rss_mb()
    if rss_mb is None:
        return
    if rss_mb >= GC_RSS_THRESHOLD_MB:
        logger.debug("High RSS %.1fMB detected after EXR conversion, triggering gc.collect()", rss_mb)
        gc.collect()

@lru_cache(maxsize=1)
def _get_default_cpu_processor() -> ocio.CPUProcessor:
    """Return a cached OCIO CPU processor built from the default config."""
    config_path = Path(settings.ocio_config_path)
    if not config_path.exists():
        raise RuntimeError(f"OCIO config not found: {config_path}")

    config = ocio.Config.CreateFromFile(str(config_path))
    transform = ocio.DisplayViewTransform()
    transform.setSrc("ACEScg")
    transform.setDisplay("sRGB")
    transform.setView("ACES 1.0 SDR-video")
    transform.setDirection(ocio.TRANSFORM_DIR_FORWARD)

    processor = config.getProcessor(transform)
    cpu_processor = processor.getDefaultCPUProcessor()
    if cpu_processor is None:
        raise RuntimeError("Failed to create OCIO CPU processor from config.ocio")
    return cpu_processor


def convert_single_exr_file_streaming(args):
    """
    Converts a single EXR file to JPG using streaming processing to minimize memory usage.
    
    Args:
        args: Tuple of (exr_path, conv_root, cpu_processor, width, height, target_width, target_height)
    
    Returns:
        Tuple of (success, exr_path, error_message)
    """
    exr_path, conv_root, cpu_processor, _, _, target_width, target_height = args
    
    try:
        FLOAT = Imath.PixelType(Imath.PixelType.FLOAT)
        
        exr = OpenEXR.InputFile(str(exr_path))
        
        # Get dimensions from the actual file
        header = exr.header()
        dw = header['dataWindow']
        width = dw.max.x - dw.min.x + 1
        height = dw.max.y - dw.min.y + 1
        
        channels = exr.channels(["R", "G", "B"], FLOAT)
        
        # Process image in chunks to save memory
        chunk_size = min(256, height)  # Process in smaller chunks
        processed_chunks = []
        
        for y_start in range(0, height, chunk_size):
            y_end = min(y_start + chunk_size, height)
            chunk_height = y_end - y_start
            
            # Read chunk data
            r_chunk = np.frombuffer(channels[0][y_start * width * 4:(y_end * width * 4)], dtype=np.float32).reshape((chunk_height, width))
            g_chunk = np.frombuffer(channels[1][y_start * width * 4:(y_end * width * 4)], dtype=np.float32).reshape((chunk_height, width))
            b_chunk = np.frombuffer(channels[2][y_start * width * 4:(y_end * width * 4)], dtype=np.float32).reshape((chunk_height, width))
            
            # Stack and process
            rgb_chunk = np.stack([r_chunk, g_chunk, b_chunk], axis=-1)
            flat_chunk = rgb_chunk.reshape(-1, 3).astype(np.float32)
            
            # Apply color transform (load default if processor not supplied)
            if cpu_processor is None:
                cpu_processor = _get_default_cpu_processor()
            img_desc = ocio.PackedImageDesc(flat_chunk, width, chunk_height, 3)
            cpu_processor.apply(img_desc)
            processed_chunk = flat_chunk.reshape(chunk_height, width, 3)
            
            processed_chunks.append(processed_chunk)
            
            # Clear chunk memory immediately
            del r_chunk, g_chunk, b_chunk, rgb_chunk, flat_chunk
        
        # Close EXR file before combining chunks
        exr.close()
        
        # Combine chunks and convert to 8-bit
        img = np.vstack(processed_chunks) if processed_chunks else np.zeros((height, width, 3), dtype=np.float32)
        img = np.clip(img * 255, 0, 255).astype(np.uint8)
        
        # Handle resizing if needed
        if target_width and target_height and (target_width != width or target_height != height):
            pil_img = Image.fromarray(img)
            pil_img = pil_img.resize((target_width, target_height), Image.Resampling.LANCZOS)
            img = np.array(pil_img)
            del pil_img
        
        # Save as JPG
        jpg_path = conv_root / f"{exr_path.stem}.jpg"
        pil_img = Image.fromarray(img)
        pil_img.save(str(jpg_path), "JPEG", quality=95, optimize=True)
        del pil_img
        
        # Cleanup
        del processed_chunks, img
        _maybe_collect_gc_for_memory_pressure()
        
        # Delete source EXR file immediately after successful conversion
        try:
            if exr_path.exists():
                exr_path.unlink()
                logger.debug(f"Deleted source file: {exr_path}")
        except Exception as del_error:
            logger.warning(f"Failed to delete source file {exr_path}: {del_error}")
        
        return (True, exr_path, None)
        
    except Exception as e:
        # Try to close EXR file in case of error
        try:
            exr.close()
        except:
            pass
        logger.error(f"Failed to convert {exr_path}: {e}")
        return (False, exr_path, str(e))

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
    dropbox_path: Optional[str] = None,
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
            f"<code>{dropbox_path}</code>"
            if dropbox_path
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

def assemble_video_from_jpg(conv_root: Path, exr_folder_name: str) -> Path:
    """
    Assembles MP4 from converted JPG/PNG files using ffmpeg.
    
    Args:
        conv_root (Path): Directory with converted frames
        exr_folder_name (str): Folder name used for output video name
    
    Returns:
        Path: Path to the generated video file
    
    Raises:
        RuntimeError: if no frames found or ffmpeg error occurs
    """
    video_path = conv_root / f"{exr_folder_name}.mp4"
    jpg_files = list(conv_root.glob("*.jpg"))
    jpeg_files = list(conv_root.glob("*.jpeg"))
    png_files = list(conv_root.glob("*.png"))
    if not jpg_files and not jpeg_files and not png_files:
        raise RuntimeError("No frames found for video assembly.")
    if (jpg_files or jpeg_files) and png_files:
        raise RuntimeError("Mixed JPG and PNG sequences are not supported.")
    if jpg_files and jpeg_files:
        raise RuntimeError("Mixed JPG and JPEG sequences are not supported.")
    if jpg_files:
        pattern = str(conv_root / "*.jpg")
    elif jpeg_files:
        pattern = str(conv_root / "*.jpeg")
    else:
        pattern = str(conv_root / "*.png")
    
    ffmpeg_bin = settings.ffmpeg_path or "ffmpeg"
    cmd = [
        ffmpeg_bin,
        "-y",
        "-pattern_type", "glob",
        "-i", pattern,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        str(video_path)
    ]
    subprocess.run(cmd, check=True)
    
    try:
        for frame_file in conv_root.glob(Path(pattern).name):
            frame_file.unlink()
        logger.debug("Deleted intermediate frame files")
    except Exception as e:
        logger.warning(f"Failed to cleanup frame files: {e}")
    
    return video_path 
