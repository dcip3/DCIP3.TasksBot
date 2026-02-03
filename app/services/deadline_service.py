"""
Deadline API service functions.
"""

from typing import Optional, List, Dict, Any, Tuple, Union
from pathlib import Path
import json
import logging

import aiohttp

from app.core.config import settings
from app.core.bot_core import get_aiosession

logger = logging.getLogger(__name__)

ALLOWED_WORKER_STATUSES = {0, 1, 2}


class DeadlineSubmissionError(RuntimeError):
    """Raised when a Deadline job submission fails."""


class WorkerStatusError(RuntimeError):
    """Raised when preferred workers are not in an allowed status."""

    def __init__(self, invalid_workers: List[Dict[str, Any]], preferred_workers: List[str]):
        message = "One or more preferred workers are not in an allowed status."
        super().__init__(message)
        self.invalid_workers = invalid_workers
        self.preferred_workers = preferred_workers


# ==========================================================================
# === DEADLINE API FUNCTIONS ===
# ==========================================================================
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
        async with session.get(f"{settings.deadline_api_url}/jobs", auth=headers, ssl=settings.deadline_tls_verify) as resp:
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
        async with session.get(f"{settings.deadline_api_url}/slaves?Data=infosettings", auth=headers, ssl=settings.deadline_tls_verify) as resp:
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
    async with session.get(url, params=params, auth=auth, ssl=settings.deadline_tls_verify) as resp:
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
        async with session.get(f"{settings.deadline_api_url}/jobs", auth=headers, ssl=settings.deadline_tls_verify) as resp:
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
        async with session.get(f"{settings.deadline_api_url}/tasks?JobID={job_id}", auth=headers, ssl=settings.deadline_tls_verify) as resp:
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
        async with session.post(submit_url, auth=auth, ssl=settings.deadline_tls_verify, json=payload) as resp:
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
                    ssl=settings.deadline_tls_verify,
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
                async with session.post(complete_url, auth=auth, ssl=settings.deadline_tls_verify) as comp_resp:
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
        async with session.put(f"{settings.deadline_api_url}/jobs", json=json_body, auth=headers, ssl=settings.deadline_tls_verify) as resp:
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
        async with session.put(f"{settings.deadline_api_url}/jobs", json=json_body, auth=headers, ssl=settings.deadline_tls_verify) as resp:
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
        async with session.put(f"{settings.deadline_api_url}/jobs", json=json_body, auth=headers, ssl=settings.deadline_tls_verify) as resp:
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
        async with session.delete(f"{settings.deadline_api_url}/jobs?JobID={job_id}", auth=headers, ssl=settings.deadline_tls_verify) as resp:
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
