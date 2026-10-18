#!/bin/bash
# Pollen Health Watchdog
# Runs every 5 minutes via cron to monitor and restart services as needed.

LOG="/var/log/pollen-watchdog.log"
ENV_FILE="/data/chat-ui/.env"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
FAILURES=""

log() {
    echo "[$TIMESTAMP] $1" >> "$LOG"
}

# Load alert settings from .env
if [ -f "$ENV_FILE" ]; then
    ALERT_EMAIL_ENABLED=$(grep '^ALERT_EMAIL_ENABLED=' "$ENV_FILE" | cut -d= -f2)
    ALERT_EMAIL_TO=$(grep '^ALERT_EMAIL_TO=' "$ENV_FILE" | cut -d= -f2)
    ALERT_EMAIL_FROM=$(grep '^ALERT_EMAIL_FROM=' "$ENV_FILE" | cut -d= -f2)
fi

record_failure() {
    local service="$1"
    local detail="$2"
    local action="$3"
    FAILURES="${FAILURES}Service: ${service}\nDetail: ${detail}\nAction: ${action}\n\n"
}

# --- 1) Check Chat API ---
# Post-cutover: the model-holder is the FastAPI process (pollen-fastapi) on 5001;
# Flask/petals-chat is retired. Check FastAPI's /api/status and restart it if down.
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://localhost:5001/api/status 2>/dev/null)

if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 400 ] 2>/dev/null; then
    log "OK: Chat API responded with HTTP $HTTP_CODE"
else
    log "FAIL: Chat API unresponsive (HTTP $HTTP_CODE). Restarting ONLY pollen-fastapi (petals-server is checked separately)..."
    sudo systemctl restart pollen-fastapi
    FASTAPI_EXIT=$?
    if [ $FASTAPI_EXIT -eq 0 ]; then
        log "RESTART: pollen-fastapi restarted successfully"
        record_failure "pollen-fastapi" "Chat API returned HTTP $HTTP_CODE" "Restarted pollen-fastapi (success)"
    else
        log "ERROR: Restart failed (pollen-fastapi=$FASTAPI_EXIT)"
        record_failure "pollen-fastapi" "Chat API returned HTTP $HTTP_CODE" "Restart attempted but failed (pollen-fastapi=$FASTAPI_EXIT)"
    fi
fi

# --- 2) Check IRC Bot ---
if systemctl is-active --quiet pollen-irc-bot; then
    IRC_PID=$(pgrep -f "irc_bot\.py" 2>/dev/null)
    log "OK: IRC bot is running (PID ${IRC_PID:-unknown})"
else
    log "FAIL: IRC bot is not running. Restarting..."
    sudo systemctl restart pollen-irc-bot
    if [ $? -eq 0 ]; then
        log "RESTART: pollen-irc-bot restarted successfully"
        record_failure "pollen-irc-bot" "Service was not active" "Restarted pollen-irc-bot (success)"
    else
        log "ERROR: Failed to restart pollen-irc-bot"
        record_failure "pollen-irc-bot" "Service was not active" "Restart attempted but failed"
    fi
fi

# --- 3) Check Petals Server ---
# Two-stage check:
#   (a) systemd is-active — restart immediately if the unit itself is down
#   (b) block-coverage — after a 12-minute startup grace window, ensure the
#       EC2 (A10G) peer is actually serving its assigned blocks (expected 28).
#       Without this check the watchdog considered "loading block 3/28" healthy,
#       leaving blocks 0-2 unserved for 10+ minutes.
SERVER_MIN_BLOCKS=20        # restart if fewer than this many blocks being served
SERVER_STARTUP_GRACE=720    # seconds to wait after service start before coverage check
SERVER_LOW_STREAK=2         # require this many consecutive low readings before restarting
LOWCOUNT_FILE="/tmp/pollen_watchdog_lowcount"   # persists the consecutive-low-reading counter
SERVER_STATE=$(systemctl is-active petals-server 2>/dev/null)

if [ "$SERVER_STATE" != "active" ]; then
    log "FAIL: petals-server is $SERVER_STATE. Restarting..."
    sudo systemctl restart petals-server
    if [ $? -eq 0 ]; then
        log "RESTART: petals-server restarted successfully"
        record_failure "petals-server" "Service state was $SERVER_STATE" "Restarted petals-server (success)"
    else
        log "ERROR: Failed to restart petals-server"
        record_failure "petals-server" "Service state was $SERVER_STATE" "Restart attempted but failed"
    fi
