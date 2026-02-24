import os

import torch
from dotenv import load_dotenv

from data_structures import ModelBackendConfig, ModelChatConfig, ModelConfig, ModelFrontendConfig

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

# ==================== Network Settings ====================
_peers_env = os.getenv("DHT_INITIAL_PEERS", "")
INITIAL_PEERS = [p.strip() for p in _peers_env.split(",") if p.strip()] if _peers_env else []

# ==================== Chat Config ====================
default_chat_config = ModelChatConfig(
    max_session_length=8192,
    sep_token="###",
    stop_token="###",
    extra_stop_sequences=["</s>"],
    generation_params=dict(do_sample=1, temperature=0.6, top_p=0.9),
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
                generation_params=dict(do_sample=1, temperature=0.6, top_p=0.9, repetition_penalty=1.1),
                system_prompt="",
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
