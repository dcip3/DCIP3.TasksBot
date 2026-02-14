"""Heavy preview generation pipelines separated from Telegram callback routing."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path, PurePosixPath
from typing import Optional

from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.bot_core import bot, download_states, stop_downloads
from app.core.config import settings
from app.core.path_utils import extract_dropbox_path
from app.core.preview_text import build_preview_caption
from app.core.ui_helpers import cancel_inline_button
from app.integrations.dropbox_helpers import (
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
from app.services.deadline import get_job_info_by_user_id
from app.services.dropbox import get_dropbox_session

logger = logging.getLogger(__name__)


def _build_server_cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                cancel_inline_button(callback_data=f"preview_server_cancel:{job_id}")
            ]
        ]
    )


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


async def render_preview_via_server_pipeline(callback_query: CallbackQuery, job_id: str) -> None:
    """Generate preview on bot host: download frames, convert, assemble, upload, send."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: user not found.", show_alert=True)
        return

    progress_msg: Optional[Message] = None
    cancel_keyboard: Optional[InlineKeyboardMarkup] = None
    stop_event: Optional[asyncio.Event] = None
    state: Optional[dict] = None
    defer_cleanup = False
    try:
        base_message = callback_query.message
        if base_message:
            progress_msg = await base_message.answer("🔍 Starting preview generation...")
        else:
            progress_msg = await bot.send_message(
                callback_query.from_user.id,
                "🔍 Starting preview generation...",
            )

        cancel_keyboard = _build_server_cancel_keyboard(job_id)
        stop_event = stop_downloads.get(job_id)
        if stop_event is None:
            stop_event = asyncio.Event()
            stop_downloads[job_id] = stop_event

        download_states[job_id] = {
            "progress_msg": progress_msg,
            "total_files": 0,
            "downloaded_files": 0,
            "stop_kb": cancel_keyboard,
            "last_progress_ts": 0.0,
            "last_progress_percent": -1,
        }
        state = download_states[job_id]

        async def finalize_cancellation() -> bool:
            if stop_event is None or state is None:
                return False
            if not (stop_event.is_set() or state.get("cancel_requested")):
                return False
            if not state.get("cancel_finalized"):
                state["cancel_finalized"] = True
                state["stop_kb"] = None
                progress = state.get("progress_msg") or progress_msg
                if progress:
                    with contextlib.suppress(Exception):
                        await progress.edit_text("⏹️ Preview generation cancelled.", reply_markup=None)
                try:
                    cleanup_job_files(job_id)
                except Exception as cleanup_error:
                    logger.error("Error cleaning up after cancellation: %s", cleanup_error)
                with contextlib.suppress(Exception):
                    await callback_query.answer("Preview generation cancelled.", show_alert=False)
            return True

        await progress_msg.edit_text(
            "📥 Step 1: Downloading files from Dropbox...",
            reply_markup=cancel_keyboard,
        )
        if await finalize_cancellation():
            return

        job_info = await get_job_info_by_user_id(callback_query.from_user.id, job_id)
        if not job_info:
            await progress_msg.edit_text("❌ Failed to get job info")
            await callback_query.answer("Failed to get job info.", show_alert=True)
            return

        outdirs = job_info.get("OutDir", [])
        if not outdirs:
            await progress_msg.edit_text("❌ No output directory found.")
            await callback_query.answer("Render path is missing.", show_alert=True)
            return

        fullpath = outdirs[0]
        if fullpath.find(settings.dropbox_root_marker) == -1:
            await progress_msg.edit_text("❌ Dropbox root marker not found in path.")
            await callback_query.answer("Could not determine Dropbox path.", show_alert=True)
            return

        dropbox_path = extract_dropbox_path(fullpath, settings.dropbox_root_marker)
        if not dropbox_path:
            await progress_msg.edit_text("❌ Could not normalize Dropbox path.")
            await callback_query.answer("Could not determine Dropbox path.", show_alert=True)
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
        session_dbx = await get_dropbox_session()

        list_result = await list_folder_all(session_dbx, dropbox_path, headers_dbx)
        if not list_result:
            await progress_msg.edit_text("❌ Failed to list folder.")
            await callback_query.answer("Could not list files in Dropbox.", show_alert=True)
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
            await progress_msg.edit_text("⚠️ No usable image files found for conversion.")
            await callback_query.answer("No frames available for preview build.", show_alert=True)
            return

        if state is not None:
            state["total_files"] = total_files
            state["downloaded_files"] = 0
            state["stop_kb"] = cancel_keyboard

        await download_exr_folder(
            session_dbx,
            "https://content.dropboxapi.com/2/files/download",
            headers_dbx,
            dropbox_path,
            local_root,
            job_id,
            download_states,
            stop_downloads,
            prefetched_result=list_result,
        )
        if await finalize_cancellation():
            return

        conv_dir = Path(settings.conv_dir) / f"{exr_folder_name}_{job_id}"
        image_files = []
        for pattern in ("*.jpg", "*.jpeg", "*.png"):
            image_files.extend(conv_dir.rglob(pattern))

        if len(image_files) == 1:
            await progress_msg.edit_text(
                "🖼️ Step 2: Sending single frame...",
                reply_markup=cancel_keyboard,
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

            caption_lines = [f"🖼️ {project_name}"]
            if dropbox_path:
                caption_lines.append(f"<code>{dropbox_path}</code>")
            caption = "\n".join(caption_lines)

            if callback_query.message:
                await callback_query.message.answer_photo(
                    photo=FSInputFile(str(image_path)),
                    caption=caption,
                    parse_mode="HTML",
                )
            else:
                await bot.send_photo(
                    callback_query.from_user.id,
                    FSInputFile(str(image_path)),
                    caption=caption,
                    parse_mode="HTML",
                )

            with contextlib.suppress(Exception):
                await progress_msg.delete()
            try:
                await callback_query.answer("Frame sent successfully!")
            except Exception as answer_error:
                logger.warning("Could not answer callback query: %s", answer_error)
            return

        await progress_msg.edit_text(
            "🎬 Step 2: Processing frames and creating video...",
            reply_markup=cancel_keyboard,
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

        await progress_msg.edit_text(
            "📏 Step 3: Checking file size...",
            reply_markup=cancel_keyboard,
        )
        max_video_size_mb = 45.0
        video_size_mb = get_file_size_mb(video_path_obj)

        if video_size_mb > max_video_size_mb:
            await progress_msg.edit_text(
                f"🗜️ Step 3.5: Compressing video ({video_size_mb:.1f} MB → target <{max_video_size_mb:.0f} MB)...",
                reply_markup=cancel_keyboard,
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

        await progress_msg.edit_text(
            f"📤 Step 4: Sending video ({final_size_mb:.1f} MB)...",
            reply_markup=cancel_keyboard,
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
            if callback_query.message:
                await callback_query.message.answer(
                    fallback_message,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    callback_query.from_user.id,
                    fallback_message,
                    parse_mode="HTML",
                )
        else:
            if callback_query.message:
                await callback_query.message.answer_video(
                    video=FSInputFile(str(final_video_path)),
                    caption=caption,
                    parse_mode="HTML",
                )
            else:
                await bot.send_video(
                    callback_query.from_user.id,
                    FSInputFile(str(final_video_path)),
                    caption=caption,
                    parse_mode="HTML",
                )

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

        with contextlib.suppress(Exception):
            await progress_msg.delete()

        try:
            await callback_query.answer("Video created successfully!")
        except Exception as answer_error:
            logger.warning("Could not answer callback query: %s", answer_error)

    except Exception as exc:
        from app.core.error_text import describe_error

        logger.error(
            "Error in server-side preview generation for user %s job %s: %s",
            callback_query.from_user.id if callback_query.from_user else "unknown",
            job_id,
            exc,
        )
        user_message = describe_error(exc) or "Error occurred while creating the preview."
        if progress_msg:
            with contextlib.suppress(Exception):
                await progress_msg.edit_text(f"❌ {user_message}")
        try:
            await callback_query.answer(user_message, show_alert=True)
        except Exception as answer_error:
            logger.warning("Could not answer callback query after failure: %s", answer_error)
            if callback_query.message:
                await callback_query.message.answer(f"❌ {user_message}")
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
        download_states.pop(job_id, None)
        stop_downloads.pop(job_id, None)
