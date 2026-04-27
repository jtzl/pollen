import time
import hivemind
from flask import jsonify

from app import app, models
import speed_tracker

logger = hivemind.get_logger(__file__)

_cache = {"data": None, "time": 0}
CACHE_TTL = 10


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
        for peer_id, span in spans.items():
            peers.append({
                "peer_id": str(peer_id)[-12:],
                "start": span.start,
                "end": span.end,
                "length": span.length,
                "throughput": round(span.server_info.throughput, 1) if span.server_info else 0,
            })

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

        result = {
            "ok": True,
            "model_name": model_name,
            "num_peers": len(spans),
            "peers": peers,
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
