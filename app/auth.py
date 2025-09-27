# app/auth.py
"""
User authentication and management system.

This module provides user authentication, password hashing, and user management
functions including login, logout, and session management for TasksBot.
"""

import hashlib
import logging
from typing import Optional, Tuple
from app.core.config import settings
from app.core.database import get_db_connection
import aiohttp

logger = logging.getLogger(__name__)


def _hash_password(password: str) -> str:
    """
    Hash password using PBKDF2-HMAC-SHA256 with salt from settings.
    
    Args:
        password: Plain text password to hash
        
    Returns:
        Hexadecimal representation of the hashed password
    """
    salt = settings.password_salt.encode('utf-8')
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100_000)
    return dk.hex()


def _verify_password(password: str, stored_hash: str) -> bool:
    """
    Verify a password against a stored hash.
    
    Args:
        password: Plain text password to verify
        stored_hash: Stored password hash to compare against
        
    Returns:
        True if password matches the hash, False otherwise
    """
    salt = settings.password_salt.encode('utf-8')
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100_000)
    return dk.hex() == stored_hash


async def create_user(username: str, password: str) -> bool:
    """
    Create a new user with plaintext password. Hashes and stores the password.
    
    Args:
        username: Unique username for the new user
        password: Plain text password to hash and store
        
    Returns:
        True if user was created successfully, False if username is already taken
    """
    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("Database not initialized")
    
    phash = _hash_password(password)
    try:
        await conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, phash)
        )
        await conn.commit()
        logger.info(f"Created user: {username}")
        return True
    except Exception as e:
        logger.warning(f"Failed to create user {username}: {e}")
        return False


async def authenticate_user(username: str, password: str, telegram_user_id: int) -> bool:
    """
    Authenticate user with username/password via Deadline RCS API.
    If valid, returns True. No local DB check.
    
    Args:
        username: User's Deadline login
        password: User's Deadline password
        telegram_user_id: Telegram user ID (not used for auth)
    
    Returns:
        True if authentication successful, False otherwise
    """
    try:
        async with aiohttp.ClientSession() as session:
            auth = aiohttp.BasicAuth(username, password)
            async with session.get(f"{settings.base_api_url}/jobs", auth=auth, ssl=False) as resp:
                if resp.status == 200:
                    return True
                else:
                    logger.warning(f"Deadline RCS auth failed for user {username}: {resp.status}")
                    return False
    except Exception as e:
        logger.error(f"Error authenticating user {username} via Deadline RCS: {e}")
        return False


