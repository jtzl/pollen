"""Model-free FastAPI app serving Pollen's non-inference routes.

Serves:
  GET  /                           (main page; byte-identical to Flask via views.render_index)
  GET  /static/{path}              (general assets: chat.js, style.css, marked.min.js, logo, ...)
  GET  /api/curated-sources        (ported from status_api.api_curated_sources)
  POST /api/generate-image         (ported from image_api.api_generate_image)
  GET  /api/image-status           (ported from image_api.api_image_status)
  GET  /static/generated/{file}    (ported from image_api.serve_generated_image)

Deliberately does NOT import app.py, utils, or model_loader, so it never
triggers load_models() and starts in well under a second. It does import
views (and therefore pollen.core.config + image_gen) to pre-render the main
page, but that path loads no model. image_gen is safe: it is a leaf module
that calls load_dotenv() itself and lazy-loads any diffusion pipeline only on
first use.

Response bodies are rendered to match Flask's jsonify byte-for-byte
(json.dumps with sort_keys=True and compact separators, plus a trailing newline).
"""
import json
import logging
import os
import time
import urllib.request

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from pollen.infrastructure import image_gen  # leaf module: loads .env itself, no model load

log = logging.getLogger("fastapi_app")

app = FastAPI(title="Pollen non-inference API", docs_url=None, redoc_url=None)

# ---------------------------------------------------------------------------
# Main page "/" -- rendered ONCE at startup, byte-identical to Flask's.
# app.py serves index_html = views.render_index(app) where app is a Flask
# instance; render_index only uses pollen.core.config + image_gen for its
# template context, so we render it here with a throwaway bare Flask instance
# purely for its Jinja environment (same tojson filter, autoescape and
# context Flask uses). This imports no model and never calls load_models().
# ---------------------------------------------------------------------------
import views  # noqa: E402  (model-free: pulls pollen.core.config + image_gen only)
from flask import Flask as _RenderFlask  # noqa: E402

_INDEX_HTML = views.render_index(_RenderFlask(__name__))


@app.get("/")
def main_page():
    # Same no-cache headers Flask's main_page sets.
    return HTMLResponse(
        content=_INDEX_HTML,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )


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


# ===========================================================================
# STAGE 3 (DORMANT until POLLEN_LOAD_MODEL=1): model-holding streaming + one-shot.
#
# Everything below is INERT on the current pollen-fastapi.service, which does
# NOT set POLLEN_LOAD_MODEL. In that case no model is loaded (petals/torch are
# never even imported here), and the two routes below return a clear error if
# called. Only the future model-holding systemd unit sets POLLEN_LOAD_MODEL=1
# -- together with HF_HUB_OFFLINE=1 / TRANSFORMERS_OFFLINE=1 / CUDA_VISIBLE_DEVICES=
# / DHT_INITIAL_PEERS=..., exactly like petals-chat.service -- to activate them.
# ===========================================================================
import asyncio
from traceback import format_exc

# Gated model load, mirroring app.py's `models = utils.load_models()` at import.
if os.getenv("POLLEN_LOAD_MODEL") == "1":
    from pollen.infrastructure.model_loader import load_models
    from pollen.core import config as _gen_config

    log.info("POLLEN_LOAD_MODEL=1 -> loading Petals model client (takes ~seconds)...")
    app.state.models = load_models()
    app.state.gen_config = _gen_config
    log.info("model client ready: keys=%s", list(app.state.models.keys()))
else:
    app.state.models = None
    app.state.gen_config = None
    log.info("POLLEN_LOAD_MODEL not set -> model-free mode (v1/v2 routes dormant)")


def _merge_request_args(query, form, body):
    """Mirror Flask get_typed_arg precedence: request.values (query+form) win
    over the JSON body. Returns a plain dict for ChatService's *_arg helpers."""
    merged = {}
    if isinstance(body, dict):
        merged.update(body)
    if form:
        merged.update(form)
    if query:
        merged.update(query)
    return merged


