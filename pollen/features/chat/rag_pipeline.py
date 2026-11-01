"""RAG pipeline: detects queries needing current info, searches, and builds augmented prompts."""

import logging
import os
import re

from pollen.features.chat.retrieval import rag_search
from pollen.features.chat import prompt_format

log = logging.getLogger("rag_pipeline")

# Factual signals: current events, stats, comparisons, recommendations,
# history/definitions/causes — aggressive trigger for search.
_FACTUAL_PATTERNS = [
    # Current events / time-bounded
    r"\b(latest|recent|current|today|tonight|yesterday|this week|this month|this year)\b",
    r"\b(news|update|score|price|weather|stock|release|announced|happened|launched|report|study)\b",
    r"\b20(2[3-9]|[3-9]\d)\b",
    # WH + copula/aux (factual lookup)
    r"\b(who|what|where|when|why|how)\b.{0,40}\b(is|was|are|were|did|does|has|have|had|will|would)\b",
    # Quantitative / statistical
    r"\b(statistics?|stat|data|average|mean|median|percent|percentage|rate|ratio|per\s+(capita|year|day|month|hour|week))\b",
    r"\bhow\s+(much|many|often|long|fast|tall|heavy|big|small|old)\b",
    # Comparisons
    r"\b(compare|comparison|versus|vs\.?|difference between|better than|worse than|pros and cons)\b",
    # Recommendations
    r"\b(recommend|suggestion|best|top\s*\d*|popular|trending|favorite|which .{0,20}should|what .{0,20}should (i|we))\b",
    # Factual framings
    r"\b(facts? about|history of|origin of|definition of|cause of|reason for|effect of|impact of|examples of)\b",
    # "how to" (frequently wants current info/tutorials)
    r"^\s*how\s+(to|do i|can i)\b",
]
_FACTUAL_RE = re.compile("|".join(_FACTUAL_PATTERNS), re.IGNORECASE)

# Strict skip: greetings, pure math, trivial acknowledgements.
_STRICT_SKIP_PATTERNS = [
    # Bare greeting / acknowledgement
    r"^(hi|hello|hey|howdy|yo|sup|thanks|thank you|bye|goodbye|ok|okay|yes|no|yeah|nope|sure|lol|haha|gm|gn|good morning|good night|good evening|good afternoon|nm|nothing)\s*[!.?,]*$",
    # Greeting followed by a short trailing token: "hi there", "hello friend", "hey bot"
    r"^(hi|hello|hey|howdy|yo|hola|sup|gm|gn|good (morning|night|evening|afternoon))\s+(there|friend|bot|mate|pal|folks|team|y'all|everyone|pollen|pollenbot|chat)\s*[!.?,]*$",
    # Common greeting questions that don't need search
    r"^(how are you|how's it going|hows it going|what'?s up|whats up|howdy partner|how goes it)\s*[!.?,]*$",
    # Pure arithmetic expression
    r"^[\s\d+\-*/^%().=,]+$",
    # "what is 5 + 3" / "calculate 2 * 7"
    r"^\s*(what is|whats|what's)\s+\d+\s*[+\-*/^%]",
    r"^\s*(calculate|compute|simplify|evaluate)\s+[\d.()]",
]
_STRICT_SKIP_RE = re.compile("|".join(_STRICT_SKIP_PATTERNS), re.IGNORECASE)

# Basic coding-syntax questions: narrow — only skip if this matches AND no factual signal.
_CODE_SYNTAX_PATTERNS = [
    r"^(what does|what is)\s+\w+\s+(keyword|operator|method|syntax|do in|mean in)\b",
    r"^how do i\s+(declare|print|return|import|define|install|pip install|npm install|require|type)\b",
    r"\b(syntax error|syntaxerror|null pointer|segfault|segmentation fault|stack trace|stacktrace|traceback)\b",
    r"^(explain|describe|define|what does|how does)\b.{0,40}\b(this code|this function|this snippet|this regex|this loop)\b",
]
_CODE_SYNTAX_RE = re.compile("|".join(_CODE_SYNTAX_PATTERNS), re.IGNORECASE)

