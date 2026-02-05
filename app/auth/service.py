"""Deadline credential authentication and session management for TasksBot."""

import logging
from typing import Optional, Tuple
from cryptography.fernet import Fernet
from app.core.config import settings
from app.storage.database import get_db_connection
import aiohttp

logger = logging.getLogger(__name__)

# Initialize Fernet cipher for password encryption
_cipher: Optional[Fernet] = None


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
            async with session.get(
                f"{settings.deadline_api_url}/jobs",
                auth=auth,
                ssl=settings.deadline_tls_verify,
            ) as resp:
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
