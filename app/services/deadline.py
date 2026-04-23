"""
Deadline API service functions.
"""

import asyncio
import json
import logging
import time
from typing import Optional, List, Dict, Any, Tuple, Union, Callable, Awaitable, TypeVar
from pathlib import Path

import aiohttp

from app.core.config import settings
from app.core.bot_core import get_aiosession

logger = logging.getLogger(__name__)

ALLOWED_WORKER_STATUSES = {0, 1, 2}
T = TypeVar("T")
_JOBS_CACHE_TTL_SECONDS = 5.0
_JOBS_CACHE_MAX_ENTRIES = 256
_jobs_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_jobs_cache_lock = asyncio.Lock()
_WORKERS_CACHE_TTL_SECONDS = 20.0
_WORKERS_CACHE_MAX_ENTRIES = 256
_workers_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_workers_cache_lock = asyncio.Lock()


class DeadlineSubmissionError(RuntimeError):
    """Raised when a Deadline job submission fails."""


class WorkerStatusError(RuntimeError):
    """Raised when preferred workers are not in an allowed status."""

    def __init__(self, invalid_workers: List[Dict[str, Any]], preferred_workers: List[str]):
        message = "One or more preferred workers are not in an allowed status."
        super().__init__(message)
        self.invalid_workers = invalid_workers
        self.preferred_workers = preferred_workers


async def _with_user_credentials(
    telegram_user_id: int,
    *,
    default: T,
    operation_name: str,
    call: Callable[[str, str], Awaitable[T]],
) -> T:
    """Resolve Deadline credentials for a Telegram user and run an operation."""
    from app.auth import get_deadline_credentials

    credentials = await get_deadline_credentials(telegram_user_id)
    if not credentials:
        logger.error("No Deadline credentials found for user %s", telegram_user_id)
        return default

    login, password = credentials
    try:
        return await call(login, password)
    except Exception as exc:
        logger.error(
            "Error during %s for user %s: %s",
            operation_name,
            telegram_user_id,
            exc,
        )
        return default


def _jobs_cache_key(login: str) -> str:
    return str(login or "").strip().lower()


def _workers_cache_key(login: str) -> str:
    return str(login or "").strip().lower()


def _prune_jobs_cache() -> None:
    now = time.monotonic()
    expired_keys = [
        key
        for key, (expires_at, _) in _jobs_cache.items()
        if expires_at <= now
    ]
    for key in expired_keys:
        _jobs_cache.pop(key, None)

    if len(_jobs_cache) <= _JOBS_CACHE_MAX_ENTRIES:
        return

    # Keep entries with the highest expiration timestamps.
    survivors = sorted(_jobs_cache.items(), key=lambda item: item[1][0], reverse=True)[
        :_JOBS_CACHE_MAX_ENTRIES
    ]
    _jobs_cache.clear()
    _jobs_cache.update(survivors)


def _prune_workers_cache() -> None:
    now = time.monotonic()
    expired_keys = [
        key
        for key, (expires_at, _) in _workers_cache.items()
        if expires_at <= now
    ]
    for key in expired_keys:
        _workers_cache.pop(key, None)

    if len(_workers_cache) <= _WORKERS_CACHE_MAX_ENTRIES:
        return

    survivors = sorted(_workers_cache.items(), key=lambda item: item[1][0], reverse=True)[
        :_WORKERS_CACHE_MAX_ENTRIES
    ]
    _workers_cache.clear()
    _workers_cache.update(survivors)


async def _fetch_jobs_by_credentials(
    login: str,
    password: str,
    *,
    use_cache: bool,
    force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    started_at = time.monotonic()
    cache_key = _jobs_cache_key(login)
    now = time.monotonic()

    if use_cache and not force_refresh:
        cached = _jobs_cache.get(cache_key)
        if cached and cached[0] > now:
            logger.info(
                "Jobs cache hit for login %s in %sms",
                login,
                int((time.monotonic() - started_at) * 1000),
            )
            return cached[1]

    logger.info("Jobs cache miss for login %s", login)

    session = await get_aiosession()
    auth = aiohttp.BasicAuth(login, password)
    request_started_at = time.monotonic()
    async with session.get(
        f"{settings.deadline_api_url}/jobs",
        auth=auth,
        ssl=settings.deadline_tls_verify,
    ) as resp:
        if resp.status != 200:
            response_text = await resp.text()
            logger.error("Failed to get jobs: %s, response: %s", resp.status, response_text)
            return []
        data = await resp.json()
        request_ms = int((time.monotonic() - request_started_at) * 1000)
        total_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            "Fetched jobs from Deadline for login %s in %sms (total=%sms, count=%s)",
            login,
            request_ms,
            total_ms,
            len(data) if isinstance(data, list) else "non-list",
        )
        if not isinstance(data, list):
            return []

    if use_cache:
        async with _jobs_cache_lock:
            expires_at = time.monotonic() + _JOBS_CACHE_TTL_SECONDS
            _jobs_cache[cache_key] = (expires_at, data)
            _prune_jobs_cache()
    return data


