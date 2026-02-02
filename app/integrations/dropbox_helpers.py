import time
import base64
import json
import logging
import random
import contextlib
from typing import Optional, List, Dict, Any
from pathlib import Path, PurePosixPath
import asyncio
import aiofiles
import aiohttp
import gc
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
_dropbox_token_lock = asyncio.Lock()

async def get_fresh_access_token() -> str:
    """
    Return a valid Dropbox access token, refreshing it via the refresh token when expired.
    """
    global _dropbox_access_token, _dropbox_access_token_expires_at
    now = int(time.time())
    if _dropbox_access_token and now < _dropbox_access_token_expires_at - 30:
        return _dropbox_access_token
    async with _dropbox_token_lock:
        now = int(time.time())
        if _dropbox_access_token and now < _dropbox_access_token_expires_at - 30:
            return _dropbox_access_token

        url = "https://api.dropboxapi.com/oauth2/token"
        creds = f"{settings.dropbox_app_key}:{settings.dropbox_app_secret}".encode("ascii")
        b64_creds = base64.b64encode(creds).decode("ascii")
        headers = {
            "Authorization": f"Basic {b64_creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "grant_type": "refresh_token",
            "refresh_token": settings.dropbox_refresh_token,
        }
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, data=data) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(
                        f"Failed to update access_token: {resp.status} – {text}"
                    )
                try:
                    token_info = await resp.json()
                except Exception as exc:
                    raise RuntimeError(
                        f"Failed to parse Dropbox token response: {text}"
                    ) from exc

        access_token = token_info.get("access_token")
        expires_in = token_info.get("expires_in", 0)
        if not access_token:
            raise RuntimeError("No access_token field in response")
        _dropbox_access_token = access_token
        _dropbox_access_token_expires_at = now + int(expires_in)
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
RETRY_STATUSES = {401, 408, 429, 500, 502, 503, 504}
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.5
RETRY_MAX_DELAY = 8.0

def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None

async def _sleep_backoff(attempt: int, retry_after: Optional[str]) -> None:
    retry_after_seconds = _parse_retry_after(retry_after)
    if retry_after_seconds is not None and retry_after_seconds > 0:
        await asyncio.sleep(retry_after_seconds)
        return
    base_delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
    jitter = random.uniform(0.0, RETRY_BASE_DELAY)
    await asyncio.sleep(base_delay + jitter)

async def _post_json_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict,
    payload: dict,
    *,
    operation: str,
) -> Optional[dict]:
    for attempt in range(RETRY_ATTEMPTS):
        try:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status in (0, 200):
                    return await resp.json()
                text = await resp.text()
                if resp.status in RETRY_STATUSES and attempt < (RETRY_ATTEMPTS - 1):
                    await _sleep_backoff(attempt, resp.headers.get("Retry-After"))
                    continue
                logger.error("%s failed: %s %s", operation, resp.status, text)
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt < (RETRY_ATTEMPTS - 1):
                await _sleep_backoff(attempt, None)
                continue
            logger.error("%s failed: %s", operation, exc)
            return None

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

async def list_folder_all(session_dbx, path: str, headers_dbx: dict) -> Optional[dict]:
    """List all entries in a Dropbox folder, handling pagination."""
    list_url = "https://api.dropboxapi.com/2/files/list_folder"
    continue_url = "https://api.dropboxapi.com/2/files/list_folder/continue"
    headers_dbx["Authorization"] = f"Bearer {await get_fresh_access_token()}"
    result = await _post_json_with_retry(
        session_dbx,
        list_url,
        headers_dbx,
        {"path": path},
        operation=f"list_folder {path}",
    )
    if result is None:
        return None

    entries = list(result.get("entries", []))
    has_more = result.get("has_more")
    cursor = result.get("cursor")

    while has_more:
        if not cursor:
            logger.error("Dropbox list_folder missing cursor for path %s", path)
            return None
        headers_dbx["Authorization"] = f"Bearer {await get_fresh_access_token()}"
        result = await _post_json_with_retry(
            session_dbx,
            continue_url,
            headers_dbx,
            {"cursor": cursor},
            operation=f"list_folder_continue {path}",
        )
        if result is None:
            return None
        entries.extend(result.get("entries", []))
        has_more = result.get("has_more")
        cursor = result.get("cursor")

    result["entries"] = entries
    result["has_more"] = False
    return result

async def fetch_dropbox_metadata(session_dbx, dropbox_path: str, headers_dbx: dict) -> dict:
    key = cache_key_metadata(dropbox_path)
    cached = metadata_cache.get(key)
    if cached:
        return cached
    meta_url = "https://api.dropboxapi.com/2/files/get_metadata"
    headers = {
        "Authorization": f"Bearer {await get_fresh_access_token()}",
        "Dropbox-API-Select-User": settings.dropbox_team_member_id,
        "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
        "Content-Type": "application/json"
    }
    result = await _post_json_with_retry(
        session_dbx,
        meta_url,
        headers,
        {"path": dropbox_path},
        operation=f"get_metadata {dropbox_path}",
    )
    if result is None:
        raise RuntimeError(f"Error getting metadata for {dropbox_path}")
    metadata_cache.set(key, result)
    return result

