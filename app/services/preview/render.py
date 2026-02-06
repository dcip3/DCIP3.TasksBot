"""
Preview creation and delivery service functions.
"""

from typing import Optional, List, Dict, Any, Tuple, Union
from pathlib import Path, PurePosixPath
from collections import Counter
import base64
import json
import logging
import ntpath
import posixpath
import re
import shlex
import subprocess
import zlib

import aiohttp

from app.core.config import settings
from app.core.path_utils import extract_dropbox_path, normalize_dropbox_path
from app.integrations.dropbox_helpers import (
    get_fresh_access_token,
    fetch_dropbox_metadata,
)
from app.services.dropbox import get_dropbox_session
from app.services.deadline import (
    ALLOWED_WORKER_STATUSES,
    DeadlineSubmissionError,
    WorkerStatusError,
    get_job_info,
    get_job_tasks,
    get_workers_by_credentials,
    submit_deadline_job,
)

logger = logging.getLogger(__name__)
_DOWNLOAD_VIDEO_GLOBAL_TIMEOUT_SECONDS = 90.0
_DOWNLOAD_VIDEO_RETRY_ATTEMPTS = 4
_DOWNLOAD_VIDEO_RETRY_BASE_DELAY = 0.25
_DOWNLOAD_VIDEO_RETRY_MAX_DELAY = 2.0


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


def _sanitize_windows_filename(name: str) -> str:
    """Replace characters that are invalid in Windows file names."""
    return re.sub(r'[\\/:*?"<>|]', "_", name)





