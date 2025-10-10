# app/core/config.py
"""
Application configuration management.

This module provides a centralized configuration system using Pydantic Settings
for environment variable management and validation.
"""

import json
from pathlib import Path
from typing import Dict, Any, Tuple, Optional
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings configuration.
    
    Manages all application settings including Telegram bot token,
    Deadline API configuration, Dropbox API credentials, and local configuration options.
    """
    
    # Telegram Bot Configuration
    tg_api_token: str
    
    # Deadline API Configuration
    base_api_url: str = Field(..., description="Base URL for the Deadline API")
    
    # Dropbox API Configuration
    dropbox_app_key: str
    dropbox_app_secret: str
    dropbox_refresh_token: str
    dropbox_team_member_id: str
    dropbox_root_namespace_id: str
    dropbox_root_marker: str = "Team Folder"
    
    # Local Application Settings
    db_path: str = Field("storage/tasks_bot.db", description="SQLite database file path")
    credentials_file: str = Field("credentials.json", description="Fallback credentials storage file")
    temp_dir: str = Field("storage/temp", description="Temp directory for intermediate files")
    conv_dir: str = Field("storage/conv", description="Directory for converted files")
    http_timeout: int = Field(30, description="HTTP timeout for external requests in seconds")

    ocio_config_path: str = Field("storage/config.ocio", description="Path to OCIO configuration file")
    ffmpeg_path: str = Field("ffmpeg", description="Path to ffmpeg executable on Deadline workers")

    # Security Settings
    password_salt: str = Field(..., description="Salt for password hashing")

    # Application Limits
    max_concurrent_downloads: int = Field(2, ge=1, description="Max parallel Dropbox downloads")
    min_free_space_bytes: int = Field(10 * 1024 * 1024 * 1024, description="Minimum required free disk space in bytes")

    # Mini App Configuration
    mini_app_url: str = Field("http://localhost:3000", description="URL for the Telegram mini app")
    
    # Worker and Job Status Mappings
    worker_status_map: Dict[int, str] = {
        0: "Unknown",
        1: "Rendering", 
        2: "Idle",
        3: "Offline",
        4: "Stalled",
        8: "StartingJob"
    }
    
    job_status_map: Dict[int, str] = {
        0: "Unknown",
        1: "Active",
        2: "Suspended", 
        3: "Completed",
        4: "Failed",
        6: "Pending"
    }

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @field_validator("dropbox_root_marker")
    def non_empty_marker(cls, v):
        """Validate that dropbox_root_marker is not empty"""
        if not v.strip():
            raise ValueError("DROPBOX_ROOT_MARKER must not be empty")
        return v


# Global settings instance
settings = Settings()  # type: ignore[reportCallIssue]


def load_credentials() -> Dict[str, Tuple[str, str, bool]]:
    """
    Load stored user credentials from JSON file.
    
    Returns:
        Dictionary mapping user_id to (login, password, notifications_enabled)
    """
    credentials_path = Path(settings.credentials_file)
    if credentials_path.exists():
        try:
            with credentials_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                # Convert to proper format: user_id -> (login, password, notifications)
                result = {}
                for user_id, creds in data.items():
                    if isinstance(creds, list) and len(creds) >= 2:
                        login, password = creds[0], creds[1]
                        notifications = creds[2] if len(creds) > 2 else False
                        result[user_id] = (login, password, notifications)
                return result
        except Exception as e:
            print(f"Error loading credentials: {e}")
    return {}


def save_credentials(data: Dict[str, Tuple[str, str, bool]]) -> None:
    """
    Save user credentials to JSON file.
    
    Args:
        data: Dictionary mapping user_id to (login, password, notifications_enabled)
    """
    credentials_path = Path(settings.credentials_file)
    try:
        # Convert to list format for JSON serialization
        json_data = {}
        for user_id, (login, password, notifications) in data.items():
            json_data[user_id] = [login, password, notifications]
        
        with credentials_path.open("w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving credentials: {e}")


def get_auth_credentials(user_id: str) -> Tuple[Optional[str], Optional[str], bool]:
    """
    Retrieve stored credentials for a given user.
    
    Args:
        user_id: Telegram user ID as string
        
    Returns:
        Tuple of (login, password, notifications_enabled) or (None, None, False) if missing
    """
    credentials = load_credentials()
    creds = credentials.get(user_id)
    if creds and len(creds) >= 2:
        login, password = creds[0], creds[1]
        notifications = creds[2] if len(creds) > 2 else False
        return login, password, notifications
    return None, None, False


def remove_auth_credentials(user_id: str) -> bool:
    """
    Remove stored credentials for a given user.
    
    Args:
        user_id: Telegram user ID as string
        
    Returns:
        True if credentials were removed, False otherwise
    """
    credentials = load_credentials()
    if user_id in credentials:
        del credentials[user_id]
        save_credentials(credentials)
        return True
    return False


# Initialize global credentials
user_credentials = load_credentials() 
