"""Scholarly search: OpenAlex, PubMed, Semantic Scholar and arXiv.

These are queried directly from the hub (plain HTTPS, no residential IP needed),
so they keep working when the node search tunnel is down. Every provider is
fail-safe: any error, timeout or rate limit yields no results from that provider
and never raises into search().

Results use the same shape as search() -- {"title", "url", "snippet"} -- plus
private keys the ranking code reads:
  _scholarly      True for every result from this module
  _no_fetch       True: the snippet is already the abstract, so enrichment must
                  not replace it with a (usually paywalled) publisher page
  _peer_reviewed  True for journal and conference papers; False when unknown
                  or a preprint (preprints also say so in the snippet)
  _provider       "openalex" | "pubmed" | "semanticscholar" | "arxiv"
"""

import json
import os
import re as _re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as _ET
from concurrent.futures import ThreadPoolExecutor, wait as _wait

from pollen.features.chat.retrieval.rag_common import log, RAG_FETCH_CHARS, _FETCH_UA

SCHOLARLY_ENABLED = os.getenv("SCHOLARLY_ENABLED", "true").lower() == "true"
SCHOLARLY_PER_SOURCE = int(os.getenv("SCHOLARLY_PER_SOURCE", "5"))   # candidates requested per provider
SCHOLARLY_TIMEOUT = float(os.getenv("SCHOLARLY_TIMEOUT", "5"))       # seconds per HTTP request
# Cap on scholarly results in the final set, so papers add to web sources rather
# than replace them. The higher cap applies when the question itself asks for
# research (see is_research_query).
SCHOLARLY_MAX_RESULTS = int(os.getenv("SCHOLARLY_MAX_RESULTS", "2"))
SCHOLARLY_MAX_RESULTS_RESEARCH = int(os.getenv("SCHOLARLY_MAX_RESULTS_RESEARCH", "4"))
# Relevance bar for papers, on the cross-encoder snippet score (good matches
# score about 6-10, loosely related ones under 4). Every paper must clear the
# floor. Unless the question asks for research, a paper must also come within
# the margin of the best web result, so a merely on-topic paper cannot displace
# a page that actually answers the question.
# Established work first: where a provider reports citation counts, skip papers
# cited fewer times than this. Waived when the question asks for recent work,
# because new papers have had no time to be cited.
SCHOLARLY_MIN_CITATIONS = int(os.getenv("SCHOLARLY_MIN_CITATIONS", "10"))
SCHOLARLY_MIN_RERANK = float(os.getenv("SCHOLARLY_MIN_RERANK", "4.0"))
SCHOLARLY_RERANK_MARGIN = float(os.getenv("SCHOLARLY_RERANK_MARGIN", "3.0"))
_PROVIDERS_ENV = os.getenv("SCHOLARLY_PROVIDERS", "openalex,pubmed,semanticscholar,arxiv")

# Optional credentials / contact details. All providers work without them at
# lower rate limits.
_OPENALEX_MAILTO = os.getenv("OPENALEX_MAILTO", "").strip()
_OPENALEX_API_KEY = os.getenv("OPENALEX_API_KEY", "").strip()
_S2_API_KEY = os.getenv("S2_API_KEY", "").strip()
_NCBI_API_KEY = os.getenv("NCBI_API_KEY", "").strip()

_RESEARCH_QUERY_RE = _re.compile(
    r"\b(stud(y|ies)|research(ers?)?|papers?|evidence|journals?|peer[- ]?reviewed?|"
    r"meta[- ]?analys[ei]s|systematic review|clinical|trials?|literature|scientific|"
    r"scholarly|academic|publications?|findings|preprints?)\b",
    _re.IGNORECASE,
)

_RECENT_QUERY_RE = _re.compile(
    r"\b(latest|recent(ly)?|newest|new|current(ly)?|this year|last year|state of the art|20(2[4-9]|3\d))\b",
    _re.IGNORECASE,
)

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="scholarly")

