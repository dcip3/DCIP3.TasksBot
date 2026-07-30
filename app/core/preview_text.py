"""Helpers for preview captions and user-facing text."""

import html
from typing import Optional

from app.core.path_utils import normalize_preview_path


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
    lut: Optional[str] = None,
    resolution: Optional[str] = None,
    color_controls: Optional[str] = None,
) -> str:
    """Build a consistent caption for preview media.

    Layout: title, blank line, optional render details (resolution, camera LUT,
    camera color controls), blank line, source path.
    """
    parts = [f"{icon} {_normalize_title(title)}"]

    details = []
    if resolution:
        display_resolution = str(resolution).strip().replace("x", " × ")
        details.append(f"📐 <code>{html.escape(display_resolution)}</code>")
    if lut:
        details.append(f"🎨 <code>{html.escape(str(lut))}</code>")
    if color_controls:
        details.append(f"🎛 <code>{html.escape(str(color_controls))}</code>")
    if details:
        parts.append("\n".join(details))

    if path:
        display_path = normalize_preview_path(path) or str(path)
        parts.append(f"<code>{display_path}</code>")

    return "\n\n".join(parts)
