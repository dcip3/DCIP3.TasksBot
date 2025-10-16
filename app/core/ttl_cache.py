# app/core/ttl_cache.py
"""
TTL (Time To Live) Cache implementation.

This module provides a cache that automatically removes old entries
after a specified time period to prevent memory leaks.
"""

import logging
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TTLCache:
    """
    Time-To-Live cache that automatically expires old entries.

    This cache stores items with timestamps and removes them after
    the specified TTL period. It's useful for preventing memory leaks
    from unbounded data structures.

    Example:
        >>> cache = TTLCache(ttl_seconds=3600)  # 1 hour TTL
        >>> cache.add("key1")
        >>> "key1" in cache  # True
        >>> # After 1 hour...
        >>> "key1" in cache  # False (automatically expired)
    """

    def __init__(self, ttl_seconds: int = 3600, max_size: Optional[int] = None):
        """
        Initialize TTL cache.

        Args:
            ttl_seconds: Time to live for cache entries in seconds (default: 3600 = 1 hour)
            max_size: Maximum number of entries (optional). When reached, oldest entries are removed.
        """
        self._cache: OrderedDict[Any, datetime] = OrderedDict()
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_size = max_size
        self._hits = 0
        self._misses = 0
        logger.info(
            f"TTL Cache initialized with ttl={ttl_seconds}s, max_size={max_size}"
        )

    def add(self, key: Any) -> None:
        """
        Add a key to the cache with current timestamp.

        Args:
            key: The key to add to the cache
        """
        # Remove expired entries before adding new one
        self._cleanup()

        # If key already exists, update its timestamp
        if key in self._cache:
            # Move to end (mark as recently used)
            self._cache.move_to_end(key)
            self._cache[key] = datetime.now()
        else:
            # Add new entry
            self._cache[key] = datetime.now()

            # If max_size is set and exceeded, remove oldest entry
            if self._max_size and len(self._cache) > self._max_size:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
                logger.debug(f"TTL Cache: removed oldest entry (max_size reached): {oldest_key}")

    def __contains__(self, key: Any) -> bool:
        """
        Check if key exists in cache and hasn't expired.

        Args:
            key: The key to check

        Returns:
            True if key exists and is not expired, False otherwise
        """
        if key not in self._cache:
            self._misses += 1
            return False

        # Check if expired
        timestamp = self._cache[key]
        if datetime.now() - timestamp >= self._ttl:
            # Expired - remove it
            del self._cache[key]
            self._misses += 1
            return False

        self._hits += 1
        return True

    def remove(self, key: Any) -> bool:
        """
        Manually remove a key from the cache.

        Args:
            key: The key to remove

        Returns:
            True if key was removed, False if it didn't exist
        """
        if key in self._cache:
            del self._cache[key]
            return True
        return False

    def _cleanup(self) -> int:
        """
        Remove all expired entries from the cache.

        Returns:
            Number of entries removed
        """
        cutoff = datetime.now() - self._ttl
        keys_to_remove = [
            key for key, timestamp in self._cache.items()
            if timestamp < cutoff
        ]

        for key in keys_to_remove:
            del self._cache[key]

        if keys_to_remove:
            logger.debug(f"TTL Cache: cleaned up {len(keys_to_remove)} expired entries")

        return len(keys_to_remove)

    def clear(self) -> None:
        """Remove all entries from the cache."""
        count = len(self._cache)
        self._cache.clear()
        logger.info(f"TTL Cache: cleared all {count} entries")

    def size(self) -> int:
        """
        Get current number of entries in cache (including expired ones).

        Returns:
            Number of entries in cache
        """
        return len(self._cache)

    def get_stats(self) -> dict:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache statistics
        """
        # Clean up first to get accurate count
        expired_count = self._cleanup()

        total_requests = self._hits + self._misses
        hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0

        return {
            "size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{hit_rate:.1f}%",
            "ttl_seconds": self._ttl.total_seconds(),
            "max_size": self._max_size,
            "expired_in_last_cleanup": expired_count,
        }

    def __len__(self) -> int:
        """Get current number of entries in cache."""
        return len(self._cache)

    def __repr__(self) -> str:
        """String representation of the cache."""
        return f"TTLCache(size={len(self._cache)}, ttl={self._ttl.total_seconds()}s)"
