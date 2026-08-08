"""Backward-compatible shim.

utils.py was split into two modules:
  - model_loader.py    : model loading (load_models, safe_decode)
  - postprocessing.py  : response cleanup (strip_filler_phrases and helpers)

The names below are re-exported so existing callers that do
`import utils` / `from utils import ...` keep working unchanged.
"""
from pollen.infrastructure.model_loader import load_models, safe_decode
from pollen.features.chat.postprocessing import (
    strip_filler_phrases,
    could_match_fallback_prefix,
    safe_emit_len_for_citations,
    is_url_query,
)

__all__ = [
    "load_models",
    "safe_decode",
    "strip_filler_phrases",
    "could_match_fallback_prefix",
    "safe_emit_len_for_citations",
    "is_url_query",
]
