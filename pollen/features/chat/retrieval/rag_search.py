"""Backward-compatible shim.

rag_search.py was split by responsibility into sibling modules:
  rag_common.py     shared config, logging, and the _domain_of helper
  rag_curated.py    curated-source fetching (organized.info)
  rag_ranking.py    domain ranking / blocking / relevance scoring
  rag_content.py    page fetching and HTML-to-text extraction
  rag_fetchers.py   direct fetchers (node search over SSH, Wikipedia)
  rag_retrieval.py  search orchestration, query building, rate limiting

The original public API is re-exported so callers that do `import rag_search`
(rag_pipeline.py, websocket_api.py) need no change.
"""
from pollen.features.chat.retrieval.rag_common import (
    RAG_ENABLED, RAG_MAX_RESULTS, RAG_SOURCES, RAG_RATE_LIMIT,
    RAG_SEARCH_TIMEOUT, RAG_FETCH_CHARS, RAG_FETCH_TIMEOUT, RAG_FETCH_WORKERS,
    PREFERRED_SOURCES, DEPRIORITIZED_SOURCES,
)
from pollen.features.chat.retrieval.rag_curated import fetch_curated_domains, fetch_curated_sources, is_community_domain
from pollen.features.chat.retrieval.rag_ranking import is_blocked_domain
from pollen.features.chat.retrieval.rag_fetchers import node_search, fetch_wikipedia
from pollen.features.chat.retrieval.rag_retrieval import search, search_with_refine, is_enabled

__all__ = [
    "RAG_ENABLED", "RAG_MAX_RESULTS", "RAG_SOURCES", "RAG_RATE_LIMIT",
    "RAG_SEARCH_TIMEOUT", "RAG_FETCH_CHARS", "RAG_FETCH_TIMEOUT", "RAG_FETCH_WORKERS",
    "PREFERRED_SOURCES", "DEPRIORITIZED_SOURCES",
    "fetch_curated_domains", "fetch_curated_sources", "is_community_domain",
    "is_blocked_domain", "node_search", "fetch_wikipedia", "search", "search_with_refine", "is_enabled",
]
