"""
Path formatting helpers for user-facing messages.
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Optional


def normalize_display_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    value = str(path).strip()
    if not value:
        return None
    if len(value) >= 3 and value[0] == "/" and value[1].isalpha() and value[2] == ":":
        value = value[1:]
    is_windows = ("\\" in value) or (len(value) >= 2 and value[1] == ":")
    path_obj = PureWindowsPath(value) if is_windows else PurePosixPath(value)
    anchor = path_obj.anchor
    parts = path_obj.parts
    stack = []
    for part in parts:
        if anchor and part == anchor:
            continue
        if part in {"", "."}:
            continue
        if part == "..":
            if stack and stack[-1] != "..":
                stack.pop()
            elif not anchor:
                stack.append(part)
            continue
        stack.append(part)
    cls = type(path_obj)
    if anchor:
        normalized = cls(anchor, *stack)
    else:
        normalized = cls(*stack)
    return str(normalized)


def normalize_dropbox_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    normalized = str(path).replace("\\", "/").strip()
    if not normalized:
        return None
    return f"/{normalized.lstrip('/')}"


def extract_dropbox_path(fullpath: Optional[str], root_marker: str) -> Optional[str]:
    if not fullpath or not root_marker:
        return None
    idx = str(fullpath).find(root_marker)
    if idx == -1:
        return None
    trimmed = str(fullpath)[idx:]
    return normalize_dropbox_path(trimmed)
