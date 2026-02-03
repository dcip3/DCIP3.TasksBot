"""Helpers for preview captions and user-facing text."""

from typing import Optional

from app.core.path_utils import normalize_display_path


def _normalize_title(title: Optional[str]) -> str:
    text = str(title or "").strip()
    if text.lower().endswith(".mp4"):
        text = text[:-4]
    return text or "Preview"


def build_preview_caption(
    title: Optional[str],
    path: Optional[str],
    *,
    icon: str = "📁",
) -> str:
    """Build a consistent caption for preview media."""
    header = f"{icon} {_normalize_title(title)}"
    if not path:
        return header
    display_path = normalize_display_path(path) or str(path)
    return f"{header}\n<code>{display_path}</code>"
