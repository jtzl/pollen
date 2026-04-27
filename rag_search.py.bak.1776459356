"""Web search module using DuckDuckGo for RAG pipeline."""

import collections
import logging
import os
import threading
import time

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("rag_search")

RAG_ENABLED = os.getenv("RAG_ENABLED", "false").lower() == "true"
RAG_MAX_RESULTS = int(os.getenv("RAG_MAX_RESULTS", "5"))
RAG_SOURCES = os.getenv("RAG_SOURCES", "").strip()
RAG_RATE_LIMIT = int(os.getenv("RAG_RATE_LIMIT", "10"))  # max searches per minute
RAG_SEARCH_TIMEOUT = int(os.getenv("RAG_SEARCH_TIMEOUT", "10"))  # seconds

# Rate limiter: track timestamps of recent searches
_search_timestamps = collections.deque()
_rate_lock = threading.Lock()


def _is_rate_limited():
    """Check if we've exceeded RAG_RATE_LIMIT searches in the last 60 seconds."""
    now = time.time()
    with _rate_lock:
        # Remove timestamps older than 60 seconds
        while _search_timestamps and _search_timestamps[0] < now - 60:
            _search_timestamps.popleft()
        if len(_search_timestamps) >= RAG_RATE_LIMIT:
            return True
        _search_timestamps.append(now)
        return False


def _build_source_filter():
    """Build a site: filter string from RAG_SOURCES env var."""
    if not RAG_SOURCES:
        return ""
    domains = [d.strip() for d in RAG_SOURCES.split(",") if d.strip()]
    if not domains:
        return ""
    return " " + " OR ".join(f"site:{d}" for d in domains)


def is_enabled():
    return RAG_ENABLED


def search(query, max_results=None):
    """Search DuckDuckGo and return top results with snippets.

    Returns a list of dicts: [{"title": ..., "url": ..., "snippet": ...}, ...]
    Returns [] silently on rate limit, timeout, or any failure.
    """
    if not RAG_ENABLED:
        return []

    if max_results is None:
        max_results = RAG_MAX_RESULTS

    if _is_rate_limited():
        log.warning("RAG search rate limited (max %d/min), skipping query=%r", RAG_RATE_LIMIT, query)
        return []

    try:
        from ddgs import DDGS

        source_filter = _build_source_filter()
        filtered_query = query + source_filter
        log.info("RAG search starting: query=%r, filter=%r, max_results=%d", query, source_filter.strip(), max_results)
        start = time.time()
        with DDGS() as ddgs:
            raw = list(ddgs.text(filtered_query, max_results=max_results, backend="html"))
        elapsed = time.time() - start

        if elapsed > RAG_SEARCH_TIMEOUT:
            log.warning("RAG search slow (%.1fs > %ds) for query=%r", elapsed, RAG_SEARCH_TIMEOUT, query)

        results = []
        for r in raw:
            results.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            })

        log.info("RAG search complete: query=%r, %d results in %.1fs", query, len(results), elapsed)
        for i, r in enumerate(results):
            log.info("  result[%d]: %s - %s", i, r["title"][:60], r["url"])
        if not results:
            log.warning("RAG search returned 0 results for query=%r (filtered_query=%r)", query, filtered_query)
        return results

    except Exception as e:
        log.error("RAG search failed for query=%r: %s (response will proceed without search)", query, e)
        return []