# arXiv asks for no more than one request every 3 seconds.
_ARXIV_MIN_INTERVAL = 3.0
_arxiv_last = 0.0
_arxiv_lock = threading.Lock()


def is_enabled():
    return SCHOLARLY_ENABLED


def is_research_query(query):
    """True when the question itself asks for studies / papers / evidence."""
    return bool(query and _RESEARCH_QUERY_RE.search(query))


def result_cap(query):
    """Maximum scholarly results allowed in the final set for this query."""
    return SCHOLARLY_MAX_RESULTS_RESEARCH if is_research_query(query) else SCHOLARLY_MAX_RESULTS


def min_citations(query):
    """Citation floor for this query (0 when it asks for recent work)."""
    return 0 if (query and _RECENT_QUERY_RE.search(query)) else SCHOLARLY_MIN_CITATIONS


def min_rerank(query, best_web_score):
    """Lowest snippet-rerank score a paper needs to stay in the pool."""
    floor = SCHOLARLY_MIN_RERANK
    if best_web_score is not None and not is_research_query(query):
        floor = max(floor, best_web_score - SCHOLARLY_RERANK_MARGIN)
    return floor


def _get(url, headers=None):
    h = {"User-Agent": _FETCH_UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=SCHOLARLY_TIMEOUT) as resp:
        return resp.read()


def _clean(text):
    """Collapse whitespace and strip stray markup from titles / abstracts."""
    if not text:
        return ""
    text = _re.sub(r"<[^>]+>", " ", str(text))
    return _re.sub(r"\s+", " ", text).strip()


def _result(provider, title, url, abstract, venue="", year=None, cites=None, peer_reviewed=False,
            preprint=False, doi=""):
    """Build one result dict. The title carries venue and year so the source pill
    is recognisable; the snippet leads with them so the model can attribute."""
    title = _clean(title)
    abstract = _clean(abstract)
    if not title or not url or not abstract:
        return None
    venue = _clean(venue)
    tag = ", ".join(p for p in (venue, str(year) if year else "") if p)
    lead = []
    if tag:
        lead.append(tag + ".")
    if preprint:
        lead.append("Preprint, not peer reviewed.")
    if cites:
        lead.append("Cited by %d." % cites)
    snippet = (" ".join(lead) + " " + abstract).strip()
    return {
        "title": ("%s (%s)" % (title, tag)) if tag else title,
        "url": url,
        "snippet": snippet[:RAG_FETCH_CHARS],
        "_scholarly": True,
        "_no_fetch": True,
        "_peer_reviewed": bool(peer_reviewed) and not preprint,
        "_provider": provider,
        "_doi": (doi or "").lower(),
    }


def _doi_url(doi):
    doi = (doi or "").strip()
    if not doi:
        return "", ""
    bare = _re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=_re.I)
    return "https://doi.org/" + bare, bare.lower()


def _openalex(term, limit, min_cites=0):
    flt = "has_abstract:true,is_retracted:false"
    if min_cites > 0:
        flt += ",cited_by_count:>%d" % (min_cites - 1)
    params = {
        "search": term,
        "per-page": max(1, int(limit)),
        "filter": flt,
        "select": "id,doi,display_name,publication_year,cited_by_count,"
                  "abstract_inverted_index,primary_location,type",
    }
    if _OPENALEX_MAILTO:
        params["mailto"] = _OPENALEX_MAILTO
    if _OPENALEX_API_KEY:
        params["api_key"] = _OPENALEX_API_KEY
    data = json.loads(_get("https://api.openalex.org/works?" + urllib.parse.urlencode(params)))
    out = []
    for w in data.get("results") or []:
        inv = w.get("abstract_inverted_index") or {}
        words = sorted((pos, word) for word, positions in inv.items() for pos in positions)
        abstract = " ".join(word for _, word in words)
        url, doi = _doi_url(w.get("doi"))
        if not url:
            continue  # no DOI means no link a reader can check; other providers may have it
        source = ((w.get("primary_location") or {}).get("source") or {})
        r = _result(
            "openalex", w.get("display_name"), url, abstract,
            venue=source.get("display_name") or "",
            year=w.get("publication_year"),
            cites=w.get("cited_by_count"),
            peer_reviewed=(source.get("type") in ("journal", "conference")
                           and w.get("type") in ("article", "review")),
            preprint=(w.get("type") == "preprint"),
            doi=doi,
        )
        if r:
            out.append(r)
    return out


