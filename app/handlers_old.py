# app/handlers.py
"""
Telegram bot command and state handlers.

This module contains all the message handlers, callback handlers, and state
machines for user interactions including authentication, job management,
worker monitoring, and realtime operations.
"""

import logging
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.utils import authorized_only, get_main_keyboard
from app.core.bot_core import bot, dp
from app.core.config import settings, user_credentials
from app.auth import authenticate_user, logout_user, is_authorized, toggle_notifications
from app.services import (
    get_jobs_list, get_workers_list, get_job_info, get_job_tasks,
    requeue_job, resume_job, suspend_job, delete_job,
    download_job_folder, create_video_from_job
)

logger = logging.getLogger(__name__)

# ============================================================================
# === STATE MACHINES ===
# ============================================================================

class LoginStates(StatesGroup):
    """State machine for user login process"""
    USERNAME = State()
    PASSWORD = State()
    DEADLINE_LOGIN = State()
    DEADLINE_PASSWORD = State()

# ============================================================================
# === AUTHENTICATION HANDLERS ===
# ============================================================================

async def cmd_login_start(message: types.Message):
    """
    Start the login process by requesting username.
    
    Args:
        message: Telegram message object
    """
    if await is_authorized(message.from_user.id):
        await message.reply("You are already authorized.")
        return
    await message.reply("Enter your username:")
    await LoginStates.USERNAME.set()


async def process_login_username(message: types.Message, state: FSMContext):
    """
    Process username input and request password.
    
    Args:
        message: Telegram message containing username
        state: FSM context for state management
    """
    await state.update_data(username=message.text.strip())
    await message.reply("Enter your password:")
    await LoginStates.PASSWORD.set()


async def process_login_password(message: types.Message, state: FSMContext):
    """
    Process password input and authenticate user.
    
    Args:
        message: Telegram message containing password
        state: FSM context for state management
    """
    data = await state.get_data()
    username = data.get("username")
    password = message.text.strip()
    
    if not isinstance(username, str) or not username:
        await message.reply("Error: username not received. Please use /login again.")
        await state.finish()
        return
        
    ok = await authenticate_user(username, password, message.from_user.id)
    if ok:
        await message.reply("Authentication successful! Now enter your Deadline login:")
        await LoginStates.DEADLINE_LOGIN.set()
    else:
        await message.reply("Invalid login or password.")
        await state.finish()


async def process_deadline_login(message: types.Message, state: FSMContext):
    """
    Process Deadline login input and request password.
    
    Args:
        message: Telegram message containing Deadline login
        state: FSM context for state management
    """
    await state.update_data(deadline_login=message.text.strip())
    await message.reply("Enter your Deadline password:")
    await LoginStates.DEADLINE_PASSWORD.set()


async def process_deadline_password(message: types.Message, state: FSMContext):
    """
    Process Deadline password input and save credentials.
    
    Args:
        message: Telegram message containing Deadline password
        state: FSM context for state management
    """
    data = await state.get_data()
    deadline_login = data.get("deadline_login")
    deadline_password = message.text.strip()
    
    if not isinstance(deadline_login, str) or not deadline_login:
        await message.reply("Error: Deadline login not received. Please use /login again.")
        await state.finish()
        return
        
    # Save Deadline credentials
    from app.auth import save_deadline_credentials
    ok = await save_deadline_credentials(message.from_user.id, deadline_login, deadline_password)
    
    if ok:
        await message.reply("Successfully authorized! Welcome to TasksBot.", reply_markup=get_main_keyboard())
    else:
        await message.reply("Error saving credentials. Please try again.")
    
    await state.finish()


async def cmd_logout(message: types.Message):
    """
    Logout the current user.
    
    Args:
        message: Telegram message object
    """
    if not await is_authorized(message.from_user.id):
        await message.reply("You were not logged in.")
        return
    await logout_user(message.from_user.id)
    await message.reply("You have been logged out.")

# ============================================================================
# === BASIC COMMAND HANDLERS ===
# ============================================================================

async def cmd_start(message: types.Message):
    """
    Handle /start command: send welcome message and main keyboard.
    
    Args:
        message: Telegram message object
    """
    await bot.send_message(
        chat_id=message.chat.id,
        text="TasksBot started. Choose an action:",
        reply_markup=get_main_keyboard()
    )


async def clear_chat_handler(message: types.Message):
    """
    Clear chat history by deleting recent messages.
    
    Args:
        message: Telegram message object
    """
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
            continue
            
    keyboard = get_main_keyboard()
    await bot.send_message(chat_id, f"\u200b\nMessages deleted: {deleted_count}", reply_markup=keyboard)

