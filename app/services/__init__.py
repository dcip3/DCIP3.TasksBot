# app/services/__init__.py
"""
API service functions for Deadline and Dropbox integration.

This module provides functions for interacting with the Deadline API
and Dropbox API for job management, worker monitoring, and file operations.
"""

import json
import logging
import ntpath
import posixpath
import re
import shlex
import subprocess
import base64
import zlib
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Optional, List, Dict, Any, Tuple, Union

import aiohttp
from app.core.config import settings
from app.integrations.dropbox_helpers import (
    get_fresh_access_token,
    fetch_dropbox_metadata,
    download_exr_folder,
    upload_video_to_dropbox
)
from app.core.bot_core import get_aiosession

logger = logging.getLogger(__name__)

ALLOWED_WORKER_STATUSES = {0, 1, 2}

# ============================================================================
# === EXCEPTIONS ===
# ============================================================================


class DeadlineSubmissionError(RuntimeError):
    """Raised when a Deadline job submission fails."""


class WorkerStatusError(RuntimeError):
    """Raised when preferred workers are not in an allowed status."""

    def __init__(self, invalid_workers: List[Dict[str, Any]], preferred_workers: List[str]):
        message = "One or more preferred workers are not in an allowed status."
        super().__init__(message)
        self.invalid_workers = invalid_workers
        self.preferred_workers = preferred_workers


# ============================================================================
# === UTILITY FUNCTIONS ===
# ============================================================================

async def create_optimized_session() -> aiohttp.ClientSession:
    """
    Create an optimized aiohttp session for better performance.
    
    Returns:
        Optimized aiohttp ClientSession
    """
    timeout = aiohttp.ClientTimeout(total=300, connect=30)  # 5 minutes total, 30 seconds connect
    connector = aiohttp.TCPConnector(
        limit=100,  # Total connection pool size
        limit_per_host=30,  # Connections per host
        ttl_dns_cache=300,  # DNS cache TTL
        use_dns_cache=True,
        keepalive_timeout=30,
        enable_cleanup_closed=True
    )
    
    return aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        headers={"User-Agent": "TasksBot/1.0"}
    )

# ============================================================================
# === DEADLINE API FUNCTIONS ===
# ============================================================================

async def get_jobs_list(telegram_user_id: int) -> List[Dict[str, Any]]:
    """
    Get list of jobs from Deadline API.

    Args:
        telegram_user_id: Telegram user ID

    Returns:
        List of job dictionaries
    """
    try:
        from app.auth import get_deadline_credentials
        credentials = await get_deadline_credentials(telegram_user_id)
        if not credentials:
            logger.error(f"No Deadline credentials found for user {telegram_user_id}")
            return []

        login, password = credentials
        logger.info(f"Requesting jobs for user {telegram_user_id} with login {login}")

        session = await get_aiosession()
        headers = aiohttp.BasicAuth(login, password)
        async with session.get(f"{settings.deadline_api_url}/jobs", auth=headers, ssl=False) as resp:
            logger.info(f"Jobs API response status: {resp.status}")
            if resp.status == 200:
                data = await resp.json()
                logger.info(f"Jobs API returned {len(data) if isinstance(data, list) else 'non-list'} items")
                return data
            else:
                response_text = await resp.text()
                logger.error(f"Failed to get jobs: {resp.status}, response: {response_text}")
                return []
    except Exception as e:
        logger.error(f"Error getting jobs: {e}")
        return []


async def _fetch_workers(login: str, password: str) -> List[Dict[str, Any]]:
    """Fetch workers list using provided Deadline credentials."""
    try:
        session = await get_aiosession()
        headers = aiohttp.BasicAuth(login, password)
        async with session.get(f"{settings.deadline_api_url}/slaves?Data=infosettings", auth=headers, ssl=False) as resp:
            logger.info(f"Slaves API response status: {resp.status}")
            if resp.status == 200:
                data = await resp.json()
                logger.info(f"Slaves API returned {len(data) if isinstance(data, list) else 'non-list'} items")
                return data
            response_text = await resp.text()
            logger.error(f"Failed to get slaves: {resp.status}, response: {response_text}")
            return []
    except Exception as e:
        logger.error(f"Error getting slaves: {e}")
        return []


