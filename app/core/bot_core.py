#!/usr/bin/env python3
"""
Bot instance, dispatcher, and shared state initialization.

This module provides the bot and dispatcher instances along with shared
variables that can be imported by other modules without causing circular imports.
"""

import asyncio
from typing import Dict, Any
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from app.core.config import settings
from app.core.ttl_cache import TTLCache
import aiohttp
import logging

# Bot and dispatcher instances
bot = Bot(token=settings.telegram_bot_token)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Global state variables
download_states: Dict[str, Dict[str, Any]] = {}
stop_downloads: Dict[str, asyncio.Event] = {}

# Use TTL cache for notified jobs to prevent memory leak
# Stores (job_id, user_id) pairs with 1 hour TTL
# After 1 hour, notifications for the same job can be sent again
notified_jobs = TTLCache(ttl_seconds=3600, max_size=10000)
auto_preview_jobs = TTLCache(ttl_seconds=3600, max_size=10000)

current_downloads: int = 0
conversion_semaphore = asyncio.Semaphore(1)

# Global aiohttp session shared across Dropbox and other APIs
aiosession: aiohttp.ClientSession | None = None

async def init_aiosession():
    global aiosession
    if aiosession is None:
        logging.info("[init_aiosession] Creating new aiohttp.ClientSession...")
        timeout = aiohttp.ClientTimeout(total=3600)
        connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
        aiosession = aiohttp.ClientSession(timeout=timeout, connector=connector)
        logging.info(f"[init_aiosession] aiosession created: {aiosession}")
    else:
        logging.info("[init_aiosession] aiosession already exists.")

async def close_aiosession():
    global aiosession
    if aiosession:
        logging.info(f"[close_aiosession] Closing aiosession: {aiosession}")
        await aiosession.close()
        aiosession = None
        logging.info("[close_aiosession] aiosession closed.")
    else:
        logging.info("[close_aiosession] aiosession was already None.")

async def get_aiosession() -> aiohttp.ClientSession:
    """
    Get or create global aiohttp session.
    
    Returns:
        aiohttp.ClientSession: Global session instance
        
    Raises:
        RuntimeError: If unable to create session
    """
    global aiosession
    await init_aiosession()
    if aiosession is None:
        raise RuntimeError("Failed to initialize aiohttp session")
    return aiosession 
