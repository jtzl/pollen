"""Image generation module for Pollen.

Uses Stable Diffusion via the diffusers library.
Configured via .env: IMAGE_ENABLED, IMAGE_MODEL, IMAGE_DEVICE, IMAGE_STEPS, IMAGE_WIDTH, IMAGE_HEIGHT.
Switching from CPU to GPU is just changing IMAGE_DEVICE=cuda in .env.
"""

import io
import os
import uuid
import time
import logging
import threading

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("image_gen")

# -- Configuration from .env --
IMAGE_ENABLED = os.getenv("IMAGE_ENABLED", "false").lower() == "true"
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "stabilityai/sdxl-turbo")
IMAGE_DEVICE = os.getenv("IMAGE_DEVICE", "cpu")
IMAGE_STEPS = int(os.getenv("IMAGE_STEPS", "4"))
IMAGE_WIDTH = int(os.getenv("IMAGE_WIDTH", "512"))
IMAGE_HEIGHT = int(os.getenv("IMAGE_HEIGHT", "512"))
IMAGE_OUTPUT_DIR = os.getenv("IMAGE_OUTPUT_DIR", "/data/chat-ui/static/generated")

# -- Module state --
_pipeline = None
_pipeline_lock = threading.Lock()
_loading = False


def _ensure_output_dir():
    os.makedirs(IMAGE_OUTPUT_DIR, exist_ok=True)


def get_pipeline():
    """Lazy-load the diffusion pipeline. Thread-safe."""
    global _pipeline, _loading
    if _pipeline is not None:
        return _pipeline

    with _pipeline_lock:
        # Double-check after acquiring lock
        if _pipeline is not None:
            return _pipeline
        if not IMAGE_ENABLED:
            raise RuntimeError("Image generation is disabled (IMAGE_ENABLED=false)")

        _loading = True
        log.info("Loading image model %s on %s ...", IMAGE_MODEL, IMAGE_DEVICE)
        start = time.time()

        import torch
        from diffusers import AutoPipelineForText2Image

        dtype = torch.float16 if IMAGE_DEVICE == "cuda" else torch.float32

        pipe = AutoPipelineForText2Image.from_pretrained(
            IMAGE_MODEL,
            torch_dtype=dtype,
            variant="fp16" if IMAGE_DEVICE == "cuda" else None,
        )
        pipe = pipe.to(IMAGE_DEVICE)

        # CPU optimizations
        if IMAGE_DEVICE == "cpu":
            try:
                pipe.enable_attention_slicing()
            except Exception:
                pass

        _pipeline = pipe
        _loading = False
        elapsed = time.time() - start
        log.info("Image model loaded in %.1fs", elapsed)
        return _pipeline


def is_enabled():
    return IMAGE_ENABLED


def is_loading():
    return _loading


def is_ready():
    return _pipeline is not None


def generate_image(prompt, steps=None, width=None, height=None, guidance_scale=0.0):
    """Generate an image from a text prompt.

    Returns:
        dict with keys: ok, filename, path, elapsed
    """
    pipe = get_pipeline()
    _ensure_output_dir()

    steps = steps or IMAGE_STEPS
    width = width or IMAGE_WIDTH
    height = height or IMAGE_HEIGHT

    # SDXL-Turbo works best with guidance_scale=0.0 and few steps
    # For other models, a higher guidance_scale (e.g. 7.5) is typical
    if "turbo" not in IMAGE_MODEL.lower():
        guidance_scale = guidance_scale or 7.5

    log.info("Generating image: prompt=%r steps=%d size=%dx%d", prompt, steps, width, height)
    start = time.time()

    import torch
    generator = None
    result = pipe(
        prompt=prompt,
        num_inference_steps=steps,
        guidance_scale=guidance_scale,
        width=width,
        height=height,
        generator=generator,
    )

    image = result.images[0]
    filename = f"{uuid.uuid4().hex[:12]}.png"
    filepath = os.path.join(IMAGE_OUTPUT_DIR, filename)
    image.save(filepath)

    elapsed = time.time() - start
    log.info("Image generated in %.1fs -> %s", elapsed, filepath)

    return {
        "ok": True,
        "filename": filename,
        "path": filepath,
        "elapsed": round(elapsed, 1),
    }
