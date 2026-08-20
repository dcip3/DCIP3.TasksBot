"""
Preview creation and delivery service functions.
"""

from typing import Optional, List, Dict, Any, Tuple, Union
from pathlib import Path
import base64
import json
import logging
import ntpath
import posixpath
import re
import shlex
import subprocess
import zlib

from app.core.config import settings
from app.services.deadline import (
    ALLOWED_WORKER_STATUSES,
    DeadlineSubmissionError,
    WorkerStatusError,
    get_job_info,
    get_workers_by_credentials,
    submit_deadline_job,
)

logger = logging.getLogger(__name__)
_SCRIPT_CACHE_PATH: Optional[Path] = None
_SCRIPT_CACHE_MTIME_NS: Optional[int] = None
_SCRIPT_CACHE_B64: Optional[str] = None


class PreviewSubmissionError(RuntimeError):
    """Raised when a preview submission fails with a user-facing reason."""

    def __init__(self, user_message: str, log_message: Optional[str] = None):
        super().__init__(log_message or user_message)
        self.user_message = user_message


def _resolve_preview_helper_script() -> Optional[Path]:
    current = Path(__file__).resolve()
    candidates = [
        current.parents[2] / "scripts" / "deadline_preview_worker.py",  # repo/app + scripts
        current.parents[3] / "scripts" / "deadline_preview_worker.py",  # repo root + scripts
        Path.cwd() / "scripts" / "deadline_preview_worker.py",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _load_preview_helper_script_b64(script_path: Path) -> str:
    global _SCRIPT_CACHE_PATH, _SCRIPT_CACHE_MTIME_NS, _SCRIPT_CACHE_B64
    stat = script_path.stat()
    mtime_ns = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))
    if (
        _SCRIPT_CACHE_B64 is not None
        and _SCRIPT_CACHE_PATH == script_path
        and _SCRIPT_CACHE_MTIME_NS == mtime_ns
    ):
        return _SCRIPT_CACHE_B64

    script_bytes = script_path.read_bytes()
    compressed_script = zlib.compress(script_bytes)
    encoded = base64.b64encode(compressed_script).decode("ascii")
    _SCRIPT_CACHE_PATH = script_path
    _SCRIPT_CACHE_MTIME_NS = mtime_ns
    _SCRIPT_CACHE_B64 = encoded
    return encoded


def _sanitize_windows_filename(name: str) -> str:
    """Replace characters that are invalid in Windows file names."""
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def _count_expected_frames(frames_str: str) -> int:
    """Count rendered frames from a Deadline Frames spec like "1-100,150-160x2"."""
    if not frames_str:
        return 0
    total = 0
    for chunk in str(frames_str).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        range_match = re.match(r"^(-?\d+)\s*-\s*(-?\d+)(?:\s*[xX:]\s*(\d+))?$", chunk)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            step_raw = range_match.group(3)
            step = int(step_raw) if step_raw and int(step_raw) > 0 else 1
            lo, hi = (start, end) if start <= end else (end, start)
            total += (hi - lo) // step + 1
            continue
        if re.match(r"^-?\d+$", chunk):
            total += 1
    return total


def _normalize_listed_workers(raw_value: Any) -> List[str]:
    """Normalize Deadline machine restriction values into a unique worker list."""
    if raw_value is None:
        return []

    if isinstance(raw_value, (list, tuple)):
        values = list(raw_value)
    else:
        raw_text = str(raw_value).strip()
        if not raw_text:
            return []
        values = raw_text.split(",")

    normalized: List[str] = []
    seen: set[str] = set()
    for value in values:
        worker_name = str(value or "").strip()
        if not worker_name:
            continue
        worker_key = worker_name.lower()
        if worker_key in seen:
            continue
        seen.add(worker_key)
        normalized.append(worker_name)
    return normalized


