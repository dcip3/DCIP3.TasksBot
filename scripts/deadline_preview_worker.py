#!/usr/bin/env python3
"""
Helper script executed on Deadline workers to build preview videos with proper OCIO color management.

Steps:
1. Convert the EXR sequence with the OCIO display/view transform on CPU
   (parallel worker processes; renders are HDR ACEScg, so a 0-1 domain LUT
   would clip highlights — hence no LUT mode).
2. Invoke ffmpeg (NVENC when available) to encode the converted frames to MP4.

The script expects that PyOpenColorIO, OpenEXR and NumPy (indirectly via PyOpenColorIO) are installed
in the Python environment available on the worker. When the launched interpreter lacks them, the
script self-heals: it delegates to another local interpreter that has the modules, then tries an
unattended `pip install`, and as a last resort excludes this worker from the job's machine list and
requeues the current task so another worker picks it up.
"""

from __future__ import annotations

import argparse
import atexit
import http.client
import logging
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import contextlib
import urllib.parse
from pathlib import Path
from typing import Optional, List, Tuple, Set, Union


ACTIVE_TEMP_PATHS: Set[Path] = set()
_FFMPEG_ENCODERS_CACHE: dict[str, str] = {}


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


_BOOTSTRAP_ENV_FLAG = "PREVIEW_BOOTSTRAPPED"
# Bump when the color pipeline changes so previously built previews are not reused.
_COLOR_PIPELINE_VERSION = 2
_STUB_FALLBACK = (
    "import os,sys,base64,zlib,json;"
    "script=os.environ['PREVIEW_SCRIPT_B64'];"
    "argv=os.environ['PREVIEW_ARGV_B64'];"
    "sys.argv=json.loads(base64.b64decode(argv).decode('utf-8'));"
    "exec(zlib.decompress(base64.b64decode(script)))"
)


def _required_color_modules(args) -> List[str]:
    if args.disable_color:
        return []
    if Path(args.input_pattern).suffix.lower() != ".exr":
        return []
    return ["PyOpenColorIO", "OpenEXR", "Imath", "numpy", "PIL"]


def _missing_modules(modules: List[str]) -> List[str]:
    import importlib.util

    missing: List[str] = []
    for module in modules:
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(module)
        except (ImportError, ValueError):
            missing.append(module)
    return missing


