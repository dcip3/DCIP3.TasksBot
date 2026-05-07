"""UI boundary for preview workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class PreviewInteraction(Protocol):
    user_id: int

    async def create_progress(
        self,
        text: str,
        *,
        cancel_callback_data: str | None = None,
    ) -> Any:
        ...

    async def update_progress(
        self,
        handle: Any,
        text: str,
        *,
        cancel_callback_data: str | None = None,
    ) -> None:
        ...

    async def delete_progress(self, handle: Any) -> None:
        ...

    async def send_text(self, text: str, *, parse_mode: str | None = None) -> None:
        ...

    async def send_photo(
        self,
        path: Path,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        ...

    async def send_video(
        self,
        path: Path,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        ...

    async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
        ...
