"""User notification and preview settings helpers."""

from typing import Optional, Tuple, Literal
import logging

from app.core.database import get_db_connection

logger = logging.getLogger(__name__)

NotificationScope = Literal["all", "own"]
DEFAULT_NOTIFICATION_SCOPE: NotificationScope = "all"
VALID_NOTIFICATION_SCOPES = {"all", "own"}
VALID_PREVIEW_RENDER_METHODS = {"server", "deadline"}
PREVIEW_DEFAULT_WORKER_AUTO = "__auto__"


def _normalize_scope(scope: Optional[str]) -> NotificationScope:
    """Normalize notification scope to a known value."""
    if not scope:
        return DEFAULT_NOTIFICATION_SCOPE
    scope_lower = scope.lower()
    return scope_lower if scope_lower in VALID_NOTIFICATION_SCOPES else DEFAULT_NOTIFICATION_SCOPE


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


async def get_preview_default_method(telegram_user_id: int) -> Optional[str]:
    """
    Get the default render method for previews.

    Returns:
        'server', 'deadline', or None if not set.
    """
    conn = get_db_connection()
    if conn is None:
        return None

    try:
        async with conn.execute(
            "SELECT preview_default_method FROM user_sessions WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row or not row[0]:
                return None
            method = (row[0] or "").strip().lower()
            if method not in VALID_PREVIEW_RENDER_METHODS:
                return None
            return method
    except Exception as e:
        logger.error(
            "Failed to fetch preview default method for user %s: %s",
            telegram_user_id,
            e,
        )
        return None


async def set_preview_default_method(telegram_user_id: int, method: Optional[str]) -> bool:
    """
    Set the default render method for previews.

    Args:
        telegram_user_id: Telegram user ID
        method: 'server', 'deadline', or None/'ask' to clear
    """
    conn = get_db_connection()
    if conn is None:
        return False

    normalized: Optional[str]
    if method is None:
        normalized = None
    else:
        candidate = method.strip().lower()
        if candidate in {"none", "ask", ""}:
            normalized = None
        elif candidate in VALID_PREVIEW_RENDER_METHODS:
            normalized = candidate
        else:
            logger.error(
                "Unsupported preview default method '%s' for user %s",
                method,
                telegram_user_id,
            )
            return False

    try:
        await conn.execute(
            """
            UPDATE user_sessions
            SET preview_default_method = ?
            WHERE telegram_user_id = ?
            """,
            (normalized, telegram_user_id),
        )
        await conn.commit()
        logger.info(
            "User %s preview default method set to %s",
            telegram_user_id,
            normalized or "ask",
        )
        return True
    except Exception as e:
        logger.error(
            "Failed to set preview default method for user %s: %s",
            telegram_user_id,
            e,
        )
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
