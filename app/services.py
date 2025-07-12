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
            async with session.get(f"{settings.base_api_url}/jobs/{job_id}", auth=headers, ssl=False) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logger.error(f"Failed to get job info: {resp.status}")
                    return None
    except Exception as e:
        logger.error(f"Error getting job info: {e}")
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
        from app.dropbox_helpers import download_exr_folder, get_fresh_access_token
        from app.core.config import settings
        import aiohttp
        from pathlib import Path
        
        # Implementation would go here
        logger.info(f"Download job folder {job_id} - integration with dropbox_helpers")
        return None
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
        from app.video_helpers import convert_exr_folder_to_srgb, assemble_video_from_exr
        from app.core.config import settings
        from pathlib import Path
        
        # Implementation would go here
        logger.info(f"Create video from job {job_id} - integration with video_helpers")
        return None
    except Exception as e:
        logger.error(f"Error creating video from job {job_id}: {e}")
        return None 