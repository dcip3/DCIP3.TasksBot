import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.job_helpers import (
    BATCH_COLUMN_WIDTH,
    JOBS_PAGE_SIZE,
    format_progress_old,
    group_and_sort_jobs,
    truncate_cell,
)
from app.core.config import settings
from app.core.utils import authorized_only
from app.services import (
    delete_job_by_user_id,
    get_job_tasks_by_user_id,
    get_jobs_list,
    get_workers_list,
    requeue_job_by_user_id,
    resume_job_by_user_id,
    suspend_job_by_user_id,
)

logger = logging.getLogger(__name__)

router = Router()


@router.message(F.text == "📂 Jobs")
@authorized_only
async def handle_jobs(message: Message, page: int = 0) -> None:
    """Display list of jobs with pagination."""
    if message.from_user is None:
        await message.answer("Error: User information not available.")
        return

    logger.info("Jobs button pressed by user %s", message.from_user.id)

    try:
        jobs = await get_jobs_list(message.from_user.id)
        if not jobs:
            await message.answer(
                "No jobs found. This could mean:\n"
                "• There are no active jobs in Deadline\n"
                "• Your user doesn't have access to jobs\n"
                "• There was an API error (check logs)"
            )
            return

        combined_jobs = await group_and_sort_jobs(jobs)
        jobs_slice = combined_jobs[page * JOBS_PAGE_SIZE : page * JOBS_PAGE_SIZE + JOBS_PAGE_SIZE]

        messages = []
        buttons = []

        def resolve_batch_label(props: dict) -> str:
            batch = (props.get("Batch") or "").strip()
            if batch:
                return batch
            name = (props.get("Name") or "Untitled").strip()
            return name or "Untitled"

        for job in jobs_slice:
            props = job.get("Props", {})
            batch = resolve_batch_label(props)
            display_batch = truncate_cell(batch)
            total_tasks = props.get("Tasks", 0)
            completed_chunks = job.get("CompletedChunks", 0)
            progress_str = format_progress_old(completed_chunks, total_tasks)
            stat = job.get("Stat", 0)
            icon = "✅" if stat == 3 else "⏸️" if stat == 2 else "▶️"

            messages.append(
                f"{icon} {display_batch:<{BATCH_COLUMN_WIDTH}} {progress_str:^16}\n{'-'*40}"
            )

            job_id = job.get("_id")
            if job_id:
                buttons.append(
                    InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}")
                )

        header = f"{'Batch':<{BATCH_COLUMN_WIDTH + 2}} {'Progress':^16}"
        header += f"\n{'-'*40}"
        batch_text = "\n".join(messages) if messages else "No data"

        if buttons:
            inline_keyboard = []
            row = []
            for idx, button in enumerate(buttons, 1):
                row.append(button)
                if idx % 2 == 0:
                    inline_keyboard.append(row)
                    row = []
            if row:
                inline_keyboard.append(row)

            total_items = len(combined_jobs)
            total_pages = (total_items + JOBS_PAGE_SIZE - 1) // JOBS_PAGE_SIZE
            nav_buttons = []
            if page > 0:
                nav_buttons.append(
                    InlineKeyboardButton(text="⬅️ Back", callback_data=f"jobs_page:{page-1}")
                )
            if (page + 1) < total_pages:
                nav_buttons.append(
                    InlineKeyboardButton(text="Next ➡️", callback_data=f"jobs_page:{page+1}")
                )
            if nav_buttons:
                inline_keyboard.append(nav_buttons)

            keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
            page_info = f"Page {page+1} of {total_pages}"

            await message.answer(
                f"<pre>{header}\n{batch_text}\n{page_info}</pre>\n\nSelect a job for details:",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await message.answer(f"<pre>{header}\n{batch_text}</pre>", parse_mode="HTML")

    except Exception as exc:
        logger.exception("Error handling jobs for user %s", message.from_user.id)
        await message.answer("Error occurred while fetching jobs.")


@router.callback_query(lambda c: c.data and c.data.startswith("jobs_page:"))
async def jobs_page_callback(callback_query: CallbackQuery) -> None:
    """Handle pagination for the jobs list."""
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

        combined_jobs = await group_and_sort_jobs(jobs)
        jobs_slice = combined_jobs[page * JOBS_PAGE_SIZE : page * JOBS_PAGE_SIZE + JOBS_PAGE_SIZE]

        messages = []
        buttons = []

        def resolve_batch_label(props: dict) -> str:
            batch = (props.get("Batch") or "").strip()
            if batch:
                return batch
            name = (props.get("Name") or "Untitled").strip()
            return name or "Untitled"

        for job in jobs_slice:
            props = job.get("Props", {})
            batch = resolve_batch_label(props)
            display_batch = truncate_cell(batch)
            total_tasks = props.get("Tasks", 0)
            completed_chunks = job.get("CompletedChunks", 0)
            progress_str = format_progress_old(completed_chunks, total_tasks)
            stat = job.get("Stat", 0)
            icon = "✅" if stat == 3 else "⏸️" if stat == 2 else "▶️"

            messages.append(
                f"{icon} {display_batch:<{BATCH_COLUMN_WIDTH}} {progress_str:^16}\n{'-'*40}"
            )

            job_id = job.get("_id")
            if job_id:
                buttons.append(
                    InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}")
                )

        header = f"{'Batch':<{BATCH_COLUMN_WIDTH + 2}} {'Progress':^16}"
        header += f"\n{'-'*40}"
        batch_text = "\n".join(messages) if messages else "No data"

        if buttons:
            inline_keyboard = []
            row = []
            for idx, button in enumerate(buttons, 1):
                row.append(button)
                if idx % 2 == 0:
                    inline_keyboard.append(row)
                    row = []
            if row:
                inline_keyboard.append(row)

            total_items = len(combined_jobs)
            total_pages = (total_items + JOBS_PAGE_SIZE - 1) // JOBS_PAGE_SIZE
            nav_buttons = []
            if page > 0:
                nav_buttons.append(
                    InlineKeyboardButton(text="⬅️ Back", callback_data=f"jobs_page:{page-1}")
                )
            if (page + 1) < total_pages:
                nav_buttons.append(
                    InlineKeyboardButton(text="Next ➡️", callback_data=f"jobs_page:{page+1}")
                )
            if nav_buttons:
                inline_keyboard.append(nav_buttons)

            keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
            page_info = f"Page {page+1} of {total_pages}"

            await callback_query.message.edit_text(
                f"<pre>{header}\n{batch_text}\n{page_info}</pre>\n\nSelect a job for details:",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await callback_query.message.edit_text(
                f"<pre>{header}\n{batch_text}</pre>", parse_mode="HTML"
            )

    except Exception as exc:
        logger.exception(
            "Error handling jobs page for user %s", callback_query.from_user.id
        )
        await callback_query.message.edit_text("Error occurred while fetching jobs.")


@router.callback_query(lambda c: c.data == "jobs_back")
async def jobs_back_callback(callback_query: CallbackQuery) -> None:
    """Return from job info view back to the first jobs page."""
    await callback_query.answer()

    if callback_query.from_user is None or callback_query.message is None:
        return

    try:
        jobs = await get_jobs_list(callback_query.from_user.id)
        if not jobs:
            await callback_query.message.edit_text("No jobs found.")
            return

        combined_jobs = await group_and_sort_jobs(jobs)
        jobs_slice = combined_jobs[:JOBS_PAGE_SIZE]

        messages = []
        buttons = []

        def resolve_batch_label(props: dict) -> str:
            batch = (props.get("Batch") or "").strip()
            if batch:
                return batch
            name = (props.get("Name") or "Untitled").strip()
            return name or "Untitled"

        for job in jobs_slice:
            props = job.get("Props", {})
            batch = resolve_batch_label(props)
            display_batch = truncate_cell(batch)
            total_tasks = props.get("Tasks", 0)
            completed_chunks = job.get("CompletedChunks", 0)
            progress_str = format_progress_old(completed_chunks, total_tasks)
            stat = job.get("Stat", 0)
            icon = "✅" if stat == 3 else "⏸️" if stat == 2 else "▶️"

            messages.append(
                f"{icon} {display_batch:<{BATCH_COLUMN_WIDTH}} {progress_str:^16}\n{'-'*40}"
            )

            job_id = job.get("_id")
            if job_id:
                buttons.append(
                    InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}")
                )

        header = f"{'Batch':<{BATCH_COLUMN_WIDTH + 2}} {'Progress':^16}"
        header += f"\n{'-'*40}"
        batch_text = "\n".join(messages) if messages else "No data"

        if buttons:
            inline_keyboard = []
            row = []
            for idx, button in enumerate(buttons, 1):
                row.append(button)
                if idx % 2 == 0:
                    inline_keyboard.append(row)
                    row = []
            if row:
                inline_keyboard.append(row)

            total_items = len(combined_jobs)
            total_pages = (total_items + JOBS_PAGE_SIZE - 1) // JOBS_PAGE_SIZE
            nav_buttons = []
            if (0 + 1) < total_pages:
                nav_buttons.append(
                    InlineKeyboardButton(text="➡️ Next", callback_data="jobs_page:1")
                )
            if nav_buttons:
                inline_keyboard.append(nav_buttons)

            keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
            page_info = f"Page 1 of {total_pages}"

            await callback_query.message.edit_text(
                f"<pre>{header}\n{batch_text}\n{page_info}</pre>\n\nSelect a job for details:",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await callback_query.message.edit_text(
                f"<pre>{header}\n{batch_text}</pre>", parse_mode="HTML"
            )

    except Exception as exc:
        logger.exception(
            "Error handling jobs back for user %s", callback_query.from_user.id
        )
        await callback_query.message.edit_text("Error occurred while fetching jobs.")


@router.callback_query(lambda c: c.data and c.data.startswith("job_info:"))
async def job_info_callback(callback_query: CallbackQuery) -> None:
    """Show detailed information for the selected job."""
    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return

    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        from app.services import get_jobs_list as fetch_jobs_list

        def resolve_batch_label(props: dict) -> str:
            batch = (props.get("Batch") or "").strip()
            if batch:
                return batch
            name = (props.get("Name") or "Untitled").strip()
            return name or "Untitled"

        all_jobs = await fetch_jobs_list(callback_query.from_user.id)
        if not all_jobs:
            await callback_query.answer("Failed to get jobs list.", show_alert=True)
            return

        selected_job = next((j for j in all_jobs if j.get("_id") == job_id), None)
        if not selected_job:
            await callback_query.answer("Job not found.", show_alert=True)
            return

        props = selected_job.get("Props", {})
        batch_name = resolve_batch_label(props)

        batch_jobs = [
            j for j in all_jobs
            if resolve_batch_label(j.get("Props", {})) == batch_name
        ]

        await callback_query.message.delete()

        job_tasks_map = {}
        tasks_futures = []
        for job in batch_jobs:
            batch_job_id = job.get("_id")
            if batch_job_id:
                tasks_futures.append(
                    (
                        batch_job_id,
                        get_job_tasks_by_user_id(callback_query.from_user.id, batch_job_id),
                    )
                )

        if tasks_futures:
            results = await asyncio.gather(
                *[future for _, future in tasks_futures], return_exceptions=True
            )
            for (batch_job_id, _), tasks in zip(tasks_futures, results):
                if isinstance(tasks, Exception):
                    logger.error("Error loading tasks for job %s: %s", batch_job_id, tasks)
                    job_tasks_map[batch_job_id] = []
                else:
                    job_tasks_map[batch_job_id] = tasks or []

        for job in batch_jobs:
            props = job.get("Props", {})
            total_tasks = props.get("Tasks", 0)
            completed_chunks = job.get("CompletedChunks", 0)
            progress_str = format_progress_old(completed_chunks, total_tasks)
            full_name = props.get("Name", "Untitled")
            name = full_name.split("/")[-1] if "/" in full_name else full_name
            stat = job.get("Stat", 0)
            stat_name = settings.job_status_map.get(stat, "Unknown")

            eta_str = "N/A"
            try:
                tasks = job_tasks_map.get(job.get("_id"), [])
                if tasks:
                    durations = []
                    for task in tasks:
                        if task.get("Stat") == 5:
                            start_str = task.get("StartRen")
                            comp_str = task.get("Comp")
                            if (
                                start_str
                                and comp_str
                                and start_str != "0001-01-01T00:00:00Z"
                                and comp_str != "0001-01-01T00:00:00Z"
                            ):
                                try:
                                    start_time = datetime.fromisoformat(start_str)
                                    comp_time = datetime.fromisoformat(comp_str)
                                    duration_val = (comp_time - start_time).total_seconds()
                                    durations.append(duration_val)
                                except Exception:  # pragma: no cover - defensive
                                    pass

                    if durations:
                        avg_duration = sum(durations) / len(durations)
                        remaining = total_tasks - completed_chunks
                        total_eta_seconds = avg_duration * remaining
                        if total_eta_seconds > 0:
                            eta_td = timedelta(seconds=int(total_eta_seconds))
                            eta_str = str(eta_td)
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Error calculating ETA for job %s: %s", job.get("_id"), exc)
                eta_str = "N/A"

            info_text = f"<pre>Job Info:\n{'-'*40}\n"
            info_text += f"Batch: {batch_name}\n"
            info_text += f"Name: {name}\n"
            info_text += f"Status: {stat_name}\n"
            info_text += f"Progress: {progress_str}\n"
            info_text += f"ETA: {eta_str}\n"
            info_text += f"{'-'*40}</pre>"

            current_job_id = job.get("_id")
            buttons = []
            if stat != 3:
                if stat == 2:
                    buttons.append(
                        InlineKeyboardButton(
                            text="▶️ Resume", callback_data=f"resume_job:{current_job_id}"
                        )
                    )
                else:
                    buttons.append(
                        InlineKeyboardButton(
                            text="⏸️ Suspend", callback_data=f"suspend_job:{current_job_id}"
                        )
                    )
                buttons.append(
                    InlineKeyboardButton(
                        text="🔄 Requeue", callback_data=f"requeue_job:{current_job_id}"
                    )
                )

            buttons.append(
                InlineKeyboardButton(text="🔍 Preview", callback_data=f"preview_job:{current_job_id}")
            )
            buttons.append(
                InlineKeyboardButton(text="🗑️ Delete", callback_data=f"delete_job:{current_job_id}")
            )
            buttons.append(
                InlineKeyboardButton(text="📋 Tasks", callback_data=f"tasks_job:{current_job_id}")
            )

            inline_keyboard = []
            row = []
            for idx, button in enumerate(buttons, 1):
                row.append(button)
                if idx % 2 == 0:
                    inline_keyboard.append(row)
                    row = []
            if row:
                inline_keyboard.append(row)

            inline_keyboard.append(
                [InlineKeyboardButton(text="⬅️ Back", callback_data="jobs_back")]
            )

            keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)

            await callback_query.message.answer(
                info_text + "\nActions:", parse_mode="HTML", reply_markup=keyboard
            )

        await callback_query.answer()

    except Exception as exc:
        logger.exception(
            "Error handling job info for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while fetching job info.", show_alert=True)


