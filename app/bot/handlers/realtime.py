import asyncio
import logging

from aiogram import F, Router
from aiogram.types import Message

from app.bot.job_helpers import (
    BATCH_COLUMN_WIDTH,
    format_progress_old,
    group_and_sort_jobs,
    truncate_cell,
)
from app.core.utils import authorized_only
from app.services import get_jobs_list

logger = logging.getLogger(__name__)

router = Router()


def stop_realtime_for_chat(chat_id: int) -> None:
    """Stop realtime updates for a specific chat."""
    if not hasattr(handle_realtime, "active_realtime_tasks"):
        return
    active_realtime_tasks = handle_realtime.active_realtime_tasks
    if chat_id in active_realtime_tasks:
        active_realtime_tasks[chat_id].cancel()
        del active_realtime_tasks[chat_id]


@router.message(F.text == "Realtime")
@authorized_only
async def handle_realtime(message: Message) -> None:
    """Handle Realtime button: show jobs list with auto-refresh every 5 seconds."""
    chat_id = message.chat.id
    user_id = message.from_user.id

    if not hasattr(handle_realtime, "active_realtime_tasks"):
        handle_realtime.active_realtime_tasks = {}
    active_realtime_tasks = handle_realtime.active_realtime_tasks

    if chat_id in active_realtime_tasks:
        active_realtime_tasks[chat_id].cancel()
        del active_realtime_tasks[chat_id]

    await message.answer("Tasks will be updated every 5 seconds until the next message.")

    async def realtime_loop() -> None:
        try:
            msg = await message.answer("Loading...")
            last_text = None
            while True:
                jobs = await get_jobs_list(user_id)
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
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Realtime loop error: %s", exc)
            await message.answer(f"Error: {exc}")

    task = asyncio.create_task(realtime_loop())
    active_realtime_tasks[chat_id] = task
