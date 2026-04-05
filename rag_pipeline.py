"""RAG pipeline: detects queries needing current info, searches, and builds augmented prompts."""

import logging
import re

import rag_search

log = logging.getLogger("rag_pipeline")

# Keywords/patterns that suggest the user wants current or factual info
_CURRENT_INFO_PATTERNS = [
    r"\b(latest|recent|current|today|tonight|yesterday|this week|this month|this year)\b",
    r"\b(news|update|score|price|weather|stock|release|announced|happened|launched)\b",
    r"\b(who is|who was|where is|when is|when was|when did)\b",
    r"\b(how much does|how many|how to get|how to find|where can|where to)\b",
    r"\b(recommend|suggestion|best|top \d|popular|trending|fun to do|things to do)\b",
    r"\b(2024|2025|2026|2027)\b",
]
_CURRENT_INFO_RE = re.compile("|".join(_CURRENT_INFO_PATTERNS), re.IGNORECASE)

# Patterns for messages that should NOT trigger search — these are handled
# better by the model alone without web context
_SKIP_PATTERNS = [
    # Greetings and short social messages
    r"^(hi|hello|hey|howdy|yo|sup|thanks|thank you|bye|goodbye|ok|yes|no|sure|lol|haha|gm|gn)\s*[!.?]*$",
    # Creative writing requests
    r"^(write|create|generate|make|compose|draft|tell me a|sing|imagine)\b",
    r"\b(poem|story|joke|song|essay|letter|script|code|function|program|class|snippet)\b",
    # Math and calculations
    r"\b\d+\s*[+\-*/^%]\s*\d+",
    r"\b(calculate|compute|solve|simplify|evaluate|convert \d)\b",
    r"^what is \d+",
    # Programming and coding
    r"\b(python|javascript|java|rust|golang|typescript|html|css|sql|bash|regex)\b.*\b(function|code|script|sort|loop|array|list|class|implement)\b",
    r"\b(function|code|script|sort|loop|array|list|class|implement)\b.*\b(python|javascript|java|rust|golang|typescript|html|css|sql|bash|regex)\b",
    r"^(explain|describe|define|what does|how does)\b.*\b(code|function|algorithm|syntax|error|bug|exception)\b",
    # Hypothetical and opinion questions
    r"^(what if|would you|could you|can you|do you|are you)\b",
    # Translation
    r"\b(translate|translation)\b",
]
_SKIP_RE = re.compile("|".join(_SKIP_PATTERNS), re.IGNORECASE)



def extract_user_message(prompt):
    """Extract the last user message from a Mixtral [INST]...[/INST] prompt."""
    # Find the last [INST] ... [/INST] block
    matches = re.findall(r'\[INST\]\s*(.*?)\s*\[/INST\]', prompt, re.DOTALL)
    if not matches:
        return prompt.strip()
    last = matches[-1].strip()
    # If there's a system prompt, it's separated by two newlines; take the last part
    parts = last.split('\n\n')
    return parts[-1].strip()


def augment_prompt_in_place(full_prompt, results):
    """Inject RAG context into an existing Mixtral prompt without re-wrapping.

    Inserts the context block and instruction right after the last [INST] tag.
    """
    context_block = format_context(results)
    if not context_block:
        return full_prompt

    instruction = (
        "You have been given current web search results. Answer the question using specific details from the results. "
        "Include names, dates, numbers, and key facts. Do not give vague summaries. "
        "Do not say you cannot access the articles or tell the user to visit sources. "
        "Present information confidently as facts. Refer to sources by number like [1]. Do not include URLs.\n\n"
    )

    # Find the last [INST] and inject context after it
    last_inst = full_prompt.rfind("[INST]")
    if last_inst < 0:
        return full_prompt

    insert_pos = last_inst + len("[INST]")
    # Skip any whitespace after [INST]
    while insert_pos < len(full_prompt) and full_prompt[insert_pos] in " \t":
        insert_pos += 1

    return (
        full_prompt[:insert_pos]
        + instruction
        + context_block + "\n\n"
        + "User question: "
        + full_prompt[insert_pos:]
    )


def needs_search(message):
    """Decide whether a message would benefit from web search context.

    Returns True for current events, news, factual lookups, recommendations.
    Returns False for greetings, math, coding, creative writing.
    """
    if not rag_search.is_enabled():
        return False
    text = message.strip()
    if len(text) < 10:
        return False
    # Skip patterns take priority — if it looks like code/math/creative, don't search
    if _SKIP_RE.search(text):
        log.debug("needs_search(%r) -> False (skip pattern matched)", text[:60])
        return False
    result = bool(_CURRENT_INFO_RE.search(text))
    log.debug("needs_search(%r) -> %s", text[:60], result)
    return result


def format_context(results):
    """Format search results into a context block for the LLM prompt."""
    if not results:
        return ""
    lines = ["[Web Search Results]"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   {r['snippet']}")
        lines.append(f"   Source: {r['url']}")
    lines.append("[End of Search Results]\n")
    return "\n".join(lines)


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
        results = rag_search.search(user_message)

    if results:
        context_block = format_context(results)

    if context_block:
        instruction = (
            "You have been given current web search results. Answer the question using specific details from the results. "
            "Include names, dates, numbers, and key facts. Do not give vague summaries. "
            "Do not say you cannot access the articles or tell the user to visit sources. "
            "Present information confidently as facts. Refer to sources by number like [1]. Do not include URLs."
        )
        prompt = (
            f"{system_prefix}"
            f"[INST] {instruction}\n\n"
            f"{context_block}\n"
            f"User question: {user_message} [/INST]"
        )
    else:
        prompt = f"{system_prefix}[INST] {user_message} [/INST]"

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

    if context_block:
        prompt = (
            f"[INST] You are {bot_name}, a helpful IRC bot powered by Mixtral "
            f"on the Petals network. Use the search results below to answer accurately. "
            f"Keep answers concise (under 250 chars). Cite sources briefly.\n\n"
            f"{context_block}\n"
            f"User {user_nick} asks: {user_message} [/INST]"
        )
    else:
        prompt = (
            f"[INST] You are {bot_name}, a helpful IRC bot powered by Mixtral "
            f"on the Petals network. Keep answers concise (under 250 chars). "
            f"User {user_nick} says: {user_message} [/INST]"
        )

    return prompt, results
