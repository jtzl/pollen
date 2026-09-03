import re as _re
import re

_FILLER_PATTERNS = [
    # Word boundaries (\b) on the leading word prevent matches that would start
    # mid-word (e.g. "fooIt is important..." -> only matches when "It" is its own
    # word). Trailing whitespace `\s+` already implies a non-word boundary on the
    # right; explicit `\b` is added on patterns whose final captured word could
    # otherwise be a prefix of a longer word ("person" vs "personal").
    _re.compile(r"\bHowever,?\s+it'?s\s+(also\s+)?important\s+to\s+note\s+that\s+", _re.IGNORECASE),
    _re.compile(r"\b(?:[Ii]t'?s|[Ii]t\s+is)\s+(?:also\s+)?important\s+to\s+notes?\b\s*(?:that\s+)?", _re.IGNORECASE),
    _re.compile(r"\b(?:[Ii]t'?s|[Ii]t\s+is)\s+(also\s+)?worth\s+(mentioning|noting)\s+(that\s+)?", _re.IGNORECASE),
    _re.compile(r"\b(This|That|Consumption|Amounts?|Numbers?|Figures?|Results?|Values?)\s+can\s+vary\s+widely\b[^.]*\.?", _re.IGNORECASE),
    _re.compile(r"\bhowever,?\s+this\s+can\s+vary\b[^.]*\.?", _re.IGNORECASE),
    _re.compile(r"\b[Ss]ome\s+(people|individuals|folks|users)\s+(may|might)\s+[^.]*?\bwhile\s+others\s+(might|may|do)\s+[^.]*?(?=\.|$)", _re.IGNORECASE | _re.DOTALL),
    _re.compile(r"\s*,?\s*(varies|varying)?\s*\bfrom\s+person\s+to\s+person\b", _re.IGNORECASE),
    _re.compile(r"\b[Ii]t\s+depends\s+on\s+the\s+person\b[^.]*\.?", _re.IGNORECASE),
    # Self-referential AI disclaimers (apostrophe-tolerant: ' or \u2019)
    _re.compile(r"\bI\s+don['\u2019]?t\s+have\s+personal\s+experiences\b", _re.IGNORECASE),
    _re.compile(r"\bI\s+don['\u2019]?t\s+have\s+emotions\b", _re.IGNORECASE),
    _re.compile(r"\bAs\s+an\s+AI\b", _re.IGNORECASE),
    _re.compile(r"\bAs\s+a\s+language\s+model\b", _re.IGNORECASE),
    _re.compile(r"\bI\s+cannot\s+feel\b", _re.IGNORECASE),
    _re.compile(r"\bI['\u2019]?m\s+just\s+an\s+AI\b", _re.IGNORECASE),
    _re.compile(r"\bI\s+am\s+an\s+AI\s+assistant\b", _re.IGNORECASE),
    # Meta-commentary about how the response was generated. These are pure
    # process disclaimers the user never needs to see. Lookaheads on the
    # sentence terminator preserve it so the preceding real sentence keeps
    # its period — `strip_filler_phrases` then collapses the "X.." that
    # results when the meta was an entire sentence.
    # "This response/answer is based (solely) on ..." -> drop whole sentence
    _re.compile(
        r"(?i)(?:^|(?<=[.!?])\s+|\n)"
        r"[Tt]his\s+(?:response|answer)\s+is\s+based(?:\s+solely)?\s+on"
        r"[^.!?\n]*(?=[.!?]|\n|$)"
    ),
    # ", (and) does not incorporate any additional knowledge ..." — drop the
    # clause (including its leading connector) but keep the sentence's
    # trailing terminator so the main clause still reads as a sentence.
    _re.compile(
        r"(?i)(?:,\s*(?:and\s+)?|\band\s+|(?<=[.!?])\s+|^|\n)"
        r"does\s+not\s+incorporate\s+any\s+additional\s+knowledge"
        r"[^.!?\n]*(?=[.!?]|\n|$)"
    ),
    # "Based solely on the provided search results, ..." (and the broader
    # family without "solely": "Based on the search results", "According to
    # the provided search results", "Based on the search results provided",
    # etc.) -> drop the prefix and any trailing comma. Re-capitalization
    # below restores the next word's leading letter when this fires at
    # sentence start.
    _re.compile(
        r"(?i)(?:^|(?<=[.!?])\s+|\n)"
        r"(?:Based\s+(?:solely\s+)?on\s+the|According\s+to\s+the)\s+"
        r"(?:provided\s+)?search\s+results?(?:\s+provided)?"
        r"\s*,?\s*"
    ),
]

# Opening meta filter: drop a leading clause/sentence that describes what the
# model can or cannot do instead of answering. Applied only at the start of
# the response. Two shapes are handled:
#   (a) "<meta-clause>, but|however|still|yet|although|though <answer>"
#       -> strip the meta-clause and the connective, keep the answer.
#   (b) "<meta-sentence>. <answer>"
#       -> strip the whole leading sentence (up to the first .!?).
_META_OPENER = (
    r"(?:"
    r"As\s+an?\s+AI\b|"
    r"As\s+a\s+language\s+model\b|"
    r"I(?:['\u2019]m|\s+am)\s+(?:just\s+|only\s+)?an?\s+AI\b|"
    r"I(?:['\u2019]m|\s+am)\s+(?:just\s+|only\s+)?a\s+language\s+model\b|"
    r"I\s+(?:cannot|can['\u2019]?t|do\s+not|don['\u2019]?t)\s+"
    r"(?:feel|experience|have\s+(?:personal|emotions|feelings|opinions|experiences|preferences|beliefs|access|the\s+ability|the\s+capability)|access|provide\s+personal|physically|truly\s+understand|possess)|"
    r"I\s+am\s+(?:not\s+able|unable)\s+to\b|"
    r"I\s+don['\u2019]?t\s+have\s+(?:the\s+)?(?:ability|capability|capacity|personal|emotions|feelings|opinions|experiences|preferences)\b"
    r")"
)
_LEADIN = r"(?:(?:Unfortunately|However|Well|Actually|Please\s+note|Note|Of\s+course)[,.]?\s+)?"

