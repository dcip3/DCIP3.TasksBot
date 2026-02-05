"""
Database operations and connection management.

This module handles SQLite database initialization, connection management,
and provides basic database operations for the application.
"""

import logging
from pathlib import Path
import aiosqlite
from app.core.config import settings

logger = logging.getLogger(__name__)

# Global database connection
tasks_db_conn: aiosqlite.Connection | None = None


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

    # Store preview upload tokens to survive bot restarts
    await tasks_db_conn.execute("""
        CREATE TABLE IF NOT EXISTS preview_upload_tokens (
            token TEXT PRIMARY KEY,
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        )
    """)
    await tasks_db_conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_preview_upload_tokens_expires
        ON preview_upload_tokens(expires_at)
    """)
    
    # Create indexes for better performance
    await tasks_db_conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_sessions_telegram_id 
        ON user_sessions(telegram_user_id)
    """)

    # Ensure notification_scope column exists for legacy databases
    try:
        await tasks_db_conn.execute(
            "ALTER TABLE user_sessions ADD COLUMN notification_scope TEXT DEFAULT 'all'"
        )
        await tasks_db_conn.commit()
        logger.info("Added notification_scope column to user_sessions table")
    except aiosqlite.OperationalError as column_error:
        message = str(column_error).lower()
        if "duplicate column name" in message:
            logger.debug("notification_scope column already exists on user_sessions table")
        else:
            logger.error("Failed to ensure notification_scope column: %s", column_error)
            raise

    # Ensure preview_default_worker column exists for legacy databases
    try:
        await tasks_db_conn.execute(
            "ALTER TABLE user_sessions ADD COLUMN preview_default_worker TEXT"
        )
        await tasks_db_conn.commit()
        logger.info("Added preview_default_worker column to user_sessions table")
    except aiosqlite.OperationalError as column_error:
        message = str(column_error).lower()
        if "duplicate column name" in message:
            logger.debug("preview_default_worker column already exists on user_sessions table")
        else:
            logger.error("Failed to ensure preview_default_worker column: %s", column_error)
            raise
    # Ensure preview_default_method column exists for legacy databases
    try:
        await tasks_db_conn.execute(
            "ALTER TABLE user_sessions ADD COLUMN preview_default_method TEXT"
        )
        await tasks_db_conn.commit()
        logger.info("Added preview_default_method column to user_sessions table")
    except aiosqlite.OperationalError as column_error:
        message = str(column_error).lower()
        if "duplicate column name" in message:
            logger.debug("preview_default_method column already exists on user_sessions table")
        else:
            logger.error("Failed to ensure preview_default_method column: %s", column_error)
            raise

    # Ensure preview_auto_enabled column exists for legacy databases
    try:
        await tasks_db_conn.execute(
            "ALTER TABLE user_sessions ADD COLUMN preview_auto_enabled INTEGER DEFAULT 0"
        )
        await tasks_db_conn.commit()
        logger.info("Added preview_auto_enabled column to user_sessions table")
    except aiosqlite.OperationalError as column_error:
        message = str(column_error).lower()
        if "duplicate column name" in message:
            logger.debug("preview_auto_enabled column already exists on user_sessions table")
        else:
            logger.error("Failed to ensure preview_auto_enabled column: %s", column_error)
            raise

    # Ensure preview_auto_scope column exists for legacy databases
    try:
        await tasks_db_conn.execute(
            "ALTER TABLE user_sessions ADD COLUMN preview_auto_scope TEXT"
        )
        await tasks_db_conn.commit()
        logger.info("Added preview_auto_scope column to user_sessions table")
    except aiosqlite.OperationalError as column_error:
        message = str(column_error).lower()
        if "duplicate column name" in message:
            logger.debug("preview_auto_scope column already exists on user_sessions table")
        else:
            logger.error("Failed to ensure preview_auto_scope column: %s", column_error)
            raise

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