# Opinion / creative / hypothetical: still search (for context) but answer leans on model.
_OPINION_PATTERNS = [
    r"^(do you|would you|could you|can you|are you|will you)\b",
    r"\b(your opinion|in your opinion|what do you think|do you think|do you feel|what would you|would you rather)\b",
    r"\b(hypothetical|imagine|what if|suppose)\b",
    r"^\s*(write|create|generate|make|compose|draft|tell me a|sing)\b",
    r"\b(poem|story|joke|song|essay|letter|script|haiku|limerick|rap)\b",
    r"\b(advice (on|for|about)|give me advice|help me decide)\b",
]
_OPINION_RE = re.compile("|".join(_OPINION_PATTERNS), re.IGNORECASE)

# Identity / self-reference: questions about who/what Pollen is. These must be
# answered from the system-prompt identity, never a web search (which pulls
# competitor facts like ChatGPT/Claude specs and makes Pollen misdescribe itself).
_IDENTITY_PATTERNS = [
    r"\bare you\s+(chatgpt|gpt-?4|gpt|claude|gemini|bard|an ai|a bot|a language model|a human|real|sentient|conscious)\b",
    r"\b(who|what)\s+(are|made|created|trained|built|owns)\s+you\b",
    r"\bwhat\s+(model|llm|ai|company)\s+are you\b",
    r"\byour name\b",
]
_IDENTITY_RE = re.compile("|".join(_IDENTITY_PATTERNS), re.IGNORECASE)

# Greeting-only subset of _STRICT_SKIP_PATTERNS. Used to short-circuit
# hi/hello/hey/good morning etc. with a canned reply instead of sending
# the raw text through Mixtral (which used to free-form continue into
# unrelated content like product names or tutorials).
_GREETING_PATTERNS = [
    r"^(hi|hello|hey|howdy|yo|sup|hola|gm|gn|good morning|good night|good evening|good afternoon)\s*[!.?,]*$",
    r"^(hi|hello|hey|howdy|yo|hola|sup|gm|gn|good (morning|night|evening|afternoon))\s+(there|friend|bot|mate|pal|folks|team|y\'all|everyone|pollen|pollenbot|chat)\s*[!.?,]*$",
    r"^(how are you|how\'s it going|hows it going|what\'?s up|whats up|howdy partner|how goes it)\s*[!.?,]*$",
]
_GREETING_RE = re.compile("|".join(_GREETING_PATTERNS), re.IGNORECASE)

GREETING_RESPONSES = (
    "Hey! How can I help?",
    "Hello! What can I help you with?",
    "Hi there! What would you like to know?",
    "Hey there! Got a question?",
    "Hello! What brings you here today?",
    "Hi! What can I look up for you?",
)


def is_greeting(message):
    """True if the message is a bare greeting that should get a canned reply
    instead of being sent to Mixtral. Stricter than classify_query=='skip'
    (which also covers bare math like '2+2')."""
    if not message:
        return False
    return bool(_GREETING_RE.search(message.strip()))


def random_greeting_response():
    import random as _random
    return _random.choice(GREETING_RESPONSES)


# --- Follow-up search contextualization --------------------------------------
# In a multi-turn chat a follow-up like "what about in children?" used to be
# searched verbatim, with no topic words at all, so the results went off-topic.
# When the last message looks anaphoric, the SEARCH QUERY (never the prompt)
# gets the content words of the preceding question(s) appended. Detection errs
# narrow: a self-contained question must pass through unchanged.

_FOLLOWUP_OPENER_RE = re.compile(
    r"^\s*(and|but|also|then|so|what about|how about|what if|why not|ok(?:ay)? but)\b"
    r"|^\s*(why|how|when|where|who|what)\s*\??\s*$",
    re.IGNORECASE)

