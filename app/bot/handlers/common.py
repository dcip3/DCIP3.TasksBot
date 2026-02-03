import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.handlers.auth import LoginStates, build_login_cancel_keyboard
from app.core.utils import get_main_keyboard

logger = logging.getLogger(__name__)

router = Router()


HELP_TEXT = (
    "ℹ️ TasksBot helps you monitor Deadline jobs, manage queues, and deliver previews.\n\n"
    "Buttons:\n"
    "📂 Jobs — browse jobs and actions.\n"
    "🖥️ Workers — check render node status.\n"
    "⚙️ Settings — notifications and preview defaults.\n"
    "\n"
    "Commands:\n"
    "/start — start the bot.\n"
    "/login — authenticate.\n"
    "/logout — log out.\n"
    "/help — show this help."
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Handle /start command: send welcome message and main keyboard."""
    await message.answer(
        text="TasksBot started. Choose an action:",
        reply_markup=get_main_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Show help text."""
    await message.answer(HELP_TEXT, reply_markup=get_main_keyboard())


@router.message()
async def handle_unknown_message(message: Message, state: FSMContext) -> None:
    """Handle any message that doesn't match other handlers."""
    current_state = await state.get_state()

    if current_state in [LoginStates.USERNAME, LoginStates.PASSWORD]:
        if current_state == LoginStates.USERNAME:
            await message.answer(
                "Please enter your Deadline login.",
                reply_markup=build_login_cancel_keyboard(),
            )
        elif current_state == LoginStates.PASSWORD:
            await message.answer(
                "Please enter your Deadline password.",
                reply_markup=build_login_cancel_keyboard(),
            )
        return

    await message.answer(
        "I don't understand this command. Please use the buttons below or type /start to see available options.",
        reply_markup=get_main_keyboard(),
    )
