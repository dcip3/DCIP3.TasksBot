"""User notification and preview settings helpers."""

from typing import Optional, Tuple, Literal
import logging

from app.storage.database import get_db_connection

logger = logging.getLogger(__name__)

NotificationScope = Literal["all", "own"]
DEFAULT_NOTIFICATION_SCOPE: NotificationScope = "all"
VALID_NOTIFICATION_SCOPES = {"all", "own"}
PREVIEW_DEFAULT_WORKER_AUTO = "__auto__"

# Probing suspends most of a render's tasks for a while, so unlike the read-only
# scopes above this one also has an "off", and it defaults to the job's own
# submitter. "all" is for an account that may reorder anybody's render - on this
# farm that means someone with the Deadline rights to suspend foreign tasks.
ProbeScope = Literal["off", "own", "all"]
DEFAULT_PROBE_SCOPE: ProbeScope = "own"
VALID_PROBE_SCOPES = {"off", "own", "all"}

__all__ = [
    "NotificationScope",
    "DEFAULT_NOTIFICATION_SCOPE",
    "VALID_NOTIFICATION_SCOPES",
    "ProbeScope",
    "DEFAULT_PROBE_SCOPE",
    "VALID_PROBE_SCOPES",
    "_normalize_probe_scope",
    "get_probe_scope",
    "set_probe_scope",
    "list_probe_release_candidates",
    "PREVIEW_DEFAULT_WORKER_AUTO",
    "PREVIEW_POST_EFFECTS",
    "get_preview_post_effects",
    "set_preview_post_effect",
    "claim_auth_failure_notice",
    "clear_auth_failure_notice",
    "_normalize_scope",
    "get_notification_settings",
    "set_notification_enabled",
    "set_notification_scope",
    "get_preview_default_worker",
    "set_preview_default_worker",
    "get_preview_auto_enabled",
    "get_preview_auto_scope",
    "set_preview_auto_enabled",
    "set_preview_auto_scope",
    "get_notification_status",
    "get_all_users_with_notifications",
    "disable_notifications_for_user",
]


def _normalize_scope(scope: Optional[str]) -> NotificationScope:
    """Normalize notification scope to a known value."""
    if not scope:
        return DEFAULT_NOTIFICATION_SCOPE
    scope_lower = scope.lower()
    return scope_lower if scope_lower in VALID_NOTIFICATION_SCOPES else DEFAULT_NOTIFICATION_SCOPE


def _normalize_probe_scope(scope: Optional[str]) -> ProbeScope:
    """Normalize a probe scope, defaulting to the safe "my jobs only"."""
    if not scope:
        return DEFAULT_PROBE_SCOPE
    scope_lower = scope.lower()
    return scope_lower if scope_lower in VALID_PROBE_SCOPES else DEFAULT_PROBE_SCOPE


