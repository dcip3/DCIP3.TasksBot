import logging
from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.auth import (
    authenticate_user,
    is_authorized,
    logout_user,
    save_deadline_credentials,
)
from app.core.ui_helpers import get_main_keyboard

logger = logging.getLogger(__name__)

router = Router()

def build_login_cancel_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard to cancel login flow."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Cancel", callback_data="login_cancel")]
        ]
    )

class LoginStates(StatesGroup):
    """State machine for user login process."""

    USERNAME = State()
    PASSWORD = State()


@router.message(Command("login"))
async def cmd_login_start(message: Message, state: FSMContext) -> None:
    """Start the login process by requesting Deadline login."""
    if message.from_user is None:
        await message.answer("Error: User information not available.")
        return

    if await is_authorized(message.from_user.id):
        await message.answer("You are already authorized.")
        return

    await state.clear()
    await state.set_state(LoginStates.USERNAME)
    await message.answer(
        "Enter your Deadline login:",
        reply_markup=build_login_cancel_keyboard(),
    )


@router.message(StateFilter(LoginStates.USERNAME))
async def process_login_username(message: Message, state: FSMContext) -> None:
    """Process Deadline login input and request password."""
    if message.text is None:
        await message.answer("Please enter a valid Deadline login.")
        return

    await state.update_data(username=message.text.strip())
    await message.answer(
        "Enter your Deadline password:",
        reply_markup=build_login_cancel_keyboard(),
    )
    await state.set_state(LoginStates.PASSWORD)


@router.message(StateFilter(LoginStates.PASSWORD))
async def process_login_password(message: Message, state: FSMContext) -> None:
    """Process Deadline password input and authenticate user via Deadline RCS."""
    if message.text is None:
        await message.answer("Please enter a valid Deadline password.")
        return

    if message.from_user is None:
        await message.answer("Error: User information not available.")
        await state.clear()
        return

    data = await state.get_data()
    username = data.get("username")
    password = message.text.strip()

    if not isinstance(username, str) or not username:
        await message.answer("Error: Deadline login not received. Please use /login again.")
        await state.clear()
        return

    ok = await authenticate_user(username, password, message.from_user.id)
    if ok:
        await save_deadline_credentials(message.from_user.id, username, password)
        await message.answer(
            "Successfully authorized! Welcome to TasksBot.",
            reply_markup=get_main_keyboard(),
        )
    else:
        await message.answer("Invalid Deadline login or password.")

    await state.clear()


@router.callback_query(F.data == "login_cancel")
async def login_cancel_callback(callback_query: CallbackQuery, state: FSMContext) -> None:
    """Cancel the login flow and clear state."""
    await state.clear()
    if callback_query.message:
        try:
            await callback_query.message.edit_text("Login cancelled.")
        except Exception:
            await callback_query.message.answer("Login cancelled.")
    await callback_query.answer("Login cancelled.", show_alert=False)


@router.message(Command("logout"))
async def cmd_logout(message: Message) -> None:
    """Logout the current user."""
    if message.from_user is None:
        await message.answer("Error: User information not available.")
        return

    if not await is_authorized(message.from_user.id):
        await message.answer("You were not logged in.")
        return

    await logout_user(message.from_user.id)
    await message.answer("You have been logged out.")
