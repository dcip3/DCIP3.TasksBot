# app/handlers_new.py
"""
Telegram bot command and state handlers for aiogram 3.x.

This module contains all the message handlers, callback handlers, and state
machines for user interactions including authentication, job management,
worker monitoring, and realtime operations.
"""

import logging
import asyncio
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command, StateFilter

from app.utils import authorized_only, get_main_keyboard
from app.core.bot_core import bot, dp
from app.core.config import settings
from app.auth import authenticate_user, logout_user, is_authorized, toggle_notifications, save_deadline_credentials
from app.services import (
    get_jobs_list, get_workers_list, get_job_info, get_job_tasks,
    requeue_job, resume_job, suspend_job, delete_job,
    download_job_folder, create_video_from_job
)

logger = logging.getLogger(__name__)

# Create router
router = Router()

# ============================================================================
# === STATE MACHINES ===
# ============================================================================

class LoginStates(StatesGroup):
    """State machine for user login process"""
    USERNAME = State()
    PASSWORD = State()

# ============================================================================
# === AUTHENTICATION HANDLERS ===
# ============================================================================

@router.message(Command("login"))
async def cmd_login_start(message: Message, state: FSMContext):
    """
    Start the login process by requesting Deadline login.
    """
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
async def process_login_username(message: Message, state: FSMContext):
    """
    Process Deadline login input and request password.
    """
    if message.text is None:
        await message.answer("Please enter a valid Deadline login.")
        return
    await state.update_data(username=message.text.strip())
    await message.answer("Enter your Deadline password:")
    await state.set_state(LoginStates.PASSWORD)

@router.message(StateFilter(LoginStates.PASSWORD))
async def process_login_password(message: Message, state: FSMContext):
    """
    Process Deadline password input and authenticate user via Deadline RCS.
    """
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
        # Save Deadline credentials
        await save_deadline_credentials(message.from_user.id, username, password)
        await message.answer("Successfully authorized! Welcome to TasksBot.", reply_markup=get_main_keyboard())
    else:
        await message.answer("Invalid Deadline login or password.")
    await state.clear()


@router.message(Command("logout"))
async def cmd_logout(message: Message):
    """
    Logout the current user.
    
    Args:
        message: Telegram message object
    """
    # Stop realtime if it's running
    stop_realtime_for_chat(message.chat.id)
    
    if message.from_user is None:
        await message.answer("Error: User information not available.")
        return
        
    if not await is_authorized(message.from_user.id):
        await message.answer("You were not logged in.")
        return
    await logout_user(message.from_user.id)
    await message.answer("You have been logged out.")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """
    Cancel the current operation and clear state.
    
    Args:
        message: Telegram message object
        state: FSM context for state management
    """
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("No active operation to cancel.")
        return
        
    await state.clear()
    await message.answer("Operation cancelled. You can start over with /login or /start.")

# ============================================================================
# === BASIC COMMAND HANDLERS ===
# ============================================================================

@router.message(Command("start"))
async def cmd_start(message: Message):
    """
    Handle /start command: send welcome message and main keyboard.
    
    Args:
        message: Telegram message object
    """
    await message.answer(
        text="TasksBot started. Choose an action:",
        reply_markup=get_main_keyboard()
    )


@router.message(Command("setup_menu"))
async def cmd_setup_menu(message: Message):
    """
    Setup the menu button for Mini App.
    
    Args:
        message: Telegram message object
    """
    from app.utils import setup_menu_button
    try:
        await message.answer("🔄 Setting up menu button...")
        await setup_menu_button()
        await message.answer("✅ Menu button setup successfully! You should now see the 'Tasks' button in the chat menu.")
    except Exception as e:
        await message.answer(f"❌ Failed to setup menu button: {e}")
        logger.error(f"Setup menu button error: {e}")


@router.message(Command("menu_status"))
async def cmd_menu_status(message: Message):
    """
    Check the current menu button status.
    
    Args:
        message: Telegram message object
    """
    try:
        # Попробуем получить текущую кнопку меню
        current_button = await bot.get_chat_menu_button()
        await message.answer(f"📋 Current menu button: {current_button}")
    except Exception as e:
        await message.answer(f"❌ Failed to get menu button status: {e}")
        logger.error(f"Get menu button status error: {e}")


@router.message(F.text == "🧹 Очистить")
async def clear_chat_handler(message: Message):
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
    await message.answer(f"\u200b\nMessages deleted: {deleted_count}", reply_markup=keyboard)