async def get_probe_scope(telegram_user_id: int) -> ProbeScope:
    """Which renders this user's credentials may probe for a better ETA."""
    conn = get_db_connection()
    if conn is None:
        return DEFAULT_PROBE_SCOPE

    try:
        async with conn.execute(
            "SELECT probe_scope FROM user_sessions WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return DEFAULT_PROBE_SCOPE
            return _normalize_probe_scope(row[0])
    except Exception as e:
        logger.error("Failed to fetch probe scope for user %s: %s", telegram_user_id, e)
        return DEFAULT_PROBE_SCOPE


async def set_probe_scope(telegram_user_id: int, scope: ProbeScope) -> ProbeScope:
    """Set which renders this user's credentials may probe."""
    conn = get_db_connection()
    if conn is None:
        return DEFAULT_PROBE_SCOPE

    normalized_scope = _normalize_probe_scope(scope)
    try:
        await conn.execute(
            """
            UPDATE user_sessions
            SET probe_scope = ?
            WHERE telegram_user_id = ?
            """,
            (normalized_scope, telegram_user_id),
        )
        await conn.commit()
        logger.info("User %s probe scope set to %s", telegram_user_id, normalized_scope)
        return normalized_scope
    except Exception as e:
        logger.error("Failed to set probe scope for user %s: %s", telegram_user_id, e)
        return DEFAULT_PROBE_SCOPE


async def list_probe_release_candidates(exclude_user_id: Optional[int] = None) -> list[int]:
    """Users whose credentials could release held tasks of someone else's job.

    Held tasks are normally resumed by whoever suspended them. When that account
    is gone - logged out, password changed, rights revoked - a foreign render
    would otherwise sit half-suspended until the backstop gives up on it, so the
    release falls back to these accounts in turn.

    Farm-wide ("all") accounts come first: they are the ones expected to have the
    Deadline rights to touch a job they do not own.
    """
    conn = get_db_connection()
    if conn is None:
        return []

    try:
        async with conn.execute(
            """
            SELECT telegram_user_id, probe_scope
            FROM user_sessions
            WHERE deadline_login IS NOT NULL
              AND deadline_login <> ''
              AND deadline_password IS NOT NULL
              AND deadline_password <> ''
            """
        ) as cursor:
            rows = await cursor.fetchall()
    except Exception as e:
        logger.error("Failed to list probe release candidates: %s", e)
        return []

    candidates = [
        (int(row[0]), _normalize_probe_scope(row[1] if len(row) > 1 else None))
        for row in rows
        if row and row[0] is not None
    ]
    return [
        user_id
        for user_id, scope in sorted(candidates, key=lambda item: item[1] != "all")
        if user_id != exclude_user_id
    ]


async def get_notification_settings(telegram_user_id: int) -> Tuple[bool, NotificationScope]:
    """
    Fetch notification settings (enabled flag and scope) for a user.
    """
    conn = get_db_connection()
    if conn is None:
        return False, DEFAULT_NOTIFICATION_SCOPE

    try:
        async with conn.execute(
            "SELECT notifications_enabled, notification_scope FROM user_sessions WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False, DEFAULT_NOTIFICATION_SCOPE
            enabled = bool(row[0])
            scope = _normalize_scope(row[1] if len(row) > 1 else None)
            return enabled, scope
    except Exception as e:
        logger.error("Failed to fetch notification settings for user %s: %s", telegram_user_id, e)
        return False, DEFAULT_NOTIFICATION_SCOPE


async def set_notification_enabled(telegram_user_id: int, enabled: bool) -> Tuple[bool, NotificationScope]:
    """
    Update the notification enabled flag for a user.

    Returns:
        Tuple of (enabled, scope) after the update.
    """
    conn = get_db_connection()
    if conn is None:
        return False, DEFAULT_NOTIFICATION_SCOPE

    try:
        await conn.execute(
            """
            UPDATE user_sessions
            SET notifications_enabled = ?
            WHERE telegram_user_id = ?
            """,
            (1 if enabled else 0, telegram_user_id),
        )
        await conn.commit()
        logger.info("User %s notification enabled flag set to %s", telegram_user_id, enabled)
    except Exception as e:
        logger.error("Failed to set notification flag for user %s: %s", telegram_user_id, e)
        return False, DEFAULT_NOTIFICATION_SCOPE

    return await get_notification_settings(telegram_user_id)


async def set_notification_scope(telegram_user_id: int, scope: NotificationScope) -> Tuple[bool, NotificationScope]:
    """
    Update the notification scope for a user.

    Returns:
        Tuple of (enabled, scope) after the update.
    """
    conn = get_db_connection()
    if conn is None:
        return False, DEFAULT_NOTIFICATION_SCOPE

    normalized_scope = _normalize_scope(scope)

    try:
        await conn.execute(
            """
            UPDATE user_sessions
            SET notification_scope = ?
            WHERE telegram_user_id = ?
            """,
            (normalized_scope, telegram_user_id),
        )
        await conn.commit()
        logger.info("User %s notification scope set to %s", telegram_user_id, normalized_scope)
    except Exception as e:
        logger.error("Failed to set notification scope for user %s: %s", telegram_user_id, e)
        return False, DEFAULT_NOTIFICATION_SCOPE

    return await get_notification_settings(telegram_user_id)


async def get_preview_default_worker(telegram_user_id: int) -> Optional[str]:
    """
    Get the default worker for preview jobs.

    Args:
        telegram_user_id: Telegram user ID

    Returns:
        Worker name or None if not set
    """
    conn = get_db_connection()
    if conn is None:
        return None

    try:
        async with conn.execute(
            "SELECT preview_default_worker FROM user_sessions WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row or not row[0]:
                return None
            raw = str(row[0]).strip()
            if not raw:
                return None
            if raw.lower() in {"auto", PREVIEW_DEFAULT_WORKER_AUTO.lower()}:
                return PREVIEW_DEFAULT_WORKER_AUTO
            return raw
    except Exception as e:
        logger.error("Failed to fetch preview default worker for user %s: %s", telegram_user_id, e)
        return None


async def set_preview_default_worker(telegram_user_id: int, worker_name: Optional[str]) -> bool:
    """
    Set the default worker for preview jobs.

    Args:
        telegram_user_id: Telegram user ID
        worker_name: Worker name or None to clear

    Returns:
        True if successful, False otherwise
    """
    conn = get_db_connection()
    if conn is None:
        return False

    normalized: Optional[str]
    if worker_name is None:
        normalized = None
    else:
        candidate = str(worker_name).strip()
        if not candidate or candidate.lower() in {"none", "ask"}:
            normalized = None
        elif candidate.lower() in {"auto", PREVIEW_DEFAULT_WORKER_AUTO.lower()}:
            normalized = PREVIEW_DEFAULT_WORKER_AUTO
        else:
            normalized = candidate

    try:
        await conn.execute(
            """
            UPDATE user_sessions
            SET preview_default_worker = ?
            WHERE telegram_user_id = ?
            """,
            (normalized, telegram_user_id),
        )
        await conn.commit()
        logger.info(
            "User %s preview default worker set to %s",
            telegram_user_id,
            normalized or "None",
        )
        return True
    except Exception as e:
        logger.error("Failed to set preview default worker for user %s: %s", telegram_user_id, e)
        return False




async def get_preview_auto_enabled(telegram_user_id: int) -> bool:
    """
    Get the auto-preview enabled flag for a user.
    """
    conn = get_db_connection()
    if conn is None:
        return False

    try:
        async with conn.execute(
            "SELECT preview_auto_enabled FROM user_sessions WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
            return bool(row[0])
    except Exception as e:
        logger.error("Failed to fetch preview auto flag for user %s: %s", telegram_user_id, e)
        return False


async def get_preview_auto_scope(telegram_user_id: int) -> NotificationScope:
    """
    Get the auto-preview scope for a user.

    Falls back to notification scope when not explicitly set.
    """
    conn = get_db_connection()
    if conn is None:
        return DEFAULT_NOTIFICATION_SCOPE

    try:
        async with conn.execute(
            "SELECT preview_auto_scope FROM user_sessions WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                return _normalize_scope(row[0])
    except Exception as e:
        logger.error("Failed to fetch preview auto scope for user %s: %s", telegram_user_id, e)

    _, scope = await get_notification_settings(telegram_user_id)
    return scope


async def set_preview_auto_enabled(telegram_user_id: int, enabled: bool) -> bool:
    """
    Enable or disable auto-preview for a user.
    """
    conn = get_db_connection()
    if conn is None:
        return False

    try:
        await conn.execute(
            """
            UPDATE user_sessions
            SET preview_auto_enabled = ?
            WHERE telegram_user_id = ?
            """,
            (1 if enabled else 0, telegram_user_id),
        )
        await conn.commit()
        logger.info("User %s preview auto flag set to %s", telegram_user_id, enabled)
        return True
    except Exception as e:
        logger.error("Failed to set preview auto flag for user %s: %s", telegram_user_id, e)
        return False


async def claim_auth_failure_notice(telegram_user_id: int) -> bool:
    """Return True the first time the user should be warned about rejected credentials.

    The timestamp is persisted, so the background warning is sent exactly once
    (surviving bot restarts) until a successful /login clears it. Interactive
    requests report the problem separately, every time they hit it.
    """
    import time

    conn = get_db_connection()
    if conn is None:
        return True

    try:
        async with conn.execute(
            "SELECT auth_failure_notified_at FROM user_sessions WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0]:
            return False

        await conn.execute(
            "UPDATE user_sessions SET auth_failure_notified_at = ? WHERE telegram_user_id = ?",
            (int(time.time()), telegram_user_id),
        )
        await conn.commit()
        return True
    except Exception as e:
        logger.error(
            "Failed to record auth failure notice for user %s: %s", telegram_user_id, e
        )
        # Do not spam when the bookkeeping itself fails.
        return False


async def clear_auth_failure_notice(telegram_user_id: int) -> None:
    """Forget the credential warning after a successful login."""
    conn = get_db_connection()
    if conn is None:
        return
    try:
        await conn.execute(
            "UPDATE user_sessions SET auth_failure_notified_at = NULL WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        await conn.commit()
    except Exception as e:
        logger.error(
            "Failed to clear auth failure notice for user %s: %s", telegram_user_id, e
        )


PREVIEW_POST_EFFECTS = {
    "color_transform": "preview_apply_color_transform",
    "lut": "preview_apply_lut",
    "color_controls": "preview_apply_color_controls",
}


async def get_preview_post_effects(telegram_user_id: int) -> dict:
    """Return which preview post effects are enabled for a user.

    Everything defaults to enabled, so a missing row or a failed read still
    produces the full-quality preview.
    """
    defaults = {key: True for key in PREVIEW_POST_EFFECTS}
    conn = get_db_connection()
    if conn is None:
        return defaults

    columns = ", ".join(PREVIEW_POST_EFFECTS.values())
    try:
        async with conn.execute(
            f"SELECT {columns} FROM user_sessions WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return defaults
            return {
                key: True if row[index] is None else bool(row[index])
                for index, key in enumerate(PREVIEW_POST_EFFECTS)
            }
    except Exception as e:
        logger.error("Failed to fetch preview post effects for user %s: %s", telegram_user_id, e)
        return defaults


async def set_preview_post_effect(telegram_user_id: int, key: str, enabled: bool) -> bool:
    """Enable or disable a single preview post effect."""
    column = PREVIEW_POST_EFFECTS.get(key)
    if column is None:
        logger.error("Unknown preview post effect '%s'", key)
        return False

    conn = get_db_connection()
    if conn is None:
        return False

    try:
        await conn.execute(
            f"UPDATE user_sessions SET {column} = ? WHERE telegram_user_id = ?",
            (1 if enabled else 0, telegram_user_id),
        )
        await conn.commit()
        logger.info("User %s preview post effect %s set to %s", telegram_user_id, key, enabled)
        return True
    except Exception as e:
        logger.error(
            "Failed to set preview post effect %s for user %s: %s", key, telegram_user_id, e
        )
        return False


async def set_preview_auto_scope(
    telegram_user_id: int,
    scope: NotificationScope,
) -> NotificationScope:
    """
    Set the auto-preview scope for a user.
    """
    conn = get_db_connection()
    if conn is None:
        return DEFAULT_NOTIFICATION_SCOPE

    normalized_scope = _normalize_scope(scope)
    try:
        await conn.execute(
            """
            UPDATE user_sessions
            SET preview_auto_scope = ?
            WHERE telegram_user_id = ?
            """,
            (normalized_scope, telegram_user_id),
        )
        await conn.commit()
        logger.info(
            "User %s preview auto scope set to %s",
            telegram_user_id,
            normalized_scope,
        )
        return normalized_scope
    except Exception as e:
        logger.error("Failed to set preview auto scope for user %s: %s", telegram_user_id, e)
        return DEFAULT_NOTIFICATION_SCOPE


async def get_notification_status(telegram_user_id: int) -> bool:
    """
    Get notification status for a user.

    Args:
        telegram_user_id: Telegram user ID

    Returns:
        True if notifications are enabled, False otherwise
    """
    conn = get_db_connection()
    if conn is None:
        return False

    try:
        enabled, _ = await get_notification_settings(telegram_user_id)
        return enabled
    except Exception as e:
        logger.error("Failed to get notification status for user %s: %s", telegram_user_id, e)
        return False


async def get_all_users_with_notifications() -> list[Tuple[int, str, str, NotificationScope]]:
    """
    Get all users with enabled notifications and their Deadline credentials.

    Returns:
        List of tuples containing (telegram_user_id, deadline_login, deadline_password, notification_scope)
    """
    conn = get_db_connection()
    if conn is None:
        logger.error("Database connection not available for get_all_users_with_notifications")
        return []

    try:
        async with conn.execute(
            """
            SELECT telegram_user_id, deadline_login, deadline_password, notification_scope
            FROM user_sessions
            WHERE notifications_enabled = 1
            """
        ) as cursor:
            rows = await cursor.fetchall()
            result = [
                (
                    row[0],
                    row[1],
                    row[2],
                    _normalize_scope(row[3] if len(row) > 3 else None),
                )
                for row in rows
            ]
            logger.info(
                "Found %s users with notifications enabled: %s",
                len(result),
                [user_id for user_id, _, _, _ in result],
            )
            return result
    except Exception as e:
        logger.error("Failed to get users with notifications: %s", e)
        return []


async def disable_notifications_for_user(telegram_user_id: int) -> None:
    """Disable notifications for a given user."""
    conn = get_db_connection()
    if conn is None:
        logger.error("Database connection not available for disable_notifications_for_user")
        return

    try:
        await conn.execute(
            "UPDATE user_sessions SET notifications_enabled = 0 WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        await conn.commit()
        logger.info("Notifications disabled for user %s", telegram_user_id)
    except Exception as e:
        logger.error("Failed to disable notifications for user %s: %s", telegram_user_id, e)
