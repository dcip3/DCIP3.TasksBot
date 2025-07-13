# app/services.py
"""
API service functions for Deadline and Dropbox integration.

This module provides functions for interacting with the Deadline API
and Dropbox API for job management, worker monitoring, and file operations.
"""

import logging
import aiohttp
from typing import List, Dict, Any, Optional
from app.core.config import settings
import json

logger = logging.getLogger(__name__)

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

async def download_job_folder(login: str, password: str, job_id: str) -> Optional[str]:
    """
    Download job folder from Dropbox.
    
    Args:
        login: User login
        password: User password
        job_id: Job ID
        
    Returns:
        Local path to downloaded folder or None if error
    """
    try:
        import aiohttp
        from pathlib import Path
        from app.dropbox_helpers import (
            download_exr_folder, get_fresh_access_token, 
            fetch_dropbox_metadata, count_exr_files
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
        
        headers_dbx = {
            "Authorization": f"Bearer {get_fresh_access_token()}",
            "Dropbox-API-Select-User": settings.dropbox_team_member_id,
            "Dropbox-API-Path-Root": {".tag": "root", "root": settings.dropbox_root_namespace_id},
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session_dbx:
            # Get metadata
            metadata = await fetch_dropbox_metadata(session_dbx, dropbox_path, headers_dbx)
            
            if metadata.get(".tag") == "file":
                logger.error("File download not supported for preview")
                return None
            elif metadata.get(".tag") != "folder":
                logger.error("Unsupported metadata type")
                return None
                
            exr_folder_name = metadata["name"]
            local_root = temp_dir / exr_folder_name
            
            # Check if files already exist
            exr_files_exist = lambda folder: folder.exists() and any(str(f).endswith(".exr") for f in folder.glob("*.exr"))
            if exr_files_exist(local_root):
                logger.info(f"Files already downloaded for job {job_id}")
                return str(local_root)
            
            # Download files
            download_url = "https://content.dropboxapi.com/2/files/download"
            total_files = await count_exr_files(session_dbx, metadata["path_display"], headers_dbx)
            
            # Create download state for progress tracking
            download_states = {
                job_id: {
                    "total_files": total_files,
                    "downloaded_count": 0,
                    "progress_msg": None,
                    "stop_kb": None
                }
            }
            stop_downloads = {}
            
            local_root.mkdir(exist_ok=True)
            await download_exr_folder(
                session_dbx, download_url, headers_dbx, 
                metadata["path_display"], local_root, job_id, 
                download_states, stop_downloads
            )
            
            return str(local_root)
            
    except Exception as e:
        logger.error(f"Error downloading job folder {job_id}: {e}")
        return None


async def create_video_from_job(login: str, password: str, job_id: str) -> Optional[str]:
    """
    Create video from job files.
    
    Args:
        login: User login
        password: User password
        job_id: Job ID
        
    Returns:
        Path to created video file or None if error
    """
    try:
        import aiohttp
        from pathlib import Path
        from app.video_helpers import convert_exr_folder_to_srgb, assemble_video_from_exr
        from app.dropbox_helpers import (
            get_fresh_access_token, fetch_dropbox_metadata, 
            upload_video_to_dropbox
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
        
        # Create directories
        temp_dir = Path("temp")
        conv_dir = Path("conv")
        temp_dir.mkdir(exist_ok=True)
        conv_dir.mkdir(exist_ok=True)
        
        headers_dbx = {
            "Authorization": f"Bearer {get_fresh_access_token()}",
            "Dropbox-API-Select-User": settings.dropbox_team_member_id,
            "Dropbox-API-Path-Root": {".tag": "root", "root": settings.dropbox_root_namespace_id},
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session_dbx:
            # Get metadata
            metadata = await fetch_dropbox_metadata(session_dbx, dropbox_path, headers_dbx)
            
            if metadata.get(".tag") == "file":
                logger.error("File processing not supported for preview")
                return None
            elif metadata.get(".tag") != "folder":
                logger.error("Unsupported metadata type")
                return None
                
            exr_folder_name = metadata["name"]
            local_root = temp_dir / exr_folder_name
            conv_root = conv_dir / exr_folder_name
            video_path = conv_root / f"{exr_folder_name}.mp4"
            
            # Check if video already exists
            if video_path.exists():
                logger.info(f"Video already exists for job {job_id}")
                return str(video_path)
            
            # Check if files already exist
            exr_files_exist = lambda folder: folder.exists() and any(str(f).endswith(".exr") for f in folder.glob("*.exr"))
            if not exr_files_exist(local_root):
                logger.error(f"No EXR files found in {local_root}")
                return None
            
            # Convert EXR files from ACES to sRGB
            logger.info(f"Converting EXR files for job {job_id}")
            convert_exr_folder_to_srgb(local_root, conv_root, "config.ocio")
            
            # Assemble video from converted EXR files
            logger.info(f"Assembling video for job {job_id}")
            video_path = assemble_video_from_exr(conv_root, exr_folder_name)
            
            # Upload to Dropbox
            logger.info(f"Uploading video to Dropbox for job {job_id}")
            dropbox_path = await upload_video_to_dropbox(video_path, metadata)
            logger.info(f"Video uploaded to Dropbox: {dropbox_path}")
            
            return str(video_path)
            
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
        
        async with aiohttp.ClientSession() as session_dbx:
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


async def download_video_from_dropbox(login: str, password: str, job_id: str) -> Optional[str]:
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
        
        async with aiohttp.ClientSession() as session_dbx:
            async with session_dbx.post(download_url, headers=dl_headers) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Error downloading video: {text}")
                    return None
                    
                temp_path = temp_dir / video_info["filename"]
                temp_path.parent.mkdir(parents=True, exist_ok=True)
                
                with open(temp_path, "wb") as f:
                    data = await resp.read()
                    f.write(data)
                    
                logger.info(f"Video downloaded to {temp_path}")
                return str(temp_path)
                
    except Exception as e:
        logger.error(f"Error downloading video for job {job_id}: {e}")
        return None 