"""
Preview upload endpoint and token helpers for worker-to-bot delivery.
"""

import html
import json
import logging
import secrets
import time
import urllib.parse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiofiles
import aiosqlite
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
        self._ttl_seconds = int(ttl_seconds)
        self._max_size = max_size
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def _open_db(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(settings.sqlite_db_path)
        await conn.execute("PRAGMA journal_mode=WAL")
        return conn

    async def _ensure_schema(self, conn: aiosqlite.Connection) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS preview_upload_tokens (
                    token TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_preview_upload_tokens_expires
                ON preview_upload_tokens(expires_at)
                """
            )
            await conn.commit()
            self._schema_ready = True

    def _serialize_payload(self, payload: PreviewUploadPayload) -> str:
        data = {
            "telegram_user_id": payload.telegram_user_id,
            "job_name": payload.job_name,
            "expected_dropbox_path": payload.expected_dropbox_path,
            "expected_filename": payload.expected_filename,
            "expected_local_path": payload.expected_local_path,
            "preview_job_id": payload.preview_job_id,
            "source_job_id": payload.source_job_id,
        }
        return json.dumps(data, ensure_ascii=True)

    def _deserialize_payload(self, raw: str) -> Optional[PreviewUploadPayload]:
        try:
            data = json.loads(raw)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        try:
            return PreviewUploadPayload(
                telegram_user_id=int(data.get("telegram_user_id")),
                job_name=str(data.get("job_name") or ""),
                expected_dropbox_path=data.get("expected_dropbox_path"),
                expected_filename=data.get("expected_filename"),
                expected_local_path=data.get("expected_local_path"),
                preview_job_id=data.get("preview_job_id"),
                source_job_id=data.get("source_job_id"),
            )
        except Exception:
            return None

    async def _cleanup(self, conn: aiosqlite.Connection) -> None:
        now = int(time.time())
        await conn.execute("DELETE FROM preview_upload_tokens WHERE expires_at <= ?", (now,))
        if self._max_size <= 0:
            return
        async with conn.execute("SELECT COUNT(*) FROM preview_upload_tokens") as cur:
            row = await cur.fetchone()
        total = row[0] if row else 0
        overflow = total - self._max_size
        if overflow > 0:
            await conn.execute(
                """
                DELETE FROM preview_upload_tokens
                WHERE token IN (
                    SELECT token FROM preview_upload_tokens
                    ORDER BY created_at ASC
                    LIMIT ?
                )
                """,
                (overflow,),
            )

    async def issue(self, payload: PreviewUploadPayload) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        expires_at = now + self._ttl_seconds
        async with await self._open_db() as conn:
            await self._ensure_schema(conn)
            await conn.execute(
                """
                INSERT OR REPLACE INTO preview_upload_tokens
                (token, expires_at, created_at, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (token, expires_at, now, self._serialize_payload(payload)),
            )
            await self._cleanup(conn)
            await conn.commit()
        return token

    async def update(self, token: str, **updates: object) -> bool:
        async with await self._open_db() as conn:
            await self._ensure_schema(conn)
            async with conn.execute(
                "SELECT expires_at, payload_json FROM preview_upload_tokens WHERE token = ?",
                (token,),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return False
            expires_at, payload_json = row
            if int(expires_at) <= int(time.time()):
                await conn.execute("DELETE FROM preview_upload_tokens WHERE token = ?", (token,))
                await conn.commit()
                return False
            payload = self._deserialize_payload(payload_json)
            if payload is None:
                await conn.execute("DELETE FROM preview_upload_tokens WHERE token = ?", (token,))
                await conn.commit()
                return False
            for key, value in updates.items():
                if hasattr(payload, key):
                    setattr(payload, key, value)
            await conn.execute(
                "UPDATE preview_upload_tokens SET payload_json = ? WHERE token = ?",
                (self._serialize_payload(payload), token),
            )
            await conn.commit()
            return True

    async def get(self, token: str) -> Optional[PreviewUploadPayload]:
        async with await self._open_db() as conn:
            await self._ensure_schema(conn)
            async with conn.execute(
                "SELECT expires_at, payload_json FROM preview_upload_tokens WHERE token = ?",
                (token,),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return None
            expires_at, payload_json = row
            if int(expires_at) <= int(time.time()):
                await conn.execute("DELETE FROM preview_upload_tokens WHERE token = ?", (token,))
                await conn.commit()
                return None
            return self._deserialize_payload(payload_json)

    async def consume(self, token: str) -> Optional[PreviewUploadPayload]:
        payload = await self.get(token)
        if payload is None:
            return None
        async with await self._open_db() as conn:
            await self._ensure_schema(conn)
            await conn.execute("DELETE FROM preview_upload_tokens WHERE token = ?", (token,))
            await conn.commit()
        return payload

    async def drop(self, token: str) -> None:
        async with await self._open_db() as conn:
            await self._ensure_schema(conn)
            await conn.execute("DELETE FROM preview_upload_tokens WHERE token = ?", (token,))
            await conn.commit()

    async def cleanup(self) -> None:
        async with await self._open_db() as conn:
            await self._ensure_schema(conn)
            await self._cleanup(conn)
            await conn.commit()


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


async def issue_preview_upload_token(payload: PreviewUploadPayload) -> Optional[str]:
    if not settings.preview_upload_enabled:
        return None
    if not get_preview_upload_url():
        logger.warning("Preview upload enabled, but PREVIEW_UPLOAD_URL is not set")
        return None
    return await _token_store.issue(payload)


async def update_preview_upload_token(token: str, preview_job_id: str) -> None:
    await _token_store.update(token, preview_job_id=preview_job_id)


async def drop_preview_upload_token(token: str) -> None:
    await _token_store.drop(token)


async def cleanup_preview_upload_tokens() -> None:
    await _token_store.cleanup()


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

    payload = await _token_store.get(token)
    if payload is None:
        return web.Response(status=403, text="Invalid or expired token")

    if payload.preview_job_id:
        notified_key = (payload.preview_job_id, payload.telegram_user_id)
        if notified_key in notified_jobs:
            await _token_store.consume(token)
            return web.Response(status=200, text="Preview already delivered")

    max_size_bytes = settings.preview_upload_max_mb * 1024 * 1024
    if request.content_length and request.content_length > max_size_bytes:
        return web.Response(status=413, text="Payload too large")

    temp_dir = Path(settings.temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    header_name = request.headers.get("X-Preview-Filename")
    filename = header_name or payload.expected_filename or "preview.mp4"
    filename = _sanitize_filename(filename)

    upload_dir = temp_dir / f"upload_{token}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    temp_path = upload_dir / filename
    bytes_written = 0
    try:
        async with aiofiles.open(temp_path, "wb") as handle:
            async for chunk in request.content.iter_chunked(1024 * 1024):
                if not chunk:
                    continue
                bytes_written += len(chunk)
                if bytes_written > max_size_bytes:
                    temp_path.unlink(missing_ok=True)
                    return web.Response(status=413, text="Payload too large")
                await handle.write(chunk)
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

    await _token_store.consume(token)
    return web.Response(status=200, text="OK")


async def _deliver_preview(payload: PreviewUploadPayload, temp_path: Path) -> None:
    safe_name = html.escape(payload.job_name or "Preview")

    stored_message = None
    if payload.preview_job_id:
        try:
            from app.services.preview.runtime import pop_preview_message

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

    from app.core.preview_text import build_preview_caption

    display_name = payload.expected_filename or preparation.video_path.name
    caption = build_preview_caption(display_name, display_path)

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
        from app.services.deadline import delete_job_by_user_id

        if payload.preview_job_id:
            await delete_job_by_user_id(payload.telegram_user_id, payload.preview_job_id)
    except Exception as exc:
        logger.warning("Failed to delete preview job %s: %s", payload.preview_job_id, exc)

    for path in {temp_path, preparation.video_path}:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    for folder in {temp_path.parent, preparation.video_path.parent}:
        try:
            if folder.name.startswith("upload_"):
                folder.rmdir()
        except Exception:
            pass