_FOLLOWUP_PRONOUN_RE = re.compile(
    r"\b(it|its|that|those|these|they|them|their|this|he|she|him|her|the same)\b",
    re.IGNORECASE)

_SEARCH_STOPWORDS = frozenset(
    ("the a an and or but of in on at to for with about from by as is are was "
     "were be been being do does did have has had will would can could should "
     "may might what which who whom when where why how any some many much more "
     "most other same so than then too very just also there here you your i we "
     "my our me us they them their it its he she him her his hers this that "
     "these those not no yes if else over under again new old tell say please "
     "give show me latest recent current news").split())


def _content_terms(text, limit=6):
    """First `limit` distinct non-stopword terms of 3+ chars, in text order."""
    terms, seen = [], set()
    for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", text or ""):
        lw = w.lower()
        if lw in _SEARCH_STOPWORDS or lw in seen:
            continue
        seen.add(lw)
        terms.append(w)
        if len(terms) >= limit:
            break
    return terms


def is_followup(message):
    """True when a message likely leans on the previous turn for its topic:
    an anaphoric opener ("and ...", "what about ...", bare "why?"), or a short
    message (<= 10 words) that contains a pronoun."""
    t = (message or "").strip()
    if not t:
        return False
    if _FOLLOWUP_OPENER_RE.search(t):
        return True
    return len(t.split()) <= 10 and bool(_FOLLOWUP_PRONOUN_RE.search(t))


def _contextualize_followup(user_messages, limit=6):
    """Search query for the LAST of `user_messages` (all user turns, oldest
    first): unchanged unless it is a follow-up, in which case up to `limit`
    content words from the preceding question(s) are appended, walking back
    through consecutive follow-ups to the question that set the topic."""
    if not user_messages:
        return ""
    last = user_messages[-1]
    if len(user_messages) == 1 or not is_followup(last):
        return last
    own = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", last)}
    terms = []
    for prev in reversed(user_messages[:-1]):
        for w in _content_terms(prev, limit):
            if w.lower() in own or len(terms) >= limit:
                continue
            own.add(w.lower())
            terms.append(w)
        if (terms and not is_followup(prev)) or len(terms) >= limit:
            break  # reached the question that set the topic, or enough terms
    if not terms:
        return last
    return last.rstrip() + " " + " ".join(terms)


def contextualized_query(full_prompt):
    """The search query for the last user turn of a (possibly multi-turn)
    prompt. Same as extract_user_message unless that turn is a follow-up."""
    return _contextualize_followup(prompt_format.extract_user_messages(full_prompt))



# --- Back-compat aliases (do not remove — other code may import these) ---
_CURRENT_INFO_PATTERNS = _FACTUAL_PATTERNS
_CURRENT_INFO_RE = _FACTUAL_RE
_SKIP_PATTERNS = _STRICT_SKIP_PATTERNS
_SKIP_RE = _STRICT_SKIP_RE



def extract_user_message(prompt):
    """Extract the last user message from an [INST] or ChatML prompt."""
    return prompt_format.extract_user_message(prompt)


# Prompt-size budget. The budget used to be derived from the servers' old
# 2048-token KV ceiling (1348 tokens), which silently truncated the lower
# half of the ranked sources out of the prompt while their pills still
# showed on the page. Since 2026-10-11 every server accepts 8192
# (MAX_SESSION_LENGTH), so the ceiling no longer binds; the budget is its
# own dial. 2200 fits all RAG_MAX_RESULTS=8 sources with ~600-char
# snippets. Raising it buys more context per source but slows prefill and
# burns the 8192-token session in fewer conversation turns.
_PROMPT_TOKEN_BUDGET = int(os.getenv("PROMPT_TOKEN_BUDGET", "2200"))
# Conservative chars-per-token estimate for Mixtral's sentencepiece tokenizer
# on English text (real tokenizer averages ~3.5 chars/token; we use 3 for safety).
_CHARS_PER_TOKEN = 3


def _estimate_tokens(text):
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN if text else 0