async def get_workers_list(telegram_user_id: int) -> List[Dict[str, Any]]:
    """
    Get list of workers (slaves) from Deadline API.
    
    Args:
        telegram_user_id: Telegram user ID
        
    Returns:
        List of worker dictionaries
    """
    try:
        from app.auth import get_deadline_credentials
        credentials = await get_deadline_credentials(telegram_user_id)
        if not credentials:
            logger.error(f"No Deadline credentials found for user {telegram_user_id}")
            return []
        
        login, password = credentials
        logger.info(f"Requesting slaves for user {telegram_user_id} with login {login}")
        return await _fetch_workers(login, password)
    except Exception as e:
        logger.error(f"Error getting slaves: {e}")
        return []


async def get_workers_by_credentials(login: str, password: str) -> List[Dict[str, Any]]:
    """Return list of workers using raw credentials."""
    return await _fetch_workers(login, password)


async def get_worker_report_contents(
    login: str,
    password: str,
    worker_names: List[str],
) -> List[Dict[str, Any]]:
    """Fetch worker report contents (including error logs) for specified workers."""

    filtered_names = []
    seen = set()
    for name in worker_names:
        if not name:
            continue
        normalized = str(name).strip()
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        filtered_names.append(normalized)

    if not filtered_names:
        return []

    params = [("Data", "reportcontents")]
    for name in filtered_names:
        params.append(("Name", name))

    url = f"{settings.deadline_api_url}/slaves"
    session = await get_aiosession()
    auth = aiohttp.BasicAuth(login, password)
    async with session.get(url, params=params, auth=auth, ssl=False) as resp:
        text = await resp.text()
        if resp.status != 200:
            logger.error(
                "Failed to fetch worker report contents (%s): %s",
                resp.status,
                text,
            )
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as decode_error:
            logger.error(
                "Worker report contents returned invalid JSON: %s (error: %s)",
                text[:200],
                decode_error,
            )
            return []

    reports: List[Dict[str, Any]] = []

    def walk(node: Any, current_worker: Optional[str] = None) -> None:
        if isinstance(node, dict):
            worker = (
                node.get("Slave")
                or node.get("Worker")
                or node.get("Name")
                or node.get("WorkerName")
                or current_worker
            )
            report_type = node.get("ReportType")
            if report_type is None and "Type" in node:
                report_type = node.get("Type")
            contents = (
                node.get("Contents")
                or node.get("ReportContents")
                or node.get("Content")
                or node.get("Text")
            )
            if contents is not None and report_type is not None:
                reports.append(
                    {
                        "worker": worker,
                        "type": report_type,
                        "contents": contents,
                        "title": node.get("Title") or node.get("Filename"),
                        "timestamp": node.get("Date") or node.get("Timestamp"),
                    }
                )
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, worker)
        elif isinstance(node, list):
            for item in node:
                walk(item, current_worker)

    walk(data)
    return reports


async def get_job_info(login: str, password: str, job_id: str) -> Optional[Dict[str, Any]]:
    """
    Get detailed information about a specific job.

    Args:
        login: User login
        password: User password
        job_id: Job ID

    Returns:
        Job information dictionary or None if error
    """
    try:
        session = await get_aiosession()
        headers = aiohttp.BasicAuth(login, password)
        # Get all jobs and find the specific one
        async with session.get(f"{settings.deadline_api_url}/jobs", auth=headers, ssl=False) as resp:
            if resp.status == 200:
                jobs = await resp.json()
                # Find the job by _id
                matching_jobs = [j for j in jobs if j.get("_id") == job_id]
                if matching_jobs:
                    return matching_jobs[0]
                else:
                    logger.error(f"Job {job_id} not found in jobs list")
                    return None
            else:
                logger.error(f"Failed to get jobs list: {resp.status}")
                return None
    except Exception as e:
        logger.error(f"Error getting job info: {e}")
        return None