def _invalidate_jobs_cache(login: str) -> None:
    _jobs_cache.pop(_jobs_cache_key(login), None)


def _invalidate_workers_cache(login: str) -> None:
    _workers_cache.pop(_workers_cache_key(login), None)


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
    async def _op(login: str, password: str) -> List[Dict[str, Any]]:
        logger.info("Requesting jobs for user %s", telegram_user_id)
        return await _fetch_jobs_by_credentials(
            login,
            password,
            use_cache=True,
        )

    return await _with_user_credentials(
        telegram_user_id,
        default=[],
        operation_name="get jobs",
        call=_op,
    )


async def get_jobs_by_credentials(
    login: str,
    password: str,
    *,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    """Return Deadline jobs for provided credentials (cached by login by default)."""
    return await _fetch_jobs_by_credentials(
        login,
        password,
        use_cache=use_cache,
        force_refresh=force_refresh,
    )


async def _fetch_workers(
    login: str,
    password: str,
    *,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    """Fetch workers list using provided Deadline credentials."""
    cache_key = _workers_cache_key(login)
    now = time.monotonic()
    if use_cache and not force_refresh:
        cached = _workers_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

    try:
        session = await get_aiosession()
        headers = aiohttp.BasicAuth(login, password)
        async with session.get(f"{settings.deadline_api_url}/slaves?Data=infosettings", auth=headers, ssl=settings.deadline_tls_verify) as resp:
            logger.info(f"Slaves API response status: {resp.status}")
            if resp.status == 200:
                data = await resp.json()
                logger.info(f"Slaves API returned {len(data) if isinstance(data, list) else 'non-list'} items")
                if isinstance(data, list) and use_cache:
                    async with _workers_cache_lock:
                        expires_at = time.monotonic() + _WORKERS_CACHE_TTL_SECONDS
                        _workers_cache[cache_key] = (expires_at, data)
                        _prune_workers_cache()
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
    async def _op(login: str, password: str) -> List[Dict[str, Any]]:
        logger.info("Requesting slaves for user %s with login %s", telegram_user_id, login)
        return await _fetch_workers(login, password)

    return await _with_user_credentials(
        telegram_user_id,
        default=[],
        operation_name="get workers",
        call=_op,
    )


async def get_workers_by_credentials(login: str, password: str) -> List[Dict[str, Any]]:
    """Return list of workers using raw credentials."""
    return await _fetch_workers(login, password)


async def get_worker_infosettings(
    login: str,
    password: str,
    worker_names: List[str],
) -> List[Dict[str, Any]]:
    """Fetch InfoSettings for one or more workers by name."""
    filtered_names: List[str] = []
    seen: set[str] = set()
    for name in worker_names:
        if not name:
            continue
        normalized = str(name).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        filtered_names.append(normalized)

    if not filtered_names:
        return []

    params: List[Tuple[str, str]] = [("Data", "infosettings")]
    for name in filtered_names:
        params.append(("Name", name))

    try:
        session = await get_aiosession()
        auth = aiohttp.BasicAuth(login, password)
        async with session.get(
            f"{settings.deadline_api_url}/slaves",
            params=params,
            auth=auth,
            ssl=settings.deadline_tls_verify,
        ) as resp:
            if resp.status != 200:
                response_text = await resp.text()
                logger.error(
                    "Failed to get worker infosettings: %s, response: %s",
                    resp.status,
                    response_text,
                )
                return []

            data = await resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                workers = data.get("Workers")
                if isinstance(workers, list):
                    return workers
                return [data]
            return []
    except Exception as exc:
        logger.error("Error getting worker infosettings: %s", exc)
        return []


async def get_worker_infosettings_by_user_id(
    telegram_user_id: int,
    worker_name: str,
) -> Optional[Dict[str, Any]]:
    """Fetch worker InfoSettings by Telegram user context."""

    async def _op(login: str, password: str) -> Optional[Dict[str, Any]]:
        entries = await get_worker_infosettings(login, password, [worker_name])
        if not entries:
            return None
        return entries[0]

    return await _with_user_credentials(
        telegram_user_id,
        default=None,
        operation_name=f"get worker infosettings ({worker_name})",
        call=_op,
    )


async def save_worker_settings(
    login: str,
    password: str,
    worker_settings: Dict[str, Any],
) -> bool:
    """Save worker settings via Deadline /slaves Command=savesettings."""
    try:
        session = await get_aiosession()
        auth = aiohttp.BasicAuth(login, password)
        payload = {
            "Command": "savesettings",
            "SlaveSettings": worker_settings,
        }
        async with session.put(
            f"{settings.deadline_api_url}/slaves",
            json=payload,
            auth=auth,
            ssl=settings.deadline_tls_verify,
        ) as resp:
            response_text = await resp.text()
            if resp.status != 200:
                logger.error(
                    "Failed to save worker settings: %s, response: %s",
                    resp.status,
                    response_text,
                )
                return False
            _invalidate_workers_cache(login)
            return True
    except Exception as exc:
        logger.error("Error saving worker settings: %s", exc)
        return False


async def save_worker_settings_by_user_id(
    telegram_user_id: int,
    worker_settings: Dict[str, Any],
) -> bool:
    """Save worker settings using Telegram user credentials."""
    return await _with_user_credentials(
        telegram_user_id,
        default=False,
        operation_name="save worker settings",
        call=lambda login, password: save_worker_settings(login, password, worker_settings),
    )


async def get_job_info_direct(login: str, password: str, job_id: str) -> Optional[Dict[str, Any]]:
    """Fetch one job by id using a targeted Deadline query, fallback to cached list lookup."""
    session = await get_aiosession()
    auth = aiohttp.BasicAuth(login, password)
    try:
        async with session.get(
            f"{settings.deadline_api_url}/jobs",
            params={"JobID": job_id},
            auth=auth,
            ssl=settings.deadline_tls_verify,
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, dict):
                    if data.get("_id") == job_id:
                        return data
                    jobs = data.get("Jobs")
                    if isinstance(jobs, list):
                        match = next((job for job in jobs if job.get("_id") == job_id), None)
                        if match is not None:
                            return match
                elif isinstance(data, list):
                    match = next((job for job in data if job.get("_id") == job_id), None)
                    if match is not None:
                        return match
                    if len(data) == 1 and isinstance(data[0], dict):
                        return data[0]
            else:
                text = await resp.text()
                logger.debug("Direct job query failed for %s: %s %s", job_id, resp.status, text)
    except Exception as exc:
        logger.debug("Direct job query exception for %s: %s", job_id, exc)

    return await get_job_info(login, password, job_id)


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


async def get_job_reports(
    login: str,
    password: str,
    job_id: str,
    *,
    report_data: str = "error",
) -> List[Dict[str, Any]]:
    """Fetch Deadline job reports for a specific job and report type."""
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return []
    normalized_report_data = str(report_data or "").strip().lower() or "error"

    try:
        session = await get_aiosession()
        auth = aiohttp.BasicAuth(login, password)
        async with session.get(
            f"{settings.deadline_api_url}/jobreports",
            params={"Data": normalized_report_data, "JobID": normalized_job_id},
            auth=auth,
            ssl=settings.deadline_tls_verify,
        ) as resp:
            raw_text = await resp.text()
            if resp.status != 200:
                logger.error(
                    "Failed to fetch job reports (%s) for %s: %s, response: %s",
                    normalized_report_data,
                    normalized_job_id,
                    resp.status,
                    raw_text,
                )
                return []
            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError as decode_error:
                logger.error(
                    "Job reports (%s) returned invalid JSON for %s: %s (error: %s)",
                    normalized_report_data,
                    normalized_job_id,
                    raw_text[:200],
                    decode_error,
                )
                return []
    except Exception as exc:
        logger.error(
            "Error fetching job reports (%s) for %s: %s",
            normalized_report_data,
            normalized_job_id,
            exc,
        )
        return []

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in ("Reports", "JobReports", "Data", "results", "Items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if {"Job", "Title", "Type"} & set(payload.keys()):
            return [payload]

    return []


async def get_job_error_reports(
    login: str,
    password: str,
    job_id: str,
) -> List[Dict[str, Any]]:
    """Fetch Deadline job error reports for a specific job."""
    return await get_job_reports(
        login,
        password,
        job_id,
        report_data="error",
    )


async def get_job_report_contents(
    login: str,
    password: str,
    job_id: str,
    report_id: str,
) -> Optional[str]:
    """Fetch the full contents of a single Deadline job error report."""
    normalized_job_id = str(job_id or "").strip()
    normalized_report_id = str(report_id or "").strip()
    if not normalized_job_id or not normalized_report_id:
        return None

    try:
        session = await get_aiosession()
        auth = aiohttp.BasicAuth(login, password)
        async with session.get(
            f"{settings.deadline_api_url}/jobreports",
            params={
                "Data": "errorcontents",
                "JobID": normalized_job_id,
                "ReportID": normalized_report_id,
            },
            auth=auth,
            ssl=settings.deadline_tls_verify,
        ) as resp:
            raw_text = await resp.text()
            if resp.status != 200:
                logger.error(
                    "Failed to fetch job report contents for %s/%s: %s, response: %s",
                    normalized_job_id,
                    normalized_report_id,
                    resp.status,
                    raw_text[:400],
                )
                return None
    except Exception as exc:
        logger.error(
            "Error fetching job report contents for %s/%s: %s",
            normalized_job_id,
            normalized_report_id,
            exc,
        )
        return None

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        payload = raw_text

    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("Contents", "ReportContents", "Content", "Text", "Log"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value

    return raw_text or None


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
        jobs = await _fetch_jobs_by_credentials(login, password, use_cache=True)
        match = next((job for job in jobs if job.get("_id") == job_id), None)
        if match is not None:
            return match

        # Cache may be stale for a few seconds; refresh once before failing.
        fresh_jobs = await _fetch_jobs_by_credentials(
            login,
            password,
            use_cache=True,
            force_refresh=True,
        )
        match = next((job for job in fresh_jobs if job.get("_id") == job_id), None)
        if match is not None:
            return match

        logger.error("Job %s not found in jobs list", job_id)
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
    return await _with_user_credentials(
        telegram_user_id,
        default=None,
        operation_name=f"get job info ({job_id})",
        call=lambda login, password: get_job_info(login, password, job_id),
    )


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
        async with session.get(
            f"{settings.deadline_api_url}/tasks",
            params={"JobID": job_id},
            auth=headers,
            ssl=settings.deadline_tls_verify,
        ) as resp:
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
    return await _with_user_credentials(
        telegram_user_id,
        default=[],
        operation_name=f"get job tasks ({job_id})",
        call=lambda login, password: get_job_tasks(login, password, job_id),
    )


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

    session = await get_aiosession()
    auth = aiohttp.BasicAuth(login, password)
    async with session.post(
        submit_url,
        auth=auth,
        ssl=settings.deadline_tls_verify,
        json=payload,
    ) as resp:
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
        for local_path, remote_name in resolved_aux:
            upload_url = f"{settings.deadline_api_url}/jobs/{job_id}/aux-files/{remote_name}"
            logger.info("Uploading aux file %s -> %s", local_path, upload_url)
            payload_bytes = await asyncio.to_thread(local_path.read_bytes)
            async with session.put(
                upload_url,
                auth=auth,
                ssl=settings.deadline_tls_verify,
                data=payload_bytes,
                headers={"Content-Type": "application/octet-stream"},
            ) as upload_resp:
                if upload_resp.status not in (200, 201, 204):
                    text = await upload_resp.text()
                    logger.error(
                        "Failed to upload aux file %s (%s): %s",
                        remote_name,
                        upload_resp.status,
                        text,
                    )
                    raise DeadlineSubmissionError(
                        f"Aux file upload failed for {remote_name} ({upload_resp.status})"
                    )

        if complete_submission:
            complete_url = f"{settings.deadline_api_url}/jobs/{job_id}/complete-submission"
            async with session.post(
                complete_url,
                auth=auth,
                ssl=settings.deadline_tls_verify,
            ) as comp_resp:
                if comp_resp.status not in (200, 201, 204):
                    text = await comp_resp.text()
                    logger.error(
                        "Failed to complete submission for job %s (%s): %s",
                        job_id,
                        comp_resp.status,
                        text,
                    )
                    raise DeadlineSubmissionError(
                        f"Complete submission failed ({comp_resp.status})"
                    )

    _invalidate_jobs_cache(login)

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
            else:
                _invalidate_jobs_cache(login)
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
    return await _with_user_credentials(
        telegram_user_id,
        default=False,
        operation_name=f"requeue job ({job_id})",
        call=lambda login, password: requeue_job(login, password, job_id),
    )


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
            else:
                _invalidate_jobs_cache(login)
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
    return await _with_user_credentials(
        telegram_user_id,
        default=False,
        operation_name=f"resume job ({job_id})",
        call=lambda login, password: resume_job(login, password, job_id),
    )


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
            else:
                _invalidate_jobs_cache(login)
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
    return await _with_user_credentials(
        telegram_user_id,
        default=False,
        operation_name=f"suspend job ({job_id})",
        call=lambda login, password: suspend_job(login, password, job_id),
    )


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
        async with session.delete(
            f"{settings.deadline_api_url}/jobs",
            params={"JobID": job_id},
            auth=headers,
            ssl=settings.deadline_tls_verify,
        ) as resp:
            success = resp.status == 200
            if not success:
                logger.error(f"Failed to delete job: {resp.status}")
            else:
                _invalidate_jobs_cache(login)
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
    return await _with_user_credentials(
        telegram_user_id,
        default=False,
        operation_name=f"delete job ({job_id})",
        call=lambda login, password: delete_job(login, password, job_id),
    )

# ============================================================================
