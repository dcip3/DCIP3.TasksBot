import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.handlers.auth import LoginStates
from app.bot.handlers.realtime import stop_realtime_for_chat
from app.core.bot_core import bot
from app.core.utils import get_main_keyboard

logger = logging.getLogger(__name__)

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Handle /start command: send welcome message and main keyboard."""
    await message.answer(
        text="TasksBot started. Choose an action:",
        reply_markup=get_main_keyboard(),
    )


@router.message(Command("setup_menu"))
async def cmd_setup_menu(message: Message) -> None:
    """Setup the menu button for Mini App."""
    from app.core.utils import setup_menu_button

    try:
        await message.answer("🔄 Setting up menu button...")
        await setup_menu_button()
        await message.answer(
            "✅ Menu button setup successfully! You should now see the 'Tasks' button in the chat menu."
        )
    except Exception as exc:
        await message.answer(f"❌ Failed to setup menu button: {exc}")
        logger.error("Setup menu button error: %s", exc)


@router.message(Command("menu_status"))
async def cmd_menu_status(message: Message) -> None:
    """Check the current menu button status."""
    try:
        current_button = await bot.get_chat_menu_button()
        await message.answer(f"📋 Current menu button: {current_button}")
    except Exception as exc:
        await message.answer(f"❌ Failed to get menu button status: {exc}")
        logger.error("Get menu button status error: %s", exc)


@router.message(F.text == "🧹 Clear")
async def clear_chat_handler(message: Message) -> None:
    """Clear chat history by deleting recent messages."""
    chat_id = message.chat.id
    from_message_id = message.message_id
    deleted_count = 0
    consecutive_failures = 0
    max_consecutive = 20

    for i in range(0, 1000):
        msg_id = from_message_id - i
        if msg_id <= 0:
            break
        try:
            await bot.delete_message(chat_id, msg_id)
            deleted_count += 1
            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive:
                break

    keyboard = get_main_keyboard()
    await message.answer("\u200b\nMessages deleted: {}".format(deleted_count), reply_markup=keyboard)


@router.message()
async def handle_unknown_message(message: Message, state: FSMContext) -> None:
    """Handle any message that doesn't match other handlers."""
    stop_realtime_for_chat(message.chat.id)

    current_state = await state.get_state()

    if current_state in [LoginStates.USERNAME, LoginStates.PASSWORD]:
        if current_state == LoginStates.USERNAME:
            await message.answer(
                "Please enter your Deadline login, or use /cancel to stop the login process."
            )
        elif current_state == LoginStates.PASSWORD:
            await message.answer(
                "Please enter your Deadline password, or use /cancel to stop the login process."
            )
        return

    await message.answer(
        "I don't understand this command. Please use the buttons below or type /start to see available options.",
        reply_markup=get_main_keyboard(),
    )
