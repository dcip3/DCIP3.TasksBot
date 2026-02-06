"""
Path formatting helpers for user-facing messages.
"""

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
    raw = str(path).replace("\\", "/").strip()
    if not raw:
        return None

    # Strip optional Windows drive prefix (e.g. "Y:/").
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raw = raw[2:]

    parts = []
    for part in raw.split("/"):
        part = part.strip()
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)

    if not parts:
        return None
    return "/" + "/".join(parts)


def extract_dropbox_path(fullpath: Optional[str], root_marker: str) -> Optional[str]:
    if not fullpath or not root_marker:
        return None
    full = str(fullpath)
    idx = full.find(root_marker)
    if idx == -1:
        return None
    trimmed = full[idx:]
    return normalize_dropbox_path(trimmed)
