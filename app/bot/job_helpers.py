# app/bot/job_helpers.py
"""
Helper functions for job processing and formatting.

This module provides utilities for grouping, sorting, and formatting
job data for display in the Telegram bot.
"""

from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List

BATCH_COLUMN_WIDTH = 22
JOBS_PAGE_SIZE = 6


async def compute_batch_activity_timestamp(batch_jobs: list[dict]) -> datetime:
    """
    Return the batch creation timestamp based on job-level metadata.

    Args:
        batch_jobs: List of jobs in the batch

    Returns:
        Latest activity timestamp or datetime.min if no valid dates
    """
    for job in sorted(batch_jobs, key=lambda j: j.get('Date') or '', reverse=True):
        date_value = job.get('Date') if isinstance(job, dict) else None
        if not date_value:
            continue
        try:
            return datetime.fromisoformat(date_value)
        except ValueError:
            continue
    return datetime.min


def group_and_combine_jobs(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Group jobs by batch name and combine their statistics.

    This function takes a list of jobs and:
    1. Groups them by batch name
    2. Sums up total tasks and completed chunks
    3. Determines overall batch status based on priority
    4. Returns a sorted list of combined job data

    Args:
        jobs: List of job dictionaries from Deadline API

    Returns:
        List of combined job dictionaries sorted by activity (newest first)
    """
    # Group jobs by batch name
    grouped_jobs = defaultdict(list)
    for job in jobs:
        batch = job.get("Props", {}).get("Batch", "Untitled")
        grouped_jobs[batch].append(job)

    # Combine jobs by batch
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

        combined_jobs.append({
            "_id": batch_jobs[0].get("_id"),
            "Props": {"Batch": batch, "Tasks": total_tasks},
            "CompletedChunks": completed_chunks,
            "Stat": batch_stat,
            "DateParsed": None,  # Will be populated later
            "_batch_jobs": batch_jobs  # Store original jobs for reference
        })

    return combined_jobs


async def group_and_sort_jobs(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Group jobs by batch and sort by activity timestamp.

    This is the main function that combines grouping, timestamp calculation,
    and sorting into one call.

    Args:
        jobs: List of job dictionaries from Deadline API

    Returns:
        List of combined and sorted job dictionaries (newest first)
    """
    combined_jobs = group_and_combine_jobs(jobs)

    # Calculate and set activity timestamps
    for job in combined_jobs:
        batch_jobs = job.get("_batch_jobs", [])
        if batch_jobs:
            job["DateParsed"] = await compute_batch_activity_timestamp(batch_jobs)
        else:
            job["DateParsed"] = datetime.min

    # Sort by DateParsed descending (newest first)
    combined_jobs.sort(key=lambda j: j["DateParsed"], reverse=True)

    return combined_jobs


def truncate_cell(text: str, max_width: int = BATCH_COLUMN_WIDTH) -> str:
    """
    Ensure table cell content fits the allocated width.

    Args:
        text: Original string to display
        max_width: Maximum allowed characters for the column

    Returns:
        Possibly truncated string with ellipsis if it exceeds the width
    """
    text = str(text)
    if len(text) <= max_width:
        return text
    if max_width <= 4:
        return text[:max_width]

    ellipsis = "..."
    tail_len = min(3, len(text))
    prefix_len = max_width - len(ellipsis) - tail_len

    if prefix_len < 1:
        tail_len = max_width - len(ellipsis) - 1
        if tail_len < 1:
            return text[:max_width]
        prefix_len = 1

    return text[:prefix_len] + ellipsis + text[-tail_len:]


def format_progress_old(completed: int, total: int) -> str:
    """
    Format progress in old style (like in an earlier bot).

    Args:
        completed: Number of completed items
        total: Total number of items

    Returns:
        Formatted progress string (e.g., "75% 15/20")
    """
    return f"{int((completed / total) * 100) if total else 0}% {completed}/{total}"