# ============================================================================
# === JOB MANAGEMENT HANDLERS ===
# ============================================================================

@authorized_only
async def handle_jobs(message: types.Message):
    """
    Handle Jobs button press: display list of jobs.
    
    Args:
        message: Telegram message object
    """
    logger.info(f"Jobs button pressed by user {message.from_user.id}")
    
    try:
        jobs = await get_jobs_list(message.from_user.id)
        if not jobs:
            await message.reply("No jobs found or error occurred.")
            return
            
        # Create inline keyboard for jobs
        keyboard = []
        per_page = 5
        page = 0  # Start with first page
        start_idx = page * per_page
        end_idx = start_idx + per_page
        page_jobs = jobs[start_idx:end_idx]
        
        for job in page_jobs:
            job_id = job.get("JobId") or job.get("_id") or job.get("Props", {}).get("JobId")
            batch = job.get("Props", {}).get("Batch", "Без имени")
            stat = job.get("Stat", 0)
            icon = get_job_icon(stat)
            
            button_text = f"{icon} {batch[:20]}"
            keyboard.append([types.InlineKeyboardButton(
                text=button_text,
                callback_data=f"job_info:{job_id}"
            )])
        
        # Add pagination buttons
        total_pages = (len(jobs) + per_page - 1) // per_page
        if total_pages > 1:
            nav_buttons = []
            if page > 0:
                nav_buttons.append(types.InlineKeyboardButton(
                    text="⬅️ Previous",
                    callback_data=f"jobs_page:{page-1}"
                ))
            if page < total_pages - 1:
                nav_buttons.append(types.InlineKeyboardButton(
                    text="Next ➡️",
                    callback_data=f"jobs_page:{page+1}"
                ))
            if nav_buttons:
                keyboard.append(nav_buttons)
        
        reply_markup = types.InlineKeyboardMarkup(inline_keyboard=keyboard)
        await message.reply(f"Jobs (page {page+1}/{total_pages}):", reply_markup=reply_markup)
        
    except Exception as e:
        logger.error(f"Error handling jobs for user {message.from_user.id}: {e}")
        await message.reply("Error occurred while fetching jobs.")