def _worker_is_usable(entry: Dict[str, Any]) -> bool:
    """Can this worker take a task at all?

    Its status is not the whole answer: a Worker that has been disabled in
    Deadline keeps reporting itself as Idle - it stays connected, it just never
    dequeues anything - so a job pinned to it waits for good.
    """
    info = entry.get("Info") or {}
    worker_settings = entry.get("Settings") or {}
    if worker_settings.get("Enable") is False:
        return False
    return info.get("Stat") in ALLOWED_WORKER_STATUSES


def _unusable_workers(
    names: List[str], workers: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Which of these workers cannot take the preview, and why."""
    by_name = {}
    for entry in workers:
        info = entry.get("Info") or {}
        name = info.get("Name")
        if name:
            by_name[name] = entry

    unusable: List[Dict[str, Any]] = []
    for name in names:
        entry = by_name.get(name)
        if entry is None:
            unusable.append(
                {"name": name, "status_code": None, "status_text": "Unknown worker"}
            )
            continue
        if _worker_is_usable(entry):
            continue
        status_code = (entry.get("Info") or {}).get("Stat")
        if (entry.get("Settings") or {}).get("Enable") is False:
            status_text = "Disabled"
        else:
            status_text = settings.worker_status_map.get(
                status_code, f"Unknown ({status_code})"
            )
        unusable.append(
            {"name": name, "status_code": status_code, "status_text": status_text}
        )
    return unusable


def _machine_restriction_fields(
    preferred_slaves: List[str], whitelist_flag: Optional[bool]
) -> Dict[str, Any]:
    """Deadline job-info fields for a machine allow or deny list.

    Deadline's submission keys are "Whitelist" and "Blacklist"; which one is
    used *is* the flag. There is no key that flips a list from one to the
    other - "WhitelistFlag" belongs to Limit Groups and is ignored here - so
    writing the deny list under "Whitelist" pinned the preview to the very
    machines the artist had excluded from the render.
    """
    if not preferred_slaves:
        return {"MachineLimit": 0}
    if whitelist_flag is False:
        # A deny list says where the preview must not go; every other machine
        # is fair game, so it must not carry a machine limit of its own.
        return {"MachineLimit": 0, "Blacklist": ",".join(preferred_slaves)}
    return {
        "MachineLimit": len(preferred_slaves),
        "Whitelist": ",".join(preferred_slaves),
    }


def preview_cannot_run_anywhere(
    preview_props: Dict[str, Any], workers: List[Dict[str, Any]]
) -> bool:
    """True when no machine on the farm may pick this preview up.

    An allow list can do that by naming only machines that cannot take a task.
    So can a deny list, once it has grown to cover every machine that can:
    workers strike themselves off a preview they failed to deliver, and two of
    them in a row leave nothing behind. Either way Deadline shows the job as
    Queued with no error, which looks exactly like a busy farm until you check
    what it is allowed to run on.
    """
    listed, whitelist_flag = _resolve_machine_restrictions(preview_props)
    if not listed:
        return False

    if whitelist_flag is False:
        denied = {name.strip().lower() for name in listed}
        return not any(
            _worker_is_usable(entry)
            and str((entry.get("Info") or {}).get("Name") or "").strip().lower() not in denied
            for entry in workers
        )

    return len(_unusable_workers(listed, workers)) == len(listed)


def _resolve_machine_restrictions(job_props: Dict[str, Any]) -> tuple[List[str], Optional[bool]]:
    """Extract whitelist/blacklist settings from the source job."""
    listed_workers = _normalize_listed_workers(job_props.get("ListedSlaves"))
    if not listed_workers:
        return [], None

    whitelist_flag_raw = job_props.get("White")
    if isinstance(whitelist_flag_raw, str):
        whitelist_flag = whitelist_flag_raw.strip().lower() not in {"false", "0", "no"}
    elif whitelist_flag_raw is None:
        whitelist_flag = True
    else:
        whitelist_flag = bool(whitelist_flag_raw)

    return listed_workers, whitelist_flag





async def create_video_from_job(
    telegram_user_id: int,
    job_id: str,
    *,
    skip_worker_validation: bool = False,
    use_any_machine: bool = False,
    specific_worker: Optional[str] = None,
    input_wait_seconds: Optional[int] = None,
    presubmitted: bool = False,
    depends_on: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Submit a Deadline CommandLine job that generates a preview video using ffmpeg.

    Args:
        telegram_user_id: Telegram user whose credentials are used.
        job_id: Source Deadline job identifier.
        skip_worker_validation: Skip checking worker statuses before submission.
        use_any_machine: Ignore preferred workers and allow Deadline to pick any machine.
        specific_worker: Force preview job to run on specific worker (overrides other settings).

    Returns:
        Dict with submission details and expected paths, or None if unable to submit.
    """
    from app.auth import get_deadline_credentials

    credentials = await get_deadline_credentials(telegram_user_id)
    if not credentials:
        logger.error("No Deadline credentials found for user %s", telegram_user_id)
        raise PreviewSubmissionError(
            "Deadline credentials are missing. Please /login again."
        )

    login, password = credentials
    job_info = await get_job_info(login, password, job_id)
    if not job_info:
        logger.error("Could not get job info for %s", job_id)
        raise PreviewSubmissionError(
            "Failed to fetch job details from Deadline. Please try again."
        )

    props = job_info.get("Props", {})
    outdirs = job_info.get("OutDir", [])
    if not outdirs:
        logger.error("No OutDir found for job %s", job_id)
        raise PreviewSubmissionError(
            "Render output path was not found for this job."
        )

    output_path = outdirs[0]
    out_files = job_info.get("OutFile", [])
    template_name = out_files[0] if out_files else ""
    pattern = template_name or "*.exr"

    def replace_hashes(match: re.Match) -> str:
        return f"%0{len(match.group(0))}d"

    pattern_fmt = re.sub(r"#+", replace_hashes, pattern)

    output_path_clean = output_path.rstrip("\\/") or output_path
    is_windows_path = "\\" in output_path_clean or ":" in output_path_clean

    frames_str = props.get("Frames", "")
    expected_frames = _count_expected_frames(frames_str)
    preview_is_still = expected_frames == 1

    video_base = Path(template_name).stem if template_name else props.get("Name") or props.get("Batch") or job_id
    video_base = re.sub(r'#+', '', video_base)
    video_base = re.sub(r'%0\d+d', '', video_base)
    video_base = video_base.rstrip('. _')
    if template_name and Path(template_name).suffix.lower() in {".jpg", ".jpeg", ".png"}:
        if is_windows_path:
            video_base = ntpath.basename(output_path_clean)
        else:
            video_base = posixpath.basename(output_path_clean)
    video_base = _sanitize_windows_filename(video_base or job_id)
    video_filename = f"{video_base}{'.png' if preview_is_still else '.mp4'}"

    if is_windows_path:
        render_output_dir = ntpath.dirname(output_path_clean) or output_path_clean
        input_sequence_path = ntpath.join(output_path_clean, pattern_fmt)
        video_output_path = ntpath.join(render_output_dir, video_filename)
    else:
        render_output_dir = posixpath.dirname(output_path_clean) or output_path_clean
        input_sequence_path = posixpath.join(output_path_clean, pattern_fmt)
        video_output_path = posixpath.join(render_output_dir, video_filename)

    expected_local_path = video_output_path
    expected_render_path = output_path_clean
    # Forward-slash form of the video path, used for user-facing captions and
    # carried in job metadata under the historical PreviewDropbox key.
    expected_display_video = video_output_path.replace("\\", "/")

    upload_token: Optional[str] = None
    upload_url: Optional[str] = None
    if settings.preview_upload_enabled:
        from app.core.preview_upload import (
            PreviewUploadPayload,
            get_preview_upload_url,
            issue_preview_upload_token,
        )

        upload_url = get_preview_upload_url()
        if upload_url:
            payload = PreviewUploadPayload(
                telegram_user_id=telegram_user_id,
                job_name=str(props.get("Name") or props.get("Batch") or job_id),
                expected_dropbox_path=None,
                expected_filename=video_filename,
                expected_local_path=expected_local_path,
                expected_render_path=expected_render_path,
                source_job_id=job_id,
            )
            upload_token = await issue_preview_upload_token(payload)

    start_match = re.search(r"-?\d+", frames_str)
    start_frame = int(start_match.group()) if start_match else 0

    fps_value = props.get("PlugInfo", {}).get("FPS")
    try:
        frame_rate = float(fps_value) if fps_value is not None else 25.0
    except (TypeError, ValueError):
        frame_rate = 25.0

    helper_script = _resolve_preview_helper_script()
    if helper_script is None:
        logger.error("Preview helper script not found in expected locations")
        raise PreviewSubmissionError(
            "Preview helper script is missing on the bot host."
        )

    script_b64 = _load_preview_helper_script_b64(helper_script)

    aux_files: List[Union[str, Path, Tuple[Union[str, Path], str]]] = []

    python_exec = settings.preview_python_executable or "python"
    ffmpeg_exec = settings.ffmpeg_path or "ffmpeg"

    attach_config = settings.preview_attach_ocio_config
    remote_config_path: Optional[str]
    if attach_config:
        local_config_path = Path(settings.ocio_config_path)
        if not local_config_path.exists():
            logger.error("Configured OCIO config not found for attachment: %s", local_config_path)
            raise PreviewSubmissionError(
                "OCIO config file was not found. Please check OCIO_CONFIG_PATH."
            )
        aux_files.append((local_config_path, local_config_path.name))
        remote_config_path = (
            f"%DEADLINE_AUX_ROOT%\\{local_config_path.name}" if is_windows_path else f"$DEADLINE_AUX_ROOT/{local_config_path.name}"
        )
    else:
        remote_config_path = settings.preview_ocio_remote_config
        if not remote_config_path:
            ocio_candidate = Path(settings.ocio_config_path)
            remote_config_path = str(ocio_candidate) if ocio_candidate.is_absolute() else None

    from app.storage.user_settings import get_preview_post_effects

    post_effects = await get_preview_post_effects(telegram_user_id)

    apply_color = settings.preview_apply_color_transform and post_effects["color_transform"]
    input_ext = Path(str(input_sequence_path)).suffix.lower()
    if input_ext in {".jpg", ".jpeg", ".png"} and apply_color:
        logger.info(
            "Disabling preview color transform for non-EXR input pattern: %s",
            input_sequence_path,
        )
        apply_color = False
    if apply_color and not remote_config_path:
        logger.warning(
            "Preview color transform enabled but no OCIO config path available; disabling color transform for job %s",
            job_id,
        )
        apply_color = False

    script_args: List[str] = [
        "--input-pattern",
        input_sequence_path,
        "--output-path",
        video_output_path,
        "--start-number",
        str(start_frame),
        "--frame-rate",
        f"{frame_rate:g}",
        "--ffmpeg-path",
        ffmpeg_exec,
        "--video-encoder",
        "auto",
        "--preset",
        "fast",
        "--crf",
        "24",
        "--max-size-mb",
        "45",
    ]

    if expected_frames > 0:
        script_args.extend(["--expected-frames", str(expected_frames)])

    if input_wait_seconds is not None and input_wait_seconds > 0:
        script_args.extend(["--input-wait-seconds", str(int(input_wait_seconds))])

    if settings.preview_temp_dir:
        script_args.extend(["--temp-dir", settings.preview_temp_dir])

    if apply_color:
        if remote_config_path:
            script_args.extend(["--ocio-config", remote_config_path])
        script_args.extend(
            [
                "--input-space",
                settings.preview_input_space,
                "--display",
                settings.preview_display,
                "--view",
                settings.preview_view,
            ]
        )
        # Camera post effects the user chose to skip (raw-render previews).
        if not post_effects["lut"]:
            script_args.append("--no-camera-lut")
        if not post_effects["color_controls"]:
            script_args.append("--no-color-controls")
    else:
        script_args.append("--disable-color")

    script_argv = ["deadline_preview_worker.py"] + script_args
    argv_json = json.dumps(script_argv)
    argv_b64 = base64.b64encode(argv_json.encode("utf-8")).decode("ascii")

    stub_code = (
        "import os,sys,base64,zlib,json;"
        "script=os.environ['PREVIEW_SCRIPT_B64'];"
        "argv=os.environ['PREVIEW_ARGV_B64'];"
        "sys.argv=json.loads(base64.b64decode(argv).decode('utf-8'));"
        "exec(zlib.decompress(base64.b64decode(script)))"
    )

    python_args = ["-c", stub_code]
    if not is_windows_path:
        python_args_str = " ".join(shlex.quote(arg) for arg in python_args)

    preferred_slaves, whitelist_flag = _resolve_machine_restrictions(props)

    # Override with specific worker if requested
    if specific_worker:
        preferred_slaves = [specific_worker]
        whitelist_flag = True
    elif use_any_machine:
        preferred_slaves = []
        whitelist_flag = None

    # An allow list may only name machines that can actually take the job. A
    # deny list needs no such check - it says where the preview must not go.
    if preferred_slaves and whitelist_flag is not False:
        workers = await get_workers_by_credentials(login, password)
        invalid_workers = _unusable_workers(preferred_slaves, workers)
        if invalid_workers and not skip_worker_validation:
            raise WorkerStatusError(invalid_workers, preferred_slaves)
        if invalid_workers:
            # Nobody is waiting to be asked here (auto previews, retries), and
            # a preview pinned to a machine that cannot run it waits in the
            # queue for ever without a word. Run it somewhere that works.
            unusable_names = {entry["name"] for entry in invalid_workers}
            logger.warning(
                "Preview for %s: dropping unusable worker(s) %s from the allow list",
                job_id,
                ", ".join(
                    f"{entry['name']} ({entry['status_text']})"
                    for entry in invalid_workers
                ),
            )
            preferred_slaves = [
                name for name in preferred_slaves if name not in unusable_names
            ]
            if not preferred_slaves:
                whitelist_flag = None

    # Outrank the source render (and its siblings) so the worker that frees up
    # picks this preview before dequeuing the next render job.
    try:
        source_priority = int(props.get("Pri", 50) or 50)
    except (TypeError, ValueError):
        source_priority = 50
    preview_priority = min(100, max(75, source_priority + 5))

    preview_job_info: Dict[str, Any] = {
        "Name": f"{props.get('Name', job_id)} - Preview",
        "Batch": props.get("Batch") or props.get("Name") or "Preview",
        "Plugin": "CommandLine",
        "UserName": props.get("User") or login,
        "Comment": f"Preview job generated by TasksBot for {job_id}",
        "Frames": "0-0",
        "ChunkSize": 1,
        "Priority": preview_priority,
        "ExtraInfo0": expected_local_path,
        "ExtraInfo1": expected_display_video,
        "ExtraInfoKeyValue0": f"PreviewLocal={expected_local_path}",
        "ExtraInfoKeyValue1": f"PreviewDropbox={expected_display_video}",
        "ExtraInfoKeyValue2": "PreviewJob=1",
        "ExtraInfoKeyValue3": f"PreviewTelegram={telegram_user_id}",
        "ExtraInfoKeyValue4": f"PreviewSource={job_id}",
        "ExtraInfoKeyValue5": f"PreviewRenderPath={expected_render_path}",
    }
    if presubmitted:
        # Marks previews queued automatically while the render is still
        # finishing; only these are reconciled against the source job.
        # Manual previews always run as requested.
        preview_job_info["ExtraInfoKeyValue6"] = "PreviewPresubmit=1"
    if depends_on:
        # Deadline holds the job in Pending until the render completes, so the
        # preview can be queued early without ever competing with the render
        # for machines. The farm event plugin releases it the moment the render
        # finishes; Deadline's own pending scan is the fallback.
        preview_job_info["JobDependencies"] = depends_on
        preview_job_info["ResumeOnCompleteDependencies"] = "true"
        preview_job_info["ResumeOnDeletedDependencies"] = "false"
        preview_job_info["ResumeOnFailedDependencies"] = "false"
    if props.get("Pool"):
        preview_job_info["Pool"] = props["Pool"]
    if props.get("SecPool"):
        preview_job_info["SecondaryPool"] = props["SecPool"]
    if props.get("Grp"):
        preview_job_info["Group"] = props["Grp"]
    preview_job_info.update(_machine_restriction_fields(preferred_slaves, whitelist_flag))

    environment_pairs: Dict[str, str] = {
        "PREVIEW_SCRIPT_B64": script_b64,
        "PREVIEW_ARGV_B64": argv_b64,
        "PREVIEW_STUB": stub_code,
    }
    if upload_token and upload_url:
        environment_pairs["PREVIEW_UPLOAD_URL"] = upload_url
        environment_pairs["PREVIEW_UPLOAD_TOKEN"] = upload_token
        if settings.preview_upload_insecure:
            environment_pairs["PREVIEW_UPLOAD_INSECURE"] = "1"
    if apply_color and remote_config_path:
        environment_pairs["OCIO"] = remote_config_path

    if environment_pairs:
        for idx, (key, value) in enumerate(environment_pairs.items()):
            preview_job_info[f"EnvironmentKeyValue{idx}"] = f"{key}={value}"

    if is_windows_path:
        def split_windows_command(command: str) -> List[str]:
            cmd_trimmed = command.strip()
            if not cmd_trimmed:
                return []
            if (
                " " in cmd_trimmed
                and not cmd_trimmed.startswith('"')
                and (":" in cmd_trimmed or "\\" in cmd_trimmed or "/" in cmd_trimmed)
            ):
                return [cmd_trimmed]
            try:
                return shlex.split(cmd_trimmed, posix=False)
            except ValueError:
                return [cmd_trimmed]

        primary_tokens = split_windows_command(python_exec)
        normalized_python = " ".join(primary_tokens).strip().lower()
        if normalized_python in {"", "python"}:
            py_invocation = subprocess.list2cmdline(["py"] + python_args)
            python_invocation = subprocess.list2cmdline(["python"] + python_args)
            arguments_str = (
                f"/C where py >NUL 2>NUL && ({py_invocation}) "
                f"|| (where py >NUL 2>NUL && exit /b 1 || {python_invocation})"
            )
            executable = "cmd.exe"
        else:
            executable = primary_tokens[0]
            arguments_str = subprocess.list2cmdline(primary_tokens[1:] + python_args)
        command_line = f"{executable} {arguments_str}"
    else:
        executable = python_exec
        arguments_str = python_args_str
        command_line = f"{python_exec} {python_args_str}"

    # No StartupDirectory: render paths are not visible from the bot host (VPS),
    # and the worker script uses absolute paths everywhere anyway.
    plugin_info = {
        "Executable": executable,
        "Arguments": arguments_str,
        "Shell": "default",
    }

    try:
        submission_response = await submit_deadline_job(
            telegram_user_id=telegram_user_id,
            job_info=preview_job_info,
            plugin_info=plugin_info,
            aux_files=aux_files,
            complete_submission=True,
        )
    except DeadlineSubmissionError as exc:
        logger.error("Failed to submit preview job for %s: %s", job_id, exc)
        raise PreviewSubmissionError(
            "Failed to submit the preview job to Deadline. Please try again.",
            str(exc),
        ) from exc

    preview_job_id = submission_response.get("job_id") or submission_response.get("_id")
    logger.info(
        "Submitted preview job %s for %s (command: %s, whitelist: %s)",
        preview_job_id,
        job_id,
        command_line,
        preferred_slaves,
    )

    if upload_token:
        from app.core.preview_upload import drop_preview_upload_token, update_preview_upload_token

        if preview_job_id:
            await update_preview_upload_token(upload_token, str(preview_job_id))
        else:
            await drop_preview_upload_token(upload_token)

    return {
        "preview_job_id": preview_job_id,
        "expected_display_path": expected_display_video,
        "expected_local_path": expected_local_path,
        "expected_render_path": expected_render_path,
        "command_line": command_line,
        "preferred_slaves": preferred_slaves,
        "submission": submission_response,
    }