async def get_job_info_by_user_id(telegram_user_id: int, job_id: str) -> Optional[Dict[str, Any]]:
    """
    Get detailed information about a specific job using telegram user ID.
    
    Args:
        telegram_user_id: Telegram user ID
        job_id: Job ID
        
    Returns:
        Job information dictionary or None if error
    """
    try:
        from app.auth import get_deadline_credentials
        credentials = await get_deadline_credentials(telegram_user_id)
        if not credentials:
            logger.error(f"No Deadline credentials found for user {telegram_user_id}")
            return None
        
        login, password = credentials
        return await get_job_info(login, password, job_id)
    except Exception as e:
        logger.error(f"Error getting job info for user {telegram_user_id}: {e}")
        return None


async def get_job_tasks(login: str, password: str, job_id: str) -> List[Dict[str, Any]]:
    """
    Get tasks for a specific job.

    Args:
        login: User login
        password: User password
        job_id: Job ID

    Returns:
        List of task dictionaries
    """
    try:
        session = await get_aiosession()
        headers = aiohttp.BasicAuth(login, password)
        async with session.get(f"{settings.deadline_api_url}/tasks?JobID={job_id}", auth=headers, ssl=False) as resp:
            if resp.status == 200:
                data = await resp.json()
                # API may return {"Tasks": [...]} or a plain list
                if isinstance(data, dict):
                    return data.get("Tasks", [])
                return data
            else:
                logger.error(f"Failed to get job tasks: {resp.status}")
                return []
    except Exception as e:
        logger.error(f"Error getting job tasks: {e}")
        return []


async def get_job_tasks_by_user_id(telegram_user_id: int, job_id: str) -> List[Dict[str, Any]]:
    """
    Get tasks for a specific job using telegram user ID.
    
    Args:
        telegram_user_id: Telegram user ID
        job_id: Job ID
        
    Returns:
        List of task dictionaries
    """
    try:
        from app.auth import get_deadline_credentials
        credentials = await get_deadline_credentials(telegram_user_id)
        if not credentials:
            logger.error(f"No Deadline credentials found for user {telegram_user_id}")
            return []
        
        login, password = credentials
        return await get_job_tasks(login, password, job_id)
    except Exception as e:
        logger.error(f"Error getting job tasks for user {telegram_user_id}: {e}")
        return []


