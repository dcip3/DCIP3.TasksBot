import time
import base64
import json
import logging
from typing import Optional, List, Dict, Any
from pathlib import Path, PurePosixPath
import asyncio
import aiofiles
import aiohttp
import requests
from aiohttp import ClientTimeout
import gc
import functools
import threading

from app.core.config import settings
from app.core.utils import make_progress_bar

logger = logging.getLogger(__name__)
PREVIEW_FRAME_EXTS = {".exr", ".jpg", ".jpeg", ".png"}

def _is_preview_frame(name: str) -> bool:
    return Path(name).suffix.lower() in PREVIEW_FRAME_EXTS

# Cached Dropbox access token details
_dropbox_access_token = None
_dropbox_access_token_expires_at = 0

def get_fresh_access_token():
    """
    Return a valid Dropbox access token, refreshing it via the refresh token when expired.
    """
    global _dropbox_access_token, _dropbox_access_token_expires_at
    now = int(time.time())
    if _dropbox_access_token and now < _dropbox_access_token_expires_at - 30:
        return _dropbox_access_token

    url = "https://api.dropboxapi.com/oauth2/token"
    creds = f"{settings.dropbox_app_key}:{settings.dropbox_app_secret}".encode("ascii")
    b64_creds = base64.b64encode(creds).decode("ascii")
    headers = {
        "Authorization": f"Basic {b64_creds}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": settings.dropbox_refresh_token
    }
    resp = requests.post(url, headers=headers, data=data)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to update access_token: {resp.status_code} – {resp.text}")
    token_info = resp.json()
    access_token = token_info.get("access_token")
    expires_in = token_info.get("expires_in", 0)
    if not access_token:
        raise RuntimeError("No access_token field in response")
    _dropbox_access_token = access_token
    _dropbox_access_token_expires_at = now + expires_in
    return _dropbox_access_token

# Simple in-memory cache with TTL support
class TTLCache:
    def __init__(self, ttl_seconds=180):
        self.ttl = ttl_seconds
        self._cache = {}
        self._lock = threading.Lock()
    def get(self, key):
        with self._lock:
            v = self._cache.get(key)
            if not v:
                return None
            value, expires = v
            if time.time() > expires:
                del self._cache[key]
                return None
            return value
    def set(self, key, value):
        with self._lock:
            self._cache[key] = (value, time.time() + self.ttl)
    def clear(self):
        with self._lock:
            self._cache.clear()

list_folder_cache = TTLCache(ttl_seconds=180)
metadata_cache = TTLCache(ttl_seconds=180)

PROGRESS_EDIT_MIN_INTERVAL = 1.5
PROGRESS_MIN_PERCENT_STEP = 1

def _should_update_progress(state: dict, percent: int) -> bool:
    """Throttle progress updates to avoid Telegram edit rate limits."""
    now = time.monotonic()
    last_ts = float(state.get("last_progress_ts", 0.0) or 0.0)
    last_percent = int(state.get("last_progress_percent", -1) or -1)
    if percent >= 100:
        state["last_progress_ts"] = now
        state["last_progress_percent"] = percent
        return True
    if (now - last_ts) < PROGRESS_EDIT_MIN_INTERVAL and abs(percent - last_percent) < PROGRESS_MIN_PERCENT_STEP:
        return False
    state["last_progress_ts"] = now
    state["last_progress_percent"] = percent
    return True

def cache_key_list_folder(path):
    return f"list_folder:{path}"

def cache_key_metadata(path):
    return f"metadata:{path}"

async def fetch_dropbox_metadata(session_dbx, dropbox_path: str, headers_dbx: dict) -> dict:
    key = cache_key_metadata(dropbox_path)
    cached = metadata_cache.get(key)
    if cached:
        return cached
    meta_url = "https://api.dropboxapi.com/2/files/get_metadata"
    headers = {
        "Authorization": f"Bearer {get_fresh_access_token()}",
        "Dropbox-API-Select-User": settings.dropbox_team_member_id,
        "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
        "Content-Type": "application/json"
    }
    async with session_dbx.post(meta_url, headers=headers, json={"path": dropbox_path}) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"Error getting metadata: {text}")
        result = await resp.json()
        metadata_cache.set(key, result)
        return result

