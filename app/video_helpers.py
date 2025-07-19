

import numpy as np
import OpenEXR
import PyOpenColorIO as ocio
import Imath
from pathlib import Path
import subprocess
import os
import logging
from pathlib import Path
from typing import Optional, Generator
import gc
import psutil
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing
import time

logger = logging.getLogger(__name__)

def get_disk_usage(path: Path) -> float:
    """Get disk usage in MB for a path."""
    try:
        total, used, free = shutil.disk_usage(path)
        return used / 1024 / 1024  # Convert to MB
    except Exception:
        return 0.0

def get_memory_usage():
    """Get current memory usage in MB."""
    process = psutil.Process()
    return process.memory_info().rss / 1024 / 1024

def get_system_resources():
    """Get current system resource usage for adaptive processing."""
    memory = psutil.virtual_memory()
    cpu_percent = psutil.cpu_percent(interval=1)
    disk = shutil.disk_usage(Path("."))
    
    return {
        'memory_available_mb': memory.available / 1024 / 1024,
        'memory_percent': memory.percent,
        'cpu_percent': cpu_percent,
        'disk_free_mb': disk.free / 1024 / 1024,
        'disk_percent': (disk.used / disk.total) * 100
    }

def calculate_optimal_workers():
    """Calculate optimal number of workers based on system resources."""
    resources = get_system_resources()
    
    # Very conservative settings for VDS with limited resources
    if resources['memory_available_mb'] < 512:  # Less than 512MB available
        return 1
    elif resources['memory_available_mb'] < 1024:  # Less than 1GB available
        return 1
    elif resources['memory_available_mb'] < 2048:  # Less than 2GB available
        return 2
    else:
        return min(3, multiprocessing.cpu_count())  # Max 3 workers even on powerful systems

def convert_single_exr_file_streaming(args):
    """
    Convert a single EXR file with streaming approach to minimize memory usage.
    Immediately deletes source file after conversion to save disk space.
    
    Args:
        args: Tuple of (exr_path, conv_root, cpu_processor, width, height, target_width, target_height)
    
    Returns:
        Tuple of (success, exr_path, error_message)
    """
    exr_path, conv_root, cpu_processor, width, height, target_width, target_height = args
    
    try:
        FLOAT = Imath.PixelType(Imath.PixelType.FLOAT)
        
        exr = OpenEXR.InputFile(str(exr_path))
        channels = exr.channels(["R", "G", "B"], FLOAT)
        
        # Get image dimensions
        header = exr.header()
        dw = header['dataWindow']
        width = dw.max.x - dw.min.x + 1
        height = dw.max.y - dw.min.y + 1
        
        # Read channels in smaller chunks to reduce memory usage
        chunk_size = min(256, height)  # Process in smaller chunks for better memory usage
        
        # Calculate scaling factors
        scale_x = target_width / width if target_width else 1.0
        scale_y = target_height / height if target_height else 1.0
        
        # Determine if we need to resize
        need_resize = scale_x != 1.0 or scale_y != 1.0
        
        if need_resize:
            # Calculate new dimensions
            new_width = int(width * scale_x)
            new_height = int(height * scale_y)
        else:
            new_width, new_height = width, height
        
        # Process image in chunks
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
            
            # Apply color transform
            if cpu_processor:
                img_desc = ocio.PackedImageDesc(flat_chunk, width, chunk_height, 3)
                cpu_processor.apply(img_desc)
            processed_chunk = flat_chunk.reshape(chunk_height, width, 3)
            
            if need_resize:
                # Simple nearest neighbor resize for speed
                from scipy.ndimage import zoom
                processed_chunk = zoom(processed_chunk, (scale_y, scale_x, 1), order=0)
            
            processed_chunks.append(processed_chunk)
            
            # Clear chunk memory immediately
            del r_chunk, g_chunk, b_chunk, rgb_chunk, flat_chunk
            gc.collect()
        
        # Combine chunks
        if processed_chunks:
            img = np.vstack(processed_chunks)
        else:
            img = np.zeros((new_height, new_width, 3), dtype=np.float32)
        
        # Save with aggressive compression
        out_exr_path = conv_root / exr_path.name
        header_out = OpenEXR.Header(new_width, new_height)
        header_out["compression"] = Imath.Compression(Imath.Compression.ZIP_COMPRESSION)
        header_out["channels"] = {
            "R": Imath.Channel(Imath.PixelType(Imath.PixelType.HALF)),
            "G": Imath.Channel(Imath.PixelType(Imath.PixelType.HALF)),
            "B": Imath.Channel(Imath.PixelType(Imath.PixelType.HALF))
        }
        
        out_exr = OpenEXR.OutputFile(str(out_exr_path), header_out)
        
        # Save in chunks to minimize memory usage
        chunk_size = min(256, new_height)
        for y_start in range(0, new_height, chunk_size):
            y_end = min(y_start + chunk_size, new_height)
            chunk = img[y_start:y_end]
            
            r_half = (chunk[:, :, 0].astype(np.float16)).tobytes()
            g_half = (chunk[:, :, 1].astype(np.float16)).tobytes()
            b_half = (chunk[:, :, 2].astype(np.float16)).tobytes()
            
            out_exr.writePixels({
                "R": r_half,
                "G": g_half,
                "B": b_half
            }, y_start)
            
            del r_half, g_half, b_half
            gc.collect()
        
        out_exr.close()
        exr.close()
        
        # Aggressive memory cleanup
        del processed_chunks, img, channels
        gc.collect()
        
        # IMMEDIATELY delete source file after successful conversion
        try:
            if exr_path.exists():
                exr_path.unlink()
                logger.debug(f"Immediately deleted source file: {exr_path}")
        except Exception as e:
            logger.warning(f"Failed to delete source file {exr_path}: {e}")
        
        return (True, exr_path, None)
        
    except Exception as e:
        return (False, exr_path, str(e))