async def submit_deadline_job(
    telegram_user_id: int,
    job_info: Dict[str, Any],
    plugin_info: Dict[str, Any],
    *,
    aux_files: Optional[List[Union[str, Path, Tuple[Union[str, Path], str]]]] = None,
    complete_submission: bool = True
) -> Dict[str, Any]:
    """
    Submit a new job to Deadline on behalf of the specified Telegram user.

    Args:
        telegram_user_id: Telegram user ID whose Deadline credentials should be used.
        job_info: Dictionary describing Deadline JobInfo settings (equivalent to .job file).
        plugin_info: Dictionary with plugin-specific parameters (equivalent to .plugin file).
        aux_files: Optional list of auxiliary files to upload. Each entry can be a path-like
            object or a tuple (local_path, remote_name). The remote name defaults to the file
            name if not provided.
        complete_submission: Whether to finalize the submission by calling
            ``/jobs/{job_id}/complete-submission`` after uploading aux files.

    Returns:
        Dictionary with submission response data including the new job id.

    Raises:
        DeadlineSubmissionError: If submission or aux upload fails.
        FileNotFoundError: If any referenced auxiliary file is missing.
    """
    from app.auth import get_deadline_credentials

    credentials = await get_deadline_credentials(telegram_user_id)
    if not credentials:
        error_msg = f"No Deadline credentials found for user {telegram_user_id}"
        logger.error(error_msg)
        raise DeadlineSubmissionError(error_msg)

    login, password = credentials
    payload: Dict[str, Any] = {
        "JobInfo": job_info,
        "PluginInfo": plugin_info,
        "AuxFiles": []
    }

    resolved_aux: List[Tuple[Path, str]] = []
    if aux_files:
        for entry in aux_files:
            remote_name: Optional[str]
            if isinstance(entry, tuple):
                local_path, remote_name = entry
            else:
                local_path, remote_name = entry, None

            local_path = Path(local_path)
            if not local_path.exists():
                raise FileNotFoundError(f"Auxiliary file not found: {local_path}")

            remote_name = remote_name or local_path.name
            resolved_aux.append((local_path, remote_name))

        if resolved_aux:
            payload["AuxFiles"] = [remote for _, remote in resolved_aux]
            for idx, (_, remote_name) in enumerate(resolved_aux):
                payload["JobInfo"][f"AuxiliarySubmissionFile{idx}"] = remote_name
            payload["JobInfo"]["CompleteSubmission"] = "False"

    submit_url = f"{settings.deadline_api_url}/jobs"
    logger.info("Submitting Deadline job via %s", submit_url)

    async with aiohttp.ClientSession() as session:
        auth = aiohttp.BasicAuth(login, password)
        async with session.post(submit_url, auth=auth, ssl=False, json=payload) as resp:
            text = await resp.text()
            if resp.status not in (200, 201, 202, 204):
                logger.error("Deadline submission failed (%s): %s", resp.status, text)
                raise DeadlineSubmissionError(f"Submission failed with status {resp.status}")
            if text:
                try:
                    submission_response = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("Unexpected non-JSON response from Deadline: %s", text)
                    submission_response = {}
            else:
                submission_response = {}

    job_id = submission_response.get("job_id") or submission_response.get("_id")
    if not job_id:
        logger.error("Deadline submission response missing job_id. Response: %s", submission_response)
        raise DeadlineSubmissionError("Deadline submission did not return a job id")
    else:
        logger.info("Deadline job submitted successfully: %s", job_id)

    if resolved_aux and job_id:
        async with aiohttp.ClientSession() as session:
            auth = aiohttp.BasicAuth(login, password)
            for local_path, remote_name in resolved_aux:
                upload_url = f"{settings.deadline_api_url}/jobs/{job_id}/aux-files/{remote_name}"
                logger.info("Uploading aux file %s -> %s", local_path, upload_url)
                async with session.put(
                    upload_url,
                    auth=auth,
                    ssl=False,
                    data=local_path.read_bytes(),
                    headers={"Content-Type": "application/octet-stream"}
                ) as upload_resp:
                    if upload_resp.status not in (200, 201, 204):
                        text = await upload_resp.text()
                        logger.error(
                            "Failed to upload aux file %s (%s): %s",
                            remote_name,
                            upload_resp.status,
                            text
                        )
                        raise DeadlineSubmissionError(
                            f"Aux file upload failed for {remote_name} ({upload_resp.status})"
                        )

            if complete_submission:
                complete_url = f"{settings.deadline_api_url}/jobs/{job_id}/complete-submission"
                async with session.post(complete_url, auth=auth, ssl=False) as comp_resp:
                    if comp_resp.status not in (200, 201, 204):
                        text = await comp_resp.text()
                        logger.error(
                            "Failed to complete submission for job %s (%s): %s",
                            job_id,
                            comp_resp.status,
                            text
                        )
                        raise DeadlineSubmissionError(
                            f"Complete submission failed ({comp_resp.status})"
                        )

    return submission_response


async def requeue_job(login: str, password: str, job_id: str) -> bool:
    """
    Requeue a job.

    Args:
        login: User login
        password: User password
        job_id: Job ID

    Returns:
        True if successful, False otherwise
    """
    try:
        session = await get_aiosession()
        headers = aiohttp.BasicAuth(login, password)
        json_body = {"Command": "requeue", "JobID": job_id}
        async with session.put(f"{settings.deadline_api_url}/jobs", json=json_body, auth=headers, ssl=False) as resp:
            success = resp.status == 200
            if not success:
                logger.error(f"Failed to requeue job: {resp.status}")
            return success
    except Exception as e:
        logger.error(f"Error requeuing job: {e}")
        return False


async def requeue_job_by_user_id(telegram_user_id: int, job_id: str) -> bool:
    """
    Requeue a job using telegram user ID.
    
    Args:
        telegram_user_id: Telegram user ID
        job_id: Job ID
        
    Returns:
        True if successful, False otherwise
    """
    try:
        from app.auth import get_deadline_credentials
        credentials = await get_deadline_credentials(telegram_user_id)
        if not credentials:
            logger.error(f"No Deadline credentials found for user {telegram_user_id}")
            return False
        
        login, password = credentials
        return await requeue_job(login, password, job_id)
    except Exception as e:
        logger.error(f"Error requeuing job for user {telegram_user_id}: {e}")
        return False


