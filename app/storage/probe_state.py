"""Persistence for probe scheduling.

While a render is being probed, most of its tasks are suspended. This table is
the only record of which tasks the bot suspended, so it has to outlive the
process - a bot restart mid-probe must not leave a render stalled.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.storage.database import get_db_connection

logger = logging.getLogger(__name__)

__all__ = [
    "ProbeState",
    "save_probe_state",
    "get_probe_state",
    "list_unreleased_probes",
    "mark_probe_released",
    "delete_probe_state",
    "cleanup_probe_state",
]


@dataclass(frozen=True)
class ProbeState:
    job_id: str
    telegram_user_id: int
    probe_task_ids: list[int]
    held_task_ids: list[int]
    started_at: int
    released_at: int | None

    @property
    def age_seconds(self) -> int:
        return max(int(time.time()) - self.started_at, 0)


def _require_connection():
    """The shared aiosqlite connection.

    `get_db_connection()` is a plain function returning an already-connected
    handle - it must NOT be awaited. Awaiting an aiosqlite Connection runs its
    startup again, which raises "threads can only be started once" on a live
    one. That is how every probe-state call failed in production while the
    surrounding try/except turned it into a log line nobody read.
    """
    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("Database is not initialised")
    return conn


def _encode(task_ids: list[int]) -> str:
    return ",".join(str(int(task_id)) for task_id in task_ids)


def _decode(value: object) -> list[int]:
    out: list[int] = []
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


def _row_to_state(row) -> ProbeState:
    return ProbeState(
        job_id=str(row[0]),
        telegram_user_id=int(row[1]),
        probe_task_ids=_decode(row[2]),
        held_task_ids=_decode(row[3]),
        started_at=int(row[4]),
        released_at=int(row[5]) if row[5] is not None else None,
    )


async def save_probe_state(
    job_id: str,
    telegram_user_id: int,
    probe_task_ids: list[int],
    held_task_ids: list[int],
) -> None:
    conn = _require_connection()
    await conn.execute(
        """
        INSERT INTO render_probe_state
            (job_id, telegram_user_id, probe_task_ids, held_task_ids, started_at, released_at)
        VALUES (?, ?, ?, ?, ?, NULL)
        ON CONFLICT(job_id) DO UPDATE SET
            telegram_user_id = excluded.telegram_user_id,
            probe_task_ids = excluded.probe_task_ids,
            held_task_ids = excluded.held_task_ids
        """,
        (
            job_id,
            telegram_user_id,
            _encode(probe_task_ids),
            _encode(held_task_ids),
            int(time.time()),
        ),
    )
    await conn.commit()


async def get_probe_state(job_id: str) -> ProbeState | None:
    conn = _require_connection()
    async with conn.execute(
        """
        SELECT job_id, telegram_user_id, probe_task_ids, held_task_ids, started_at, released_at
        FROM render_probe_state WHERE job_id = ?
        """,
        (job_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return _row_to_state(row) if row else None


async def list_unreleased_probes() -> list[ProbeState]:
    """Every job that still has tasks the bot is holding back."""
    conn = _require_connection()
    async with conn.execute(
        """
        SELECT job_id, telegram_user_id, probe_task_ids, held_task_ids, started_at, released_at
        FROM render_probe_state WHERE released_at IS NULL
        """
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_state(row) for row in rows]


async def mark_probe_released(job_id: str) -> None:
    conn = _require_connection()
    await conn.execute(
        "UPDATE render_probe_state SET released_at = ?, held_task_ids = '' WHERE job_id = ?",
        (int(time.time()), job_id),
    )
    await conn.commit()


async def delete_probe_state(job_id: str) -> None:
    conn = _require_connection()
    await conn.execute("DELETE FROM render_probe_state WHERE job_id = ?", (job_id,))
    await conn.commit()


async def cleanup_probe_state(max_age_seconds: int = 7 * 24 * 60 * 60) -> None:
    conn = _require_connection()
    await conn.execute(
        "DELETE FROM render_probe_state WHERE released_at IS NOT NULL AND released_at < ?",
        (int(time.time()) - max_age_seconds,),
    )
    await conn.commit()