async def list_folder_cached(session_dbx, path, headers_dbx):
    key = cache_key_list_folder(path)
    cached = list_folder_cache.get(key)
    if cached:
        return cached
    result = await list_folder_all(session_dbx, path, headers_dbx)
    if result is None:
        return None
    list_folder_cache.set(key, result)
    return result

async def count_exr_files(session_dbx: aiohttp.ClientSession, path: str, headers_dbx: dict) -> int:
    """
    Counts EXR/JPG/PNG files recursively in a Dropbox folder (excluding cryptomatte and conflicted copies).
    """
    # Create a copy of headers and serialize Dropbox-API-Path-Root
    headers_copy = headers_dbx.copy()
    if "Dropbox-API-Path-Root" in headers_copy:
        path_root = headers_copy["Dropbox-API-Path-Root"]
        if not isinstance(path_root, str):
            headers_copy["Dropbox-API-Path-Root"] = json.dumps(path_root)

    result = await list_folder_all(session_dbx, path, headers_copy)
    if not result:
        return 0

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
    for attempt in range(RETRY_ATTEMPTS):
        retry_after = None
        async with semaphore:
            try:
                # Create fresh headers for each download
                dl_headers = {
                    "Authorization": f"Bearer {await get_fresh_access_token()}",
                    "Dropbox-API-Select-User": settings.dropbox_team_member_id,
                    "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
                    "Dropbox-API-Arg": headers["Dropbox-API-Arg"]
                }
                
                async with session.post(download_url, headers=dl_headers) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        retry_after = resp.headers.get("Retry-After")
                        if resp.status in RETRY_STATUSES and attempt < (RETRY_ATTEMPTS - 1):
                            logger.warning(
                                "Retrying download %s (%s): %s",
                                local_file.name,
                                resp.status,
                                text,
                            )
                        else:
                            logger.error(
                                "Failed to download %s: %s %s",
                                local_file.name,
                                resp.status,
                                text,
                            )
                            return False
                    else:
                        # Ensure parent directory exists
                        local_file.parent.mkdir(parents=True, exist_ok=True)
                        
                        # Write file in chunks
                        async with aiofiles.open(local_file, 'wb') as f:
                            async for chunk in resp.content.iter_chunked(8192):  # 8KB chunks
                                await f.write(chunk)
                        return True
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt >= (RETRY_ATTEMPTS - 1):
                    logger.error(f"Error downloading {local_file.name}: {exc}")
                    return False
                logger.warning("Retrying download %s after error: %s", local_file.name, exc)
            except Exception as e:
                logger.error(f"Error downloading {local_file.name}: {e}")
                return False

        if local_file.exists():
            with contextlib.suppress(Exception):
                local_file.unlink()
        if attempt < (RETRY_ATTEMPTS - 1):
            await _sleep_backoff(attempt, retry_after)
    return False

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
                "Authorization": f"Bearer {await get_fresh_access_token()}",
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

    async def _iter_file_chunks(path: Path, chunk_size: int = 1024 * 1024):
        async with aiofiles.open(path, "rb") as f:
            while True:
                chunk = await f.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    async with aiohttp.ClientSession() as session_upload:
        for attempt in range(RETRY_ATTEMPTS):
            headers_upload = {
                "Authorization": f"Bearer {await get_fresh_access_token()}",
                "Dropbox-API-Select-User": settings.dropbox_team_member_id,
                "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
                "Dropbox-API-Arg": json.dumps({"path": dropbox_upload_path, "mode": "overwrite"}),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(video_path.stat().st_size),
            }
            try:
                async with session_upload.post(
                    upload_url, headers=headers_upload, data=_iter_file_chunks(video_path)
                ) as resp_up:
                    if resp_up.status == 200:
                        return dropbox_upload_path
                    text = await resp_up.text()
                    if resp_up.status in RETRY_STATUSES and attempt < (RETRY_ATTEMPTS - 1):
                        logger.warning(
                            "Retrying upload %s (%s): %s",
                            video_path.name,
                            resp_up.status,
                            text,
                        )
                        await _sleep_backoff(attempt, resp_up.headers.get("Retry-After"))
                        continue
                    raise RuntimeError(f"Error uploading video to Dropbox: {text}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt < (RETRY_ATTEMPTS - 1):
                    logger.warning("Retrying upload %s after error: %s", video_path.name, exc)
                    await _sleep_backoff(attempt, None)
                    continue
                raise RuntimeError(f"Error uploading video to Dropbox: {exc}") from exc
    return dropbox_upload_path
