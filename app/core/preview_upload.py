"""
Preview upload endpoint and token helpers for worker-to-bot delivery.
"""

from __future__ import annotations

import html
import logging
import secrets
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Tuple

from aiohttp import web
from aiogram.types import FSInputFile

from app.core.bot_core import bot, notified_jobs
from app.core.config import settings
from app.core.path_utils import normalize_display_path
from app.integrations.video_helpers import prepare_video_for_delivery

logger = logging.getLogger(__name__)

UPLOAD_PATH = "/preview-upload"


@dataclass
class PreviewUploadPayload:
    telegram_user_id: int
    job_name: str
    expected_dropbox_path: Optional[str]
    expected_filename: Optional[str]
    expected_local_path: Optional[str]
    preview_job_id: Optional[str] = None
    source_job_id: Optional[str] = None


class PreviewUploadTokenStore:
    def __init__(self, ttl_seconds: int, max_size: int = 10000) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_size = max_size
        self._entries: Dict[str, Tuple[datetime, PreviewUploadPayload]] = {}

    def issue(self, payload: PreviewUploadPayload) -> str:
        token = secrets.token_urlsafe(32)
        self._entries[token] = (datetime.utcnow() + self._ttl, payload)
        self._cleanup()
        return token

    def update(self, token: str, **updates: object) -> bool:
        entry = self._entries.get(token)
        if not entry:
            return False
        expires_at, payload = entry
        if self._is_expired(expires_at):
            self._entries.pop(token, None)
            return False
        for key, value in updates.items():
            if hasattr(payload, key):
                setattr(payload, key, value)
        return True

    def get(self, token: str) -> Optional[PreviewUploadPayload]:
        entry = self._entries.get(token)
        if not entry:
            return None
        expires_at, payload = entry
        if self._is_expired(expires_at):
            self._entries.pop(token, None)
            return None
        return payload

    def consume(self, token: str) -> Optional[PreviewUploadPayload]:
        payload = self.get(token)
        if payload is None:
            return None
        self._entries.pop(token, None)
        return payload

    def drop(self, token: str) -> None:
        self._entries.pop(token, None)

    def _is_expired(self, expires_at: datetime) -> bool:
        return datetime.utcnow() >= expires_at

    def _cleanup(self) -> None:
        now = datetime.utcnow()
        expired = [token for token, (expires_at, _) in self._entries.items() if expires_at <= now]
        for token in expired:
            self._entries.pop(token, None)
        if len(self._entries) > self._max_size:
            overflow = len(self._entries) - self._max_size
            for token in list(self._entries.keys())[:overflow]:
                self._entries.pop(token, None)


_token_store = PreviewUploadTokenStore(
    ttl_seconds=settings.preview_upload_token_ttl,
    max_size=10000,
)

_upload_runner: Optional[web.AppRunner] = None
_upload_site: Optional[web.BaseSite] = None


def get_preview_upload_url() -> Optional[str]:
    raw = (settings.preview_upload_url or "").strip()
    if not raw:
        return None
    parsed = urllib.parse.urlparse(raw)
    if not parsed.path or parsed.path == "/":
        return raw.rstrip("/") + UPLOAD_PATH
    return raw


def issue_preview_upload_token(payload: PreviewUploadPayload) -> Optional[str]:
    if not settings.preview_upload_enabled:
        return None
    if not get_preview_upload_url():
        logger.warning("Preview upload enabled, but PREVIEW_UPLOAD_URL is not set")
        return None
    return _token_store.issue(payload)


def update_preview_upload_token(token: str, preview_job_id: str) -> None:
    _token_store.update(token, preview_job_id=preview_job_id)


def drop_preview_upload_token(token: str) -> None:
    _token_store.drop(token)


async def start_preview_upload_server() -> None:
    if not settings.preview_upload_enabled:
        return
    global _upload_runner, _upload_site
    if _upload_runner is not None:
        return

    max_size_bytes = settings.preview_upload_max_mb * 1024 * 1024
    app = web.Application(client_max_size=max_size_bytes)
    app.router.add_post(UPLOAD_PATH, _handle_preview_upload)

    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(
            runner,
            host=settings.preview_upload_bind_host,
            port=settings.preview_upload_port,
        )
        await site.start()
    except Exception as exc:
        logger.error("Failed to start preview upload server: %s", exc)
        await runner.cleanup()
        return

    _upload_runner = runner
    _upload_site = site
    logger.info(
        "Preview upload server listening on %s:%s",
        settings.preview_upload_bind_host,
        settings.preview_upload_port,
    )


async def stop_preview_upload_server() -> None:
    global _upload_runner, _upload_site
    if _upload_runner is None:
        return
    await _upload_runner.cleanup()
    _upload_runner = None
    _upload_site = None
    logger.info("Preview upload server stopped")


