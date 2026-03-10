#!/bin/bash
set -euo pipefail

# =============================================================================
# Pollen — One-Click Install Script
# Sets up everything: venv, dependencies, config, systemd services, cron
# =============================================================================

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="/data/petals-env"
ENV_FILE="$REPO_DIR/.env"
ENV_EXAMPLE="$REPO_DIR/.env.example"
WATCHDOG_LOG="/var/log/pollen-watchdog.log"

# -- Colors --
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $1"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $1"; exit 1; }
header(){ echo -e "\n${BOLD}==> $1${NC}"; }

# =============================================================================
# 1) Pre-flight checks
# =============================================================================
header "Pre-flight checks"

if [ "$EUID" -ne 0 ]; then
    if ! sudo -n true 2>/dev/null; then
        fail "This script needs sudo privileges for systemd setup. Re-run with: sudo ./install.sh"
    fi
fi

# Python version check
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "${major:-0}" -ge 3 ] && [ "${minor:-0}" -ge 8 ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    fail "Python 3.8+ is required but not found. Install it with: sudo apt install python3 python3-venv python3-pip"
fi
ok "Python found: $PYTHON ($version)"

# Check for venv support
if ! "$PYTHON" -c "import venv" 2>/dev/null; then
    warn "python3-venv not installed. Installing..."
    sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv
fi

# Check GPU
if command -v nvidia-smi &>/dev/null; then
    GPU_INFO=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo "unknown")
    ok "NVIDIA GPU detected: $GPU_INFO"
else
    warn "No NVIDIA GPU detected. Will run on CPU (slow inference)."
fi

# =============================================================================
# 2) Virtual environment
# =============================================================================
header "Setting up Python virtual environment"

if [ -d "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/activate" ]; then
    ok "Virtual environment already exists at $VENV_DIR"
else
    info "Creating virtual environment at $VENV_DIR..."
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Virtual environment created"
fi

source "$VENV_DIR/bin/activate"
ok "Activated: $(which python3)"

# =============================================================================
# 3) Install dependencies
# =============================================================================
header "Installing Python dependencies"

pip install --upgrade pip setuptools wheel -q
info "Installing from requirements.txt..."
pip install -r "$REPO_DIR/requirements.txt" -q
pip install irc matrix-nio -q
ok "All dependencies installed"

# =============================================================================
# 4) Configuration
# =============================================================================
header "Configuration"

if [ -f "$ENV_FILE" ]; then
    ok ".env already exists — keeping your current configuration"
    SKIP_CONFIG=true
else
    info "Creating .env from .env.example..."
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    SKIP_CONFIG=false
fi

if [ "$SKIP_CONFIG" != "true" ]; then
    echo ""
    echo -e "${BOLD}Let's configure your Pollen instance.${NC}"
    echo -e "Press Enter to accept defaults shown in [brackets].\n"

    read -rp "Model repository [mistralai/Mixtral-8x7B-Instruct-v0.1]: " INPUT_MODEL
    if [ -n "$INPUT_MODEL" ]; then
        sed -i "s|^MODEL_REPO=.*|MODEL_REPO=$INPUT_MODEL|" "$ENV_FILE"
    fi

    echo ""
    echo "DHT peers are required to connect to the Petals network."
    echo "Format: /ip4/x.x.x.x/tcp/31337/p2p/QmPeerID"
    echo "You can add multiple peers separated by commas."
    read -rp "DHT initial peers: " INPUT_PEERS
    if [ -n "$INPUT_PEERS" ]; then
        sed -i "s|^DHT_INITIAL_PEERS=.*|DHT_INITIAL_PEERS=$INPUT_PEERS|" "$ENV_FILE"
    fi

    echo ""
    read -rp "Enable IRC bot? [y/N]: " INPUT_IRC
    if [[ "$INPUT_IRC" =~ ^[Yy] ]]; then
        sed -i "s|^IRC_ENABLED=.*|IRC_ENABLED=true|" "$ENV_FILE"
        read -rp "  IRC server [magickbeans.com]: " INPUT_IRC_SERVER
        [ -n "$INPUT_IRC_SERVER" ] && sed -i "s|^IRC_SERVER=.*|IRC_SERVER=$INPUT_IRC_SERVER|" "$ENV_FILE"
        read -rp "  IRC channel [#pollen]: " INPUT_IRC_CHAN
        [ -n "$INPUT_IRC_CHAN" ] && sed -i "s|^IRC_CHANNEL=.*|IRC_CHANNEL=$INPUT_IRC_CHAN|" "$ENV_FILE"
    else
        sed -i "s|^IRC_ENABLED=.*|IRC_ENABLED=false|" "$ENV_FILE"
    fi

    echo ""
    read -rp "Enable Matrix bot? [y/N]: " INPUT_MATRIX
    if [[ "$INPUT_MATRIX" =~ ^[Yy] ]]; then
        sed -i "s|^MATRIX_ENABLED=.*|MATRIX_ENABLED=true|" "$ENV_FILE"
        read -rp "  Matrix homeserver URL: " INPUT_MATRIX_HS
        [ -n "$INPUT_MATRIX_HS" ] && sed -i "s|^MATRIX_HOMESERVER=.*|MATRIX_HOMESERVER=$INPUT_MATRIX_HS|" "$ENV_FILE"
        read -rp "  Matrix access token: " INPUT_MATRIX_TOKEN
        [ -n "$INPUT_MATRIX_TOKEN" ] && sed -i "s|^MATRIX_ACCESS_TOKEN=.*|MATRIX_ACCESS_TOKEN=$INPUT_MATRIX_TOKEN|" "$ENV_FILE"
        read -rp "  Matrix room ID: " INPUT_MATRIX_ROOM
        [ -n "$INPUT_MATRIX_ROOM" ] && sed -i "s|^MATRIX_ROOM_ID=.*|MATRIX_ROOM_ID=$INPUT_MATRIX_ROOM|" "$ENV_FILE"
    fi

    ok "Configuration saved to $ENV_FILE"
