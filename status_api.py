import json
import subprocess
import time
import urllib.request
import hivemind
from flask import jsonify

from app import app, models
import speed_tracker

logger = hivemind.get_logger(__file__)

_cache = {"data": None, "time": 0}
CACHE_TTL = 10

# Server-side proxy for the WordPress curated-sources API. Lives here so the
# browser never makes a cross-origin request (the WP endpoint doesn't send
# Access-Control-Allow-Origin). Cached for 5 minutes since votes/categories
# change slowly and the modal can be opened repeatedly.
CURATED_SOURCES_URL = "https://makeyouraismarter.com/wp-json/pollen/v1/sources?limit=100"
CURATED_CATEGORIES_URL = "https://makeyouraismarter.com/wp-json/pollen/v1/categories"
CURATED_SOURCES_TTL = 300
_curated_sources_cache = {"data": None, "expires_at": 0.0}


def _split_category(raw):
    """Split a category string into (parent, sub).

    Accepts either a flat slug ("science") or a hierarchical form using one
    of "/", ">" or ":" as the separator ("science/biology", "Science > Biology").
    Returns lowercased, stripped (parent, sub) — sub is "" for flat input.
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

    Sources without a subcategory live directly in the parent\'s `sources`.
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


# Map truncated peer IDs to friendly node info
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
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            parts = result.stdout.strip().split(",")
            if len(parts) == 2:
                return int(parts[0].strip()), int(parts[1].strip())
    except Exception:
        pass
    return None, None


def _get_sequence_manager(model):
    """Find the RemoteSequenceManager from a distributed model."""
    for accessor in [
        lambda m: m.model.layers.sequence_manager,
        lambda m: m.transformer.h.sequence_manager,
    ]:
        try:
            return accessor(model)
        except AttributeError:
            continue
    # Fallback: search all modules
    from petals.client.remote_sequential import RemoteSequential
    for module in model.modules():
        if isinstance(module, RemoteSequential):
            return module.sequence_manager
    return None


@app.route("/api/status")
def api_status():
    now = time.time()
    if _cache["data"] and now - _cache["time"] < CACHE_TTL:
        return jsonify(_cache["data"])

    try:
        model_name = list(models.keys())[0]
        model, tokenizer, backend_config = models[model_name]

        seq_manager = _get_sequence_manager(model)
        if seq_manager is None:
            return jsonify({"ok": False, "error": "Could not find sequence manager"})

        dht = seq_manager.dht
        block_uids = seq_manager.block_uids
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
            "tokens_per_second": speed_tracker.get_avg_speed(),
            "uptime_seconds": speed_tracker.get_uptime(),
        }

        _cache["data"] = result
        _cache["time"] = now
        return jsonify(result)

    except Exception as e:
        logger.warning("Status API error:", exc_info=True)
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/curated-sources")
def api_curated_sources():
    """Server-side proxy for the WordPress curated-sources endpoint.

    Avoids browser CORS issues since the WP API does not set
    Access-Control-Allow-Origin. Response is cached in-process for
    CURATED_SOURCES_TTL seconds.
    """
    now = time.time()
    cached = _curated_sources_cache["data"]
    if cached is not None and now < _curated_sources_cache["expires_at"]:
        return jsonify(cached)

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
            logger.info("Curated categories fetch skipped: %s", cat_err)

        if isinstance(data, dict):
            result = dict(data)
        else:
            result = {"sources": sources, "count": len(sources)}
        result["categories"] = _build_categories(sources)
        if category_meta:
            result["category_meta"] = category_meta

        _curated_sources_cache["data"] = result
        _curated_sources_cache["expires_at"] = now + CURATED_SOURCES_TTL
        return jsonify(result)
    except Exception as e:
        logger.warning("Curated sources proxy fetch failed: %s", e)
        # Serve stale cache if available — better than a hard error in the UI.
        if cached is not None:
            return jsonify(cached)
        return jsonify({"ok": False, "error": str(e), "sources": [], "count": 0, "categories": []}), 502
