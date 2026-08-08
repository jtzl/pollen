"""Curated-source fetching from organized.info (domains, sources, community detection)."""

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

from pollen.features.chat.retrieval.rag_common import *


# WordPress source curation: domains marked as authoritative get top ranking.
CURATED_DOMAINS_URL = os.getenv(
    "CURATED_DOMAINS_URL",
    "https://organized.info/wp-json/pollen/v1/sources/domains",
)
CURATED_DOMAINS_TTL = 300
# Use a shorter TTL on failure so a transient API blip doesn't pin us to the
# env fallback for a full hour.
CURATED_DOMAINS_FAILURE_TTL = 300

_curated_cache = {"domains": None, "expires_at": 0.0, "is_fallback": False}
_curated_lock = threading.Lock()


def fetch_curated_domains():
    """Return curated domain whitelist from the WordPress API.

    Cached for 1 hour on success, 5 min on failure. Falls back to
    PREFERRED_SOURCES (from .env) if the API is unreachable, returns
    a malformed payload, or yields zero usable domains.
    """
    now = time.time()
    if _curated_cache["domains"] is not None and now < _curated_cache["expires_at"]:
        return _curated_cache["domains"]

    with _curated_lock:
        if _curated_cache["domains"] is not None and time.time() < _curated_cache["expires_at"]:
            return _curated_cache["domains"]

        try:
            req = urllib.request.Request(
                CURATED_DOMAINS_URL,
                headers={"User-Agent": "Pollen-RAG/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))

            if isinstance(payload, list):
                raw = payload
            elif isinstance(payload, dict):
                raw = payload.get("domains") or payload.get("data") or []
            else:
                raw = []

            domains = []
            for item in raw:
                if isinstance(item, str):
                    d = item
                elif isinstance(item, dict):
                    d = item.get("domain") or item.get("name") or item.get("host") or ""
                else:
                    d = ""
                d = d.strip().lower().lstrip(".")
                if d.startswith("www."):
                    d = d[4:]
                if d:
                    domains.append(d)
            domains = tuple(dict.fromkeys(domains))

            if not domains:
                raise ValueError("curated domains response empty or malformed")

            _curated_cache["domains"] = domains
            _curated_cache["expires_at"] = time.time() + CURATED_DOMAINS_TTL
            _curated_cache["is_fallback"] = False
            log.info("Fetched %d curated domains from %s", len(domains), CURATED_DOMAINS_URL)
            try:
                fetch_curated_sources()
            except Exception:
                pass
            return domains
        except Exception as e:
            log.warning(
                "Curated domains fetch failed (%s); using PREFERRED_SOURCES fallback (%d domains)",
                e, len(PREFERRED_SOURCES),
            )
            _curated_cache["domains"] = PREFERRED_SOURCES
            _curated_cache["expires_at"] = time.time() + CURATED_DOMAINS_FAILURE_TTL
            _curated_cache["is_fallback"] = True
            return PREFERRED_SOURCES


# --- Curated sources (per-domain category metadata) ----------------------
# The /sources endpoint returns each curated entry with its category
# ("community", "science", "general", ...). Cached alongside the
# curated-domains cache and refreshed at the same cadence. Used by
# rag_pipeline to detect community-style sources and tweak the prompt.
CURATED_SOURCES_URL = os.getenv(
    "CURATED_SOURCES_URL",
    "https://organized.info/wp-json/pollen/v1/sources",
)

# Hardcoded fallback for well-known community sites. Always unioned with the
# API-derived community set so a curator forgetting to tag (say) reddit.com
# does not break the pipeline.
_COMMUNITY_FALLBACK_DOMAINS = frozenset({
    "reddit.com",
    "stackoverflow.com",
    "stackexchange.com",
    "serverfault.com",
    "superuser.com",
    "askubuntu.com",
    "news.ycombinator.com",
    "discord.com",
    "discord.gg",
    "discourse.org",
    "quora.com",
})

# Substring tells: anything containing these in the host is community-ish
# regardless of category. Matched after stripping leading "www.".
_COMMUNITY_DOMAIN_SUBSTRINGS = (
    "forum.", "forums.", ".forum.", ".forums.",
    "community.", ".community.",
)

_curated_sources_cache = {
    "by_domain": None,        # {domain: category(lower)}
    "community": None,        # frozenset of community-flagged domains
    "expires_at": 0.0,
    "is_fallback": False,
}
_curated_sources_lock = threading.Lock()


def fetch_curated_sources():
    """Return {domain: category} from the WP /sources endpoint.

    Cached with the same TTLs as the curated-domains cache (1h on
    success, 5m on failure). On failure the by-domain map is empty
    but the community set still resolves to the hardcoded fallback so
    is_community_domain() keeps working without the API.
    """
    now = time.time()
    if _curated_sources_cache["by_domain"] is not None and now < _curated_sources_cache["expires_at"]:
        return _curated_sources_cache["by_domain"]

    with _curated_sources_lock:
        if _curated_sources_cache["by_domain"] is not None and time.time() < _curated_sources_cache["expires_at"]:
            return _curated_sources_cache["by_domain"]

        by_domain = {}
        try:
            req = urllib.request.Request(
                CURATED_SOURCES_URL,
                headers={"User-Agent": "Pollen-RAG/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))

            if isinstance(payload, list):
                raw = payload
            elif isinstance(payload, dict):
                raw = payload.get("sources") or payload.get("data") or []
            else:
                raw = []

            for item in raw:
                if not isinstance(item, dict):
                    continue
                d = (item.get("domain") or item.get("name") or item.get("host") or "")
                d = d.strip().lower().lstrip(".")
                if d.startswith("www."):
                    d = d[4:]
                if not d:
                    continue
                cat = (item.get("category") or "").strip().lower()
                by_domain[d] = cat

            community = {d for d, c in by_domain.items() if "community" in c or "forum" in c}
            community |= _COMMUNITY_FALLBACK_DOMAINS

            _curated_sources_cache["by_domain"] = by_domain
            _curated_sources_cache["community"] = frozenset(community)
            _curated_sources_cache["expires_at"] = time.time() + CURATED_DOMAINS_TTL
            _curated_sources_cache["is_fallback"] = False
            log.info(
                "Fetched %d curated sources from %s (%d community)",
                len(by_domain), CURATED_SOURCES_URL, len(community),
            )
            return by_domain
        except Exception as e:
            log.warning(
                "Curated sources fetch failed (%s); community detection using hardcoded fallback (%d domains)",
                e, len(_COMMUNITY_FALLBACK_DOMAINS),
            )
            _curated_sources_cache["by_domain"] = {}
            _curated_sources_cache["community"] = frozenset(_COMMUNITY_FALLBACK_DOMAINS)
            _curated_sources_cache["expires_at"] = time.time() + CURATED_DOMAINS_FAILURE_TTL
            _curated_sources_cache["is_fallback"] = True
            return {}


def is_community_domain(domain_or_url):
    """True when the given URL/domain is a community source.

    Combines the API-tagged community set with a hardcoded fallback
    list and a couple of host-substring rules ("forum.", "community.")
    so well-known community sites are caught regardless of curation.
    """
    if not domain_or_url:
        return False
    s = str(domain_or_url).strip().lower()
    if "://" in s or s.startswith("//"):
        d = _domain_of(s)
    else:
        d = s.lstrip(".")
        if d.startswith("www."):
            d = d[4:]
    if not d:
        return False
    if _curated_sources_cache["community"] is None:
        # Lazily prime the cache. Failure here just means we use the
        # hardcoded fallback set, which is what the failure path of
        # fetch_curated_sources() installs anyway.
        try:
            fetch_curated_sources()
        except Exception:
            pass
    community = _curated_sources_cache["community"] or _COMMUNITY_FALLBACK_DOMAINS
    if d in community:
        return True
    for c in community:
        if d.endswith("." + c):
            return True
    for sub in _COMMUNITY_DOMAIN_SUBSTRINGS:
        if sub in d:
            return True
    return False