async def resume_job(login: str, password: str, job_id: str) -> bool:
    """
    Resume a suspended job.

    Args:
        login: User login
        password: User password
        job_id: Job ID

    Returns:
        True if successful, False otherwise
    """
    try:
        session = await get_aiosession()
        headers = aiohttp.BasicAuth(login, password)
        json_body = {"Command": "resume", "JobID": job_id}
        async with session.put(f"{settings.deadline_api_url}/jobs", json=json_body, auth=headers, ssl=False) as resp:
            success = resp.status == 200
            if not success:
                logger.error(f"Failed to resume job: {resp.status}")
            return success
    except Exception as e:
        logger.error(f"Error resuming job: {e}")
        return False


async def resume_job_by_user_id(telegram_user_id: int, job_id: str) -> bool:
    """
    Resume a suspended job using telegram user ID.
    
    Args:
        telegram_user_id: Telegram user ID
        job_id: Job ID
        
    Returns:
        True if successful, False otherwise
    """
    try:
        from app.auth import get_deadline_credentials
        credentials = await get_deadline_credentials(telegram_user_id)
        if not credentials:
            logger.error(f"No Deadline credentials found for user {telegram_user_id}")
            return False
        
        login, password = credentials
        return await resume_job(login, password, job_id)
    except Exception as e:
        logger.error(f"Error resuming job for user {telegram_user_id}: {e}")
        return False


async def suspend_job(login: str, password: str, job_id: str) -> bool:
    """
    Suspend a job.

    Args:
        login: User login
        password: User password
        job_id: Job ID

    Returns:
        True if successful, False otherwise
    """
    try:
        session = await get_aiosession()
        headers = aiohttp.BasicAuth(login, password)
        json_body = {"Command": "suspend", "JobID": job_id}
        async with session.put(f"{settings.deadline_api_url}/jobs", json=json_body, auth=headers, ssl=False) as resp:
            success = resp.status == 200
            if not success:
                logger.error(f"Failed to suspend job: {resp.status}")
            return success
    except Exception as e:
        logger.error(f"Error suspending job: {e}")
        return False


async def suspend_job_by_user_id(telegram_user_id: int, job_id: str) -> bool:
    """
    Suspend a job using telegram user ID.
    
    Args:
        telegram_user_id: Telegram user ID
        job_id: Job ID
        
    Returns:
        True if successful, False otherwise
    """
    try:
        from app.auth import get_deadline_credentials
        credentials = await get_deadline_credentials(telegram_user_id)
        if not credentials:
            logger.error(f"No Deadline credentials found for user {telegram_user_id}")
            return False
        
        login, password = credentials
        return await suspend_job(login, password, job_id)
    except Exception as e:
        logger.error(f"Error suspending job for user {telegram_user_id}: {e}")
        return False


async def delete_job(login: str, password: str, job_id: str) -> bool:
    """
    Delete a job.

    Args:
        login: User login
        password: User password
        job_id: Job ID

    Returns:
        True if successful, False otherwise
    """
    try:
        session = await get_aiosession()
        headers = aiohttp.BasicAuth(login, password)
        async with session.delete(f"{settings.deadline_api_url}/jobs?JobID={job_id}", auth=headers, ssl=False) as resp:
            success = resp.status == 200
            if not success:
                logger.error(f"Failed to delete job: {resp.status}")
            return success
    except Exception as e:
        logger.error(f"Error deleting job: {e}")
        return False


async def delete_job_by_user_id(telegram_user_id: int, job_id: str) -> bool:
    """
    Delete a job using telegram user ID.
    
    Args:
        telegram_user_id: Telegram user ID
        job_id: Job ID
        
    Returns:
        True if successful, False otherwise
    """
    try:
        from app.auth import get_deadline_credentials
        credentials = await get_deadline_credentials(telegram_user_id)
        if not credentials:
            logger.error(f"No Deadline credentials found for user {telegram_user_id}")
            return False
        
        login, password = credentials
        return await delete_job(login, password, job_id)
    except Exception as e:
        logger.error(f"Error deleting job for user {telegram_user_id}: {e}")
        return False

