import logging
from aiogram import Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.auth import (
    authenticate_user,
    is_authorized,
    logout_user,
    save_deadline_credentials,
)
from app.core.utils import get_main_keyboard

logger = logging.getLogger(__name__)

router = Router()


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
    await message.answer("Enter your Deadline login:")


@router.message(StateFilter(LoginStates.USERNAME))
async def process_login_username(message: Message, state: FSMContext) -> None:
    """Process Deadline login input and request password."""
    if message.text is None:
        await message.answer("Please enter a valid Deadline login.")
        return

    await state.update_data(username=message.text.strip())
    await message.answer("Enter your Deadline password:")
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


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Cancel the current operation and clear state."""
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("No active operation to cancel.")
        return

    await state.clear()
    await message.answer("Operation cancelled. You can start over with /login or /start.")