def _fit_prompt_to_budget(full_prompt, context_block, results=None):
    """Shrink context_block inside full_prompt so total estimated tokens <= budget.

    When `results` is given, the block is REBUILT with every snippet trimmed
    to an even share, so every source keeps its [n] entry in the prompt (the
    page numbers its pills from this same order; the old tail-chop silently
    dropped the lower-ranked sources from the prompt while their pills still
    showed). Without `results` it falls back to the tail-chop.

    Returns (fitted_prompt, fitted_context_block). If the context block is
    not present in full_prompt (or fits already), returns inputs unchanged.
    """
    if not context_block:
        return full_prompt, context_block
    if _estimate_tokens(full_prompt) <= _PROMPT_TOKEN_BUDGET:
        return full_prompt, context_block

    # overhead = prompt chars that are *not* the context block
    overhead = max(0, len(full_prompt) - len(context_block))
    budget_chars = _PROMPT_TOKEN_BUDGET * _CHARS_PER_TOKEN
    allowed_ctx = budget_chars - overhead
    if allowed_ctx < 400:
        # Overhead alone already too large; nothing more we can safely trim.
        return full_prompt, context_block

    if results:
        trimmed = format_context(results, max_chars=allowed_ctx)
    else:
        trimmed = context_block[:allowed_ctx]
        # Cut at the last newline so we don't leave a half-line dangling
        nl = trimmed.rfind("\n")
        if nl > 200:
            trimmed = trimmed[:nl]
        trimmed = trimmed.rstrip() + "\n[... context truncated to fit token budget ...]\n[End of Search Results]\n"
    new_prompt = full_prompt.replace(context_block, trimmed, 1)
    log.info("RAG prompt trimmed: %d->%d chars (~%d->%d tokens)",
             len(full_prompt), len(new_prompt),
             _estimate_tokens(full_prompt), _estimate_tokens(new_prompt))
    return new_prompt, trimmed


_FACTUAL_INSTRUCTION = (
    "Base your answer primarily on the search results provided below, and cite them by number like [1] when you use a specific fact. Do not include URLs. "
    "Cite ONLY with those bracketed numbers inside your sentences. Never append a reference list, bibliography or works-cited section: the sources are shown to the reader automatically, and invented publication details (volumes, pages, DOIs) are worse than none. "
    "When the search results are thin, irrelevant, or missing, answer from your own knowledge instead. Do not refuse or say you could not find information just because the search was weak. "
    "Synthesize across sources, include exact numbers, dates, names, and data points, and draw connections between them. "
    "Write as an expert explaining to a peer, not as a search engine listing results. "
    "Give direct specific answers with concrete data. Never hedge with phrases like 'however this can vary', 'it depends on the person', 'some people may', 'its important to note'. If you have a specific answer give it. If you genuinely do not know, say so briefly. Do not pad responses with obvious qualifiers. "
    "Lead with the direct answer in your very first sentence. If no exact figure exists, give a best estimate or a range immediately, then explain. Never open by stating you cannot provide an exact number or that it depends on various factors."
)

_QUICK_INSTRUCTION = (
    "Base your answer on the search results below. Cite sources by number like [1]. No URLs. "
    "Answer in one or two sentences at most, direct answer only, no preamble, no caveats, no extra context. "
    "If no exact answer exists give a best estimate or range in one sentence. If you dont know, say so in one sentence."
)

_OPINION_INSTRUCTION = (
    "You are a knowledgeable assistant. The user is asking an opinion, creative, or advice-style question \u2014 answer primarily from your own reasoning and experience. "
    "Use the search results below as supplementary context only, not as primary evidence. "
    "Draw on them when they offer a specific fact worth citing, otherwise trust your own knowledge. Write naturally and directly. "
    "Refer to sources by number like [1] only when you cite a specific fact from them. Do not include URLs. "
    "Give direct specific answers. Never hedge with phrases like 'however this can vary', 'it depends on the person', 'some people may', 'its important to note'. If you have an answer give it. If you dont know say so. Do not pad responses with obvious qualifiers."
)

