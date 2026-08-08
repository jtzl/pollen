"""Page fetching and HTML-to-text extraction, plus result enrichment."""

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



# Hosts that reliably serve bot-detection walls instead of article bodies.
# We skip the page fetch entirely and fall back to the DuckDuckGo snippet.
_SKIP_FETCH_HOSTS = {
    "sciencedirect.com",
    "linkinghub.elsevier.com",
    "elsevier.com",
    "onlinelibrary.wiley.com",
    "tandfonline.com",
    "jstor.org",
    "ieeexplore.ieee.org",
    "academia.edu",
    "researchgate.net",
    "pubs.acs.org",
    "cell.com",
    "link.springer.com",
    "springer.com",
    "sciencemag.org",
    "science.org",
}


def _is_skip_fetch_host(url):
    try:
        host = _urlparse(url).netloc.lower()
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    if host in _SKIP_FETCH_HOSTS:
        return True
    return any(host.endswith("." + h) for h in _SKIP_FETCH_HOSTS)


# Phrases that indicate a captcha/anti-bot interstitial rather than real content.
_BOT_DETECTION_RE = _re.compile(
    r"(?i)("
    r"\bcaptcha\b|"
    r"please\s+confirm\s+you\s+are\s+a?\s*human|"
    r"verify\s+you\s+are\s+a?\s*human|"
    r"are\s+you\s+a\s+robot|"
    r"prove\s+you\s+are\s+human|"
    r"\baccess\s+denied\b|"
    r"\b403\s+forbidden\b|"
    r"enable\s+javascript|"
    r"checking\s+your\s+browser|"
    r"attention\s+required|"
    r"\bsecurity\s+check\b|"
    r"unusual\s+traffic\s+from|"
    r"just\s+a\s+moment\b"
    r")"
)


_NAV_NOISE_RE = _re.compile(
    r"(?i)\b("
    r"cookie policy|privacy policy|terms of service|terms of use|all rights reserved|"
    r"sign in|log in|log out|create account|subscribe|newsletter|"
    r"skip to (main )?content|back to top|share this|follow us|"
    r"toggle [^.]{0,40}subsection|jump to navigation|jump to search|"
    r"this article needs additional citations|edit source|view source"
    r")\b"
)
_NAV_TAG_RE = _re.compile(
    r"(?is)<(nav|header|footer|aside|form|button|svg|iframe|script|style|noscript)\b[^>]*>.*?</\1>"
)
_MAIN_RE = _re.compile(r"(?is)<(main|article)\b[^>]*>(.*?)</\1>")


def _html_to_paragraphs(raw_html):
    """Strip HTML to a list of text paragraphs (stdlib only, no bs4).

    Removes nav/header/footer/aside/form blocks; prefers <main>/<article>
    content if the document exposes it; drops paragraphs that look like
    boilerplate (cookie banners, nav labels, subscribe prompts, etc.).
    """
    s = raw_html

    # Prefer the main/article region if present (pulls the first match)
    m = _MAIN_RE.search(s)
    if m:
        s = m.group(2)

    # Strip boilerplate regions and inline scripts/styles
    prev = None
    while prev != s:
        prev = s
        s = _NAV_TAG_RE.sub(" ", s)

    # Treat block-level end tags as paragraph boundaries
    s = _re.sub(r"(?i)</(p|div|li|section|article|h[1-6]|tr|td|blockquote)>", "\n\n", s)
    s = _re.sub(r"(?i)<br\s*/?>", "\n", s)

    # Strip all remaining tags
    s = _re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)

    paragraphs = []
    for chunk in s.split("\n\n"):
        t = _re.sub(r"\s+", " ", chunk).strip()
        if len(t) < 60:  # short fragments are usually nav/breadcrumb/meta
            continue
        if _NAV_NOISE_RE.search(t):
            continue
        # Drop paragraphs with too few spaces relative to length (menu lists, dense token noise)
        words = t.split()
        if len(words) < 8:
            continue
        # Drop paragraphs where >50% of tokens are 1-2 char (menu lists) or ALL-CAPS headings
        short_tokens = sum(1 for w in words if len(w) <= 2)
        if short_tokens > len(words) * 0.5:
            continue
        paragraphs.append(t)
    return paragraphs


def _score_paragraph(paragraph, query_terms):
    """Count query-term hits (case-insensitive). Gives a simple relevance rank."""
    if not query_terms:
        return 0
    lower = paragraph.lower()
    return sum(lower.count(t) for t in query_terms)



_TITLE_TAG_RE = _re.compile(r"<title[^>]*>(.*?)</title>", _re.IGNORECASE | _re.DOTALL)
_OG_TITLE_RE_A = _re.compile(
    r"""<meta[^>]+property=["']og:title["'][^>]*content=["']([^"']+)["']""",
    _re.IGNORECASE,
)
_OG_TITLE_RE_B = _re.compile(
    r"""<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:title["']""",
    _re.IGNORECASE,
)


def _extract_page_title(html_text):
    """Best-effort page title: og:title -> <title> -> ''. Single-line, trimmed."""
    if not html_text:
        return ""
    t = ""
    for rx in (_OG_TITLE_RE_A, _OG_TITLE_RE_B):
        m = rx.search(html_text)
        if m:
            t = m.group(1)
            break
    if not t:
        m = _TITLE_TAG_RE.search(html_text)
        if m:
            t = m.group(1)
    if not t:
        return ""
    t = _html.unescape(t)
    t = _re.sub(r"\s+", " ", t).strip()
    return t[:200]