@router.message(F.text == "🖥️ Workers")
@authorized_only
async def handle_workers(message: Message) -> None:
    """Display worker list."""
    if message.from_user is None:
        await message.answer("Error: User information not available.")
        return

    logger.info("Workers button pressed by user %s", message.from_user.id)

    try:
        workers = await get_workers_list(message.from_user.id)
        if not workers:
            await message.answer(
                "No workers (slaves) found. This could mean:\n"
                "• There are no active workers in Deadline\n"
                "• Your user doesn't have access to workers\n"
                "• There was an API error (check logs)"
            )
            return

        lines = []
        for worker in workers:
            info = worker.get("Info", {})
            name = info.get("Name", "Unknown")
            stat_num = info.get("Stat", 0)
            stat = settings.worker_status_map.get(stat_num, f"Unknown ({stat_num})")
            lines.append(f"{name:<24} {stat}")

        header = f"{'Name':<24} Status"
        header += f"\n{'-'*40}"
        body = "\n".join(lines) if lines else "No data"

        await message.answer(f"<pre>{header}\n{body}</pre>", parse_mode="HTML")

    except Exception as exc:
        logger.exception(
            "Error handling workers for user %s", message.from_user.id
        )
        await message.answer("Error occurred while fetching workers.")


@router.callback_query(lambda c: c.data and c.data.startswith("requeue_job:"))
async def requeue_job_callback(callback_query: CallbackQuery) -> None:
    """Handle requeue job button press."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        success = await requeue_job_by_user_id(callback_query.from_user.id, job_id)
        if success:
            await callback_query.answer("Job requeued successfully!")
        else:
            await callback_query.answer("Failed to requeue job.", show_alert=True)
    except Exception as exc:
        logger.exception(
            "Error requeuing job for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while requeuing job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("resume_job:"))
async def resume_job_callback(callback_query: CallbackQuery) -> None:
    """Handle resume job button press."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        success = await resume_job_by_user_id(callback_query.from_user.id, job_id)
        if success:
            await callback_query.answer("Job resumed successfully!")
        else:
            await callback_query.answer("Failed to resume job.", show_alert=True)
    except Exception as exc:
        logger.exception(
            "Error resuming job for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while resuming job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("suspend_job:"))
async def suspend_job_callback(callback_query: CallbackQuery) -> None:
    """Handle suspend job button press."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        success = await suspend_job_by_user_id(callback_query.from_user.id, job_id)
        if success:
            await callback_query.answer("Job suspended successfully!")
        else:
            await callback_query.answer("Failed to suspend job.", show_alert=True)
    except Exception as exc:
        logger.exception(
            "Error suspending job for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while suspending job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("delete_job:"))
async def delete_job_callback(callback_query: CallbackQuery) -> None:
    """Handle delete job button press."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        success = await delete_job_by_user_id(callback_query.from_user.id, job_id)
        if success:
            await callback_query.answer("Job deleted successfully!")
            if callback_query.message:
                await callback_query.message.edit_text("Job has been deleted.")
        else:
            await callback_query.answer("Failed to delete job.", show_alert=True)
    except Exception as exc:
        logger.exception(
            "Error deleting job for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while deleting job.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("tasks_job:"))
async def tasks_job_callback(callback_query: CallbackQuery) -> None:
    """Show task breakdown for a job."""
    if callback_query.from_user is None or callback_query.data is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    job_id = callback_query.data.split(":", 1)[1]

    try:
        tasks = await get_job_tasks_by_user_id(callback_query.from_user.id, job_id)
        if not tasks:
            if callback_query.message:
                await callback_query.message.answer("No tasks found for this job.")
            await callback_query.answer()
            return

        lines = []
        header = f"{'Frames':<18} {'Prog':^10} {'Time':^12}"
        header += f"\n{'-'*42}"
        lines.append(header)

        def get_task_icon(stat: int) -> str:
            if stat == 5:
                return "✅"
            if stat == 4:
                return "▶️"
            if stat == 3:
                return "⏸️"
            if stat == 6:
                return "❌"
            if stat in (2, 8):
                return "⏳"
            return "❓"

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
                    if stat == 5:
                        comp_str = task.get("Comp")
                        if comp_str and comp_str != "0001-01-01T00:00:00Z":
                            comp_time = datetime.fromisoformat(comp_str)
                            duration = comp_time.astimezone(timezone.utc) - start_time.astimezone(
                                timezone.utc
                            )
                            rendertime_str = str(duration).split(".")[0]
                    elif stat == 4:
                        now_utc = datetime.now(timezone.utc)
                        duration = now_utc - start_time.astimezone(timezone.utc)
                        rendertime_str = str(duration).split(".")[0]
                except Exception:  # pragma: no cover - defensive
                    pass
            line = f"{icon} {frames:<16} {prog:^10} {rendertime_str:^12}"
            lines.append(line)

        message_text = "<pre>" + "\n".join(lines) + "</pre>"
        if callback_query.message:
            await callback_query.message.answer(message_text, parse_mode="HTML")
        await callback_query.answer()

    except Exception as exc:
        logger.exception(
            "Error getting tasks for user %s", callback_query.from_user.id
        )
        await callback_query.answer("Error occurred while fetching tasks.", show_alert=True)
