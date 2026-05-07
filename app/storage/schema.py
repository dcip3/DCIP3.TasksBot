"""Shared SQLite schema helpers."""

from __future__ import annotations

import json
import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def ensure_column(
    conn: aiosqlite.Connection,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> bool:
    try:
        await conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
        return True
    except aiosqlite.OperationalError as column_error:
        message = str(column_error).lower()
        if "duplicate column name" in message:
            logger.debug("%s column already exists on %s table", column_name, table_name)
            return False
        logger.error("Failed to ensure %s column on %s: %s", column_name, table_name, column_error)
        raise


async def _backfill_preview_upload_columns(conn: aiosqlite.Connection) -> None:
    try:
        async with conn.execute(
            """
            SELECT token, payload_json, preview_job_id, source_job_id
            FROM preview_upload_tokens
            WHERE (preview_job_id IS NULL OR preview_job_id = '')
               OR (source_job_id IS NULL OR source_job_id = '')
            """
        ) as cursor:
            rows = await cursor.fetchall()
    except Exception as exc:
        logger.warning("Failed to read preview upload tokens for backfill: %s", exc)
        return

    updated = 0
    for token, payload_json, preview_job_id, source_job_id in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        next_preview_id = preview_job_id or payload.get("preview_job_id")
        next_source_id = source_job_id or payload.get("source_job_id")
        if not next_preview_id and not next_source_id:
            continue
        await conn.execute(
            """
            UPDATE preview_upload_tokens
            SET preview_job_id = COALESCE(NULLIF(preview_job_id, ''), ?),
                source_job_id = COALESCE(NULLIF(source_job_id, ''), ?)
            WHERE token = ?
            """,
            (next_preview_id, next_source_id, token),
        )
        updated += 1
    if updated:
        logger.info("Backfilled preview upload metadata for %s token(s)", updated)


async def ensure_preview_upload_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS preview_upload_tokens (
            token TEXT PRIMARY KEY,
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            claimed_until INTEGER NOT NULL DEFAULT 0,
            preview_job_id TEXT,
            source_job_id TEXT,
            status TEXT NOT NULL DEFAULT 'issued',
            temp_path TEXT,
            bytes_written INTEGER NOT NULL DEFAULT 0,
            received_at INTEGER NOT NULL DEFAULT 0,
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_at INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            payload_json TEXT NOT NULL
        )
        """
    )
    for column_name, column_sql in (
        ("claimed_until", "INTEGER NOT NULL DEFAULT 0"),
        ("preview_job_id", "TEXT"),
        ("source_job_id", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'issued'"),
        ("temp_path", "TEXT"),
        ("bytes_written", "INTEGER NOT NULL DEFAULT 0"),
        ("received_at", "INTEGER NOT NULL DEFAULT 0"),
        ("delivery_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("next_retry_at", "INTEGER NOT NULL DEFAULT 0"),
        ("last_error", "TEXT"),
    ):
        await ensure_column(conn, "preview_upload_tokens", column_name, column_sql)

    await _backfill_preview_upload_columns(conn)
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_preview_upload_tokens_expires
        ON preview_upload_tokens(expires_at)
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_preview_upload_tokens_preview_job
        ON preview_upload_tokens(preview_job_id)
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_preview_upload_tokens_retry
        ON preview_upload_tokens(status, next_retry_at)
        """
    )