fi

# Source config for service setup
set +u
source "$ENV_FILE" 2>/dev/null || true
set -u

# =============================================================================
# 5) Create systemd service files
# =============================================================================
header "Setting up systemd services"

PYTHON_BIN="$VENV_DIR/bin/python3"
GUNICORN_BIN="$VENV_DIR/bin/gunicorn"
SVC_ENV="PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin"

# --- petals-dht ---
PUBLIC_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo "0.0.0.0")

sudo tee /etc/systemd/system/petals-dht.service > /dev/null <<SVCEOF
[Unit]
Description=Petals DHT Node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=$REPO_DIR
ExecStart=$PYTHON_BIN -m petals.cli.run_dht --host_maddrs /ip4/0.0.0.0/tcp/31337 --announce_maddrs /ip4/$PUBLIC_IP/tcp/31337
Restart=always
RestartSec=15
Environment=$SVC_ENV

[Install]
WantedBy=multi-user.target
SVCEOF
ok "petals-dht.service created"

# --- petals-server ---
PEERS_FLAG=""
PEERS_VAL="${DHT_INITIAL_PEERS:-}"
if [ -n "$PEERS_VAL" ] && [ "$PEERS_VAL" != "/ip4/YOUR_IP/tcp/31337/p2p/YOUR_PEER_ID" ]; then
    PEERS_FLAG="--initial_peers $PEERS_VAL"
fi
MODEL_VAL="${MODEL_REPO:-mistralai/Mixtral-8x7B-Instruct-v0.1}"

sudo tee /etc/systemd/system/petals-server.service > /dev/null <<SVCEOF
[Unit]
Description=Petals Model Server
After=network-online.target petals-dht.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=$REPO_DIR
ExecStart=$PYTHON_BIN -m petals.cli.run_server $MODEL_VAL $PEERS_FLAG --port 31338
Restart=always
RestartSec=30
Environment=$SVC_ENV

[Install]
WantedBy=multi-user.target
SVCEOF
ok "petals-server.service created"

# --- petals-chat (the web UI) ---
PORT_VAL="${PORT:-5000}"

sudo tee /etc/systemd/system/petals-chat.service > /dev/null <<SVCEOF
[Unit]
Description=Pollen Chat Web UI
After=network-online.target petals-server.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=$REPO_DIR
ExecStart=$GUNICORN_BIN app:app --bind 0.0.0.0:$PORT_VAL --worker-class gthread --threads 10 --timeout 600
Restart=always
RestartSec=10
Environment=$SVC_ENV

[Install]
WantedBy=multi-user.target
SVCEOF
ok "petals-chat.service created"

# --- IRC bot ---
sudo tee /etc/systemd/system/pollen-irc.service > /dev/null <<SVCEOF
[Unit]
Description=Pollen IRC Bot
After=network-online.target petals-chat.service
Wants=petals-chat.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=$REPO_DIR
ExecStart=$PYTHON_BIN $REPO_DIR/irc_bot.py
Restart=always
RestartSec=15
Environment=$SVC_ENV