async def create_video_from_job(
    telegram_user_id: int,
    job_id: str,
    *,
    skip_worker_validation: bool = False,
    use_any_machine: bool = False,
    specific_worker: Optional[str] = None,
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
    status_value = job_info.get("Stat")
    status_text = str(status_value).strip().lower() if status_value is not None else ""
    completed_statuses = {"3", "complete", "completed", "finished", "done", "succeeded", "success"}
    is_job_completed = False
    if isinstance(status_value, int):
        is_job_completed = status_value == 3
    elif status_text:
        is_job_completed = status_text in completed_statuses

    outdirs = job_info.get("OutDir", [])
    if not outdirs:
        logger.error("No OutDir found for job %s", job_id)
        raise PreviewSubmissionError(
            "Render output path was not found for this job."
        )

    output_path = outdirs[0]
    idx = output_path.find(settings.dropbox_root_marker)
    dropbox_marker_found = idx != -1
    if idx == -1:
        logger.warning("Dropbox root marker not found in path: %s", output_path)
        dropbox_folder = output_path.replace("\\", "/")
    else:
        trimmed = output_path[idx:]
        dropbox_folder = "/" + trimmed.replace("\\", "/").lstrip("/")

    out_files = job_info.get("OutFile", [])
    template_name = out_files[0] if out_files else ""
    pattern = template_name or "*.exr"

    def replace_hashes(match: re.Match) -> str:
        return f"%0{len(match.group(0))}d"

    pattern_fmt = re.sub(r"#+", replace_hashes, pattern)

    output_path_clean = output_path.rstrip("\\/") or output_path
    is_windows_path = "\\" in output_path_clean or ":" in output_path_clean

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
    video_filename = f"{video_base}.mp4"

    if is_windows_path:
        render_output_dir = ntpath.dirname(output_path_clean) or output_path_clean
        input_sequence_path = ntpath.join(output_path_clean, pattern_fmt)
        video_output_path = ntpath.join(render_output_dir, video_filename)
    else:
        render_output_dir = posixpath.dirname(output_path_clean) or output_path_clean
        input_sequence_path = posixpath.join(output_path_clean, pattern_fmt)
        video_output_path = posixpath.join(render_output_dir, video_filename)

    expected_local_path = video_output_path

    dropbox_folder_normalized = normalize_dropbox_path(dropbox_folder)
    if dropbox_folder_normalized:
        dropbox_parent = PurePosixPath(dropbox_folder_normalized).parent
        if str(dropbox_parent) in {"", "."}:
            expected_dropbox_video = video_filename
        else:
            expected_dropbox_video = str(dropbox_parent / video_filename)
    else:
        expected_dropbox_video = video_filename

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
            dropbox_hint = expected_dropbox_video if dropbox_marker_found else None
            payload = PreviewUploadPayload(
                telegram_user_id=telegram_user_id,
                job_name=str(props.get("Name") or props.get("Batch") or job_id),
                expected_dropbox_path=dropbox_hint,
                expected_filename=video_filename,
                expected_local_path=expected_local_path,
                source_job_id=job_id,
            )
            upload_token = await issue_preview_upload_token(payload)

    frames_str = props.get("Frames", "")
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

    script_bytes = helper_script.read_bytes()
    compressed_script = zlib.compress(script_bytes)
    script_b64 = base64.b64encode(compressed_script).decode("ascii")

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

    apply_color = settings.preview_apply_color_transform
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

    color_mode = (getattr(settings, "preview_color_mode", "lut") or "lut").strip().lower()

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
        "--preset",
        "medium",
        "--crf",
        "20",
        "--max-size-mb",
        "45",
    ]
    script_args.extend(["--color-mode", color_mode])

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
                "--lut-size",
                str(settings.preview_lut_size),
            ]
        )
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

    tasks = await get_job_tasks(login, password, job_id)
    slave_counter: Counter[str] = Counter(
        task.get("Slave") for task in tasks if isinstance(task, dict) and task.get("Slave")
    )

    # Prefer the worker that created/submitted the job
    job_creator_machine = props.get("Mach")
    preferred_slaves = []

    if job_creator_machine:
        # Put creator's machine first
        preferred_slaves.append(job_creator_machine)
        # Add other workers that rendered this job
        for slave, _ in slave_counter.most_common():
            if slave != job_creator_machine and slave not in preferred_slaves:
                preferred_slaves.append(slave)
    else:
        # Fallback to workers that rendered tasks
        preferred_slaves = [slave for slave, _ in slave_counter.most_common()]

    # Override with specific worker if requested
    if specific_worker:
        preferred_slaves = [specific_worker]
    elif use_any_machine:
        preferred_slaves = []

    if preferred_slaves and not skip_worker_validation:
        workers = await get_workers_by_credentials(login, password)
        worker_map = {}
        for worker in workers:
            info = worker.get("Info", {})
            name = info.get("Name")
            if name:
                worker_map[name] = info
        invalid_workers: List[Dict[str, Any]] = []
        for slave in preferred_slaves:
            info = worker_map.get(slave)
            if not info:
                invalid_workers.append(
                    {
                        "name": slave,
                        "status_code": None,
                        "status_text": "Unknown worker",
                    }
                )
                continue
            status_code = info.get("Stat")
            if status_code not in ALLOWED_WORKER_STATUSES:
                status_text = settings.worker_status_map.get(status_code, f"Unknown ({status_code})")
                invalid_workers.append(
                    {
                        "name": slave,
                        "status_code": status_code,
                        "status_text": status_text,
                    }
                )
        if invalid_workers:
            raise WorkerStatusError(invalid_workers, preferred_slaves)

    preview_job_info: Dict[str, Any] = {
        "Name": f"{props.get('Name', job_id)} - Preview",
        "Batch": props.get("Batch") or props.get("Name") or "Preview",
        "Plugin": "CommandLine",
        "UserName": props.get("User") or login,
        "Comment": f"Preview job generated by TasksBot for {job_id}",
        "Frames": "0-0",
        "ChunkSize": 1,
        "Priority": 75,
        "MachineLimit": len(preferred_slaves) if preferred_slaves else 0,
        "ExtraInfo0": expected_local_path,
        "ExtraInfo1": expected_dropbox_video,
        "ExtraInfoKeyValue0": f"PreviewLocal={expected_local_path}",
        "ExtraInfoKeyValue1": f"PreviewDropbox={expected_dropbox_video}",
        "ExtraInfoKeyValue2": "PreviewJob=1",
        "ExtraInfoKeyValue3": f"PreviewTelegram={telegram_user_id}",
        "ExtraInfoKeyValue4": f"PreviewSource={job_id}",
    }
    if not is_job_completed:
        preview_job_info["JobDependency0"] = job_id
    if props.get("Pool"):
        preview_job_info["Pool"] = props["Pool"]
    if props.get("SecPool"):
        preview_job_info["SecondaryPool"] = props["SecPool"]
    if props.get("Grp"):
        preview_job_info["Group"] = props["Grp"]
    if preferred_slaves:
        preview_job_info["Whitelist"] = ",".join(preferred_slaves)

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
        stub_placeholder = '"%PREVIEW_STUB%"'
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

        candidate_commands: List[List[str]] = []
        primary_tokens = split_windows_command(python_exec)
        fallback_candidates = [
            ["py"],
            ["python"],
        ]
        for fallback in fallback_candidates:
            if fallback not in candidate_commands:
                candidate_commands.append(fallback)
        if primary_tokens:
            normalized = " ".join(primary_tokens).strip().lower()
            if normalized and primary_tokens not in candidate_commands:
                if normalized in {"python", "py", "py -3", "py -3.11"}:
                    candidate_commands.append(primary_tokens)
                else:
                    candidate_commands.insert(0, primary_tokens)

        command_segments = []
        for tokens in candidate_commands:
            cmd_head = subprocess.list2cmdline(tokens)
            segment = f"{cmd_head} -c {stub_placeholder}"
            command_segments.append(segment)
        fallback_command = " || ".join(command_segments)
        arguments_str = f"/C {fallback_command}"
        executable = "cmd.exe"
        command_line = f"{executable} {arguments_str}"
    else:
        executable = python_exec
        arguments_str = python_args_str
        command_line = f"{python_exec} {python_args_str}"

    startup_dir: Optional[str] = render_output_dir
    try:
        if startup_dir and not Path(startup_dir).exists():
            logger.warning(
                "Preview startup directory %s is not accessible; using default working directory",
                startup_dir,
            )
            startup_dir = None
    except Exception as path_error:
        logger.warning(
            "Could not verify preview startup directory %s: %s",
            startup_dir,
            path_error,
        )
        startup_dir = None

    plugin_info = {
        "Executable": executable,
        "Arguments": arguments_str,
        "Shell": "default",
    }
    if startup_dir:
        plugin_info["StartupDirectory"] = startup_dir

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
        "expected_dropbox_path": expected_dropbox_video,
        "expected_local_path": expected_local_path,
        "command_line": command_line,
        "preferred_slaves": preferred_slaves,
        "submission": submission_response,
    }


