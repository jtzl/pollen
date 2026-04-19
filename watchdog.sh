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
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://localhost:5000/api/status 2>/dev/null)

if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 400 ] 2>/dev/null; then
    log "OK: Chat API responded with HTTP $HTTP_CODE"
else
    log "FAIL: Chat API unresponsive (HTTP $HTTP_CODE). Restarting ONLY petals-chat (petals-server is checked separately)..."
    sudo systemctl restart petals-chat
    PETALS_CHAT_EXIT=$?
    if [ $PETALS_CHAT_EXIT -eq 0 ]; then
        log "RESTART: petals-chat restarted successfully"
        record_failure "petals-chat" "Chat API returned HTTP $HTTP_CODE" "Restarted petals-chat (success)"
    else
        log "ERROR: Restart failed (petals-chat=$PETALS_CHAT_EXIT)"
        record_failure "petals-chat" "Chat API returned HTTP $HTTP_CODE" "Restart attempted but failed (petals-chat=$PETALS_CHAT_EXIT)"
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
        EC2_BLOCKS=$(curl -s --max-time 6 http://localhost:5000/api/status 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for n in d.get('nodes', []):
        if n.get('name') == 'EC2 (A10G)':
            print(int(n.get('num_blocks') or 0))
            break
    else:
        print('unknown')
except Exception:
    print('unknown')
" 2>/dev/null)
        if [ "$EC2_BLOCKS" = "unknown" ] || [ -z "$EC2_BLOCKS" ]; then
            log "WARN: could not determine EC2 peer block count from /api/status (skipping coverage check)"
        elif [ "$EC2_BLOCKS" -lt "$SERVER_MIN_BLOCKS" ] 2>/dev/null; then
            log "FAIL: petals-server active but serving only $EC2_BLOCKS blocks (expected >= $SERVER_MIN_BLOCKS). Restarting..."
            sudo systemctl restart petals-server
            if [ $? -eq 0 ]; then
                log "RESTART: petals-server restarted to recover block coverage"
                record_failure "petals-server" "Active but serving only $EC2_BLOCKS blocks (expected >= $SERVER_MIN_BLOCKS)" "Restarted petals-server (success)"
            else
                log "ERROR: Failed to restart petals-server for coverage"
                record_failure "petals-server" "Active but serving only $EC2_BLOCKS blocks" "Restart attempted but failed"
            fi
        else
            log "OK: petals-server serving $EC2_BLOCKS blocks (uptime=${SERVER_UPTIME}s)"
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

# --- 5) Check tunnel port 31331 ---
if ! ss -tln | grep -q ':31331 '; then
    log "WARN: Tunnel port 31331 is not listening (physical node may be disconnected)"
    record_failure "petals-tunnel" "Port 31331 not listening on EC2" "No local action taken (tunnel is managed by physical node)"
fi

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

log "--- Health check complete ---"