else
    # Compute how long the unit has been active
    SERVER_START_TS=$(systemctl show -p ActiveEnterTimestamp --value petals-server 2>/dev/null)
    SERVER_START_EPOCH=$(date -d "$SERVER_START_TS" +%s 2>/dev/null || echo 0)
    NOW_EPOCH=$(date +%s)
    SERVER_UPTIME=$(( NOW_EPOCH - SERVER_START_EPOCH ))

    if [ "$SERVER_UPTIME" -lt "$SERVER_STARTUP_GRACE" ]; then
        log "OK: petals-server active, within startup grace (uptime=${SERVER_UPTIME}s < ${SERVER_STARTUP_GRACE}s)"
    else
        # Read the EC2 (A10G) peer's current block count from the chat API
        EC2_BLOCKS=$(curl -s --max-time 6 http://localhost:5001/api/status 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for n in d.get('nodes', []):
        if n.get('name') == 'EC2 (A10G)':
            nb = n.get('num_blocks')
            # 'absent' = status answered but the hub announces no blocks: the
            # loaded-but-silent wedge (seen 2026-10-11..18, a week undetected).
            print(int(nb) if nb else 'absent')
            break
    else:
        print('absent')
except Exception:
    print('noapi')
" 2>/dev/null)
        if [ "$EC2_BLOCKS" = "noapi" ] || [ -z "$EC2_BLOCKS" ]; then
            log "WARN: /api/status unreadable (skipping coverage check; stage 1 owns the API)"
            rm -f "$LOWCOUNT_FILE"   # transient reading -> reset consecutive-low streak
        elif [ "$EC2_BLOCKS" = "absent" ]; then
            # The hub server is up but SILENT on the DHT: a restart recovers it.
            # Same consecutive-reading rule and the same driver guard as the
            # low-coverage branch below.
            LOW_STREAK=$(( $(cat "$LOWCOUNT_FILE" 2>/dev/null || echo 0) + 1 ))
            echo "$LOW_STREAK" > "$LOWCOUNT_FILE"
            if [ "$LOW_STREAK" -lt "$SERVER_LOW_STREAK" ]; then
                log "WARN: petals-server active but announcing NO blocks (reading $LOW_STREAK/$SERVER_LOW_STREAK) - deferring restart"
            elif ! nvidia-smi -L >/dev/null 2>&1; then
                log "CRITICAL: petals-server silent on the DHT AND nvidia-smi fails (driver mismatch). Restart SUPPRESSED: a fresh process would be CPU-only and crash-loop. Needs planned driver maintenance."
                record_failure "petals-server" "Silent on DHT with NVIDIA driver/library mismatch" "Auto-restart suppressed; manual maintenance required"
                rm -f "$LOWCOUNT_FILE"
            else
                log "FAIL: petals-server active but silent on the DHT for $LOW_STREAK consecutive checks (loaded-but-not-announcing wedge). Restarting..."
                sudo systemctl restart petals-server
                if [ $? -eq 0 ]; then
                    log "RESTART: petals-server restarted to recover DHT announcements"
                    record_failure "petals-server" "Active but silent on the DHT for $LOW_STREAK consecutive checks" "Restarted petals-server (success)"
                else
                    log "ERROR: Failed to restart petals-server for silent-wedge recovery"
                    record_failure "petals-server" "Active but silent on the DHT" "Restart attempted but failed"
                fi
                rm -f "$LOWCOUNT_FILE"
            fi
        elif [ "$EC2_BLOCKS" -lt "$SERVER_MIN_BLOCKS" ] 2>/dev/null; then
            # Require SERVER_LOW_STREAK consecutive low readings (~5 min apart) before
            # restarting, so a single bad /api/status snapshot can't bounce the hub.
            LOW_STREAK=$(( $(cat "$LOWCOUNT_FILE" 2>/dev/null || echo 0) + 1 ))
            echo "$LOW_STREAK" > "$LOWCOUNT_FILE"
            if [ "$LOW_STREAK" -ge "$SERVER_LOW_STREAK" ]; then
                # DRIVER-MISMATCH GUARD: when the NVIDIA userspace has been
                # updated under a running process (nvidia-smi fails with
                # "Driver/library version mismatch"), the CURRENT process keeps
                # its GPU, but a RESTARTED one comes up CPU-only and crash-loops
                # on "float16 is not supported on CPU" -- killing all coverage
                # instead of recovering it. Never auto-restart in that state.
                if ! nvidia-smi -L >/dev/null 2>&1; then
                    log "CRITICAL: petals-server serving only $EC2_BLOCKS blocks AND nvidia-smi fails (driver/library mismatch). Restart SUPPRESSED: a fresh process would be CPU-only and crash-loop. Needs planned driver maintenance."
                    record_failure "petals-server" "Low coverage ($EC2_BLOCKS blocks) with NVIDIA driver/library mismatch" "Auto-restart suppressed (would crash-loop CPU-only); manual maintenance required"
                    rm -f "$LOWCOUNT_FILE"
                    SKIP_PETALS_RESTART=1
                fi
            fi
            if [ "$LOW_STREAK" -ge "$SERVER_LOW_STREAK" ] && [ "${SKIP_PETALS_RESTART:-0}" != "1" ]; then
                log "FAIL: petals-server active but serving only $EC2_BLOCKS blocks (expected >= $SERVER_MIN_BLOCKS) for $LOW_STREAK consecutive checks. Restarting..."
                sudo systemctl restart petals-server
                if [ $? -eq 0 ]; then
                    log "RESTART: petals-server restarted to recover block coverage"
                    record_failure "petals-server" "Active but serving only $EC2_BLOCKS blocks for $LOW_STREAK consecutive checks (expected >= $SERVER_MIN_BLOCKS)" "Restarted petals-server (success)"
                else
                    log "ERROR: Failed to restart petals-server for coverage"
                    record_failure "petals-server" "Active but serving only $EC2_BLOCKS blocks" "Restart attempted but failed"
                fi
                rm -f "$LOWCOUNT_FILE"   # reset streak after acting
            else
                log "WARN: petals-server serving only $EC2_BLOCKS blocks (low reading $LOW_STREAK/$SERVER_LOW_STREAK) - deferring restart until confirmed"
            fi
        else
            log "OK: petals-server serving $EC2_BLOCKS blocks (uptime=${SERVER_UPTIME}s)"
            rm -f "$LOWCOUNT_FILE"   # healthy reading -> reset consecutive-low streak
        fi
    fi
fi

# --- 3.5) Cluster Coverage / Stall / Peer-Drop Detection ---
# DETECT + LOG ONLY, and fully TOPOLOGY-INDEPENDENT: no hardcoded node count and
# no fixed block-range -> node mapping (block ranges get repinned and nodes are
# added as the swarm grows). Two topology-agnostic signals, both read fresh from
# the SAME /api/status each run:
#   (a) COVERAGE: are ALL blocks served? (block_coverage < total_blocks)
#   (b) PEER DROP: did the announcing-peer count fall from its recent max?
#       (catches a redundant node stalling WITHOUT dropping coverage)
# We never auto-recover: an in-place petals restart can crash-loop on a driver
# mismatch, and nodes without sudo cannot be rebooted remotely. Loud log line only
# (+ email if it were enabled). Keyed on aggregate signals, so the hub's cosmetic
# None-None (NVML quirk) can never trip either check.
COVERAGE_LOW_STREAK=3                                       # consecutive low-coverage readings (~15 min) before alerting
PEER_DROP_STREAK=3                                          # consecutive peer-drop readings (~15 min) before alerting
COVERAGE_LOWSTREAK_FILE="/tmp/pollen_coverage_lowstreak"    # dedicated; never shared with stage 3
PEERS_RECENTMAX_FILE="/tmp/pollen_peers_recentmax"          # self-adjusting baseline peer count (no hardcoded expected count)
PEERS_DROPSTREAK_FILE="/tmp/pollen_peers_dropstreak"        # consecutive peer-drop counter

COV_PARSE=$(curl -s --max-time 6 http://localhost:5001/api/status 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    cov = d.get('block_coverage'); tot = d.get('total_blocks'); npeers = d.get('num_peers')
    bs = d.get('block_status') or []
    nodes = d.get('nodes') or []
    if cov is None or tot is None or npeers is None:
        print('SKIP'); sys.exit()
    missing = [i for i, v in enumerate(bs) if not v]
    if not missing:
        rng = ''; owners = ''
    else:
        lo = min(missing); hi = max(missing)
        rng = ('%d-%d' % (lo, hi)) if lo != hi else ('%d' % lo)
        mset = set(missing)
        names = []
        for n in nodes:
            s = n.get('blocks_start'); e = n.get('blocks_end')
            if s is None or e is None:
                continue
            if any(s <= i < e for i in mset):
                names.append(n.get('name') or n.get('peer_id') or 'unnamed')
        owners = ','.join(names) if names else 'none'
    print('%s|%s|%s|%s|%s' % (cov, tot, rng, owners, npeers))
except Exception:
    print('SKIP')
" 2>/dev/null)

if [ -z "$COV_PARSE" ] || [ "$COV_PARSE" = "SKIP" ]; then
    log "WARN: coverage/peer check skipped (status unreadable)"
    rm -f "$COVERAGE_LOWSTREAK_FILE" "$PEERS_DROPSTREAK_FILE"
else
    COV_NOW=$(echo "$COV_PARSE" | cut -d'|' -f1)
    COV_TOT=$(echo "$COV_PARSE" | cut -d'|' -f2)
    COV_RANGE=$(echo "$COV_PARSE" | cut -d'|' -f3)
    COV_OWNERS=$(echo "$COV_PARSE" | cut -d'|' -f4)
    PEERS_NOW=$(echo "$COV_PARSE" | cut -d'|' -f5)

    # ---- Signal (a): coverage (are all blocks served? works at any node count/layout) ----
    if [ "$COV_NOW" -ge "$COV_TOT" ] 2>/dev/null; then
        log "OK: cluster coverage $COV_NOW/$COV_TOT (full)"
        rm -f "$COVERAGE_LOWSTREAK_FILE"
    else
        COV_STREAK=$(( $(cat "$COVERAGE_LOWSTREAK_FILE" 2>/dev/null || echo 0) + 1 ))
        echo "$COV_STREAK" > "$COVERAGE_LOWSTREAK_FILE"
        # Identify the owner(s) DYNAMICALLY from the live nodes[] ranges (never hardcoded).
        if [ -n "$COV_OWNERS" ] && [ "$COV_OWNERS" != "none" ]; then
            COV_WHO="node(s) advertising that range: $COV_OWNERS (likely stalled)"
        else
            COV_WHO="no node currently serving blocks $COV_RANGE"
        fi
        if [ "$COV_STREAK" -lt "$COVERAGE_LOW_STREAK" ]; then
            log "WARN: cluster coverage $COV_NOW/$COV_TOT (low reading $COV_STREAK/$COVERAGE_LOW_STREAK) - deferring alert (may be a node mid-reload)"
        elif [ "$COV_STREAK" -eq "$COVERAGE_LOW_STREAK" ]; then
            log "STALL DETECTED: coverage $COV_NOW/$COV_TOT sustained ~15min - blocks $COV_RANGE unserved; $COV_WHO. Manual recovery needed (reboot the stalled node; an in-place petals restart can crash-loop on driver mismatch; nodes without sudo need their owner)."
            record_failure "cluster-coverage" "Coverage $COV_NOW/$COV_TOT sustained ~15min; blocks $COV_RANGE unserved; $COV_WHO" "DETECT+LOG only (auto-recovery unsafe); manual recovery required"
        elif [ $(( (COV_STREAK - COVERAGE_LOW_STREAK) % 6 )) -eq 0 ]; then
            log "STALL ONGOING: coverage still $COV_NOW/$COV_TOT - blocks $COV_RANGE unserved; $COV_WHO. Still needs manual recovery."
            record_failure "cluster-coverage" "Coverage still $COV_NOW/$COV_TOT; blocks $COV_RANGE unserved; $COV_WHO" "DETECT+LOG only; manual recovery still required"
        fi
    fi

    # ---- Signal (b): peer drop (self-adjusting baseline; works whether 2 or 20 peers) ----
    if [ -n "$PEERS_NOW" ]; then
        PEERS_MAX=$(cat "$PEERS_RECENTMAX_FILE" 2>/dev/null || echo "$PEERS_NOW")
        if [ "$PEERS_NOW" -ge "$PEERS_MAX" ] 2>/dev/null; then
            echo "$PEERS_NOW" > "$PEERS_RECENTMAX_FILE"   # baseline ratchets UP as nodes join
            rm -f "$PEERS_DROPSTREAK_FILE"                # recovered -> reset drop streak
            log "OK: announcing peers $PEERS_NOW (baseline $PEERS_NOW)"
        else
            PEER_STREAK=$(( $(cat "$PEERS_DROPSTREAK_FILE" 2>/dev/null || echo 0) + 1 ))
            echo "$PEER_STREAK" > "$PEERS_DROPSTREAK_FILE"
            if [ "$PEER_STREAK" -lt "$PEER_DROP_STREAK" ]; then
                log "WARN: announcing peers $PEERS_NOW < recent max $PEERS_MAX (low reading $PEER_STREAK/$PEER_DROP_STREAK) - deferring alert"
            elif [ "$PEER_STREAK" -eq "$PEER_DROP_STREAK" ]; then
                log "PEER DROP: num_peers fell from $PEERS_MAX to $PEERS_NOW (sustained ~15min) - a node stopped announcing. Manual check needed (coverage may still be full if the node was redundant)."
                record_failure "cluster-peers" "num_peers fell from $PEERS_MAX to $PEERS_NOW (sustained ~15min) - a node stopped announcing" "DETECT+LOG only; manual check required"
            elif [ $(( (PEER_STREAK - PEER_DROP_STREAK) % 6 )) -eq 0 ]; then
                log "PEER DROP ONGOING: num_peers still $PEERS_NOW (recent max $PEERS_MAX) - node still not announcing."
                record_failure "cluster-peers" "num_peers still $PEERS_NOW (recent max $PEERS_MAX)" "DETECT+LOG only; manual check still required"
            fi
        fi
    fi
fi

# --- 4) Check DHT ---
if ! systemctl is-active --quiet petals-dht; then
    log "FAIL: petals-dht is not running. Restarting..."
    sudo systemctl restart petals-dht
    if [ $? -eq 0 ]; then
        log "RESTART: petals-dht restarted successfully"
        record_failure "petals-dht" "Service was not active" "Restarted petals-dht (success)"
    else
        log "ERROR: Failed to restart petals-dht"
        record_failure "petals-dht" "Service was not active" "Restart attempted but failed"
    fi
fi

# --- 5) Check all four reverse-tunnel ports ---
# Each node dials in with two tunnels: one for its Petals traffic, one for
# SSH access from the hub. A missing port means that node's machine is
# offline or its tunnel service died; detect-only, the node owns the dial.
check_tunnel() { # port, label
    if ! ss -tln | grep -q ":$1 "; then
        log "WARN: Tunnel port $1 not listening ($2 - node offline or its tunnel service died)"
        record_failure "tunnel-$1" "Port $1 ($2) not listening on EC2" "No local action taken (tunnels are dialed by the node)"
    fi
}
check_tunnel 31331 "3090 petals"
check_tunnel 31333 "3090 ssh"
check_tunnel 31339 "4090 petals"
check_tunnel 31332 "4090 ssh" 

# --- Send email alert if there were failures ---
if [ -n "$FAILURES" ] && [ "$ALERT_EMAIL_ENABLED" = "true" ]; then
    SUBJECT="[Pollen Watchdog] Service failure detected at $TIMESTAMP"
    BODY="Pollen Watchdog detected the following issue(s) at $TIMESTAMP on $(hostname):\n\n${FAILURES}---\nLog: $LOG\nThis is an automated alert from the Pollen health watchdog."
    echo -e "$BODY" | mail -s "$SUBJECT" -r "$ALERT_EMAIL_FROM" "$ALERT_EMAIL_TO"
    if [ $? -eq 0 ]; then
        log "ALERT: Email sent to $ALERT_EMAIL_TO"
    else
        log "ERROR: Failed to send alert email to $ALERT_EMAIL_TO"
    fi
fi

# One-line health verdict for humans: `cat /var/log/pollen-health.status`.
# There is deliberately NO alert delivery (operator decision 2026-10-18):
# the state is checked by looking, at the start of every work session, so
# this file makes the look instant - last verdict, coverage, peer count,
# and any failures from this run.
HEALTH_FILE="/var/log/pollen-health.status"
if [ -n "${COV_NOW:-}" ] && [ -n "${COV_TOT:-}" ]; then
    HSUM="coverage=${COV_NOW}/${COV_TOT} peers=${PEERS_NOW:-?}"
else
    HSUM="coverage=unknown"
fi
if [ -n "$FAILURES" ]; then
    printf '%s
' "[$TIMESTAMP] PROBLEM $HSUM :: $(echo -e "$FAILURES" | tr '
' ' ' | cut -c1-400)" | sudo tee "$HEALTH_FILE" >/dev/null
else
    printf '%s
' "[$TIMESTAMP] OK $HSUM" | sudo tee "$HEALTH_FILE" >/dev/null
fi

log "--- Health check complete ---"
