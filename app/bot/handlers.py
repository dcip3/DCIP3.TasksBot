# app/bot/handlers.py
"""
Telegram bot command and state handlers for aiogram 3.x.

This module contains all the message handlers, callback handlers, and state
machines for user interactions including authentication, job management,
worker monitoring, and realtime operations.
"""

import logging
import asyncio
import contextlib
import json
from datetime import datetime, timezone
from typing import Optional, cast
from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command, StateFilter

from app.core.utils import (
    authorized_only,
    get_main_keyboard,
    register_preview_message,
    cleanup_temp_and_conv,
)
from app.core.bot_core import bot, dp, download_states, stop_downloads
from app.core.config import settings
from app.auth import (
    authenticate_user,
    logout_user,
    is_authorized,
    save_deadline_credentials,
    get_notification_settings,
    set_notification_enabled,
    set_notification_scope,
    NotificationScope,
)
from app.services import (
    get_jobs_list, get_workers_list, get_job_info_by_user_id, get_job_tasks_by_user_id,
    requeue_job_by_user_id, resume_job_by_user_id, suspend_job_by_user_id, delete_job_by_user_id,
    create_video_from_job, check_video_exists_in_dropbox, download_video_from_dropbox,
    get_dropbox_session,
    WorkerStatusError
)
from app.bot.job_helpers import (
    group_and_sort_jobs,
    truncate_cell,
    format_progress_old
)
from pathlib import Path
from app.integrations.video_helpers import (
    assemble_video_from_jpg,
    cleanup_job_files,
    cleanup_old_files,
    compress_video_if_needed,
    get_file_size_mb,
)
from app.integrations.dropbox_helpers import (
    get_fresh_access_token,
    fetch_dropbox_metadata,
    download_exr_folder,
    upload_video_to_dropbox,
)

logger = logging.getLogger(__name__)

# Create router
router = Router()

# Column width constants for text tables
BATCH_COLUMN_WIDTH = 22
PAGE_SIZE = 6


def _render_settings_root_text() -> str:
    """Return text for the root settings menu."""
    return "Settings\nSelect a section to configure."


def _build_settings_root_keyboard() -> InlineKeyboardMarkup:
    """Create the root inline keyboard for settings."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔔 Notifications",
                    callback_data="settings:notifications",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Close",
                    callback_data="settings:close",
                ),
            ],
        ]
    )


def _render_notification_settings_text(enabled: bool, scope: str) -> str:
    """Return descriptive text for the notification settings submenu."""
    status_text = "enabled" if enabled else "disabled"
    scope_lower = scope.lower()
    if scope_lower == "own":
        scope_text = "My jobs only"
    else:
        scope_text = "All jobs"

    details = [
        "Notification Settings",
        f"Status: {status_text}",
        f"Scope: {scope_text}",
        "",
        "Choose how you would like to receive job alerts.",
    ]
    if not enabled:
        details.append("Notifications are currently disabled.")
    return "\n".join(details)


def _build_notification_keyboard(enabled: bool, scope: str) -> InlineKeyboardMarkup:
    """Create inline keyboard for notification options."""
    scope_normalized = scope.lower()
    enable_label = ("✅ " if enabled else "◻ ") + "Receive notifications"
    all_jobs_label = ("✅ " if scope_normalized == "all" else "◻ ") + "All jobs"
    own_jobs_label = ("✅ " if scope_normalized == "own" else "◻ ") + "My jobs only"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=enable_label,
                    callback_data="settings:notif:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    text=all_jobs_label,
                    callback_data="settings:notif:scope:all",
                ),
                InlineKeyboardButton(
                    text=own_jobs_label,
                    callback_data="settings:notif:scope:own",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="settings:back:root",
                ),
                InlineKeyboardButton(
                    text="Close",
                    callback_data="settings:close",
                ),
            ],
        ]
    )

def _build_render_method_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """Inline keyboard offering render method choices."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="☁️ Deadline",
                    callback_data=f"preview_render:deadline:{job_id}",
                ),
                InlineKeyboardButton(
                    text="🖥 Server",
                    callback_data=f"preview_render:server:{job_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✖️ Cancel",
                    callback_data="preview_cancel",
                )
            ],
        ]
    )


