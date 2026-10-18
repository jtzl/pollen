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