def convert_exr_folder_to_srgb_optimized(local_root: Path, conv_root: Path, ocio_config_path: str, 
                                       target_width: Optional[int] = None, target_height: Optional[int] = None):
    """
    Optimized EXR conversion with streaming processing and aggressive resource management.
    
    Args:
        local_root (Path): folder with original EXR files.
        conv_root (Path): folder to save converted EXR files.
        ocio_config_path (str): path to OCIO config file.
        target_width (Optional[int]): target width for resizing (None = no resize)
        target_height (Optional[int]): target height for resizing (None = no resize)
    
    Raises:
        RuntimeError: if no EXR files are found or OCIO config is missing.
    """
    conv_root.mkdir(parents=True, exist_ok=True)
    exr_files = sorted([f for f in local_root.glob("*.exr") if "cryptomatte" not in f.name.lower()])
    if not exr_files:
        raise RuntimeError("No EXR frames found for conversion.")

    if not Path(ocio_config_path).exists():
        raise RuntimeError(f"OCIO config not found: {ocio_config_path}")
    
    # Log initial resource usage
    initial_resources = get_system_resources()
    logger.info(f"Initial resources - Memory: {initial_resources['memory_available_mb']:.0f}MB available, "
                f"CPU: {initial_resources['cpu_percent']:.1f}%, Disk: {initial_resources['disk_free_mb']:.0f}MB free")
    
    # Check available disk space
    disk_usage_before = get_disk_usage(conv_root)
    logger.info(f"Disk usage before conversion: {disk_usage_before:.1f} MB")
    
    # Setup OCIO
    config = ocio.Config.CreateFromFile(str(ocio_config_path))
    transform = ocio.DisplayViewTransform()
    transform.setSrc("ACEScg")
    transform.setDisplay("sRGB")
    transform.setView("ACES 1.0 SDR-video")
    transform.setDirection(ocio.TRANSFORM_DIR_FORWARD)
    processor = config.getProcessor(transform)
    cpu_processor = processor.getDefaultCPUProcessor()
    
    # Get first frame dimensions for reference
    first_frame = OpenEXR.InputFile(str(exr_files[0]))
    header = first_frame.header()
    dw = header['dataWindow']
    width = dw.max.x - dw.min.x + 1
    height = dw.max.y - dw.min.y + 1
    first_frame.close()
    
    # Prepare conversion arguments
    conversion_args = [(f, conv_root, cpu_processor, width, height, target_width, target_height) for f in exr_files]
    
    # Track statistics
    start_time = time.time()
    successful_conversions = 0
    failed_conversions = 0
    
    # Process files
    with ThreadPoolExecutor(max_workers=1) as executor:
        # Submit tasks in smaller batches to avoid overwhelming the system
        batch_size = 3  # Process in batches of 3 files
        
        for i in range(0, len(conversion_args), batch_size):
            batch = conversion_args[i:i + batch_size]
            
            # Check resources before each batch
            resources = get_system_resources()
            if resources['memory_available_mb'] < 256:  # Less than 256MB available
                logger.warning("Low memory detected, forcing garbage collection")
                gc.collect()
                time.sleep(1)  # Give system time to free memory
            
            # Submit batch
            future_to_exr = {executor.submit(convert_single_exr_file_streaming, args): args[0] for args in batch}
            
            # Process completed tasks
            for future in as_completed(future_to_exr):
                exr_path = future_to_exr[future]
                try:
                    success, path, error = future.result()
                    if success:
                        successful_conversions += 1
                        if successful_conversions % 3 == 0:  # Log progress every 3 files
                            elapsed = time.time() - start_time
                            rate = successful_conversions / elapsed if elapsed > 0 else 0
                            logger.info(f"Converted {successful_conversions}/{len(exr_files)} files "
                                      f"({rate:.1f} files/sec)")
                    else:
                        failed_conversions += 1
                        logger.error(f"Failed to convert {path}: {error}")
                except Exception as e:
                    failed_conversions += 1
                    logger.error(f"Exception during conversion of {exr_path}: {e}")
            
            # Force cleanup after each batch
            gc.collect()
    
    # Final statistics
    total_time = time.time() - start_time
    disk_usage_after = get_disk_usage(conv_root)
    disk_usage_diff = disk_usage_after - disk_usage_before
    
    logger.info(f"Conversion completed in {total_time:.1f}s")
    logger.info(f"Successfully converted: {successful_conversions}/{len(exr_files)} files")
    logger.info(f"Failed conversions: {failed_conversions}")
    logger.info(f"Disk usage change: {disk_usage_diff:.1f} MB")
    
    if failed_conversions > 0:
        raise RuntimeError(f"Failed to convert {failed_conversions} files")

