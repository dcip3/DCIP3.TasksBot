"""Heavy preview generation pipelines separated from Telegram callback routing."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from app.core.config import settings
from app.core.maintenance import make_progress_bar
from app.core.path_utils import extract_dropbox_path
from app.core.preview_text import build_preview_caption
from app.integrations.dropbox_helpers import (
    count_exr_files,
    download_exr_folder,
    fetch_dropbox_metadata,
    get_fresh_access_token,
    list_folder_all,
    upload_video_to_dropbox,
)
from app.integrations.video_helpers import (
    assemble_video_from_jpg,
    cleanup_job_files,
    get_file_size_mb,
    prepare_video_for_delivery,
)
from app.core.bot_core import get_aiosession
from app.services.deadline import get_job_info_by_user_id
from app.services.preview.interaction import PreviewInteraction
from app.services.preview.state import preview_state

logger = logging.getLogger(__name__)


async def _upload_video_to_dropbox_and_cleanup(
    *,
    session_dbx,
    headers_dbx: dict,
    dropbox_path: str,
    video_path: Path,
    job_id: str,
) -> None:
    try:
        metadata = await fetch_dropbox_metadata(session_dbx, dropbox_path, headers_dbx)
        uploaded_path = await upload_video_to_dropbox(video_path, metadata, job_id)
        logger.info("Uploaded preview video for job %s to Dropbox path %s", job_id, uploaded_path)
    except Exception as upload_error:
        logger.error("Error uploading preview video for job %s in background: %s", job_id, upload_error)
    finally:
        try:
            cleanup_job_files(job_id)
        except Exception as cleanup_error:
            logger.error("Error cleaning up job files after background upload: %s", cleanup_error)


async def maybe_render_single_frame_preview(
    user_id: int,
    job_id: str,
    interaction: PreviewInteraction,
) -> bool:
    job_info = await get_job_info_by_user_id(user_id, job_id)
    if not job_info:
        return False

    outdirs = job_info.get("OutDir", [])
    dropbox_path = extract_dropbox_path(
        outdirs[0] if outdirs else None,
        settings.dropbox_root_marker,
    )
    if not dropbox_path:
        return False

    headers_dbx = {
        "Authorization": f"Bearer {await get_fresh_access_token()}",
        "Dropbox-API-Select-User": settings.dropbox_team_member_id,
        "Dropbox-API-Path-Root": json.dumps(
            {".tag": "root", "root": settings.dropbox_root_namespace_id}
        ),
        "Content-Type": "application/json",
    }
    session_dbx = await get_aiosession()
    try:
        total_files = await count_exr_files(
            session_dbx,
            dropbox_path,
            headers_dbx,
            stop_after=2,
        )
    except Exception as exc:
        logger.warning("Single-frame check failed for job %s: %s", job_id, exc)
        return False

    if total_files != 1:
        return False

    await render_preview_via_server_pipeline(user_id, job_id, interaction)
    return True


async def render_preview_via_server_pipeline(
    user_id: int,
    job_id: str,
    interaction: PreviewInteraction,
) -> None:
    """Generate preview on bot host: download frames, convert, assemble, upload, send."""
    progress_handle: Any | None = None
    stop_event: Optional[asyncio.Event] = None
    state: Optional[dict] = None
    defer_cleanup = False
    cancel_callback_data = f"preview_server_cancel:{job_id}"
    try:
        progress_handle = await interaction.create_progress(
            "🔍 Starting preview generation..."
        )

        stop_event = preview_state.get_or_create_stop_event(job_id)

        async def update_download_progress(percent: int, downloaded: int, total: int) -> None:
            bar_text = f"Step 1: Downloading and converting {percent}% ({downloaded}/{total})"

            await interaction.update_progress(
                progress_handle,
                f"{bar_text}\n{make_progress_bar(percent)}",
                cancel_callback_data=cancel_callback_data,
            )

        preview_state.download_states[job_id] = {
            "progress_handle": progress_handle,
            "interaction": interaction,
            "progress_callback": update_download_progress,
            "total_files": 0,
            "downloaded_files": 0,
            "last_progress_ts": 0.0,
            "last_progress_percent": -1,
        }
        state = preview_state.download_states[job_id]

        async def finalize_cancellation() -> bool:
            if stop_event is None or state is None:
                return False
            if not (stop_event.is_set() or state.get("cancel_requested")):
                return False
            if not state.get("cancel_finalized"):
                state["cancel_finalized"] = True
                progress = state.get("progress_handle") or progress_handle
                if progress is not None:
                    with contextlib.suppress(Exception):
                        await interaction.update_progress(
                            progress,
                            "⏹️ Preview generation cancelled.",
                        )
                try:
                    cleanup_job_files(job_id)
                except Exception as cleanup_error:
                    logger.error("Error cleaning up after cancellation: %s", cleanup_error)
                with contextlib.suppress(Exception):
                    await interaction.answer("Preview generation cancelled.", show_alert=False)
            return True

        await interaction.update_progress(
            progress_handle,
            "📥 Step 1: Downloading files from Dropbox...",
            cancel_callback_data=cancel_callback_data,
        )
        if await finalize_cancellation():
            return

        job_info = await get_job_info_by_user_id(user_id, job_id)
        if not job_info:
            await interaction.update_progress(progress_handle, "❌ Failed to get job info")
            await interaction.answer("Failed to get job info.", show_alert=True)
            return

        outdirs = job_info.get("OutDir", [])
        if not outdirs:
            await interaction.update_progress(progress_handle, "❌ No output directory found.")
            await interaction.answer("Render path is missing.", show_alert=True)
            return

        fullpath = outdirs[0]
        if fullpath.find(settings.dropbox_root_marker) == -1:
            await interaction.update_progress(progress_handle, "❌ Dropbox root marker not found in path.")
            await interaction.answer("Could not determine Dropbox path.", show_alert=True)
            return

        dropbox_path = extract_dropbox_path(fullpath, settings.dropbox_root_marker)
        if not dropbox_path:
            await interaction.update_progress(progress_handle, "❌ Could not normalize Dropbox path.")
            await interaction.answer("Could not determine Dropbox path.", show_alert=True)
            return

        temp_dir = Path(settings.temp_dir)
        temp_dir.mkdir(exist_ok=True)
        exr_folder_name = Path(dropbox_path).parts[-1] or job_id
        local_root = temp_dir / f"{exr_folder_name}_{job_id}"
        local_root.mkdir(parents=True, exist_ok=True)

        headers_dbx = {
            "Authorization": f"Bearer {await get_fresh_access_token()}",
            "Dropbox-API-Select-User": settings.dropbox_team_member_id,
            "Dropbox-API-Path-Root": json.dumps(
                {".tag": "root", "root": settings.dropbox_root_namespace_id}
            ),
            "Content-Type": "application/json",
        }
        session_dbx = await get_aiosession()

        list_result = await list_folder_all(session_dbx, dropbox_path, headers_dbx)
        if not list_result:
            await interaction.update_progress(progress_handle, "❌ Failed to list folder.")
            await interaction.answer("Could not list files in Dropbox.", show_alert=True)
            return

        preview_exts = (".exr", ".jpg", ".jpeg", ".png")
        total_files = sum(
            1
            for entry in list_result.get("entries", [])
            if entry.get(".tag") == "file"
            and entry["name"].lower().endswith(preview_exts)
            and "cryptomatte" not in entry["name"].lower()
            and "conflicted copy" not in entry["name"].lower()
        )
        if total_files <= 0:
            await interaction.update_progress(
                progress_handle,
                "⚠️ No usable image files found for conversion.",
            )
            await interaction.answer("No frames available for preview build.", show_alert=True)
            return

        if state is not None:
            state["total_files"] = total_files
            state["downloaded_files"] = 0

        await download_exr_folder(
            session_dbx,
            "https://content.dropboxapi.com/2/files/download",
            headers_dbx,
            dropbox_path,
            local_root,
            job_id,
            preview_state.download_states,
            preview_state.stop_downloads,
            prefetched_result=list_result,
        )
        if await finalize_cancellation():
            return

        conv_dir = Path(settings.conv_dir) / f"{exr_folder_name}_{job_id}"
        image_files = []
        for pattern in ("*.jpg", "*.jpeg", "*.png"):
            image_files.extend(conv_dir.rglob(pattern))

        if len(image_files) == 1:
            await interaction.update_progress(
                progress_handle,
                "🖼️ Step 2: Sending single frame...",
                cancel_callback_data=cancel_callback_data,
            )
            if await finalize_cancellation():
                return

            image_path = image_files[0]
            project_name = image_path.stem
            try:
                path_parts = (dropbox_path or "").split("/")
                for idx_part, part in enumerate(path_parts):
                    if part == "render" and idx_part + 1 < len(path_parts):
                        project_name = path_parts[idx_part + 1]
                        break
            except Exception:
                pass

            caption = build_preview_caption(project_name, dropbox_path, icon="🖼️")

            await interaction.send_photo(image_path, caption=caption, parse_mode="HTML")

            await interaction.delete_progress(progress_handle)
            try:
                await interaction.answer("Frame sent successfully!")
            except Exception as answer_error:
                logger.warning("Could not answer callback query: %s", answer_error)
            return

        await interaction.update_progress(
            progress_handle,
            "🎬 Step 2: Processing frames and creating video...",
            cancel_callback_data=cancel_callback_data,
        )
        if await finalize_cancellation():
            return

        video_path = await asyncio.to_thread(assemble_video_from_jpg, conv_dir, str(exr_folder_name))
        if await finalize_cancellation():
            return

        video_path_obj = Path(video_path)
        expected_dropbox_video_path = str(PurePosixPath(dropbox_path).parent / video_path_obj.name)

        if await finalize_cancellation():
            return

        await interaction.update_progress(
            progress_handle,
            "📏 Step 3: Checking file size...",
            cancel_callback_data=cancel_callback_data,
        )
        max_video_size_mb = 45.0
        video_size_mb = get_file_size_mb(video_path_obj)

        if video_size_mb > max_video_size_mb:
            await interaction.update_progress(
                progress_handle,
                f"🗜️ Step 3.5: Compressing video ({video_size_mb:.1f} MB → target <{max_video_size_mb:.0f} MB)...",
                cancel_callback_data=cancel_callback_data,
            )
            if await finalize_cancellation():
                return

        preparation = await prepare_video_for_delivery(
            video_path_obj,
            expected_dropbox_video_path,
            max_size_mb=max_video_size_mb,
            initial_size_mb=video_size_mb,
        )
        final_video_path = preparation.video_path
        final_size_mb = preparation.size_mb
        fallback_message = preparation.fallback_message

        if await finalize_cancellation():
            return

        await interaction.update_progress(
            progress_handle,
            f"📤 Step 4: Sending video ({final_size_mb:.1f} MB)...",
            cancel_callback_data=cancel_callback_data,
        )
        if await finalize_cancellation():
            return

        video_filename = final_video_path.name
        expected_dropbox_video_path = str(PurePosixPath(dropbox_path).parent / video_filename)
        project_name = video_filename.replace(".mp4", "")
        try:
            path_parts = (expected_dropbox_video_path or "").split("/")
            for idx_part, part in enumerate(path_parts):
                if part == "render" and idx_part + 1 < len(path_parts):
                    project_name = path_parts[idx_part + 1]
                    break
        except Exception:
            pass

        caption = build_preview_caption(project_name, expected_dropbox_video_path or None)
        if fallback_message:
            await interaction.send_text(fallback_message, parse_mode="HTML")
        else:
            await interaction.send_video(final_video_path, caption=caption, parse_mode="HTML")

        asyncio.create_task(
            _upload_video_to_dropbox_and_cleanup(
                session_dbx=session_dbx,
                headers_dbx=headers_dbx,
                dropbox_path=dropbox_path,
                video_path=final_video_path,
                job_id=job_id,
            )
        )
        defer_cleanup = True

        await interaction.delete_progress(progress_handle)

        try:
            await interaction.answer("Video created successfully!")
        except Exception as answer_error:
            logger.warning("Could not answer callback query: %s", answer_error)

    except Exception as exc:
        from app.core.error_text import describe_error

        logger.error(
            "Error in server-side preview generation for user %s job %s: %s",
            user_id,
            job_id,
            exc,
        )
        user_message = describe_error(exc) or "Error occurred while creating the preview."
        if progress_handle is not None:
            with contextlib.suppress(Exception):
                await interaction.update_progress(progress_handle, f"❌ {user_message}")
        try:
            await interaction.answer(user_message, show_alert=True)
        except Exception as answer_error:
            logger.warning("Could not answer callback query after failure: %s", answer_error)
            await interaction.send_text(f"❌ {user_message}")
        try:
            cleanup_job_files(job_id)
        except Exception as cleanup_error:
            logger.error("Error cleaning up job files after failure: %s", cleanup_error)
    finally:
        if not defer_cleanup:
            try:
                cleanup_job_files(job_id)
            except Exception as cleanup_error:
                logger.error("Error cleaning up job files after preview workflow: %s", cleanup_error)
        preview_state.clear_download(job_id)