# (a) clause form: meta-phrase, <optional non-terminator text>, but/however/... <answer>
_OPENING_META_CLAUSE = _re.compile(
    r"^\s*" + _LEADIN + _META_OPENER +
    r"[^.!?\n]*?,\s+(?:but|however|still|yet|although|though)\s+",
    _re.IGNORECASE,
)
# (b) full-sentence form: meta-sentence + terminator
_OPENING_META_SENTENCE = _re.compile(
    r"^\s*" + _LEADIN + _META_OPENER +
    r"[^.!?\n]*[.!?](?:\s+|\n|$)",
    _re.IGNORECASE,
)


def _strip_opening_meta_sentence(text):
    """Remove up to 2 leading meta-commentary clauses/sentences before the answer."""
    if not text:
        return text
    for _ in range(2):
        # Prefer clause form first so "X, but Y" keeps Y instead of being eaten whole
        new = _OPENING_META_CLAUSE.sub("", text, count=1)
        if new == text:
            new = _OPENING_META_SENTENCE.sub("", text, count=1)
        if new == text:
            break
        text = new
    # Capitalise first letter if the new lead starts with a lowercase word
    text = _re.sub(r"^(\s*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text, count=1)
    return text


# ============================================================
# Response cleanup — URL and citation-section removal.
# Contract enforced by _strip_urls():
#   1. Response body contains NO URL in any form:
#      - complete http(s):// URLs
#      - angle-bracketed <https://...>
#      - markdown [text](url)
#      - partial/truncated URLs from token cutoff: `<https`, `https:`,
#        `https://en.wikip`, `<https://foo` (no closing bracket), etc.
#      - URLs with connector prefixes ("available at:", "Source:", etc.)
#   2. No trailing References/Sources/Bibliography/Citations/Footnotes
#      section — strip from heading to end of text unconditionally.
#   3. Inline citations like `[2]. However,` and `source [1], data` are
#      preserved untouched. Only `[N]` alone on its own line is dropped.
# ============================================================

# --- URL body fragments (used inside larger patterns) -----------------------
# Matches the URL portion of a full OR partial URL, with/without angle brackets.
# Captures: optional leading `<`, `http(s)`, optional `:`, optional `//`,
# optional partial body (any chars except whitespace/brackets), optional
# trailing `>`. Anchored by `\bhttps?` so the word "https" alone (no colon,
# no leading `<`) isn't matched — see _PARTIAL_URL_RE for that edge.
_URL_BODY = (
    r"https?"                               # http or https
    r"(?::(?:/+[^\s<>)\]\"']*)?)?"         # optional :, optional //path
)

# --- Complete URL forms ----------------------------------------------------
# Markdown links: keep anchor text, drop URL. Partial URL tolerated inside ().
_MD_LINK_URL_RE = _re.compile(
    r"\[([^\]\n]+?)\]\(\s*" + _URL_BODY + r"\s*\)"
)
# Angle-bracketed URLs (possibly truncated: may be missing the closing `>`).
_ANGLE_URL_RE = _re.compile(r"<\s*" + _URL_BODY + r"\s*>?")
# Bare URLs: https://... (complete).
_BARE_URL_RE = _re.compile(r"https?://[^\s<>)\]\"']+", _re.IGNORECASE)

# --- Partial URL catchall --------------------------------------------------
# Run AFTER all complete-URL patterns. Catches residual fragments like:
#   "https:" (no //), "https://en.wikip" (truncated), "<https" / "<https:"
# Requires a colon OR a leading `<` so we don't false-positive on the bare
# word "https" / "HTTPS" used as a protocol mention in prose.
_PARTIAL_URL_RE = _re.compile(
    r"<\s*https?(?::(?:/*[^\s<>)\]\"']*)?)?\s*>?"   # <https, <https:, <https://foo...
    r"|"
    r"\bhttps?:(?:/+[^\s<>)\]\"']*)?",              # https:, https:/, https://foo...
    _re.IGNORECASE,
)

# --- Trailing References/Sources block heading -----------------------------
# Only matches heading keywords at the START of a line (multiline ^), so
# "the references she gave:" mid-sentence never matches. Followed by
# `:` / `.` / `-` to distinguish from natural-prose occurrences like
# "References are scarce".
_REF_BLOCK_HEADING_RE = _re.compile(
    r"(?im)(?:^|[.!?]\s+)"           # start of line OR after sentence end
    r"[ \t]*(?:\*\*|__)?[ \t]*"
    r"(?:References?\s*\(?s?\)?|Sources?|Bibliography|Citations?|"
    r"Further\s+reading|Footnotes?|Works\s+cited|See\s+also)"
    r"[ \t]*[:.\-]"
)

# --- Connector + URL (strips phrase and URL together) ----------------------
# URL portion accepts partial URLs too, so "Source: https:" is fully stripped.
_URL_WITH_PREFIX_RE = _re.compile(
    r"(?i)(?:,\s*)?"
    r"\b(?:available\s+at|source|sources|see(?:\s+also)?|"
    r"retrieved\s+from|found\s+at|(?:read\s+)?more\s+(?:info(?:\s+at)?|details|at)|"
    r"full\s+(?:details|text|article|info)|"
    r"additional\s+(?:info|details|information)|"
    r"link(?:s)?|url|visit)"
    r"\s*[:\-]?\s*"
    r"(?:<\s*)?" + _URL_BODY + r"\s*>?"
)

# --- [N] followed by URL (numbered reference line) -------------------------
_REF_BRACKET_URL_RE = _re.compile(
    r"\[\s*\d{1,3}\s*\]\s*:?\s*(?:<\s*)?" + _URL_BODY + r"\s*>?"
)

# --- Dangling connector phrases with URL already stripped ------------------
# Narrow list: only phrases that are ~always URL-attribution. Common phrases
# like "more details" or "additional info" are NOT here — they appear in
# normal prose and we must not corrupt those sentences.
_DANGLING_PREFIX_RE = _re.compile(
    r"(?im)(?:,\s*)?"
    r"\b(?:available\s+at|retrieved\s+from|read\s+more\s+at)"
    r"\s*[:\-]?\s*(?=[.,;]?\s*(?:\n|$))"
)
# "Source:" / "Sources:" at start of line with nothing meaningful after.
_DANGLING_SOURCE_LINE_RE = _re.compile(
    r"(?im)^\s*(?:\*\*|__)?\s*sources?\s*[:\-]\s*(?:\*\*|__)?\s*(?=[.,;]?\s*(?:\n|$))"
)

# --- Orphan [N] markers left behind (own line only) ------------------------
# Inline citations like "as shown in [2]" or "[2]. However," survive.
_ORPHAN_REF_NUM_RE = _re.compile(r"(?m)^[ \t]*\[\s*\d{1,2}\s*\][ \t]*$\n?")


def _strip_trailing_ref_block(text):
    """Remove any trailing References/Sources/Bibliography/Citations/
    Footnotes/Works-cited/See-also section unconditionally. If a heading
    appears anywhere in the text at start-of-line, drop everything from
    the heading to the end. Rule 2 of the cleanup contract.
    """
    if not text:
        return text
    last = None
    for m in _REF_BLOCK_HEADING_RE.finditer(text):
        last = m
    if last is None:
        return text
    return text[: last.start()].rstrip().rstrip(".,:;")


# Fabricated trailing academic citation: a bibliography-style reference the
# model appends after the answer with NO "References"/"Sources" heading (so
# _strip_trailing_ref_block, which is heading-gated, misses it). These are
# redundant (real sources render as citation pills) and usually hallucinated.
#
# Matching is anchored on the diagnostic academic signature -- a (YYYY) year
# TOGETHER WITH a volume(issue), pages locator like "127(6), 489-498" -- both
# required, at the very end of the text. A bare year or surname in ordinary
# prose lacks the volume(issue),pages structure and is never touched. The
# citation's left boundary is found via an author-list lead ("Lastname, X.")
# or a Title-Case organisation/title lead running straight into the year, so
# the real answer sentence before it (and its terminal punctuation) is kept.
_FAB_CIT_RE = _re.compile(
    r"(?:(?<=[.!?])[ \t]+|\n+)"                       # boundary: after a sentence, or a new line
    r"(?P<cit>"
    r"(?:"
    r"[A-Za-z][A-Za-z.'’-]*,[ \t]+[A-Z]\.[^\n]{0,90}?\(\d{4}\)"   # (a) author list -> (YYYY)
    r"|"
    r"(?:[A-Z][A-Za-z.'’&-]*[ \t]+){1,7}\(\d{4}\)"                # (b) Title-Case/org -> (YYYY)
    r")"
    r"[^\n]*?"                                        # ... title / journal ...
    r"\b\d{1,4}\(\d{1,3}\)[,:]?[ \t]*\d{1,4}[ \t]*[-–][ \t]*\d{1,4}"  # volume(issue), pages
    r"[.”\"'\)\]]*"                               # optional trailing punctuation
    r")[ \t]*\Z"
)


def _strip_trailing_fabricated_citation(text):
    """Remove a trailing fabricated academic citation appended after the answer.

    Fires only on the academic signature: a (YYYY) year together with a
    volume(issue), pages locator, at the very end of the text. Requiring the
    volume(issue),pages structure means a bare year, a surname, or "according
    to a 2020 study" in ordinary prose is never stripped. The real answer
    sentence before the citation, including its terminal punctuation, is kept.
    """
    if not text:
        return text
    m = _FAB_CIT_RE.search(text)
    if not m:
        return text
    return text[: m.start()].rstrip()


# Spontaneous trailing self-confidence tail Mixtral sometimes appends AFTER the
# answer -- a "Confidence: 90%" line and/or an adjacent paragraph opening
# "I am 90% confident ...". No prompt source and no other stripper touches it.
# Matched ONLY as a trailing block at the very end of the text (\Z), and only
# when it begins on its own line (a newline boundary precedes it) -- so a
# mid-sentence "confident" or a real answer sentence is never affected. The
# block may not span a blank line into another paragraph: (?!\n\n) stops the
# consumption at a paragraph break, so if substantive content follows, \Z fails
# and nothing is stripped. A numeric percentage is required (bare "confident"
# or "Confidence: high" never match).
_TRAILING_CONFIDENCE_RE = _re.compile(
    r"(?:(?<=[.!?])[ \t]*\n+|\n+)"                                  # block starts on its own line/para
    r"(?:"
    r"Confidence:[ \t]*\d+[ \t]*%?(?:(?!\n\n)[\s\S])*"              # (i) "Confidence: 90%" (+ adjacent tail)
    r"|"
    r"(?:I['’]?m|I[ \t]+am)[ \t]+\d+[ \t]*%?[ \t]+confident\b(?:(?!\n\n)[\s\S])*"  # (ii) "I am 90% confident ..."
    r")"
    r"\s*\Z",
    _re.IGNORECASE,
)


def _strip_trailing_confidence_block(text):
    """Remove a trailing self-confidence tail ("Confidence: N%" line and/or an
    adjacent "I am N% confident ..." paragraph) that Mixtral spontaneously
    appends after the answer, when it sits at the very END of the response with
    nothing substantive after it. Trailing-anchored (\\Z) and gated to its own
    line, so a mid-sentence "confident" or a real answer that continues after
    the phrase is never touched. Additive: a no-op when the tail is absent.
    """
    if not text:
        return text
    m = _TRAILING_CONFIDENCE_RE.search(text)
    if not m:
        return text
    return text[: m.start()].rstrip()


# Inline source attributions the model writes alongside the real [N] citation
# pills, e.g. "(Source: Owl Labs, 2019)" or the fused "[1, 5](Sources: ...)".
# Real sources render as pills, so these parentheticals are redundant clutter.
# Only parentheticals that OPEN with Source:/Sources: (any case of the first
# letter) are matched; the regex deliberately excludes any preceding [N]
# marker, so the fused form loses only the parenthetical and keeps its marker.
# Ordinary parentheticals -- "(for example, ...)", "(around 20 percent)",
# "(such as X)", "(7 to 9 hours)" -- do not start with Source:/Sources: and are
# left untouched.
_INLINE_SOURCE_ATTR_RE = _re.compile(r"\(\s*[Ss]ources?\s*:[^)]*\)")


def _strip_inline_source_attributions(text):
    """Remove inline (Source: ...) / (Sources: ...) parentheticals, preserving
    a fused [N] marker and any other (non-source) parenthetical. Tidies the
    whitespace / punctuation spacing left where a mid-sentence group is cut."""
    if not text or "ource" not in text:
        return text
    new = _INLINE_SOURCE_ATTR_RE.sub("", text)
    if new == text:
        return text
    # Collapse the double space / space-before-punctuation a mid-sentence
    # removal leaves behind (e.g. "productive , contributing" -> "productive,").
    new = _re.sub(r"[ \t]{2,}", " ", new)
    new = _re.sub(r"[ \t]+([,.;:!?])", r"\1", new)
    return new


def _strip_urls(text, keep_urls=False):
    """Bulletproof URL + citation-block cleanup.

    Ordering matters: complete URL patterns run before the partial catchall
    so they strip cleanly; dangling-connector and orphan-[N] cleanup runs
    last so they only catch leftovers.
    """
    if not text:
        return text
    # 1) Drop trailing References/Sources/etc. block wholesale.
    text = _strip_trailing_ref_block(text)
    # 2) Connector-phrase + URL together ("Source: https://...").
    text = _URL_WITH_PREFIX_RE.sub("", text)
    # 3) [N] + URL ("[1] https://...").
    text = _REF_BRACKET_URL_RE.sub("", text)
    # 4) Markdown links: keep anchor, drop URL.
    text = _MD_LINK_URL_RE.sub(r"\1", text)
    # When keep_urls is True (e.g. the user explicitly asked for a URL),
    # skip the bare/angle/partial URL removal and the dangling-promise
    # cleanup so a real URL the model gave as the answer is preserved.
    if not keep_urls:
        # 5) Complete angle-bracketed URLs.
        text = _ANGLE_URL_RE.sub("", text)
        # 6) Bare complete URLs.
        text = _BARE_URL_RE.sub("", text)
        # 7) Partial / truncated URL remnants from token cutoff.
        text = _PARTIAL_URL_RE.sub("", text)
        # 7b) Truncated trailing "<" fragments left behind when token limit
        # cuts off before the URL body even started. _ANGLE_URL_RE and
        # _PARTIAL_URL_RE require http(s) inside the brackets, so a "<"
        # with no body still slips through. Examples:
        #   "...retrieved from <"     -> ""
        #   "...source: <"            -> ""
        #   "...etrieved from <"      -> "" (truncated connector form)
        #   "Read more at\n  <"       -> "Read more at"
        # Pattern A drops a known attribution connector + "<...$".
        # Pattern B drops any orphan "<" sitting alone at end of a line
        # (whitespace required before it so mid-prose "<" is preserved).
        text = _re.sub(
            r"(?im)(?:,\s*)?"
            r"\b(?:available\s+at|retrieved\s+from|read\s+more\s+at|"
            r"source[s]?|see(?:\s+also)?|found\s+at|"
            r"more\s+(?:info(?:\s+at)?|details|at)|"
            r"link[s]?|url|visit)"
            r"\s*[:\-]?\s*<[^>\n]*$",
            "",
            text,
        )
        text = _re.sub(r"(?m)\s+<[^\s>]*$", "", text)
        # 7c) Dangling URL promises: the model announces a link/URL/site but
        # provides none ("The URL for X is.", "The website is", "you can find it
        # at", "here is the link", "It is available at."). Real URLs were already
        # stripped above; the negative lookahead additionally protects any URL or
        # domain that survived, and the end-of-line anchor keeps sentences that
        # continue ("the site is great", "available at 5pm", "found it at the
        # store") intact. Only fires at a clause boundary (line start / after a
        # sentence terminator) so mid-sentence appends are left untouched rather
        # than corrupted.
        text = _re.sub(
            r"(?im)"
            r"(?:(?<=[.!?])\s+|^)"
            r"(?:"
            r"the\s+(?:url|link|website|site)(?:\s+for\s+[^.:\n]{1,50})?\s+is"
            r"|here\s+is\s+the\s+(?:link|url|website)"
            r"|you\s+can\s+(?:find|access|visit|reach)\s+(?:it|them)\s+at"
            r"|(?:it|this|they|these|the\s+\w+(?:\s+\w+){0,2})\s+(?:is|are|can\s+be)\s+available\s+at"
            r"|available\s+at"
            r"|visit\s+(?:it|the\s+(?:site|website|link))\s+at"
            r")"
            r"(?![^\n]*(?:https?://|www\.|[a-z0-9-]+\.[a-z]{2,}))"
            r"\s*[:\-]?\s*[.!?]*\s*(?=\n|$)",
            "",
            text,
        )
    # 8) Cleanup: empty parens/brackets left behind by markdown strip.
    text = _re.sub(r"\(\s*\)", "", text)
    text = _re.sub(r"\[\s*\]", "", text)
    # 8b) Drop orphan "(-" artifacts left over when URL stripping consumes
    # a URL inside a parenthesized phrase. Example pipeline:
    #   raw  "(<https://buzz.com>-uzz)"
    #   -> URL strip -> "(-uzz)"
    #   -> here     -> "uzz"
    # Two passes:
    #   First, the full "(-word)" form so the closing paren goes with it
    #   ("(-uzz)" -> "uzz", "(-nd)" -> "nd", "(-hese)" -> "hese",
    #    "(-, foo)" -> "foo").
    #   Then a fallback for the open-only case where the close paren was
    #   already lost upstream ("(-uzz " -> "uzz "). Numeric content like
    #   "(-1, 2)" stays untouched because both patterns require a letter.
    text = _re.sub(r"\(-,?\s*([a-zA-Z][a-zA-Z]*)\)", r"\1", text)
    text = _re.sub(r"\(-,?\s*(?=[a-zA-Z])", "", text)
    # 9) Dangling connector phrases with no URL remaining.
    text = _DANGLING_PREFIX_RE.sub("", text)
    text = _DANGLING_SOURCE_LINE_RE.sub("", text)
    # 10) Orphan [N] markers on their own lines (inline [N] preserved).
    text = _ORPHAN_REF_NUM_RE.sub("", text)
    return text


# ============================================================
# Response deduplication: the model sometimes loops, restarting
# a numbered list or repeating a paragraph. Detect that and
# truncate at the repetition boundary, then trim to the last
# complete sentence so the output reads cleanly.
# ============================================================
_NUMBERED_ITEM_RE = _re.compile(r"(?m)^\s*(\d{1,2})[.)]\s+")
_SENTENCE_END_RE  = _re.compile(r"[.!?](?=[\s\"'\)\]]|$)")
_WORD_RE = _re.compile(r"\w+")


def _normalize_for_compare(s):
    """Lowercase and collapse non-word runs so 'Foo. bar' matches 'foo bar'."""
    return _re.sub(r"\W+", " ", s.lower()).strip()


def _trim_to_last_sentence(text):
    """Trim trailing partial clause to the last complete sentence."""
    if not text:
        return text
    matches = list(_SENTENCE_END_RE.finditer(text))
    if not matches:
        return text.rstrip()
    return text[: matches[-1].end()].rstrip()


def _snap_to_word_boundary(text, idx):
    """Walk back from idx to the last whitespace so we never truncate inside a word."""
    if idx <= 0 or idx >= len(text):
        return idx
    if text[idx - 1].isspace() or text[idx].isspace():
        return idx
    j = idx
    while j > 0 and not text[j - 1].isspace():
        j -= 1
    return j if j > 0 else idx


def _deduplicate_response(text):
    """Detect repeating paragraphs or list restarts and truncate."""
    if not text or len(text) < 200:
        return text

    # 1) Numbered-list restart: a second "1." opener usually means the model
    #    looped and restarted the list. But a legitimately different second
    #    list (e.g. a "Cons:" list after a "Pros:" list) also opens with "1.".
    #    Only treat it as a restart and cut when BOTH: (a) the second "1." is
    #    NOT introduced by a new section heading, and (b) its first item is a
    #    near-duplicate of the first list's first item (real repeated content,
    #    not just shared "1./2./3." scaffolding).
    opener_ones = [m for m in _NUMBERED_ITEM_RE.finditer(text) if m.group(1) == "1"]
    if len(opener_ones) >= 2:
        cutoff = opener_ones[1].start()
        # Only treat as a restart if there was substantive content before it
        if cutoff > 120:
            # (a) Section-heading guard: a short line ending in ":" or a bold
            #     "**...**" line right before the 2nd "1." marks a new, distinct
            #     list -> not a loop.
            _preceding = text[:cutoff].rstrip("\n")
            _last_line = _preceding.rsplit("\n", 1)[-1].strip()
            _heading_before = _last_line.endswith(":") or (
                _last_line.startswith("**") and _last_line.endswith("**"))
            # (b) Content guard: real loop only if the 2nd list's first item
            #     actually repeats the 1st list's first item's content.
            def _item_body(_m):
                _start = _m.end()
                _nxt = _NUMBERED_ITEM_RE.search(text, _start)
                _end = _nxt.start() if _nxt else len(text)
                return _normalize_for_compare(text[_start:_end])
            _b1 = _item_body(opener_ones[0])
            _b2 = _item_body(opener_ones[1])
            _similar = bool(_b1) and bool(_b2) and (
                _b2[:100] in _b1 or _b1[:100] in _b2)
            if not _heading_before and _similar:
                cutoff = _snap_to_word_boundary(text, cutoff)
                return _trim_to_last_sentence(text[:cutoff])

    # 2) Paragraph-level duplication: if a paragraph (>= ~10 words) has a
    #    normalised 100-char prefix that already appears earlier in the
    #    normalised text, treat as a loop and cut at that paragraph.
    paragraphs = text.split("\n\n")
    if len(paragraphs) >= 2:
        running_norm = ""
        offset = 0  # character offset into the original text of this paragraph
        for i, para in enumerate(paragraphs):
            norm = _normalize_for_compare(para)
            word_count = len(_WORD_RE.findall(para))
            if word_count >= 10:
                key = norm[:100]
                if key and key in running_norm:
                    cut = offset
                    if cut > 120:
                        cut = _snap_to_word_boundary(text, cut)
                        return _trim_to_last_sentence(text[:cut])
            running_norm += " " + norm
            offset += len(para) + 2  # +2 for the "\n\n" separator

    # 3) Numbered-item duplication across different numbers (e.g. item 4 is a
    #    rehash of item 2): compare normalised item bodies.
    items = list(_NUMBERED_ITEM_RE.finditer(text))
    if len(items) >= 3:
        bodies = []
        for idx, m in enumerate(items):
            start = m.end()
            end = items[idx + 1].start() if idx + 1 < len(items) else len(text)
            body = _normalize_for_compare(text[start:end])
            if len(body) < 40:
                bodies.append(body)
                continue
            key = body[:100]
            for prev in bodies:
                if key and key in prev:
                    cut = m.start()
                    if cut > 120:
                        cut = _snap_to_word_boundary(text, cut)
                        return _trim_to_last_sentence(text[:cut])
            bodies.append(body)

    return text


# ============================================================
# Citation clamping: Mixtral occasionally cites a source number
# that exceeds the actual RAG source count (e.g. "[14]" when
# only 4 sources were returned). When the real count is known
# we clamp any out-of-range [N] to [max_sources]. With max_sources
# unset or 0 we leave [N] alone — non-RAG prose may legitimately
# use bracketed numbers and the existing pipeline already strips
# orphan [N] markers in `_strip_urls()`.
# ============================================================
_CITATION_NUM_RE = _re.compile(r"\[\s*(\d{1,3})\s*\]")


def _cap_citation_numbers(text, max_sources):
    """Clamp [N] where N > max_sources to [max_sources]; drop [0]/[N<1]."""
    if not text or not max_sources or max_sources < 1:
        return text

    def _fix(m):
        n = int(m.group(1))
        if n < 1:
            return ""
        if n > max_sources:
            return f"[{max_sources}]"
        return m.group(0)

    return _CITATION_NUM_RE.sub(_fix, text)


_HEDGE_MARKERS = (
    "not possible to provide",
    "no specific answer",
    "no exact answer",
    "difficult to provide an exact",
    "cannot provide an exact",
    "depends on various factors",
)

# Broad hedge detector: paraphrase family of "depends on ... factors",
# "no clear/specific/exact answer", "(not possible/difficult) to provide", etc.
_HEDGE_RE = _re.compile(
    r"depends on \w+ (?:factors|variables|conditions)|no (?:clear|specific|exact|single|definitive|one|precise) answer|(?:not possible|no way|impossible|difficult|hard) to (?:provide|give|determine|say|estimate|calculate)|there is no (?:single|one|exact|specific|clear) (?:answer|number)|varies (?:greatly|widely|depending|considerably)",
    _re.IGNORECASE,
)

# Leading "However,"/"That said,"/"In any case," optionally followed by an
# "I can provide/offer/share/give ..." bridge clause up to its first . or :
_HEDGE_HOWEVER_RE = _re.compile(
    r"^(?:however|that said|in any case|in either case),?\s*(?:i can (?:provide|offer|share|give)[^.:]*[.:])?\s*",
    _re.IGNORECASE,
)


def _strip_hedge_preamble(text):
    """Drop a leading hedge sentence (e.g. "It is not possible to provide an
    exact number ... depends on various factors.") plus any "However, I can
    provide ..." bridge that follows it, so the answer leads with substance.
    Returns text unchanged when the first sentence is not a hedge."""
    if not text:
        return text
    sentences = _re.split(r"(?<=[.!?])\s+", text.strip())
    drop = 0
    for s in sentences[:3]:
        if _HEDGE_RE.search(s):
            drop += 1
        else:
            break
    if drop == 0:
        return text
    rest = " ".join(sentences[drop:]).strip()
    rest = _HEDGE_HOWEVER_RE.sub("", rest, count=1).strip()
    if not rest:
        return text
    return rest[0].upper() + rest[1:]


_PARTIAL_CITATION_RE = _re.compile(r"\[\s*\d{0,3}\s*$")


def safe_emit_len_for_citations(text):
    """Return the index where a trailing unclosed citation bracket begins, else len(text). Used in streaming to hold back a partial citation until it is complete and can be capped."""
    if not text:
        return 0
    m = _PARTIAL_CITATION_RE.search(text)
    return m.start() if m else len(text)


# ============================================================
# String-based fallback for "Based on the search results"-style filler
# prefixes. The regex pattern in _FILLER_PATTERNS works correctly in
# unit tests but does not match reliably in the live API path; this
# fallback runs after all regex stripping and uses a plain-string
# comparison against a small list of known prefix forms so the
# observable user-facing text stays clean even when the regex misses.
# ============================================================
_FALLBACK_PREFIXES = (
    "based on the search results provided,",
    "based on the search results,",
    "based on the provided search results,",
    "according to the search results provided,",
    "according to the search results,",
)


def _strip_fallback_prefix(text):
    """Drop any of `_FALLBACK_PREFIXES` from the very start of `text`
    (case-insensitive) and capitalize the first letter of what remains.
    No-op when the text doesn't start with one of the known prefixes."""
    if not text:
        return text
    text = text.lstrip()
    lower = text.lower()
    for prefix in _FALLBACK_PREFIXES:
        if lower.startswith(prefix):
            rest = text[len(prefix):].lstrip()
            if rest:
                rest = rest[0].upper() + rest[1:]
            return rest
    return text


def could_match_fallback_prefix(text):
    """Return True if `text` could still grow into a `_FALLBACK_PREFIXES`
    match in a future step (i.e. the lowercase lstripped form is a non-empty
    prefix of one of the known fallback prefix strings). Used by the
    streaming websocket path to hold off on emitting a delta while the
    cumulative output is still potentially a head-fragment of a filler
    phrase that a later strip pass will remove. Once the text diverges from
    every known fallback prefix, this returns False and streaming resumes."""
    if not text:
        return True  # empty cumulative output could still grow into anything
    lower = text.lstrip().lower()
    if not lower:
        return True
    for prefix in _FALLBACK_PREFIXES:
        if prefix.startswith(lower):
            return True
    return False


# ============================================================
# Post-strip repair: handle the rare cases where a filler-phrase
# strip still leaves a partial word at the start (defensive layer
# behind the \b boundaries on the patterns themselves) or an
# incomplete trailing sentence after a clause was removed.
# ============================================================
# A short suffix-like fragment leading the response strongly suggests an
# over-eager strip ate the word's prefix (e.g. "...important" -> "tant"
# leftover). Catches the standard English derivational/inflectional suffix
# vocabulary when one sits alone at the very start of the text, followed
# by whitespace and more content.
_SUFFIX_FRAGMENT_RE = _re.compile(
    r"^[\s.,;:]*"
    r"(?:ing|tion|sion|ment|ness|able|ible|ant|ent|ly|ed|er|es|al|ity|ous|ive|ize|ise|s)"
    r"[.,;:]?\s+\S",
    _re.IGNORECASE,
)


def _trim_mid_word_start(text):
    """Drop a leading orphan word-fragment if a strip cut mid-word and left a
    standalone English suffix at the start (e.g. "ing the result is clear" or
    "tion was difficult"). Conservative on purpose: only fires when the first
    whitespace-delimited token is exactly a common suffix and another word
    follows. The primary defense against mid-word cuts is the `\\b` boundary
    on each filler pattern; this is the safety net for any case that slips
    through (or for edge cases caused by future patterns)."""
    if not text:
        return text
    m = _SUFFIX_FRAGMENT_RE.match(text)
    if not m:
        return text
    # Drop the suffix fragment but keep the character that the lookahead
    # `\\S` consumed back in the result by trimming only up to the start of
    # that following non-space char.
    cut = m.end() - 1
    return text[cut:].lstrip()


def _trim_incomplete_trailing_sentence(text):
    """If `text` ends without a sentence terminator, trim back to the last
    complete sentence. Preserves text that ends with closing punctuation
    immediately after a terminator (e.g. '.")', '?")'). Returns the input
    unchanged when no earlier terminator exists."""
    if not text:
        return text
    rstripped = text.rstrip()
    if not rstripped:
        return text
    last = rstripped[-1]
    if last in ".!?":
        return text
    if last in "\"')]}" and len(rstripped) >= 2 and rstripped[-2] in ".!?":
        return text
    trimmed = _trim_to_last_sentence(rstripped)
    if trimmed and trimmed != rstripped:
        return trimmed
    return text


# URL-seeking queries: the user explicitly wants a URL/link/website/address.
# When true, callers pass keep_urls=True so a URL the model gives as the direct
# answer survives the URL-stripping pipeline.
_URL_QUERY_RE = _re.compile(
    r"\bwhat(?:'?s| is| are)?\s+(?:the\s+)?(?:url|link|website|web\s*site|web\s*address|web\s*page|homepage)\b"
    r"|\b(?:url|link|website|web\s*site|site|web\s*address|web\s*page|homepage)\b[^.\n]{0,25}\b(?:of|for)\b"
    r"|\b(?:of|for)\b[^.\n]{0,25}\b(?:url|link|website|web\s*site|web\s*address|web\s*page|homepage)\b",
    _re.IGNORECASE,
)


def is_url_query(text):
    """True when the user is asking for a URL/link/website/address, e.g.
    "what is the url of X", "link for X", "website of X". Case-insensitive.
    Used to set keep_urls=True so a URL the model gives as the direct answer
    survives the URL-stripping pipeline."""
    return bool(text and _URL_QUERY_RE.search(text))


def _normalize_sections(text):
    """Split a multi-section answer cleanly: when a short line ending with a
    colon introduces a list, make that heading its own paragraph and restart
    the numbered list at 1 beneath it. No-op unless the text has at least one
    such heading and at least one numbered item."""
    if not text:
        return text
    lines = text.split("\n")
    heading = _re.compile(r"^\s*\*{0,2}[A-Za-z][^:\n]{0,58}:\*{0,2}\s*$")
    item = _re.compile(r"^(\s*)(?:\d+[.)]+|[-*\u2022])(\s+)(.*)$")
    if not any(heading.match(l) for l in lines):
        return text
    if not any(item.match(l) for l in lines):
        return text
    out = []
    counter = 0
    for line in lines:
        if heading.match(line):
            if out and out[-1].strip() != "":
                out.append("")
            out.append(line.rstrip())
            out.append("")
            counter = 0
            continue
        m = item.match(line)
        if m:
            counter += 1
            out.append(m.group(1) + str(counter) + ". " + m.group(3))
            continue
        out.append(line)
    return "\n".join(out)


def strip_filler_phrases(text, max_citations=None, is_final=True, keep_urls=False):
    """Remove hedging/filler phrases, raw URLs, loop-repetition, and
    out-of-range citation markers from LLM output. When `max_citations`
    is provided (the number of RAG sources actually returned for this
    response), any inline [N] with N > max_citations is clamped to
    [max_citations]. `is_final` gates the post-strip word/sentence
    repair pass — callers that stream incremental output should pass
    `is_final=False` so the trailing partial sentence is kept until
    the final tick (otherwise per-step trimming would non-monotonically
    shrink the cumulative filtered text)."""
    if not text:
        return text
    text = _strip_opening_meta_sentence(text)
    text = _strip_hedge_preamble(text)
    for pat in _FILLER_PATTERNS:
        text = pat.sub("", text)
    text = _strip_fallback_prefix(text)
    text = _strip_urls(text, keep_urls=keep_urls)
    text = _deduplicate_response(text)
    text = _cap_citation_numbers(text, max_citations)
    if is_final:
        text = _trim_mid_word_start(text)
        text = _trim_incomplete_trailing_sentence(text)
        text = _normalize_sections(text)
        # Remove a trailing fabricated academic citation (no heading, so
        # _strip_trailing_ref_block above misses it). is_final only, so it
        # runs on the websocket final_text and the http one-shot, never on a
        # streaming step where the citation may still be forming.
        text = _strip_trailing_fabricated_citation(text)
        # Remove a trailing self-confidence tail ("Confidence: N%" and/or an
        # "I am N% confident ..." paragraph) Mixtral spontaneously appends. No
        # prompt source; trailing-anchored (\Z) so it never touches mid-text.
        text = _strip_trailing_confidence_block(text)
        # Strip inline (Source: ...) attributions the model writes
        # alongside the real [N] pills (mid-sentence, so not caught by
        # the trailing strippers above). Preserves any fused [N] marker.
        text = _strip_inline_source_attributions(text)
    text = _re.sub(r" {2,}", " ", text)
    text = _re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = _re.sub(r",\s*,+", ",", text)
    text = _re.sub(r"\.\s*\.+", ".", text)
    text = _re.sub(r"\n{3,}", "\n\n", text)
    text = _re.sub(r"^[\s.,;:]+", "", text)
    text = _re.sub(r"\s+\.\s+", ". ", text)
    # Meta-strip fixups: if a sentence terminator landed directly against a
    # capital letter ("X.Y"), reinsert the word space; and if stripping left
    # a lowercase leading letter ("caffeine ..."), capitalize it.
    text = _re.sub(r"([.!?])([A-Z])", r"\1 \2", text)
    text = _re.sub(r"^(\s*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text, count=1)
    # Re-capitalize the first letter of any paragraph (\n\n + lowercase).
    # Mixtral occasionally starts paragraphs with a lowercase word ("there
    # are…", "while tourism…"); this step normalizes them. Runs last so any
    # earlier strip that introduces a new paragraph break is also covered.
    text = _re.sub(r"(\n\n)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    return text


# Citation-marker post-processing (moved here from websocket_api.py; these are
# response-cleanup helpers and belong with the rest of the post-processing).
def _filter_cited_sources(text, sources):
    """Return only the source entries whose 1-based index appears as an [n]
    citation marker in the model output text. Empty list if none were cited."""
    if not sources:
        return []
    cited = set()
    for grp in re.findall(r"\[([\d,\s]+)\]", text or ""):
        for part in grp.split(","):
            part = part.strip()
            if part.isdigit():
                cited.add(int(part))
    return [sources[n - 1] for n in sorted(cited) if 1 <= n <= len(sources)]


def _strip_orphan_markers(text, n_sources):
    """Remove [n] citation markers whose index has no matching source (index
    out of range, or ALL markers when n_sources == 0), so the reader never sees
    a citation marker without a corresponding source pill. Valid in-range
    indices are kept; a compound marker like [1, 5] keeps only the valid parts
    and is dropped entirely if none remain."""
    if not text:
        return text

    def _repl(m):
        valid = [p.strip() for p in m.group(1).split(",")
                 if p.strip().isdigit() and 1 <= int(p.strip()) <= n_sources]
        return "[" + ", ".join(valid) + "]" if valid else ""

    out = re.sub(r"\[([\d,\s]+)\]", _repl, text)
    # Tidy whitespace left where a marker was removed.
    out = re.sub(r" {2,}", " ", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    return out