_COMMUNITY_INSTRUCTION = (
    "Some of the sources below are community forums or user groups. "
    "When referencing these, recommend the user connect with the community "
    "directly for more detailed help, rather than just summarizing the content."
)



_LIST_INSTRUCTION = (
    "When the user asks for a list of alternatives, recommendations, or examples, "
    "provide specific named examples with brief descriptions rather than explaining "
    "the general concept. If the search results do not contain specific named examples "
    "that directly match what the user asked for, say so explicitly rather than "
    "presenting tangentially related items as if they answered the question."
)

_LIST_QUERY_RE = re.compile(
    r"\b("
    r"best\s+\w+|"
    r"top\s+\d*\s*\w+|"
    r"alternatives?\s+to\b|"
    r"\w+\s+alternatives?\b|"
    r"recommendations?\b|"
    r"recommend\s+(?:me\s+)?\w+|"
    r"examples?\s+of\b|"
    r"list\s+(?:of|some)\b|"
    r"what\s+are\s+(?:some|the\s+best)\b|"
    r"(?:any|some|good)\s+\w+\s+(?:for|to)\b"
    r")",
    re.IGNORECASE,
)


def _is_list_query(text):
    return bool(text and _LIST_QUERY_RE.search(text))


def _extend_instruction(instruction, results, user_msg):
    """Append optional instruction snippets based on result domains and query shape."""
    if any(rag_search.is_community_domain(r.get("url", "")) for r in (results or [])):
        instruction = instruction + " " + _COMMUNITY_INSTRUCTION
    if _is_list_query(user_msg):
        instruction = instruction + " " + _LIST_INSTRUCTION
    return instruction


def augment_prompt_in_place(full_prompt, results):
    """Inject RAG context into an existing Mixtral prompt without re-wrapping.

    Inserts the context block and instruction right after the last [INST] tag.
    """
    context_block = format_context(results)
    if not context_block:
        return full_prompt

    user_msg = extract_user_message(full_prompt)
    if user_msg.strip().lower().startswith("quick:"):
        instruction = _QUICK_INSTRUCTION
    else:
        mode = classify_query(user_msg)
        instruction = _OPINION_INSTRUCTION if mode == "opinion" else _FACTUAL_INSTRUCTION
        instruction = _extend_instruction(instruction, results, user_msg)
    instruction = instruction + "\n\n"

    # Find the last user-turn marker and inject context after it
    marker = prompt_format.insert_marker()
    last_inst = full_prompt.rfind(marker)
    if last_inst < 0:
        return full_prompt

    insert_pos = last_inst + len(marker)
    # Skip any whitespace after the marker
    while insert_pos < len(full_prompt) and full_prompt[insert_pos] in " \t":
        insert_pos += 1

    built = (
        full_prompt[:insert_pos]
        + context_block + "\n\n"
        + instruction
        + "User question: "
        + full_prompt[insert_pos:]
    )
    fitted, _ = _fit_prompt_to_budget(built, context_block, results)
    return fitted


def classify_query(message):
    """Classify a user message as 'skip', 'factual', or 'opinion'.

    - 'skip':    pure greeting, bare arithmetic, or a narrow coding-syntax question.
    - 'factual': explicit factual/statistical/current-event/comparison/recommendation signal.
    - 'opinion': hypothetical/creative/advice-style. Still searches, but the prompt
                 instructs the model to rely more on its own knowledge.

    When unsure the classifier defaults to 'factual' so search runs by default.
    """
    t = (message or "").strip()
    if not t or len(t) < 4:
        return "skip"
    # Identity / self-reference questions ("are you ChatGPT", "who made you",
    # "what model are you") answer from the system-prompt identity; a web search
    # here just pulls competitor facts, so skip search for them.
    if _IDENTITY_RE.search(t):
        return "skip"
    # Only skip for pure greetings / acknowledgements and bare arithmetic.
    if _STRICT_SKIP_RE.search(t):
        return "skip"
    # Opinion / creative framing -> search with opinion-mode instructions.
    if _OPINION_RE.search(t) and not _FACTUAL_RE.search(t):
        return "opinion"
    # Everything else (factual, informational, coding, recommendations, how-to,
    # ambiguous queries) -> search with factual-mode instructions.
    return "factual"


