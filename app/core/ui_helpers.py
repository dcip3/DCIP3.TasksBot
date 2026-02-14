"""UI helpers and access decorators for bot handlers."""

from __future__ import annotations

from functools import wraps
from typing import Any, Optional

from aiogram.types import InlineKeyboardButton, KeyboardButton, Message, ReplyKeyboardMarkup


def authorized_only(handler):
    """Decorator that allows handler execution only for authorized users."""

    @wraps(handler)
    async def wrapper(message: Message, *args, **kwargs):
        from app.auth import is_authorized

        if message.from_user is None:
            await message.reply("Access denied. User information not available.")
            return
        if not await is_authorized(message.from_user.id):
            await message.reply("Access denied. Please use /login to authenticate.")
            return
        return await handler(message, *args, **kwargs)

    return wrapper


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Create the main persistent keyboard for top-level navigation."""
    kb = [
        [
            KeyboardButton(text="📂 Jobs", request_contact=False, request_location=False),
            KeyboardButton(text="🖥️ Workers", request_contact=False, request_location=False),
        ],
        [
            KeyboardButton(text="⚙️ Settings", request_contact=False, request_location=False),
        ],
    ]
    return ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=False,
        is_persistent=False,
        input_field_placeholder="",
    )


def inline_button(
    *,
    text: str,
    callback_data: Optional[str] = None,
    style: Optional[str] = None,
    **kwargs: Any,
) -> InlineKeyboardButton:
    """Create an inline button with optional Bot API style."""
    payload: dict[str, Any] = {"text": text, **kwargs}
    if callback_data is not None:
        payload["callback_data"] = callback_data
    if style:
        payload["style"] = style
    return InlineKeyboardButton(**payload)


def back_inline_button(callback_data: str, text: str = "⬅️ Back") -> InlineKeyboardButton:
    """Create a blue-styled back button."""
    return inline_button(text=text, callback_data=callback_data, style="primary")


def close_inline_button(callback_data: str, text: str = "✖️ Close") -> InlineKeyboardButton:
    """Create a red-styled close button."""
    return inline_button(text=text, callback_data=callback_data, style="danger")


def cancel_inline_button(callback_data: str, text: str = "✖️ Cancel") -> InlineKeyboardButton:
    """Create a red-styled cancel button."""
    return inline_button(text=text, callback_data=callback_data, style="danger")


def selectable_inline_button(
    *,
    text: str,
    callback_data: str,
    selected: bool = False,
    prefix_selected: str = "✅ ",
    prefix_unselected: str = "",
) -> InlineKeyboardButton:
    """Create an option button with green style for selected state."""
    prefix = prefix_selected if selected else prefix_unselected
    style = "success" if selected else None
    return inline_button(
        text=f"{prefix}{text}",
        callback_data=callback_data,
        style=style,
    )