def _clean_search_title(title):
    """DDG occasionally returns multiple search-result titles concatenated into one
    string (e.g. 'Title oneTitle twoTitle three...'). Cut to the first apparent
    title using ellipsis separators or missing-space camelCase boundaries."""
    if not title:
        return ""
    t = _re.sub(r"\s+", " ", title).strip()
    for sep in (" ... ", "...", "…"):
        if sep in t:
            head = t.split(sep, 1)[0].rstrip()
            if len(head) >= 8:
                t = head
                break
    m = _re.search(r"([a-z!?.])([A-Z][a-z]{2,})", t)
    if m and m.start() >= 20 and (len(t) - m.start()) >= 25:
        t = t[:m.start() + 1].rstrip()
    return t[:200]


def _fetch_page_text(url, query, max_chars=None, timeout=None):
    """Fetch URL, extract up to max_chars of query-relevant paragraphs. Returns '' on failure."""
    import requests
    if max_chars is None:
        max_chars = RAG_FETCH_CHARS
    if timeout is None:
        timeout = RAG_FETCH_TIMEOUT
    if _is_skip_fetch_host(url):
        log.debug("RAG fetch skipped (blocklisted host): %s", url)
        return ("", "")
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": _FETCH_UA, "Accept": "text/html,*/*;q=0.5"},
            allow_redirects=True,
            stream=True,
        )
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype.lower() and "xml" not in ctype.lower() and ctype:
            return ("", "")
        # Read at most ~2 MB to avoid huge pages
        raw = resp.raw.read(3_000_000, decode_content=True)
        try:
            text = raw.decode(resp.encoding or "utf-8", errors="replace")
        except Exception:
            text = raw.decode("utf-8", errors="replace")
    except Exception as e:
        log.debug("RAG fetch failed %s: %s", url, e)
        return ("", "")

    # Bail early if the page is a captcha/anti-bot interstitial. We scan a
    # generous prefix of the HTML-stripped-ish text so short wall pages match
    # even when most of the body is stripped as boilerplate.
    _probe = _re.sub(r"(?is)<script\b.*?</script>|<style\b.*?</style>|<[^>]+>", " ", text[:16000])
    if _BOT_DETECTION_RE.search(_probe):
        log.debug("RAG fetch discarded (bot-detection page): %s", url)
        return ("", "")

    paragraphs = _html_to_paragraphs(text)
    if not paragraphs:
        return ("", "")

    query_terms = [t for t in _re.findall(r"[A-Za-z0-9]{3,}", (query or "").lower()) if t not in {"the", "and", "what", "who", "why", "how", "when", "where", "does", "this", "that", "with", "from", "for", "about", "are", "was", "were", "has", "have"}]

    # Score all paragraphs, pick top-scoring ones, then reorder them by original position
    scored = [(i, p, _score_paragraph(p, query_terms)) for i, p in enumerate(paragraphs)]
    # Keep first paragraph as a baseline even if it scores 0 (often the lede)
    first_kept = [(0, paragraphs[0], _score_paragraph(paragraphs[0], query_terms))] if paragraphs else []
    ranked = sorted(scored, key=lambda x: (-x[2], x[0]))

    chosen = {}
    for idx, para, sc in ranked:
        if sc <= 0 and chosen:  # stop taking zero-score ones once we have some content
            break
        chosen[idx] = para
        total = sum(len(v) for v in chosen.values()) + 2 * max(0, len(chosen) - 1)
        if total >= max_chars:
            break

    # Always include the lede so the summary is coherent
    if first_kept and first_kept[0][0] not in chosen:
        chosen[first_kept[0][0]] = first_kept[0][1]

    # Emit in document order, cap to max_chars
    out_parts = [chosen[k] for k in sorted(chosen.keys())]
    out = "\n\n".join(out_parts)
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0] + "\u2026"
    # Final safety: if bot-detection text slipped through into a kept paragraph,
    # drop the enrichment so the caller falls back to the DDG snippet.
    if _BOT_DETECTION_RE.search(out):
        log.debug("RAG fetch discarded (bot-detection in output): %s", url)
        return ("", "")
    return out, _extract_page_title(text)

def _enrich_results(results, query):
    """Fetch each result URL in parallel and replace/augment snippet with richer text."""
    if not results:
        return results
    urls = [(i, r.get("url", "")) for i, r in enumerate(results) if r.get("url", "").startswith("http")]
    if not urls:
        return results
    workers = min(RAG_FETCH_WORKERS, len(urls))
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_fetch_page_text, url, query): i for i, url in urls}
            for fut in as_completed(futures, timeout=RAG_FETCH_TIMEOUT * 2 + 4):
                i = futures[fut]
                try:
                    extracted = fut.result()
                except Exception as e:
                    log.debug("RAG fetch future error idx=%d: %s", i, e)
                    continue
                if isinstance(extracted, tuple):
                    page_text, page_title = extracted
                else:
                    page_text, page_title = (extracted or ""), ""
                if page_text:
                    results[i]["snippet"] = page_text
                if page_title:
                    results[i]["title"] = page_title
                # else: keep original DDG snippet/title as fallback
    except Exception as e:
        log.warning("RAG enrichment partial failure: %s", e)
    return results
