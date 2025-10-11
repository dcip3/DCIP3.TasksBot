# app/integrations/api_routes.py
"""
API routes for Telegram Mini App integration.

This module provides REST API endpoints for the mini app to interact with
the bot's functionality including authentication, job management, and worker monitoring.
"""

import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel
import hashlib
import hmac
import json
import urllib.parse

from app.auth import authenticate_user, is_authorized, logout_user, get_deadline_credentials
from app.core.config import settings
from app.services import (
    get_jobs_list, get_workers_list, get_job_info, get_job_tasks,
    requeue_job, resume_job, suspend_job, delete_job,
    download_job_folder, create_video_from_job, WorkerStatusError
)

logger = logging.getLogger(__name__)

# Create router
router = APIRouter()

# Development mode - set to True for testing without Telegram WebApp
DEV_MODE = True

# Pydantic models
class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    success: bool
    user: Optional[Dict[str, Any]] = None
    message: Optional[str] = None

class AuthResponse(BaseModel):
    authenticated: bool
    user: Optional[Dict[str, Any]] = None

# Telegram WebApp validation
def validate_telegram_init_data(init_data: str = Header(None)) -> Optional[int]:
    """
    Validate Telegram WebApp init data and extract user ID.
    
    Args:
        init_data: Telegram init data from header
        
    Returns:
        Telegram user ID if valid, None otherwise
    """
    if DEV_MODE:
        # In development mode, return a test user ID
        logger.info("Development mode: using test user ID")
        return 123456789
    
    if not init_data:
        return None
    
    try:
        # Parse init data
        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        
        # Extract user data
        user_str = parsed_data.get('user', '{}')
        user_data = json.loads(user_str)
        user_id = user_data.get('id')
        
        if not user_id:
            return None
            
        # TODO: Add proper signature validation
        # For now, we'll trust the init data
        return user_id
        
    except Exception as e:
        logger.error(f"Error validating init data: {e}")
        return None

# Dependency for authenticated users
async def get_current_user(telegram_user_id: Optional[int] = Depends(validate_telegram_init_data)):
    if not telegram_user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    if not await is_authorized(telegram_user_id):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    return telegram_user_id

