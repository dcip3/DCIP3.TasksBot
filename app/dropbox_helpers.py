import time
import base64
import json
import logging
from typing import Optional
from pathlib import Path, PurePosixPath
import asyncio
import aiofiles
import aiohttp
import requests
from aiohttp import ClientTimeout

from app.core.config import settings

logger = logging.getLogger(__name__)

# Переменные для кеширования access_token
_dropbox_access_token = None
_dropbox_access_token_expires_at = 0

def get_fresh_access_token():
    """
    Возвращает действующий Dropbox access_token. Если текущий ещё не истёк,
    возвращает кешированный. Иначе обновляет по refresh_token.
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

async def fetch_dropbox_metadata(session_dbx: aiohttp.ClientSession, dropbox_path: str, headers_dbx: dict) -> dict:
    """
    Получает метаданные объекта в Dropbox по указанному пути.
    """
    meta_url = "https://api.dropboxapi.com/2/files/get_metadata"
    
    # Создаем копию заголовков и сериализуем Dropbox-API-Path-Root
    headers_copy = headers_dbx.copy()
    if "Dropbox-API-Path-Root" in headers_copy:
        path_root = headers_copy["Dropbox-API-Path-Root"]
        if not isinstance(path_root, str):
            headers_copy["Dropbox-API-Path-Root"] = json.dumps(path_root)
    
    async with session_dbx.post(meta_url, headers=headers_copy, json={"path": dropbox_path}) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"Error getting metadata: {text}")
        return await resp.json()

async def count_exr_files(session_dbx: aiohttp.ClientSession, path: str, headers_dbx: dict) -> int:
    """
    Считает количество EXR-файлов (исключая cryptomatte) рекурсивно в папке Dropbox.
    """
    list_url = "https://api.dropboxapi.com/2/files/list_folder"
    
    # Создаем копию заголовков и сериализуем Dropbox-API-Path-Root
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
        name = entry["name"]
        if "cryptomatte" in name.lower():
            continue
        if entry[".tag"] == "file" and name.lower().endswith(".exr"):
            count += 1
        elif entry[".tag"] == "folder":
            count += await count_exr_files(session_dbx, entry["path_display"], headers_dbx)
    return count

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
            async with session.post(download_url, headers=headers) as resp:
                if resp.status != 200:
                    return False
                
                local_file.parent.mkdir(parents=True, exist_ok=True)
                async with aiofiles.open(local_file, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(8192):  # 8KB chunks
                        await f.write(chunk)
                return True
        except Exception as e:
            logger.error(f"Error downloading {local_file.name}: {e}")
            return False


async def download_exr_folder_parallel(
    session_dbx: aiohttp.ClientSession,
    download_url: str,
    headers_dbx: dict,
    path: str,
    local_folder: Path,
    job_id: str,
    download_states: dict,
    stop_downloads: dict,
    max_concurrent: int = 5
):
    """
    Рекурсивно скачивает EXR-файлы из папки Dropbox в локальную директорию с параллельным скачиванием.
    
    Args:
        session_dbx: aiohttp session
        download_url: Dropbox download URL
        headers_dbx: Base headers
        path: Dropbox path
        local_folder: Local folder path
        job_id: Job ID
        download_states: Download state tracking
        stop_downloads: Stop download flags
        max_concurrent: Maximum concurrent downloads
    """
    list_url = "https://api.dropboxapi.com/2/files/list_folder"
    
    # Создаем копию заголовков и сериализуем Dropbox-API-Path-Root
    headers_copy = headers_dbx.copy()
    if "Dropbox-API-Path-Root" in headers_copy:
        path_root = headers_copy["Dropbox-API-Path-Root"]
        if not isinstance(path_root, str):
            headers_copy["Dropbox-API-Path-Root"] = json.dumps(path_root)
    
    async with session_dbx.post(list_url, headers=headers_copy, json={"path": path}) as list_resp:
        if list_resp.status not in (0, 200):
            return
        result = await list_resp.json()

    state = download_states.get(job_id)
    if state is None:
        return
    
    # Collect all files to download
    files_to_download = []
    
    for entry in result.get("entries", []):
        name = entry["name"]
        if "cryptomatte" in name.lower():
            continue
            
        if entry[".tag"] == "file" and name.lower().endswith(".exr"):
            local_file = local_folder / name
            dl_headers = {
                "Authorization": f"Bearer {get_fresh_access_token()}",
                "Dropbox-API-Select-User": settings.dropbox_team_member_id,
                "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
                "Dropbox-API-Arg": json.dumps({"path": entry["path_display"]})
            }
            files_to_download.append((download_url, dl_headers, local_file))
            
        elif entry[".tag"] == "folder":
            subfolder = local_folder / name
            subfolder.mkdir(exist_ok=True)
            # Recursively collect files from subfolders
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
    
    # Download files in parallel
    if files_to_download:
        semaphore = asyncio.Semaphore(max_concurrent)
        tasks = []
        
        for download_url, dl_headers, local_file in files_to_download:
            task = download_file_parallel(session_dbx, download_url, dl_headers, local_file, semaphore)
            tasks.append(task)
        
        # Wait for all downloads to complete
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Update progress
        downloaded_count = state.get("downloaded_count", 0)
        successful_downloads = sum(1 for result in results if result is True)
        downloaded_count += successful_downloads
        
        if job_id in download_states:
            download_states[job_id]["downloaded_count"] = downloaded_count
            
        total_files = state.get("total_files", 0)
        percent = int((downloaded_count / total_files) * 100) if total_files else 0
        progress_msg = state.get("progress_msg")
        
        try:
            if progress_msg:
                stop_kb = state.get("stop_kb")
                await progress_msg.edit_text(f"Step 1: Downloading {percent}% ({downloaded_count}/{total_files})", reply_markup=stop_kb)
        except Exception:
            pass


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
    Рекурсивно скачивает EXR-файлы из папки Dropbox в локальную директорию.
    Использует параллельное скачивание для ускорения.
    """
    # Use parallel download by default
    await download_exr_folder_parallel(
        session_dbx,
        download_url,
        headers_dbx,
        path,
        local_folder,
        job_id,
        download_states,
        stop_downloads,
        max_concurrent=5  # Adjust based on your needs
    )

async def upload_video_to_dropbox(video_path: Path, metadata: dict, job_id: Optional[str] = None) -> str:
    """
    Загружает видео-файл на Dropbox в ту же директорию, что и исходные EXR.
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