async def _prompt_render_method(
    callback_query: CallbackQuery,
    job_id: str,
    message_text: Optional[str] = None,
) -> None:
    """Send a prompt asking the user to choose how to render the preview."""
    text = message_text or "Choose how to create the preview."
    keyboard = _build_render_method_keyboard(job_id)
    target_message = callback_query.message
    if target_message:
        await target_message.answer(text, reply_markup=keyboard)
    elif callback_query.from_user:
        await bot.send_message(callback_query.from_user.id, text, reply_markup=keyboard)
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

        # Group and sort jobs using helper function
        combined_jobs = await group_and_sort_jobs(jobs)
        
        # Pagination by newest first
        jobs_slice = combined_jobs[page * PAGE_SIZE : page * PAGE_SIZE + PAGE_SIZE]
        
        # Format message in old style with HTML pre tags
        messages = []
        buttons = []
        
        for job in jobs_slice:
            props = job.get("Props", {})
            batch = props.get("Batch", "Untitled")
            display_batch = truncate_cell(batch)
            total_tasks = props.get("Tasks", 0)
            completed_chunks = job.get("CompletedChunks", 0)
            progress_str = format_progress_old(completed_chunks, total_tasks)
            stat = job.get("Stat", 0)
            
            icon = "✅" if stat == 3 else "⏸️" if stat == 2 else "▶️"
                
            messages.append(
                f"{icon} {display_batch:<{BATCH_COLUMN_WIDTH}} {progress_str:^16}\n{'-'*40}"
            )
            
            # Add button for this job
            job_id = job.get("_id")
            if job_id:
                buttons.append(InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}"))
        
        header = f"{'Batch':<{BATCH_COLUMN_WIDTH + 2}} {'Progress':^16}"
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
            total_pages = (total_items + PAGE_SIZE - 1) // PAGE_SIZE
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


@router.message(F.text == "⚙️ Settings")
@authorized_only
async def handle_settings_menu(message: Message):
    """
    Display the settings menu with inline navigation.
    """
    stop_realtime_for_chat(message.chat.id)

    if message.from_user is None:
        await message.answer("Error: User information not available.")
        return

    await message.answer(
        _render_settings_root_text(),
        reply_markup=_build_settings_root_keyboard(),
    )

# ============================================================================
# === NOTIFICATION HANDLERS ===
# ============================================================================

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

