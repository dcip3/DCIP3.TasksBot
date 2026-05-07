# app/core/config.py
"""
Application configuration management.

This module provides a centralized configuration system using Pydantic Settings
for environment variable management and validation.
"""

from typing import Dict, Optional
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
    telegram_bot_token: str
    
    # Deadline API Configuration
    deadline_api_url: str = Field(
        ...,
        description="Base URL for the Deadline API",
    )
    deadline_tls_verify: bool = Field(
        True,
        description="Verify TLS certificates for Deadline API requests",
    )
    
    # Dropbox API Configuration
    dropbox_app_key: str
    dropbox_app_secret: str
    dropbox_refresh_token: str
    dropbox_team_member_id: str
    dropbox_root_namespace_id: str
    dropbox_root_marker: str = "Team Folder"
    
    # Local Application Settings
    sqlite_db_path: str = Field("data/app.db", description="SQLite database file path")
    temp_dir: str = Field("data/temp", description="Temp directory for intermediate files")
    conv_dir: str = Field("data/conv", description="Directory for converted files")
    http_timeout: int = Field(30, description="HTTP timeout for external requests in seconds")

    ocio_config_path: str = Field("data/config.ocio", description="Path to OCIO configuration file")
    preview_apply_color_transform: bool = Field(
        True,
        description="Apply OCIO color transform when generating preview videos",
    )
    preview_input_space: str = Field(
        "ACEScg",
        description="OCIO input color space for preview conversion",
    )
    preview_display: str = Field(
        "sRGB",
        description="OCIO display for preview conversion",
    )
    preview_view: str = Field(
        "ACES 1.0 SDR-video",
        description="OCIO view for preview conversion",
    )
    preview_lut_size: int = Field(
        65,
        description="Cube size to bake for preview LUT (only used on render nodes)",
    )
    preview_ocio_remote_config: Optional[str] = Field(
        default=None,
        description="Absolute path to OCIO config accessible from Deadline workers",
    )
    preview_attach_ocio_config: bool = Field(
        False,
        description="Upload the local OCIO config as an auxiliary file if workers cannot access it directly",
    )
    preview_python_executable: str = Field(
        "python",
        description="Python executable available on Deadline workers to run preview helper script",
    )
    preview_temp_dir: Optional[str] = Field(
        default=None,
        description="Optional temp directory on workers for preview helper script",
    )
    preview_color_mode: str = Field(
        "lut",
        description="Color transform mode for previews: 'lut' generates a LUT, 'cpu' applies OCIO on CPU",
    )
    ffmpeg_path: str = Field("ffmpeg", description="Path to ffmpeg executable on Deadline workers")
    preview_upload_enabled: bool = Field(
        False,
        description="Enable worker-to-bot preview upload endpoint",
    )
    preview_upload_bind_host: str = Field(
        "0.0.0.0",
        description="Bind host for preview upload HTTP server",
    )
    preview_upload_port: int = Field(
        8081,
        description="Port for preview upload HTTP server",
    )
    preview_upload_url: Optional[str] = Field(
        default=None,
        description="Public URL that workers should POST previews to",
    )
    preview_upload_token_ttl: int = Field(
        7 * 24 * 60 * 60,
        ge=60,
        description="One-time preview upload token TTL in seconds",
    )
    preview_upload_max_mb: int = Field(
        100,
        ge=1,
        description="Max upload size for preview videos in MB",
    )
    preview_upload_insecure: bool = Field(
        False,
        description="Allow insecure TLS for worker uploads (self-signed certs)",
    )
    preview_upload_delivery_wait_seconds: int = Field(
        600,
        ge=60,
        description="How long to wait for worker preview upload delivery after Deadline completion",
    )
    preview_upload_delivery_max_attempts: int = Field(
        10,
        ge=1,
        description="Maximum bot-side attempts to deliver a received worker preview upload",
    )
    preview_upload_recovery_interval_seconds: int = Field(
        120,
        ge=30,
        description="Interval for retrying received worker preview uploads",
    )

    # Security Settings
    encryption_key: str = Field(..., description="Fernet encryption key for password storage (generate with Fernet.generate_key())")

    # Application Limits
    max_concurrent_downloads: int = Field(2, ge=1, description="Max parallel Dropbox downloads")
    min_free_space_bytes: int = Field(10 * 1024 * 1024 * 1024, description="Minimum required free disk space in bytes")

    # Job monitoring intervals (seconds)
    job_watcher_interval_normal: int = Field(60, ge=5, description="Job monitoring interval in seconds (normal mode)")
    job_watcher_interval_preview: int = Field(5, ge=5, description="Job monitoring interval in seconds (when preview jobs are active)")

    # Development mode
    dev_mode: bool = Field(False, description="Enable development mode (disables some security checks)")

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

    @field_validator("preview_ocio_remote_config", mode="before")
    def normalize_preview_ocio_remote_config(cls, v):
        """Normalize optional OCIO config path sent via env."""
        if v is None:
            return None
        if isinstance(v, str):
            raw = v.strip()
            if not raw:
                return None
            if raw.lower() in {"auto", "default", "system", "none", "local"}:
                return None
            return raw
        return v

# Global settings instance
settings = Settings()  # type: ignore[reportCallIssue]
