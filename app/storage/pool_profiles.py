"""Persistence helpers for pool mode/preferences used by Pools UI."""

from __future__ import annotations

import json
import logging
import time
from typing import Literal, Optional, TypedDict

from app.storage.database import get_db_connection

logger = logging.getLogger(__name__)

PoolMode = Literal["disk", "manual"]


class PoolProfile(TypedDict):
    pool_name: str
    mode: PoolMode
    disk_letter: Optional[str]
    manual_workers: list[str]


def _normalize_mode(mode: Optional[str]) -> PoolMode:
    return "disk" if str(mode or "").strip().lower() == "disk" else "manual"


def _normalize_disk_letter(letter: Optional[str]) -> Optional[str]:
    if not letter:
        return None
    candidate = str(letter).strip().upper()
    if len(candidate) == 1 and candidate.isalpha():
        return candidate
    return None


def _normalize_workers(workers: Optional[list[str]]) -> list[str]:
    if not workers:
        return []
    seen: set[str] = set()
    normalized: list[str] = []
    for item in workers:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append(name)
    return sorted(normalized, key=str.lower)


async def get_pool_profile(pool_name: str) -> Optional[PoolProfile]:
    conn = get_db_connection()
    if conn is None:
        return None

    normalized_pool = str(pool_name or "").strip()
    if not normalized_pool:
        return None

    try:
        async with conn.execute(
            """
            SELECT pool_name, mode, disk_letter, manual_workers_json
            FROM pool_profiles
            WHERE lower(pool_name) = lower(?)
            LIMIT 1
            """,
            (normalized_pool,),
        ) as cursor:
            row = await cursor.fetchone()
    except Exception as exc:
        logger.error("Failed to get pool profile for %s: %s", normalized_pool, exc)
        return None

    if not row:
        return None

    raw_workers = row[3] or "[]"
    workers: list[str]
    try:
        parsed = json.loads(raw_workers)
        workers = _normalize_workers(parsed if isinstance(parsed, list) else [])
    except Exception:
        workers = []

    return {
        "pool_name": str(row[0]),
        "mode": _normalize_mode(row[1]),
        "disk_letter": _normalize_disk_letter(row[2]),
        "manual_workers": workers,
    }


async def set_pool_profile(
    pool_name: str,
    *,
    mode: PoolMode,
    disk_letter: Optional[str],
    manual_workers: Optional[list[str]],
) -> bool:
    conn = get_db_connection()
    if conn is None:
        return False

    normalized_pool = str(pool_name or "").strip()
    if not normalized_pool:
        return False

    normalized_mode = _normalize_mode(mode)
    normalized_disk = _normalize_disk_letter(disk_letter)
    normalized_workers = _normalize_workers(manual_workers)
    payload_workers = json.dumps(normalized_workers, ensure_ascii=True)

    try:
        async with conn.execute(
            "SELECT pool_name FROM pool_profiles WHERE lower(pool_name) = lower(?) LIMIT 1",
            (normalized_pool,),
        ) as cursor:
            existing_row = await cursor.fetchone()
        storage_pool_name = str(existing_row[0]) if existing_row else normalized_pool

        await conn.execute(
            """
            INSERT INTO pool_profiles (pool_name, mode, disk_letter, manual_workers_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(pool_name) DO UPDATE SET
                mode = excluded.mode,
                disk_letter = excluded.disk_letter,
                manual_workers_json = excluded.manual_workers_json,
                updated_at = excluded.updated_at
            """,
            (
                storage_pool_name,
                normalized_mode,
                normalized_disk,
                payload_workers,
                int(time.time()),
            ),
        )
        await conn.commit()
        return True
    except Exception as exc:
        logger.error("Failed to set pool profile for %s: %s", normalized_pool, exc)
        return False


async def rename_pool_profile(old_name: str, new_name: str) -> bool:
    conn = get_db_connection()
    if conn is None:
        return False

    old_pool = str(old_name or "").strip()
    new_pool = str(new_name or "").strip()
    if not old_pool or not new_pool:
        return False

    try:
        await conn.execute(
            "UPDATE pool_profiles SET pool_name = ?, updated_at = ? WHERE lower(pool_name) = lower(?)",
            (new_pool, int(time.time()), old_pool),
        )
        await conn.commit()
        return True
    except Exception as exc:
        logger.error("Failed to rename pool profile %s -> %s: %s", old_pool, new_pool, exc)
        return False


async def delete_pool_profile(pool_name: str) -> bool:
    conn = get_db_connection()
    if conn is None:
        return False

    normalized_pool = str(pool_name or "").strip()
    if not normalized_pool:
        return False

    try:
        await conn.execute(
            "DELETE FROM pool_profiles WHERE lower(pool_name) = lower(?)",
            (normalized_pool,),
        )
        await conn.commit()
        return True
    except Exception as exc:
        logger.error("Failed to delete pool profile for %s: %s", normalized_pool, exc)
        return False
