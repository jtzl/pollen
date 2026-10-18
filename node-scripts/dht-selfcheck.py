#!/home/irfan/petals-env/bin/python
"""Self-healing DHT registration check for the RTX 3090 Petals node.

The node's block server can keep running while its p2pd silently loses the
DHT bootstrap path (e.g. after tunnel-keepalive.sh rebuilds the SSH tunnel).
The server then holds blocks in VRAM but is invisible to the swarm.

This check asks the hub's DHT whether THIS node's peer id is present in an
ONLINE state, and restarts petals-server only after repeated, unambiguous
absence.

Safety rules:
  * A DHT query failure is INCONCLUSIVE, never "absent" - it must not restart.
  * Requires ABSENT_THRESHOLD consecutive absent results before restarting.
  * Enforces COOLDOWN_S after any restart before another is allowed.
  * Skips entirely if petals-server started less than MIN_UPTIME_S ago
    (block loading takes ~4 min before the server announces itself).
  * Single-instance via flock, so overlapping timer runs cannot stack.
"""
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------- settings
BOOTSTRAP = "/ip4/127.0.0.1/tcp/31337/p2p/QmUdaa6myUMrforCjAQmtMn1bXE9GJjpvecLfjZXXn7Ddq"
# Per-node override: the only setting that differs between nodes
# (3090: /home/irfan/.petals_identity, 4090: /home/irfan/.petals_identity.key).
IDENTITY_PATH = os.environ.get("PETALS_IDENTITY", "/home/irfan/.petals_identity")
DHT_PREFIX = "NousResearch/Nous-Hermes-2-Mixtral-8x7B-DPO"
NUM_BLOCKS = 32
UNIT = "petals-server.service"

STATE_PATH = "/home/irfan/.dht_selfcheck_state.json"
LOG_PATH = "/home/irfan/dht-selfcheck.log"
LOCK_PATH = "/home/irfan/.dht_selfcheck.lock"

ABSENT_THRESHOLD = 3        # consecutive absent results before restarting
COOLDOWN_S = 20 * 60        # min seconds between restarts
MIN_UPTIME_S = 10 * 60      # skip if the unit started more recently than this
DHT_TIMEOUT_S = 60          # bootstrap timeout for the client DHT


def log(msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"consecutive_absent": 0, "last_restart_ts": 0}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log("WARN could not persist state: %s" % e)


def systemctl(*args):
    return subprocess.run(
        ["systemctl", "--user"] + list(args),
        capture_output=True, text=True, timeout=120,
    )


def service_uptime_s():
    """Seconds since petals-server last entered active state, or None."""
    try:
        res = systemctl("show", UNIT, "-p", "ActiveEnterTimestampMonotonic", "--value")
        enter_us = int((res.stdout or "0").strip())
        if enter_us <= 0:
            return None
        with open("/proc/uptime") as f:
            now_s = float(f.read().split()[0])
        return now_s - (enter_us / 1e6)
    except Exception:
        return None


def own_peer_id():
    from hivemind.p2p.p2p_daemon_bindings.datastructures import PeerID
    with open(IDENTITY_PATH, "rb") as f:
        return str(PeerID.from_identity(f.read()))


def query_dht_online_peers():
    """Return a set of peer-id strings ONLINE in the swarm.

    Raises on any failure - the caller treats that as INCONCLUSIVE.
    """
    from hivemind import DHT
    from petals.data_structures import UID_DELIMITER, ServerState
    from petals.utils.dht import get_remote_module_infos

    uids = tuple("%s%s%d" % (DHT_PREFIX, UID_DELIMITER, i) for i in range(NUM_BLOCKS))
    dht = DHT(
        initial_peers=[BOOTSTRAP],
        client_mode=True,
        num_workers=32,
        startup_timeout=DHT_TIMEOUT_S,
        start=True,
    )
    try:
        infos = get_remote_module_infos(dht, uids, latest=True)
        online = set()
        for info in infos:
            if info and info.servers:
                for pid, server in info.servers.items():
                    if server.state == ServerState.ONLINE:
                        online.add(str(pid))
        return online
    finally:
        # Always tear the client DHT (and its p2pd) down - this runs every
        # 5 minutes and must not leak daemons.
        try:
            dht.shutdown()
        except Exception:
            pass


def main():
    state = load_state()
    now = time.time()

    # ---- guard: recently restarted unit (block load takes ~4 min) ----------
    uptime = service_uptime_s()
    if uptime is None:
        log("SKIP could not determine %s uptime" % UNIT)
        return 0
    if uptime < MIN_UPTIME_S:
        log("SKIP %s up only %.0fs (< %ds), still settling" % (UNIT, uptime, MIN_UPTIME_S))
        return 0

    # ---- query the swarm ---------------------------------------------------
    try:
        me = own_peer_id()
    except Exception as e:
        log("INCONCLUSIVE cannot read identity %s: %s" % (IDENTITY_PATH, e))
        return 0

    try:
        online = query_dht_online_peers()
    except Exception as e:
        # Bootstrap unreachable / DHT error: NOT evidence of being orphaned.
        log("INCONCLUSIVE DHT query failed (%s: %s) - not counting as absent"
            % (type(e).__name__, e))
        return 0

    if not online:
        # No peers at all means we cannot see the swarm, not that we are absent.
        log("INCONCLUSIVE DHT returned zero ONLINE peers - not counting as absent")
        return 0

    short = me[-12:]
    if me in online:
        if state.get("consecutive_absent", 0):
            log("OK registered as ...%s (clearing absent streak of %d)"
                % (short, state["consecutive_absent"]))
        else:
            log("OK registered as ...%s (%d ONLINE peers)" % (short, len(online)))
        state["consecutive_absent"] = 0
        save_state(state)
        return 0

    # ---- absent ------------------------------------------------------------
    streak = int(state.get("consecutive_absent", 0)) + 1
    state["consecutive_absent"] = streak
    peers_short = ",".join(sorted(p[-12:] for p in online))
    log("ABSENT ...%s not in swarm (streak %d/%d; ONLINE peers: %s)"
        % (short, streak, ABSENT_THRESHOLD, peers_short))

    if streak < ABSENT_THRESHOLD:
        save_state(state)
        return 0

    since_restart = now - float(state.get("last_restart_ts", 0) or 0)
    if since_restart < COOLDOWN_S:
        log("COOLDOWN threshold reached but only %.0fs since last restart (< %ds) - not restarting"
            % (since_restart, COOLDOWN_S))
        save_state(state)
        return 0

    log("RESTART threshold %d reached - restarting %s" % (ABSENT_THRESHOLD, UNIT))
    try:
        res = systemctl("restart", UNIT)
        if res.returncode == 0:
            log("RESTART issued successfully")
        else:
            log("RESTART FAILED rc=%d stderr=%s" % (res.returncode, (res.stderr or "").strip()[:200]))
    except Exception as e:
        log("RESTART FAILED %s: %s" % (type(e).__name__, e))

    state["consecutive_absent"] = 0
    state["last_restart_ts"] = now
    save_state(state)
    return 0


if __name__ == "__main__":
    # single instance only
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("SKIP another selfcheck is already running")
        sys.exit(0)
    try:
        sys.exit(main())
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