@router.callback_query(lambda c: c.data and c.data.startswith("settings:"))
async def settings_callback_handler(callback_query: CallbackQuery):
    """Handle inline settings navigation and updates."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return

    if callback_query.message is None:
        await callback_query.answer("Error: Message not available.", show_alert=True)
        return

    if not await is_authorized(callback_query.from_user.id):
        await callback_query.answer("Please login first.", show_alert=True)
        return

    user_id = callback_query.from_user.id
    parts = callback_query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    async def show_root():
        try:
            await callback_query.message.edit_text(
                _render_settings_root_text(),
                reply_markup=_build_settings_root_keyboard(),
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def show_notifications():
        enabled, scope = await get_notification_settings(user_id)
        try:
            await callback_query.message.edit_text(
                _render_notification_settings_text(enabled, scope),
                reply_markup=_build_notification_keyboard(enabled, scope),
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    if action == "close":
        await callback_query.message.edit_text("Settings closed.")
        await callback_query.answer()
        return

    if action == "notifications":
        await show_notifications()
        await callback_query.answer()
        return

    if action == "notif" and len(parts) > 2:
        sub_action = parts[2]
        if sub_action == "toggle":
            current_enabled, _ = await get_notification_settings(user_id)
            enabled, scope = await set_notification_enabled(user_id, not current_enabled)
            await show_notifications()
            await callback_query.answer("Notifications enabled" if enabled else "Notifications disabled")
            return
        if sub_action == "scope" and len(parts) > 3:
            scope_value = parts[3]
            if scope_value not in {"all", "own"}:
                await callback_query.answer("Unsupported option.", show_alert=True)
                return
            enabled, scope = await set_notification_scope(
                user_id,
                cast(NotificationScope, scope_value),
            )
            await show_notifications()
            if scope == "own":
                await callback_query.answer("Scope set to my jobs only")
            else:
                await callback_query.answer("Scope set to all jobs")
            return

    if action == "back":
        target = parts[2] if len(parts) > 2 else "root"
        if target == "root":
            await show_root()
            await callback_query.answer()
            return

    await callback_query.answer()


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

        # Group and sort jobs using helper function
        combined_jobs = await group_and_sort_jobs(jobs)
        
        # Pagination: 4 jobs per page
        jobs_slice = combined_jobs[page * PAGE_SIZE : page * PAGE_SIZE + PAGE_SIZE]
        
        # Format message in old style with HTML pre tags
        messages = []
        buttons = []
        
        for job in jobs_slice:
            props = job.get("Props", {})
            batch = props.get("Batch", "Untitled")
            display_batch = truncate_cell(batch)
            total_tasks = props.get("Tasks", 0)
            completed_chunks = job.get("CompletedChunks", 0)
            progress_str = format_progress_old(completed_chunks, total_tasks)
            stat = job.get("Stat", 0)
            
            icon = "✅" if stat == 3 else "⏸️" if stat == 2 else "▶️"
                
            messages.append(
                f"{icon} {display_batch:<{BATCH_COLUMN_WIDTH}} {progress_str:^16}\n{'-'*40}"
            )
            
            # Add button for this job
            job_id = job.get("_id")
            if job_id:
                buttons.append(InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}"))
        
        header = f"{'Batch':<{BATCH_COLUMN_WIDTH + 2}} {'Progress':^16}"
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
            total_pages = (total_items + PAGE_SIZE - 1) // PAGE_SIZE
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

        # Batch load all tasks for all jobs in the batch to avoid N+1 problem
        job_tasks_map = {}
        tasks_futures = []
        for job in batch_jobs:
            job_id = job.get("_id")
            if job_id:
                tasks_futures.append((job_id, get_job_tasks_by_user_id(callback_query.from_user.id, job_id)))

        if tasks_futures:
            results = await asyncio.gather(*[future for _, future in tasks_futures], return_exceptions=True)
            for (job_id, _), tasks in zip(tasks_futures, results):
                if isinstance(tasks, Exception):
                    logger.error(f"Error loading tasks for job {job_id}: {tasks}")
                    job_tasks_map[job_id] = []
                else:
                    job_tasks_map[job_id] = tasks or []

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
                # Get tasks from pre-loaded map to avoid N+1 queries
                tasks = job_tasks_map.get(job.get("_id"), [])
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
                text="🔄 New render",
                callback_data=f"preview_render_options:{job_id}"
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
        
        # Video doesn't exist - prompt user to choose render method
        await _prompt_render_method(
            callback_query,
            job_id,
            "No preview yet. Choose a render method:",
        )
        await callback_query.answer()
        return
            
    except Exception as e:
        logger.error(f"Error handling preview for user {callback_query.from_user.id}: {e}")
        await callback_query.answer("Error occurred while generating preview.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("preview_render_options:"))
async def preview_render_options_callback(callback_query: CallbackQuery):
    """Show render method choices when user wants to create a new preview."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]
    await _prompt_render_method(
        callback_query,
        job_id,
        "Select a preview render method:",
    )
    try:
        await callback_query.answer()
    except Exception:
        # Callback may already be answered elsewhere; ignore
        pass