# Authentication endpoints
@router.post("/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest, telegram_user_id: Optional[int] = Depends(validate_telegram_init_data)):
    """Login user with Deadline credentials"""
    if not telegram_user_id:
        raise HTTPException(status_code=400, detail="Telegram user ID required")
    
    try:
        success = await authenticate_user(request.username, request.password, telegram_user_id)
        
        if success:
            # Save credentials for future use
            from app.auth import save_deadline_credentials
            await save_deadline_credentials(telegram_user_id, request.username, request.password)
            
            # Get user info from Telegram
            user_info = {
                "id": telegram_user_id,
                "username": request.username,
                "deadline_user": request.username
            }
            
            return LoginResponse(
                success=True,
                user=user_info,
                message="Successfully authenticated"
            )
        else:
            return LoginResponse(
                success=False,
                message="Invalid credentials"
            )
            
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@router.post("/auth/logout")
async def logout(current_user: int = Depends(get_current_user)):
    """Logout current user"""
    try:
        await logout_user(current_user)
        return {"success": True, "message": "Logged out successfully"}
    except Exception as e:
        logger.error(f"Logout error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@router.get("/auth/check", response_model=AuthResponse)
async def check_auth(current_user: int = Depends(get_current_user)):
    """Check if user is authenticated"""
    try:
        credentials = await get_deadline_credentials(current_user)
        if credentials:
            user_info = {
                "id": current_user,
                "username": credentials[0],
                "deadline_user": credentials[0]
            }
            return AuthResponse(authenticated=True, user=user_info)
        else:
            return AuthResponse(authenticated=False)
    except Exception as e:
        logger.error(f"Auth check error: {e}")
        return AuthResponse(authenticated=False)

# Jobs endpoints
@router.get("/jobs")
async def get_jobs(current_user: int = Depends(get_current_user)):
    """Get list of jobs"""
    try:
        jobs = await get_jobs_list(current_user)
        return jobs
    except Exception as e:
        logger.error(f"Error getting jobs: {e}")
        raise HTTPException(status_code=500, detail="Failed to get jobs")

@router.get("/jobs/{job_id}")
async def get_job_details(job_id: str, current_user: int = Depends(get_current_user)):
    """Get job details"""
    try:
        credentials = await get_deadline_credentials(current_user)
        if not credentials:
            raise HTTPException(status_code=401, detail="No credentials found")
        
        login, password = credentials
        job_info = await get_job_info(login, password, job_id)
        
        if not job_info:
            raise HTTPException(status_code=404, detail="Job not found")
        
        return job_info
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting job details: {e}")
        raise HTTPException(status_code=500, detail="Failed to get job details")

@router.get("/jobs/{job_id}/tasks")
async def get_job_tasks_endpoint(job_id: str, current_user: int = Depends(get_current_user)):
    """Get tasks for a specific job"""
    try:
        credentials = await get_deadline_credentials(current_user)
        if not credentials:
            raise HTTPException(status_code=401, detail="No credentials found")
        
        login, password = credentials
        tasks = await get_job_tasks(login, password, job_id)
        
        return tasks
    except Exception as e:
        logger.error(f"Error getting job tasks: {e}")
        raise HTTPException(status_code=500, detail="Failed to get job tasks")

@router.put("/jobs/{job_id}/requeue")
async def requeue_job_endpoint(job_id: str, current_user: int = Depends(get_current_user)):
    """Requeue a job"""
    try:
        credentials = await get_deadline_credentials(current_user)
        if not credentials:
            raise HTTPException(status_code=401, detail="No credentials found")
        
        login, password = credentials
        success = await requeue_job(login, password, job_id)
        
        if success:
            return {"success": True, "message": "Job requeued successfully"}
        else:
            raise HTTPException(status_code=400, detail="Failed to requeue job")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error requeuing job: {e}")
        raise HTTPException(status_code=500, detail="Failed to requeue job")

@router.put("/jobs/{job_id}/resume")
async def resume_job_endpoint(job_id: str, current_user: int = Depends(get_current_user)):
    """Resume a suspended job"""
    try:
        credentials = await get_deadline_credentials(current_user)
        if not credentials:
            raise HTTPException(status_code=401, detail="No credentials found")
        
        login, password = credentials
        success = await resume_job(login, password, job_id)
        
        if success:
            return {"success": True, "message": "Job resumed successfully"}
        else:
            raise HTTPException(status_code=400, detail="Failed to resume job")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resuming job: {e}")
        raise HTTPException(status_code=500, detail="Failed to resume job")

@router.put("/jobs/{job_id}/suspend")
async def suspend_job_endpoint(job_id: str, current_user: int = Depends(get_current_user)):
    """Suspend a job"""
    try:
        credentials = await get_deadline_credentials(current_user)
        if not credentials:
            raise HTTPException(status_code=401, detail="No credentials found")
        
        login, password = credentials
        success = await suspend_job(login, password, job_id)
        
        if success:
            return {"success": True, "message": "Job suspended successfully"}
        else:
            raise HTTPException(status_code=400, detail="Failed to suspend job")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error suspending job: {e}")
        raise HTTPException(status_code=500, detail="Failed to suspend job")

@router.delete("/jobs/{job_id}")
async def delete_job_endpoint(job_id: str, current_user: int = Depends(get_current_user)):
    """Delete a job"""
    try:
        credentials = await get_deadline_credentials(current_user)
        if not credentials:
            raise HTTPException(status_code=401, detail="No credentials found")
        
        login, password = credentials
        success = await delete_job(login, password, job_id)
        
        if success:
            return {"success": True, "message": "Job deleted successfully"}
        else:
            raise HTTPException(status_code=400, detail="Failed to delete job")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting job: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete job")

# Workers endpoints
@router.get("/slaves")
async def get_workers(current_user: int = Depends(get_current_user)):
    """Get list of workers"""
    try:
        workers = await get_workers_list(current_user)
        return workers
    except Exception as e:
        logger.error(f"Error getting workers: {e}")
        raise HTTPException(status_code=500, detail="Failed to get workers")

# Preview endpoints
@router.post("/jobs/{job_id}/download")
async def download_job_files(job_id: str, current_user: int = Depends(get_current_user)):
    """Download job files from Dropbox"""
    try:
        credentials = await get_deadline_credentials(current_user)
        if not credentials:
            raise HTTPException(status_code=401, detail="No credentials found")
        
        login, password = credentials
        local_path = await download_job_folder(login, password, job_id)
        
        if not local_path:
            raise HTTPException(status_code=404, detail="Failed to download job files")
        
        return {"success": True, "local_path": local_path}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading job files: {e}")
        raise HTTPException(status_code=500, detail="Failed to download job files")

@router.post("/jobs/{job_id}/create-video")
async def create_job_video(job_id: str, current_user: int = Depends(get_current_user)):
    """Create video from job files"""
    try:
        credentials = await get_deadline_credentials(current_user)
        if not credentials:
            raise HTTPException(status_code=401, detail="No credentials found")
        
        result = await create_video_from_job(current_user, job_id)
        if not result:
            raise HTTPException(status_code=500, detail="Failed to submit preview job")

        return {
            "success": True,
            "message": "Preview job submitted to Deadline",
            **result,
        }
    except WorkerStatusError as worker_error:
        detail = {
            "message": "Preferred workers are not ready",
            "invalid_workers": worker_error.invalid_workers,
            "preferred_workers": worker_error.preferred_workers,
        }
        raise HTTPException(status_code=409, detail=detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating video: {e}")
        raise HTTPException(status_code=500, detail="Failed to create video")

# Video streaming endpoint
@router.get("/video/{video_path:path}")
async def stream_video(video_path: str, current_user: int = Depends(get_current_user)):
    """Stream video file"""
    try:
        from fastapi.responses import FileResponse
        import os
        
        # Decode the video path
        decoded_path = urllib.parse.unquote(video_path)
        
        # Check if file exists
        if not os.path.exists(decoded_path):
            raise HTTPException(status_code=404, detail="Video file not found")
        
        # Return the video file
        return FileResponse(
            decoded_path,
            media_type="video/mp4",
            filename=os.path.basename(decoded_path)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error streaming video: {e}")
        raise HTTPException(status_code=500, detail="Failed to stream video")

# Health check
@router.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "ok", "service": "tasksbot-api"} 
