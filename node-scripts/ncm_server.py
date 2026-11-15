"""NCM summarizer service for Pollen (runs on the 4090, EXPERIMENTAL).

Serves the Stanford NCM xsum model over local HTTP. The hub reaches it through
the ncm-tunnel reverse SSH tunnel (EC2:31341 -> localhost:5002), same pattern
as the image server. NOT wired into user-facing Pollen answers: quality of the
released research checkpoints is not reliable enough yet (2026-11-16).

Endpoints:
    GET  /health            -> {"ok": true, "loaded": bool}
    POST /summarize         <- {"text": "<long news-style article>"}
                            -> {"ok": true, "summary": str, "gen_seconds": float}

The model is lazy-loaded on the first request (zero VRAM while idle); loading
takes a few seconds once. Models run in fp16 because the 4090's VRAM is almost
fully booked by the Petals blocks and the image server.
"""
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NCM_ROOT = os.environ.get("NCM_ROOT", "/home/irfan/ncm")
PORT = int(os.environ.get("NCM_PORT", "5002"))
MAX_TEXT_CHARS = int(os.environ.get("NCM_MAX_TEXT_CHARS", "40000"))

sys.path.insert(0, NCM_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ncm_server")

_lock = threading.Lock()
_m = {}


def _load():
    """Load tokenizer, generator and generation settings once (fp16)."""
    import torch
    from omegaconf import OmegaConf
    from models.next_concept import NextConceptTransformer
    from tokenization.vqvae_tokenizer import VQVAETokenizer
    from transformers import AutoTokenizer

    t0 = time.time()
    cfg = OmegaConf.load(os.path.join(NCM_ROOT, "config/eval/xsum.yaml"))
    ck = os.path.join(NCM_ROOT, "artifacts/xsum")
    cfg.tokenizer.vqvae.checkpoint_path = os.path.join(ck, cfg.checkpoints.tokenizer)

    tk = VQVAETokenizer.from_config(cfg.tokenizer)
    tk.vqvae = tk.vqvae.half().cuda().eval()
    net = NextConceptTransformer(vqvae_local=tk.vqvae, config=cfg.model).half().cuda()
    sd = torch.load(os.path.join(ck, cfg.checkpoints.generator), map_location="cpu", weights_only=False)
    net.load_state_dict(sd["model"], strict=True)
    net.eval()

    G = cfg.generation
    allow = None
    spec = str(G.frequency_allowance) if G.get("frequency_allowance") else ""
    if spec and ":" in spec:
        path, ref = spec.split(":", 1)
        obj = torch.load(os.path.join(ck, path), map_location="cuda", weights_only=False)
        for k in ref.split("."):
            obj = obj[k]
        allow = obj.double()

    adj_raw = str(G.get("adjacency_penalty", "") or "")
    _m.update(
        torch=torch,
        tk=tk,
        net=net,
        allow=allow,
        src=AutoTokenizer.from_pretrained(str(cfg.tokenizer.pre_tokenizer.model_id)),
        P=int(cfg.model.prefix_len),
        eos=tk.pre_tokenizer.eos_token_id,
        pad=tk.pre_tokenizer.pad_token_id,
        temp=float(G.get("temperature", 1.0)),
        top_k=int(G.get("top_k", 1)),
        top_p=float(G.get("top_p", 0.0)),
        cfg_scale=float(G.get("cfg_scale", 0.0)),
        freq=float(G.get("frequency_penalty", 0.0)),
        mu=float(G.get("frequency_deficit", 0.0)),
        adj=[float(v) for v in adj_raw.split(",")] if adj_raw else None,
    )
    log.info("model loaded in %.1fs", time.time() - t0)


def _pick_tokens(logits):
    """Greedy decode with frequency/adjacency penalties (ported from eval_shard.py)."""
    torch = _m["torch"]
    freq, mu, ADJ, ALLOW = _m["freq"], _m["mu"], _m["adj"], _m["allow"]
    if freq <= 0 and not ADJ:
        return logits.argmax(-1)
    B, T, V = logits.shape
    lg = logits.double()
    allow = (ALLOW[:V] * T)[None, :] if ALLOW is not None else None
    al = ADJ[0] if ADJ else 0.0
    aw = int(ADJ[1]) if ADJ and len(ADJ) > 1 else 1
    ad = ADJ[2] if ADJ and len(ADJ) > 2 else 1.0
    cnt = torch.zeros(B, V, dtype=torch.float64, device=lg.device)
    out = torch.empty(B, T, dtype=torch.long, device=lg.device)
    hist = []
    for t in range(T):
        step = lg[:, t, :].clone()
        if freq > 0:
            excess = (cnt - allow).clamp_min(0.0) if allow is not None else cnt
            step -= freq * excess
            if mu > 0 and allow is not None:
                step += mu * ((T - t) / T) * (allow - cnt).clamp_min(0.0)
        for j, h in enumerate(reversed(hist[-aw:])):
            step.scatter_add_(1, h[:, None], torch.full((B, 1), -al * ad ** j, dtype=torch.float64, device=lg.device))
        p = step.argmax(-1)
        out[:, t] = p
        hist.append(p)
        cnt.scatter_add_(1, p[:, None], torch.ones(B, 1, dtype=torch.float64, device=lg.device))
    return out


def summarize(text):
    torch = _m["torch"]
    P, eos, pad = _m["P"], _m["eos"], _m["pad"]
    ids = _m["src"](text)["input_ids"]
    prefix = (ids[:P] + [pad] * P)[:P]
    x = torch.tensor([prefix], device="cuda")
    t0 = time.time()
    with torch.no_grad(), torch.autocast("cuda", dtype=torch.float16):
        g = _m["net"].generate(None, x, temperature=_m["temp"], top_k=_m["top_k"],
                               top_p=_m["top_p"], cfg_scale=_m["cfg_scale"], cfg_null_eos=eos)
        _, zl = _m["tk"].vqvae.vq.idx_to_zhats(g, return_zlevels=True)
        out_ids = _pick_tokens(_m["tk"].vqvae.decoder(zl[-1])).tolist()[0]
    elapsed = time.time() - t0
    if eos in out_ids:
        out_ids = out_ids[:out_ids.index(eos)]
    summary = _m["tk"].pre_tokenizer.decode(
        [t for t in out_ids if t not in (eos, pad)], skip_special_tokens=True)
    return summary.strip(), elapsed


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "loaded": bool(_m)})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/summarize":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > MAX_TEXT_CHARS * 4:
                self._send(400, {"ok": False, "error": "bad content length"})
                return
            data = json.loads(self.rfile.read(length))
            text = (data.get("text") or "").strip()
            if not text:
                self._send(400, {"ok": False, "error": "empty text"})
                return
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS]
            with _lock:
                if not _m:
                    _load()
                summary, elapsed = summarize(text)
            self._send(200, {"ok": True, "summary": summary, "gen_seconds": round(elapsed, 3)})
        except Exception as e:
            log.exception("summarize failed")
            self._send(500, {"ok": False, "error": str(e)})


if __name__ == "__main__":
    log.info("ncm_server listening on 127.0.0.1:%d (model lazy-loads on first request)", PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