def needs_search(message):
    """True when the message should trigger a web search. Aggressive by default."""
    if not rag_search.is_enabled():
        return False
    c = classify_query(message)
    log.debug("needs_search(%r) classify=%s -> %s", (message or "")[:60], c, c != "skip")
    return c != "skip"


def format_context(results, max_chars=None):
    """Format search results into a context block for the LLM prompt.

    max_chars: optional cap on the whole block. When the full block is over
    it, every snippet is trimmed to a RANK-WEIGHTED share (results arrive in
    final rank order, so source 1 keeps about 3x the room of the last one)
    with an 80-char floor. ALL sources stay in the prompt with their [n]
    numbers intact; sources are never dropped from the tail (the page
    numbers its pills from this same order).
    """
    if not results:
        return ""

    def _build(caps):
        lines = ["[Web Search Results]"]
        for i, r in enumerate(results, 1):
            snippet = r.get("snippet") or ""
            cap = caps[i - 1] if caps else None
            if cap is not None and len(snippet) > cap:
                cut = snippet[:cap].rsplit(" ", 1)[0]
                snippet = (cut if len(cut) >= 60 else snippet[:cap]) + "…"
            lines.append(f"{i}. {r.get('title', '')}")
            lines.append(f"   {snippet}")
            lines.append(f"   Source: {r.get('url', '')}")
        lines.append("[End of Search Results]\n")
        return "\n".join(lines)

    block = _build(None)
    if max_chars is None or len(block) <= max_chars:
        return block
    # Chars that are not snippet text (titles, URLs, markers) stay fixed;
    # split what remains across the snippets, weighted linearly from 1.5
    # (top-ranked) down to 0.5 (last), floor 80 chars each.
    overhead = len(block) - sum(len(r.get("snippet") or "") for r in results)
    avail = max(0, max_chars - overhead)
    n = len(results)
    weights = [1.5 - (i / max(1, n - 1)) for i in range(n)] if n > 1 else [1.0]
    wsum = sum(weights)
    caps = [max(80, int(avail * w / wsum)) for w in weights]
    return _build(caps)


def build_augmented_prompt(user_message, system_prefix="", search_results=None):
    """Search if needed and build an augmented Mixtral [INST] prompt.

    Args:
        user_message: The raw user message.
        system_prefix: Optional system-level text before [INST].
        search_results: Pre-fetched results, or None to auto-search.

    Returns:
        (prompt_string, search_results_or_empty_list)
    """
    results = []
    context_block = ""

    if search_results is not None:
        results = search_results
    elif needs_search(user_message):
        results = rag_search.search_with_refine(user_message)

    if results:
        context_block = format_context(results)

    if context_block:
        mode = classify_query(user_message)
        instruction = _OPINION_INSTRUCTION if mode == "opinion" else _FACTUAL_INSTRUCTION
        instruction = _extend_instruction(instruction, results, user_message)
        prompt = prompt_format.build_augmented(system_prefix, instruction, context_block, user_message)
        prompt, context_block = _fit_prompt_to_budget(prompt, context_block, results)
    else:
        prompt = prompt_format.build_plain(system_prefix, user_message)

    return prompt, results


def augment_for_irc(user_message, bot_name="PollenBot", user_nick="user"):
    """Build an augmented prompt sized for IRC (concise output).

    Returns:
        (prompt_string, search_results_or_empty_list)
    """
    results = []
    context_block = ""

    if needs_search(user_message):
        results = rag_search.search(user_message)
        context_block = format_context(results)

    prompt = prompt_format.build_irc(bot_name, user_nick, user_message, context_block)

    return prompt, results
