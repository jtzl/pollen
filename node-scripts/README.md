# Node scripts (canonical copies)

These run ON the GPU nodes, not on the hub. The copies here are canonical so
they get updated with the codebase (the 2026 lesson: the on-node copy of
dht-selfcheck.py still watched the old Mixtral DHT prefix months after the
model migration, so it never worked).

## dht-selfcheck.py
Runs every 5 min from a user systemd timer on each node; if the node's own
peer id is absent from the swarm for 3 consecutive checks, it restarts the
node's petals-server (20-min cooldown, 10-min settle guard after restarts).
Per-node identity path comes from the PETALS_IDENTITY environment variable
set in the service unit.

Deploy after changing: copy to /home/irfan/dht-selfcheck.py on each node
(via the hub: ssh -p 31333 (3090) / -p 31332 (4090) irfan@localhost).
When the MODEL changes, update DHT_PREFIX here and redeploy to BOTH nodes.

## ncm_server.py (EXPERIMENTAL, 4090 only)
NCM summarizer service (Stanford HazyResearch/ncm xsum model, step 2 of the
NCM experiment, 2026-11-16). Lives at /home/irfan/ncm/ncm_server.py on the
4090, run by the ncm-server.service user unit; ncm-tunnel.service exposes it
to the hub as EC2:31341 (same pattern as the image server). Lazy-loads the
model in fp16 on first request (zero VRAM idle, ~5s first load, ~0.25s per
summary after). From the hub: POST http://127.0.0.1:31341/summarize with
{"text": "<long news-style article>"}. NOT wired into user-facing answers:
released checkpoint quality is unreliable (about a third of summaries
garbled). Needs /home/irfan/ncm (repo + venv + artifacts/xsum checkpoints)
and the fp16 patches described in CLAUDE.md's NCM item.