@app.websocket("/api/v2/generate")
async def ws_api_generate(websocket: WebSocket):
    """Async port of pollen.features.chat.websocket_api.ws_api_generate.

    Reproduces the same open/receive/session/ChatService/frame flow, driving the
    blocking ChatService.generate_stream through a threadpool bridge (validated in
    scratch_ws_bridge_test.py) so the event loop is never blocked. Frames are sent
    with json.dumps(frame) -- byte-identical to Flask's ws.send(json.dumps(frame)).
    """
    # Same imports Flask's websocket_api uses: config module (for STEP_TIMEOUT and
    # to pass to ChatService, exactly as Flask does), ChatService, MissingBlocksError.
    from pollen.core import config
    from pollen.features.chat.chat_service import ChatService
    from petals.client.routing.sequence_manager import MissingBlocksError

    await websocket.accept()
    models = app.state.models
    try:
        # Flask: json.loads(ws.receive(timeout=config.STEP_TIMEOUT))
        request = json.loads(await asyncio.wait_for(websocket.receive_text(), config.STEP_TIMEOUT))
        assert request["type"] == "open_inference_session"
        model_name = request["model"]
        max_length = request["max_length"]

        # Dormant-safe guard (not in Flask; required so the model-free process can
        # host this route inertly). Never fires once POLLEN_LOAD_MODEL=1 loads models.
        if models is None:
            await websocket.send_text(json.dumps(
                {"ok": False, "error": "model not loaded (POLLEN_LOAD_MODEL not set on this process)"}))
            await websocket.close()
            return

        model, tokenizer, backend_config = models[model_name]

        # Same-origin / license check, mirroring Flask exactly:
        #   if not backend_config.public_api and
        #      http_request.origin != f"{http_request.scheme}://{http_request.host}": raise ValueError
        # Starlette's ws scheme is "ws"/"wss"; map to Flask's http/https.
        if not backend_config.public_api:
            scheme = "https" if websocket.url.scheme == "wss" else "http"
            host = websocket.headers.get("host")
            if websocket.headers.get("origin") != f"{scheme}://{host}":
                raise ValueError(f"We do not provide public API for {model_name} due to license restrictions")

        loop = asyncio.get_running_loop()
        with model.inference_session(max_length=max_length) as session:
            await websocket.send_text(json.dumps({"ok": True}))
            service = ChatService(model, tokenizer, backend_config, config)

            while True:  # multi-turn: one iteration per client "generate" message
                # Flask: json.loads(ws.receive(timeout=config.STEP_TIMEOUT))
                request = json.loads(await asyncio.wait_for(websocket.receive_text(), config.STEP_TIMEOUT))
                assert request["type"] == "generate"

                # Threadpool bridge (validated in scratch_ws_bridge_test.py): run the
                # blocking generator in a worker thread; stream each frame as produced.
                queue: asyncio.Queue = asyncio.Queue()
                sentinel = object()
                gen_error = {"hit": False}

                def produce(req):
                    try:
                        for frame in service.generate_stream(req, session):
                            loop.call_soon_threadsafe(queue.put_nowait, frame)
                    except MissingBlocksError:
                        gen_error["hit"] = True
                        loop.call_soon_threadsafe(queue.put_nowait, {
                            "ok": False, "code": "cluster_unavailable", "traceback": format_exc()})
                    except Exception:
                        gen_error["hit"] = True
                        loop.call_soon_threadsafe(queue.put_nowait, {"ok": False, "traceback": format_exc()})
                    finally:
                        loop.call_soon_threadsafe(queue.put_nowait, sentinel)

                producer = loop.run_in_executor(None, produce, request)
                try:
                    while True:
                        frame = await queue.get()
                        if frame is sentinel:
                            break
                        await websocket.send_text(json.dumps(frame))
                finally:
                    await producer

                # Flask lets a generation error (MissingBlocksError / Exception)
                # propagate out of the multi-turn loop and terminate the connection.
                # The error frame was already sent above; now end the session to match.
                if gen_error["hit"]:
                    break

    except WebSocketDisconnect:
        pass
    except Exception:
        log.warning("ws.generate failed", exc_info=True)
        try:
            await websocket.send_text(json.dumps({"ok": False, "traceback": format_exc()}))
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.post("/api/v1/generate")
async def api_v1_generate(request: Request):
    """Async port of pollen.features.chat.http_api.http_api_generate, via
    ChatService.generate_once. Gated: 503 if no model loaded on this process."""
    from pollen.features.chat.chat_service import ChatService

    models = app.state.models
    config = app.state.gen_config
    if models is None or config is None:
        return FlaskJSONResponse(
            {"ok": False, "error": "model not loaded (POLLEN_LOAD_MODEL not set on this process)"},
            status_code=503)

    # Merge args the way Flask's get_typed_arg reads them (values over json body).
    query = dict(request.query_params)
    body = None
    form = {}
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype == "application/json" or ctype.endswith("+json"):
        try:
            body = await request.json()
        except Exception:
            body = None
    else:
        try:
            form = dict(await request.form())
        except Exception:
            form = {}
    req = _merge_request_args(query, form, body)

    model_name = req.get("model") or config.MODEL_REPO
    model, tokenizer, backend_config = models[model_name]
    service = ChatService(model, tokenizer, backend_config, config)
    # generate_once is blocking (model.generate); keep the event loop free.
    result = await run_in_threadpool(service.generate_once, req)
    return FlaskJSONResponse(result)


# ---------------------------------------------------------------------------
# General static assets (chat.js, style.css, marked.min.js, logo.svg, ...).
# Mounted LAST, on purpose: Starlette matches routes in registration order, so
# the explicit /static/generated/{filename} route defined above is matched
# FIRST and is not shadowed by this catch-all mount. Normal /static/* paths
# (which are not /static/generated/<single-segment>) fall through to here and
# are served from the same static/ directory Flask uses.
# ---------------------------------------------------------------------------
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
