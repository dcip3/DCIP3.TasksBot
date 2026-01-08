import logging

from app.core.bot_core import dp

from . import auth, common, jobs, preview, settings

logger = logging.getLogger(__name__)


def register_handlers() -> None:
    """Register all handlers with the dispatcher."""
    dp.include_router(auth.router)
    dp.include_router(jobs.router)
    dp.include_router(settings.router)
    dp.include_router(preview.router)
    dp.include_router(common.router)
    logger.info("All handlers registered")


__all__ = ["register_handlers"]
