"""Dropbox integration service functions."""

import logging

import aiohttp

from app.core.bot_core import get_aiosession

logger = logging.getLogger(__name__)


async def get_dropbox_session() -> aiohttp.ClientSession:
    """Get an active aiohttp session for Dropbox API calls."""
    try:
        return await get_aiosession()
    except Exception as e:
        logger.error(f"Error getting Dropbox session: {e}")
        raise RuntimeError(f"Failed to get Dropbox session: {e}")
