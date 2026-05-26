"""
Preview upload endpoint and token helpers for worker-to-bot delivery.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import secrets
import shutil
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiofiles
import aiosqlite
from aiohttp import web

from app.core.bot_core import bot
from app.core.config import settings
from app.core.path_utils import normalize_preview_path
from app.integrations.video_helpers import prepare_video_for_delivery
from app.services.job_state import notified_jobs
from app.storage.schema import ensure_preview_upload_schema

logger = logging.getLogger(__name__)

UPLOAD_PATH = "/preview-upload"
STATUS_ISSUED = "issued"
STATUS_CLAIMED = "claimed"
STATUS_RECEIVED = "received"
STATUS_DELIVERING = "delivering"
STATUS_FAILED = "failed"
_DELIVERY_LEASE_SECONDS = 5 * 60
_UPLOAD_STATUSES_WITH_FILE = {STATUS_RECEIVED, STATUS_DELIVERING, STATUS_FAILED}
_UPLOAD_PART_SUFFIX = ".part"
_STATE_COLUMNS = """
    token, expires_at, created_at, claimed_until, payload_json,
    preview_job_id, source_job_id, status, temp_path, bytes_written,
    received_at, delivery_attempts, next_retry_at, last_error
"""


@dataclass
class PreviewUploadPayload:
    telegram_user_id: int
    job_name: str
    expected_dropbox_path: Optional[str]
    expected_filename: Optional[str]
    expected_local_path: Optional[str]
    preview_job_id: Optional[str] = None
    source_job_id: Optional[str] = None
    expected_render_path: Optional[str] = None


@dataclass
class PreviewUploadState:
    token: str
    expires_at: int
    created_at: int
    claimed_until: int
    payload: PreviewUploadPayload
    preview_job_id: Optional[str]
    source_job_id: Optional[str]
    status: str
    temp_path: Optional[str]
    bytes_written: int
    received_at: int
    delivery_attempts: int
    next_retry_at: int
    last_error: Optional[str]

    @property
    def has_received_file(self) -> bool:
        return bool(self.temp_path) and self.status in _UPLOAD_STATUSES_WITH_FILE

    @property
    def attempts_exhausted(self) -> bool:
        return self.delivery_attempts >= settings.preview_upload_delivery_max_attempts


class PreviewUploadTokenStore:
    def __init__(self, ttl_seconds: int, max_size: int = 10000) -> None:
        self._ttl_seconds = int(ttl_seconds)
        self._max_size = max_size
        self._claim_lease_seconds = 5 * 60
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    def _open_db(self) -> aiosqlite.Connection:
        return aiosqlite.connect(settings.sqlite_db_path)

    async def _ensure_schema(self, conn: aiosqlite.Connection) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            await ensure_preview_upload_schema(conn)
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
            "expected_render_path": payload.expected_render_path,
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
                expected_render_path=data.get("expected_render_path"),
            )
        except Exception:
            return None

    def _row_to_state(self, row: tuple) -> Optional[PreviewUploadState]:
        (
            token,
            expires_at,
            created_at,
            claimed_until,
            payload_json,
            preview_job_id,
            source_job_id,
            status,
            temp_path,
            bytes_written,
            received_at,
            delivery_attempts,
            next_retry_at,
            last_error,
        ) = row
        payload = self._deserialize_payload(payload_json)
        if payload is None:
            return None
        if preview_job_id and not payload.preview_job_id:
            payload.preview_job_id = str(preview_job_id)
        if source_job_id and not payload.source_job_id:
            payload.source_job_id = str(source_job_id)
        return PreviewUploadState(
            token=str(token),
            expires_at=int(expires_at),
            created_at=int(created_at),
            claimed_until=int(claimed_until or 0),
            payload=payload,
            preview_job_id=str(preview_job_id) if preview_job_id else payload.preview_job_id,
            source_job_id=str(source_job_id) if source_job_id else payload.source_job_id,
            status=str(status or STATUS_ISSUED),
            temp_path=str(temp_path) if temp_path else None,
            bytes_written=int(bytes_written or 0),
            received_at=int(received_at or 0),
            delivery_attempts=int(delivery_attempts or 0),
            next_retry_at=int(next_retry_at or 0),
            last_error=str(last_error) if last_error else None,
        )

    def _cleanup_upload_artifacts(self, token: str, temp_path: Optional[str]) -> None:
        candidates: list[Path] = []
        if temp_path:
            candidates.append(Path(temp_path))
        candidates.append(Path(settings.temp_dir) / f"upload_{token}")

        seen: set[Path] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                if candidate.is_file():
                    parent = candidate.parent
                    candidate.unlink(missing_ok=True)
                    if parent.name.startswith("upload_"):
                        shutil.rmtree(parent, ignore_errors=True)
                elif candidate.is_dir() and candidate.name.startswith("upload_"):
                    shutil.rmtree(candidate, ignore_errors=True)
            except Exception as exc:
                logger.warning("Failed to remove stale preview upload %s: %s", candidate, exc)

    async def _cleanup(self, conn: aiosqlite.Connection) -> None:
        now = int(time.time())
        async with conn.execute(
            "SELECT token, temp_path FROM preview_upload_tokens WHERE expires_at <= ?",
            (now,),
        ) as cur:
            expired_rows = await cur.fetchall()
        for token, temp_path in expired_rows:
            self._cleanup_upload_artifacts(str(token), temp_path)
        await conn.execute("DELETE FROM preview_upload_tokens WHERE expires_at <= ?", (now,))
        if self._max_size <= 0:
            return
        async with conn.execute("SELECT COUNT(*) FROM preview_upload_tokens") as cur:
            row = await cur.fetchone()
        total = row[0] if row else 0
        overflow = total - self._max_size
        if overflow > 0:
            async with conn.execute(
                """
                SELECT token, temp_path FROM preview_upload_tokens
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (overflow,),
            ) as cur:
                overflow_rows = await cur.fetchall()
            for token, temp_path in overflow_rows:
                self._cleanup_upload_artifacts(str(token), temp_path)
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
        async with self._open_db() as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await self._ensure_schema(conn)
            await conn.execute(
                """
                INSERT OR REPLACE INTO preview_upload_tokens
                (
                    token, expires_at, created_at, claimed_until,
                    preview_job_id, source_job_id, status, temp_path,
                    bytes_written, received_at, delivery_attempts,
                    next_retry_at, last_error, payload_json
                )
                VALUES (?, ?, ?, 0, ?, ?, ?, NULL, 0, 0, 0, 0, NULL, ?)
                """,
                (
                    token,
                    expires_at,
                    now,
                    payload.preview_job_id,
                    payload.source_job_id,
                    STATUS_ISSUED,
                    self._serialize_payload(payload),
                ),
            )
            await self._cleanup(conn)
            await conn.commit()
        return token

    async def claim(self, token: str) -> Optional[PreviewUploadPayload]:
        """Atomically claim a token so only one upload request can write the file."""
        now = int(time.time())
        claim_until = now + self._claim_lease_seconds
        async with self._open_db() as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await self._ensure_schema(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                async with conn.execute(
                    f"SELECT {_STATE_COLUMNS} FROM preview_upload_tokens WHERE token = ?",
                    (token,),
                ) as cur:
                    row = await cur.fetchone()
                if not row:
                    await conn.rollback()
                    return None

                state = self._row_to_state(row)
                if state is None:
                    await conn.execute("DELETE FROM preview_upload_tokens WHERE token = ?", (token,))
                    await conn.commit()
                    return None
                if state.expires_at <= now:
                    self._cleanup_upload_artifacts(token, state.temp_path)
                    await conn.execute("DELETE FROM preview_upload_tokens WHERE token = ?", (token,))
                    await conn.commit()
                    return None
                if state.status in _UPLOAD_STATUSES_WITH_FILE:
                    await conn.rollback()
                    return None
                if state.claimed_until > now:
                    await conn.rollback()
                    return None

                await conn.execute(
                    """
                    UPDATE preview_upload_tokens
                    SET claimed_until = ?, status = ?
                    WHERE token = ?
                    """,
                    (claim_until, STATUS_CLAIMED, token),
                )
                await conn.commit()
                return state.payload
            except Exception:
                await conn.rollback()
                raise

    async def update(self, token: str, **updates: object) -> bool:
        async with self._open_db() as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
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
                """
                UPDATE preview_upload_tokens
                SET payload_json = ?,
                    preview_job_id = ?,
                    source_job_id = ?
                WHERE token = ?
                """,
                (
                    self._serialize_payload(payload),
                    payload.preview_job_id,
                    payload.source_job_id,
                    token,
                ),
            )
            await conn.commit()
            return True

    async def get_state(self, token: str) -> Optional[PreviewUploadState]:
        async with self._open_db() as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await self._ensure_schema(conn)
            async with conn.execute(
                f"SELECT {_STATE_COLUMNS} FROM preview_upload_tokens WHERE token = ?",
                (token,),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return None
            state = self._row_to_state(row)
            if state is None:
                return None
            if state.expires_at <= int(time.time()):
                self._cleanup_upload_artifacts(token, state.temp_path)
                await conn.execute("DELETE FROM preview_upload_tokens WHERE token = ?", (token,))
                await conn.commit()
                return None
            return state

    async def get_by_preview_job(self, preview_job_id: str) -> Optional[PreviewUploadState]:
        async with self._open_db() as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await self._ensure_schema(conn)
            async with conn.execute(
                f"""
                SELECT {_STATE_COLUMNS}
                FROM preview_upload_tokens
                WHERE preview_job_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (preview_job_id,),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return None
            state = self._row_to_state(row)
            if state is None:
                return None
            if state.expires_at <= int(time.time()):
                self._cleanup_upload_artifacts(state.token, state.temp_path)
                await conn.execute(
                    "DELETE FROM preview_upload_tokens WHERE token = ?",
                    (state.token,),
                )
                await conn.commit()
                return None
            return state

    async def mark_received(self, token: str, temp_path: Path, bytes_written: int) -> bool:
        now = int(time.time())
        async with self._open_db() as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await self._ensure_schema(conn)
            await conn.execute(
                """
                UPDATE preview_upload_tokens
                SET status = ?,
                    temp_path = ?,
                    bytes_written = ?,
                    received_at = ?,
                    claimed_until = 0,
                    next_retry_at = ?
                WHERE token = ?
                """,
                (
                    STATUS_RECEIVED,
                    str(temp_path),
                    int(bytes_written),
                    now,
                    now,
                    token,
                ),
            )
            await conn.commit()
            return conn.total_changes > 0

    async def mark_delivery_started(self, token: str) -> Optional[PreviewUploadState]:
        now = int(time.time())
        lease_until = now + _DELIVERY_LEASE_SECONDS
        async with self._open_db() as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await self._ensure_schema(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                async with conn.execute(
                    f"SELECT {_STATE_COLUMNS} FROM preview_upload_tokens WHERE token = ?",
                    (token,),
                ) as cur:
                    row = await cur.fetchone()
                if not row:
                    await conn.rollback()
                    return None
                state = self._row_to_state(row)
                if state is None:
                    await conn.execute("DELETE FROM preview_upload_tokens WHERE token = ?", (token,))
                    await conn.commit()
                    return None
                if state.expires_at <= now:
                    self._cleanup_upload_artifacts(token, state.temp_path)
                    await conn.execute("DELETE FROM preview_upload_tokens WHERE token = ?", (token,))
                    await conn.commit()
                    return None
                if state.delivery_attempts >= settings.preview_upload_delivery_max_attempts:
                    await conn.rollback()
                    return None
                if state.status == STATUS_DELIVERING and state.next_retry_at > now:
                    await conn.rollback()
                    return None
                if state.status not in _UPLOAD_STATUSES_WITH_FILE:
                    await conn.rollback()
                    return None

                attempts = state.delivery_attempts + 1
                await conn.execute(
                    """
                    UPDATE preview_upload_tokens
                    SET status = ?,
                        delivery_attempts = ?,
                        next_retry_at = ?
                    WHERE token = ?
                    """,
                    (STATUS_DELIVERING, attempts, lease_until, token),
                )
                await conn.commit()
                state.status = STATUS_DELIVERING
                state.delivery_attempts = attempts
                state.next_retry_at = lease_until
                return state
            except Exception:
                await conn.rollback()
                raise

    async def mark_delivery_failed(self, token: str, error: str) -> None:
        now = int(time.time())
        state = await self.get_state(token)
        if state is None:
            return
        retry_delay = _delivery_retry_delay_seconds(state.delivery_attempts)
        next_retry_at = (
            0
            if state.delivery_attempts >= settings.preview_upload_delivery_max_attempts
            else now + retry_delay
        )
        async with self._open_db() as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await self._ensure_schema(conn)
            await conn.execute(
                """
                UPDATE preview_upload_tokens
                SET status = ?,
                    next_retry_at = ?,
                    last_error = ?
                WHERE token = ?
                """,
                (STATUS_FAILED, next_retry_at, error[:1000], token),
            )
            await conn.commit()

    async def release_claim(self, token: str) -> None:
        async with self._open_db() as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await self._ensure_schema(conn)
            await conn.execute(
                """
                UPDATE preview_upload_tokens
                SET claimed_until = 0,
                    status = CASE WHEN status = ? THEN ? ELSE status END
                WHERE token = ?
                """,
                (STATUS_CLAIMED, STATUS_ISSUED, token),
            )
            await conn.commit()

    async def consume_claimed(self, token: str) -> None:
        await self.drop(token)

    async def drop(self, token: str) -> None:
        async with self._open_db() as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await self._ensure_schema(conn)
            async with conn.execute(
                "SELECT temp_path FROM preview_upload_tokens WHERE token = ?",
                (token,),
            ) as cur:
                row = await cur.fetchone()
            temp_path = row[0] if row else None
            await conn.execute("DELETE FROM preview_upload_tokens WHERE token = ?", (token,))
            await conn.commit()
        self._cleanup_upload_artifacts(token, temp_path)

    async def cleanup(self) -> None:
        async with self._open_db() as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await self._ensure_schema(conn)
            await self._cleanup(conn)
            await conn.commit()

    async def list_recoverable(self) -> list[PreviewUploadState]:
        now = int(time.time())
        async with self._open_db() as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await self._ensure_schema(conn)
            async with conn.execute(
                f"""
                SELECT {_STATE_COLUMNS}
                FROM preview_upload_tokens
                WHERE expires_at > ?
                  AND status IN (?, ?, ?, ?, ?)
                ORDER BY created_at ASC
                """,
                (
                    now,
                    STATUS_ISSUED,
                    STATUS_CLAIMED,
                    STATUS_RECEIVED,
                    STATUS_DELIVERING,
                    STATUS_FAILED,
                ),
            ) as cur:
                rows = await cur.fetchall()
        states: list[PreviewUploadState] = []
        for row in rows:
            state = self._row_to_state(row)
            if state is not None:
                states.append(state)
        return states


_token_store = PreviewUploadTokenStore(
    ttl_seconds=settings.preview_upload_token_ttl,
    max_size=10000,
)

_upload_runner: Optional[web.AppRunner] = None
_upload_site: Optional[web.BaseSite] = None
_delivery_tasks: dict[str, asyncio.Task[None]] = {}


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


async def get_preview_upload_state_for_job(preview_job_id: str) -> Optional[PreviewUploadState]:
    if not preview_job_id:
        return None
    return await _token_store.get_by_preview_job(preview_job_id)


async def drop_preview_upload_token(token: str) -> None:
    await _token_store.drop(token)


async def cleanup_preview_upload_tokens() -> None:
    await _token_store.cleanup()


async def recover_preview_uploads() -> int:
    """Schedule retry delivery for received uploads and recover legacy upload dirs."""
    if not settings.preview_upload_enabled:
        return 0

    await _token_store.cleanup()
    now = int(time.time())
    scheduled = 0
    for state in await _token_store.list_recoverable():
        if state.delivery_attempts >= settings.preview_upload_delivery_max_attempts:
            continue

        temp_path = _resolve_upload_temp_path(state)
        if temp_path and state.status in {STATUS_ISSUED, STATUS_CLAIMED}:
            await _token_store.mark_received(
                state.token,
                temp_path,
                temp_path.stat().st_size,
            )
            state = await _token_store.get_state(state.token) or state

        if state.status in {STATUS_RECEIVED, STATUS_FAILED}:
            if state.next_retry_at and state.next_retry_at > now:
                continue
            if _start_delivery_task(state.token):
                scheduled += 1
        elif state.status == STATUS_DELIVERING and state.next_retry_at <= now:
            if _start_delivery_task(state.token):
                scheduled += 1
    return scheduled


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
    if _upload_runner is not None:
        await _upload_runner.cleanup()
        _upload_runner = None
        _upload_site = None
        logger.info("Preview upload server stopped")

    tasks = list(_delivery_tasks.values())
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _delivery_tasks.clear()


def _extract_token(request: web.Request) -> Optional[str]:
    header_token = request.headers.get("X-Preview-Token")
    if header_token:
        return header_token.strip()
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()
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


def _delivery_retry_delay_seconds(attempts: int) -> int:
    if attempts <= 0:
        return 60
    return min(60 * (2 ** max(0, attempts - 1)), 10 * 60)


def _resolve_upload_temp_path(state: PreviewUploadState) -> Optional[Path]:
    candidates: list[Path] = []
    if state.temp_path:
        candidates.append(Path(state.temp_path))
    upload_dir = Path(settings.temp_dir) / f"upload_{state.token}"
    if state.payload.expected_filename:
        candidates.append(upload_dir / _sanitize_filename(state.payload.expected_filename))
    if upload_dir.exists():
        candidates.extend(
            path
            for path in upload_dir.iterdir()
            if path.is_file() and not path.name.endswith(_UPLOAD_PART_SUFFIX)
        )

    for candidate in candidates:
        try:
            if candidate.is_file() and not candidate.name.endswith(_UPLOAD_PART_SUFFIX):
                return candidate
        except Exception:
            continue
    return None


def _start_delivery_task(token: str) -> bool:
    existing = _delivery_tasks.get(token)
    if existing and not existing.done():
        return False

    task = asyncio.create_task(_deliver_received_upload(token))
    _delivery_tasks[token] = task

    def _cleanup_task(done_task: asyncio.Task[None]) -> None:
        _delivery_tasks.pop(token, None)
        with contextlib.suppress(asyncio.CancelledError):
            exc = done_task.exception()
            if exc:
                logger.error("Preview upload delivery task failed for %s: %s", token, exc)

    task.add_done_callback(_cleanup_task)
    return True


async def _handle_preview_upload(request: web.Request) -> web.Response:
    token = _extract_token(request)
    if not token:
        return web.Response(status=401, text="Missing token")

    payload = await _token_store.claim(token)
    if payload is None:
        state = await _token_store.get_state(token)
        if state and state.status in _UPLOAD_STATUSES_WITH_FILE and _resolve_upload_temp_path(state):
            _start_delivery_task(token)
            return web.Response(status=202, text="Preview already received")
        return web.Response(status=403, text="Invalid, expired, or in-progress token")

    should_release_claim = True

    try:
        if payload.preview_job_id:
            notified_key = (payload.preview_job_id, payload.telegram_user_id)
            if notified_key in notified_jobs:
                await _token_store.consume_claimed(token)
                should_release_claim = False
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
        part_path = upload_dir / f"{filename}{_UPLOAD_PART_SUFFIX}"
        bytes_written = 0
        try:
            part_path.unlink(missing_ok=True)
            temp_path.unlink(missing_ok=True)
            async with aiofiles.open(part_path, "wb") as handle:
                async for chunk in request.content.iter_chunked(1024 * 1024):
                    if not chunk:
                        continue
                    bytes_written += len(chunk)
                    if bytes_written > max_size_bytes:
                        part_path.unlink(missing_ok=True)
                        return web.Response(status=413, text="Payload too large")
                    await handle.write(chunk)
                await handle.flush()
        except Exception as exc:
            logger.error("Failed to write preview upload: %s", exc)
            part_path.unlink(missing_ok=True)
            return web.Response(status=500, text="Upload failed")

        expected_length = request.content_length
        if expected_length is not None and bytes_written != expected_length:
            logger.warning(
                "Incomplete preview upload for token %s: wrote %s of %s bytes",
                token,
                bytes_written,
                expected_length,
            )
            part_path.unlink(missing_ok=True)
            return web.Response(status=400, text="Incomplete upload")

        try:
            part_path.replace(temp_path)
        except Exception as exc:
            logger.error("Failed to finalize preview upload: %s", exc)
            part_path.unlink(missing_ok=True)
            temp_path.unlink(missing_ok=True)
            return web.Response(status=500, text="Upload finalize failed")

        await _token_store.mark_received(token, temp_path, bytes_written)
        should_release_claim = False
        _start_delivery_task(token)
        return web.Response(status=202, text="Upload received")
    finally:
        if should_release_claim:
            try:
                await _token_store.release_claim(token)
            except Exception as exc:
                logger.warning("Failed to release preview upload token claim: %s", exc)


async def _deliver_received_upload(token: str) -> None:
    state = await _token_store.mark_delivery_started(token)
    if state is None:
        return

    temp_path = _resolve_upload_temp_path(state)
    if temp_path is None:
        await _token_store.mark_delivery_failed(token, "Uploaded preview file is missing")
        return

    try:
        await _deliver_preview(state.payload, temp_path)
    except Exception as exc:
        logger.error("Failed to deliver preview upload %s: %s", token, exc)
        await _token_store.mark_delivery_failed(token, str(exc))
        failed_state = await _token_store.get_state(token)
        if failed_state and failed_state.attempts_exhausted:
            await _notify_delivery_exhausted(failed_state)
        return

    await _token_store.consume_claimed(token)


async def _notify_delivery_exhausted(state: PreviewUploadState) -> None:
    payload = state.payload
    safe_name = html.escape(payload.job_name or "Preview")
    path_hint = payload.expected_render_path or payload.expected_dropbox_path or payload.expected_local_path
    display_path = normalize_preview_path(path_hint) or path_hint or "unknown path"
    message_text = (
        f"⚠️ Preview for {safe_name} could not be delivered after "
        f"{state.delivery_attempts} attempt(s).\n"
        f"{display_path}"
    )
    if state.last_error:
        message_text += f"\n\nLast error: {html.escape(state.last_error)}"
    message_text += "\n\nThe preview job will be removed."

    target_chat_id = payload.telegram_user_id
    if payload.preview_job_id:
        try:
            from app.services.preview.runtime import (
                peek_preview_message,
                pop_preview_message,
                stop_preview_animation,
            )

            stop_preview_animation(payload.preview_job_id)
            stored_message = peek_preview_message(payload.preview_job_id)
            if stored_message:
                chat_id, message_id = stored_message
                target_chat_id = chat_id
                try:
                    await bot.edit_message_text(
                        message_text,
                        chat_id=chat_id,
                        message_id=message_id,
                    )
                except Exception as edit_error:
                    logger.warning(
                        "Failed to edit exhausted upload message for %s: %s",
                        payload.preview_job_id,
                        edit_error,
                    )
                    await bot.send_message(target_chat_id, message_text)
                pop_preview_message(payload.preview_job_id)
            else:
                await bot.send_message(target_chat_id, message_text)
        except Exception as exc:
            logger.warning("Failed to notify exhausted upload %s: %s", state.token, exc)
            await bot.send_message(target_chat_id, message_text)
    else:
        await bot.send_message(target_chat_id, message_text)

    if payload.preview_job_id:
        notified_jobs.add((payload.preview_job_id, payload.telegram_user_id))
        try:
            from app.services.deadline import delete_job_by_user_id

            await delete_job_by_user_id(payload.telegram_user_id, payload.preview_job_id)
        except Exception as exc:
            logger.warning("Failed to delete exhausted preview job %s: %s", payload.preview_job_id, exc)


async def _deliver_preview(payload: PreviewUploadPayload, temp_path: Path) -> None:
    safe_name = html.escape(payload.job_name or "Preview")

    if payload.preview_job_id:
        try:
            from app.services.preview.runtime import stop_preview_animation

            stop_preview_animation(payload.preview_job_id)
        except Exception as exc:
            logger.debug("Preview animation stop failed: %s", exc)

    path_hint = payload.expected_render_path or payload.expected_dropbox_path or payload.expected_local_path
    display_path = normalize_preview_path(path_hint)
    from app.services.preview.delivery import is_preview_image_path

    if is_preview_image_path(temp_path):
        from app.integrations.video_helpers import VideoDeliveryPreparation

        preparation = VideoDeliveryPreparation(
            video_path=temp_path,
            size_mb=temp_path.stat().st_size / (1024 * 1024),
            fallback_message=None,
        )
    else:
        preparation = await prepare_video_for_delivery(
            temp_path,
            dropbox_path=display_path,
        )

    if preparation.fallback_message and display_path is None:
        # Preserve historical "no path hint" simplification of fallback wording.
        preparation.fallback_message = (
            "⚠️ Preview video is ready but too large to send via Telegram."
        )

    from app.core.preview_text import build_preview_caption
    from app.services.deadline import delete_job_by_user_id
    from app.services.preview.delivery import send_ready_preview_video

    display_name = payload.expected_filename or preparation.video_path.name
    caption = build_preview_caption(display_name, display_path)

    async def _delete() -> bool:
        if not payload.preview_job_id:
            return False
        return await delete_job_by_user_id(payload.telegram_user_id, payload.preview_job_id)

    await send_ready_preview_video(
        target_user_id=payload.telegram_user_id,
        preview_job_id=payload.preview_job_id,
        job_name=safe_name,
        preparation=preparation,
        caption=caption,
        delete_job=_delete,
    )

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
