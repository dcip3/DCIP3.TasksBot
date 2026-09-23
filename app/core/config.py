# app/core/config.py
"""
Application configuration management.

This module provides a centralized configuration system using Pydantic Settings
for environment variable management and validation.
"""

from typing import Dict, Optional
from zoneinfo import ZoneInfo
from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings configuration.

    Manages all application settings including Telegram bot token,
    Deadline API configuration, and local configuration options.
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

    # Local Application Settings
    sqlite_db_path: str = Field("data/app.db", description="SQLite database file path")
    temp_dir: str = Field("data/temp", description="Temp directory for intermediate files")
    conv_dir: str = Field("data/conv", description="Directory for converted files")

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
    deadline_event_secret: Optional[str] = Field(
        default=None,
        description=(
            "Shared secret for the /deadline-event push endpoint (must match the "
            "TasksBot Deadline event plugin config); empty disables the endpoint"
        ),
    )
    preview_max_dimension: int = Field(
        1920,
        ge=0,
        description=(
            "Fit previews inside this many pixels on their longest side; a "
            "render's own size is not always one Telegram's player can decode "
            "(0 keeps the render size)"
        ),
    )
    preview_presubmit_enabled: bool = Field(
        True,
        description=(
            "Submit auto previews while the source render is finishing its last tasks, "
            "so the freed worker picks the preview before the next render job"
        ),
    )
    preview_presubmit_input_wait: int = Field(
        3600,
        ge=60,
        description="input-wait-seconds passed to presubmitted preview jobs (frames may still be rendering)",
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

    # Job monitoring intervals (seconds)
    job_watcher_interval_normal: int = Field(60, ge=5, description="Job monitoring interval in seconds (normal mode)")
    job_watcher_interval_preview: int = Field(5, ge=5, description="Job monitoring interval in seconds (when preview jobs are active)")

    # Maintenance scheduler (6-hourly file cleanup, hourly housekeeping)
    scheduler_timezone: str = Field(
        "UTC",
        description="IANA time zone the maintenance jobs are scheduled in, e.g. Europe/Berlin",
    )

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
        extra="ignore",
        # A rejected value is often a mistyped copy of a secret such as
        # ENCRYPTION_KEY; the startup error must not print it into the logs.
        hide_input_in_errors=True,
    )

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

    @field_validator("encryption_key")
    def validate_encryption_key(cls, v):
        """Stop at startup, not at the first login that has a password to store."""
        try:
            Fernet(v.encode())
        except Exception as exc:
            raise ValueError(
                "ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64-encoded "
                "bytes); generate one with Fernet.generate_key()"
            ) from exc
        return v

    @field_validator("scheduler_timezone")
    def validate_scheduler_timezone(cls, v):
        """Fail at startup with an error naming this setting, not later in APScheduler."""
        name = v.strip()
        try:
            ZoneInfo(name)
        except Exception as exc:
            raise ValueError(f"{v!r} is not a known IANA time zone") from exc
        return name

# Global settings instance
settings = Settings()  # type: ignore[reportCallIssue]
