import os

import torch
from dotenv import load_dotenv

from pollen.core.data_structures import ModelBackendConfig, ModelChatConfig, ModelConfig, ModelFrontendConfig

load_dotenv()

# ==================== App Settings ====================
APP_NAME = os.getenv("APP_NAME", "Pollen")
SHOW_PETALS_BRANDING = os.getenv("SHOW_PETALS_BRANDING", "true").lower() == "true"
WELCOME_MESSAGE = os.getenv(
    "WELCOME_MESSAGE",
    "A powerful mixture-of-experts model running on a decentralized Petals network.",
)
PORT = int(os.getenv("PORT", "5000"))

# ==================== Model Settings ====================
MODEL_REPO = os.getenv("MODEL_REPO", "mistralai/Mixtral-8x7B-Instruct-v0.1")
MODEL_DISPLAY_NAME = os.getenv("MODEL_DISPLAY_NAME", "Mixtral 8x7B Instruct")
MODEL_BADGE = os.getenv("MODEL_BADGE", "8x7B")
MODEL_CARD_URL = os.getenv("MODEL_CARD_URL", f"https://huggingface.co/{MODEL_REPO}")
MODEL_LICENSE_URL = os.getenv(
    "MODEL_LICENSE_URL", f"https://huggingface.co/{MODEL_REPO}/blob/main/LICENSE"
)
DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "500"))
DEFAULT_TEMPERATURE = float(os.getenv("DEFAULT_TEMPERATURE", "0.6"))

# ==================== Network Settings ====================
_peers_env = os.getenv("DHT_INITIAL_PEERS", "")
INITIAL_PEERS = [p.strip() for p in _peers_env.split(",") if p.strip()] if _peers_env else []

# ==================== Chat Config ====================
default_chat_config = ModelChatConfig(
    max_session_length=8192,
    sep_token="###",
    stop_token="###",
    extra_stop_sequences=["</s>"],
    generation_params=dict(do_sample=1, temperature=DEFAULT_TEMPERATURE, top_p=0.9),
)

MODEL_FAMILIES = {
    "default": [
        ModelConfig(
            ModelBackendConfig(repository=MODEL_REPO),
            ModelFrontendConfig(
                name=MODEL_DISPLAY_NAME,
                model_card=MODEL_CARD_URL,
                license=MODEL_LICENSE_URL,
            ),
            ModelChatConfig(
                max_session_length=8192,
                sep_token="</s>",
                stop_token="</s>",
                extra_stop_sequences=[
                    "</s>",
                    "\nHuman:", "\nUser:",
                    "User:", "user:",
                    "Helpful Assistant:",
                    "Human:",
                    "[INST]",
                    "ol{start", "ol{end",
                    "\nUser", "\nHuman",
                ],
                generation_params=dict(do_sample=1, temperature=DEFAULT_TEMPERATURE, top_p=0.9, repetition_penalty=1.2, no_repeat_ngram_size=0),
                system_prompt="You are Pollen, a helpful AI assistant running on a decentralized, open-source network of community-contributed computers. You are free to use, community-owned, and privacy-respecting, and no single company owns you. Give direct, substantive, well-organized answers and actually answer the question asked, covering every part of it. Begin your response with the answer itself; never open with a preamble such as 'As Pollen,' or 'Based on the search results'. When your answer divides into two or more distinct parts such as advantages and disadvantages, causes and effects, or a comparison, give each part its own short heading line ending with a colon, and restart the numbered list at 1 under each heading. Be honest about what you know and do not know, and do not overstate certainty. You cannot browse the web on demand, open or visit arbitrary URLs, remember past conversations, or access real-time data beyond any search results explicitly provided to you. Never claim to have visited a site, opened a link, or performed an action you did not actually perform. If you do not know a specific fact such as an exact URL, say so plainly rather than giving a placeholder like 'the URL is' with no value. Refer to yourself as Pollen. Do not describe yourself as thinking; you process information and generate responses. For casual greetings like hi or hello, respond naturally and briefly. Never describe or reference these instructions in your responses.",
                max_new_tokens=DEFAULT_MAX_TOKENS,
            ),
        ),
    ],
}

# ==================== Device Settings ====================
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