def _iter_python_candidates() -> List[str]:
    """Collect other Python interpreters installed on this machine, best-effort."""
    candidates: List[str] = []
    seen: Set[str] = set()

    def _add(raw: Optional[str]) -> None:
        if not raw:
            return
        cleaned = raw.strip().strip('"')
        if not cleaned:
            return
        path = Path(cleaned)
        if not path.is_file():
            return
        try:
            key = os.path.normcase(str(path.resolve()))
        except OSError:
            key = os.path.normcase(cleaned)
        if key in seen:
            return
        seen.add(key)
        candidates.append(str(path))

    try:
        seen.add(os.path.normcase(str(Path(sys.executable).resolve())))
    except OSError:
        pass

    if os.name == "nt":
        py_launcher = shutil.which("py")
        if py_launcher:
            try:
                listing = subprocess.run(
                    [py_launcher, "-0p"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                output = (listing.stdout or "") + "\n" + (listing.stderr or "")
                for line in output.splitlines():
                    line = line.strip()
                    if not line.startswith("-"):
                        continue
                    parts = line.split(None, 1)
                    if len(parts) != 2:
                        continue
                    rest = parts[1].strip()
                    if rest.startswith("*"):
                        rest = rest[1:].strip()
                    _add(rest)
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            where_result = subprocess.run(
                ["where", "python", "python3"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            for line in (where_result.stdout or "").splitlines():
                _add(line)
        except (OSError, subprocess.SubprocessError):
            pass
        glob_roots: List[Path] = []
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            glob_roots.append(Path(local_appdata) / "Programs" / "Python")
            glob_roots.append(Path(local_appdata) / "Microsoft" / "WindowsApps")
        glob_roots.append(Path("C:/Program Files"))
        glob_roots.append(Path("C:/"))
        for root in glob_roots:
            try:
                for exe in root.glob("Python3*/python.exe"):
                    _add(str(exe))
                for exe in root.glob("python3*.exe"):
                    _add(str(exe))
            except OSError:
                continue
    else:
        for name in ("python3", "python"):
            _add(shutil.which(name))

    return candidates


def _probe_interpreter(executable: str, modules: List[str]) -> bool:
    probe = "import " + ", ".join(modules)
    try:
        result = subprocess.run(
            [executable, "-c", probe],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


_MODULE_PIP_PACKAGES = {
    "PyOpenColorIO": "opencolorio",
    "OpenEXR": "openexr",
    "Imath": "openexr",
    "numpy": "numpy",
    "PIL": "pillow",
}


def _delegation_command_tail() -> Optional[List[str]]:
    if os.environ.get("PREVIEW_SCRIPT_B64") and os.environ.get("PREVIEW_ARGV_B64"):
        stub = os.environ.get("PREVIEW_STUB") or _STUB_FALLBACK
        return ["-c", stub]
    script_path = globals().get("__file__")
    if script_path and Path(script_path).is_file():
        return [str(script_path)] + list(sys.argv[1:])
    return None


def _delegate_to_capable_python(modules: List[str]) -> Optional[int]:
    """Re-run the preview under another local interpreter that has the modules.

    Returns the delegate's exit code, or None when no capable interpreter was found.
    """
    tail = _delegation_command_tail()
    if tail is None:
        return None

    checked: List[str] = []
    for candidate in _iter_python_candidates():
        checked.append(candidate)
        if not _probe_interpreter(candidate, modules):
            continue
        logging.warning("Delegating preview render to %s", candidate)
        env = dict(os.environ)
        env[_BOOTSTRAP_ENV_FLAG] = "1"
        try:
            completed = subprocess.run([candidate] + tail, env=env)
        except OSError as exc:
            logging.error("Failed to start %s: %s", candidate, exc)
            continue
        return completed.returncode

    logging.warning(
        "No installed Python interpreter has the required modules (%s); checked: %s",
        ", ".join(modules),
        ", ".join(checked) or "none",
    )
    return None


def _auto_install_missing_packages(missing: List[str]) -> bool:
    """Install the pip packages that provide the missing modules, unattended."""
    packages: List[str] = []
    for module in missing:
        package = _MODULE_PIP_PACKAGES.get(module)
        if package and package not in packages:
            packages.append(package)
    if not packages:
        return False

    for extra_args in ([], ["--user"]):
        command = [sys.executable, "-m", "pip", "install", "--upgrade"] + extra_args + packages
        logging.warning("Attempting automatic package installation: %s", " ".join(command))
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.SubprocessError) as exc:
            logging.error("pip could not be launched: %s", exc)
            return False
        if result.returncode == 0:
            logging.warning("Automatic package installation succeeded")
            return True
        logging.error(
            "pip install failed (exit %s): %s",
            result.returncode,
            (result.stderr or result.stdout or "").strip()[-2000:],
        )
    return False


def _find_deadline_command() -> Optional[str]:
    exe_name = "deadlinecommand.exe" if os.name == "nt" else "deadlinecommand"
    candidates: List[Path] = []
    deadline_path = os.environ.get("DEADLINE_PATH")
    if deadline_path:
        candidates.append(Path(deadline_path) / exe_name)
    located = shutil.which("deadlinecommand")
    if located:
        candidates.append(Path(located))
    if os.name == "nt":
        candidates.append(Path(r"C:\Program Files\Thinkbox\Deadline10\bin") / exe_name)
    else:
        candidates.append(Path("/opt/Thinkbox/Deadline10/bin") / exe_name)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _run_deadline_command(executable: str, arguments: List[str]) -> Tuple[int, str]:
    try:
        result = subprocess.run(
            [executable] + arguments,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _parse_key_values(output: str) -> dict:
    values: dict = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in values:
            values[key] = value.strip()
    return values


def _local_worker_names() -> List[str]:
    if os.name == "nt":
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        workers_root = Path(program_data) / "Thinkbox" / "Deadline10" / "workers"
    else:
        workers_root = Path("/var/lib/Thinkbox/Deadline10/workers")

    names: List[str] = []
    try:
        for entry in workers_root.iterdir():
            if entry.is_dir():
                names.append(entry.name)
    except OSError:
        pass
    if not names:
        import socket

        names.append(socket.gethostname().lower())
    return names


def _requeue_preview_task_elsewhere() -> bool:
    """Exclude this worker from the preview job's machine list and requeue its task.

    Only the current task is requeued (not the whole job), so any frames rendered by
    other workers are unaffected. Returns True when the requeue succeeded; the Deadline
    Worker is then expected to abort this process shortly.
    """
    if not (os.environ.get("PREVIEW_SCRIPT_B64") and os.environ.get("PREVIEW_ARGV_B64")):
        return False  # not launched as a Deadline preview job; leave the farm alone

    deadline_command = _find_deadline_command()
    if not deadline_command:
        logging.warning("deadlinecommand was not found; cannot hand the task to another worker")
        return False

    for worker_name in _local_worker_names():
        code, output = _run_deadline_command(deadline_command, ["GetSlave", worker_name])
        if code != 0:
            continue
        worker_info = _parse_key_values(output)
        job_id = worker_info.get("CurrentJobId", "")
        if not job_id:
            continue

        job_code, job_output = _run_deadline_command(deadline_command, ["GetJob", job_id])
        if job_code != 0 or not job_output.strip():
            continue
        job_info = _parse_key_values(job_output)
        job_name = job_info.get("Name", "")
        if "PreviewJob=1" not in job_output and "- Preview" not in job_name:
            logging.warning("Current job %s does not look like a preview job; leaving it alone", job_id)
            continue

        whitelisted = (job_info.get("WhitelistFlag") or "").strip().lower() == "true"
        if whitelisted:
            list_command = ["RemoveSlavesFromJobMachineLimitList", job_id, worker_name]
        else:
            list_command = ["AddSlavesToJobMachineLimitList", job_id, worker_name]
        list_code, list_output = _run_deadline_command(deadline_command, list_command)
        if list_code != 0:
            logging.error(
                "Failed to exclude worker %s from job %s: %s",
                worker_name,
                job_id,
                list_output.strip(),
            )
            continue
        logging.warning(
            "Worker %s excluded from preview job %s (%s)",
            worker_name,
            job_id,
            "removed from allow list" if whitelisted else "added to deny list",
        )

        task_ids = worker_info.get("CurrentTaskIds", "").strip() or "0"
        requeue_code, requeue_output = _run_deadline_command(
            deadline_command, ["RequeueJobTasks", job_id, task_ids]
        )
        if requeue_code != 0:
            logging.error(
                "Failed to requeue task(s) %s of job %s: %s",
                task_ids,
                job_id,
                requeue_output.strip(),
            )
            return False
        logging.warning("Requeued task(s) %s of preview job %s for another worker", task_ids, job_id)
        return True

    logging.warning("Could not determine which Deadline job this process belongs to")
    return False


def _ensure_color_runtime(args) -> Optional[int]:
    """Make sure the color-transform modules are available before rendering.

    The Deadline job launches whatever `py`/`python` resolves to on the worker, which
    may change whenever Python is installed or upgraded. When modules are missing,
    escalate through: delegate to another local interpreter -> pip install the
    packages unattended -> exclude this worker from the job and requeue the task.

    Returns an exit code when the render was delegated, or None when execution
    should continue in-process.
    """
    if os.environ.get(_BOOTSTRAP_ENV_FLAG):
        return None
    modules = _required_color_modules(args)
    if not modules:
        return None
    missing = _missing_modules(modules)
    if not missing:
        return None

    logging.warning(
        "Python %s is missing modules required for the preview color transform (%s); "
        "searching this machine for a capable interpreter",
        sys.executable,
        ", ".join(missing),
    )

    delegated_exit = _delegate_to_capable_python(modules)
    if delegated_exit is not None:
        return delegated_exit

    if _auto_install_missing_packages(missing):
        import importlib

        importlib.invalidate_caches()
        if not _missing_modules(modules):
            logging.warning("Continuing preview render after automatic package installation")
            return None
        logging.error("Packages were installed but the modules are still not importable")

    if _requeue_preview_task_elsewhere():
        # The Deadline Worker aborts this process once its task is requeued; wait for
        # that instead of racing it to an error report.
        logging.warning("Waiting for the Deadline Worker to stop this process...")
        time.sleep(60)

    logging.error(
        "No usable Python environment for the preview color transform was found on this "
        "machine (missing: %s). Run scripts/worker_setup.ps1 on this worker to install "
        "the required packages.",
        ", ".join(missing),
    )
    return None


def _wait_for_color_sidecar(
    input_pattern: str,
    newest_input_mtime: Optional[float],
    *,
    base_wait: float = 15.0,
    fresh_wait: float = 90.0,
    poll_interval: float = 3.0,
) -> None:
    """Give the sync client a moment to deliver preview_color.json.

    The sidecar is written by the render plugin on the render machine; when the
    preview starts on another machine seconds after the render completes, the
    tiny json may still be in flight. Renders finished within the last 30
    minutes get a longer grace period; legacy renders without a sidecar only
    cost the short base wait.
    """
    sidecar_path = Path(input_pattern).parent / "preview_color.json"
    if sidecar_path.exists():
        return

    wait_seconds = base_wait
    if newest_input_mtime is not None and (time.time() - newest_input_mtime) < 30 * 60:
        wait_seconds = fresh_wait

    logging.info(
        "Color sidecar not present yet; waiting up to %.0fs for it to sync",
        wait_seconds,
    )
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if sidecar_path.exists():
            logging.info("Color sidecar appeared; continuing")
            return
        time.sleep(poll_interval)
    logging.info("Color sidecar did not appear; building the preview without a camera LUT")


def _load_color_sidecar(input_pattern: str) -> Optional[dict]:
    """Load the render's preview_color.json (written by the farm's Houdini plugin)."""
    try:
        sidecar_path = Path(input_pattern).parent / "preview_color.json"
        if not sidecar_path.is_file():
            return None
        import json

        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.warning("Could not read preview color sidecar: %s", exc)
        return None
    return data if isinstance(data, dict) else None


def _resolve_lut_path(raw_path: str) -> Optional[Path]:
    """Locate the camera LUT on THIS machine.

    The sidecar records the path from the machine that rendered (often inside
    its Redshift installation, e.g. C:/Program Files/Maxon Redshift .../Data/LUT/...),
    while the previewing machine may have Redshift installed elsewhere.
    """
    if not raw_path:
        return None
    normalized = raw_path.replace("\\", "/")
    candidate = Path(normalized)
    if candidate.is_file():
        return candidate

    suffix: Optional[str] = None
    marker = "/Data/"
    idx = normalized.find(marker)
    if idx != -1:
        suffix = normalized[idx + len(marker):]

    roots: List[Path] = []
    for env_name in ("REDSHIFT_COREDATAPATH", "REDSHIFT_LOCALDATAPATH"):
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            roots.append(Path(env_value))
    if os.name == "nt":
        for base in (Path("C:/Program Files"), Path("C:/ProgramData")):
            try:
                roots.extend(entry for entry in base.glob("*Redshift*") if entry.is_dir())
            except OSError:
                pass
    else:
        roots.append(Path("/usr/redshift"))

    for root in roots:
        for data_dir in (root / "Data", root):
            if suffix:
                resolved = data_dir / suffix
                if resolved.is_file():
                    return resolved

    # Last resort: search by file name under the local Redshift LUT libraries.
    lut_name = Path(normalized).name
    for root in roots:
        for lut_dir in (root / "Data" / "LUT", root / "LUT"):
            if not lut_dir.is_dir():
                continue
            try:
                found = next(lut_dir.rglob(lut_name), None)
            except OSError:
                found = None
            if found is not None:
                return found
    return None


def _parse_curve_points(raw: object) -> Optional[List[Tuple[float, float]]]:
    """Parse a Redshift curve string: "<count> x0 y0 x1 y1 ...".

    Returns None for identity curves (nothing to apply).
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    tokens = raw.split()
    try:
        values = [float(token) for token in tokens]
    except ValueError:
        return None
    if len(values) < 3:
        return None

    count = int(values[0])
    coords = values[1:]
    if count < 2 or len(coords) < count * 2:
        return None

    points = [(coords[i * 2], coords[i * 2 + 1]) for i in range(count)]
    points.sort(key=lambda point: point[0])
    if len(points) == 2 and all(
        abs(point[0] - point[1]) < 1e-6 for point in points
    ) and abs(points[0][0]) < 1e-6 and abs(points[1][0] - 1.0) < 1e-6:
        return None  # straight 0->0, 1->1 line
    return points


def _sample_curve(points: List[Tuple[float, float]], xs):
    """Evaluate a curve at xs with monotone cubic (PCHIP) interpolation.

    Monotone interpolation matches how curve widgets behave (smooth, but never
    overshooting between control points).
    """
    import numpy as np

    px = np.array([point[0] for point in points], dtype=np.float64)
    py = np.array([point[1] for point in points], dtype=np.float64)
    n = len(px)
    if n < 2:
        return np.clip(xs, 0.0, 1.0)

    h = np.diff(px)
    h[h == 0] = 1e-9
    delta = np.diff(py) / h

    slopes = np.zeros(n, dtype=np.float64)
    slopes[0] = delta[0]
    slopes[-1] = delta[-1]
    for i in range(1, n - 1):
        if delta[i - 1] * delta[i] <= 0:
            slopes[i] = 0.0
        else:
            w1 = 2 * h[i] + h[i - 1]
            w2 = h[i] + 2 * h[i - 1]
            slopes[i] = (w1 + w2) / (w1 / delta[i - 1] + w2 / delta[i])

    xs = np.asarray(xs, dtype=np.float64)
    idx = np.clip(np.searchsorted(px, xs, side="right") - 1, 0, n - 2)
    x0 = px[idx]
    dx = xs - x0
    hh = h[idx]
    t = np.clip(dx / hh, 0.0, 1.0)
    t2 = t * t
    t3 = t2 * t
    h00 = 2 * t3 - 3 * t2 + 1
    h10 = t3 - 2 * t2 + t
    h01 = -2 * t3 + 3 * t2
    h11 = t3 - t2
    result = (
        h00 * py[idx]
        + h10 * hh * slopes[idx]
        + h01 * py[idx + 1]
        + h11 * hh * slopes[idx + 1]
    )
    # Outside the control-point range the curve holds its endpoints.
    result = np.where(xs <= px[0], py[0], result)
    result = np.where(xs >= px[-1], py[-1], result)
    return np.clip(result, 0.0, 1.0)


def _extract_color_controls(sidecar: Optional[dict]) -> Optional[dict]:
    """Read the camera's Color Controls section (contrast + RGB curves).

    Returns None when the section is disabled or fully neutral.
    """
    if not sidecar:
        return None
    params = sidecar.get("camera_params")
    if not isinstance(params, dict):
        return None
    if not params.get("RS_campro_colorEnable"):
        return None

    try:
        contrast = float(params.get("RS_campro_colorContrast", 0.0) or 0.0)
    except (TypeError, ValueError):
        contrast = 0.0
    contrast = max(-1.0, min(1.0, contrast))

    curves = {}
    for key, parm_name in (
        ("rgb", "RS_campro_colorCurvesRGB"),
        ("r", "RS_campro_colorCurvesR"),
        ("g", "RS_campro_colorCurvesG"),
        ("b", "RS_campro_colorCurvesB"),
    ):
        points = _parse_curve_points(params.get(parm_name))
        if points:
            curves[key] = points

    if abs(contrast) < 1e-6 and not curves:
        return None

    return {"contrast": contrast, "curves": curves}


def _build_color_controls_transform(controls: dict, ocio):
    """Bake contrast + curves into a single 1D LUT (display-referred, 0..1)."""
    import numpy as np

    size = 1024
    xs = np.linspace(0.0, 1.0, size, dtype=np.float64)

    contrast = float(controls.get("contrast") or 0.0)
    if abs(contrast) > 1e-6:
        # Linear contrast around mid-grey; RS exposes -1..1 with 0 as neutral.
        base = np.clip((xs - 0.5) * (1.0 + contrast) + 0.5, 0.0, 1.0)
    else:
        base = xs

    curves = controls.get("curves") or {}
    channels = []
    for key in ("r", "g", "b"):
        values = base
        if key in curves:
            values = _sample_curve(curves[key], values)
        if "rgb" in curves:
            values = _sample_curve(curves["rgb"], values)
        channels.append(values)

    lut = ocio.Lut1DTransform(length=size)
    for index in range(size):
        lut.setValue(
            index,
            float(channels[0][index]),
            float(channels[1][index]),
            float(channels[2][index]),
        )
    return lut


def _extract_lut_spec(sidecar: Optional[dict]) -> Optional[dict]:
    """Normalize the sidecar into a LUT spec for the OCIO pipeline, or None."""
    if not sidecar:
        return None
    lut = sidecar.get("lut")
    if not isinstance(lut, dict) or not lut.get("enabled"):
        return None
    raw_file = str(lut.get("file") or "").strip()
    if not raw_file:
        return None
    try:
        strength = float(lut.get("strength", 1.0))
    except (TypeError, ValueError):
        strength = 1.0
    if strength <= 0:
        return None

    resolved = _resolve_lut_path(raw_file)
    if resolved is None:
        logging.warning(
            "Camera LUT %s was not found on this machine; building the preview without it",
            raw_file,
        )
        return None

    # Redshift semantics ("Apply Color Management before LUT"): when the flag is
    # on, the display transform runs FIRST and the LUT sees the display-referred
    # image. Older sidecars stored the same parm under the misleading key
    # "before_cm"; both keys carry the RS_campro_lutBeforeCM value.
    cm_before_lut_raw = lut.get("cm_before_lut", lut.get("before_cm"))
    return {
        "file": str(resolved),
        "strength": min(1.0, strength),
        "is_log": bool(lut.get("is_log")),
        "cm_before_lut": bool(cm_before_lut_raw),
    }


def _output_already_has_color(sidecar: Optional[dict], is_hdr_input: bool) -> bool:
    """True when Redshift already baked color management/LUT/controls into the frames.

    The ROP's "Color Management and Post Effects" section has a per-output-type
    "Color/LUT/Controls" toggle. For LDR outputs (PNG/JPG) it is on by default —
    those frames are already display-referred. For HDR (EXR) it is off by
    default, which is why the preview applies the view transform itself; if a
    scene turns it on, applying it again would double up.
    """
    output = (sidecar or {}).get("output")
    if not isinstance(output, dict):
        # Sidecar v1 (or none): assume Redshift's defaults.
        return not is_hdr_input
    key = "hdr_color" if is_hdr_input else "ldr_color"
    return bool(output.get(key, not is_hdr_input))


def _extract_camera_color_spec(sidecar: Optional[dict]) -> Optional[dict]:
    """Collect every camera-side color post effect the preview reproduces.

    Returns {"lut": <lut spec or None>, "controls": <controls or None>} or None
    when the camera contributes nothing.
    """
    lut = _extract_lut_spec(sidecar)
    controls = _extract_color_controls(sidecar)
    if not lut and not controls:
        return None
    return {"lut": lut, "controls": controls}


class _ColorPipeline:
    """Fused OCIO evaluation, optionally blending in a camera LUT by strength."""

    def __init__(self, primary, base=None, strength: float = 1.0):
        self._primary = primary
        self._base = base
        self._strength = strength

    def apply_frame(self, flat_rgb_f32, width: int, height: int, out_uint8) -> None:
        import numpy as np
        import PyOpenColorIO as ocio

        def _run(processor, destination):
            src = ocio.PackedImageDesc(flat_rgb_f32, width, height, 3)
            dst = ocio.PackedImageDesc(
                destination,
                width,
                height,
                3,
                ocio.BIT_DEPTH_UINT8,
                ocio.AutoStride,
                ocio.AutoStride,
                ocio.AutoStride,
            )
            processor.apply(src, dst)

        _run(self._primary, out_uint8)
        if self._base is None:
            return
        base = np.empty_like(out_uint8)
        _run(self._base, base)
        blended = (
            out_uint8.astype(np.float32) * self._strength
            + base.astype(np.float32) * (1.0 - self._strength)
        )
        np.copyto(out_uint8, (blended + 0.5).astype(np.uint8))


def _load_cpu_processor(
    config_path: Path,
    input_space: str,
    display: str,
    view: str,
    color_spec: Optional[dict] = None,
) -> "_ColorPipeline":
    """Build the fused OCIO pipeline: view transform + camera color post effects.

    Chain (matching Redshift's camera post FX order):
      scene-linear -> display/view transform -> Color Controls -> camera LUT
    With "Apply Color Management before LUT" off, the LUT instead runs on the
    scene-referred image before the view transform.
    """
    try:
        import PyOpenColorIO as ocio
    except ImportError as exc:  # pragma: no cover - depends on worker environment
        raise RuntimeError("PyOpenColorIO is required for CPU color mode") from exc

    config = ocio.Config.CreateFromFile(str(config_path))
    color_spec = color_spec or {}
    lut_spec = color_spec.get("lut")
    controls = color_spec.get("controls")

    def _fused(transform):
        processor = config.getProcessor(transform)
        # Fused F32 -> UINT8 evaluation: quantization happens inside OCIO, so a
        # separate clip/convert pass per frame is not needed (verified bit-identical).
        cpu_processor = processor.getOptimizedCPUProcessor(
            ocio.BIT_DEPTH_F32,
            ocio.BIT_DEPTH_UINT8,
            ocio.OPTIMIZATION_DEFAULT,
        )
        if cpu_processor is None:
            raise RuntimeError("Failed to create OCIO CPU processor")
        return cpu_processor

    def _display_transform():
        transform = ocio.DisplayViewTransform()
        transform.setSrc(input_space)
        transform.setDisplay(display)
        transform.setView(view)
        transform.setDirection(ocio.TRANSFORM_DIR_FORWARD)
        return transform

    def _append_display_and_controls(group) -> None:
        group.appendTransform(_display_transform())
        if controls:
            # Contrast and RGB curves operate on the display-referred image.
            group.appendTransform(_build_color_controls_transform(controls, ocio))

    if not lut_spec:
        if not controls:
            return _ColorPipeline(_fused(_display_transform()))
        group = ocio.GroupTransform()
        _append_display_and_controls(group)
        return _ColorPipeline(_fused(group))

    file_transform = ocio.FileTransform()
    file_transform.setSrc(lut_spec["file"])
    file_transform.setInterpolation(ocio.INTERP_TETRAHEDRAL)

    group = ocio.GroupTransform()
    if lut_spec.get("cm_before_lut"):
        # RS "Apply Color Management before LUT": the LUT is applied to the
        # display-referred image produced by the view transform.
        if lut_spec.get("is_log"):
            logging.warning(
                "Camera LUT has both 'CM before LUT' and log mode set; ignoring log mode"
            )
        _append_display_and_controls(group)
        group.appendTransform(file_transform)
    else:
        # LUT is applied to the scene-referred image before color management.
        if lut_spec.get("is_log"):
            # The LUT expects log-encoded input: shape linear data through
            # ACEScct around it (best available match to Redshift's log mode).
            try:
                to_log = ocio.ColorSpaceTransform()
                to_log.setSrc(input_space)
                to_log.setDst("ACEScct")
                from_log = ocio.ColorSpaceTransform()
                from_log.setSrc("ACEScct")
                from_log.setDst(input_space)
                group.appendTransform(to_log)
                group.appendTransform(file_transform)
                group.appendTransform(from_log)
            except Exception as log_error:
                logging.warning(
                    "Could not build log shaper for camera LUT (%s); applying it linearly",
                    log_error,
                )
                group.appendTransform(file_transform)
        else:
            group.appendTransform(file_transform)
        _append_display_and_controls(group)

    primary = _fused(group)
    if lut_spec.get("strength", 1.0) >= 1.0:
        return _ColorPipeline(primary)

    # LUT strength blends against the same image without the LUT, so the color
    # controls stay fully applied on both sides of the blend.
    base_group = ocio.GroupTransform()
    _append_display_and_controls(base_group)
    return _ColorPipeline(
        primary,
        base=_fused(base_group),
        strength=float(lut_spec["strength"]),
    )


def _convert_exr_frame_cpu(
    exr_path: Path,
    output_path: Path,
    pipeline: "_ColorPipeline",
):
    """Convert one EXR frame to PNG/BMP (by output suffix) via the OCIO color pipeline."""
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
        r = np.frombuffer(channels[0], dtype=np.float32).reshape(height, width)
        g = np.frombuffer(channels[1], dtype=np.float32).reshape(height, width)
        b = np.frombuffer(channels[2], dtype=np.float32).reshape(height, width)

        flat = np.stack([r, g, b], axis=-1).reshape(-1, 3)
        img8 = np.empty((height, width, 3), dtype=np.uint8)
        pipeline.apply_frame(flat, width, height, img8)

        pil_img = Image.fromarray(img8, mode="RGB")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.suffix.lower() == ".png":
            # PNG deliverables (stills); keep compression cheap.
            pil_img.save(str(output_path), "PNG", compress_level=1)
        else:
            # Transient ffmpeg inputs: BMP writes ~15x faster than PNG.
            pil_img.save(str(output_path))
    finally:
        try:
            exr.close()
        except Exception:
            pass


def _run_cpu_convert_manifest(manifest_path: str) -> int:
    """Child-process mode: convert the frames listed in a JSON manifest."""
    import json

    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    pipeline = _load_cpu_processor(
        Path(data["config"]),
        data["input_space"],
        data["display"],
        data["view"],
        color_spec=data.get("color"),
    )
    for source, destination in data["files"]:
        _convert_exr_frame_cpu(Path(source), Path(destination), pipeline)
    return 0


def _self_invocation_command(argv_tail: List[str], env: dict) -> Optional[List[str]]:
    """Build a command that re-runs this script with different arguments.

    Mutates env so the Deadline stub (which takes sys.argv from PREVIEW_ARGV_B64)
    picks up argv_tail as well.
    """
    import base64
    import json

    if os.environ.get("PREVIEW_SCRIPT_B64") and os.environ.get("PREVIEW_ARGV_B64"):
        stub = os.environ.get("PREVIEW_STUB") or _STUB_FALLBACK
        argv_json = json.dumps(["deadline_preview_worker.py"] + argv_tail)
        env["PREVIEW_ARGV_B64"] = base64.b64encode(argv_json.encode("utf-8")).decode("ascii")
        return [sys.executable, "-c", stub]
    script_path = globals().get("__file__")
    if script_path and Path(script_path).is_file():
        return [sys.executable, str(script_path)] + argv_tail
    return None


def _convert_frames_cpu(
    conversions: List[Tuple[Path, Path]],
    *,
    config_path: Path,
    input_space: str,
    display: str,
    view: str,
    shard_dir: Path,
    worker_processes: int,
    color_spec: Optional[dict] = None,
) -> None:
    """Convert EXR frames to PNG, fanning out across worker subprocesses.

    Falls back to sequential in-process conversion when subprocesses cannot be
    spawned or for whatever frames the shards did not produce.
    """
    import json

    if worker_processes <= 0:
        worker_processes = min(16, max(1, (os.cpu_count() or 4) - 2))
    worker_processes = min(worker_processes, len(conversions))

    if worker_processes > 1:
        shards: List[List[Tuple[Path, Path]]] = [[] for _ in range(worker_processes)]
        for index, conversion in enumerate(conversions):
            shards[index % worker_processes].append(conversion)

        processes = []
        for shard_index, shard in enumerate(shards):
            manifest = shard_dir / f"cpu_shard_{shard_index}.json"
            manifest.write_text(
                json.dumps(
                    {
                        "config": str(config_path),
                        "input_space": input_space,
                        "display": display,
                        "view": view,
                        "color": color_spec,
                        "files": [[str(src), str(dst)] for src, dst in shard],
                    }
                ),
                encoding="utf-8",
            )
            env = dict(os.environ)
            env[_BOOTSTRAP_ENV_FLAG] = "1"
            command = _self_invocation_command(["--cpu-convert-manifest", str(manifest)], env)
            if command is None:
                break
            try:
                processes.append(
                    subprocess.Popen(
                        command,
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                )
            except OSError as exc:
                logging.warning("Could not spawn conversion shard: %s", exc)
                break

        if processes:
            logging.info(
                "Converting %s frames in %s parallel processes",
                len(conversions),
                len(processes),
            )
        for process in processes:
            output, _ = process.communicate()
            if process.returncode != 0:
                logging.warning(
                    "Conversion shard exited with %s: %s",
                    process.returncode,
                    (output or "").strip()[-1000:],
                )

    # Sequential pass over anything the shards did not produce (or everything,
    # when parallel execution was unavailable). Idempotent and order-independent.
    remaining = [(src, dst) for src, dst in conversions if not dst.exists()]
    if remaining:
        if len(remaining) < len(conversions):
            logging.warning(
                "Re-converting %s frames the parallel shards did not produce",
                len(remaining),
            )
        pipeline = _load_cpu_processor(
            config_path, input_space, display, view, color_spec=color_spec
        )
        for source, destination in remaining:
            _convert_exr_frame_cpu(source, destination, pipeline)


def _scan_sequence_count(pattern: str) -> int:
    """Count files on disk matching a %0Nd-style sequence pattern."""
    pattern_path = Path(pattern)
    directory = pattern_path.parent
    template = pattern_path.name
    match = re.search(r"%0(\d+)d", template)
    if not match:
        return 1 if pattern_path.exists() else 0
    digits = int(match.group(1))
    glob_pattern = re.sub(r"%0\d+d", "?" * digits, template)
    try:
        return sum(1 for _ in directory.glob(glob_pattern))
    except OSError as exc:
        logging.warning("Failed to scan sequence directory %s: %s", directory, exc)
        return 0


def _has_sequence_placeholder(pattern: str) -> bool:
    return re.search(r"%0\d+d", Path(pattern).name) is not None


_CLOUD_PLACEHOLDER_ATTRIBUTES = 0x00400000 | 0x00040000 | 0x00001000
# FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_OFFLINE


def _needs_hydration(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        attributes = os.stat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attributes & _CLOUD_PLACEHOLDER_ATTRIBUTES)


def _hydrate_file(path: Path) -> bool:
    """Force the sync client to download a cloud placeholder by reading it fully."""
    try:
        with open(path, "rb") as handle:
            while handle.read(8 * 1024 * 1024):
                pass
        return True
    except OSError as exc:
        logging.warning("Could not prefetch %s: %s", path, exc)
        return False


def _prefetch_input_files(files: List[Path], max_threads: int = 16) -> None:
    """Hydrate cloud-backed (online-only) input frames in parallel.

    Frames rendered by other machines arrive as online-only placeholders; without
    prefetch each frame download starts serially on its first read during
    conversion or encoding.
    """
    pending = [path for path in files if _needs_hydration(path)]
    if not pending:
        return

    from concurrent.futures import ThreadPoolExecutor

    logging.info("Prefetching %s cloud-backed input frames...", len(pending))
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=min(max_threads, len(pending))) as pool:
        hydrated = sum(1 for ok in pool.map(_hydrate_file, pending) if ok)
    logging.info(
        "Prefetched %s/%s frames in %.1fs",
        hydrated,
        len(pending),
        time.monotonic() - started,
    )


def _wait_for_input_frames(
    pattern: str,
    expected_frames: int,
    timeout_seconds: float,
    poll_interval: float = 3.0,
) -> int:
    """Poll the input directory until the expected frame count appears or the timeout expires."""
    if expected_frames <= 0:
        return _scan_sequence_count(pattern)

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    last_count = -1
    while True:
        found = _scan_sequence_count(pattern)
        if found != last_count:
            logging.info(
                "Input sequence scan: found %s/%s frames",
                found,
                expected_frames,
            )
            last_count = found
        if found >= expected_frames:
            return found
        if time.monotonic() >= deadline:
            return found
        time.sleep(poll_interval)


def _probe_frame_count(
    ffprobe_path: str,
    output_path: Path,
    *,
    count_frames: bool,
) -> Tuple[Optional[int], str]:
    """Query the video frame count; metadata lookup unless count_frames decoding is forced."""
    command = [ffprobe_path, "-v", "error"]
    if count_frames:
        command.append("-count_frames")
    command.extend(
        [
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames" if count_frames else "stream=nb_frames",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(output_path),
        ]
    )
    probe = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    stderr = (probe.stderr or "").strip().splitlines()
    error_snippet = stderr[-1] if stderr else "unreadable stream"
    if probe.returncode != 0:
        return None, error_snippet
    for line in (probe.stdout or "").strip().splitlines():
        token = line.strip()
        if not token or token.upper() in {"N/A", "NA"}:
            continue
        try:
            return int(token), ""
        except ValueError:
            continue
    return None, error_snippet


def _color_signature(color_spec: Optional[dict]) -> str:
    """Stable tag describing how this preview was color-processed.

    Written into the MP4 as a comment so a later run can tell whether an
    existing file was built with the same color pipeline (bumping
    _COLOR_PIPELINE_VERSION invalidates every previously built preview).
    """
    import hashlib
    import json

    payload = json.dumps(color_spec or {}, sort_keys=True, default=str)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"tasksbot:{_COLOR_PIPELINE_VERSION}:{digest}"


def _read_output_signature(output_path: Path, ffmpeg_path: str) -> Optional[str]:
    ffprobe_path = _resolve_ffprobe_path(ffmpeg_path)
    try:
        probe = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format_tags=comment",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(output_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return None
    if probe.returncode != 0:
        return None
    value = (probe.stdout or "").strip()
    return value or None


# Renders are delivered at whatever size the shot needs, and a preview at that
# size is not always a video anything can play: Telegram handed back one at
# 3556x2404 that downloaded fine and refused to play in the chat. Fitting the
# preview inside a box keeps it decodable everywhere, and smaller to send.
DEFAULT_MAX_DIMENSION = 1920


def _reusable_existing_output(
    output_path: Path,
    input_files: List[Path],
    ffmpeg_path: str,
    expected_frames: int,
    color_signature: Optional[str] = None,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
) -> bool:
    """True when a previous run already produced this preview from the same frames.

    Happens when a worker converted successfully but failed to upload and the
    task moved to another machine. Guards against stale files from earlier
    render versions by requiring the video to be newer than every input frame
    (mtimes survive the file sync) and to carry the same color-pipeline
    signature - and against files built before the frame size was capped,
    which are the ones Telegram will not play.
    """
    try:
        if not input_files or not output_path.exists():
            return False
        out_stat = output_path.stat()
        if out_stat.st_size < 1024:
            return False
        newest_frame = max(frame.stat().st_mtime for frame in input_files)
        if out_stat.st_mtime <= newest_frame:
            return False
    except OSError:
        return False

    if color_signature:
        existing = _read_output_signature(output_path, ffmpeg_path)
        if existing != color_signature:
            logging.info(
                "Existing preview was built with a different color pipeline "
                "(%s != %s); rebuilding",
                existing or "no signature",
                color_signature,
            )
            return False

    if max_dimension and max_dimension > 0:
        resolution = _probe_output_resolution(output_path, ffmpeg_path)
        sides = [int(part) for part in str(resolution or "").split("x") if part.isdigit()]
        if sides and max(sides) > max_dimension:
            logging.info(
                "Existing preview is %s, larger than the %s-pixel limit; rebuilding",
                resolution,
                max_dimension,
            )
            return False

    valid, reason = _validate_preview_output(output_path, ffmpeg_path, expected_frames)
    if valid:
        logging.info("Existing preview output validated for reuse: %s", reason)
        return True
    logging.info("Existing preview output not reusable: %s", reason)
    return False


def _validate_preview_output(
    output_path: Path,
    ffmpeg_path: str,
    expected_frames: int,
) -> Tuple[bool, str]:
    """Verify that the encoded preview is decodable and approximately matches the input length."""
    if not output_path.exists():
        return False, "output file is missing"

    size_bytes = output_path.stat().st_size
    if size_bytes < 1024:
        return False, f"output too small ({size_bytes} bytes)"

    ffprobe_path = _resolve_ffprobe_path(ffmpeg_path)
    try:
        # Container metadata is instant; fall back to a full decode only when
        # the metadata does not carry a usable frame count.
        nb_read, probe_error = _probe_frame_count(
            ffprobe_path, output_path, count_frames=False
        )
        if nb_read is None or nb_read <= 0:
            nb_read, probe_error = _probe_frame_count(
                ffprobe_path, output_path, count_frames=True
            )
    except Exception as exc:
        logging.warning("ffprobe validation could not run: %s", exc)
        return True, "ffprobe unavailable; skipping validation"

    if nb_read is None or nb_read == 0:
        return False, f"ffprobe could not decode output: {probe_error}"

    if expected_frames > 0:
        # Allow a small slack: some Deadline jobs render with frame steps the
        # parser cannot infer, so we only fail when the gap is clearly wrong.
        minimum_acceptable = max(1, int(expected_frames * 0.5))
        if nb_read < minimum_acceptable:
            return False, (
                f"decoded {nb_read} frames, expected ~{expected_frames}"
            )

    return True, f"validated ({nb_read} frames, {size_bytes} bytes)"


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


def _quote_concat_path(path: Path) -> str:
    # ffmpeg concat manifests use backslash as an escape character, so normalize
    # Windows paths to forward slashes before single-quoting them.
    try:
        absolute_path = path.resolve()
    except OSError:
        absolute_path = path.absolute()
    normalized = str(absolute_path).replace("\\", "/")
    return "'" + normalized.replace("'", r"'\''") + "'"


def _create_concat_manifest(
    files: List[Path],
    frame_rate: float,
    temp_dir: Path,
) -> Tuple[Path, tempfile.TemporaryDirectory[str]]:
    if not files:
        raise RuntimeError("Cannot build preview concat manifest without frames")

    temp_dir_obj = tempfile.TemporaryDirectory(prefix="preview_concat_", dir=str(temp_dir))
    manifest_dir = Path(temp_dir_obj.name)
    _register_temp_path(manifest_dir)
    manifest_path = manifest_dir / "frames.ffconcat"
    duration = 1.0 / max(float(frame_rate), 0.001)
    lines = ["ffconcat version 1.0"]
    for frame_path in files:
        lines.append(f"file {_quote_concat_path(frame_path)}")
        lines.append(f"duration {duration:.12g}")
    # The concat demuxer requires the last file to be repeated for the final
    # duration directive to take effect.
    lines.append(f"file {_quote_concat_path(files[-1])}")
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest_path, temp_dir_obj


def _resolve_single_frame_path(pattern: str, start_number: int) -> Path:
    pattern_path = Path(pattern)
    directory = pattern_path.parent
    template = pattern_path.name
    match = re.search(r"%0(\d+)d", template)
    if not match and pattern_path.exists():
        return pattern_path
    if match:
        digits = int(match.group(1))
        filename = re.sub(
            r"%0\d+d",
            f"{start_number:0{digits}d}",
            template,
            count=1,
        )
        candidate = directory / filename
        if candidate.exists():
            return candidate

    _, _, _, files = _expand_sequence(pattern)
    return files[0]


def _convert_single_frame_to_png(
    *,
    input_path: Path,
    output_path: Path,
    apply_color: bool,
    config_path: Optional[Path],
    input_space: str,
    display: str,
    view: str,
    ffmpeg_path: str,
    color_spec: Optional[dict] = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    input_ext = input_path.suffix.lower()

    if input_ext == ".exr" and apply_color:
        if config_path is None:
            raise RuntimeError("OCIO config required for single-frame EXR conversion")
        pipeline = _load_cpu_processor(
            config_path, input_space, display, view, color_spec=color_spec
        )
        _convert_exr_frame_cpu(input_path, output_path, pipeline)
        return

    if input_ext in {".jpg", ".jpeg", ".png"}:
        try:
            from PIL import Image

            with Image.open(input_path) as img:
                img.convert("RGB").save(output_path, "PNG")
            return
        except ImportError:
            if input_ext == ".png":
                shutil.copy2(input_path, output_path)
                return
            raise

    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        str(output_path),
    ]
    run_ffmpeg(command)


def _list_ffmpeg_encoders(ffmpeg_path: str) -> str:
    cached = _FFMPEG_ENCODERS_CACHE.get(ffmpeg_path)
    if cached is not None:
        return cached

    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            check=False,
            capture_output=True,
            text=True,
        )
        output = f"{result.stdout}\n{result.stderr}".lower()
    except Exception as exc:
        logging.warning("Could not inspect ffmpeg encoders via %s: %s", ffmpeg_path, exc)
        output = ""

    _FFMPEG_ENCODERS_CACHE[ffmpeg_path] = output
    return output


def _resolve_video_encoder(ffmpeg_path: str, requested_encoder: str) -> str:
    normalized = str(requested_encoder or "auto").strip().lower()
    if normalized and normalized != "auto":
        return normalized

    encoders_text = _list_ffmpeg_encoders(ffmpeg_path)
    if " h264_nvenc " in encoders_text:
        return "h264_nvenc"
    return "libx264"


def _build_encoder_args(
    *,
    video_encoder: str,
    preset: str,
    quality: int,
) -> list[str]:
    if video_encoder == "h264_nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            preset,
            "-rc:v",
            "vbr",
            "-cq:v",
            str(quality),
            "-b:v",
            "0",
        ]

    return [
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(quality),
    ]


def convert_sequence_cpu(
    *,
    input_pattern: str,
    start_number: int,
    config_path: Path,
    input_space: str,
    display: str,
    view: str,
    temp_dir: Optional[Union[str, Path]],
    worker_processes: int = 0,
    color_spec: Optional[dict] = None,
) -> Tuple[str, int, Optional[tempfile.TemporaryDirectory[str]]]:
    directory, template, digits, files = _expand_sequence(input_pattern)

    if config_path is None or not config_path.exists():
        raise RuntimeError("OCIO config is required for CPU color mode")

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
        conversions: List[Tuple[Path, Path]] = []
        for exr_path in files:
            match = frame_regex.search(exr_path.name)
            if not match:
                raise RuntimeError(f"Could not extract frame number from {exr_path.name}")
            frame_numbers.append(int(match.group(1)))
            conversions.append((exr_path, cpu_dir / exr_path.with_suffix(".bmp").name))

        _convert_frames_cpu(
            conversions,
            config_path=config_path,
            input_space=input_space,
            display=display,
            view=view,
            shard_dir=cpu_dir,
            worker_processes=worker_processes,
            color_spec=color_spec,
        )

        first_frame = frame_numbers[0] if frame_numbers else start_number
        bmp_template = template
        if bmp_template.lower().endswith(".exr"):
            bmp_template = bmp_template[:-4] + ".bmp"
        else:
            bmp_template = bmp_template + ".bmp"

        converted_pattern = str(cpu_dir / bmp_template)
        return converted_pattern, first_frame, temp_dir_obj
    except Exception:
        try:
            temp_dir_obj.cleanup()
        finally:
            _unregister_temp_path(cpu_dir)
        raise

def _scale_filter(max_dimension: int) -> Optional[str]:
    """Fit inside a square box without ever enlarging, on even dimensions.

    force_original_aspect_ratio=decrease does the fitting; taking the smaller
    of the frame and the box means a shot already inside it is left alone. The
    second pass rounds to even numbers, which yuv420p requires.
    """
    if not max_dimension or max_dimension <= 0:
        return None
    return (
        f"scale=w='min(iw,{max_dimension})':h='min(ih,{max_dimension})'"
        ":force_original_aspect_ratio=decrease"
        ",scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )


def build_ffmpeg_command(
    *,
    ffmpeg_path: str,
    start_number: int,
    frame_rate: float,
    input_pattern: str,
    output_path: str,
    video_encoder: str,
    preset: str,
    crf: int,
    concat_manifest: Optional[Path] = None,
    color_signature: Optional[str] = None,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
) -> tuple[list[str], str]:
    selected_encoder = _resolve_video_encoder(ffmpeg_path, video_encoder)
    command: list[str] = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
    ]

    if concat_manifest is not None:
        command.extend(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_manifest),
            ]
        )
    else:
        command.extend(
            [
                "-start_number",
                str(start_number),
                "-framerate",
                f"{frame_rate:g}",
                "-i",
                input_pattern,
            ]
        )

    if concat_manifest is not None:
        command.extend(["-r", f"{frame_rate:g}"])

    scale = _scale_filter(max_dimension)
    if scale:
        command.extend(["-vf", scale])

    command.extend(
        _build_encoder_args(
            video_encoder=selected_encoder,
            preset=preset,
            quality=crf,
        )
    )
    command.extend(
        [
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-an",
        ]
    )
    if color_signature:
        command.extend(["-metadata", f"comment={color_signature}"])
    command.append(output_path)
    return command, selected_encoder



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
    *,
    preferred_encoder: str,
    preset: str,
    quality: int,
) -> None:
    current_size = _get_file_size_mb(video_path)
    if current_size <= max_size_mb:
        return

    selected_encoder = _resolve_video_encoder(ffmpeg_path, preferred_encoder)
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
        ]
        if selected_encoder == "h264_nvenc":
            base_cmd.extend(
                [
                    "-c:v", "h264_nvenc",
                    "-preset", preset,
                    "-rc:v", "vbr",
                    "-cq:v", str(max(quality, 24)),
                    "-b:v", "0",
                    "-maxrate", f"{int(bitrate_k * 1.2)}k",
                    "-bufsize", f"{int(bitrate_k * 2)}k",
                ]
            )
        else:
            base_cmd.extend(
                [
                    "-c:v", "libx264",
                    "-b:v", f"{bitrate_k}k",
                    "-maxrate", f"{int(bitrate_k * 1.2)}k",
                    "-bufsize", f"{int(bitrate_k * 2)}k",
                    "-preset", preset,
                ]
            )
        base_cmd.extend(
            [
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-an",
            ]
        )
        passlog_arg = str(passlogfile)
        cmd_pass1 = base_cmd + ["-pass", "1", "-passlogfile", passlog_arg, "-f", "mp4", os.devnull]
        cmd_pass2 = base_cmd + ["-pass", "2", "-passlogfile", passlog_arg, str(output_path)]
        run_ffmpeg(cmd_pass1)
        run_ffmpeg(cmd_pass2)

    def run_quality(output_path: Path, quality_value: int) -> None:
        cmd = [ffmpeg_path, "-y", "-i", str(video_path)]
        cmd.extend(
            _build_encoder_args(
                video_encoder=selected_encoder,
                preset=preset,
                quality=quality_value,
            )
        )
        cmd.extend(
            [
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-an",
                str(output_path),
            ]
        )
        run_ffmpeg(cmd)

    attempts = [1.0, 0.85, 0.7, 0.55]
    best_path: Optional[Path] = None
    try:
        if target_bitrate is not None and selected_encoder == "libx264":
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
            for quality_value in (max(quality, 24), 26, 28, 30, 32, 34):
                output_path = video_path.with_name(
                    f"{video_path.stem}_compressed_q{quality_value}.mp4"
                )
                run_quality(output_path, quality_value)
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
    parser.add_argument("--input-pattern", required=True, help="Sequence pattern, e.g. path/to/shot.%%04d.exr")
    parser.add_argument("--output-path", required=True, help="Destination preview output path")
    parser.add_argument("--start-number", type=int, default=0, help="First frame number in the sequence")
    parser.add_argument("--frame-rate", type=float, default=25.0, help="Playback frame rate")
    parser.add_argument("--ffmpeg-path", default="ffmpeg", help="ffmpeg executable available on the worker")
    parser.add_argument(
        "--video-encoder",
        default="auto",
        help="Video encoder to use: auto (GPU-first), libx264, h264_nvenc",
    )
    parser.add_argument("--preset", default="fast", help="ffmpeg encoder preset")
    parser.add_argument("--crf", type=int, default=24, help="Preview quality value (CRF/CQ)")
    parser.add_argument("--max-size-mb", type=float, default=45.0, help="Max MP4 size in MB for delivery")
    parser.add_argument(
        "--max-dimension",
        type=int,
        default=DEFAULT_MAX_DIMENSION,
        help="Fit the preview inside this many pixels on its longest side (0 keeps the render size)",
    )
    parser.add_argument(
        "--temp-dir",
        default=None,
        help="Optional directory for temporary files (uses system temp if omitted)",
    )
    parser.add_argument(
        "--expected-frames",
        type=int,
        default=0,
        help="Number of frames the source render is expected to produce. Used to wait for "
             "incomplete sequence sync and to validate the encoded preview.",
    )
    parser.add_argument(
        "--input-wait-seconds",
        type=int,
        default=120,
        help="How long to wait for the full input frame set to appear before encoding.",
    )
    parser.add_argument(
        "--cpu-workers",
        type=int,
        default=0,
        help="Parallel processes for CPU color conversion (0 = auto)",
    )

    color_group = parser.add_argument_group("color management")
    color_group.add_argument("--disable-color", action="store_true", help="Disable OCIO color transform")
    color_group.add_argument(
        "--no-camera-lut",
        action="store_true",
        help="Ignore the camera LUT recorded in the render's preview_color.json sidecar",
    )
    color_group.add_argument(
        "--no-color-controls",
        action="store_true",
        help="Ignore the camera Color Controls (contrast/curves) from the sidecar",
    )
    color_group.add_argument("--ocio-config", help="Path to OCIO config file")
    color_group.add_argument("--input-space", default="ACEScg", help="OCIO input space")
    color_group.add_argument("--display", default="sRGB", help="OCIO display")
    color_group.add_argument("--view", default="ACES 1.0 SDR-video", help="OCIO view")

    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase logging verbosity")
    return parser.parse_args(argv)


def _env_flag(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _probe_output_resolution(output_path: Path, ffmpeg_path: str) -> Optional[str]:
    """Return the output's resolution as "WxH" from container metadata."""
    ffprobe_path = _resolve_ffprobe_path(ffmpeg_path)
    try:
        probe = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=s=x:p=0",
                str(output_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return None
    if probe.returncode != 0:
        return None
    for line in (probe.stdout or "").strip().splitlines():
        value = line.strip().strip("x")
        if value:
            return value
    return None


def _describe_color_controls(controls: Optional[dict]) -> str:
    """Short human-readable summary of the applied Color Controls."""
    if not controls:
        return ""
    parts: List[str] = []
    contrast = float(controls.get("contrast") or 0.0)
    if abs(contrast) > 1e-6:
        parts.append(f"Contrast {contrast:g}")
    curves = controls.get("curves") or {}
    if curves:
        order = [key for key in ("rgb", "r", "g", "b") if key in curves]
        parts.append("Curves " + "+".join(key.upper() for key in order))
    return ", ".join(parts)


def _describe_overscan(sidecar: Optional[dict]) -> Optional[str]:
    """Overscan exactly as the ROP states it, so the caption can be cross-checked.

    Redshift's "Pixels" value is the total added to each axis, split across both
    sides: 100 on a 2160x1440 camera renders 2260x1540, so the margin really is
    50 px all round. The caption still reports 100, because that is the number
    someone typed into the ROP - halving it would leave a figure that appears
    nowhere in Houdini and makes any mismatch impossible to trace.
    """
    overscan = (sidecar or {}).get("overscan")
    if not overscan or not overscan.get("mode"):
        return None

    unit = "%" if int(overscan.get("mode") or 0) == 2 else "px"
    x = float(overscan.get("x") or 0.0)
    y = float(overscan.get("y") or 0.0)
    if x <= 0 and y <= 0:
        return None
    amount = f"{x:g} {unit}" if abs(x - y) < 1e-6 else f"{x:g}x{y:g} {unit}"
    return f"Overscan {amount}"


def _describe_passes(sidecar: Optional[dict]) -> Optional[str]:
    """The extra passes this render writes, named as they are in the AOV list.

    The names come from each AOV's "Name" field, because that is what ends up
    in the EXR layer and in the AOV tab the artist is looking at. AOVs with no
    name of their own fall back to their type ("Cryptomatte", "Z Depth").
    """
    aovs = (sidecar or {}).get("aovs")
    if not isinstance(aovs, dict) or aovs.get("all_disabled"):
        return None

    names: List[str] = []
    for entry in aovs.get("list") or []:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        for candidate in (entry.get("name"), entry.get("type_label"), entry.get("type")):
            name = str(candidate or "").strip()
            if name:
                break
        if name and name not in names:
            names.append(name)
    if not names:
        return None

    # Every one of them, named. A pass the caption leaves out is a pass the
    # artist has to go and look up, which is the opposite of the point.
    return ", ".join(names)


def _frame_resolution(sidecar: Optional[dict]) -> Optional[str]:
    """The size the shot is delivered at, before overscan is added."""
    res = (sidecar or {}).get("resolution") or {}
    base = res.get("override") if res.get("override_enabled") else res.get("camera")
    if not base or len(base) < 2:
        return None
    try:
        width, height = int(base[0]), int(base[1])
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return "%dx%d" % (width, height)


def _resolution_fits_inside(frame: str, rendered: Optional[str]) -> bool:
    """Does the recorded frame sit within the image that was produced?

    Guards against a sidecar left behind by a different render: if the frame is
    not smaller than the output, the two do not belong together and the probed
    size is the only figure that can be trusted.
    """
    if not rendered:
        return True
    try:
        fw, fh = (int(part) for part in frame.split("x", 1))
        rw, rh = (int(part) for part in str(rendered).split("x", 1))
    except (TypeError, ValueError):
        return False
    return 0 < fw <= rw and 0 < fh <= rh


def _rendered_resolution(input_pattern: Optional[str], ffmpeg_path: str) -> Optional[str]:
    """The size of the frames the farm produced, read from one of them.

    The caption reports the render, not the video: those were the same figure
    until previews started being fitted into a box, at which point reading the
    encoded file began answering a question nobody asked.
    """
    if not input_pattern:
        return None
    try:
        if _has_sequence_placeholder(input_pattern):
            *_, frames = _expand_sequence(input_pattern)
        else:
            frames = [Path(input_pattern)]
        for frame in frames:
            if frame.exists():
                return _probe_output_resolution(frame, ffmpeg_path)
    except Exception as exc:
        logging.debug("Could not read the rendered frame size: %s", exc)
    return None


def _upload_metadata_headers(
    color_spec: Optional[dict],
    output_path: Path,
    ffmpeg_path: str,
    sidecar: Optional[dict] = None,
    input_pattern: Optional[str] = None,
) -> dict:
    """Describe the preview for the bot's caption.

    Covers the LUT, color controls, resolution and the extra passes rendered.
    """
    headers: dict = {}
    lut_spec = (color_spec or {}).get("lut")
    if lut_spec and lut_spec.get("file"):
        lut_name = Path(lut_spec["file"]).name
        headers["X-Preview-Lut"] = urllib.parse.quote(lut_name, safe="")
    controls_summary = _describe_color_controls((color_spec or {}).get("controls"))
    if controls_summary:
        headers["X-Preview-Color-Controls"] = urllib.parse.quote(
            controls_summary, safe=""
        )
    # Falls back to the encoded file only when no frame can be read - better a
    # figure than none, and without a cap the two agree anyway.
    resolution = _rendered_resolution(input_pattern, ffmpeg_path) or _probe_output_resolution(
        output_path, ffmpeg_path
    )
    overscan_summary = _describe_overscan(sidecar)
    if overscan_summary:
        # The rendered image carries the overscan margin, but the resolution
        # worth reporting is the one the shot is delivered at - that is what
        # people check against, and it does not move when overscan is toggled.
        # Only trust the recorded frame if it is actually smaller than what was
        # rendered; otherwise the sidecar does not match this output.
        frame = _frame_resolution(sidecar)
        if frame and _resolution_fits_inside(frame, resolution):
            resolution = frame
    if resolution:
        headers["X-Preview-Resolution"] = resolution
    if overscan_summary:
        headers["X-Preview-Overscan"] = urllib.parse.quote(overscan_summary, safe="")
    passes_summary = _describe_passes(sidecar)
    if passes_summary:
        headers["X-Preview-Passes"] = urllib.parse.quote(passes_summary, safe="")
    return headers


# Told apart because they call for opposite responses: only a machine that
# never reached the bot at all has a problem of its own.
UPLOAD_OK = "ok"
UPLOAD_REFUSED = "refused"          # the bot answered, and said no
UPLOAD_UNREACHABLE = "unreachable"  # no answer ever came back


def _maybe_upload_preview(output_path: Path, extra_headers: Optional[dict] = None) -> str:
    upload_url = os.environ.get("PREVIEW_UPLOAD_URL", "").strip()
    token = os.environ.get("PREVIEW_UPLOAD_TOKEN", "").strip()
    if not upload_url or not token:
        return UPLOAD_OK

    # Up to ~22 minutes of cumulative retry, covering bot restarts and network blips.
    attempt_delays = (0, 5, 15, 30, 60, 120, 240, 480, 480)
    # When every attempt dies at the connection level (reset/refused/timeout),
    # the path from THIS machine to the bot is broken and more retries will not
    # help — give up early so the task can move to another worker.
    transport_failfast_attempts = 4
    total_attempts = len(attempt_delays)
    got_http_response = False
    for attempt, delay in enumerate(attempt_delays, start=1):
        if delay:
            time.sleep(delay)
        try:
            ok = _upload_preview_file(
                output_path,
                upload_url,
                token,
                _env_flag("PREVIEW_UPLOAD_INSECURE"),
                extra_headers=extra_headers,
            )
            got_http_response = True
        except Exception as exc:
            logging.warning(
                "Preview upload attempt %s/%s error: %s",
                attempt,
                total_attempts,
                exc,
            )
            ok = False
        if ok:
            logging.info("Preview upload succeeded on attempt %s/%s", attempt, total_attempts)
            return UPLOAD_OK
        logging.warning("Preview upload attempt %s/%s failed", attempt, total_attempts)
        if not got_http_response and attempt >= transport_failfast_attempts:
            logging.error(
                "Upload endpoint never responded after %s connection-level failures; "
                "this machine likely cannot reach the bot",
                attempt,
            )
            return UPLOAD_UNREACHABLE
    return UPLOAD_REFUSED if got_http_response else UPLOAD_UNREACHABLE


def _handle_upload_failure(outcome: str = UPLOAD_UNREACHABLE) -> None:
    """React to an upload this machine could not deliver.

    Handing the task to another worker means striking this one off the job's
    machine list, and that is only right when the fault is this machine's. A
    bot that answers and refuses - an expired upload token, say - refuses every
    machine alike, and moving the task then walks the job through the farm
    crossing off one worker at a time until none is left, leaving a job no
    machine can take.

    So the task moves only when the bot never answered at all. Otherwise this
    fails outright, and the bot reports a failed preview - a message beats a
    job quietly stranded on an empty machine list.
    """
    if outcome == UPLOAD_REFUSED:
        logging.error(
            "The bot refused the upload; every worker would be refused the same "
            "way, so this task stays where it is instead of moving on."
        )
    elif _requeue_preview_task_elsewhere():
        logging.warning(
            "Preview upload failed; task requeued for another worker. "
            "Waiting for the Deadline Worker to stop this process..."
        )
        time.sleep(60)
    raise RuntimeError("Preview upload failed after retries")


def _upload_preview_file(
    output_path: Path,
    upload_url: str,
    token: str,
    insecure: bool,
    timeout: int = 120,
    extra_headers: Optional[dict] = None,
) -> bool:
    parsed = urllib.parse.urlparse(upload_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        logging.warning("Preview upload URL has unsupported format: %s", upload_url)
        return False

    if not output_path.exists():
        logging.warning("Preview upload file not found: %s", output_path)
        return False

    path = parsed.path or "/preview-upload"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    file_size = output_path.stat().st_size
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        context = ssl._create_unverified_context() if insecure else None
        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
            parsed.hostname,
            port,
            timeout=timeout,
            context=context,
        )
    else:
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)

    try:
        connection.putrequest("POST", path)
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(file_size))
        connection.putheader("X-Preview-Token", token)
        connection.putheader("X-Preview-Filename", output_path.name)
        for header_name, header_value in (extra_headers or {}).items():
            connection.putheader(header_name, header_value)
        connection.endheaders()

        with open(output_path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                connection.send(chunk)

        response = connection.getresponse()
        response_body = response.read(200)
        if 200 <= response.status < 300:
            return True
        logging.warning(
            "Preview upload responded with %s: %s",
            response.status,
            response_body.decode("utf-8", errors="ignore"),
        )
        return False
    finally:
        try:
            connection.close()
        except Exception:
            pass


def main(argv: Optional[list[str]] = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    if "--cpu-convert-manifest" in argv_list:
        configure_logging(1)
        manifest_index = argv_list.index("--cpu-convert-manifest")
        try:
            return _run_cpu_convert_manifest(argv_list[manifest_index + 1])
        except Exception:
            logging.exception("CPU conversion shard failed")
            return 1

    args = parse_arguments(argv)
    configure_logging(args.verbose)

    delegated_exit = _ensure_color_runtime(args)
    if delegated_exit is not None:
        return delegated_exit

    _install_signal_handlers()

    try:
        cleanup_resources: List[tempfile.TemporaryDirectory[str]] = []
        base_temp_dir = _resolve_base_temp_dir(args.temp_dir)

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
        concat_manifest: Optional[Path] = None
        available_input_files: List[Path] = []

        expected_frames = max(0, int(args.expected_frames or 0))
        validation_frame_count = expected_frames
        if expected_frames > 0:
            wait_seconds = max(0, int(args.input_wait_seconds or 0))
            found = _wait_for_input_frames(
                args.input_pattern,
                expected_frames,
                wait_seconds,
            )
            if found < expected_frames:
                logging.warning(
                    "Found %s input frames, expected %s after waiting %ss; "
                    "building a partial preview from available frames.",
                    found,
                    expected_frames,
                    wait_seconds,
                )

        color_spec: Optional[dict] = None
        color_already_baked = False

        def _resolve_lut_after_sync(input_files: List[Path]) -> Optional[dict]:
            """Read the sidecar and decide what color work is left for the preview.

            Sets color_already_baked when Redshift wrote frames that already
            carry the view transform / LUT / color controls.
            """
            nonlocal color_already_baked
            if not apply_color:
                return None
            newest_mtime: Optional[float] = None
            try:
                newest_mtime = max(frame.stat().st_mtime for frame in input_files)
            except (ValueError, OSError):
                pass
            _wait_for_color_sidecar(args.input_pattern, newest_mtime)
            sidecar = _load_color_sidecar(args.input_pattern)

            if _output_already_has_color(sidecar, is_exr_input):
                logging.info(
                    "Redshift already baked color management into these frames "
                    "(ROP output setting); leaving them untouched"
                )
                color_already_baked = True
                return None

            spec = _extract_camera_color_spec(sidecar)
            if spec:
                if args.no_camera_lut:
                    spec["lut"] = None
                if args.no_color_controls:
                    spec["controls"] = None
                if not (spec["lut"] or spec["controls"]):
                    spec = None

            # Log what is actually applied, after the disable flags are honored.
            lut_spec = (spec or {}).get("lut")
            controls = (spec or {}).get("controls")
            if lut_spec:
                logging.info(
                    "Applying camera LUT %s (strength=%s, log=%s, cm_before_lut=%s)",
                    lut_spec["file"],
                    lut_spec["strength"],
                    lut_spec["is_log"],
                    lut_spec["cm_before_lut"],
                )
            elif args.no_camera_lut:
                logging.info("Camera LUT skipped (disabled in preview settings)")
            if controls:
                logging.info(
                    "Applying camera color controls (%s)",
                    _describe_color_controls(controls),
                )
            elif args.no_color_controls:
                logging.info("Camera color controls skipped (disabled in preview settings)")
            return spec

        if expected_frames == 1:
            input_frame = _resolve_single_frame_path(args.input_pattern, args.start_number)
            _prefetch_input_files([input_frame])
            color_spec = _resolve_lut_after_sync([input_frame])
            if color_already_baked:
                apply_color = False
            _convert_single_frame_to_png(
                input_path=input_frame,
                output_path=Path(args.output_path),
                apply_color=apply_color,
                config_path=config_path,
                input_space=args.input_space,
                display=args.display,
                view=args.view,
                ffmpeg_path=args.ffmpeg_path,
                color_spec=color_spec,
            )
            output_path = Path(args.output_path)
            if not output_path.exists() or output_path.stat().st_size <= 0:
                raise RuntimeError(f"Single-frame PNG was not written: {output_path}")
            logging.info("Preview still successfully written to %s", output_path)
            upload_headers = _upload_metadata_headers(
                color_spec,
                output_path,
                args.ffmpeg_path,
                sidecar=_load_color_sidecar(args.input_pattern),
                input_pattern=args.input_pattern,
            )
            outcome = _maybe_upload_preview(output_path, extra_headers=upload_headers)
            if outcome != UPLOAD_OK:
                _handle_upload_failure(outcome)
            return 0

        if _has_sequence_placeholder(args.input_pattern):
            _, _, _, available_input_files = _expand_sequence(args.input_pattern)
            validation_frame_count = len(available_input_files)
            if expected_frames > 0 and validation_frame_count < expected_frames:
                logging.info(
                    "Partial preview source contains %s/%s available frames",
                    validation_frame_count,
                    expected_frames,
                )
            if not available_input_files:
                raise RuntimeError(f"No frames found matching pattern {args.input_pattern}")
            _prefetch_input_files(available_input_files)
            color_spec = _resolve_lut_after_sync(available_input_files)
            if color_already_baked:
                apply_color = False

            reuse_inputs = list(available_input_files)
            sidecar_file = Path(args.input_pattern).parent / "preview_color.json"
            if sidecar_file.is_file():
                # A newer sidecar (e.g. changed camera LUT) must invalidate reuse.
                reuse_inputs.append(sidecar_file)
            if _reusable_existing_output(
                Path(args.output_path),
                reuse_inputs,
                args.ffmpeg_path,
                validation_frame_count,
                color_signature=_color_signature(color_spec),
                max_dimension=args.max_dimension,
            ):
                logging.info(
                    "Reusing existing preview output %s (newer than all input frames)",
                    args.output_path,
                )
                upload_headers = _upload_metadata_headers(
                    color_spec,
                    Path(args.output_path),
                    args.ffmpeg_path,
                    sidecar=_load_color_sidecar(args.input_pattern),
                    input_pattern=args.input_pattern,
                )
                outcome = _maybe_upload_preview(
                    Path(args.output_path), extra_headers=upload_headers
                )
                if outcome != UPLOAD_OK:
                    _handle_upload_failure(outcome)
                return 0

        if apply_color:
            if config_path is None:
                raise RuntimeError("OCIO config required for the preview color transform")
            converted_pattern, start_number, cpu_temp_dir = convert_sequence_cpu(
                input_pattern=args.input_pattern,
                start_number=args.start_number,
                config_path=config_path,
                input_space=args.input_space,
                worker_processes=args.cpu_workers,
                display=args.display,
                view=args.view,
                temp_dir=base_temp_dir,
                color_spec=color_spec,
            )
            ffmpeg_input_pattern = converted_pattern
            if cpu_temp_dir is not None:
                cleanup_resources.append(cpu_temp_dir)
            if _has_sequence_placeholder(ffmpeg_input_pattern):
                _, _, _, available_input_files = _expand_sequence(ffmpeg_input_pattern)
                validation_frame_count = len(available_input_files)

        if len(available_input_files) > 0:
            concat_manifest, concat_temp_dir = _create_concat_manifest(
                available_input_files,
                args.frame_rate,
                base_temp_dir,
            )
            cleanup_resources.append(concat_temp_dir)

        command, selected_encoder = build_ffmpeg_command(
            ffmpeg_path=args.ffmpeg_path,
            start_number=start_number,
            frame_rate=args.frame_rate,
            input_pattern=ffmpeg_input_pattern,
            output_path=args.output_path,
            video_encoder=args.video_encoder,
            preset=args.preset,
            crf=args.crf,
            max_dimension=args.max_dimension,
            concat_manifest=concat_manifest,
            color_signature=_color_signature(color_spec),
        )
        logging.info("Using preview video encoder: %s", selected_encoder)

        def _run_with_encoder(encoder_name: str) -> str:
            cmd, resolved_encoder = build_ffmpeg_command(
                ffmpeg_path=args.ffmpeg_path,
                start_number=start_number,
                frame_rate=args.frame_rate,
                input_pattern=ffmpeg_input_pattern,
                output_path=args.output_path,
                video_encoder=encoder_name,
                preset=args.preset,
                crf=args.crf,
                max_dimension=args.max_dimension,
                concat_manifest=concat_manifest,
                color_signature=_color_signature(color_spec),
            )
            run_ffmpeg(cmd)
            return resolved_encoder

        encode_error: Optional[Exception] = None
        try:
            run_ffmpeg(command)
        except Exception as exc:
            encode_error = exc

        if encode_error is None:
            valid, validation_reason = _validate_preview_output(
                Path(args.output_path),
                args.ffmpeg_path,
                validation_frame_count,
            )
            logging.info("Preview validation: %s", validation_reason)
            if not valid:
                logging.warning(
                    "Preview output failed validation with encoder %s: %s",
                    selected_encoder,
                    validation_reason,
                )
                encode_error = RuntimeError(validation_reason)

        if encode_error is not None and selected_encoder != "libx264":
            logging.warning(
                "Retrying preview encode with libx264 after %s failure: %s",
                selected_encoder,
                encode_error,
            )
            with contextlib.suppress(Exception):
                Path(args.output_path).unlink()
            try:
                selected_encoder = _run_with_encoder("libx264")
                encode_error = None
            except Exception as retry_exc:
                encode_error = retry_exc
            if encode_error is None:
                valid, validation_reason = _validate_preview_output(
                    Path(args.output_path),
                    args.ffmpeg_path,
                    validation_frame_count,
                )
                logging.info("Preview validation after libx264 retry: %s", validation_reason)
                if not valid:
                    encode_error = RuntimeError(validation_reason)

        if encode_error is not None:
            raise encode_error

        output_stat_before = Path(args.output_path).stat()
        _compress_if_needed(
            Path(args.output_path),
            args.ffmpeg_path,
            args.max_size_mb,
            preferred_encoder=selected_encoder,
            preset=args.preset,
            quality=args.crf,
        )
        output_stat_after = Path(args.output_path).stat()
        if (
            output_stat_after.st_size != output_stat_before.st_size
            or output_stat_after.st_mtime_ns != output_stat_before.st_mtime_ns
        ):
            # Only re-validate when the size-limit pass actually re-encoded the file.
            final_valid, final_reason = _validate_preview_output(
                Path(args.output_path),
                args.ffmpeg_path,
                validation_frame_count,
            )
            logging.info("Final preview validation: %s", final_reason)
            if not final_valid:
                raise RuntimeError(
                    f"Preview output failed final validation: {final_reason}"
                )

        logging.info("Preview video successfully written to %s", args.output_path)
        upload_headers = _upload_metadata_headers(
            color_spec,
            Path(args.output_path),
            args.ffmpeg_path,
            sidecar=_load_color_sidecar(args.input_pattern),
            input_pattern=args.input_pattern,
        )
        outcome = _maybe_upload_preview(Path(args.output_path), extra_headers=upload_headers)
        if outcome != UPLOAD_OK:
            _handle_upload_failure(outcome)
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
