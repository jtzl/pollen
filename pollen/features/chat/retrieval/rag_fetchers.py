"""Direct fetchers: node search over SSH and direct Wikipedia fetch."""

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



def fetch_wikipedia(query, limit=2):
    """Fetch the top Wikipedia article intros for `query` via the MediaWiki API.

    Returns a list of result dicts in the SAME shape as search():
    [{"title": ..., "url": ..., "snippet": ...}, ...]. Any error (network,
    timeout, bad payload) yields [] so callers can treat it as "no results".
    """
    try:
        import urllib.parse

        api = "https://en.wikipedia.org/w/api.php"
        ua = {"User-Agent": _FETCH_UA, "Accept": "application/json"}

        # 1) Search for the top matching article titles.
        search_qs = urllib.parse.urlencode({
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": max(1, int(limit)),
            "format": "json",
        })
        req = urllib.request.Request(api + "?" + search_qs, headers=ua)
        with urllib.request.urlopen(req, timeout=6) as resp:
            sdata = json.loads(resp.read().decode("utf-8", errors="replace"))
        hits = (sdata.get("query") or {}).get("search") or []
        titles = [h["title"] for h in hits if h.get("title")][:int(limit)]
        if not titles:
            return []

        # 2) For each title, pull the plaintext intro extract and build the URL.
        results = []
        for title in titles:
            extract_qs = urllib.parse.urlencode({
                "action": "query",
                "prop": "extracts",
                "exintro": 1,
                "explaintext": 1,
                "format": "json",
                "titles": title,
            })
            req2 = urllib.request.Request(api + "?" + extract_qs, headers=ua)
            with urllib.request.urlopen(req2, timeout=6) as resp:
                edata = json.loads(resp.read().decode("utf-8", errors="replace"))
            pages = (edata.get("query") or {}).get("pages") or {}
            extract = ""
            for page in pages.values():
                extract = page.get("extract", "") or ""
                break
            url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
            results.append({
                "title": title,
                "url": url,
                "snippet": extract[:RAG_FETCH_CHARS],
            })
        return results
    except Exception as e:
        log.warning("fetch_wikipedia failed for %r: %s", query, e)
        return []


def node_search(query, max_results):
    """Run web search on a residential contributor node via the reverse SSH tunnel.

    The node runs DDGS from a residential IP that search engines do not block
    (this EC2 datacenter IP gets 403/429). Returns a list of dicts in the ddgs
    raw shape {"href", "title", "body"} so the existing search() loop consumes
    them unchanged. Returns [] on any error/timeout/nonzero exit/empty output.
    """
    try:
        remote = "~/pollen-search-env/bin/python3 ~/pollen_search.py " + shlex.quote(query) + " " + str(int(max_results))
        cmd = [
            "ssh", "-p", "31333",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ControlMaster=auto",
            "-o", "ControlPath=/home/ubuntu/.ssh/cm-%r@%h:%p",
            "-o", "ControlPersist=60",
            "irfan@localhost",
            remote,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        if proc.returncode != 0:
            log.warning("RAG node_search ssh exit %d for query=%r: %s",
                        proc.returncode, query, (proc.stderr or "").strip()[:200])
            return []
        data = json.loads(proc.stdout)
        if not isinstance(data, list):
            return []
        out = []
        for r in data:
            if not isinstance(r, dict):
                continue
            out.append({
                "href": r.get("url", ""),
                "title": r.get("title", ""),
                "body": r.get("snippet", ""),
            })
        if max_results:
            out = out[:max_results]
        return out
    except Exception as e:
        log.warning("RAG node_search failed for query=%r: %s", query, e)
        return []
