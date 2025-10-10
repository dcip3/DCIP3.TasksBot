"""
Helpers for working with the application's persistent storage directories.
"""

from pathlib import Path

from app.core.config import settings


def get_storage_path() -> Path:
    """Return the root storage path configured for the application."""
    return Path(settings.temp_dir).parent


def ensure_storage_structure() -> None:
    """Create the expected storage directories if they are missing."""
    storage_root = get_storage_path()
    for subdir in (storage_root / "temp", storage_root / "conv"):
        subdir.mkdir(parents=True, exist_ok=True)
