"""
Database operations and connection management.

This module handles SQLite database initialization, connection management,
and provides basic database operations for the application.
"""

import logging
import time
from pathlib import Path
import aiosqlite
from app.core.config import settings
from app.storage.schema import ensure_column, ensure_preview_upload_schema

logger = logging.getLogger(__name__)

# Global database connection
tasks_db_conn: aiosqlite.Connection | None = None


async def _ensure_user_session_column(column_name: str, column_sql: str) -> None:
    if tasks_db_conn is None:
        raise RuntimeError("Database connection is not initialized")
    added = await ensure_column(
        tasks_db_conn,
        "user_sessions",
        column_name,
        column_sql,
    )
    await tasks_db_conn.commit()
    if added:
        logger.info("Added %s column to user_sessions table", column_name)


async def init_db():
    """
    Initialize the SQLite database and create all necessary tables.
    
    Creates the following tables:
    - user_sessions: Stores Deadline credentials and user settings
    """
    global tasks_db_conn
    db_path = Path(settings.sqlite_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if db_path.exists() and db_path.is_dir():
        error_message = (
            f"Configured SQLITE_DB_PATH '{db_path}' points to a directory. "
            "If you're running via Docker, ensure the bind mount targets a file "
            "or mount the entire data directory (e.g. ./data:/app/data)."
        )
        logger.error(error_message)
        raise RuntimeError(error_message)

    if not db_path.exists():
        db_path.touch()
        logger.info("Created new SQLite database file at %s", db_path)

    tasks_db_conn = await aiosqlite.connect(settings.sqlite_db_path)
    
    # Create user_sessions table for storing session data
    await tasks_db_conn.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id INTEGER UNIQUE,
            deadline_login TEXT,
            deadline_password TEXT,
            notifications_enabled INTEGER DEFAULT 0,
            notification_scope TEXT DEFAULT 'all',
            preview_default_worker TEXT,
            preview_default_method TEXT,
            preview_auto_enabled INTEGER DEFAULT 0,
            preview_auto_scope TEXT,
            last_login TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Store preview upload tokens to survive bot restarts.
    await ensure_preview_upload_schema(tasks_db_conn)

    # Persist auto-preview dedupe history across restarts
    await tasks_db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_preview_history (
            telegram_user_id INTEGER NOT NULL,
            job_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (telegram_user_id, job_id)
        )
        """
    )
    await tasks_db_conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_preview_history_created_at
        ON auto_preview_history(created_at)
        """
    )

    # Create indexes for better performance
    await tasks_db_conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_sessions_telegram_id 
        ON user_sessions(telegram_user_id)
    """)

    await _ensure_user_session_column("notification_scope", "TEXT DEFAULT 'all'")
    await _ensure_user_session_column("preview_default_worker", "TEXT")
    await _ensure_user_session_column("preview_default_method", "TEXT")
    await _ensure_user_session_column("preview_auto_enabled", "INTEGER DEFAULT 0")
    await _ensure_user_session_column("preview_auto_scope", "TEXT")
    # When the user was last told their stored credentials are rejected, so the
    # warning is not repeated on every bot restart.
    await _ensure_user_session_column("auth_failure_notified_at", "INTEGER")
    # Per-user preview post effects; all enabled by default.
    await _ensure_user_session_column("preview_apply_color_transform", "INTEGER DEFAULT 1")
    await _ensure_user_session_column("preview_apply_lut", "INTEGER DEFAULT 1")
    await _ensure_user_session_column("preview_apply_color_controls", "INTEGER DEFAULT 1")

    try:
        await tasks_db_conn.execute(
            """
            UPDATE user_sessions
            SET preview_auto_scope = notification_scope
            WHERE preview_auto_scope IS NULL OR preview_auto_scope = ''
            """
        )
        await tasks_db_conn.commit()
    except Exception as update_error:
        logger.warning(
            "Failed to backfill preview_auto_scope: %s",
            update_error,
        )

    try:
        cutoff = int(time.time()) - (14 * 24 * 60 * 60)
        await tasks_db_conn.execute(
            "DELETE FROM auto_preview_history WHERE created_at < ?",
            (cutoff,),
        )
        await tasks_db_conn.commit()
    except Exception as cleanup_error:
        logger.warning("Failed to cleanup stale auto_preview_history rows: %s", cleanup_error)

    await tasks_db_conn.commit()
    logger.info("Database initialized successfully")


async def close_db():
    """
    Close the SQLite database connection.
    
    Safely closes the database connection and resets the global connection
    variable to None.
    """
    global tasks_db_conn
    if tasks_db_conn:
        await tasks_db_conn.close()
        tasks_db_conn = None
        logger.info("Database connection closed")


def get_db_connection() -> aiosqlite.Connection | None:
    """
    Get the current database connection.
    
    Returns:
        The current aiosqlite connection or None if not initialized
    """
    return tasks_db_conn
