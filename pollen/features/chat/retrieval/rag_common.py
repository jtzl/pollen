"""rag_search shared foundation: config/env, logging, and the _domain_of helper."""

import collections
import json
import logging
import os
import threading
import time
import urllib.request
import shlex
import subprocess
import html as _html
import re as _re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse as _urlparse

from dotenv import load_dotenv

load_dotenv()


log = logging.getLogger("rag_search")

RAG_ENABLED = os.getenv("RAG_ENABLED", "false").lower() == "true"
RAG_MAX_RESULTS = int(os.getenv("RAG_MAX_RESULTS", "5"))
RAG_SOURCES = os.getenv("RAG_SOURCES", "").strip()
RAG_RATE_LIMIT = int(os.getenv("RAG_RATE_LIMIT", "10"))  # max searches per minute
RAG_SEARCH_TIMEOUT = int(os.getenv("RAG_SEARCH_TIMEOUT", "10"))  # seconds
RAG_FETCH_CHARS = int(os.getenv("RAG_FETCH_CHARS", "800"))  # max chars extracted per page
RAG_FETCH_TIMEOUT = float(os.getenv("RAG_FETCH_TIMEOUT", "5"))  # seconds per page
RAG_FETCH_WORKERS = int(os.getenv("RAG_FETCH_WORKERS", "6"))
PREFERRED_SOURCES = tuple(d.strip().lower() for d in os.getenv("PREFERRED_SOURCES", "").split(",") if d.strip())
# Built-in deprioritization list: ranked below curated/preferred but NOT blocked.
# Matches the bare domain and any subdomain via the existing endswith check in _source_rank.
_DEPRIORITIZED_DEFAULTS = (
    "wikipedia.org",
)
_DEPRIORITIZED_ENV = tuple(d.strip().lower() for d in os.getenv("DEPRIORITIZED_SOURCES", "").split(",") if d.strip())
# Merge defaults + env, de-duped, preserving order (defaults first).
DEPRIORITIZED_SOURCES = tuple(dict.fromkeys(_DEPRIORITIZED_DEFAULTS + _DEPRIORITIZED_ENV))



def _domain_of(url):
    try:
        from urllib.parse import urlparse
        h = urlparse(url).hostname or ""
        return h.lower().lstrip(".").removeprefix("www.")
    except Exception:
        return ""


_FETCH_UA = "Pollen-RAG/1.0 (+https://makeyouraismarter.com)"


__all__ = [
    'log',
    'RAG_ENABLED',
    'RAG_MAX_RESULTS',
    'RAG_SOURCES',
    'RAG_RATE_LIMIT',
    'RAG_SEARCH_TIMEOUT',
    'RAG_FETCH_CHARS',
    'RAG_FETCH_TIMEOUT',
    'RAG_FETCH_WORKERS',
    'PREFERRED_SOURCES',
    '_DEPRIORITIZED_DEFAULTS',
    '_DEPRIORITIZED_ENV',
    'DEPRIORITIZED_SOURCES',
    '_domain_of',
    '_FETCH_UA',
]
