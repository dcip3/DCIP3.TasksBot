# app/core/bot_core.py
"""
Bot instance, dispatcher, and shared state initialization.

This module provides the bot and dispatcher instances along with shared
variables that can be imported by other modules without causing circular imports.
"""

import asyncio
from typing import Dict, Set, Any
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from app.core.config import settings

# Bot and dispatcher instances
bot = Bot(token=settings.tg_api_token)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Global state variables
download_states: Dict[str, Dict[str, Any]] = {}
stop_downloads: Dict[str, bool] = {}
notified_jobs: Set[str] = set()
active_realtime_tasks: Dict[str, asyncio.Task] = {}
current_downloads: int = 0
conversion_semaphore = asyncio.Semaphore(1) 