def cleanup_original_files_aggressive(exr_files):
    """Aggressively clean up original EXR files to save disk space."""
    try:
        cleaned_count = 0
        for exr_file in exr_files:
            if exr_file.exists():
                try:
                    exr_file.unlink()
                    cleaned_count += 1
                except Exception as e:
                    logger.warning(f"Failed to delete {exr_file}: {e}")
        
        if cleaned_count > 0:
            logger.info(f"Aggressively cleaned up {cleaned_count} original EXR files")
            gc.collect()  # Force garbage collection after cleanup
            
    except Exception as e:
        logger.warning(f"Failed to clean up original EXR files: {e}")

def assemble_video_from_exr_optimized(conv_root: Path, exr_folder_name: str) -> Path:
    """
    Optimized video assembly with VDS-friendly ffmpeg settings.
    
    Args:
        conv_root (Path): folder with converted EXR files.
        exr_folder_name (str): name of the folder used for the output video name.
    
    Returns:
        Path: path to the generated video file.
    
    Raises:
        RuntimeError: if no EXR files are found or if ffmpeg fails.
    """
    video_path = conv_root / f"{exr_folder_name}.mp4"
    exr_pattern = str(conv_root / "*.exr")
    if not any(conv_root.glob("*.exr")):
        raise RuntimeError("No frames to assemble video.")
    
    # Count frames for progress logging
    frame_count = len(list(conv_root.glob("*.exr")))
    logger.info(f"Assembling video from {frame_count} frames with VDS-optimized settings")
    
    # VDS-optimized ffmpeg settings for speed and efficiency
    # Use ultrafast preset for maximum speed, higher CRF for smaller files
    cmd = [
        "ffmpeg",
        "-y",  # Overwrite output files
        "-pattern_type", "glob",
        "-i", exr_pattern,
        "-c:v", "libx264",  # H.264 codec for compatibility
        "-preset", "ultrafast",  # Fastest encoding preset
        "-crf", "23",  # Balanced quality/size (higher than original 18)
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-threads", "1",  # Use single thread to avoid overwhelming VDS
        "-profile:v", "baseline",  # Baseline profile for maximum compatibility
        "-level", "3.1",  # Lower level for better compatibility
        "-tune", "fastdecode",  # Optimize for fast decoding
        "-metadata", "title=" + exr_folder_name,
        "-metadata", "encoder=DCIP3.TasksBot_VDS_Optimized",
        str(video_path)
    ]
    
    logger.info(f"Running VDS-optimized ffmpeg command: {' '.join(cmd)}")
    
    # Monitor resources during encoding
    start_time = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)  # 15 minute timeout
    
    if result.returncode != 0:
        logger.error(f"VDS-optimized encoding failed: {result.stderr}")
        raise RuntimeError(f"VDS-optimized encoding failed: {result.stderr}")
    
    # Check final file size and encoding time
    final_size_mb = get_file_size_mb(video_path)
    encoding_time = time.time() - start_time
    logger.info(f"Video created successfully: {final_size_mb:.2f} MB in {encoding_time:.1f}s")
    
    return video_path