# ============================================================================
# === JOB MANAGEMENT HANDLERS ===
# ============================================================================

@router.message(F.text == "Jobs")
@authorized_only
async def handle_jobs(message: Message, page: int = 0):
    """
    Handle Jobs button press: display list of jobs with pagination.
    
    Args:
        message: Telegram message object
        page: Page number (default 0)
    """
    # Stop realtime if it's running
    stop_realtime_for_chat(message.chat.id)
    
    if message.from_user is None:
        await message.answer("Error: User information not available.")
        return
        
    logger.info(f"Jobs button pressed by user {message.from_user.id}")
    
    try:
        jobs = await get_jobs_list(message.from_user.id)
        if not jobs:
            await message.answer("No jobs found. This could mean:\n• There are no active jobs in Deadline\n• Your user doesn't have access to jobs\n• There was an API error (check logs)")
            return
            
        # Group jobs by batch name (like in old version)
        from collections import defaultdict
        grouped_jobs = defaultdict(list)
        for job in jobs:
            batch = job.get("Props", {}).get("Batch", "Без имени")
            grouped_jobs[batch].append(job)
        
        # Combine jobs by batch (like in old version)
        from datetime import datetime
        combined_jobs = []
        for batch, batch_jobs in grouped_jobs.items():
            total_tasks = sum(j.get("Props", {}).get("Tasks", 0) for j in batch_jobs)
            completed_chunks = sum(j.get("CompletedChunks", 0) for j in batch_jobs)
            
            # Determine batch-level status with priority: Active > Pending > Suspended > Failed > Completed > Unknown
            status_list = [j.get("Stat", 0) for j in batch_jobs]
            if 1 in status_list:
                batch_stat = 1      # Active
            elif 6 in status_list:
                batch_stat = 6      # Pending
            elif 2 in status_list:
                batch_stat = 2      # Suspended
            elif 4 in status_list:
                batch_stat = 4      # Failed
            elif all(s == 3 for s in status_list):
                batch_stat = 3      # Completed
            else:
                batch_stat = 0      # Unknown
                
            # Determine the most recent Date among jobs in this batch
            dates = []
            for j in batch_jobs:
                date_str = j.get("Date")
                if date_str:
                    try:
                        dates.append(datetime.fromisoformat(date_str))
                    except Exception:
                        pass
            max_date = max(dates) if dates else datetime.min
            
            combined_jobs.append({
                "_id": batch_jobs[0].get("_id"),
                "Props": {"Batch": batch, "Tasks": total_tasks},
                "CompletedChunks": completed_chunks,
                "Stat": batch_stat,
                "DateParsed": max_date
            })
        
        # Sort by DateParsed descending (newest first)
        combined_jobs.sort(key=lambda j: j["DateParsed"], reverse=True)
        
        # Pagination: 4 jobs per page
        jobs_slice = combined_jobs[page*4 : page*4+4]
        normal_jobs = [job for job in jobs_slice if job.get("Stat", 0) != 2]
        suspended_jobs = [job for job in jobs_slice if job.get("Stat", 0) == 2]
        
        # Format message in old style with HTML pre tags
        messages = []
        buttons = []
        
        for job in normal_jobs:
            props = job.get("Props", {})
            batch = props.get("Batch", "Без имени")
            total_tasks = props.get("Tasks", 0)
            completed_chunks = job.get("CompletedChunks", 0)
            progress_str = format_progress_old(completed_chunks, total_tasks)
            stat = job.get("Stat", 0)
            
            if stat == 3:
                icon = "✅"
            else:
                icon = "▶️"
                
            messages.append(f"{icon} {batch:<22} {progress_str:^16}\n{'-'*40}")
            
            # Add button for this job
            job_id = job.get("_id")
            if job_id:
                buttons.append(InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}"))
        
        # Add suspended jobs section
        if suspended_jobs:
            messages.append("")
            title = " suspended "
            line_length = 40
            dashes_each_side = (line_length - len(title)) // 2
            separator = "-" * dashes_each_side + title + "-" * (line_length - dashes_each_side - len(title))
            messages.append(separator)
            
            for job in suspended_jobs:
                props = job.get("Props", {})
                batch = props.get("Batch", "Без имени")
                total_tasks = props.get("Tasks", 0)
                completed_chunks = job.get("CompletedChunks", 0)
                progress_str = format_progress_old(completed_chunks, total_tasks)
                
                messages.append(f"⏸️ {batch:<22} {progress_str:^16}\n{'-'*40}")
                
                # Add button for this suspended job
                job_id = job.get("_id")
                if job_id:
                    buttons.append(InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}"))
        
        header = f"{'Batch':<24} {'Progress':^16}"
        header += f"\n{'-'*40}"
        batch_text = "\n".join(messages) if messages else "Нет данных"
        
        # Create inline keyboard
        if buttons:
            inline_keyboard = []
            row = []
            for i, button in enumerate(buttons, 1):
                row.append(button)
                if i % 2 == 0:
                    inline_keyboard.append(row)
                    row = []
            if row:
                inline_keyboard.append(row)
            
            # Navigation buttons
            total_items = len(combined_jobs)
            total_pages = (total_items + 3) // 4
            nav_buttons = []
            if page > 0:
                nav_buttons.append(InlineKeyboardButton(text="⬅ Назад", callback_data=f"jobs_page:{page-1}"))
            if (page + 1) < total_pages:
                nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡", callback_data=f"jobs_page:{page+1}"))
            if nav_buttons:
                inline_keyboard.append(nav_buttons)
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
            page_info = f"Страница {page+1} из {total_pages}"
            
            await message.answer(
                f"<pre>{header}\n{batch_text}\n{page_info}</pre>\n\nВыберите задачу для подробной информации:",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        else:
            await message.answer(f"<pre>{header}\n{batch_text}</pre>", parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Error handling jobs for user {message.from_user.id}: {e}")
        await message.answer("Error occurred while fetching jobs.")

# ============================================================================
# === WORKER MANAGEMENT HANDLERS ===
# ============================================================================

@router.message(F.text == "Workers")
@authorized_only
async def handle_workers(message: Message):
    """
    Handle Workers button press: display list of workers.
    
    Args:
        message: Telegram message object
    """
    # Stop realtime if it's running
    stop_realtime_for_chat(message.chat.id)
    
    if message.from_user is None:
        await message.answer("Error: User information not available.")
        return
        
    logger.info(f"Workers button pressed by user {message.from_user.id}")
    
    try:
        workers = await get_workers_list(message.from_user.id)
        if not workers:
            await message.answer("No workers (slaves) found. This could mean:\n• There are no active workers in Deadline\n• Your user doesn't have access to workers\n• There was an API error (check logs)")
            return
            
        # Format message in old style with HTML pre tags
        messages = []
        for worker in workers:
            info = worker.get("Info", {})
            name = info.get("Name", "Unknown")
            stat_num = info.get("Stat", 0)
            stat = settings.worker_status_map.get(stat_num, f"Unknown ({stat_num})")
            messages.append(f"{name:<24} {stat}")
        
        header = f"{'Name':<24} Status"
        header += f"\n{'-'*40}"
        body = "\n".join(messages) if messages else "Нет данных"
        
        await message.answer(f"<pre>{header}\n{body}</pre>", parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Error handling workers for user {message.from_user.id}: {e}")
        await message.answer("Error occurred while fetching workers.")

# ============================================================================
# === NOTIFICATION HANDLERS ===
# ============================================================================

@router.message(F.text == "🔔 Уведомления")
@authorized_only
async def toggle_notifications_handler(message: Message):
    """
    Handle notifications toggle button.
    
    Args:
        message: Telegram message object
    """
    # Stop realtime if it's running
    stop_realtime_for_chat(message.chat.id)
    
    if message.from_user is None:
        await message.answer("Error: User information not available.")
        return
        
    try:
        new_status = await toggle_notifications(message.from_user.id)
        
        status_text = "enabled" if new_status else "disabled"
        await message.answer(f"Notifications {status_text}.")
        
    except Exception as e:
        logger.error(f"Error toggling notifications for user {message.from_user.id}: {e}")
        await message.answer("Error occurred while toggling notifications.")




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


def format_progress_old(completed: int, total: int) -> str:
    """Format progress in old style (like in an earlier bot)."""
    return f"{int((completed / total) * 100) if total else 0}% {completed}/{total}"


def stop_realtime_for_chat(chat_id: int):
    """Stop realtime updates for a specific chat."""
    if not hasattr(handle_realtime, "active_realtime_tasks"):
        return
    active_realtime_tasks = handle_realtime.active_realtime_tasks
    if chat_id in active_realtime_tasks:
        active_realtime_tasks[chat_id].cancel()
        del active_realtime_tasks[chat_id]


# ============================================================================
# === CALLBACK HANDLERS ===
# ============================================================================

@router.callback_query(lambda c: c.data and c.data.startswith("jobs_page:"))
async def jobs_page_callback(callback_query: CallbackQuery):
    """Handle jobs pagination."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return
        
    page_str = callback_query.data.split(":", 1)[1]
    try:
        page = int(page_str)
    except ValueError:
        await callback_query.answer("Неверный номер страницы.", show_alert=True)
        return
    
    await callback_query.answer()
    
    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return
        
    if callback_query.message is None:
        await callback_query.answer("Error: Message not available.", show_alert=True)
        return
    
    try:
        jobs = await get_jobs_list(callback_query.from_user.id)
        if not jobs:
            await callback_query.message.edit_text("No jobs found.")
            return
            
        # Group jobs by batch name (like in old version)
        from collections import defaultdict
        grouped_jobs = defaultdict(list)
        for job in jobs:
            batch = job.get("Props", {}).get("Batch", "Без имени")
            grouped_jobs[batch].append(job)
        
        # Combine jobs by batch (like in old version)
        from datetime import datetime
        combined_jobs = []
        for batch, batch_jobs in grouped_jobs.items():
            total_tasks = sum(j.get("Props", {}).get("Tasks", 0) for j in batch_jobs)
            completed_chunks = sum(j.get("CompletedChunks", 0) for j in batch_jobs)
            
            # Determine batch-level status with priority: Active > Pending > Suspended > Failed > Completed > Unknown
            status_list = [j.get("Stat", 0) for j in batch_jobs]
            if 1 in status_list:
                batch_stat = 1      # Active
            elif 6 in status_list:
                batch_stat = 6      # Pending
            elif 2 in status_list:
                batch_stat = 2      # Suspended
            elif 4 in status_list:
                batch_stat = 4      # Failed
            elif all(s == 3 for s in status_list):
                batch_stat = 3      # Completed
            else:
                batch_stat = 0      # Unknown
                
            # Determine the most recent Date among jobs in this batch
            dates = []
            for j in batch_jobs:
                date_str = j.get("Date")
                if date_str:
                    try:
                        dates.append(datetime.fromisoformat(date_str))
                    except Exception:
                        pass
            max_date = max(dates) if dates else datetime.min
            
            combined_jobs.append({
                "_id": batch_jobs[0].get("_id"),
                "Props": {"Batch": batch, "Tasks": total_tasks},
                "CompletedChunks": completed_chunks,
                "Stat": batch_stat,
                "DateParsed": max_date
            })
        
        # Sort by DateParsed descending (newest first)
        combined_jobs.sort(key=lambda j: j["DateParsed"], reverse=True)
        
        # Pagination: 4 jobs per page
        jobs_slice = combined_jobs[page*4 : page*4+4]
        normal_jobs = [job for job in jobs_slice if job.get("Stat", 0) != 2]
        suspended_jobs = [job for job in jobs_slice if job.get("Stat", 0) == 2]
        
        # Format message in old style with HTML pre tags
        messages = []
        buttons = []
        
        for job in normal_jobs:
            props = job.get("Props", {})
            batch = props.get("Batch", "Без имени")
            total_tasks = props.get("Tasks", 0)
            completed_chunks = job.get("CompletedChunks", 0)
            progress_str = format_progress_old(completed_chunks, total_tasks)
            stat = job.get("Stat", 0)
            
            if stat == 3:
                icon = "✅"
            else:
                icon = "▶️"
                
            messages.append(f"{icon} {batch:<22} {progress_str:^16}\n{'-'*40}")
            
            # Add button for this job
            job_id = job.get("_id")
            if job_id:
                buttons.append(InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}"))
        
        # Add suspended jobs section
        if suspended_jobs:
            messages.append("")
            title = " suspended "
            line_length = 40
            dashes_each_side = (line_length - len(title)) // 2
            separator = "-" * dashes_each_side + title + "-" * (line_length - dashes_each_side - len(title))
            messages.append(separator)
            
            for job in suspended_jobs:
                props = job.get("Props", {})
                batch = props.get("Batch", "Без имени")
                total_tasks = props.get("Tasks", 0)
                completed_chunks = job.get("CompletedChunks", 0)
                progress_str = format_progress_old(completed_chunks, total_tasks)
                
                messages.append(f"⏸️ {batch:<22} {progress_str:^16}\n{'-'*40}")
                
                # Add button for this suspended job
                job_id = job.get("_id")
                if job_id:
                    buttons.append(InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}"))
        
        header = f"{'Batch':<24} {'Progress':^16}"
        header += f"\n{'-'*40}"
        batch_text = "\n".join(messages) if messages else "Нет данных"
        
        # Create inline keyboard
        if buttons:
            inline_keyboard = []
            row = []
            for i, button in enumerate(buttons, 1):
                row.append(button)
                if i % 2 == 0:
                    inline_keyboard.append(row)
                    row = []
            if row:
                inline_keyboard.append(row)
            
            # Navigation buttons
            total_items = len(combined_jobs)
            total_pages = (total_items + 3) // 4
            nav_buttons = []
            if page > 0:
                nav_buttons.append(InlineKeyboardButton(text="⬅ Назад", callback_data=f"jobs_page:{page-1}"))
            if (page + 1) < total_pages:
                nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡", callback_data=f"jobs_page:{page+1}"))
            if nav_buttons:
                inline_keyboard.append(nav_buttons)
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
            page_info = f"Страница {page+1} из {total_pages}"
            
            await callback_query.message.edit_text(
                f"<pre>{header}\n{batch_text}\n{page_info}</pre>\n\nВыберите задачу для подробной информации:",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        else:
            await callback_query.message.edit_text(f"<pre>{header}\n{batch_text}</pre>", parse_mode="HTML")
            
    except Exception as e:
        logger.error(f"Error handling jobs page for user {callback_query.from_user.id}: {e}")
        await callback_query.message.edit_text("Error occurred while fetching jobs.")


@router.callback_query(lambda c: c.data and c.data.startswith("job_info:"))
async def job_info_callback(callback_query: CallbackQuery):
    """Handle job info button press."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return
        
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return
    
    job_id = callback_query.data.split(":", 1)[1]
    
    try:
        job_info = await get_job_info(callback_query.from_user.id, job_id)
        if not job_info:
            await callback_query.answer("Job not found.", show_alert=True)
            return
        
        # Format job info in old style
        props = job_info.get("Props", {})
        batch = props.get("Batch", "Без имени")
        total_tasks = props.get("Tasks", 0)
        completed_chunks = job_info.get("CompletedChunks", 0)
        progress_str = format_progress_old(completed_chunks, total_tasks)
        full_name = props.get("Name", "Без имени")
        name = full_name.split("/")[-1] if "/" in full_name else full_name
        stat = job_info.get("Stat", 0)
        stat_name = settings.job_status_map.get(stat, "Unknown")
        
        # Create job info message
        info_text = f"<pre>Job Info:\n{'-'*40}\n"
        info_text += f"Batch: {batch}\n"
        info_text += f"Name: {name}\n"
        info_text += f"Status: {stat_name}\n"
        info_text += f"Progress: {progress_str}\n"
        info_text += f"Total Tasks: {total_tasks}\n"
        info_text += f"Completed: {completed_chunks}\n"
        info_text += f"{'-'*40}</pre>"
        
        # Create action buttons
        buttons = []
        if stat != 3:  # Not completed
            if stat == 2:  # Suspended
                buttons.append(InlineKeyboardButton(text="▶ Resume", callback_data=f"resume_job:{job_id}"))
            else:
                buttons.append(InlineKeyboardButton(text="⏸ Suspend", callback_data=f"suspend_job:{job_id}"))
            buttons.append(InlineKeyboardButton(text="🔄 Requeue", callback_data=f"requeue_job:{job_id}"))
        
        buttons.append(InlineKeyboardButton(text="🔍 Preview", callback_data=f"preview_job:{job_id}"))
        buttons.append(InlineKeyboardButton(text="🗑 Delete", callback_data=f"delete_job:{job_id}"))
        buttons.append(InlineKeyboardButton(text="📋 Tasks", callback_data=f"tasks_job:{job_id}"))
        buttons.append(InlineKeyboardButton(text="⬅ Back", callback_data="jobs_back"))
        
        # Arrange buttons in rows
        inline_keyboard = []
        row = []
        for i, button in enumerate(buttons, 1):
            row.append(button)
            if i % 2 == 0:
                inline_keyboard.append(row)
                row = []
        if row:
            inline_keyboard.append(row)
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
        
        await callback_query.message.edit_text(
            info_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        
    except Exception as e:
        logger.error(f"Error handling job info for user {callback_query.from_user.id}: {e}")
        await callback_query.answer("Error occurred while fetching job info.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("preview_job:"))
async def preview_job_callback(callback_query: CallbackQuery):
    """Handle preview job button press."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return
        
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return
    
    job_id = callback_query.data.split(":", 1)[1]
    
    try:
        # Get user credentials
        from app.auth import get_deadline_credentials
        credentials = await get_deadline_credentials(callback_query.from_user.id)
        if not credentials:
            await callback_query.answer("No credentials found. Please login again.", show_alert=True)
            return
        
        login, password = credentials
        
        # Send initial message
        progress_msg = await callback_query.message.answer("🔍 Starting preview generation...")
        
        try:
            # Step 1: Download files
            await progress_msg.edit_text("📥 Step 1: Downloading files from Dropbox...")
            local_path = await download_job_folder(login, password, job_id)
            
            if not local_path:
                await progress_msg.edit_text("❌ Failed to download job files")
                return
            
            # Step 2: Create video
            await progress_msg.edit_text("🎬 Step 2: Converting EXR files and creating video...")
            video_path = await create_video_from_job(login, password, job_id)
            
            if not video_path:
                await progress_msg.edit_text("❌ Failed to create video")
                return
            
            # Step 3: Send video
            await progress_msg.edit_text("📤 Step 3: Sending video...")
            from aiogram.types import FSInputFile
            await callback_query.message.answer_document(
                document=FSInputFile(video_path),
                caption="🎬 Preview video generated successfully!"
            )
            
            await progress_msg.delete()
            
        except Exception as e:
            logger.error(f"Error in preview generation: {e}")
            await progress_msg.edit_text(f"❌ Error during preview generation: {str(e)}")
            
    except Exception as e:
        logger.error(f"Error handling preview for user {callback_query.from_user.id}: {e}")
        await callback_query.answer("Error occurred while generating preview.", show_alert=True)


@router.callback_query(lambda c: c.data == "jobs_back")
async def jobs_back_callback(callback_query: CallbackQuery):
    """Handle back button from job info."""
    await callback_query.answer()
    # Send new jobs message instead of trying to reuse handle_jobs
    if callback_query.from_user:
        try:
            jobs = await get_jobs_list(callback_query.from_user.id)
            if not jobs:
                await callback_query.message.edit_text("No jobs found.")
                return
                
            # Group jobs by batch name (like in old version)
            from collections import defaultdict
            grouped_jobs = defaultdict(list)
            for job in jobs:
                batch = job.get("Props", {}).get("Batch", "Без имени")
                grouped_jobs[batch].append(job)
            
            # Combine jobs by batch (like in old version)
            from datetime import datetime
            combined_jobs = []
            for batch, batch_jobs in grouped_jobs.items():
                total_tasks = sum(j.get("Props", {}).get("Tasks", 0) for j in batch_jobs)
                completed_chunks = sum(j.get("CompletedChunks", 0) for j in batch_jobs)
                
                # Determine batch-level status with priority: Active > Pending > Suspended > Failed > Completed > Unknown
                status_list = [j.get("Stat", 0) for j in batch_jobs]
                if 1 in status_list:
                    batch_stat = 1      # Active
                elif 6 in status_list:
                    batch_stat = 6      # Pending
                elif 2 in status_list:
                    batch_stat = 2      # Suspended
                elif 4 in status_list:
                    batch_stat = 4      # Failed
                elif all(s == 3 for s in status_list):
                    batch_stat = 3      # Completed
                else:
                    batch_stat = 0      # Unknown
                    
                # Determine the most recent Date among jobs in this batch
                dates = []
                for j in batch_jobs:
                    date_str = j.get("Date")
                    if date_str:
                        try:
                            dates.append(datetime.fromisoformat(date_str))
                        except Exception:
                            pass
                max_date = max(dates) if dates else datetime.min
                
                combined_jobs.append({
                    "_id": batch_jobs[0].get("_id"),
                    "Props": {"Batch": batch, "Tasks": total_tasks},
                    "CompletedChunks": completed_chunks,
                    "Stat": batch_stat,
                    "DateParsed": max_date
                })
            
            # Sort by DateParsed descending (newest first)
            combined_jobs.sort(key=lambda j: j["DateParsed"], reverse=True)
            
            # Pagination: 4 jobs per page
            jobs_slice = combined_jobs[0*4 : 0*4+4]
            normal_jobs = [job for job in jobs_slice if job.get("Stat", 0) != 2]
            suspended_jobs = [job for job in jobs_slice if job.get("Stat", 0) == 2]
            
            # Format message in old style with HTML pre tags
            messages = []
            buttons = []
            
            for job in normal_jobs:
                props = job.get("Props", {})
                batch = props.get("Batch", "Без имени")
                total_tasks = props.get("Tasks", 0)
                completed_chunks = job.get("CompletedChunks", 0)
                progress_str = format_progress_old(completed_chunks, total_tasks)
                stat = job.get("Stat", 0)
                
                if stat == 3:
                    icon = "✅"
                else:
                    icon = "▶️"
                    
                messages.append(f"{icon} {batch:<22} {progress_str:^16}\n{'-'*40}")
                
                # Add button for this job
                job_id = job.get("_id")
                if job_id:
                    buttons.append(InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}"))
            
            # Add suspended jobs section
            if suspended_jobs:
                messages.append("")
                title = " suspended "
                line_length = 40
                dashes_each_side = (line_length - len(title)) // 2
                separator = "-" * dashes_each_side + title + "-" * (line_length - dashes_each_side - len(title))
                messages.append(separator)
                
                for job in suspended_jobs:
                    props = job.get("Props", {})
                    batch = props.get("Batch", "Без имени")
                    total_tasks = props.get("Tasks", 0)
                    completed_chunks = job.get("CompletedChunks", 0)
                    progress_str = format_progress_old(completed_chunks, total_tasks)
                    
                    messages.append(f"⏸️ {batch:<22} {progress_str:^16}\n{'-'*40}")
                    
                    # Add button for this suspended job
                    job_id = job.get("_id")
                    if job_id:
                        buttons.append(InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}"))
            
            header = f"{'Batch':<24} {'Progress':^16}"
            header += f"\n{'-'*40}"
            batch_text = "\n".join(messages) if messages else "Нет данных"
            
            # Create inline keyboard
            if buttons:
                inline_keyboard = []
                row = []
                for i, button in enumerate(buttons, 1):
                    row.append(button)
                    if i % 2 == 0:
                        inline_keyboard.append(row)
                        row = []
                if row:
                    inline_keyboard.append(row)
                
                # Navigation buttons
                total_items = len(combined_jobs)
                total_pages = (total_items + 3) // 4
                nav_buttons = []
                if 0 > 0:
                    nav_buttons.append(InlineKeyboardButton(text="⬅ Назад", callback_data=f"jobs_page:{0-1}"))
                if (0 + 1) < total_pages:
                    nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡", callback_data=f"jobs_page:{0+1}"))
                if nav_buttons:
                    inline_keyboard.append(nav_buttons)
                
                keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
                page_info = f"Страница {0+1} из {total_pages}"
                
                await callback_query.message.edit_text(
                    f"<pre>{header}\n{batch_text}\n{page_info}</pre>\n\nВыберите задачу для подробной информации:",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            else:
                await callback_query.message.edit_text(f"<pre>{header}\n{batch_text}</pre>", parse_mode="HTML")
                
        except Exception as e:
            logger.error(f"Error handling jobs back for user {callback_query.from_user.id}: {e}")
            await callback_query.message.edit_text("Error occurred while fetching jobs.")

@router.message(F.text == "Realtime")
@authorized_only
async def handle_realtime(message: Message):
    """
    Handle Realtime button: show jobs list with auto-refresh every 5 seconds.
    """
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Глобальный словарь для хранения задач
    if not hasattr(handle_realtime, "active_realtime_tasks"):
        handle_realtime.active_realtime_tasks = {}
    active_realtime_tasks = handle_realtime.active_realtime_tasks
    
    # Остановить предыдущий realtime, если есть
    if chat_id in active_realtime_tasks:
        active_realtime_tasks[chat_id].cancel()
        del active_realtime_tasks[chat_id]

    await message.answer("Задачи будут обновляться каждые 5 секунд до следующего сообщения.")

    async def realtime_loop():
        from collections import defaultdict
        from datetime import datetime
        try:
            msg = await message.answer("Загрузка...")
            last_text = None
            while True:
                jobs = await get_jobs_list(user_id)
                # Группировка и форматирование как в handle_jobs
                grouped_jobs = defaultdict(list)
                for job in jobs:
                    batch = job.get("Props", {}).get("Batch", "Без имени")
                    grouped_jobs[batch].append(job)
                combined_jobs = []
                for batch, batch_jobs in grouped_jobs.items():
                    total_tasks = sum(j.get("Props", {}).get("Tasks", 0) for j in batch_jobs)
                    completed_chunks = sum(j.get("CompletedChunks", 0) for j in batch_jobs)
                    status_list = [j.get("Stat", 0) for j in batch_jobs]
                    if 1 in status_list:
                        batch_stat = 1
                    elif 6 in status_list:
                        batch_stat = 6
                    elif 2 in status_list:
                        batch_stat = 2
                    elif 4 in status_list:
                        batch_stat = 4
                    elif all(s == 3 for s in status_list):
                        batch_stat = 3
                    else:
                        batch_stat = 0
                    dates = []
                    for j in batch_jobs:
                        date_str = j.get("Date")
                        if date_str:
                            try:
                                dates.append(datetime.fromisoformat(date_str))
                            except Exception:
                                pass
                    max_date = max(dates) if dates else datetime.min
                    combined_jobs.append({
                        "Props": {"Batch": batch, "Tasks": total_tasks},
                        "CompletedChunks": completed_chunks,
                        "Stat": batch_stat,
                        "DateParsed": max_date
                    })
                combined_jobs.sort(key=lambda j: j["DateParsed"], reverse=True)
                normal_jobs = [job for job in combined_jobs if job.get("Stat", 0) != 2]
                suspended_jobs = [job for job in combined_jobs if job.get("Stat", 0) == 2]
                messages = []
                for job in normal_jobs:
                    props = job.get("Props", {})
                    batch = props.get("Batch", "Без имени")
                    total_tasks = props.get("Tasks", 0)
                    completed_chunks = job.get("CompletedChunks", 0)
                    progress_str = format_progress_old(completed_chunks, total_tasks)
                    stat = job.get("Stat", 0)
                    icon = "✅" if stat == 3 else "▶️"
                    messages.append(f"{icon} {batch:<22} {progress_str:^16}\n{'-'*40}")
                if suspended_jobs:
                    messages.append("")
                    title = " suspended "
                    line_length = 40
                    dashes_each_side = (line_length - len(title)) // 2
                    separator = "-" * dashes_each_side + title + "-" * (line_length - dashes_each_side - len(title))
                    messages.append(separator)
                    for job in suspended_jobs:
                        props = job.get("Props", {})
                        batch = props.get("Batch", "Без имени")
                        total_tasks = props.get("Tasks", 0)
                        completed_chunks = job.get("CompletedChunks", 0)
                        progress_str = format_progress_old(completed_chunks, total_tasks)
                        messages.append(f"⏸️ {batch:<22} {progress_str:^16}\n{'-'*40}")
                header = f"{'Batch':<24} {'Progress':^16}"
                header += f"\n{'-'*40}"
                batch_text = "\n".join(messages) if messages else "Нет данных"
                new_text = f"<pre>{header}\n{batch_text}</pre>"
                if new_text != last_text:
                    await msg.edit_text(new_text, parse_mode="HTML")
                    last_text = new_text
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await message.answer(f"Ошибка: {e}")
    task = asyncio.create_task(realtime_loop())
    active_realtime_tasks[chat_id] = task

# ============================================================================
# === HANDLER REGISTRATION ===
# ============================================================================

def register_handlers():
    """Register all handlers with the dispatcher."""
    dp.include_router(router)
    logger.info("All handlers registered")


@router.message()
async def handle_unknown_message(message: Message, state: FSMContext):
    """
    Handle any message that doesn't match other handlers.
    
    Args:
        message: Telegram message object
        state: FSM context for state management
    """
    # Stop realtime if it's running
    stop_realtime_for_chat(message.chat.id)
    
    current_state = await state.get_state()
    
    # If we're in a login state, provide context-specific help
    if current_state in [LoginStates.USERNAME, LoginStates.PASSWORD]:
        if current_state == LoginStates.USERNAME:
            await message.answer("Please enter your Deadline login, or use /cancel to stop the login process.")
        elif current_state == LoginStates.PASSWORD:
            await message.answer("Please enter your Deadline password, or use /cancel to stop the login process.")
    else:
        # Default response for unknown commands
        await message.answer(
            "I don't understand this command. Please use the buttons below or type /start to see available options.",
            reply_markup=get_main_keyboard()
        ) 