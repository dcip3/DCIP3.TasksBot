"""UI helpers and access decorators for bot handlers."""

from __future__ import annotations

from functools import wraps

from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup


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
