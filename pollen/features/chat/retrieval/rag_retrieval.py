"""Web-search orchestration: search(), query building, rate limiting."""

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
from pollen.features.chat.retrieval.rag_ranking import is_blocked_domain, _source_rank, _extract_keywords, _relevance_score, _authority_score
from pollen.features.chat.retrieval.rag_content import _enrich_results, _clean_search_title
from pollen.features.chat.retrieval.rag_fetchers import node_search, fetch_wikipedia




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


_QUERY_STOPWORDS = {
    "a","an","and","or","but","of","to","in","on","at","for","is","are","was","were","be","been","being",
    "have","has","had","do","does","did","will","would","should","could","can","may","might",
    "i","you","he","she","it","we","they","this","that","these","those",
    "what","which","who","whom","whose","when","where","why","how",
    "my","your","his","her","its","our","their","me","us","them",
    "the","as","by","with","from","about","into","than","then","so","just","also","very",
}

# casual/interrogative -> data-oriented synonym map
_QUERY_SYNONYMS = {
    "eat": "consume", "eats": "consumes", "eating": "consumption",
    "drink": "consume", "drinks": "consumes", "drinking": "consumption",
    "sleep": "sleep duration", "sleeps": "sleep duration", "sleeping": "sleep duration",
    "earn": "income", "earns": "income", "earning": "income",
    "make": "earn", "makes": "earns", "making": "earning",
    "spend": "expenditure", "spends": "expenditure", "spending": "expenditure",
    "live": "lifespan", "lives": "lifespan", "living": "lifespan",
    "die": "mortality", "dies": "mortality", "dying": "mortality",
    "average": "mean", "typical": "mean",
    "american": "U.S.", "americans": "U.S.",
    "often": "frequency", "many": "number", "much": "amount",
    "kid": "child", "kids": "children",
    "guy": "person", "guys": "people",
}