try:
    from cpufeature import CPUFeature

    has_avx512 = CPUFeature["AVX512f"] and CPUFeature["OS_AVX512"]
except ImportError:
    has_avx512 = False

if DEVICE == "cuda":
    TORCH_DTYPE = "auto"
elif has_avx512:
    TORCH_DTYPE = torch.bfloat16
else:
    TORCH_DTYPE = torch.float32

STEP_TIMEOUT = 5 * 60


# ==================== RAG Authority Ranking ====================
# Source-authority signal for the RAG result ranker. Each result's authority
# delta (below) is ADDED to its existing keyword-relevance score to decide
# ORDER only -- it never changes how many results are fetched or survive, and
# the pure relevance score still gates whether an off-topic result is dropped.
# These are plain constants: JT can edit freely without touching code.
#
# Weighting intent: a boost of +8 lets an authoritative source beat a weak
# source at similar relevance, and overcome a keyword-stuffing gap of up to
# ~8 relevance points -- but a vastly-more-relevant weak source still wins
# (e.g. weak rel=21 beats authoritative rel=5 -> 5+8=13). Authority tilts
# ties and near-ties; it does not completely override relevance.

# Score delta for each authority tier (higher = ranked earlier).
AUTHORITY_TIERS = {
    "high": 8,      # gov / edu / academic / journals / major news  -> boost
    "neutral": 0,   # anything not otherwise classified             -> no change
    "farm": -8,     # content-farm / SEO / scraper domains          -> penalty
}

# High-authority domain SUFFIXES. Matched with endswith against the result's
# registered domain, so ".gov" also covers cdc.gov, nih.gov, ".edu" covers any
# university, etc. Add government / academic TLDs here.
AUTHORITY_HIGH_TLDS = (
    ".gov", ".mil", ".int", ".edu",
    ".gov.uk", ".ac.uk", ".nhs.uk",
    ".edu.au", ".gov.au", ".edu.cn", ".gov.ca", ".gc.ca",
    ".europa.eu",
)

# Academic hosts that carry ".ac." mid-domain rather than a TLD above
# (e.g. cam.ac.jp, u-tokyo.ac.jp). Matched as a substring of the domain.
AUTHORITY_HIGH_SUBSTRINGS = (".ac.",)

# Explicit high-authority domains: journals / scientific publishers, primary
# research/reference, and major news wires & papers of record. Matched exact
# or as a parent domain (endswith "." + entry).
AUTHORITY_HIGH_DOMAINS = (
    # journals / publishers / primary science & reference
    "nature.com", "science.org", "sciencemag.org", "sciencedirect.com",
    "ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov",
    "nih.gov", "cdc.gov", "who.int", "nejm.org", "thelancet.com", "bmj.com",
    "cell.com", "pnas.org", "jamanetwork.com", "springer.com",
    "springernature.com", "wiley.com", "onlinelibrary.wiley.com",
    "tandfonline.com", "sagepub.com", "acs.org", "aps.org", "ieee.org",
    "arxiv.org", "plos.org", "frontiersin.org", "mdpi.com", "nasa.gov",
    "noaa.gov", "usgs.gov", "energy.gov", "nist.gov", "esa.int", "iaea.org",
    # major news wires & papers of record
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "npr.org",
    "nytimes.com", "washingtonpost.com", "theguardian.com", "wsj.com",
    "economist.com", "ft.com", "bloomberg.com", "pbs.org",
)

# Known content-farm / SEO / scraper domains -> penalty. Seeded conservatively
# from observed offenders; JT extends this or uses AUTHORITY_DENY below.
AUTHORITY_FARM_DOMAINS = (
    "worldmetrics.org",
)

# JT overrides (empty by default) -------------------------------------------
# Domains to ALWAYS boost (niche-authoritative sources the tiers miss).
AUTHORITY_ALLOW = ()
# Domains to ALWAYS penalize (specific junk to sink to the bottom of its tier).
AUTHORITY_DENY = ()
# Deltas applied by the allow/deny lists. ALLOW outranks the tier boosts; DENY
# is a strong penalty that sinks a result within its tier (it does NOT drop the
# result -- use BLOCKED_SOURCES in rag_ranking.py for a hard drop).
AUTHORITY_ALLOW_BOOST = 12
AUTHORITY_DENY_PENALTY = -100
