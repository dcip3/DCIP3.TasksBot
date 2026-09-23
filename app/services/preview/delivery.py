"""Shared helpers for delivering ready preview videos to a user.

Both `core.preview_upload` (worker push) and `services.preview.runtime`
(Deadline-watcher path) end with the same sequence: edit the stored progress
message to "ready", send the video (or a fallback message if too large),
record `notified_jobs`, and delete the preview job from Deadline. This module
extracts that common tail so both call sites share one implementation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from aiogram.types import FSInputFile, InlineKeyboardMarkup

from app.core.bot_core import bot
from app.integrations.video_helpers import (
    VideoDeliveryPreparation,
    make_video_thumbnail,
    probe_video_metadata,
)
from app.services.job_state import notified_jobs
from app.services.preview.state import preview_state

logger = logging.getLogger(__name__)
PREVIEW_IMAGE_EXTS = {".png", ".jpg", ".jpeg"}


async def _send_video_with_shape(chat_id: int, video_path: Path, caption: str) -> None:
    """Send a preview, telling Telegram what shape it is.

    Without explicit width/height and a matching thumbnail, Telegram clients
    guess the player geometry - and phones in particular guess square, which
    squashes a 3:2 render. Desktop usually gets it right, which is why this only
    showed up on mobile. Both the dimensions and the poster frame are read from
    the file actually being sent, so a video the bot re-compressed still reports
    its real size.
    """
    metadata = await asyncio.to_thread(probe_video_metadata, video_path)
    thumb_path = await asyncio.to_thread(make_video_thumbnail, video_path)

    kwargs: dict = {}
    if metadata:
        kwargs.update(
            width=metadata.width,
            height=metadata.height,
            supports_streaming=True,
        )
        if metadata.duration:
            kwargs["duration"] = metadata.duration
    else:
        logger.warning("No video metadata for %s; Telegram will guess", video_path.name)
    if thumb_path:
        kwargs["thumbnail"] = FSInputFile(str(thumb_path))

    try:
        await bot.send_video(
            chat_id,
            FSInputFile(str(video_path)),
            caption=caption,
            parse_mode="HTML",
            **kwargs,
        )
    finally:
        if thumb_path:
            with contextlib.suppress(Exception):
                thumb_path.unlink()


def is_preview_image_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in PREVIEW_IMAGE_EXTS


def _peek_message(preview_job_id: Optional[str]) -> Optional[tuple[int, int]]:
    if not preview_job_id:
        return None
    return preview_state.message_registry.get(preview_job_id)


def _pop_message(preview_job_id: Optional[str]) -> Optional[tuple[int, int]]:
    if not preview_job_id:
        return None
    # Silent auto previews are followed without a message; stop following them
    # here too, otherwise the watcher would keep polling a delivered job.
    preview_state.tracked_previews.pop(preview_job_id, None)
    return preview_state.message_registry.pop(preview_job_id, None)


async def _edit_or_send(
    chat_id: int,
    text: str,
    *,
    message_id: Optional[int],
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Optional[int]:
    """Edit a stored progress message in-place; fall back to a fresh message on failure."""
    if message_id is not None:
        try:
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
            return message_id
        except Exception as edit_error:
            logger.warning("Failed to edit preview progress message: %s", edit_error)
    sent_message = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    return getattr(sent_message, "message_id", None)


async def _delete_message(chat_id: int, message_id: Optional[int]) -> None:
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as exc:
        logger.warning("Failed to delete preview ready message %s: %s", message_id, exc)


async def send_ready_preview_video(
    *,
    target_user_id: int,
    preview_job_id: Optional[str],
    job_name: str,
    preparation: VideoDeliveryPreparation,
    caption: str,
    delete_job: Callable[[], Awaitable[bool]],
) -> None:
    """Edit the "ready" message, send the video (or fallback), notify and delete.

    `delete_job` lets the caller choose between credentials- and user_id-based
    deletion (`deadline.delete_job` vs `delete_job_by_user_id`).
    """
    stored = _peek_message(preview_job_id)
    chat_id, message_id = stored if stored else (target_user_id, None)

    ready_text = f"🎬 Preview for {job_name} is ready."
    ready_message_id = await _edit_or_send(chat_id, ready_text, message_id=message_id)
    is_image = is_preview_image_path(preparation.video_path)

    if preparation.fallback_message:
        if preparation.size_mb:
            logger.warning(
                "Preview video %s is %.1f MB; sending fallback message",
                preparation.video_path.name,
                preparation.size_mb,
            )
        await bot.send_message(
            chat_id,
            preparation.fallback_message,
            parse_mode="HTML",
        )
    elif is_image:
        await bot.send_photo(
            chat_id,
            FSInputFile(str(preparation.video_path)),
            caption=caption,
            parse_mode="HTML",
        )
    else:
        await _send_video_with_shape(chat_id, preparation.video_path, caption)

    await _delete_message(chat_id, ready_message_id)

    if preview_job_id:
        _pop_message(preview_job_id)
        notified_jobs.add((preview_job_id, target_user_id))

    try:
        await delete_job()
    except Exception as exc:
        logger.warning("Failed to delete preview job %s: %s", preview_job_id, exc)