def _extract_token(request: web.Request) -> Optional[str]:
    header_token = request.headers.get("X-Preview-Token")
    if header_token:
        return header_token.strip()
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()
    query_token = request.query.get("token")
    if query_token:
        return query_token.strip()
    return None


def _sanitize_filename(name: str) -> str:
    cleaned = Path(name).name
    safe = []
    for ch in cleaned:
        if ch.isalnum() or ch in {".", "_", "-"}:
            safe.append(ch)
        else:
            safe.append("_")
    final = "".join(safe).strip("._")
    return final or "preview.mp4"


async def _handle_preview_upload(request: web.Request) -> web.Response:
    token = _extract_token(request)
    if not token:
        return web.Response(status=401, text="Missing token")

    payload = _token_store.get(token)
    if payload is None:
        return web.Response(status=403, text="Invalid or expired token")

    if payload.preview_job_id:
        notified_key = (payload.preview_job_id, payload.telegram_user_id)
        if notified_key in notified_jobs:
            _token_store.consume(token)
            return web.Response(status=200, text="Preview already delivered")

    max_size_bytes = settings.preview_upload_max_mb * 1024 * 1024
    if request.content_length and request.content_length > max_size_bytes:
        return web.Response(status=413, text="Payload too large")

    temp_dir = Path(settings.temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    header_name = request.headers.get("X-Preview-Filename")
    filename = header_name or payload.expected_filename or "preview.mp4"
    filename = _sanitize_filename(filename)

    temp_path = temp_dir / f"upload_{token}_{filename}"
    bytes_written = 0
    try:
        with open(temp_path, "wb") as handle:
            async for chunk in request.content.iter_chunked(1024 * 1024):
                if not chunk:
                    continue
                bytes_written += len(chunk)
                if bytes_written > max_size_bytes:
                    handle.close()
                    temp_path.unlink(missing_ok=True)
                    return web.Response(status=413, text="Payload too large")
                handle.write(chunk)
    except Exception as exc:
        logger.error("Failed to write preview upload: %s", exc)
        temp_path.unlink(missing_ok=True)
        return web.Response(status=500, text="Upload failed")

    try:
        await _deliver_preview(payload, temp_path)
    except Exception as exc:
        logger.error("Failed to deliver preview upload: %s", exc)
        temp_path.unlink(missing_ok=True)
        return web.Response(status=500, text="Delivery failed")

    _token_store.consume(token)
    return web.Response(status=200, text="OK")


async def _deliver_preview(payload: PreviewUploadPayload, temp_path: Path) -> None:
    safe_name = html.escape(payload.job_name or "Preview")

    stored_message = None
    if payload.preview_job_id:
        try:
            from app.core.utils import pop_preview_message

            stored_message = pop_preview_message(payload.preview_job_id)
        except Exception as exc:
            logger.debug("Preview progress message lookup failed: %s", exc)

    ready_text = f"🎬 Preview for {safe_name} is ready."
    target_chat_id = payload.telegram_user_id
    if stored_message:
        chat_id, message_id = stored_message
        target_chat_id = chat_id
        try:
            await bot.edit_message_text(
                ready_text,
                chat_id=chat_id,
                message_id=message_id,
            )
        except Exception as edit_error:
            logger.warning("Failed to edit preview progress message: %s", edit_error)
            await bot.send_message(target_chat_id, ready_text)
    else:
        await bot.send_message(target_chat_id, ready_text)

    path_hint = payload.expected_local_path or payload.expected_dropbox_path
    display_path = normalize_display_path(path_hint)
    preparation = await prepare_video_for_delivery(
        temp_path,
        dropbox_path=display_path,
    )

    display_name = payload.expected_filename or preparation.video_path.name
    caption_parts = [f"📁 {display_name}"]
    if display_path:
        caption_parts.append(f"<code>{display_path}</code>")
    caption = "\n".join(caption_parts)

    if preparation.fallback_message:
        fallback_message = preparation.fallback_message
        if display_path is None:
            fallback_message = (
                "⚠️ Preview video is ready but too large to send via Telegram."
            )
        await bot.send_message(
            target_chat_id,
            fallback_message,
            parse_mode="HTML",
        )
    else:
        await bot.send_video(
            target_chat_id,
            FSInputFile(str(preparation.video_path)),
            caption=caption,
            parse_mode="HTML",
        )

    if payload.preview_job_id:
        notified_jobs.add((payload.preview_job_id, payload.telegram_user_id))

    try:
        from app.services import delete_job_by_user_id

        if payload.preview_job_id:
            await delete_job_by_user_id(payload.telegram_user_id, payload.preview_job_id)
    except Exception as exc:
        logger.warning("Failed to delete preview job %s: %s", payload.preview_job_id, exc)

    for path in {temp_path, preparation.video_path}:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
