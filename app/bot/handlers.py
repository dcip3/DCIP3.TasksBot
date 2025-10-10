# app/bot/handlers.py
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

from app.core.utils import authorized_only, get_main_keyboard
from app.core.bot_core import bot, dp
from app.core.config import settings
from app.auth import authenticate_user, logout_user, is_authorized, toggle_notifications, save_deadline_credentials
from app.services import (
    get_jobs_list, get_workers_list, get_job_info_by_user_id, get_job_tasks_by_user_id,
    requeue_job_by_user_id, resume_job_by_user_id, suspend_job_by_user_id, delete_job_by_user_id,
    download_job_folder, create_video_from_job, check_video_exists_in_dropbox, download_video_from_dropbox
)
from app.integrations.dropbox_helpers import download_exr_folder, fetch_dropbox_metadata, upload_video_to_dropbox
from app.core.bot_core import download_states, stop_downloads
import json
from app.integrations.video_helpers import assemble_video_from_jpg
from pathlib import Path

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
    from app.core.utils import setup_menu_button
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
        # Attempt to fetch the current menu button
        current_button = await bot.get_chat_menu_button()
        await message.answer(f"📋 Current menu button: {current_button}")
    except Exception as e:
        await message.answer(f"❌ Failed to get menu button status: {e}")
        logger.error(f"Get menu button status error: {e}")