async def check_video_exists_in_dropbox(
    login: str,
    password: str,
    job_id: str,
    dropbox_path_hint: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Check if video already exists in Dropbox for a job.
    
    Args:
        login: User login
        password: User password
        job_id: Job ID
        dropbox_path_hint: Optional explicit Dropbox path to the expected video file
        
    Returns:
        Video info dict if exists, None otherwise
    """
    try:
        import aiohttp
        from pathlib import PurePosixPath
        from app.integrations.dropbox_helpers import get_fresh_access_token, fetch_dropbox_metadata
        from app.core.config import settings
        
        session_dbx = await get_dropbox_session()
        headers_dbx = {
            "Authorization": f"Bearer {await get_fresh_access_token()}",
            "Dropbox-API-Select-User": settings.dropbox_team_member_id,
            "Dropbox-API-Path-Root": {".tag": "root", "root": settings.dropbox_root_namespace_id},
            "Content-Type": "application/json"
        }

        normalized_hint = normalize_dropbox_path(dropbox_path_hint)
        if normalized_hint:
            try:
                video_metadata = await fetch_dropbox_metadata(session_dbx, normalized_hint, headers_dbx)
                if video_metadata.get(".tag") == "file":
                    video_filename = PurePosixPath(normalized_hint).name
                    return {
                        "exists": True,
                        "filename": video_filename,
                        "dropbox_path": normalized_hint,
                        "metadata": video_metadata,
                    }
            except Exception as hint_error:
                logger.debug(
                    "Dropbox hint lookup failed for job %s at %s: %s",
                    job_id,
                    normalized_hint,
                    hint_error,
                )

        # Fallback to resolving path through the Deadline job
        job_info = await get_job_info(login, password, job_id)
        if not job_info:
            logger.error(f"Could not get job info for {job_id}")
            return None

        outdirs = job_info.get("OutDir", [])
        if not outdirs:
            logger.error(f"No OutDir found for job {job_id}")
            return None

        fullpath = outdirs[0]
        dropbox_path = extract_dropbox_path(fullpath, settings.dropbox_root_marker)
        if not dropbox_path:
            logger.info(
                "Skipping Dropbox lookup for job %s: output path is outside configured Dropbox root (%s)",
                job_id,
                fullpath,
            )
            return None

        # Get metadata for the folder
        metadata = await fetch_dropbox_metadata(session_dbx, dropbox_path, headers_dbx)
            
        if metadata.get(".tag") != "folder":
            logger.error("Not a folder")
            return None
                
        # Check if video exists in the same folder
        exr_parent = str(PurePosixPath(metadata["path_display"]).parent)
        video_filename = f"{metadata['name']}.mp4"
        video_dropbox_path = f"{exr_parent}/{video_filename}"
            
        try:
            # Try to get metadata for the video file
            video_metadata = await fetch_dropbox_metadata(session_dbx, video_dropbox_path, headers_dbx)
            if video_metadata.get(".tag") == "file":
                return {
                    "exists": True,
                    "filename": video_filename,
                    "dropbox_path": video_dropbox_path,
                    "metadata": video_metadata
                }
        except Exception:
            # Video doesn't exist
            pass
                
        return None
            
    except Exception as e:
        logger.error(f"Error checking video existence for job {job_id}: {e}")
        return None


async def download_video_from_dropbox(
    login: str,
    password: str,
    job_id: str,
    dropbox_path_hint: Optional[str] = None,
) -> Optional[tuple[str, str]]:
    """
    Download existing video from Dropbox.
    
    Args:
        login: User login
        password: User password
        job_id: Job ID
        
    Returns:
        Local path to downloaded video or None if error
    """
    try:
        import aiohttp
        import aiofiles
        import json
        import asyncio
        import random
        import contextlib
        from pathlib import Path
        from app.integrations.dropbox_helpers import get_fresh_access_token
        from app.core.config import settings
        
        # Check if video exists
        video_info = await check_video_exists_in_dropbox(
            login,
            password,
            job_id,
            dropbox_path_hint=dropbox_path_hint,
        )
        if not video_info:
            logger.error(f"Video not found in Dropbox for job {job_id}")
            return None
            
        # Create temp directory
        temp_dir = Path(settings.temp_dir)
        temp_dir.mkdir(exist_ok=True)
        
        # Download video
        download_url = "https://content.dropboxapi.com/2/files/download"
        dl_headers = {
            "Authorization": f"Bearer {await get_fresh_access_token()}",
            "Dropbox-API-Select-User": settings.dropbox_team_member_id,
            "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
            "Dropbox-API-Arg": json.dumps({"path": video_info["dropbox_path"]})
        }
        
        session_dbx = await get_dropbox_session()
        retry_statuses = {401, 408, 429, 500, 502, 503, 504}

        def _compute_delay(attempt: int, retry_after: Optional[str]) -> float:
            if retry_after:
                try:
                    return max(float(retry_after), 0.0)
                except ValueError:
                    pass
            delay = min(
                _DOWNLOAD_VIDEO_RETRY_BASE_DELAY * (2 ** attempt),
                _DOWNLOAD_VIDEO_RETRY_MAX_DELAY,
            )
            return delay + random.uniform(0.0, _DOWNLOAD_VIDEO_RETRY_BASE_DELAY)

        # Use the original filename for local storage
        filename = video_info["filename"]
        temp_path = temp_dir / filename
        temp_path.parent.mkdir(parents=True, exist_ok=True)

        async with asyncio.timeout(_DOWNLOAD_VIDEO_GLOBAL_TIMEOUT_SECONDS):
            for attempt in range(_DOWNLOAD_VIDEO_RETRY_ATTEMPTS):
                dl_headers = {
                    "Authorization": f"Bearer {await get_fresh_access_token()}",
                    "Dropbox-API-Select-User": settings.dropbox_team_member_id,
                    "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
                    "Dropbox-API-Arg": json.dumps({"path": video_info["dropbox_path"]})
                }
                try:
                    async with session_dbx.post(download_url, headers=dl_headers) as resp:
                        if resp.status == 200:
                            async with aiofiles.open(temp_path, "wb") as f:
                                async for chunk in resp.content.iter_chunked(1024 * 1024):
                                    await f.write(chunk)
                            logger.info(f"Video downloaded to {temp_path}")
                            return (str(temp_path), video_info["dropbox_path"])

                        text = await resp.text()
                        if resp.status in retry_statuses and attempt < (_DOWNLOAD_VIDEO_RETRY_ATTEMPTS - 1):
                            delay = _compute_delay(attempt, resp.headers.get("Retry-After"))
                            logger.warning(
                                "Retrying Dropbox download %s (%s): %s",
                                filename,
                                resp.status,
                                text,
                            )
                            await asyncio.sleep(delay)
                        else:
                            logger.error(f"Error downloading video: {text}")
                            return None
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    if attempt < (_DOWNLOAD_VIDEO_RETRY_ATTEMPTS - 1):
                        delay = _compute_delay(attempt, None)
                        logger.warning("Retrying Dropbox download %s after error: %s", filename, exc)
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"Error downloading video for job {job_id}: {exc}")
                        return None
                if temp_path.exists():
                    with contextlib.suppress(Exception):
                        temp_path.unlink()
    except TimeoutError:
        logger.error(
            "Timed out downloading video for job %s after %.0f seconds",
            job_id,
            _DOWNLOAD_VIDEO_GLOBAL_TIMEOUT_SECONDS,
        )
        return None

    except Exception as e:
        logger.error(f"Error downloading video for job {job_id}: {e}")
        return None
