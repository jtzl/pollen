"""Model-free FastAPI app serving Pollen's non-inference routes.

Serves ONLY:
  GET  /api/curated-sources        (ported from status_api.api_curated_sources)
  POST /api/generate-image         (ported from image_api.api_generate_image)
  GET  /api/image-status           (ported from image_api.api_image_status)
  GET  /static/generated/{file}    (ported from image_api.serve_generated_image)

Deliberately does NOT import app.py, utils, model_loader, or config, so it
never triggers load_models() and starts in well under a second. image_gen is
safe: it is a leaf module that calls load_dotenv() itself and lazy-loads any
diffusion pipeline only on first use.

Response bodies are rendered to match Flask's jsonify byte-for-byte
(json.dumps with sort_keys=True and compact separators, plus a trailing newline).
"""
import json
import logging
import os
import time
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from pollen.infrastructure import image_gen  # leaf module: loads .env itself, no model load

log = logging.getLogger("fastapi_app")

app = FastAPI(title="Pollen non-inference API", docs_url=None, redoc_url=None)


class FlaskJSONResponse(JSONResponse):
    """Render JSON exactly like Flask's jsonify: sorted keys + trailing newline."""

    def render(self, content) -> bytes:
        return (json.dumps(content, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# /api/curated-sources  -- logic ported from status_api.py (verbatim)
# ---------------------------------------------------------------------------
CURATED_SOURCES_URL = "https://organized.info/wp-json/pollen/v1/sources?limit=100"
CURATED_CATEGORIES_URL = "https://organized.info/wp-json/pollen/v1/categories"
CURATED_SOURCES_TTL = 300
_curated_sources_cache = {"data": None, "expires_at": 0.0}


def _split_category(raw):
    """Split a category string into (parent, sub).

    Accepts either a flat slug ("science") or a hierarchical form using one
    of "/", ">" or ":" as the separator ("science/biology", "Science > Biology").
    Returns lowercased, stripped (parent, sub) -- sub is "" for flat input.
    """
    if not raw:
        return ("", "")
    s = str(raw).strip()
    if not s:
        return ("", "")
    for sep in ("/", ">", ":"):
        if sep in s:
            parent, _, sub = s.partition(sep)
            return (parent.strip().lower(), sub.strip().lower())
    return (s.lower(), "")


def _build_categories(sources):
    """Group flat-source list into [{name, count, sources, subcategories}, ...].

    Sources without a subcategory live directly in the parent's `sources`.
    Sources with a slash-style category (e.g. "science/biology") nest under
    `subcategories`. Parents and subcategories are sorted alphabetically.
    """
    by_parent = {}
    for s in sources or []:
        parent, sub = _split_category(s.get("category", ""))
        if not parent:
            parent = "uncategorized"
        bucket = by_parent.setdefault(parent, {"sources": [], "subs": {}})
        if sub:
            sub_bucket = bucket["subs"].setdefault(sub, [])
            sub_bucket.append(s)
        else:
            bucket["sources"].append(s)
    result = []
    for parent in sorted(by_parent.keys()):
        bucket = by_parent[parent]
        subs = []
        for sub_name in sorted(bucket["subs"].keys()):
            sub_sources = bucket["subs"][sub_name]
            subs.append({
                "name": sub_name,
                "count": len(sub_sources),
                "sources": sub_sources,
            })
        total = len(bucket["sources"]) + sum(sc["count"] for sc in subs)
        result.append({
            "name": parent,
            "count": total,
            "sources": bucket["sources"],
            "subcategories": subs,
        })
    return result


@app.get("/api/curated-sources")
def api_curated_sources():
    """Server-side proxy for the WordPress curated-sources endpoint.

    Avoids browser CORS issues since the WP API does not set
    Access-Control-Allow-Origin. Response is cached in-process for
    CURATED_SOURCES_TTL seconds.
    """
    now = time.time()
    cached = _curated_sources_cache["data"]
    if cached is not None and now < _curated_sources_cache["expires_at"]:
        return FlaskJSONResponse(cached)

    try:
        req = urllib.request.Request(
            CURATED_SOURCES_URL,
            headers={"User-Agent": "Pollen-RAG/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))

        sources = data.get("sources", []) if isinstance(data, dict) else []

        # Best-effort fetch of the canonical category list. Used only to expose
        # category counts to the UI; grouping below still works without it.
        category_meta = []
        try:
            cat_req = urllib.request.Request(
                CURATED_CATEGORIES_URL,
                headers={"User-Agent": "Pollen-RAG/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(cat_req, timeout=5) as cat_resp:
                category_meta = json.loads(cat_resp.read().decode("utf-8", errors="replace"))
            if not isinstance(category_meta, list):
                category_meta = []
        except Exception as cat_err:
            log.info("Curated categories fetch skipped: %s", cat_err)

        if isinstance(data, dict):
            result = dict(data)
        else:
            result = {"sources": sources, "count": len(sources)}
        result["categories"] = _build_categories(sources)
        if category_meta:
            result["category_meta"] = category_meta

        _curated_sources_cache["data"] = result
        _curated_sources_cache["expires_at"] = now + CURATED_SOURCES_TTL
        return FlaskJSONResponse(result)
    except Exception as e:
        log.warning("Curated sources proxy fetch failed: %s", e)
        # Serve stale cache if available -- better than a hard error in the UI.
        if cached is not None:
            return FlaskJSONResponse(cached)
        return FlaskJSONResponse(
            {"ok": False, "error": str(e), "sources": [], "count": 0, "categories": []},
            status_code=502,
        )


# ---------------------------------------------------------------------------
# image routes -- logic ported from image_api.py
# ---------------------------------------------------------------------------
@app.post("/api/generate-image")
async def api_generate_image(request: Request):
    if not image_gen.is_enabled():
        return FlaskJSONResponse({"ok": False, "error": "Image generation is disabled"}, status_code=503)

    # Flask: (request.json or {}).get("prompt","").strip() if request.is_json
    #        else request.form.get("prompt","").strip()
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype == "application/json" or ctype.endswith("+json"):
        try:
            body = await request.json()
        except Exception:
            body = None
        prompt = ((body or {}).get("prompt", "") or "").strip()
    else:
        try:
            form = await request.form()
            prompt = (form.get("prompt") or "").strip()
        except Exception:
            prompt = ""

    if not prompt:
        return FlaskJSONResponse({"ok": False, "error": "No prompt provided"}, status_code=400)
    if len(prompt) > 500:
        return FlaskJSONResponse({"ok": False, "error": "Prompt too long (max 500 chars)"}, status_code=400)

    try:
        # generate_image blocks (remote HTTP or local diffusers); keep the loop free.
        result = await run_in_threadpool(image_gen.generate_image, prompt)
        return FlaskJSONResponse({
            "ok": True,
            "filename": result["filename"],
            "url": "/static/generated/%s" % result["filename"],
            "elapsed": result["elapsed"],
        })
    except Exception as e:
        log.error("Image generation failed: %s", e, exc_info=True)
        return FlaskJSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/image-status")
def api_image_status():
    return FlaskJSONResponse({
        "enabled": image_gen.is_enabled(),
        "loading": image_gen.is_loading(),
        "ready": image_gen.is_ready(),
        "model": image_gen.IMAGE_MODEL,
        "device": image_gen.IMAGE_DEVICE,
    })


@app.get("/static/generated/{filename}")
def serve_generated_image(filename: str):
    """Serve generated images from the output directory.

    Mirrors send_from_directory: rejects traversal and returns 404 when the
    file does not exist.
    """
    if not filename or filename in (".", "..") or "/" in filename or "\\" in filename:
        return Response(status_code=404)
    directory = os.path.abspath(image_gen.IMAGE_OUTPUT_DIR)
    path = os.path.abspath(os.path.join(directory, filename))
    if not path.startswith(directory + os.sep) or not os.path.isfile(path):
        return Response(status_code=404)
    return FileResponse(path)


# ===========================================================================
# /api/status -- model-free port of status_api.py
# ===========================================================================
# status_api.py reaches the Petals sequence manager through the loaded model
# purely to get two things: seq_manager.dht and seq_manager.block_uids.
# Neither actually needs model weights:
#   * the DHT is built from config.INITIAL_PEERS (client_mode), exactly as
#     petals/client/routing/sequence_manager.py:88-95 does when dht is None;
#   * block_uids are a pure naming convention -- see
#     petals/client/remote_sequential.py:46:
#         tuple(f"{config.dht_prefix}{UID_DELIMITER}{i}" for i in range(...))
#     where dht_prefix is the repo name with "." -> "-" (mixtral/config.py:27-31)
#     and the count is config.num_hidden_layers, both readable from the HF
#     cache offline without instantiating the model.
# Everything downstream (get_remote_module_infos / compute_spans) then matches
# status_api.py line for line.
import threading

from pollen.infrastructure import speed_tracker

_status_cache = {"data": None, "time": 0}
CACHE_TTL = 10

# Copied verbatim from status_api.py
NODE_MAP = {
    "WUG9d9G7tHHQ": {
        "name": "EC2 (A10G)",
        "is_hub": True,
        "vram_cmd": ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
    },
    "maM3HmC8hDZj": {
        "name": "Physical Node (RTX 3090)",
        "is_hub": False,
        "vram_cmd": ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                      "irfan@98.61.139.87",
                      "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits"],
    },
    "QZPFxNcv8eDh": {
        "name": "Zlamabama (RTX 4090)",
        "is_hub": False,
        "vram_cmd": ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                      "-p", "31332", "irfan@localhost",
                      "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits"],
    },
}


def _get_vram(cmd):
    """Run a command to get VRAM usage. Returns (used_mb, total_mb) or (None, None)."""
    import subprocess
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            parts = result.stdout.strip().split(",")
            if len(parts) == 2:
                return int(parts[0].strip()), int(parts[1].strip())
    except Exception:
        pass
    return None, None


# Lazily-built DHT + block UIDs, so process startup stays fast and the heavy
# petals/torch imports only happen on the first /api/status request.
_dht_lock = threading.Lock()
_dht_state = {"dht": None, "block_uids": None, "model_name": None}


def _ensure_dht():
    """Build (once) a client-mode DHT and the block UIDs, without any model."""
    if _dht_state["dht"] is not None:
        return _dht_state
    with _dht_lock:
        if _dht_state["dht"] is not None:
            return _dht_state
        from hivemind import DHT
        from petals.data_structures import UID_DELIMITER
        from petals.utils.auto_config import AutoDistributedConfig

        from pollen.core import config as pollen_config

        # Same key the Flask worker reports: models[] is keyed by
        # backend_config.key, and load_models() inserts that key first.
        family = list(pollen_config.MODEL_FAMILIES.values())[0]
        model_config = list(family)[0]
        model_name = model_config.backend.key
        repo = model_config.backend.repository

        # Reads config.json from the HF cache (offline); no weights loaded.
        distributed_config = AutoDistributedConfig.from_pretrained(repo)
        block_uids = tuple(
            "%s%s%d" % (distributed_config.dht_prefix, UID_DELIMITER, i)
            for i in range(distributed_config.num_hidden_layers)
        )

        dht = DHT(
            initial_peers=pollen_config.INITIAL_PEERS,
            client_mode=True,
            num_workers=32,
            start=True,
        )
        _dht_state["dht"] = dht
        _dht_state["block_uids"] = block_uids
        _dht_state["model_name"] = model_name
        log.info("status DHT ready: %d block uids, prefix=%s",
                 len(block_uids), distributed_config.dht_prefix)
    return _dht_state


@app.get("/api/status")
def api_status():
    now = time.time()
    if _status_cache["data"] and now - _status_cache["time"] < CACHE_TTL:
        return FlaskJSONResponse(_status_cache["data"])

    try:
        state = _ensure_dht()
        dht = state["dht"]
        block_uids = state["block_uids"]
        model_name = state["model_name"]
        num_blocks = len(block_uids)

        from petals.utils.dht import get_remote_module_infos, compute_spans
        from petals.data_structures import ServerState

        module_infos = get_remote_module_infos(dht, block_uids, latest=True)
        spans = compute_spans(module_infos, min_state=ServerState.ONLINE)

        # Build peer list
        peers = []
        peer_map = {}
        for peer_id, span in spans.items():
            pid = str(peer_id)[-12:]
            peer_info = {
                "peer_id": pid,
                "start": span.start,
                "end": span.end,
                "length": span.length,
                "throughput": round(span.server_info.throughput, 1) if span.server_info else 0,
            }
            peers.append(peer_info)
            peer_map[pid] = peer_info

        # Block coverage
        block_status = []
        for i, info in enumerate(module_infos):
            covered = False
            if info and info.servers:
                for pid, si in info.servers.items():
                    if si.state == ServerState.ONLINE:
                        covered = True
                        break
            block_status.append(covered)

        coverage = sum(block_status)

        # Build nodes array with VRAM info
        nodes = []
        seen_peers = set()
        for pid, node_info in NODE_MAP.items():
            peer = peer_map.get(pid)
            is_up = peer is not None
            seen_peers.add(pid)

            vram_used, vram_total = _get_vram(node_info["vram_cmd"])

            node = {
                "name": node_info["name"],
                "peer_id": pid,
                "status": "up" if is_up else "down",
                "blocks_start": peer["start"] if is_up else None,
                "blocks_end": peer["end"] if is_up else None,
                "num_blocks": peer["length"] if is_up else 0,
                "throughput": peer["throughput"] if is_up else 0,
                "vram_used_mb": vram_used,
                "vram_total_mb": vram_total,
                "is_hub": bool(node_info.get("is_hub", False)),
            }
            nodes.append(node)

        # Include any unknown peers not in NODE_MAP
        for peer in peers:
            if peer["peer_id"] not in seen_peers:
                nodes.append({
                    "name": "Unknown (%s)" % peer["peer_id"],
                    "peer_id": peer["peer_id"],
                    "status": "up",
                    "blocks_start": peer["start"],
                    "blocks_end": peer["end"],
                    "num_blocks": peer["length"],
                    "throughput": peer["throughput"],
                    "vram_used_mb": None,
                    "vram_total_mb": None,
                    "is_hub": False,
                })

        result = {
            "ok": True,
            "model_name": model_name,
            "num_peers": len(spans),
            "peers": peers,
            "nodes": nodes,
            "block_status": block_status,
            "block_coverage": coverage,
            "total_blocks": num_blocks,
            # Read from the shared state file the Flask worker publishes, so
            # these match what Flask reports rather than this process's own.
            "tokens_per_second": speed_tracker.get_shared_avg_speed(),
            "uptime_seconds": speed_tracker.get_shared_uptime(),
        }

        _status_cache["data"] = result
        _status_cache["time"] = now
        return FlaskJSONResponse(result)

    except Exception as e:
        log.warning("Status API error: %s", e, exc_info=True)
        return FlaskJSONResponse({"ok": False, "error": str(e)})
