"""Helpers for preview captions and user-facing text."""

import html
import re
from typing import Optional

from app.core.path_utils import normalize_preview_path


def _pretty_dimensions(value: object) -> str:
    """Turn "2260x1540" into "2260 × 1540", leaving words like "px" alone."""
    return re.sub(r"(?<=\d)\s*x\s*(?=\d)", " × ", str(value).strip())


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
    overscan: Optional[str] = None,
    passes: Optional[str] = None,
) -> str:
    """Build a consistent caption for preview media.

    Layout: title, blank line, optional render details (resolution, overscan,
    extra passes, camera LUT, camera color controls), blank line, source path.
    """
    parts = [f"{icon} {_normalize_title(title)}"]

    details = []
    if resolution:
        details.append(f"📐 <code>{html.escape(_pretty_dimensions(resolution))}</code>")
    if overscan:
        # The preview is wider than the delivered frame; say so, because
        # otherwise the extra margin reads as part of the shot.
        details.append(f"🖼 <code>{html.escape(_pretty_dimensions(overscan))}</code>")
    if passes:
        # The AOVs riding along with the beauty. Names only - no dimension
        # formatting, because an AOV may legitimately be called "x2".
        details.append(f"🧩 <code>{html.escape(str(passes))}</code>")
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
