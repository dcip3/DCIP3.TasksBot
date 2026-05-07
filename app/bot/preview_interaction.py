"""Telegram adapter for preview workflow interactions."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup

from app.core.bot_core import bot
from app.core.ui_helpers import cancel_inline_button


class TelegramPreviewInteraction:
    def __init__(self, callback_query: CallbackQuery) -> None:
        if callback_query.from_user is None:
            raise ValueError("Callback query has no user")
        self.callback_query = callback_query
        self.user_id = callback_query.from_user.id

    def _cancel_keyboard(self, callback_data: str | None) -> InlineKeyboardMarkup | None:
        if callback_data is None:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[[cancel_inline_button(callback_data=callback_data)]]
        )

    async def create_progress(
        self,
        text: str,
        *,
        cancel_callback_data: str | None = None,
    ) -> Any:
        reply_markup = self._cancel_keyboard(cancel_callback_data)
        if self.callback_query.message:
            return await self.callback_query.message.answer(text, reply_markup=reply_markup)
        return await bot.send_message(self.user_id, text, reply_markup=reply_markup)

    async def update_progress(
        self,
        handle: Any,
        text: str,
        *,
        cancel_callback_data: str | None = None,
    ) -> None:
        await handle.edit_text(
            text,
            reply_markup=self._cancel_keyboard(cancel_callback_data),
        )

    async def delete_progress(self, handle: Any) -> None:
        with contextlib.suppress(Exception):
            await handle.delete()

    async def send_text(self, text: str, *, parse_mode: str | None = None) -> None:
        if self.callback_query.message:
            await self.callback_query.message.answer(text, parse_mode=parse_mode)
            return
        await bot.send_message(self.user_id, text, parse_mode=parse_mode)

    async def send_photo(
        self,
        path: Path,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        photo = FSInputFile(str(path))
        if self.callback_query.message:
            await self.callback_query.message.answer_photo(
                photo=photo,
                caption=caption,
                parse_mode=parse_mode,
            )
            return
        await bot.send_photo(self.user_id, photo, caption=caption, parse_mode=parse_mode)

    async def send_video(
        self,
        path: Path,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        video = FSInputFile(str(path))
        if self.callback_query.message:
            await self.callback_query.message.answer_video(
                video=video,
                caption=caption,
                parse_mode=parse_mode,
            )
            return
        await bot.send_video(self.user_id, video, caption=caption, parse_mode=parse_mode)

    async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
        with contextlib.suppress(Exception):
            await self.callback_query.answer(text, show_alert=show_alert)
