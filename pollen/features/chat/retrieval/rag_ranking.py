"""Result ranking and filtering: domain rank/block, relevance scoring, keywords."""

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
from pollen.features.chat.retrieval.rag_curated import fetch_curated_domains
from pollen.core.config import (
    AUTHORITY_TIERS,
    AUTHORITY_ALLOW,
    AUTHORITY_DENY,
    AUTHORITY_ALLOW_BOOST,
    AUTHORITY_DENY_PENALTY,
    AUTHORITY_HIGH_TLDS,
    AUTHORITY_HIGH_SUBSTRINGS,
    AUTHORITY_HIGH_DOMAINS,
    AUTHORITY_FARM_DOMAINS,
)


# Sources to drop entirely from RAG results before they reach Mixtral.
# Matches exact domain and any subdomain (e.g. old.reddit.com, www.reddit.com).
BLOCKED_SOURCES = frozenset({
    "reddit.com",
    "quora.com",
    "answers.com",
})


def is_blocked_domain(domain_or_url):
    """True when the URL/domain matches a BLOCKED_SOURCES entry or any subdomain of one."""
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
    for b in BLOCKED_SOURCES:
        if d == b or d.endswith("." + b):
            return True
    return False


def _source_rank(url):
    """Lower rank = appears earlier.
    -1=curated (highest), 0=preferred (env), 1=neutral, 2=deprioritized.
    """
    d = _domain_of(url)
    if not d:
        return 1
    for cur in fetch_curated_domains():
        if d == cur or d.endswith("." + cur):
            return -1
    for pref in PREFERRED_SOURCES:
        if d == pref or d.endswith("." + pref):
            return 0
    for dep in DEPRIORITIZED_SOURCES:
        if d == dep or d.endswith("." + dep):
            return 2
    return 1


# Common English stopwords + query-filler words to ignore when extracting
# keywords for relevance scoring.
_RELEVANCE_STOPWORDS = frozenset([
    "a","an","and","or","the","of","to","in","on","at","for","is","are","was","were","be","been","being",
    "have","has","had","do","does","did","will","would","should","could","can","may","might",
    "i","you","he","she","it","we","they","this","that","these","those","me","us","them",
    "what","which","who","whom","whose","when","where","why","how",
    "my","your","his","her","its","our","their",
    "as","by","with","from","about","into","than","then","so","just","also","very",
    "tell","show","give","find","get","need","want","know","think","make","help","please",
    "good","best","top","great","nice","some","any","all","more","most","less",
    "vs","versus",
])


def _extract_keywords(query):
    """Return a list of unique lowercase keywords from the user query,
    excluding stopwords and tokens shorter than 3 characters."""
    if not query:
        return []
    tokens = _re.findall(r"[A-Za-z0-9]+", query.lower())
    seen = set()
    kws = []
    for t in tokens:
        if len(t) < 3 or t in _RELEVANCE_STOPWORDS:
            continue
        if t in seen:
            continue
        seen.add(t)
        kws.append(t)
    return kws


def _relevance_score(keywords, title, snippet):
    """Score how well a result matches the user query. Title hits are weighted
    double snippet hits so headline-relevant results win ties. Returns 0 when
    no keyword appears anywhere."""
    if not keywords:
        return 0
    title_l = (title or "").lower()
    snip_l = (snippet or "").lower()
    score = 0
    for kw in keywords:
        score += title_l.count(kw) * 2
        score += snip_l.count(kw)
    return score


def _keyword_coverage(keywords, title, snippet):
    """Topicality signal complementing _relevance_score's raw count: how many of
    the query's DISTINCT core keywords appear in title+snippet, and that count as
    a fraction of the total. Matching is WORD-START anchored (\\b + keyword): this
    still catches plurals/inflections ('artist' -> 'artists') but avoids the
    mid-word false positives that raw substring produces ('site' -> 'website',
    which otherwise let off-topic pages pass). Returns (n_distinct_matches, ratio)."""
    if not keywords:
        return 0, 0.0
    hay = ((title or "") + " " + (snippet or "")).lower()
    n = 0
    for kw in keywords:
        if _re.search(r"\b" + _re.escape(kw), hay):
            n += 1
    return n, (n / len(keywords))


def _authority_score(url):
    """Authority delta added to a result's relevance score to reorder results
    by source quality. Positive lifts a source, negative sinks it. Order of
    checks (first match wins): JT deny -> JT allow -> high-authority explicit
    domains -> high-authority TLD/academic patterns -> content-farm penalty ->
    neutral 0. Domain matching is exact or parent-domain (endswith "." + entry),
    mirroring _source_rank so subdomains are covered."""
    d = _domain_of(url)
    if not d:
        return 0

    def _hit(dom, entries):
        for e in entries:
            e = e.lstrip(".")
            if dom == e or dom.endswith("." + e):
                return True
        return False

    # JT explicit overrides first.
    if _hit(d, AUTHORITY_DENY):
        return AUTHORITY_DENY_PENALTY
    if _hit(d, AUTHORITY_ALLOW):
        return AUTHORITY_ALLOW_BOOST

    # High-authority: explicit journal/news/reference domains.
    if _hit(d, AUTHORITY_HIGH_DOMAINS):
        return AUTHORITY_TIERS["high"]
    # High-authority: gov/edu/academic TLD suffixes and ".ac." academic hosts.
    for tld in AUTHORITY_HIGH_TLDS:
        if d.endswith(tld):
            return AUTHORITY_TIERS["high"]
    for sub in AUTHORITY_HIGH_SUBSTRINGS:
        if sub in d:
            return AUTHORITY_TIERS["high"]

    # Known content-farm / SEO / scraper domains.
    if _hit(d, AUTHORITY_FARM_DOMAINS):
        return AUTHORITY_TIERS["farm"]

    return AUTHORITY_TIERS["neutral"]
