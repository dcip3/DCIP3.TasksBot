"""
Dropbox integration service functions.
"""

from typing import Optional, List, Tuple
from pathlib import Path
import json
import logging

import aiohttp

from app.core.config import settings
from app.core.path_utils import extract_dropbox_path
from app.core.bot_core import get_aiosession
from app.integrations.dropbox_helpers import get_fresh_access_token, fetch_dropbox_metadata, list_folder_all
from app.services.deadline_service import get_job_info

logger = logging.getLogger(__name__)

# ==========================================================================
# === DROPBOX INTEGRATION FUNCTIONS ===
# ==========================================================================
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
        dropbox_path = extract_dropbox_path(fullpath, settings.dropbox_root_marker)
        if not dropbox_path:
            logger.error(f"Failed to normalize Dropbox path for job {job_id}: {fullpath}")
            return None
        
        # Create temp directory
        temp_dir = Path(settings.temp_dir)
        temp_dir.mkdir(exist_ok=True)
        
        # Prepare Dropbox headers
        headers_dbx = {
            "Authorization": f"Bearer {await get_fresh_access_token()}",
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
            
        # List files in folder (handle pagination)
        result = await list_folder_all(session_dbx, metadata["path_display"], headers_dbx)
        if not result:
            logger.error("Failed to list folder contents for %s", metadata["path_display"])
            return None
        
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
                    "Authorization": f"Bearer {await get_fresh_access_token()}",
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
