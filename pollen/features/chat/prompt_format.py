"""Single source of truth for chat prompt formatting.

Supports BOTH Mixtral's `[INST] ... [/INST]` format and ChatML (used by
Nous-Hermes-2-Mixtral-8x7B-DPO and other ChatML models), selected by
config.PROMPT_FORMAT ("inst" | "chatml").

INVARIANT: while MODEL_REPO is Mixtral, PROMPT_FORMAT == "inst" and every
builder / parser here returns byte-for-byte what the call sites produced before
this module existed. The "chatml" branch is additive and only becomes active
when MODEL_REPO switches to a ChatML model.
"""

import re

from pollen.core import config


def _fmt(mode=None):
    """Resolve the active format ('inst' or 'chatml')."""
    return mode if mode is not None else getattr(config, "PROMPT_FORMAT", "inst")


def is_chatml(mode=None):
    return _fmt(mode) == "chatml"


def stop_sequences(mode=None):
    """extra_stop_sequences appropriate to the active prompt format.

    inst -> ["</s>"] (Mixtral EOS), chatml -> ["<|im_end|>"] (ChatML turn end).
    """
    return ["<|im_end|>"] if _fmt(mode) == "chatml" else ["</s>"]


def insert_marker(mode=None):
    """Substring after which augment_prompt_in_place injects RAG context.

    inst -> "[INST]" (exactly as before), chatml -> "<|im_start|>user\n".
    """
    return "<|im_start|>user\n" if _fmt(mode) == "chatml" else "[INST]"


def wrap_user(text, mode=None):
    """Wrap a bare user message.

    inst -> "[INST] {text} [/INST]" (byte-identical to the old inline wrap).
    """
    if _fmt(mode) == "chatml":
        return "<|im_start|>user\n%s<|im_end|>\n<|im_start|>assistant\n" % text
    return "[INST] %s [/INST]" % text


def build_plain(system_prefix, user_message, mode=None):
    """No-context prompt. inst -> "{system_prefix}[INST] {user} [/INST]"."""
    if _fmt(mode) == "chatml":
        sys_block = ("<|im_start|>system\n%s<|im_end|>\n" % system_prefix) if system_prefix else ""
        return "%s<|im_start|>user\n%s<|im_end|>\n<|im_start|>assistant\n" % (sys_block, user_message)
    return "%s[INST] %s [/INST]" % (system_prefix, user_message)


def build_augmented(system_prefix, instruction, context_block, user_message, mode=None):
    """RAG-augmented prompt.

    inst -> exactly:
        {system_prefix}[INST] {instruction}\n\n{context_block}\nUser question: {user} [/INST]
    """
    if _fmt(mode) == "chatml":
        sys_block = ("<|im_start|>system\n%s<|im_end|>\n" % system_prefix) if system_prefix else ""
        return (
            "%s<|im_start|>user\n%s\n\n%s\nUser question: %s<|im_end|>\n<|im_start|>assistant\n"
            % (sys_block, instruction, context_block, user_message)
        )
    return (
        "%s[INST] %s\n\n%s\nUser question: %s [/INST]"
        % (system_prefix, instruction, context_block, user_message)
    )


def build_irc(bot_name, user_nick, user_message, context_block, mode=None):
    """IRC prompt (concise). Branches on whether context_block is present.

    inst -> exactly the two f-strings augment_for_irc used before.
    """
    chatml = _fmt(mode) == "chatml"
    if context_block:
        if chatml:
            return (
                "<|im_start|>system\nYou are %s, a helpful IRC bot powered by Mixtral "
                "on the Petals network. Use the search results below to answer accurately. "
                "Keep answers concise (under 250 chars). Cite sources briefly.<|im_end|>\n"
                "<|im_start|>user\n%s\nUser %s asks: %s<|im_end|>\n<|im_start|>assistant\n"
                % (bot_name, context_block, user_nick, user_message)
            )
        return (
            "[INST] You are %s, a helpful IRC bot powered by Mixtral "
            "on the Petals network. Use the search results below to answer accurately. "
            "Keep answers concise (under 250 chars). Cite sources briefly.\n\n"
            "%s\nUser %s asks: %s [/INST]"
            % (bot_name, context_block, user_nick, user_message)
        )
    if chatml:
        return (
            "<|im_start|>system\nYou are %s, a helpful IRC bot powered by Mixtral "
            "on the Petals network. Keep answers concise (under 250 chars).<|im_end|>\n"
            "<|im_start|>user\nUser %s says: %s<|im_end|>\n<|im_start|>assistant\n"
            % (bot_name, user_nick, user_message)
        )
    return (
        "[INST] You are %s, a helpful IRC bot powered by Mixtral "
        "on the Petals network. Keep answers concise (under 250 chars). "
        "User %s says: %s [/INST]"
        % (bot_name, user_nick, user_message)
    )


def quick_portion(inputs, mode=None):
    """Text after the last user-turn marker, lowercased & stripped.

    Used to detect the "quick:" shortcut. inst -> byte-identical to the old
    `(inputs or "").rsplit("[INST]", 1)[-1].strip().lower()`.
    """
    marker = "<|im_start|>user\n" if _fmt(mode) == "chatml" else "[INST]"
    return (inputs or "").rsplit(marker, 1)[-1].strip().lower()


_INST_RE = re.compile(r'\[INST\]\s*(.*?)\s*\[/INST\]', re.DOTALL)
_CHATML_USER_RE = re.compile(r'<\|im_start\|>user\n(.*?)<\|im_end\|>', re.DOTALL)


def extract_user_message(prompt):
    """Extract the last user message from an [INST] OR a ChatML prompt.

    For [INST] prompts this is byte-identical to the previous implementation
    (same regex, same '\\n\\n'-split tail). ChatML is handled additively.
    """
    matches = _INST_RE.findall(prompt or "")
    if matches:
        return matches[-1].strip().split('\n\n')[-1].strip()
    cm = _CHATML_USER_RE.findall(prompt or "")
    if cm:
        return cm[-1].strip().split('\n\n')[-1].strip()
    # Trailing user block with no closing <|im_end|> (assistant turn open).
    idx = (prompt or "").rfind("<|im_start|>user\n")
    if idx >= 0:
        tail = (prompt or "")[idx + len("<|im_start|>user\n"):]
        tail = tail.split("<|im_end|>")[0]
        return tail.split('\n\n')[-1].strip()
    return (prompt or "").strip()