async def list_folder_cached(session_dbx, path, headers_dbx):
    key = cache_key_list_folder(path)
    cached = list_folder_cache.get(key)
    if cached:
        return cached
    list_url = "https://api.dropboxapi.com/2/files/list_folder"
    async with session_dbx.post(list_url, headers=headers_dbx, json={"path": path}) as list_resp:
        if list_resp.status not in (0, 200):
            return None
        result = await list_resp.json()
        list_folder_cache.set(key, result)
        return result

async def count_exr_files(session_dbx: aiohttp.ClientSession, path: str, headers_dbx: dict) -> int:
    """
    Counts EXR/JPG/PNG files recursively in a Dropbox folder (excluding cryptomatte and conflicted copies).
    """
    list_url = "https://api.dropboxapi.com/2/files/list_folder"
    
    # Create a copy of headers and serialize Dropbox-API-Path-Root
    headers_copy = headers_dbx.copy()
    if "Dropbox-API-Path-Root" in headers_copy:
        path_root = headers_copy["Dropbox-API-Path-Root"]
        if not isinstance(path_root, str):
            headers_copy["Dropbox-API-Path-Root"] = json.dumps(path_root)
    
    async with session_dbx.post(list_url, headers=headers_copy, json={"path": path}) as list_resp:
        if list_resp.status not in (0, 200):
            return 0
        result = await list_resp.json()

    count = 0
    for entry in result.get("entries", []):
        name = entry["name"].lower()
        # Skip cryptomatte and conflicted copy files
        if "cryptomatte" in name or "conflicted copy" in name:
            continue
        if entry[".tag"] == "file" and _is_preview_frame(entry["name"]):
            count += 1
        elif entry[".tag"] == "folder":
            count += await count_exr_files(session_dbx, entry["path_display"], headers_dbx)
    return count

# FileQueue defaults to batch_size=8
class FileQueue:
    def __init__(self, batch_size: int = 8):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.batch_size = batch_size
        self.processing: List[str] = []
        self.completed: List[str] = []
        self.failed: List[str] = []

    async def add(self, file_info: Dict[str, Any]):
        await self.queue.put(file_info)

    async def get_batch(self) -> List[Dict[str, Any]]:
        batch = []
        try:
            for _ in range(self.batch_size):
                if self.queue.empty():
                    break
                batch.append(await self.queue.get())
        except asyncio.QueueEmpty:
            pass
        return batch

    def mark_processing(self, file_path: str):
        self.processing.append(file_path)

    def mark_completed(self, file_path: str):
        if file_path in self.processing:
            self.processing.remove(file_path)
        self.completed.append(file_path)

    def mark_failed(self, file_path: str):
        if file_path in self.processing:
            self.processing.remove(file_path)
        self.failed.append(file_path)

    @property
    def is_empty(self) -> bool:
        return self.queue.empty() and not self.processing