@router.message(F.text == "🧹 Clear")
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
            batch = job.get("Props", {}).get("Batch", "Untitled")
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
            batch = props.get("Batch", "Untitled")
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
                batch = props.get("Batch", "Untitled")
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
        batch_text = "\n".join(messages) if messages else "No data"
        
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
                nav_buttons.append(InlineKeyboardButton(text="⬅ Back", callback_data=f"jobs_page:{page-1}"))
            if (page + 1) < total_pages:
                nav_buttons.append(InlineKeyboardButton(text="Next ➡", callback_data=f"jobs_page:{page+1}"))
            if nav_buttons:
                inline_keyboard.append(nav_buttons)
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
            page_info = f"Page {page+1} of {total_pages}"
            
            await message.answer(
                f"<pre>{header}\n{batch_text}\n{page_info}</pre>\n\nSelect a job for details:",
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
        body = "\n".join(messages) if messages else "No data"
        
        await message.answer(f"<pre>{header}\n{body}</pre>", parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Error handling workers for user {message.from_user.id}: {e}")
        await message.answer("Error occurred while fetching workers.")

# ============================================================================
# === NOTIFICATION HANDLERS ===
# ============================================================================

@router.message(F.text == "🔔 Notifications")
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
        await callback_query.answer("Invalid page number.", show_alert=True)
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
            batch = job.get("Props", {}).get("Batch", "Untitled")
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
            batch = props.get("Batch", "Untitled")
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
                batch = props.get("Batch", "Untitled")
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
        batch_text = "\n".join(messages) if messages else "No data"
        
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
                nav_buttons.append(InlineKeyboardButton(text="⬅ Back", callback_data=f"jobs_page:{page-1}"))
            if (page + 1) < total_pages:
                nav_buttons.append(InlineKeyboardButton(text="Next ➡", callback_data=f"jobs_page:{page+1}"))
            if nav_buttons:
                inline_keyboard.append(nav_buttons)
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
            page_info = f"Page {page+1} of {total_pages}"
            
            await callback_query.message.edit_text(
                f"<pre>{header}\n{batch_text}\n{page_info}</pre>\n\nSelect a job for details:",
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
        # Get all jobs first
        from app.services import get_jobs_list
        all_jobs = await get_jobs_list(callback_query.from_user.id)
        if not all_jobs:
            await callback_query.answer("Failed to get jobs list.", show_alert=True)
            return

        # Find the selected job and get its batch name
        selected_job = next((j for j in all_jobs if j.get("_id") == job_id), None)
        if not selected_job:
            await callback_query.answer("Job not found.", show_alert=True)
            return

        batch_name = selected_job.get("Props", {}).get("Batch")
        if not batch_name:
            await callback_query.answer("Invalid job data.", show_alert=True)
            return

        # Find all jobs with the same batch name
        batch_jobs = [j for j in all_jobs if j.get("Props", {}).get("Batch") == batch_name]
        
        # Delete the original message
        await callback_query.message.delete()
        
        # Send info for each job in the batch
        for job in batch_jobs:
            props = job.get("Props", {})
            total_tasks = props.get("Tasks", 0)
            completed_chunks = job.get("CompletedChunks", 0)
            progress_str = format_progress_old(completed_chunks, total_tasks)
            full_name = props.get("Name", "Untitled")
            name = full_name.split("/")[-1] if "/" in full_name else full_name
            stat = job.get("Stat", 0)
            stat_name = settings.job_status_map.get(stat, "Unknown")
            
            # Calculate ETA for this job
            eta_str = "N/A"
            try:
                tasks = await get_job_tasks_by_user_id(callback_query.from_user.id, job.get("_id"))
                if tasks:
                    from datetime import datetime, timedelta
                    # Calculate durations of completed tasks
                    durations = []
                    for task in tasks:
                        if task.get("Stat") == 5:  # Completed
                            start_str = task.get("StartRen")
                            comp_str = task.get("Comp")
                            if (start_str and comp_str and 
                                start_str != "0001-01-01T00:00:00Z" and 
                                comp_str != "0001-01-01T00:00:00Z"):
                                try:
                                    start_time = datetime.fromisoformat(start_str)
                                    comp_time = datetime.fromisoformat(comp_str)
                                    duration_val = (comp_time - start_time).total_seconds()
                                    durations.append(duration_val)
                                except Exception:
                                    pass
                    
                    if durations:
                        avg_duration = sum(durations) / len(durations)
                        remaining = total_tasks - completed_chunks
                        total_eta_seconds = avg_duration * remaining
                        if total_eta_seconds > 0:
                            eta_td = timedelta(seconds=int(total_eta_seconds))
                            eta_str = str(eta_td)
            except Exception as e:
                logger.error(f"Error calculating ETA for job {job.get('_id')}: {e}")
                eta_str = "N/A"
            
            # Create job info message
            info_text = f"<pre>Job Info:\n{'-'*40}\n"
            info_text += f"Batch: {batch_name}\n"
            info_text += f"Name: {name}\n"
            info_text += f"Status: {stat_name}\n"
            info_text += f"Progress: {progress_str}\n"
            info_text += f"ETA: {eta_str}\n"
            info_text += f"Total Tasks: {total_tasks}\n"
            info_text += f"Completed: {completed_chunks}\n"
            info_text += f"{'-'*40}</pre>"
            
            # Create action buttons for this job
            current_job_id = job.get("_id")
            buttons = []
            if stat != 3:  # Not completed
                if stat == 2:  # Suspended
                    buttons.append(InlineKeyboardButton(text="▶ Resume", callback_data=f"resume_job:{current_job_id}"))
                else:
                    buttons.append(InlineKeyboardButton(text="⏸ Suspend", callback_data=f"suspend_job:{current_job_id}"))
                buttons.append(InlineKeyboardButton(text="🔄 Requeue", callback_data=f"requeue_job:{current_job_id}"))
            
            buttons.append(InlineKeyboardButton(text="🔍 Preview", callback_data=f"preview_job:{current_job_id}"))
            buttons.append(InlineKeyboardButton(text="🗑 Delete", callback_data=f"delete_job:{current_job_id}"))
            buttons.append(InlineKeyboardButton(text="📋 Tasks", callback_data=f"tasks_job:{current_job_id}"))
            
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
            
            # Add back button in a separate row
            inline_keyboard.append([InlineKeyboardButton(text="⬅ Back", callback_data="jobs_back")])
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
            
            # Send combined message with info and buttons
            await callback_query.message.answer(
                info_text + "\nActions:",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        
        await callback_query.answer()
        
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
        
        # Check if video already exists in Dropbox
        video_info = await check_video_exists_in_dropbox(login, password, job_id)
        if video_info:
            # Video exists - offer options
            send_button = InlineKeyboardButton(
                text="📤 Send from Dropbox",
                callback_data=f"send_dbx_video:{job_id}"
            )
            recreate_button = InlineKeyboardButton(
                text="🔄 Create new",
                callback_data=f"create_new_video:{job_id}"
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[send_button, recreate_button]]
            )
            await callback_query.message.answer(
                f"🎬 Video '{video_info['filename']}' already exists on Dropbox.",
                reply_markup=keyboard
            )
            await callback_query.answer()
            return
        
        # Video doesn't exist - create new one
        await create_new_video_process(callback_query, login, password, job_id)
            
    except Exception as e:
        logger.error(f"Error handling preview for user {callback_query.from_user.id}: {e}")
        await callback_query.answer("Error occurred while generating preview.", show_alert=True)


async def create_new_video_process(callback_query: CallbackQuery, login: str, password: str, job_id: str):
    """Create new video from job files."""
    try:
        # Send initial message
        progress_msg = await callback_query.message.answer("🔍 Starting preview generation...")
        
        try:
            # Step 1: Download files and convert with progress bar
            await progress_msg.edit_text("📥 Step 1: Downloading files from Dropbox...")
            # Prepare Dropbox session and headers
            import aiohttp
            from app.core.config import settings
            from app.services import get_job_info, get_dropbox_session
            job_info = await get_job_info(login, password, job_id)
            if not job_info:
                await progress_msg.edit_text("❌ Failed to get job info")
                return
            outdirs = job_info.get("OutDir", [])
            if not outdirs:
                await progress_msg.edit_text("❌ No OutDir found for job")
                return
            fullpath = outdirs[0]
            idx = fullpath.find(settings.dropbox_root_marker)
            if idx == -1:
                await progress_msg.edit_text("❌ Dropbox root marker not found in path")
                return
            trimmed = fullpath[idx:]
            dropbox_path = "/" + trimmed.replace("\\", "/").lstrip("/")
            from pathlib import Path
            temp_dir = Path(settings.temp_dir)
            temp_dir.mkdir(exist_ok=True)
            exr_folder_name = Path(dropbox_path).parts[-1]
            if not exr_folder_name:
                exr_folder_name = job_id
            local_root = temp_dir / f"{exr_folder_name}_{job_id}"
            local_root.mkdir(exist_ok=True)
            # Prepare Dropbox headers
            from app.integrations.dropbox_helpers import get_fresh_access_token
            headers_dbx = {
                "Authorization": f"Bearer {get_fresh_access_token()}",
                "Dropbox-API-Select-User": settings.dropbox_team_member_id,
                "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
                "Content-Type": "application/json"
            }
            session_dbx = await get_dropbox_session()
            # List files to get total count
            list_url = "https://api.dropboxapi.com/2/files/list_folder"
            async with session_dbx.post(list_url, headers=headers_dbx, json={"path": dropbox_path}) as list_resp:
                if list_resp.status != 200:
                    await progress_msg.edit_text(f"❌ Failed to list folder: {list_resp.status}")
                    return
                result = await list_resp.json()
            total_files = sum(1 for entry in result.get("entries", []) if entry[".tag"] == "file" and entry["name"].lower().endswith(".exr") and "cryptomatte" not in entry["name"].lower() and "conflicted copy" not in entry["name"].lower())
            # Setup download_states for progress bar
            download_states[job_id] = {
                "progress_msg": progress_msg,
                "total_files": total_files,
                "stop_kb": None  # Add stop button if needed
            }
            stop_downloads[job_id] = None
            # Start download and conversion with progress bar
            await download_exr_folder(
                session_dbx,
                "https://content.dropboxapi.com/2/files/download",
                headers_dbx,
                dropbox_path,
                local_root,
                job_id,
                download_states,
                stop_downloads
            )
            # Step 2: Create video from already converted JPGs
            await progress_msg.edit_text("🎬 Step 2: Converting EXR files and creating video...")
            conv_dir = Path(settings.conv_dir) / f"{exr_folder_name}_{job_id}"
            video_path = assemble_video_from_jpg(conv_dir, str(exr_folder_name))
            if not video_path:
                await progress_msg.edit_text("❌ Failed to create video")
                return
            # Upload video to Dropbox and get Dropbox path
            try:
                # Fetch EXR folder metadata for correct Dropbox path
                metadata = await fetch_dropbox_metadata(session_dbx, dropbox_path, headers_dbx)
                dropbox_video_path = await upload_video_to_dropbox(Path(video_path), metadata, job_id)
            except Exception as e:
                logger.error(f"Error uploading video to Dropbox: {e}")
                dropbox_video_path = dropbox_path  # fallback for caption
            # Step 3: Check file size and compress if needed for Telegram (50MB limit)
            await progress_msg.edit_text("📏 Step 3: Checking file size...")
            from app.integrations.video_helpers import get_file_size_mb, compress_video_if_needed
            from pathlib import Path
            video_path_obj = Path(video_path)
            video_size_mb = get_file_size_mb(video_path_obj)
            logger.info(f"Video size: {video_size_mb:.2f} MB")
            # Compress video if it's larger than 45MB (safe margin for Telegram's 50MB limit)
            if video_size_mb > 45.0:
                await progress_msg.edit_text(f"🗜️ Step 3.5: Compressing video ({video_size_mb:.1f} MB → target: <45 MB)...")
                final_video_path = compress_video_if_needed(video_path_obj, max_size_mb=45.0)
                final_size_mb = get_file_size_mb(final_video_path)
                logger.info(f"Final video size after compression: {final_size_mb:.2f} MB")
            else:
                final_video_path = video_path_obj
                final_size_mb = video_size_mb
            await progress_msg.edit_text(f"📤 Step 4: Sending video ({final_size_mb:.1f} MB)...")
            try:
                from aiogram.types import FSInputFile
                video_filename = final_video_path.name
                # Extract project name from dropbox path
                project_name = video_filename.replace('.mp4', '')  # Default fallback
                try:
                    path_parts = dropbox_video_path.split('/')
                    for i, part in enumerate(path_parts):
                        if part == 'render' and i + 1 < len(path_parts):
                            project_name = path_parts[i + 1]
                            break
                except Exception:
                    pass  # Use default if parsing fails
                # Create caption with project name and dropbox path (avoid None)
                caption = f"📁 {project_name}\n<code>{dropbox_video_path or ''}</code>"
                await callback_query.message.answer_video(
                    video=FSInputFile(str(final_video_path)),
                    caption=caption,
                    parse_mode="HTML"
                )
                # Clean up compressed files if they were created
                if final_video_path != video_path_obj:
                    from app.integrations.video_helpers import cleanup_compressed_files
                    cleanup_compressed_files(video_path_obj)
            except Exception as send_error:
                logger.error(f"Error sending video: {send_error}")
                error_msg = str(send_error)
                if "Request Entity Too Large" in error_msg:
                    await progress_msg.edit_text(f"❌ Video is too large ({final_size_mb:.1f} MB). Telegram limit is 50 MB.")
                else:
                    await progress_msg.edit_text(f"❌ Error sending video: {error_msg}")
                return
            
            await progress_msg.delete()
            
            # Answer callback to stop button animation (with error handling for old queries)
            try:
                await callback_query.answer("Video created successfully!")
            except Exception as answer_error:
                logger.warning(f"Could not answer callback query (likely too old): {answer_error}")
                # Don't send additional message - video was already sent
            
            # Clean up temp and conv directories after successful video creation and sending
            # This matches the behavior of the old version
            try:
                from app.core.utils import cleanup_temp_and_conv
                cleanup_temp_and_conv()
                logger.info(f"Cleaned up temp and conv directories after video creation for job {job_id}")
            except Exception as cleanup_error:
                logger.error(f"Error cleaning up directories after video creation: {cleanup_error}")
            
        except Exception as e:
            logger.error(f"Error in preview generation: {e}")
            try:
                await progress_msg.edit_text(f"❌ Error during preview generation: {str(e)}")
            except Exception as edit_error:
                logger.warning(f"Could not edit progress message: {edit_error}")
                await callback_query.message.answer(f"❌ Error during preview generation: {str(e)}")
            
            try:
                await callback_query.answer("Error occurred while creating video.", show_alert=True)
            except Exception as answer_error:
                logger.warning(f"Could not answer callback query: {answer_error}")
                await callback_query.message.answer("❌ Error occurred while creating video.")
            
            # Clean up job files even on error
            try:
                from app.integrations.video_helpers import cleanup_job_files, cleanup_old_files
                from app.core.utils import cleanup_temp_and_conv
                cleanup_job_files(job_id)
                # Clean up temp and conv directories
                cleanup_temp_and_conv()
                # Also clean up old files to save disk space
                cleanup_old_files(max_age_hours=6)
                logger.info(f"Cleaned up files after error for job {job_id}")
            except Exception as cleanup_error:
                logger.error(f"Error cleaning up job files after error: {cleanup_error}")
            
    except Exception as e:
        logger.error(f"Error in create_new_video_process: {e}")
        # Clean up files even on critical error
        try:
            from app.core.utils import cleanup_temp_and_conv
            from app.integrations.video_helpers import cleanup_job_files, cleanup_old_files
            cleanup_job_files(job_id)
            cleanup_temp_and_conv()
            cleanup_old_files(max_age_hours=6)
            logger.info(f"Cleaned up files after critical error for job {job_id}")
        except Exception as cleanup_error:
            logger.error(f"Error cleaning up files after critical error: {cleanup_error}")
        
        await callback_query.answer("Error occurred while creating video.", show_alert=True)


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
                batch = job.get("Props", {}).get("Batch", "Untitled")
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
                batch = props.get("Batch", "Untitled")
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
                    batch = props.get("Batch", "Untitled")
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
            batch_text = "\n".join(messages) if messages else "No data"
            
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
                    nav_buttons.append(InlineKeyboardButton(text="⬅ Back", callback_data=f"jobs_page:{0-1}"))
                if (0 + 1) < total_pages:
                    nav_buttons.append(InlineKeyboardButton(text="Next ➡", callback_data=f"jobs_page:{0+1}"))
                if nav_buttons:
                    inline_keyboard.append(nav_buttons)
                
                keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
                page_info = f"Page {0+1} of {total_pages}"
                
                await callback_query.message.edit_text(
                    f"<pre>{header}\n{batch_text}\n{page_info}</pre>\n\nSelect a job for details:",
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
    
    # Global dictionary used to store active realtime tasks
    if not hasattr(handle_realtime, "active_realtime_tasks"):
        handle_realtime.active_realtime_tasks = {}
    active_realtime_tasks = handle_realtime.active_realtime_tasks
    
    # Stop a previous realtime job if it exists
    if chat_id in active_realtime_tasks:
        active_realtime_tasks[chat_id].cancel()
        del active_realtime_tasks[chat_id]

    await message.answer("Tasks will be updated every 5 seconds until the next message.")

    async def realtime_loop():
        from collections import defaultdict
        from datetime import datetime
        try:
            msg = await message.answer("Loading...")
            last_text = None
            while True:
                jobs = await get_jobs_list(user_id)
                # Group and format jobs the same way as handle_jobs
                grouped_jobs = defaultdict(list)
                for job in jobs:
                    batch = job.get("Props", {}).get("Batch", "Untitled")
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
                    batch = props.get("Batch", "Untitled")
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
                        batch = props.get("Batch", "Untitled")
                        total_tasks = props.get("Tasks", 0)
                        completed_chunks = job.get("CompletedChunks", 0)
                        progress_str = format_progress_old(completed_chunks, total_tasks)
                        messages.append(f"⏸️ {batch:<22} {progress_str:^16}\n{'-'*40}")
                header = f"{'Batch':<24} {'Progress':^16}"
                header += f"\n{'-'*40}"
                batch_text = "\n".join(messages) if messages else "No data"
                new_text = f"<pre>{header}\n{batch_text}</pre>"
                if new_text != last_text:
                    await msg.edit_text(new_text, parse_mode="HTML")
                    last_text = new_text
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await message.answer(f"Error: {e}")
    task = asyncio.create_task(realtime_loop())
    active_realtime_tasks[chat_id] = task


@router.callback_query(lambda c: c.data and c.data.startswith("requeue_job:"))
async def requeue_job_callback(callback_query: CallbackQuery):
    """Handle requeue job button press."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return
        
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return
    
    job_id = callback_query.data.split(":", 1)[1]
    
    try:
        success = await requeue_job_by_user_id(callback_query.from_user.id, job_id)
        if success:
            await callback_query.answer("Job requeued successfully!")
        else:
            await callback_query.answer("Failed to requeue job.", show_alert=True)
    except Exception as e:
        logger.error(f"Error requeuing job for user {callback_query.from_user.id}: {e}")
        await callback_query.answer("Error occurred while requeuing job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("resume_job:"))
async def resume_job_callback(callback_query: CallbackQuery):
    """Handle resume job button press."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return
        
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return
    
    job_id = callback_query.data.split(":", 1)[1]
    
    try:
        success = await resume_job_by_user_id(callback_query.from_user.id, job_id)
        if success:
            await callback_query.answer("Job resumed successfully!")
        else:
            await callback_query.answer("Failed to resume job.", show_alert=True)
    except Exception as e:
        logger.error(f"Error resuming job for user {callback_query.from_user.id}: {e}")
        await callback_query.answer("Error occurred while resuming job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("suspend_job:"))
async def suspend_job_callback(callback_query: CallbackQuery):
    """Handle suspend job button press."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return
        
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return
    
    job_id = callback_query.data.split(":", 1)[1]
    
    try:
        success = await suspend_job_by_user_id(callback_query.from_user.id, job_id)
        if success:
            await callback_query.answer("Job suspended successfully!")
        else:
            await callback_query.answer("Failed to suspend job.", show_alert=True)
    except Exception as e:
        logger.error(f"Error suspending job for user {callback_query.from_user.id}: {e}")
        await callback_query.answer("Error occurred while suspending job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("delete_job:"))
async def delete_job_callback(callback_query: CallbackQuery):
    """Handle delete job button press."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return
        
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return
    
    job_id = callback_query.data.split(":", 1)[1]
    
    try:
        success = await delete_job_by_user_id(callback_query.from_user.id, job_id)
        if success:
            await callback_query.answer("Job deleted successfully!")
            # Update the message to show job was deleted
            await callback_query.message.edit_text("Job has been deleted.")
        else:
            await callback_query.answer("Failed to delete job.", show_alert=True)
    except Exception as e:
        logger.error(f"Error deleting job for user {callback_query.from_user.id}: {e}")
        await callback_query.answer("Error occurred while deleting job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("tasks_job:"))
async def tasks_job_callback(callback_query: CallbackQuery):
    """Handle tasks job button press."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return
        
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return
    
    job_id = callback_query.data.split(":", 1)[1]
    
    try:
        tasks = await get_job_tasks_by_user_id(callback_query.from_user.id, job_id)
        if not tasks:
            await callback_query.message.answer("No tasks found for this job.")
            await callback_query.answer()
            return

        # Format tasks list
        lines = []
        header = f"{'Frames':<18} {'Prog':^10} {'Time':^12}"
        header += f"\n{'-'*42}"
        lines.append(header)
        
        def get_task_icon(stat):
            if stat == 5:   # Completed
                return "✅"
            elif stat == 4: # Rendering
                return "▶️"
            elif stat == 3: # Suspended
                return "⏸️"
            elif stat == 6: # Failed
                return "❌"
            elif stat in (2, 8): # Queued or Pending
                return "⏳"
            else:           # Unknown or other states
                return "❓"

        from datetime import datetime, timezone
        for task in tasks:
            frames = task.get("Frames", "")
            prog = task.get("Prog", "")
            stat = task.get("Stat", 1)
            icon = get_task_icon(stat)
            rendertime_str = ""
            start_str = task.get("StartRen")
            if start_str and start_str != "0001-01-01T00:00:00Z":
                try:
                    start_time = datetime.fromisoformat(start_str)
                    # Completed tasks
                    if stat == 5:
                        comp_str = task.get("Comp")
                        if comp_str and comp_str != "0001-01-01T00:00:00Z":
                            comp_time = datetime.fromisoformat(comp_str)
                            duration = comp_time.astimezone(timezone.utc) - start_time.astimezone(timezone.utc)
                            rendertime_str = str(duration).split(".")[0]
                    # Currently rendering tasks
                    elif stat == 4:
                        now_utc = datetime.now(timezone.utc)
                        duration = now_utc - start_time.astimezone(timezone.utc)
                        rendertime_str = str(duration).split(".")[0]
                except Exception:
                    pass
            line = f"{icon} {frames:<16} {prog:^10} {rendertime_str:^12}"
            lines.append(line)

        message_text = "<pre>" + "\n".join(lines) + "</pre>"
        await callback_query.message.answer(message_text, parse_mode="HTML")
        await callback_query.answer()
        
    except Exception as e:
        logger.error(f"Error getting tasks for user {callback_query.from_user.id}: {e}")
        await callback_query.answer("Error occurred while fetching tasks.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("send_dbx_video:"))
async def send_dbx_video_callback(callback_query: CallbackQuery):
    """Handle send video from Dropbox button press."""
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
        
        # Download video from Dropbox
        progress_msg = await callback_query.message.answer("📥 Downloading video from Dropbox...")
        
        try:
            video_result = await download_video_from_dropbox(login, password, job_id)
            
            if not video_result:
                await progress_msg.edit_text("❌ Failed to download video from Dropbox")
                return
            
            # video_result is now a tuple: (video_path, dropbox_path)
            video_path, dropbox_path = video_result
            
            # Check file size (no compression needed for documents up to 2GB)
            await progress_msg.edit_text("📏 Checking file size...")
            from app.integrations.video_helpers import get_file_size_mb
            from aiogram.types import FSInputFile
            from pathlib import Path
            
            video_path_obj = Path(video_path)
            video_size_mb = get_file_size_mb(video_path_obj)
            logger.info(f"Video size: {video_size_mb:.2f} MB")
            
            # No compression needed - documents support up to 2GB
            final_video_path = video_path_obj
            final_size_mb = video_size_mb
            
            await progress_msg.edit_text(f"📤 Sending video ({final_size_mb:.1f} MB)...")
            
            video_filename = final_video_path.name
            
            # Extract project name from dropbox path
            # Example: /Team Folder/Project/render/shot_v01/Redshift_ROP1.mp4
            # We want to extract: shot_v01
            project_name = video_filename.replace('.mp4', '')  # Default fallback
            try:
                # Split path and look for the render folder
                path_parts = dropbox_path.split('/')
                for i, part in enumerate(path_parts):
                    if part == 'render' and i + 1 < len(path_parts):
                        project_name = path_parts[i + 1]
                        break
            except Exception:
                pass  # Use default if parsing fails
            
            # Create caption with project name instead of filename
            caption = f"📁 {project_name}\n<code>{dropbox_path}</code>"
            
            # Send as video instead of document
            await callback_query.message.answer_video(
                video=FSInputFile(str(final_video_path)),
                caption=caption,
                parse_mode="HTML"
            )
            
            # No compression cleanup needed - we send original files
                
            # Clean up job files after successful send
            from app.integrations.video_helpers import cleanup_job_files, cleanup_old_files
            cleanup_job_files(job_id)
            # Also clean up old files to save disk space
            cleanup_old_files(max_age_hours=6)  # Clean files older than 6 hours
            
            # Clean up temp and conv directories after successful video send
            # This matches the behavior of the old version
            try:
                from app.core.utils import cleanup_temp_and_conv
                cleanup_temp_and_conv()
                logger.info(f"Cleaned up temp and conv directories after video send for job {job_id}")
            except Exception as cleanup_error:
                logger.error(f"Error cleaning up directories after video send: {cleanup_error}")
            
            await progress_msg.delete()
            
            # Clean up temporary file
            try:
                Path(video_path).unlink()
            except Exception:
                pass
            
            # Answer callback to stop button animation
            try:
                await callback_query.answer("Video sent successfully!")
            except Exception as answer_error:
                logger.warning(f"Could not answer callback query: {answer_error}")
                # Don't send additional message - video was already sent
                
        except Exception as e:
            logger.error(f"Error downloading video from Dropbox: {e}")
            await progress_msg.edit_text(f"❌ Error downloading video: {str(e)}")
            await callback_query.answer("Error occurred while downloading video.", show_alert=True)
            
    except Exception as e:
        logger.error(f"Error handling send_dbx_video for user {callback_query.from_user.id}: {e}")
        await callback_query.answer("Error occurred while downloading video.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("create_new_video:"))
async def create_new_video_callback(callback_query: CallbackQuery):
    """Handle create new video button press."""
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
        
        # Create new video
        await create_new_video_process(callback_query, login, password, job_id)
        
        # Answer callback to stop button animation (if not already answered in create_new_video_process)
        try:
            await callback_query.answer("Video creation completed!")
        except Exception as answer_error:
            logger.warning(f"Could not answer callback query in create_new_video_callback: {answer_error}")
            # Already answered in create_new_video_process or query is too old
        
    except Exception as e:
        logger.error(f"Error handling create_new_video for user {callback_query.from_user.id}: {e}")
        await callback_query.answer("Error occurred while creating video.", show_alert=True)


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