def _reformulate_query(query):
    """Generate 2-3 query variants for broader DDG coverage. Always returns >=1 entry.

    Variant A: the original query, verbatim.
    Variant B: stopwords stripped, plus data-oriented hint ("statistics data").
    Variant C: synonym-substituted content words (e.g. "eat" -> "consume").
    """
    q = (query or "").strip()
    if not q:
        return [q]
    variants = [q]
    words = _re.findall(r"[A-Za-z0-9']+", q)
    if not words:
        return variants
    lower = [w.lower() for w in words]
    keywords = [w for w in lower if w not in _QUERY_STOPWORDS and len(w) > 1]

    if keywords and len(keywords) < len(lower):
        variant_b = " ".join(keywords) + " statistics data"
        if variant_b and variant_b.lower() != q.lower() and variant_b not in variants:
            variants.append(variant_b)

    src_words = keywords if keywords else lower
    subbed = [_QUERY_SYNONYMS.get(w, w) for w in src_words]
    if subbed and subbed != src_words:
        variant_c = " ".join(subbed)
        if variant_c and variant_c.lower() != q.lower() and variant_c not in variants:
            variants.append(variant_c)

    return variants[:2]  # capped at 2 for token budget


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
        # Strip prompt-wrapper tokens so they don't leak into node search queries.
        if query:
            for _tok in ("<s>", "</s>", "[INST]", "[/INST]"):
                query = query.replace(_tok, " ")
            query = query.strip()
        variants = _reformulate_query(query)
        per_variant = max(3, -(-max_results // max(1, len(variants))))  # ceil div
        log.info("RAG search starting: query=%r, %d variants=%r, filter=%r, max_results=%d, per_variant=%d",
                 query, len(variants), variants, source_filter.strip(), max_results, per_variant)

        results = []
        seen_urls = set()
        start = time.time()
        try:
            with DDGS() as ddgs:
                for v in variants:
                    fq = v  # curated site: filter no longer forced onto node query
                    try:
                        raw = node_search(fq, per_variant)
                    except Exception as ve:
                        log.warning("RAG DDG variant failed %r: %s", v, ve)
                        continue
                    added = 0
                    for r in raw:
                        url = r.get("href", "")
                        if not url or url in seen_urls:
                            continue
                        if is_blocked_domain(url):
                            log.info("RAG blocked domain: %s", url)
                            seen_urls.add(url)
                            continue
                        seen_urls.add(url)
                        results.append({
                            "title": _clean_search_title(r.get("title", "")),
                            "url": url,
                            "snippet": r.get("body", ""),
                        })
                        added += 1
                        if len(results) >= max_results:
                            break
                    log.info("RAG variant %r: %d new results (total=%d)", v, added, len(results))
                    if len(results) >= max_results:
                        break
        except Exception as de:
            log.error("RAG DDG session failed: %s", de)

        elapsed = time.time() - start
        if elapsed > RAG_SEARCH_TIMEOUT:
            log.warning("RAG search slow (%.1fs > %ds) for query=%r", elapsed, RAG_SEARCH_TIMEOUT, query)

        # Fallback: if curated-source filter returned nothing across all variants, retry without the filter
        if not results and source_filter:
            log.warning("RAG curated multi-variant returned 0 results for query=%r, falling back to unfiltered DDG", query)
            fb_start = time.time()
            try:
                with DDGS() as ddgs:
                    for v in variants:
                        try:
                            fb_raw = node_search(v, per_variant)
                        except Exception as ve:
                            log.warning("RAG fallback variant failed %r: %s", v, ve)
                            continue
                        for r in fb_raw:
                            url = r.get("href", "")
                            if not url or url in seen_urls:
                                continue
                            if is_blocked_domain(url):
                                log.info("RAG blocked domain (fallback): %s", url)
                                seen_urls.add(url)
                                continue
                            seen_urls.add(url)
                            results.append({
                                "title": _clean_search_title(r.get("title", "")),
                                "url": url,
                                "snippet": r.get("body", ""),
                            })
                            if len(results) >= max_results:
                                break
                        if len(results) >= max_results:
                            break
                log.info("RAG fallback complete: %d results in %.1fs", len(results), time.time() - fb_start)
            except Exception as fe:
                log.error("RAG fallback failed for query=%r: %s", query, fe)

        # Relevance filter + ranking:
        # (1) score each result by how many query keywords appear in title+snippet
        # (title matches weighted 2x);
        # (2) drop results with score == 0 (off-topic noise);
        # (3) sort by (source_rank, -relevance, original_index): source rank is
        #     PRIMARY (curated < preferred < neutral < deprioritized), so a
        #     deprioritized domain always ranks below a non-deprioritized one
        #     even when its relevance score is higher. Within the same rank
        #     tier we fall back to relevance, then insertion order.
        # Directly fetch Wikipedia and merge into the pool. DDG deprioritizes
        # and often misses Wikipedia; these direct hits are deduped by URL
        # against the DDG results and exempted from the Wikipedia
        # deprioritization below so they can surface when DDG returned nothing.
        wiki_direct_urls = set()
        try:
            # Build a clean Wikipedia search term: strip any [INST]/<s> prompt
            # wrapper, then use extracted keywords (nouns) rather than the full
            # natural-language question. list=search returns junk for phrasings
            # like "how long does it take to..." but works on noun terms.
            _wq = query or ""
            for _tok in ("<s>", "</s>", "[INST]", "[/INST]"):
                _wq = _wq.replace(_tok, " ")
            _wiki_kw = _extract_keywords(_wq)
            # Drop weak words that pull irrelevant Wikipedia articles (e.g. "long"
            # -> "Nia Long" on a guitar question). This narrows the Wikipedia
            # search term ONLY; the relevance-scoring keywords below are unaffected.
            # Fall back to the full keyword list if dropping empties the term.
            _WIKI_WEAK = {"long", "take", "many", "much", "get", "make", "does",
                          "way", "work", "good", "best", "need", "learn"}
            _wiki_strong = [k for k in _wiki_kw if k not in _WIKI_WEAK]
            wiki_query = " ".join(_wiki_strong or _wiki_kw) or _wq.strip()
            for wr in fetch_wikipedia(wiki_query):
                wurl = wr.get("url", "")
                if not wurl or wurl in seen_urls or is_blocked_domain(wurl):
                    continue
                seen_urls.add(wurl)
                wiki_direct_urls.add(wurl)
                results.append(wr)
            if wiki_direct_urls:
                log.info("RAG wikipedia direct-fetch added %d result(s) for wiki_query=%r (query=%r)",
                         len(wiki_direct_urls), wiki_query, query)
        except Exception as we:
            log.warning("RAG wikipedia merge failed for query=%r: %s", query, we)

        keywords = _extract_keywords(query)
        scored = []
        dropped = 0
        for i, r in enumerate(results):
            rs = _relevance_score(keywords, r.get("title", ""), r.get("snippet", ""))
            if rs <= 0:
                dropped += 1
                log.info("RAG relevance drop score=0: %s - %s", r.get("title", "")[:60], r.get("url", ""))
                continue
            sr = _source_rank(r.get("url", ""))
            # Exempt directly-fetched Wikipedia results from the Wikipedia
            # deprioritization (rank 2 -> neutral 1) so they compete on relevance
            # and can surface when DuckDuckGo missed them.
            if sr == 2 and r.get("url", "") in wiki_direct_urls:
                sr = 1
            # Authority-aware ORDER: add an authority delta (gov/edu/journal/
            # news boost, content-farm penalty) to the base relevance score.
            # This reorders results WITHIN their source_rank tier. The pure
            # relevance score `rs` still gates survival (the rs<=0 drop above)
            # and RAG_MAX_RESULTS is unchanged -- only ORDER changes.
            auth = _authority_score(r.get("url", ""))
            combined = rs + auth
            scored.append((rs, sr, i, r, combined))
        # source_rank tier stays PRIMARY; within a tier, sort by combined
        # relevance+authority (desc), then original insertion order.
        scored.sort(key=lambda t: (t[1], -t[4], t[2]))
        # Light per-domain cap so a single domain (e.g. Wikipedia) can't be the
        # only source: allow at most _MAX_PER_DOMAIN results from any one domain
        # while still filling up to max_results. Preserves sorted order, so the
        # [n] citation numbering (format_context) stays consistent.
        _MAX_PER_DOMAIN = 2
        _dom_counts = {}
        results = []
        for _t in scored:
            _r = _t[3]
            _dom = _domain_of(_r.get("url", ""))
            if _dom and _dom_counts.get(_dom, 0) >= _MAX_PER_DOMAIN:
                continue
            _dom_counts[_dom] = _dom_counts.get(_dom, 0) + 1
            results.append(_r)
            if len(results) >= max_results:
                break

        log.info("RAG search complete: query=%r, %d relevant results (%d dropped) in %.1fs",
                 query, len(results), dropped, elapsed)
        for i, (rs, sr, _idx, r, comb) in enumerate(scored):
            log.info("  result[%d] relevance=%d authority=%+d combined=%d source_rank=%d: %s - %s",
                     i, rs, comb - rs, comb, sr, r.get("title", "")[:60], r.get("url", ""))
        if not results:
            log.warning("RAG search returned 0 relevant results for query=%r (variants=%r, dropped=%d)",
                        query, variants, dropped)

        # Enrich: fetch each URL and extract up to RAG_FETCH_CHARS of relevant paragraphs
        enrich_start = time.time()
        results = _enrich_results(results, query)
        log.info("RAG enrichment complete: %d results, %.1fs", len(results), time.time() - enrich_start)
        return results

    except Exception as e:
        log.error("RAG search failed for query=%r: %s (response will proceed without search)", query, e)
        return []
