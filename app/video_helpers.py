

import numpy as np
import OpenEXR
import PyOpenColorIO as ocio
import Imath
from pathlib import Path
import subprocess

def convert_exr_folder_to_srgb(local_root: Path, conv_root: Path, ocio_config_path: str):
    """
    Converts all EXR files from ACEScg to sRGB using OCIO.

    Args:
        local_root (Path): folder with original EXR files.
        conv_root (Path): folder to save converted EXR files.
        ocio_config_path (str): path to OCIO config file.

    Raises:
        RuntimeError: if no EXR files are found or OCIO config is missing.
    """
    conv_root.mkdir(parents=True, exist_ok=True)
    exr_files = sorted([f for f in local_root.glob("*.exr") if "cryptomatte" not in f.name.lower()])
    if not exr_files:
        raise RuntimeError("No EXR frames found for conversion.")

    if not Path(ocio_config_path).exists():
        raise RuntimeError(f"OCIO config not found: {ocio_config_path}")
    config = ocio.Config.CreateFromFile(str(ocio_config_path))
    transform = ocio.DisplayViewTransform()
    transform.setSrc("ACEScg")
    transform.setDisplay("sRGB")
    transform.setView("ACES 1.0 SDR-video")
    transform.setDirection(ocio.TRANSFORM_DIR_FORWARD)
    processor = config.getProcessor(transform)
    cpu_processor = processor.getDefaultCPUProcessor()

    # Define dimensions based on the first file
    first_exr = exr_files[0]
    exr_file = OpenEXR.InputFile(str(first_exr))
    header = exr_file.header()
    dw = header["dataWindow"]
    width = dw.max.x - dw.min.x + 1
    height = dw.max.y - dw.min.y + 1

    FLOAT = Imath.PixelType(Imath.PixelType.FLOAT)
    for exr_path in exr_files:
        exr = OpenEXR.InputFile(str(exr_path))
        channels = exr.channels(["R", "G", "B"], FLOAT)
        r = np.frombuffer(channels[0], dtype=np.float32).reshape((height, width))
        g = np.frombuffer(channels[1], dtype=np.float32).reshape((height, width))
        b = np.frombuffer(channels[2], dtype=np.float32).reshape((height, width))
        rgb = np.stack([r, g, b], axis=-1)
        flat_image = rgb.reshape(-1, 3).astype(np.float32)

        img_desc = ocio.PackedImageDesc(flat_image, width, height, 3)
        cpu_processor.apply(img_desc)
        img = flat_image.reshape(height, width, 3)

        out_exr_path = conv_root / exr_path.name
        header_out = OpenEXR.Header(width, height)
        half_chan = Imath.Channel(Imath.PixelType(Imath.PixelType.HALF))
        header_out["channels"] = {"R": half_chan, "G": half_chan, "B": half_chan}
        out_exr = OpenEXR.OutputFile(str(out_exr_path), header_out)
        r_half = (img[:, :, 0].astype(np.float16)).tobytes()
        g_half = (img[:, :, 1].astype(np.float16)).tobytes()
        b_half = (img[:, :, 2].astype(np.float16)).tobytes()
        out_exr.writePixels({"R": r_half, "G": g_half, "B": b_half})
        out_exr.close()

def assemble_video_from_exr(conv_root: Path, exr_folder_name: str) -> Path:
    """
    Assembles MP4 from converted EXR files using ffmpeg.

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
    cmd = [
        "ffmpeg",
        "-y",
        "-pattern_type", "glob",
        "-i", exr_pattern,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        str(video_path)
    ]
    subprocess.run(cmd, check=True)
    return video_path