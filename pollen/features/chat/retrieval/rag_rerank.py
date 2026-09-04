"""CPU cross-encoder reranker for RAG results.

Lazily loads cross-encoder/ms-marco-MiniLM-L-6-v2 (pre-cached in
/data/huggingface) ONCE per process and scores (query, doc) pairs on CPU
(~88MB model). Every entry point is wrapped so any failure returns an empty
list and NEVER breaks the search pipeline -- the caller falls back to the
existing ordering when reranking is unavailable.

Self-contained: does NOT import rag_retrieval (avoids a circular import).
Respects the process HF offline env (the model is already cached locally, so it
loads with HF_HUB_OFFLINE=1 as the live service runs).
"""
import logging
import time
import threading

log = logging.getLogger("rag_rerank")

_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"
_MAX_LENGTH = 512

_tok = None
_mdl = None
_load_failed = False
_load_lock = threading.Lock()


def _ensure_loaded():
    """Load tokenizer+model once (thread-safe). Returns True when ready, False
    if the load failed (caller then skips reranking). Never raises."""
    global _tok, _mdl, _load_failed
    if _mdl is not None and _tok is not None:
        return True
    if _load_failed:
        return False
    with _load_lock:
        if _mdl is not None and _tok is not None:
            return True
        if _load_failed:
            return False
        try:
            import torch  # noqa: F401
            from transformers import AutoTokenizer, AutoModelForSequenceClassification
            t0 = time.time()
            tok = AutoTokenizer.from_pretrained(_MODEL_NAME)
            mdl = AutoModelForSequenceClassification.from_pretrained(_MODEL_NAME).to("cpu").eval()
            _tok, _mdl = tok, mdl
            log.info("rag_rerank: loaded %s on CPU in %.1fs", _MODEL_NAME, time.time() - t0)
            return True
        except Exception as e:
            _load_failed = True
            log.warning("rag_rerank: model load failed (%s); reranking disabled", e)
            return False


def is_available():
    """True if the model is loaded/loadable (cheap after first call)."""
    return _ensure_loaded()


def rerank_scores(query, docs):
    """Return a relevance score per doc (higher = more relevant) for `query`,
    batched in a single CPU forward pass. Returns [] on empty input or ANY
    failure, so the caller can safely fall back to the existing ordering."""
    if not query or not docs:
        return []
    try:
        if not _ensure_loaded():
            return []
        import torch
        pairs = [[query, (d or "")] for d in docs]
        with torch.no_grad():
            enc = _tok(pairs, padding=True, truncation=True,
                       return_tensors="pt", max_length=_MAX_LENGTH)
            logits = _mdl(**enc).logits.squeeze(-1)
            scores = logits.tolist()
        # squeeze(-1) on a single-item batch can yield a scalar -> normalize.
        if not isinstance(scores, list):
            scores = [scores]
        return scores
    except Exception as e:
        log.warning("rag_rerank: scoring failed (%s); returning [] (caller falls back)", e)
        return []
