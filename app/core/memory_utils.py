import gc
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger(__name__)

GC_RSS_THRESHOLD_MB = 1024


def get_process_rss_mb() -> Optional[float]:
    if not sys.platform.startswith("linux"):
        return None
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as fh:
            parts = fh.read().split()
        if len(parts) < 2:
            return None
        rss_pages = int(parts[1])
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        return (rss_pages * page_size) / (1024 * 1024)
    except Exception:
        return None


_batch_counters: dict[str, int] = {}


def maybe_collect_gc(
    *,
    threshold_mb: float = GC_RSS_THRESHOLD_MB,
    every_n: int = 1,
    counter_key: str = "default",
    log_context: str = "",
) -> None:
    """Trigger gc.collect() when process RSS exceeds threshold.

    every_n>1 batches checks across calls to avoid reading /proc on every batch.
    counter_key isolates batch counters between independent call sites.
    """
    if every_n > 1:
        count = _batch_counters.get(counter_key, 0) + 1
        if count < every_n:
            _batch_counters[counter_key] = count
            return
        _batch_counters[counter_key] = 0

    rss_mb = get_process_rss_mb()
    if rss_mb is None:
        return
    if rss_mb >= threshold_mb:
        suffix = f" {log_context}" if log_context else ""
        logger.debug("High RSS %.1fMB detected%s, triggering gc.collect()", rss_mb, suffix)
        gc.collect()
