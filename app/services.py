# app/services.py
"""
API service functions for Deadline and Dropbox integration.

This module provides functions for interacting with the Deadline API
and Dropbox API for job management, worker monitoring, and file operations.
"""

import json
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Union

import aiohttp
from aiogram.types import FSInputFile

from app.core.config import settings
from app.dropbox_helpers import (
    get_fresh_access_token,
    fetch_dropbox_metadata,
    download_exr_folder,
    upload_video_to_dropbox
)
from app.video_helpers import convert_exr_folder_to_srgb_optimized, assemble_video_from_jpg
from app.core.bot_core import get_aiosession

logger = logging.getLogger(__name__)

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
        
        async with aiohttp.ClientSession() as session:
            headers = aiohttp.BasicAuth(login, password)
            async with session.get(f"{settings.base_api_url}/jobs", auth=headers, ssl=False) as resp:
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
        
        async with aiohttp.ClientSession() as session:
            headers = aiohttp.BasicAuth(login, password)
            async with session.get(f"{settings.base_api_url}/slaves?Data=infosettings", auth=headers, ssl=False) as resp:
                logger.info(f"Slaves API response status: {resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"Slaves API returned {len(data) if isinstance(data, list) else 'non-list'} items")
                    return data
                else:
                    response_text = await resp.text()
                    logger.error(f"Failed to get slaves: {resp.status}, response: {response_text}")
                    return []
    except Exception as e:
        logger.error(f"Error getting slaves: {e}")
        return []


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
        async with aiohttp.ClientSession() as session:
            headers = aiohttp.BasicAuth(login, password)
            # Get all jobs and find the specific one
            async with session.get(f"{settings.base_api_url}/jobs", auth=headers, ssl=False) as resp:
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
        async with aiohttp.ClientSession() as session:
            headers = aiohttp.BasicAuth(login, password)
            async with session.get(f"{settings.base_api_url}/tasks?JobID={job_id}", auth=headers, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # API возвращает {"Tasks": [...]} или просто список
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
        async with aiohttp.ClientSession() as session:
            headers = aiohttp.BasicAuth(login, password)
            json_body = {"Command": "requeue", "JobID": job_id}
            async with session.put(f"{settings.base_api_url}/jobs", json=json_body, auth=headers, ssl=False) as resp:
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
        async with aiohttp.ClientSession() as session:
            headers = aiohttp.BasicAuth(login, password)
            json_body = {"Command": "resume", "JobID": job_id}
            async with session.put(f"{settings.base_api_url}/jobs", json=json_body, auth=headers, ssl=False) as resp:
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
        async with aiohttp.ClientSession() as session:
            headers = aiohttp.BasicAuth(login, password)
            json_body = {"Command": "suspend", "JobID": job_id}
            async with session.put(f"{settings.base_api_url}/jobs", json=json_body, auth=headers, ssl=False) as resp:
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
        async with aiohttp.ClientSession() as session:
            headers = aiohttp.BasicAuth(login, password)
            async with session.delete(f"{settings.base_api_url}/jobs?JobID={job_id}", auth=headers, ssl=False) as resp:
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
        from app.dropbox_helpers import (
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
        dropbox_path = "/" + trimmed.replace("\\", "/").lstrip("/")
        
        # Create temp directory
        temp_dir = Path("temp")
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
            
        for entry in result.get("entries", []):
            name = entry["name"].lower()
            if "cryptomatte" in name or "conflicted copy" in name:
                continue
            if entry[".tag"] == "file" and name.endswith(".exr"):
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
            logger.error("No valid EXR files found in folder")
            return None
                
        logger.info(f"Found {len(file_list)} valid EXR files to download")
        return file_list
            
    except Exception as e:
        logger.error(f"Error preparing download list for job {job_id}: {e}")
        return None

async def create_video_from_job(login: str, password: str, job_id: str) -> Optional[Tuple[str, str]]:
    """
    Create video from job's EXR files and upload it to Dropbox.
    
    Returns:
        Optional[Tuple[str, str]]: Tuple of (video_path, dropbox_path) if successful, None otherwise
    """
    try:
        # Get job info from Deadline
        job_info = await get_job_info(login, password, job_id)
        if not job_info:
            logger.error(f"Could not get job info for {job_id}")
            return None
            
        # Get output path from job info
        outdirs = job_info.get("OutDir", [])
        if not outdirs:
            logger.error(f"No OutDir found for job {job_id}")
            return None
            
        # Get first output directory
        output_path = outdirs[0]
        
        # Find root folder marker
        from app.core.config import settings
        idx = output_path.find(settings.dropbox_root_marker)
        if idx == -1:
            logger.error(f"Dropbox root marker not found in path: {output_path}")
            return None
            
        trimmed = output_path[idx:]
        dropbox_path = "/" + trimmed.replace("\\", "/").lstrip("/")
            
        # Create temp directory for job
        from pathlib import Path
        temp_root = Path("temp")
        conv_root = Path("conv")
        temp_root.mkdir(exist_ok=True)
        conv_root.mkdir(exist_ok=True)
        
        # Get folder name from path
        exr_folder_name = Path(dropbox_path).parts[-1]  # Use parts[-1] instead of name
        if not exr_folder_name:
            exr_folder_name = job_id  # Fallback to job ID
            
        # Create job-specific directories
        job_temp_dir = temp_root / f"{exr_folder_name}_{job_id}"
        job_conv_dir = conv_root / f"{exr_folder_name}_{job_id}"
        job_conv_dir.mkdir(exist_ok=True)
        
        # Get list of files to download
        file_list = await download_job_folder(login, password, job_id)
        if not file_list:
            logger.error(f"Failed to get file list for job {job_id}")
            return None
        
        # Convert files
        await convert_exr_folder_to_srgb_optimized(file_list, job_conv_dir, "config.ocio")
        
        # Create video from converted files
        video_path = assemble_video_from_jpg(job_conv_dir, str(exr_folder_name))  # Convert exr_folder_name to string
        if not video_path:
            logger.error("Failed to create video")
            return None
            
        # Get metadata for upload
        headers_dbx = {
            "Authorization": f"Bearer {get_fresh_access_token()}",
            "Dropbox-API-Select-User": settings.dropbox_team_member_id,
            "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
            "Content-Type": "application/json"
        }
        
        session_dbx = await get_dropbox_session()
        async with session_dbx.post(f"{settings.base_api_url}/files/upload", headers=headers_dbx, data=FSInputFile(Path(video_path))) as resp:
            if resp.status != 200:
                logger.error(f"Failed to upload video to Dropbox: {resp.status}")
                return None
                
            # Get the final path from the response headers
            dropbox_video_path = resp.headers.get("X-Dropbox-Path")
            if not dropbox_video_path:
                logger.error("Dropbox upload response missing X-Dropbox-Path header")
                return None
                
            logger.info(f"Video uploaded to Dropbox: {dropbox_video_path}")
            return str(video_path), dropbox_video_path
        
    except Exception as e:
        logger.error(f"Error creating video from job {job_id}: {e}")
        return None


async def check_video_exists_in_dropbox(login: str, password: str, job_id: str) -> Optional[Dict[str, Any]]:
    """
    Check if video already exists in Dropbox for a job.
    
    Args:
        login: User login
        password: User password
        job_id: Job ID
        
    Returns:
        Video info dict if exists, None otherwise
    """
    try:
        import aiohttp
        from pathlib import PurePosixPath
        from app.dropbox_helpers import get_fresh_access_token, fetch_dropbox_metadata
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
        dropbox_path = "/" + trimmed.replace("\\", "/").lstrip("/")
        
        headers_dbx = {
            "Authorization": f"Bearer {get_fresh_access_token()}",
            "Dropbox-API-Select-User": settings.dropbox_team_member_id,
            "Dropbox-API-Path-Root": {".tag": "root", "root": settings.dropbox_root_namespace_id},
            "Content-Type": "application/json"
        }
        
        session_dbx = await get_dropbox_session()
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


async def download_video_from_dropbox(login: str, password: str, job_id: str) -> Optional[tuple[str, str]]:
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
        from app.dropbox_helpers import get_fresh_access_token
        from app.core.config import settings
        
        # Check if video exists
        video_info = await check_video_exists_in_dropbox(login, password, job_id)
        if not video_info:
            logger.error(f"Video not found in Dropbox for job {job_id}")
            return None
            
        # Create temp directory
        temp_dir = Path("temp")
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