async def job_info_callback(callback_query: types.CallbackQuery):
    """
    Handle job info callback: display detailed job information.
    
    Args:
        callback_query: Callback query object
    """
    try:
        job_id = callback_query.data.split(":")[1]
        user_id = str(callback_query.from_user.id)
        login, password, _ = user_credentials[user_id]
        
        job_info = await get_job_info(login, password, job_id)
        if not job_info:
            await callback_query.answer("Error fetching job info.")
            return
            
        # Create job info message
        batch = job_info.get("Props", {}).get("Batch", "Без имени")
        stat = job_info.get("Stat", 0)
        stat_name = settings.job_status_map.get(stat, "Unknown")
        icon = get_job_icon(stat)
        
        total_tasks = job_info.get("Props", {}).get("Tasks", 0)
        completed_chunks = job_info.get("CompletedChunks", 0)
        progress = format_progress(completed_chunks, total_tasks)
        
        message_text = f"{icon} **Job: {batch}**\n"
        message_text += f"Status: {stat_name}\n"
        message_text += f"Progress: {progress}\n"
        message_text += f"Job ID: {job_id}"
        
        # Create action buttons
        keyboard = []
        
        # Tasks button
        keyboard.append([types.InlineKeyboardButton(
            text="📋 Tasks",
            callback_data=f"tasks_job:{job_id}"
        )])
        
        # Action buttons based on status
        if stat == 1:  # Active
            keyboard.append([
                types.InlineKeyboardButton(text="⏸️ Suspend", callback_data=f"suspend_job:{job_id}"),
                types.InlineKeyboardButton(text="🔄 Requeue", callback_data=f"requeue_job:{job_id}")
            ])
        elif stat == 2:  # Suspended
            keyboard.append([
                types.InlineKeyboardButton(text="▶️ Resume", callback_data=f"resume_job:{job_id}"),
                types.InlineKeyboardButton(text="🔄 Requeue", callback_data=f"requeue_job:{job_id}")
            ])
        elif stat == 3:  # Completed
            keyboard.append([
                types.InlineKeyboardButton(text="📁 Download", callback_data=f"download_job:{job_id}"),
                types.InlineKeyboardButton(text="🎬 Create Video", callback_data=f"create_video:{job_id}")
            ])
        
        # Delete button
        keyboard.append([types.InlineKeyboardButton(
            text="🗑️ Delete",
            callback_data=f"delete_job:{job_id}"
        )])
        
        reply_markup = types.InlineKeyboardMarkup(inline_keyboard=keyboard)
        await callback_query.message.edit_text(message_text, reply_markup=reply_markup, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Error in job_info_callback: {e}")
        await callback_query.answer("Error occurred.")

# ============================================================================
# === WORKER MANAGEMENT HANDLERS ===
# ============================================================================

@authorized_only
async def handle_workers(message: types.Message):
    """
    Handle Workers button press: display list of workers.
    
    Args:
        message: Telegram message object
    """
    logger.info(f"Workers button pressed by user {message.from_user.id}")
    
    try:
        workers = await get_workers_list(message.from_user.id)
        if not workers:
            await message.reply("No workers found or error occurred.")
            return
            
        # Create workers list message
        message_text = "**Workers:**\n\n"
        
        for worker in workers:
            name = worker.get("Props", {}).get("Name", "Unknown")
            stat = worker.get("Stat", 0)
            stat_name = settings.worker_status_map.get(stat, "Unknown")
            icon = get_worker_icon(stat)
            
            message_text += f"{icon} **{name}** - {stat_name}\n"
        
        await message.reply(message_text, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Error handling workers for user {message.from_user.id}: {e}")
        await message.reply("Error occurred while fetching workers.")

# ============================================================================
# === NOTIFICATION HANDLERS ===
# ============================================================================

@authorized_only
async def toggle_notifications_handler(message: types.Message):
    """
    Handle notifications toggle button.
    
    Args:
        message: Telegram message object
    """
    try:
        from app.auth import toggle_notifications
        new_status = await toggle_notifications(message.from_user.id)
        
        status_text = "enabled" if new_status else "disabled"
        await message.reply(f"Notifications {status_text}.")
        
    except Exception as e:
        logger.error(f"Error toggling notifications for user {message.from_user.id}: {e}")
        await message.reply("Error occurred while toggling notifications.")

# ============================================================================
# === HELPER FUNCTIONS ===
# ============================================================================

def get_job_icon(stat: int) -> str:
    """Get icon for job status."""
    if stat == 0:
        return "❓"
    elif stat == 1:
        return "🔄"
    elif stat == 2:
        return "⏸️"
    elif stat == 3:
        return "✅"
    elif stat == 4:
        return "❌"
    elif stat == 6:
        return "⏳"
    else:
        return "❓"


def get_worker_icon(stat: int) -> str:
    """Get icon for worker status."""
    if stat == 0:
        return "❓"
    elif stat == 1:
        return "🔄"
    elif stat == 2:
        return "💤"
    elif stat == 3:
        return "🔴"
    elif stat == 4:
        return "⚠️"
    elif stat == 8:
        return "🚀"
    else:
        return "❓"


def format_progress(completed: int, total: int) -> str:
    """Format progress as percentage string."""
    percentage = int((completed / total) * 100) if total else 0
    return f"{percentage}% ({completed}/{total})"

# ============================================================================
# === HANDLER REGISTRATION ===
# ============================================================================

def register_handlers():
    """Register all handlers with the dispatcher."""
    
    # Authentication handlers
    dp.register_message_handler(cmd_login_start, commands=['login'])
    dp.register_message_handler(process_login_username, state=LoginStates.USERNAME)
    dp.register_message_handler(process_login_password, state=LoginStates.PASSWORD)
    dp.register_message_handler(process_deadline_login, state=LoginStates.DEADLINE_LOGIN)
    dp.register_message_handler(process_deadline_password, state=LoginStates.DEADLINE_PASSWORD)
    dp.register_message_handler(cmd_logout, commands=['logout'])
    
    # Basic command handlers
    dp.register_message_handler(cmd_start, commands=['start'])
    dp.register_message_handler(clear_chat_handler, lambda message: message.text == "🧹 Очистить")
    
    # Job management handlers
    dp.register_message_handler(handle_jobs, lambda message: message.text == "Jobs")
    dp.register_callback_query_handler(job_info_callback, lambda c: c.data and c.data.startswith("job_info:"))
    
    # Worker management handlers
    dp.register_message_handler(handle_workers, lambda message: message.text == "Workers")
    
    # Notification handlers
    dp.register_message_handler(toggle_notifications_handler, lambda message: message.text == "🔔 Уведомления")
    
    logger.info("All handlers registered") 