# ============================================================================
# === DROPBOX INTEGRATION FUNCTIONS ===
# ============================================================================

async def get_dropbox_session() -> aiohttp.ClientSession:
    """
    Get an active aiohttp session for Dropbox API calls.
    
    Returns:
        aiohttp.ClientSession: Active session instance
        
    Raises:
        RuntimeError: If unable to get a valid session
    """
    try:
        return await get_aiosession()
    except Exception as e:
        logger.error(f"Error getting Dropbox session: {e}")
        raise RuntimeError(f"Failed to get Dropbox session: {e}")

async def download_job_folder(login: str, password: str, job_id: str) -> Optional[List[Tuple[str, dict, Path]]]:
    """
    Get list of files to download from Dropbox.
    
    Args:
        login: User login
        password: User password
        job_id: Job ID
        
    Returns:
        Optional[List[Tuple[str, dict, Path]]]: List of (url, headers, local_path) tuples or None if error
    """
    try:
        import aiohttp
        from pathlib import Path
        from app.integrations.dropbox_helpers import (
            get_fresh_access_token, 
            fetch_dropbox_metadata
        )
        from app.core.config import settings
        
        # Get job info to find output directory
        job_info = await get_job_info(login, password, job_id)
        if not job_info:
            logger.error(f"Could not get job info for {job_id}")
            return None
            
        outdirs = job_info.get("OutDir", [])
        if not outdirs:
            logger.error(f"No OutDir found for job {job_id}")
            return None
            
        fullpath = outdirs[0]
        # Find root folder marker
        idx = fullpath.find(settings.dropbox_root_marker)
        if idx == -1:
            logger.error(f"Dropbox root marker not found in path: {fullpath}")
            return None
            
        trimmed = fullpath[idx:]
        dropbox_path = _normalize_dropbox_path(trimmed)
        if not dropbox_path:
            logger.error(f"Failed to normalize Dropbox path for job {job_id}: {trimmed}")
            return None
        
        # Create temp directory
        temp_dir = Path(settings.temp_dir)
        temp_dir.mkdir(exist_ok=True)
        
        # Prepare Dropbox headers
        headers_dbx = {
            "Authorization": f"Bearer {get_fresh_access_token()}",
            "Dropbox-API-Select-User": settings.dropbox_team_member_id,
            "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
            "Content-Type": "application/json"
        }
        
        session_dbx = await get_dropbox_session()
        # Get metadata
        metadata = await fetch_dropbox_metadata(session_dbx, dropbox_path, headers_dbx)
            
        if metadata.get(".tag") == "file":
            logger.error("File download not supported for preview")
            return None
        elif metadata.get(".tag") != "folder":
            logger.error("Unsupported metadata type")
            return None
                
        exr_folder_name = metadata["name"]
        # Use job_id to make the path unique
        local_root = temp_dir / f"{exr_folder_name}_{job_id}"
        local_root.mkdir(exist_ok=True)
            
        # List files in folder
        list_url = "https://api.dropboxapi.com/2/files/list_folder"
        async with session_dbx.post(list_url, headers=headers_dbx, json={"path": metadata["path_display"]}) as list_resp:
            if list_resp.status != 200:
                logger.error(f"Failed to list folder: {list_resp.status}")
                return None
            result = await list_resp.json()
        
        # Prepare file list for download
        download_url = "https://content.dropboxapi.com/2/files/download"
        file_list = []
            
        preview_exts = (".exr", ".jpg", ".jpeg", ".png")
        for entry in result.get("entries", []):
            name = entry["name"].lower()
            if "cryptomatte" in name or "conflicted copy" in name:
                continue
            if entry[".tag"] == "file" and name.endswith(preview_exts):
                api_args = {"path": entry["path_display"]}
                headers = {
                    "Authorization": f"Bearer {get_fresh_access_token()}",
                    "Dropbox-API-Select-User": settings.dropbox_team_member_id,
                    "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
                    "Dropbox-API-Arg": json.dumps(api_args)
                }
                local_path = local_root / entry["name"]
                file_list.append((download_url, headers, local_path))
        
        if not file_list:
            logger.error("No valid image files found in folder")
            return None
                
        logger.info(f"Found {len(file_list)} valid image files to download")
        return file_list
            
    except Exception as e:
        logger.error(f"Error preparing download list for job {job_id}: {e}")
        return None

