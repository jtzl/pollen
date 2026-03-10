# Pollen Setup Guide

A plain-English walkthrough from zero to running Pollen cluster. This guide assumes you have a fresh Ubuntu server (physical machine, VM, or cloud instance) and basic Linux terminal knowledge.

**Time estimate:** 15 minutes with a good internet connection.

---

## Table of Contents

1. [What you're building](#what-youre-building)
2. [Get a server](#step-1-get-a-server)
3. [Install GPU drivers](#step-2-install-gpu-drivers)
4. [Install Python](#step-3-install-python)
5. [Open your firewall](#step-4-open-your-firewall)
6. [Install Pollen](#step-5-install-pollen)
7. [Configure your node](#step-6-configure-your-node)
8. [Start everything](#step-7-start-everything)
9. [Join an existing cluster](#step-8-join-an-existing-cluster)
10. [Verify it works](#step-9-verify-it-works)
11. [What to do if something goes wrong](#what-to-do-if-something-goes-wrong)

---

## What you're building

By the end of this guide you'll have:

- A **Petals DHT node** that helps other nodes find each other on the network
- A **Petals model server** that runs a chunk of a large language model on your GPU
- The **Pollen web UI** where anyone can chat with the model through their browser
- Optionally, an **IRC bot** and/or **Matrix bot** that bring the model into chat rooms

All of these run as systemd services, so they start on boot and restart if they crash. A watchdog cron job monitors health every 5 minutes.

Here's what the pieces look like:

```
Your Server
├── petals-dht        (helps peers find each other)
├── petals-server     (runs model blocks on your GPU)
├── petals-chat       (web UI on port 5000)
├── pollen-irc        (optional: IRC bot)
├── pollen-matrix     (optional: Matrix bot)
└── watchdog.sh       (cron: auto-restarts crashed services)
```

---

## Step 1: Get a server

You need a Linux machine with a public IP address. Any of these work:

**Cloud (easiest)**
- **AWS EC2:** `g4dn.xlarge` (1x T4 16 GB, ~$0.53/hr) or `g5.xlarge` (1x A10G 24 GB, ~$1.01/hr)
- **Google Cloud:** `n1-standard-4` + 1x T4
- **Lambda Labs:** cheapest GPU instances, often has A10 and A100 available
- **Vast.ai / RunPod:** community GPU rentals, cheapest option

**Home server**
- Any desktop with an NVIDIA GPU (8 GB+ VRAM recommended)
- Must have a public IP or be able to set up port forwarding (see Step 4)

**Minimum specs:**
- 4 GB RAM (8 GB recommended)
- 20 GB free disk space
- NVIDIA GPU with 8 GB+ VRAM (CPU works but is very slow)
- Ubuntu 20.04 or newer

SSH into your server before continuing:

```bash
ssh user@your-server-ip
```

---

## Step 2: Install GPU drivers

**Skip this step if:** You're running on CPU only, or your cloud instance came with drivers pre-installed (test with `nvidia-smi`).

### Check if drivers are already installed

```bash
nvidia-smi
```

If this shows your GPU model and driver version, skip to Step 3. If it says "command not found", continue below.

### Install NVIDIA drivers on Ubuntu

```bash
# Update package list
sudo apt update

# Install the driver (535 is stable as of 2024; use the version recommended for your GPU)
sudo apt install -y nvidia-driver-535

# Reboot to load the driver
sudo reboot
```

After reboot, SSH back in and verify:

```bash
nvidia-smi
```

You should see something like:

```
+-----------------------------------------------------------------------------+
| NVIDIA-SMI 535.xx.xx    Driver Version: 535.xx.xx    CUDA Version: 12.2     |
|-------------------------------+----------------------+----------------------+
| GPU  Name        Persistence-M| Bus-Id        Disp.A | Volatile Uncorr. ECC |
| Fan  Temp  Perf  Pwr:Usage/Cap|         Memory-Usage | GPU-Util  Compute M. |
|   0  Tesla T4            Off  | 00000000:00:1E.0 Off |                    0 |
| N/A   30C    P8     9W /  70W |      0MiB / 15360MiB |      0%      Default |
+-------------------------------+----------------------+----------------------+
```

### Troubleshooting GPU drivers

If the driver install fails:

```bash
# Remove any broken installs
sudo apt remove --purge 'nvidia-*'
sudo apt autoremove

# Add the official NVIDIA PPA for latest drivers
sudo add-apt-repository ppa:graphics-drivers/ppa
sudo apt update
sudo apt install -y nvidia-driver-535
sudo reboot
```

If you have a very new GPU (RTX 4090, H100, etc.), you may need driver version 545 or newer:

```bash
sudo apt install -y nvidia-driver-545
```

---

## Step 3: Install Python

**Skip this step if:** `python3 --version` shows 3.8 or newer.

```bash
# Check current version
python3 --version

# If missing or too old, install Python 3.10
sudo apt update
sudo apt install -y python3 python3-pip python3-venv
```

Verify:

```bash
python3 --version
# Should print: Python 3.10.x (or newer)
```

---

## Step 4: Open your firewall

Pollen needs three ports open:

| Port | Protocol | Purpose | Who connects |
|------|----------|---------|-------------|
| **31337** | TCP | Petals DHT (peer discovery) | Other Petals nodes worldwide |
| **31338** | TCP | Petals model server | Other Petals nodes worldwide |
| **5000** | TCP | Pollen web UI | Anyone you want to use the chat |

### AWS EC2

Go to **EC2 Console > Security Groups > your instance's security group > Inbound Rules > Edit**:

| Type | Protocol | Port Range | Source |
|------|----------|------------|--------|
| Custom TCP | TCP | 31337 | 0.0.0.0/0 |
| Custom TCP | TCP | 31338 | 0.0.0.0/0 |
| Custom TCP | TCP | 5000 | 0.0.0.0/0 |

### UFW (Ubuntu's built-in firewall)

```bash
# Check if UFW is active
sudo ufw status

# If active, open the ports
sudo ufw allow 31337/tcp
sudo ufw allow 31338/tcp
sudo ufw allow 5000/tcp

# Verify
sudo ufw status numbered
```

### iptables (if not using UFW)

```bash
sudo iptables -A INPUT -p tcp --dport 31337 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 31338 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 5000 -j ACCEPT

# Make rules persist across reboot
sudo apt install -y iptables-persistent
sudo netfilter-persistent save
```

### Home server / NAT traversal

If your server is behind a home router, you need to forward the ports:

1. Find your server's local IP: `ip addr show | grep "inet "` (look for something like `192.168.1.x`)
2. Log into your router's admin page (usually `192.168.1.1`)
3. Find "Port Forwarding" or "NAT" settings
4. Add forwards:
   - External port 31337 → internal `192.168.1.x:31337` (TCP)
   - External port 31338 → internal `192.168.1.x:31338` (TCP)
   - External port 5000 → internal `192.168.1.x:5000` (TCP)
5. Find your public IP: `curl ifconfig.me`

**If port forwarding doesn't work** (carrier-grade NAT), your ISP doesn't give you a real public IP. Options:
- Call your ISP and ask for a public IP (some provide this free on request)
- Use a cheap VPS ($5/month from DigitalOcean/Hetzner) and WireGuard tunnel
- Use Tailscale or Cloudflare Tunnel as a workaround

**Test that your ports are open** (from a different machine):

```bash
nc -zv YOUR_PUBLIC_IP 31337
nc -zv YOUR_PUBLIC_IP 5000
```

---

## Step 5: Install Pollen

The fastest way:

```bash
# Clone the repository
cd /data  # or wherever you want it
git clone https://github.com/nalamk/pollen.git chat-ui
cd chat-ui

# Run the installer
sudo ./install.sh
```

The installer will:
- Check your Python version and GPU
- Create a virtual environment at `/data/petals-env`
- Install all Python dependencies
- Ask you to configure the model and network (see Step 6)
- Create systemd services for all components
- Set up the watchdog cron job
- Start everything

If you prefer to install manually, see the [Manual Installation](README.md#manual-installation) section in the README.

---

## Step 6: Configure your node

The installer prompts you for configuration. Here's what each question means:

### Model repository

```
Model repository [mistralai/Mixtral-8x7B-Instruct-v0.1]:
```

This is the HuggingFace model ID. Press Enter to accept the default (Mixtral 8x7B), or type a different model. The model must be supported by Petals. Popular choices:

- `mistralai/Mixtral-8x7B-Instruct-v0.1` — good all-around, mixture-of-experts
- `meta-llama/Llama-2-70b-chat-hf` — Meta's Llama 2 (requires HuggingFace access approval)
- `bigscience/bloom` — open multilingual model

### DHT initial peers

```
DHT initial peers:
```

This is how your node finds the Petals network. You need at least one peer's multiaddress. Format:

```
/ip4/54.166.153.249/tcp/31337/p2p/QmExamplePeerID123
```

**Where to get peers:**
- If joining an existing cluster, the cluster admin gives you this address
- If starting a new cluster, leave blank — you'll be the first peer and others will connect to you
- Check the [Petals health monitor](https://health.petals.dev) for public peers

You can enter multiple peers separated by commas.

### IRC and Matrix bots

The installer asks if you want to enable bots. You can always enable them later by editing `.env`:

```bash
nano /data/chat-ui/.env
# Change IRC_ENABLED=true or MATRIX_ENABLED=true
# Then restart the service:
sudo systemctl restart pollen-irc
```

---

## Step 7: Start everything

If you used `install.sh`, everything is already started. To manage services manually:

```bash
# Start all core services
sudo systemctl start petals-dht
sudo systemctl start petals-server
sudo systemctl start petals-chat

# Start bots (if enabled)
sudo systemctl start pollen-irc
sudo systemctl start pollen-matrix

# Check what's running
sudo systemctl status petals-dht petals-server petals-chat pollen-irc pollen-matrix
```

**First startup takes a while.** The model server needs to:
1. Download model weights from HuggingFace (~10-90 GB depending on model, cached after first download)
2. Load the weights onto your GPU
3. Register with the DHT network
4. Wait for enough peers to cover all model blocks

Watch the progress:

```bash
sudo journalctl -u petals-server -f
```

---

## Step 8: Join an existing cluster

If someone else is already running Pollen/Petals and you want to add your GPU to their network:

1. **Get their DHT peer address.** Ask the cluster admin for their multiaddress. It looks like:
   ```
   /ip4/54.166.153.249/tcp/31337/p2p/QmXxxYyyZzz...
   ```

2. **Add it to your config:**
   ```bash
   nano /data/chat-ui/.env
   # Set:
   DHT_INITIAL_PEERS=/ip4/54.166.153.249/tcp/31337/p2p/QmXxxYyyZzz
   ```

3. **Restart the services:**
   ```bash
   sudo systemctl restart petals-dht
   sudo systemctl restart petals-server
   sudo systemctl restart petals-chat
   ```

4. **Verify connection:** Check the status API to see if peers are visible:
   ```bash
   curl -s http://localhost:5000/api/status | python3 -m json.tool
   ```
   Look for `"num_peers"` — it should be greater than 0.

### Starting a new cluster

If you're the first node:

1. Leave `DHT_INITIAL_PEERS` empty (or remove it)
2. Start `petals-dht` — it will create a new DHT network
3. Find your peer ID:
   ```bash
   sudo journalctl -u petals-dht | grep "peer_id"
   ```
4. Your multiaddress is: `/ip4/YOUR_PUBLIC_IP/tcp/31337/p2p/YOUR_PEER_ID`
5. Share this address with others so they can join

### Adding more servers to the cluster

Each additional server that joins helps the network:
- **More GPU VRAM = more model blocks covered** — once all blocks of the model are covered by at least one peer, the model can generate text.
- **Redundant coverage = reliability** — if two peers both serve blocks 0–7, the network keeps working if one goes offline.
- **More peers = higher throughput** — Petals can pipeline requests across multiple paths through the network.

---

## Step 9: Verify it works

### Check service status

```bash
# Quick overview
for svc in petals-dht petals-server petals-chat pollen-irc pollen-matrix; do
    status=$(systemctl is-active $svc 2>/dev/null)
    echo "$svc: $status"
done
```

### Check the API

```bash
curl -s http://localhost:5000/api/status | python3 -m json.tool
```

You should see:

```json
{
    "ok": true,
    "model_name": "default",
    "num_peers": 3,
    "block_coverage": 32,
    "total_blocks": 32,
    "tokens_per_second": 2.5,
    "uptime_seconds": 3600
}
```

Key things to check:
- `"ok": true` — the API is responding
- `"block_coverage"` equals `"total_blocks"` — all model blocks are served by at least one peer
- `"num_peers"` > 0 — you're connected to the network

### Open the web UI

In your browser, go to:

```
http://YOUR_SERVER_IP:5000
```

You should see the Pollen chat interface. Type a message and press Enter. If everything is working, you'll see the model start typing a response in real-time.

### Test from the command line

```bash
curl -s http://localhost:5000/api/v1/generate \
    -X POST -H "Content-Type: application/json" \
    -d '{"model": "default", "inputs": "What is the meaning of life?", "max_new_tokens": 50}' \
    | python3 -m json.tool
```

---

## What to do if something goes wrong

### Services won't start

```bash
# Check what went wrong
sudo journalctl -u SERVICE_NAME -n 50 --no-pager

# Common issues:
# - "ModuleNotFoundError" → virtual env not activated in service file
#   Fix: re-run install.sh
# - "Address already in use" → something else is using the port
#   Fix: ss -tlnp | grep PORT_NUMBER, then kill the process
# - "No such file or directory" → wrong path in service file
#   Fix: check /etc/systemd/system/SERVICE_NAME.service
```

### No peers connecting

1. Verify your ports are open (Step 4)
2. Verify your `DHT_INITIAL_PEERS` is correct
3. Check the DHT logs: `sudo journalctl -u petals-dht -f`
4. Try pinging the peer: `nc -zv PEER_IP 31337`

### Model generates garbage or doesn't respond

- **Block coverage incomplete:** Check `curl localhost:5000/api/status` — `block_coverage` must equal `total_blocks`
- **Not enough VRAM:** Check `nvidia-smi` for memory usage
- **Wrong model version:** Make sure all peers in the cluster are serving the same model

### Need to start over

```bash
# Stop everything
sudo systemctl stop petals-dht petals-server petals-chat pollen-irc pollen-matrix

# Remove services
sudo rm /etc/systemd/system/petals-*.service /etc/systemd/system/pollen-*.service
sudo systemctl daemon-reload

# Remove virtual environment
sudo rm -rf /data/petals-env

# Remove watchdog cron
crontab -l | grep -v watchdog | crontab -

# Re-run installer
cd /data/chat-ui
sudo ./install.sh
```

---

## Next steps

- **Read the full README** for configuration details on every `.env` variable
- **Set up the watchdog** if the installer didn't do it already — it keeps things running unattended
- **Enable bots** to bring the model into IRC or Matrix channels
- **Add more GPUs** to the cluster for better performance and redundancy
- **Check the Petals docs** at [petals.dev](https://petals.dev) for advanced networking and tuning