def get_file_size_mb(file_path: Path) -> float:
    """
    Get file size in megabytes.
    
    Args:
        file_path: Path to the file
        
    Returns:
        File size in MB
    """
    if not file_path.exists():
        return 0.0
    return file_path.stat().st_size / (1024 * 1024)

def compress_video_if_needed(video_path: Path, max_size_mb: float = 45.0) -> Path:
    """
    Compress video only if it's larger than max_size_mb.
    Uses VDS-optimized compression settings.
    Replaces original file with compressed version to maintain original filename.
    
    Args:
        video_path: Path to the original video
        max_size_mb: Maximum file size in MB (default 45MB to be safe)
        
    Returns:
        Path to the final video (original or compressed)
    """
    if not video_path.exists():
        logger.error(f"Video file not found: {video_path}")
        return video_path
    
    file_size_mb = get_file_size_mb(video_path)
    logger.info(f"Video size: {file_size_mb:.2f} MB")
    
    if file_size_mb <= max_size_mb:
        logger.info("Video size is within limits, no compression needed")
        return video_path
    
    logger.info(f"Video is too large ({file_size_mb:.2f} MB), applying VDS-optimized compression...")
    
    # VDS-optimized compression levels for speed and efficiency
    compression_levels = [
        {"crf": 26, "preset": "ultrafast", "description": "VDS Fast compression (H.264)"},
        {"crf": 28, "preset": "ultrafast", "description": "VDS High compression (H.264)"},
        {"crf": 30, "preset": "ultrafast", "description": "VDS Ultra compression (H.264)"}
    ]
    
    for level in compression_levels:
        # Create temporary compressed file
        temp_compressed_path = video_path.parent / f"temp_compressed_{video_path.name}"
        
        try:
            cmd = [
                "ffmpeg", "-i", str(video_path),
                "-c:v", "libx264",  # Use H.264 for better compatibility
                "-crf", str(level["crf"]),
                "-preset", level["preset"],
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-threads", "1",  # Use single thread for VDS
                "-profile:v", "baseline",  # Baseline profile for maximum compatibility
                "-level", "3.1",  # Lower level for better compatibility
                "-tune", "fastdecode",  # Optimize for fast decoding
                "-metadata", "title=" + video_path.stem,
                "-metadata", "encoder=DCIP3.TasksBot_VDS_Optimized",
                "-y"
            ]
            
            # Add bitrate limit if specified
            if "bitrate" in level:
                cmd.extend(["-b:v", level["bitrate"]])
            
            cmd.append(str(temp_compressed_path))
            
            logger.info(f"Running {level['description']}: CRF={level['crf']}, preset={level['preset']}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode != 0:
                logger.error(f"VDS compression failed: {result.stderr}")
                if temp_compressed_path.exists():
                    temp_compressed_path.unlink()
                continue
            
            # Check if compression was successful
            compressed_size_mb = get_file_size_mb(temp_compressed_path)
            logger.info(f"Compressed video size: {compressed_size_mb:.2f} MB")
            
            if compressed_size_mb <= max_size_mb:
                logger.info("VDS compression successful, replacing original with compressed version")
                # Replace original file with compressed version
                video_path.unlink()  # Delete original
                temp_compressed_path.rename(video_path)  # Rename compressed to original name
                return video_path
            else:
                logger.warning(f"Compressed video still too large ({compressed_size_mb:.2f} MB), trying next level")
                # Clean up this level and try next
                if temp_compressed_path.exists():
                    temp_compressed_path.unlink()
                    
        except subprocess.TimeoutExpired:
            logger.error("Video compression timed out")
            if temp_compressed_path.exists():
                temp_compressed_path.unlink()
            continue
        except Exception as e:
            logger.error(f"Error during video compression: {e}")
            if temp_compressed_path.exists():
                temp_compressed_path.unlink()
            continue
    
    logger.error("All VDS compression levels failed to reduce size enough")
    return video_path

def cleanup_compressed_files(video_path: Path):
    """
    Clean up temporary compressed files.
    
    Args:
        video_path: Path to the original video
    """
    try:
        # Clean up temporary compression files
        temp_pattern = f"temp_compressed_{video_path.name}"
        temp_files = list(video_path.parent.glob(temp_pattern))
        
        for temp_file in temp_files:
            if temp_file.exists():
                temp_file.unlink()
                logger.info(f"Cleaned up temporary compression file: {temp_file}")
                
    except Exception as e:
        logger.error(f"Error cleaning up compressed files: {e}")


def cleanup_job_files(job_id: str, base_folder_name: Optional[str] = None):
    """
    Clean up all files related to a specific job.
    Optimized for disk space cleanup.
    
    Args:
        job_id: Job ID to clean up
        base_folder_name: Base folder name (optional, will be auto-detected if not provided)
    """
    try:
        from pathlib import Path
        
        temp_dir = Path("temp")
        conv_dir = Path("conv")
        
        # Log disk usage before cleanup
        temp_usage_before = get_disk_usage(temp_dir) if temp_dir.exists() else 0
        conv_usage_before = get_disk_usage(conv_dir) if conv_dir.exists() else 0
        
        if base_folder_name:
            # Clean specific job folders
            temp_job_dir = temp_dir / f"{base_folder_name}_{job_id}"
            conv_job_dir = conv_dir / f"{base_folder_name}_{job_id}"
            
            if temp_job_dir.exists():
                import shutil
                shutil.rmtree(temp_job_dir)
                logger.info(f"Cleaned up temp job directory: {temp_job_dir}")
                
            if conv_job_dir.exists():
                import shutil
                shutil.rmtree(conv_job_dir)
                logger.info(f"Cleaned up conv job directory: {conv_job_dir}")
        else:
            # Clean all folders containing job_id
            for directory in [temp_dir, conv_dir]:
                if not directory.exists():
                    continue
                    
                for item in directory.iterdir():
                    if item.is_dir() and job_id in item.name:
                        import shutil
                        shutil.rmtree(item)
                        logger.info(f"Cleaned up job directory: {item}")
        
        # Log disk usage after cleanup
        temp_usage_after = get_disk_usage(temp_dir) if temp_dir.exists() else 0
        conv_usage_after = get_disk_usage(conv_dir) if conv_dir.exists() else 0
        
        temp_freed = temp_usage_before - temp_usage_after
        conv_freed = conv_usage_before - conv_usage_after
        
        logger.info(f"Disk space freed: temp={temp_freed:.1f}MB, conv={conv_freed:.1f}MB, total={temp_freed + conv_freed:.1f}MB")
                        
    except Exception as e:
        logger.error(f"Error cleaning up job files for {job_id}: {e}")

def cleanup_old_files(max_age_hours: int = 24):
    """
    Clean up old temporary files to save disk space.
    
    Args:
        max_age_hours: Maximum age of files in hours before cleanup
    """
    try:
        from pathlib import Path
        import time
        
        temp_dir = Path("temp")
        conv_dir = Path("conv")
        
        current_time = time.time()
        max_age_seconds = max_age_hours * 3600
        
        cleaned_files = 0
        freed_space = 0
        
        for directory in [temp_dir, conv_dir]:
            if not directory.exists():
                continue
                
            for item in directory.iterdir():
                if item.is_file():
                    file_age = current_time - item.stat().st_mtime
                    if file_age > max_age_seconds:
                        file_size = item.stat().st_size
                        item.unlink()
                        cleaned_files += 1
                        freed_space += file_size
                        logger.info(f"Cleaned up old file: {item}")
                elif item.is_dir():
                    # Check if directory is empty or old
                    try:
                        dir_age = current_time - item.stat().st_mtime
                        if dir_age > max_age_seconds:
                            import shutil
                            shutil.rmtree(item)
                            cleaned_files += 1
                            logger.info(f"Cleaned up old directory: {item}")
                    except Exception:
                        pass
        
        freed_space_mb = freed_space / 1024 / 1024
        logger.info(f"Cleanup completed: {cleaned_files} items removed, {freed_space_mb:.1f}MB freed")
        
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")