async def create_new_video_process(
    callback_query: CallbackQuery,
    job_id: str,
    *,
    use_any_machine: bool = False,
    skip_worker_validation: bool = False,
    progress_message: Optional[Message] = None,
) -> None:
    """Submit a Deadline job that generates a preview video via ffmpeg."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: user not found.", show_alert=True)
        return

    initial_text = (
        "🧾 Submitting preview job to Deadline..."
        if not use_any_machine
        else "🧾 Submitting preview job without machine restrictions..."
    )
    progress_msg = progress_message
    if progress_msg is None:
        progress_msg = await callback_query.message.answer(initial_text)
    else:
        try:
            await progress_msg.edit_text(initial_text, reply_markup=None)
        except Exception:
            progress_msg = await callback_query.message.answer(initial_text)
    try:
        result = await create_video_from_job(
            callback_query.from_user.id,
            job_id,
            skip_worker_validation=skip_worker_validation,
            use_any_machine=use_any_machine,
        )
        if not result:
            await progress_msg.edit_text("❌ Failed to submit the job to Deadline.")
            await callback_query.answer("Failed to submit the job.", show_alert=True)
            return

        preview_id = result.get("preview_job_id")
        preferred_slaves = result.get("preferred_slaves") or []
        dropbox_path = result.get("expected_dropbox_path")

        await progress_msg.edit_text("✅ Preview job queued\n□ □ □")
        if preview_id:
            register_preview_message(preview_id, progress_msg.chat.id, progress_msg.message_id)
        await callback_query.answer("Preview job queued!", show_alert=False)
    except WorkerStatusError as worker_error:
        status_lines = []
        for item in worker_error.invalid_workers:
            name = item.get("name", "Unknown")
            status_code = item.get("status_code")
            status_text = item.get("status_text") or "Unknown"
            if status_code is None:
                status_lines.append(f"• {name}: {status_text}")
            else:
                status_lines.append(f"• {name}: {status_text} ({status_code})")

        status_block = "\n".join(status_lines) if status_lines else "• No status information"
        message_text = (
            "⚠️ Preview job could not be queued: preferred workers are unavailable.\n"
            f"{status_block}\n\n"
            "Choose an action:"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Check again", callback_data=f"preview_retry:{job_id}"
                    ),
                    InlineKeyboardButton(
                        text="☁️ Use any worker", callback_data=f"preview_force:{job_id}"
                    ),
                ],
                [InlineKeyboardButton(text="✖️ Cancel", callback_data="preview_cancel")],
            ]
        )
        await progress_msg.edit_text(message_text, reply_markup=keyboard)
        await callback_query.answer("Preferred workers are unavailable.", show_alert=False)
    except Exception as e:
        logger.error(
            "Error submitting preview job for user %s: %s",
            callback_query.from_user.id if callback_query.from_user else "unknown",
            e,
        )
        await progress_msg.edit_text("❌ An error occurred while submitting the job.")
        try:
            await callback_query.answer("Failed to submit the job.", show_alert=True)
        except Exception:
            pass


async def render_preview_via_server(callback_query: CallbackQuery, job_id: str) -> None:
    """Replicate the optimized local render pipeline: download EXRs, convert, assemble, and deliver."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: user not found.", show_alert=True)
        return

    progress_msg: Optional[Message] = None
    try:
        base_message = callback_query.message
        if base_message:
            progress_msg = await base_message.answer("🔍 Starting preview generation...")
        else:
            progress_msg = await bot.send_message(callback_query.from_user.id, "🔍 Starting preview generation...")

        await progress_msg.edit_text("📥 Step 1: Downloading files from Dropbox...")

        job_info = await get_job_info_by_user_id(callback_query.from_user.id, job_id)
        if not job_info:
            await progress_msg.edit_text("❌ Failed to get job info")
            await callback_query.answer("Failed to get job info.", show_alert=True)
            return

        props = job_info.get("Props", {}) or {}
        job_name = props.get("Name") or props.get("Batch") or job_id

        outdirs = job_info.get("OutDir", [])
        if not outdirs:
            await progress_msg.edit_text("❌ No output directory found.")
            await callback_query.answer("Render path is missing.", show_alert=True)
            return

        fullpath = outdirs[0]
        idx = fullpath.find(settings.dropbox_root_marker)
        if idx == -1:
            await progress_msg.edit_text("❌ Dropbox root marker not found in path.")
            await callback_query.answer("Could not determine Dropbox path.", show_alert=True)
            return

        trimmed = fullpath[idx:]
        dropbox_path = "/" + trimmed.replace("\\", "/").lstrip("/")

        temp_dir = Path(settings.temp_dir)
        temp_dir.mkdir(exist_ok=True)
        exr_folder_name = Path(dropbox_path).parts[-1] or job_id
        local_root = temp_dir / f"{exr_folder_name}_{job_id}"
        local_root.mkdir(parents=True, exist_ok=True)

        headers_dbx = {
            "Authorization": f"Bearer {get_fresh_access_token()}",
            "Dropbox-API-Select-User": settings.dropbox_team_member_id,
            "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": settings.dropbox_root_namespace_id}),
            "Content-Type": "application/json",
        }
        session_dbx = await get_dropbox_session()

        list_url = "https://api.dropboxapi.com/2/files/list_folder"
        async with session_dbx.post(list_url, headers=headers_dbx, json={"path": dropbox_path}) as list_resp:
            if list_resp.status != 200:
                await progress_msg.edit_text(f"❌ Failed to list folder: {list_resp.status}")
                await callback_query.answer("Could not list files in Dropbox.", show_alert=True)
                return
            list_result = await list_resp.json()

        total_files = sum(
            1
            for entry in list_result.get("entries", [])
            if entry.get(".tag") == "file"
            and entry["name"].lower().endswith(".exr")
            and "cryptomatte" not in entry["name"].lower()
            and "conflicted copy" not in entry["name"].lower()
        )
        if total_files <= 0:
            await progress_msg.edit_text("⚠️ No usable EXR files found for conversion.")
            await callback_query.answer("No frames available for preview build.", show_alert=True)
            return

        download_states[job_id] = {
            "progress_msg": progress_msg,
            "total_files": total_files,
            "stop_kb": None,
        }
        stop_downloads[job_id] = None

        await download_exr_folder(
            session_dbx,
            "https://content.dropboxapi.com/2/files/download",
            headers_dbx,
            dropbox_path,
            local_root,
            job_id,
            download_states,
            stop_downloads,
        )

        conv_dir = Path(settings.conv_dir) / f"{exr_folder_name}_{job_id}"
        await progress_msg.edit_text("🎬 Step 2: Converting EXR files and creating video...")
        video_path = await asyncio.to_thread(assemble_video_from_jpg, conv_dir, str(exr_folder_name))

        try:
            metadata = await fetch_dropbox_metadata(session_dbx, dropbox_path, headers_dbx)
            dropbox_video_path = await upload_video_to_dropbox(Path(video_path), metadata, job_id)
        except Exception as upload_error:
            logger.error(f"Error uploading video to Dropbox: {upload_error}")
            dropbox_video_path = dropbox_path

        await progress_msg.edit_text("📏 Step 3: Checking file size...")
        video_path_obj = Path(video_path)
        video_size_mb = get_file_size_mb(video_path_obj)

        if video_size_mb > 45.0:
            await progress_msg.edit_text(
                f"🗜️ Step 3.5: Compressing video ({video_size_mb:.1f} MB → target <45 MB)..."
            )
            final_video_path = await asyncio.to_thread(compress_video_if_needed, video_path_obj, 45.0)
            final_size_mb = get_file_size_mb(final_video_path)
        else:
            final_video_path = video_path_obj
            final_size_mb = video_size_mb

        await progress_msg.edit_text(f"📤 Step 4: Sending video ({final_size_mb:.1f} MB)...")

        video_filename = final_video_path.name
        project_name = video_filename.replace(".mp4", "")
        try:
            path_parts = (dropbox_video_path or "").split("/")
            for idx_part, part in enumerate(path_parts):
                if part == "render" and idx_part + 1 < len(path_parts):
                    project_name = path_parts[idx_part + 1]
                    break
        except Exception:
            pass

        caption = f"📁 {project_name}\n<code>{dropbox_video_path or ''}</code>"
        if callback_query.message:
            await callback_query.message.answer_video(
                video=FSInputFile(str(final_video_path)),
                caption=caption,
                parse_mode="HTML",
            )
        else:
            await bot.send_video(
                callback_query.from_user.id,
                FSInputFile(str(final_video_path)),
                caption=caption,
                parse_mode="HTML",
            )

        with contextlib.suppress(Exception):
            await progress_msg.delete()

        try:
            await callback_query.answer("Video created successfully!")
        except Exception as answer_error:
            logger.warning(f"Could not answer callback query (likely expired): {answer_error}")

        try:
            cleanup_temp_and_conv()
            cleanup_old_files(max_age_hours=6)
        except Exception as cleanup_error:
            logger.error(f"Error cleaning up directories after video creation: {cleanup_error}")
    except Exception as exc:
        logger.error(
            "Error in server-side preview generation for user %s job %s: %s",
            callback_query.from_user.id if callback_query.from_user else "unknown",
            job_id,
            exc,
        )
        if progress_msg:
            with contextlib.suppress(Exception):
                await progress_msg.edit_text(f"❌ Error during preview generation: {exc}")
        try:
            await callback_query.answer("Error occurred while creating video.", show_alert=True)
        except Exception as answer_error:
            logger.warning(f"Could not answer callback query after failure: {answer_error}")
            if callback_query.message:
                await callback_query.message.answer("❌ Error occurred while creating video.")
        try:
            cleanup_job_files(job_id)
            cleanup_temp_and_conv()
            cleanup_old_files(max_age_hours=6)
        except Exception as cleanup_error:
            logger.error(f"Error cleaning up job files after failure: {cleanup_error}")
    finally:
        download_states.pop(job_id, None)
        stop_downloads.pop(job_id, None)