def _sanitize_windows_filename(name: str) -> str:
    """Replace characters that are invalid in Windows file names."""
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def _normalize_dropbox_path(path: Optional[str]) -> Optional[str]:
    """Normalize Dropbox-style paths to start with a single leading slash."""
    if not path:
        return None
    normalized = path.replace("\\", "/").strip()
    if not normalized:
        return None
    return f"/{normalized.lstrip('/')}"





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
        return None

    login, password = credentials
    job_info = await get_job_info(login, password, job_id)
    if not job_info:
        logger.error("Could not get job info for %s", job_id)
        return None

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
        return None

    output_path = outdirs[0]
    idx = output_path.find(settings.dropbox_root_marker)
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

    dropbox_folder_normalized = _normalize_dropbox_path(dropbox_folder)
    if dropbox_folder_normalized:
        dropbox_parent = PurePosixPath(dropbox_folder_normalized).parent
        if str(dropbox_parent) in {"", "."}:
            expected_dropbox_video = video_filename
        else:
            expected_dropbox_video = str(dropbox_parent / video_filename)
    else:
        expected_dropbox_video = video_filename

    frames_str = props.get("Frames", "")
    start_match = re.search(r"-?\d+", frames_str)
    start_frame = int(start_match.group()) if start_match else 0

    fps_value = props.get("PlugInfo", {}).get("FPS")
    try:
        frame_rate = float(fps_value) if fps_value is not None else 25.0
    except (TypeError, ValueError):
        frame_rate = 25.0

    helper_script = Path(__file__).resolve().parents[2] / "scripts" / "deadline_preview_worker.py"
    if not helper_script.exists():
        logger.error("Preview helper script not found: %s", helper_script)
        return None

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
            return None
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
        return None

    preview_job_id = submission_response.get("job_id") or submission_response.get("_id")
    logger.info(
        "Submitted preview job %s for %s (command: %s, whitelist: %s)",
        preview_job_id,
        job_id,
        command_line,
        preferred_slaves,
    )

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
            "Authorization": f"Bearer {get_fresh_access_token()}",
            "Dropbox-API-Select-User": settings.dropbox_team_member_id,
            "Dropbox-API-Path-Root": {".tag": "root", "root": settings.dropbox_root_namespace_id},
            "Content-Type": "application/json"
        }

        normalized_hint = _normalize_dropbox_path(dropbox_path_hint)
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
        idx = fullpath.find(settings.dropbox_root_marker)
        if idx == -1:
            logger.error(f"Dropbox root marker not found in path: {fullpath}")
            return None

        trimmed = fullpath[idx:]
        dropbox_path = _normalize_dropbox_path(trimmed)
        if not dropbox_path:
            logger.error(f"Failed to normalize Dropbox path for job {job_id}: {trimmed}")
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
        import json
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
            "Authorization": f"Bearer {get_fresh_access_token()}",
            "Dropbox-API-Select-User": settings.dropbox_team_member_id,
            "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
            "Dropbox-API-Arg": json.dumps({"path": video_info["dropbox_path"]})
        }
        
        session_dbx = await get_dropbox_session()
        async with session_dbx.post(download_url, headers=dl_headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error(f"Error downloading video: {text}")
                return None
            
            # Use the original filename for local storage
            filename = video_info["filename"]
            temp_path = temp_dir / filename
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(temp_path, "wb") as f:
                data = await resp.read()
                f.write(data)
                
            logger.info(f"Video downloaded to {temp_path}")
            return (str(temp_path), video_info["dropbox_path"])
                
    except Exception as e:
        logger.error(f"Error downloading video for job {job_id}: {e}")
        return None 