async def download_file_parallel(
    session: aiohttp.ClientSession,
    download_url: str,
    headers: dict,
    local_file: Path,
    semaphore: asyncio.Semaphore
) -> bool:
    """
    Download a single file with semaphore control.
    
    Args:
        session: aiohttp session
        download_url: Dropbox download URL
        headers: Request headers
        local_file: Local file path
        semaphore: Semaphore to limit concurrent downloads
        
    Returns:
        True if successful, False otherwise
    """
    async with semaphore:
        try:
            # Create fresh headers for each download
            dl_headers = {
                "Authorization": f"Bearer {get_fresh_access_token()}",
                "Dropbox-API-Select-User": settings.dropbox_team_member_id,
                "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
                "Dropbox-API-Arg": headers["Dropbox-API-Arg"]
            }
            
            async with session.post(download_url, headers=dl_headers) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to download {local_file.name}: {resp.status}")
                    return False
                
                # Ensure parent directory exists
                local_file.parent.mkdir(parents=True, exist_ok=True)
                
                # Write file in chunks
                async with aiofiles.open(local_file, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(8192):  # 8KB chunks
                        await f.write(chunk)
                return True
        except Exception as e:
            logger.error(f"Error downloading {local_file.name}: {e}")
            return False

async def process_file_batch(
    session: aiohttp.ClientSession,
    download_url: str,
    file_queue: FileQueue,
    local_folder: Path,
    job_id: str,
    download_states: dict,
    stop_downloads: dict,
    headers_dbx: dict
):
    """Process a batch of files - download and convert them."""
    from app.integrations.video_helpers import convert_single_exr_file_streaming
    
    batch = await file_queue.get_batch()
    if not batch:
        return

    # Download files in parallel
    semaphore = asyncio.Semaphore(5)  # Limit concurrent downloads
    download_tasks = []
    
    # Create conversion directory
    conv_folder = Path(settings.conv_dir) / local_folder.name
    conv_folder.mkdir(parents=True, exist_ok=True)
    
    # Prepare download tasks for all files in batch
    for file_info in batch:
        suffix = Path(file_info["name"]).suffix.lower()
        if suffix == ".exr":
            local_file = local_folder / file_info["name"]
        else:
            normalized_name = f"{Path(file_info['name']).stem}{suffix}"
            local_file = conv_folder / normalized_name
        file_queue.mark_processing(str(local_file))
        
        dl_headers = {
            "Authorization": f"Bearer {get_fresh_access_token()}",
            "Dropbox-API-Select-User": settings.dropbox_team_member_id,
            "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
            "Dropbox-API-Arg": json.dumps({"path": file_info["path_display"]})
        }
        task = download_file_parallel(session, download_url, dl_headers, local_file, semaphore)
        download_tasks.append((task, local_file))

    # Wait for all downloads to complete
    download_results = await asyncio.gather(*(task for task, _ in download_tasks), return_exceptions=True)
    
    # Process downloaded files
    for i, (download_success, (_, local_file)) in enumerate(zip(download_results, download_tasks)):
        try:
            if isinstance(download_success, Exception):
                logger.error(f"Failed to download {local_file}: {download_success}")
                file_queue.mark_failed(str(local_file))
                continue
                
            if not download_success:
                file_queue.mark_failed(str(local_file))
                logger.error(f"Failed to download {local_file}")
                continue
            
            if local_file.suffix.lower() == ".exr":
                success, _, error = await asyncio.to_thread(
                    convert_single_exr_file_streaming,
                    (local_file, conv_folder, None, None, None, None, None),
                )
                if success:
                    file_queue.mark_completed(str(local_file))
                else:
                    file_queue.mark_failed(str(local_file))
                    logger.error(f"Failed to convert {local_file}: {error}")
            else:
                file_queue.mark_completed(str(local_file))
                
        except Exception as e:
            file_queue.mark_failed(str(local_file))
            logger.error(f"Error processing {local_file}: {e}")
            
        # Update progress after each file is processed
        if job_id in download_states:
            state = download_states[job_id]
            total_files = state.get("total_files", 0)
            downloaded_count = len(file_queue.completed)
            percent = int((downloaded_count / total_files) * 100) if total_files else 0
            
            progress_msg = state.get("progress_msg")
            try:
                if progress_msg and _should_update_progress(state, percent):
                    stop_kb = state.get("stop_kb")
                    bar = make_progress_bar(percent)
                    await progress_msg.edit_text(
                        f"Step 1: Downloading and converting {percent}% ({downloaded_count}/{total_files})\n{bar}",
                        reply_markup=stop_kb
                    )
            except Exception:
                pass

async def download_exr_folder_parallel(
    session_dbx: aiohttp.ClientSession,
    download_url: str,
    headers_dbx: dict,
    path: str,
    local_folder: Path,
    job_id: str,
    download_states: dict,
    stop_downloads: dict,
    max_concurrent: int = 8
):
    """
    Recursively downloads preview frames from Dropbox folder with parallel processing.
    """
    # Use cached list-folder response instead of direct request
    result = await list_folder_cached(session_dbx, path, headers_dbx)
    if not result:
        return

    file_queue = FileQueue(batch_size=max_concurrent)
    
    for entry in result.get("entries", []):
        name = entry["name"].lower()
        # Skip cryptomatte and conflicted copy files
        if "cryptomatte" in name or "conflicted copy" in name:
            continue
        if entry[".tag"] == "file" and _is_preview_frame(entry["name"]):
            await file_queue.add(entry)
        elif entry[".tag"] == "folder":
            subfolder = local_folder / entry["name"]
            subfolder.mkdir(exist_ok=True)
            await download_exr_folder_parallel(
                session_dbx,
                download_url,
                headers_dbx,
                entry["path_display"],
                subfolder,
                job_id,
                download_states,
                stop_downloads,
                max_concurrent
            )
            if stop_downloads.get(job_id) and stop_downloads[job_id].is_set():
                return

    # Process remaining files in the queue
    while not file_queue.is_empty:
        batch = await file_queue.get_batch()
        if not batch:
            break
            
        # Process batch
        await process_file_batch(
            session_dbx,
            download_url,
            file_queue,
            local_folder,
            job_id,
            download_states,
            stop_downloads,
            headers_dbx
        )

async def download_exr_folder(
    session_dbx: aiohttp.ClientSession,
    download_url: str,
    headers_dbx: dict,
    path: str,
    local_folder: Path,
    job_id: str,
    download_states: dict,
    stop_downloads: dict
):
    """
    Download and convert preview frames from Dropbox folder.
    Uses parallel processing with batching for efficiency.
    """
    # Use cached list-folder response instead of direct request
    result = await list_folder_cached(session_dbx, path, headers_dbx)
    if not result:
        return
    
    # Create file queue with larger batch size for parallel processing
    file_queue = FileQueue(batch_size=8)  # Increased from 3 to 5
    
    # Create conversion directory
    conv_folder = Path(settings.conv_dir) / local_folder.name
    conv_folder.mkdir(parents=True, exist_ok=True)
    
    # Add files to queue
    for entry in result.get("entries", []):
        name = entry["name"].lower()
        # Skip cryptomatte and conflicted copy files
        if "cryptomatte" in name or "conflicted copy" in name:
            continue
            
        if entry[".tag"] == "file" and _is_preview_frame(entry["name"]):
            await file_queue.add(entry)
            
        elif entry[".tag"] == "folder":
            # Process subfolders recursively
            subfolder = local_folder / entry["name"]
            subfolder.mkdir(exist_ok=True)
            await download_exr_folder(
                session_dbx, download_url, headers_dbx,
                entry["path_display"], subfolder, job_id,
                download_states, stop_downloads
            )
            
            if stop_downloads.get(job_id) and stop_downloads[job_id].is_set():
                return
    
    # Process files in batches with parallel downloads and conversions
    while not file_queue.is_empty and not (stop_downloads.get(job_id) and stop_downloads[job_id].is_set()):
        batch = await file_queue.get_batch()
        if not batch:
            break
            
        # Download files in parallel
        semaphore = asyncio.Semaphore(5)  # Allow 5 concurrent downloads
        download_tasks = []
        
        for file_info in batch:
            suffix = Path(file_info["name"]).suffix.lower()
            if suffix == ".exr":
                local_file = local_folder / file_info["name"]
            else:
                normalized_name = f"{Path(file_info['name']).stem}{suffix}"
                local_file = conv_folder / normalized_name
            file_queue.mark_processing(str(local_file))
            
            dl_headers = {
                "Authorization": f"Bearer {get_fresh_access_token()}",
                "Dropbox-API-Select-User": settings.dropbox_team_member_id,
                "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
                "Dropbox-API-Arg": json.dumps({"path": file_info["path_display"]})
            }
            
            task = download_file_parallel(
                session_dbx,
                download_url,
                dl_headers,
                local_file,
                semaphore
            )
            download_tasks.append((task, local_file, file_info))
        
        # Wait for all downloads in batch to complete
        download_results = await asyncio.gather(*(task for task, _, _ in download_tasks))
        
        # Process downloaded files
        conversion_tasks = []
        conversion_items = []
        from app.integrations.video_helpers import convert_single_exr_file_streaming
        
        for success, (_, local_file, file_info) in zip(download_results, download_tasks):
            if success:
                if Path(file_info["name"]).suffix.lower() == ".exr":
                    conversion_task = asyncio.create_task(
                        asyncio.to_thread(
                            convert_single_exr_file_streaming,
                            (local_file, conv_folder, None, None, None, None, None),
                        )
                    )
                    conversion_tasks.append(conversion_task)
                    conversion_items.append(local_file)
                else:
                    file_queue.mark_completed(str(local_file))
            else:
                file_queue.mark_failed(str(local_file))
                logger.error(f"Failed to download {local_file.name}")
        
        # Wait for all conversions to complete
        if conversion_tasks:
            conversion_results = await asyncio.gather(*conversion_tasks, return_exceptions=True)
            
            # Process conversion results
            for result, local_file in zip(conversion_results, conversion_items):
                if isinstance(result, Exception):
                    file_queue.mark_failed(str(local_file))
                    logger.error(f"Error converting {local_file}: {result}")
                else:
                    success, _, error = result
                    if success:
                        file_queue.mark_completed(str(local_file))
                    else:
                        file_queue.mark_failed(str(local_file))
                        logger.error(f"Failed to convert {local_file}: {error}")
        
        # Update progress
        if job_id in download_states:
            state = download_states[job_id]
            total_files = state.get("total_files", 0)
            downloaded_count = len(file_queue.completed)
            percent = int((downloaded_count / total_files) * 100) if total_files else 0
            
            progress_msg = state.get("progress_msg")
            try:
                if progress_msg and _should_update_progress(state, percent):
                    stop_kb = state.get("stop_kb")
                    bar = make_progress_bar(percent)
                    await progress_msg.edit_text(
                        f"Step 1: Downloading and converting {percent}% ({downloaded_count}/{total_files})\n{bar}",
                        reply_markup=stop_kb
                    )
            except Exception:
                pass
        
        # Force memory cleanup after each batch
        gc.collect()

async def upload_video_to_dropbox(video_path: Path, metadata: dict, job_id: Optional[str] = None) -> str:
    """
    Upload a rendered video file back to the Dropbox folder containing the source frames.
    """
    filename = video_path.name
    exr_parent = str(PurePosixPath(metadata["path_display"]).parent)
    
    # Use the original filename without job_id suffix for better naming
    # This matches the behavior of the old version
    dropbox_upload_path = f"{exr_parent}/{filename}"

    upload_url = "https://content.dropboxapi.com/2/files/upload"
    headers_upload = {
        "Authorization": f"Bearer {get_fresh_access_token()}",
        "Dropbox-API-Select-User": settings.dropbox_team_member_id,
        "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
        "Dropbox-API-Arg": json.dumps({"path": dropbox_upload_path, "mode": "overwrite"}),
        "Content-Type": "application/octet-stream"
    }
    data = video_path.read_bytes()
    async with aiohttp.ClientSession() as session_upload:
        async with session_upload.post(upload_url, headers=headers_upload, data=data) as resp_up:
            if resp_up.status != 200:
                text = await resp_up.text()
                raise RuntimeError(f"Error uploading video to Dropbox: {text}")
    return dropbox_upload_path