@router.callback_query(lambda c: c.data and c.data.startswith("preview_render:"))
async def preview_render_callback(callback_query: CallbackQuery):
    """Handle render method choice for previews."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    parts = callback_query.data.split(":", 2)
    if len(parts) != 3:
        await callback_query.answer("Invalid selection.", show_alert=True)
        return

    _, mode, job_id = parts

    if mode == "deadline":
        await create_new_video_process(callback_query, job_id)
        return

    if mode == "server":
        await render_preview_via_server(callback_query, job_id)
        return

    await callback_query.answer("Unknown action.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("preview_retry:"))
async def preview_retry_callback(callback_query: CallbackQuery):
    """Retry worker status check before submitting preview."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]
    try:
        await create_new_video_process(
            callback_query,
            job_id,
            progress_message=callback_query.message,
        )
    except Exception as exc:
        logger.error("Error retrying preview submission: %s", exc)
        await callback_query.answer("Retry failed.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("preview_force:"))
async def preview_force_callback(callback_query: CallbackQuery):
    """Force preview submission without worker whitelist."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]
    try:
        await create_new_video_process(
            callback_query,
            job_id,
            use_any_machine=True,
            skip_worker_validation=True,
            progress_message=callback_query.message,
        )
    except Exception as exc:
        logger.error("Error forcing preview submission: %s", exc)
        await callback_query.answer("Failed to submit without restrictions.", show_alert=True)


@router.callback_query(lambda c: c.data == "preview_cancel")
async def preview_cancel_callback(callback_query: CallbackQuery):
    """Cancel preview submission attempt."""
    await callback_query.answer("Action cancelled.", show_alert=False)
    try:
        await callback_query.message.edit_text("Action cancelled.", reply_markup=None)
    except Exception:
        pass


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

            # Group and sort jobs using helper function
            combined_jobs = await group_and_sort_jobs(jobs)
            
            # Pagination: first page preview
            jobs_slice = combined_jobs[:PAGE_SIZE]
            
            # Format message in old style with HTML pre tags
            messages = []
            buttons = []
            
            for job in jobs_slice:
                props = job.get("Props", {})
                batch = props.get("Batch", "Untitled")
                display_batch = truncate_cell(batch)
                total_tasks = props.get("Tasks", 0)
                completed_chunks = job.get("CompletedChunks", 0)
                progress_str = format_progress_old(completed_chunks, total_tasks)
                stat = job.get("Stat", 0)
                
                icon = "✅" if stat == 3 else "⏸️" if stat == 2 else "▶️"
                    
                messages.append(
                    f"{icon} {display_batch:<{BATCH_COLUMN_WIDTH}} {progress_str:^16}\n{'-'*40}"
                )
                
                # Add button for this job
                job_id = job.get("_id")
                if job_id:
                    buttons.append(InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}"))
            
            header = f"{'Batch':<{BATCH_COLUMN_WIDTH + 2}} {'Progress':^16}"
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
                total_pages = (total_items + PAGE_SIZE - 1) // PAGE_SIZE
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
        try:
            msg = await message.answer("Loading...")
            last_text = None
            while True:
                jobs = await get_jobs_list(user_id)
                # Group and format jobs using helper function
                combined_jobs = await group_and_sort_jobs(jobs)
                messages = []
                for job in combined_jobs:
                    props = job.get("Props", {})
                    batch = props.get("Batch", "Untitled")
                    display_batch = truncate_cell(batch)
                    total_tasks = props.get("Tasks", 0)
                    completed_chunks = job.get("CompletedChunks", 0)
                    progress_str = format_progress_old(completed_chunks, total_tasks)
                    stat = job.get("Stat", 0)
                    icon = "✅" if stat == 3 else "⏸️" if stat == 2 else "▶️"
                    messages.append(
                        f"{icon} {display_batch:<{BATCH_COLUMN_WIDTH}} {progress_str:^16}\n{'-'*40}"
                    )
                header = f"{'Batch':<{BATCH_COLUMN_WIDTH + 2}} {'Progress':^16}"
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

        from datetime import datetime
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
            from aiogram.types import FSInputFile
            
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
        await _prompt_render_method(
            callback_query,
            job_id,
            "Select a preview render method:",
        )
        with contextlib.suppress(Exception):
            await callback_query.answer()
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