def _pubmed(term, limit, min_cites=0):
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    common = {"db": "pubmed", "tool": "pollen"}
    if _NCBI_API_KEY:
        common["api_key"] = _NCBI_API_KEY
    q = dict(common, term=term, retmax=max(1, int(limit)), sort="relevance", retmode="json")
    ids = (json.loads(_get(base + "esearch.fcgi?" + urllib.parse.urlencode(q)))
           .get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return []
    q = dict(common, id=",".join(ids), retmode="xml", rettype="abstract")
    root = _ET.fromstring(_get(base + "efetch.fcgi?" + urllib.parse.urlencode(q)))
    out = []
    for art in root.iter("PubmedArticle"):
        pmid = (art.findtext("./MedlineCitation/PMID") or "").strip()
        article = art.find("./MedlineCitation/Article")
        if not pmid or article is None:
            continue
        title_el = article.find("ArticleTitle")
        title = "".join(title_el.itertext()) if title_el is not None else ""
        parts = []
        for ab in article.iter("AbstractText"):
            text = "".join(ab.itertext())
            label = ab.get("Label")
            parts.append((label + ": " + text) if label else text)
        year = (article.findtext("./Journal/JournalIssue/PubDate/Year")
                or (article.findtext("./Journal/JournalIssue/PubDate/MedlineDate") or "")[:4])
        r = _result(
            "pubmed", title.rstrip("."), "https://pubmed.ncbi.nlm.nih.gov/%s/" % pmid, " ".join(parts),
            venue=article.findtext("./Journal/Title") or "",
            year=year if (year or "").isdigit() else None,
            peer_reviewed=True,
            doi=(art.findtext("./PubmedData/ArticleIdList/ArticleId[@IdType='doi']") or ""),
        )
        if r:
            out.append(r)
    return out


def _semanticscholar(term, limit, min_cites=0):
    params = {
        "query": term,
        "limit": max(1, int(limit)),
        "minCitationCount": max(0, int(min_cites)),
        "fields": "title,abstract,year,citationCount,externalIds,url,venue,publicationTypes",
    }
    headers = {"x-api-key": _S2_API_KEY} if _S2_API_KEY else None
    data = json.loads(_get("https://api.semanticscholar.org/graph/v1/paper/search?"
                           + urllib.parse.urlencode(params), headers))
    out = []
    for p in data.get("data") or []:
        ext = p.get("externalIds") or {}
        url, doi = _doi_url(ext.get("DOI"))
        if not url and ext.get("ArXiv"):
            url = "https://arxiv.org/abs/" + ext["ArXiv"]
        if not url:
            url = p.get("url") or ""
        types = p.get("publicationTypes") or []
        r = _result(
            "semanticscholar", p.get("title"), url, p.get("abstract"),
            venue=p.get("venue") or "",
            year=p.get("year"),
            cites=p.get("citationCount"),
            peer_reviewed=bool(p.get("venue")) and bool({"JournalArticle", "Review", "Conference"} & set(types)),
            preprint=bool(ext.get("ArXiv")) and not p.get("venue"),
            doi=doi,
        )
        if r:
            out.append(r)
    return out


def _arxiv(term, limit, min_cites=0):
    global _arxiv_last
    with _arxiv_lock:
        now = time.time()
        if now - _arxiv_last < _ARXIV_MIN_INTERVAL:
            return []
        _arxiv_last = now
    words = [w for w in _re.findall(r"[A-Za-z0-9]+", term)][:6]
    if not words:
        return []
    params = {
        "search_query": " AND ".join("all:" + w for w in words),
        "max_results": max(1, int(limit)),
        "sortBy": "relevance",
    }
    root = _ET.fromstring(_get("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in root.findall("a:entry", ns):
        abs_url = _re.sub(r"v\d+$", "", (e.findtext("a:id", "", ns) or "").strip())
        abs_url = abs_url.replace("http://arxiv.org/", "https://arxiv.org/")
        r = _result(
            "arxiv", e.findtext("a:title", "", ns), abs_url, e.findtext("a:summary", "", ns),
            venue="arXiv",
            year=(e.findtext("a:published", "", ns) or "")[:4] or None,
            preprint=True,
        )
        if r:
            out.append(r)
    return out


_PROVIDER_FUNCS = {
    "openalex": _openalex,
    "pubmed": _pubmed,
    "semanticscholar": _semanticscholar,
    "arxiv": _arxiv,
}
# Order here is the merge order (and so the tie-break when two providers return
# the same paper): the richest metadata first.
# Semantic Scholar's keyless tier answers 429 almost every time, so it only runs
# when S2_API_KEY is set.
PROVIDERS = tuple(
    p.strip().lower() for p in _PROVIDERS_ENV.split(",")
    if p.strip().lower() in _PROVIDER_FUNCS and (p.strip().lower() != "semanticscholar" or _S2_API_KEY)
)


def _run(name, term, limit, min_cites):
    t0 = time.time()
    try:
        res = _PROVIDER_FUNCS[name](term, limit, min_cites)
        log.info("RAG scholarly %s: %d result(s) in %.1fs", name, len(res), time.time() - t0)
        return res
    except Exception as e:
        # Rate limits are expected on the keyless tiers; anything else is worth a warning.
        level = log.info if getattr(e, "code", None) == 429 else log.warning
        level("RAG scholarly %s failed for %r: %s", name, term, e)
        return []


def start(term, query=None, per_source=None):
    """Kick off every provider in the background and return a handle for
    collect(). `term` is the keyword string sent to the providers; `query` is the
    user's original wording, used only to decide the citation floor. Returns None
    when disabled or there is nothing to search for, so search() can run this
    alongside the (slower) web search at no extra latency."""
    term = (term or "").strip()
    if not SCHOLARLY_ENABLED or not term or not PROVIDERS:
        return None
    n = per_source or SCHOLARLY_PER_SOURCE
    cites = min_citations(query if query is not None else term)
    return {
        "deadline": time.time() + SCHOLARLY_TIMEOUT + 1.0,
        "futures": [(name, _POOL.submit(_run, name, term, n, cites)) for name in PROVIDERS],
    }


def collect(handle):
    """Gather whatever finished before the deadline, de-duplicated by DOI then by
    title. Providers still running are left to finish and are ignored."""
    if not handle:
        return []
    futures = [f for _, f in handle["futures"]]
    _wait(futures, timeout=max(0.2, handle["deadline"] - time.time()))
    out, index = [], {}
    for name, fut in handle["futures"]:
        if not fut.done():
            log.warning("RAG scholarly %s timed out; skipping", name)
            continue
        for r in fut.result():
            keys = {_re.sub(r"[^a-z0-9]+", "", r["title"].rsplit(" (", 1)[0].lower())}
            if r.get("_doi"):
                keys.add("doi:" + r["_doi"])
            dup = next((index[k] for k in keys if k in index), None)
            if dup is None:
                index.update((k, len(out)) for k in keys)
                out.append(r)
            elif r["_peer_reviewed"] and not out[dup]["_peer_reviewed"]:
                out[dup] = r  # same paper: keep the peer-reviewed copy
                index.update((k, dup) for k in keys)
    return out


def fetch_scholarly(term, query=None, per_source=None):
    """Blocking convenience wrapper: start() + collect()."""
    return collect(start(term, query, per_source))
