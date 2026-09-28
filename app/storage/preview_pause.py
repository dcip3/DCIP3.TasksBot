"""When the bot first saw a waiting preview's render paused.

A pre-submitted preview waits Pending on its render. A render left paused
releases its preview after a grace period (see job_watcher). The clock used
to live in memory, so every restart or deploy started it over - with deploys
under an hour apart a paused render's preview never went. Kept per preview,
so two accounts' previews of one render each keep their own clock.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.storage.database import get_db_connection

__all__ = [
    "PreviewPause",
    "record_pause",
    "list_pauses",
    "drop_pause",
]


@dataclass(frozen=True)
class PreviewPause:
    preview_job_id: str
    source_job_id: str
    telegram_user_id: int
    paused_at: int


def _require_connection():
    # A plain function returning the live connection - never await it (see
    # probe_state._require_connection).
    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("Database is not initialised")
    return conn


async def record_pause(preview_job_id: str, source_job_id: str, telegram_user_id: int) -> None:
    """Note the pause now; a pause already noted keeps its original time."""
    conn = _require_connection()
    await conn.execute(
        """
        INSERT INTO preview_pauses (preview_job_id, source_job_id, telegram_user_id, paused_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(preview_job_id) DO NOTHING
        """,
        (preview_job_id, source_job_id, telegram_user_id, int(time.time())),
    )
    await conn.commit()


async def list_pauses(telegram_user_id: int) -> dict[str, PreviewPause]:
    """This account's previews whose render is known to be paused, by preview job id."""
    conn = _require_connection()
    async with conn.execute(
        """
        SELECT preview_job_id, source_job_id, telegram_user_id, paused_at
        FROM preview_pauses WHERE telegram_user_id = ?
        """,
        (telegram_user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return {
        str(row[0]): PreviewPause(
            preview_job_id=str(row[0]),
            source_job_id=str(row[1]),
            telegram_user_id=int(row[2]),
            paused_at=int(row[3]),
        )
        for row in rows
    }


async def drop_pause(preview_job_id: str) -> None:
    conn = _require_connection()
    await conn.execute("DELETE FROM preview_pauses WHERE preview_job_id = ?", (preview_job_id,))
    await conn.commit()