async def is_authorized(telegram_user_id: int) -> bool:
    """
    Check if Deadline credentials exist for the given telegram_user_id.
    Returns True if credentials are found, False otherwise.
    """
    logger.info(f"Checking authorization for user {telegram_user_id}")
    
    # Try user_sessions table
    conn = get_db_connection()
    if conn is not None:
        try:
            async with conn.execute(
                "SELECT 1 FROM user_sessions WHERE telegram_user_id = ?",
                (telegram_user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    logger.info(f"User {telegram_user_id} is authorized (found in user_sessions)")
                    return True
                else:
                    logger.info(f"User {telegram_user_id} not found in user_sessions")
        except Exception as e:
            logger.error(f"Error checking user_sessions for {telegram_user_id}: {e}")
    else:
        logger.warning("Database connection not available")
    
    # Fallback: check credentials.json if used
    try:
        from app.core.config import get_auth_credentials
        login, password, _ = get_auth_credentials(str(telegram_user_id))
        if login and password:
            logger.info(f"User {telegram_user_id} is authorized (found in credentials.json)")
            return True
        else:
            logger.info(f"User {telegram_user_id} not found in credentials.json")
    except Exception as e:
        logger.error(f"Error checking credentials.json for {telegram_user_id}: {e}")
    
    logger.info(f"User {telegram_user_id} is not authorized")
    return False


async def logout_user(telegram_user_id: int) -> bool:
    """
    Clear Deadline credentials for a user, effectively logging them out.
    
    Args:
        telegram_user_id: Telegram user ID to logout
        
    Returns:
        True if logout successful, False otherwise
    """
    success = True
    
    # Remove from user_sessions table
    conn = get_db_connection()
    if conn is not None:
        try:
            await conn.execute(
                "DELETE FROM user_sessions WHERE telegram_user_id = ?",
                (telegram_user_id,)
            )
            await conn.commit()
        except Exception as e:
            logger.error(f"Failed to remove user_sessions for {telegram_user_id}: {e}")
            success = False
    
    # Remove from credentials.json if exists
    try:
        from app.core.config import remove_auth_credentials
        remove_auth_credentials(str(telegram_user_id))
    except Exception as e:
        logger.error(f"Failed to remove credentials.json for {telegram_user_id}: {e}")
        success = False
    
    if success:
        logger.info(f"User with telegram_id {telegram_user_id} logged out")
    
    return success


async def save_deadline_credentials(telegram_user_id: int, deadline_login: str, deadline_password: str) -> bool:
    """
    Save Deadline credentials for a user.
    (Used to persist login/password so we do not ask on each request.)
    
    Args:
        telegram_user_id: Telegram user ID
        deadline_login: Deadline login
        deadline_password: Deadline password
    
    Returns:
        True if saved successfully, False otherwise
    """
    logger.info(f"Saving Deadline credentials for user {telegram_user_id}")
    
    conn = get_db_connection()
    if conn is None:
        logger.error("Database connection not available")
        return False
    
    try:
        # Insert or update session data
        await conn.execute("""
            INSERT OR REPLACE INTO user_sessions 
            (telegram_user_id, deadline_login, deadline_password, last_login) 
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """, (telegram_user_id, deadline_login, deadline_password))
        await conn.commit()
        logger.info(f"Successfully saved Deadline credentials for user {telegram_user_id}")
        
        # Verify the save by checking if the record exists
        async with conn.execute(
            "SELECT 1 FROM user_sessions WHERE telegram_user_id = ?",
            (telegram_user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                logger.info(f"Verified: credentials saved for user {telegram_user_id}")
                return True
            else:
                logger.error(f"Failed to verify saved credentials for user {telegram_user_id}")
                return False
                
    except Exception as e:
        logger.error(f"Failed to save Deadline credentials for user {telegram_user_id}: {e}")
        return False


async def get_deadline_credentials(telegram_user_id: int) -> Optional[Tuple[str, str]]:
    """
    Get Deadline credentials for a user.
    
    Args:
        telegram_user_id: Telegram user ID
        
    Returns:
        Tuple of (login, password) or None if not found
    """
    logger.info(f"Getting Deadline credentials for user {telegram_user_id}")
    
    conn = get_db_connection()
    if conn is None:
        logger.error("Database connection not available")
        return None
    
    try:
        async with conn.execute(
            "SELECT deadline_login, deadline_password FROM user_sessions WHERE telegram_user_id = ?",
            (telegram_user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                logger.info(f"Found credentials for user {telegram_user_id}")
                return (row[0], row[1])
            else:
                logger.info(f"No credentials found for user {telegram_user_id}")
                return None
    except Exception as e:
        logger.error(f"Failed to get Deadline credentials for user {telegram_user_id}: {e}")
        return None


async def toggle_notifications(telegram_user_id: int) -> bool:
    """
    Toggle notification settings for a user.
    
    Args:
        telegram_user_id: Telegram user ID
        
    Returns:
        New notification status
    """
    conn = get_db_connection()
    if conn is None:
        return False
    
    try:
        # Get current status
        async with conn.execute(
            "SELECT notifications_enabled FROM user_sessions WHERE telegram_user_id = ?",
            (telegram_user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            current_status = row[0] if row else 0
        
        # Toggle status
        new_status = 1 if current_status == 0 else 0
        
        # Update status
        await conn.execute(
            "UPDATE user_sessions SET notifications_enabled = ? WHERE telegram_user_id = ?",
            (new_status, telegram_user_id)
        )
        await conn.commit()
        
        logger.info(f"User {telegram_user_id} notifications toggled to: {new_status}")
        return bool(new_status)
    except Exception as e:
        logger.error(f"Failed to toggle notifications for user {telegram_user_id}: {e}")
        return False


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
        async with conn.execute(
            "SELECT notifications_enabled FROM user_sessions WHERE telegram_user_id = ?",
            (telegram_user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False
    except Exception as e:
        logger.error(f"Failed to get notification status for user {telegram_user_id}: {e}")
        return False


async def get_all_users_with_notifications() -> list[Tuple[int, str, str]]:
    """
    Get all users with enabled notifications and their Deadline credentials.
    
    Returns:
        List of tuples containing (telegram_user_id, deadline_login, deadline_password)
    """
    conn = get_db_connection()
    if conn is None:
        logger.error("Database connection not available for get_all_users_with_notifications")
        return []
    
    try:
        async with conn.execute("""
            SELECT telegram_user_id, deadline_login, deadline_password 
            FROM user_sessions 
            WHERE notifications_enabled = 1
        """) as cursor:
            rows = await cursor.fetchall()
            result = [(row[0], row[1], row[2]) for row in rows]
            logger.info(f"Found {len(result)} users with notifications enabled: {[user_id for user_id, _, _ in result]}")
            return result
    except Exception as e:
        logger.error(f"Failed to get users with notifications: {e}")
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
            (telegram_user_id,)
        )
        await conn.commit()
        logger.info(f"Notifications disabled for user {telegram_user_id}")
    except Exception as e:
        logger.error(f"Failed to disable notifications for user {telegram_user_id}: {e}")
