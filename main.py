# main.py
"""
Main application entry point with FastAPI integration for Telegram Mini App.

This module initializes both the Telegram bot and FastAPI server for mini app support.
"""

import asyncio
import logging
import uvicorn
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Import bot modules
from app.core.bot_core import dp, bot
from app.core.config import settings
from app.core.utils import on_startup, on_shutdown
from app.bot.handlers import register_handlers

# Import API routes
from app.integrations.api_routes import router as api_router

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create combined FastAPI app
app = FastAPI(title="TasksBot", version="1.0.0")

# Add CORS middleware
# Parse CORS origins from comma-separated string
cors_origins_list = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Include API routes directly
app.include_router(api_router, prefix="/api")

# Startup and shutdown events
def _log_bot_task_result(task: asyncio.Task) -> None:
    """Log unexpected results of the polling task."""
    try:
        task.result()
    except asyncio.CancelledError:
        logger.info("Bot polling task cancelled")
    except Exception:
        logger.exception("Bot polling task failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events."""
    logger.info("Starting TasksBot with API support...")

    register_handlers()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    bot_task = asyncio.create_task(dp.start_polling(bot, skip_updates=True))
    bot_task.add_done_callback(_log_bot_task_result)

    logger.info("TasksBot started successfully")

    try:
        yield
    finally:
        logger.info("Shutting down TasksBot...")
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            logger.debug("Bot polling task cancellation confirmed")
        except Exception:
            logger.exception("Error while awaiting bot polling task during shutdown")

# Set lifespan
app.router.lifespan_context = lifespan

# Health check endpoint
@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "TasksBot API",
        "version": "1.0.0",
        "status": "running"
    }

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok", "service": "tasksbot"}

if __name__ == "__main__":
    # Run with uvicorn
    reload_enabled = settings.dev_mode
    reload_kwargs = {}
    if reload_enabled:
        # Limit watch scope to source directories to avoid reloading on storage/temp writes
        reload_kwargs["reload_dirs"] = ["app", str(Path(__file__).resolve().parent)]

    logger.info("Starting uvicorn (reload=%s)", reload_enabled)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=reload_enabled,
        log_level="info",
        **reload_kwargs,
    )