[Install]
WantedBy=multi-user.target
SVCEOF
ok "pollen-irc.service created"

# --- Matrix bot ---
sudo tee /etc/systemd/system/pollen-matrix.service > /dev/null <<SVCEOF
[Unit]
Description=Pollen Matrix Bot
After=network-online.target petals-chat.service
Wants=petals-chat.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=$REPO_DIR
ExecStart=$PYTHON_BIN $REPO_DIR/matrix_bot.py
Restart=always
RestartSec=15
Environment=$SVC_ENV

[Install]
WantedBy=multi-user.target
SVCEOF
ok "pollen-matrix.service created"

# =============================================================================
# 6) Watchdog cron job
# =============================================================================
header "Setting up health watchdog"

sudo touch "$WATCHDOG_LOG"
sudo chown ubuntu:ubuntu "$WATCHDOG_LOG"

CRON_LINE="*/5 * * * * /bin/bash $REPO_DIR/watchdog.sh >> $WATCHDOG_LOG 2>&1"
(crontab -l 2>/dev/null | grep -v "watchdog.sh" || true; echo "$CRON_LINE") | crontab -
ok "Watchdog cron job installed (runs every 5 minutes)"

# =============================================================================
# 7) Enable and start services
# =============================================================================
header "Starting services"

sudo systemctl daemon-reload
sudo systemctl enable petals-dht petals-server petals-chat pollen-irc pollen-matrix 2>/dev/null || true

for svc in petals-dht petals-server petals-chat; do
    sudo systemctl restart "$svc"
    sleep 2
    if systemctl is-active --quiet "$svc"; then
        ok "$svc is running"
    else
        warn "$svc failed to start — check: sudo journalctl -u $svc -n 30"
    fi
done

if grep -qi "^IRC_ENABLED=true" "$ENV_FILE" 2>/dev/null; then
    sudo systemctl restart pollen-irc
    sleep 1
    systemctl is-active --quiet pollen-irc && ok "pollen-irc is running" || warn "pollen-irc failed to start"
else
    info "IRC bot disabled — skipping"
    sudo systemctl stop pollen-irc 2>/dev/null || true
fi

if grep -qi "^MATRIX_ENABLED=true" "$ENV_FILE" 2>/dev/null; then
    sudo systemctl restart pollen-matrix
    sleep 1
    systemctl is-active --quiet pollen-matrix && ok "pollen-matrix is running" || warn "pollen-matrix failed to start"
else
    info "Matrix bot disabled — skipping"
    sudo systemctl stop pollen-matrix 2>/dev/null || true
fi

# =============================================================================
# 8) Verify
# =============================================================================
header "Verifying installation"

sleep 3
echo ""
echo -e "${BOLD}Service Status:${NC}"
echo "  -------------------------------------------------------"
for svc in petals-dht petals-server petals-chat pollen-irc pollen-matrix; do
    STATUS=$(systemctl is-active "$svc" 2>/dev/null || echo "inactive")
    if [ "$STATUS" = "active" ]; then
        echo -e "  ${GREEN}●${NC} $svc"
    else
        echo -e "  ${RED}●${NC} $svc ($STATUS)"
    fi
done
echo "  -------------------------------------------------------"

echo ""
info "Testing chat API..."
sleep 2
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://localhost:${PORT_VAL}/api/status 2>/dev/null || echo "000")
if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 400 ] 2>/dev/null; then
    ok "Chat API is responding (HTTP $HTTP_CODE)"
else
    warn "Chat API not responding yet (HTTP $HTTP_CODE) — it may still be loading the model"
fi

# =============================================================================
# Done!
# =============================================================================
echo ""
echo -e "${GREEN}${BOLD}============================================${NC}"
echo -e "${GREEN}${BOLD}  Pollen installation complete!${NC}"
echo -e "${GREEN}${BOLD}============================================${NC}"
echo ""
echo -e "  Web UI:    ${CYAN}http://${PUBLIC_IP}:${PORT_VAL}${NC}"
echo -e "  Config:    ${CYAN}$ENV_FILE${NC}"
echo -e "  Logs:      ${CYAN}sudo journalctl -u petals-chat -f${NC}"
echo -e "  Watchdog:  ${CYAN}tail -f $WATCHDOG_LOG${NC}"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status petals-chat      # check web UI status"
echo "    sudo systemctl restart petals-server    # restart model server"
echo "    sudo journalctl -u pollen-irc -f        # follow IRC bot logs"
echo "    sudo journalctl -u pollen-matrix -f     # follow Matrix bot logs"
echo ""
