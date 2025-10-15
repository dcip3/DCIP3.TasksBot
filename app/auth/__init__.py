# app/auth/__init__.py
"""
User authentication and management system.

This module provides user authentication, password hashing, and user management
functions including login, logout, and session management for TasksBot.
"""

import hashlib
import logging
from typing import Optional, Tuple, Literal
from cryptography.fernet import Fernet
from app.core.config import settings
from app.core.database import get_db_connection
import aiohttp

logger = logging.getLogger(__name__)

# Initialize Fernet cipher for password encryption
_cipher: Optional[Fernet] = None

NotificationScope = Literal["all", "own"]
DEFAULT_NOTIFICATION_SCOPE: NotificationScope = "all"
VALID_NOTIFICATION_SCOPES = {"all", "own"}

def _get_cipher() -> Fernet:
    """Get or create Fernet cipher for password encryption."""
    global _cipher
    if _cipher is None:
        _cipher = Fernet(settings.encryption_key.encode())
    return _cipher


def _encrypt_password(password: str) -> str:
    """
    Encrypt password using Fernet symmetric encryption.

    Args:
        password: Plain text password to encrypt

    Returns:
        Encrypted password as base64 string
    """
    cipher = _get_cipher()
    encrypted = cipher.encrypt(password.encode('utf-8'))
    return encrypted.decode('utf-8')


def _decrypt_password(encrypted_password: str) -> str:
    """
    Decrypt password using Fernet symmetric encryption.

    Args:
        encrypted_password: Encrypted password as base64 string

    Returns:
        Decrypted plain text password
    """
    cipher = _get_cipher()
    decrypted = cipher.decrypt(encrypted_password.encode('utf-8'))
    return decrypted.decode('utf-8')


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
        deadline_password: Deadline password (will be encrypted before storage)

    Returns:
        True if saved successfully, False otherwise
    """
    logger.info(f"Saving Deadline credentials for user {telegram_user_id}")

    conn = get_db_connection()
    if conn is None:
        logger.error("Database connection not available")
        return False

    try:
        # Encrypt password before storage
        encrypted_password = _encrypt_password(deadline_password)

        # Insert or update session data
        await conn.execute("""
            INSERT OR REPLACE INTO user_sessions
            (telegram_user_id, deadline_login, deadline_password, last_login)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """, (telegram_user_id, deadline_login, encrypted_password))
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
        Tuple of (login, decrypted_password) or None if not found
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
                # Decrypt password before returning
                try:
                    decrypted_password = _decrypt_password(row[1])
                    return (row[0], decrypted_password)
                except Exception as decrypt_error:
                    logger.error(f"Failed to decrypt password for user {telegram_user_id}: {decrypt_error}")
                    return None
            else:
                logger.info(f"No credentials found for user {telegram_user_id}")
                return None
    except Exception as e:
        logger.error(f"Failed to get Deadline credentials for user {telegram_user_id}: {e}")
        return None


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
        logger.error(f"Failed to fetch notification settings for user {telegram_user_id}: {e}")
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
        logger.error(f"Failed to get notification status for user {telegram_user_id}: {e}")
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
        async with conn.execute("""
            SELECT telegram_user_id, deadline_login, deadline_password, notification_scope
            FROM user_sessions 
            WHERE notifications_enabled = 1
        """) as cursor:
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
