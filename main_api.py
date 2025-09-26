# main_api.py
"""
Main application entry point with FastAPI integration for Telegram Mini App.

This module initializes both the Telegram bot and FastAPI server for mini app support.
"""

import asyncio
import logging
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Import bot modules
from app.core.bot_core import dp, bot
from app.utils import on_startup, on_shutdown
from app.handlers import register_handlers

# Import API routes
from app.api_routes import router as api_router

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create combined FastAPI app
app = FastAPI(title="TasksBot", version="1.0.0")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict to explicit domains in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes directly
app.include_router(api_router, prefix="/api")

# Startup and shutdown events
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events"""
    # Startup
    logger.info("Starting TasksBot with API support...")
    
    # Register bot handlers
    register_handlers()
    
    # Setup bot startup and shutdown handlers
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    # Start bot polling in background
    bot_task = asyncio.create_task(dp.start_polling(bot, skip_updates=True))
    
    logger.info("TasksBot started successfully")
    
    yield
    
    # Shutdown
    logger.info("Shutting down TasksBot...")
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass

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
    uvicorn.run(
        "main_api:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    ) 
