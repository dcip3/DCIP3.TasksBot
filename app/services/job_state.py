"""Shared runtime caches for job notification workflows."""

from app.core.ttl_cache import TTLCache

notified_jobs = TTLCache(ttl_seconds=3600, max_size=10000)
auto_preview_jobs = TTLCache(ttl_seconds=3600, max_size=10000)
