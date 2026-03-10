# Pollen

**Decentralized AI chat, powered by the swarm.**

Pollen is a self-hosted chat interface that runs large language models across a distributed network of GPUs using [Petals](https://petals.dev). Instead of needing a single expensive server, Pollen splits the model across multiple machines — anyone can contribute GPU power, and everyone gets access to state-of-the-art models.

Think of it as BitTorrent for AI inference: each node serves a few layers of the model, and Pollen stitches them together into a seamless chat experience with a modern web UI, IRC bot, Matrix bot, and more.

## Features

- **Chat Web UI** — real-time streaming chat with a clean, dark-themed interface. Markdown rendering, syntax highlighting, adjustable parameters (temperature, top-p, max tokens), and multi-conversation support.
- **IRC Bot** — drop Pollen into any IRC channel. Users can chat with the model, generate images, and check network status with simple commands.
- **Matrix Bot** — same functionality as the IRC bot, but for Matrix rooms. Responds to mentions and commands.
- **Image Generation** — text-to-image via Stable Diffusion (SDXL-Turbo by default). Available through the web UI, IRC, and Matrix.
- **Network Telemetry** — live dashboard showing peer connectivity, block coverage across the model, throughput stats, and uptime.
- **Health Watchdog** — cron-based monitor that checks service health every 5 minutes and automatically restarts crashed components.
- **REST & WebSocket APIs** — programmatic access for building your own integrations on top of Pollen.

## Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| **OS** | Ubuntu 20.04+ / Debian 11+ | Ubuntu 22.04 LTS |
| **Python** | 3.8 | 3.10+ |
| **RAM** | 4 GB | 8 GB+ |
| **GPU** | None (CPU works, slowly) | NVIDIA GPU with 8 GB+ VRAM |
| **CUDA** | 11.7 (if using GPU) | 12.x |
| **Network** | Open port 31337 (TCP) for DHT | Static IP or port-forwarded |
| **Disk** | 2 GB for code + deps | 10 GB+ if caching model weights |

## Quick Start

```bash
git clone https://github.com/nalamk/pollen.git
cd pollen
sudo ./install.sh
```

The install script handles everything automatically:

1. Checks for Python 3.8+ and GPU availability
2. Creates a virtual environment and installs all dependencies
3. Walks you through basic configuration (model, DHT peers, bots)
4. Sets up systemd services for all components
5. Installs the health watchdog cron job
6. Starts everything and verifies it's running

After installation, open `http://YOUR_IP:5000` in your browser.

## Manual Installation

If you prefer to set things up yourself:

### 1. Clone and enter the repository

```bash
git clone https://github.com/nalamk/pollen.git
cd pollen
```

### 2. Create a virtual environment

```bash
python3 -m venv /data/petals-env
source /data/petals-env/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
pip install irc matrix-nio    # for chat bots
```

### 4. Configure

```bash
cp .env.example .env
nano .env                     # edit to taste — see Configuration Guide below
```

At minimum, set `DHT_INITIAL_PEERS` to connect to the Petals network.

### 5. Start the DHT node

```bash
python -m petals.cli.run_dht \
    --host_maddrs /ip4/0.0.0.0/tcp/31337 \
    --announce_maddrs /ip4/YOUR_PUBLIC_IP/tcp/31337
```

### 6. Start the model server

```bash
python -m petals.cli.run_server mistralai/Mixtral-8x7B-Instruct-v0.1 \
    --initial_peers /ip4/PEER_IP/tcp/31337/p2p/PEER_ID \
    --port 31338
```

### 7. Start the web UI

```bash
gunicorn app:app \
    --bind 0.0.0.0:5000 \
    --worker-class gthread \
    --threads 10 \
    --timeout 600
```

### 8. (Optional) Start the bots

```bash
python irc_bot.py      # IRC bot
python matrix_bot.py   # Matrix bot
```

### 9. (Optional) Set up the watchdog

Add to crontab (`crontab -e`):

```cron
*/5 * * * * /bin/bash /data/chat-ui/watchdog.sh >> /var/log/pollen-watchdog.log 2>&1
```

## Configuration Guide

All configuration is done through the `.env` file. Copy `.env.example` to `.env` and edit the values.

### App Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_NAME` | `Pollen` | Name displayed in the browser tab and UI header. Change this if you're running a branded instance. |
| `PORT` | `5000` | TCP port the web UI listens on. Make sure your firewall allows inbound traffic on this port. |

### Model Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_REPO` | `mistralai/Mixtral-8x7B-Instruct-v0.1` | HuggingFace repository ID of the model to serve. Must be a model supported by Petals. Examples: `meta-llama/Llama-2-70b-chat-hf`, `bigscience/bloom`. |
| `MODEL_DISPLAY_NAME` | `Mixtral 8x7B Instruct` | Human-friendly model name shown in the UI. |
| `MODEL_BADGE` | `8x7B` | Short label shown as a badge next to the app name in the UI. |
| `MODEL_CARD_URL` | *(auto)* | Link to the model card. Auto-generated from `MODEL_REPO` if not set. |
| `MODEL_LICENSE_URL` | *(auto)* | Link to the model license. Auto-generated from `MODEL_REPO` if not set. |
| `DEFAULT_MAX_TOKENS` | `500` | Default maximum tokens per response. Users can override this in the UI settings panel. Higher values allow longer responses but increase latency. |

### Network Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `DHT_INITIAL_PEERS` | *(required)* | Comma-separated list of Petals DHT peer multiaddresses. This is how your node finds the network. Format: `/ip4/x.x.x.x/tcp/31337/p2p/QmPeerID`. Get peers from the [Petals community](https://petals.dev) or run your own DHT node. |

### UI Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `SHOW_PETALS_BRANDING` | `true` | Show the "Powered by Petals" badge in the UI. Set to `false` for a clean branded experience. |
| `WELCOME_MESSAGE` | *(see .env.example)* | Welcome text shown on the chat screen before any messages. Supports plain text. |

### IRC Bot Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `IRC_ENABLED` | `true` | Enable or disable the IRC bot entirely. |
| `IRC_SERVER` | `magickbeans.com` | IRC server hostname to connect to. |
| `IRC_PORT` | `6667` | IRC server port. Use `6697` for SSL. |
| `IRC_CHANNEL` | `#pollen` | Channel the bot joins on connect. |
| `IRC_NICKNAME` | `PollenBot` | Bot's IRC nickname. |
| `IRC_API_BASE` | `http://127.0.0.1:5000` | Base URL of the Pollen API the bot calls. Only change this if the web UI runs on a different host/port. |

### Matrix Bot Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MATRIX_ENABLED` | `false` | Enable or disable the Matrix bot. |
| `MATRIX_HOMESERVER` | *(empty)* | Full URL of your Matrix homeserver, e.g. `https://matrix.org`. |
| `MATRIX_USER_ID` | *(empty)* | Bot's Matrix user ID, e.g. `@pollenbot:matrix.org`. |
| `MATRIX_ACCESS_TOKEN` | *(empty)* | Access token for the bot account. Generate one via Element or the Matrix API. |
| `MATRIX_ROOM_ID` | *(empty)* | Room the bot listens in, e.g. `!abc123:matrix.org`. |
| `MATRIX_DISPLAY_NAME` | `PollenBot` | Display name the bot uses in the room. |
| `MATRIX_API_BASE` | `http://127.0.0.1:5000` | Base URL of the Pollen API. |

### Image Generation Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `IMAGE_ENABLED` | `true` | Enable text-to-image generation. Requires additional disk space for the diffusion model. |
| `IMAGE_MODEL` | `stabilityai/sdxl-turbo` | HuggingFace model for image generation. SDXL-Turbo is fast and works on CPU. |
| `IMAGE_DEVICE` | `cpu` | Device for image generation: `cpu` or `cuda`. GPU is significantly faster. |
| `IMAGE_STEPS` | `4` | Number of diffusion steps. More steps = higher quality but slower. SDXL-Turbo works well with 4. |
| `IMAGE_WIDTH` | `512` | Output image width in pixels. |
| `IMAGE_HEIGHT` | `512` | Output image height in pixels. |

## Architecture Overview

### How Petals distributes a model

A large language model like Mixtral 8x7B has dozens of transformer layers (called "blocks"). Running all of them on one machine requires expensive hardware. Petals solves this by splitting the model across many machines — each one holds and runs a few consecutive blocks.

```
Full model: 32 transformer blocks
            ┌─────────────────────────────────────────────────┐
            │ 0  1  2  3  4  5  6  7  8  ... 28 29 30 31     │
            └─────────────────────────────────────────────────┘

Distributed across 4 peers:
  Peer A (8 GB GPU):  blocks 0–7      ████████░░░░░░░░░░░░░░░░░░░░░░░░
  Peer B (8 GB GPU):  blocks 8–15     ░░░░░░░░████████░░░░░░░░░░░░░░░░
  Peer C (16 GB GPU): blocks 16–27    ░░░░░░░░░░░░░░░░████████████░░░░
  Peer D (4 GB GPU):  blocks 28–31    ░░░░░░░░░░░░░░░░░░░░░░░░░░░░████
```

When generating text, hidden states flow through the chain: Peer A processes blocks 0–7 and sends the result to Peer B, who processes 8–15, and so on. The final peer produces the next token. This happens for every token, but Petals pipelines the work so multiple tokens can be in-flight at once.

**DHT (Distributed Hash Table)** is how peers find each other. Every peer registers which blocks it serves in a shared DHT. When a client wants to generate text, it looks up the DHT to find a path through all blocks. Port 31337/TCP must be open for DHT communication.

### How Pollen connects to Petals

Pollen is the user-facing layer on top of Petals:

```
 Users
  │
  ├── Browser ──── WebSocket ──┐
  ├── IRC ─────── irc_bot.py ──┤
  └── Matrix ── matrix_bot.py ─┤
                               │
                          ┌────▼─────────────────┐
                          │     Pollen Web App    │
                          │   (Flask + Gunicorn)  │
                          │                       │
                          │  app.py ── routes      │
                          │  websocket_api.py      │  Streaming token generation
                          │  http_api.py           │  Single-shot generation
                          │  status_api.py         │  Network telemetry
                          │  image_api.py          │  Text-to-image
                          │  image_gen.py          │  Diffusion pipeline
                          └────┬─────────────────┘
                               │
                    AutoDistributedModelForCausalLM
                          (petals client)
                               │
              ┌────────────────┼────────────────┐
              │                │                │
         ┌────▼────┐     ┌────▼────┐      ┌────▼────┐
         │ Peer A  │     │ Peer B  │      │ Peer C  │
         │ blk 0-7 │────▶│ blk 8-15│─────▶│blk 16-31│
         └─────────┘     └─────────┘      └─────────┘
                    DHT (peer discovery)
```

1. **`app.py`** boots Flask, loads the distributed model via `utils.load_models()`, and registers all route blueprints.
2. **`utils.py`** calls `AutoDistributedModelForCausalLM.from_pretrained()` with the DHT peer list. This creates a client that knows how to route inference through remote peers.
3. **`websocket_api.py`** handles the primary chat path: the browser opens a WebSocket, sends prompts, and receives tokens one at a time as they're generated across the swarm. It tracks generation speed for the telemetry dashboard.
4. **`http_api.py`** provides a simpler REST endpoint for non-streaming generation.
5. **`status_api.py`** queries the DHT to build a real-time map of which peers are online, which blocks they serve, and whether the full model is covered.
6. **`image_gen.py`** runs a local Stable Diffusion pipeline (not distributed) for text-to-image generation.
7. **`irc_bot.py`** and **`matrix_bot.py`** connect to chat networks and forward user messages to the local HTTP API, posting responses back to the channel/room.
8. **`watchdog.sh`** runs via cron every 5 minutes, checks if the API responds, and restarts crashed services.

### Data flow for a single chat message

```
1. User types "Hello" in the browser
2. Browser opens WebSocket to /api/v2/generate
3. Browser sends: {"type": "open_inference_session", "model": "...", "max_length": 8192}
4. Server creates a petals inference session (finds peers via DHT)
5. Browser sends: {"type": "generate", "inputs": "[INST] Hello [/INST]", ...}
6. Server tokenizes input, calls model.generate() with the session
7. Petals routes hidden states: Peer A → Peer B → Peer C → ... → final token
8. Server decodes the token and sends: {"ok": true, "outputs": "Hi", "stop": false}
9. Steps 6–8 repeat for each token until a stop sequence or max tokens
10. Browser renders tokens as they arrive (streaming effect)
```

## Troubleshooting

### NAT / Firewall Issues

**Symptom:** DHT node starts but no peers connect. Block coverage stays at 0. The status dashboard shows 0 peers.

**Why this happens:** Petals peers communicate over TCP. If your machine is behind a firewall, NAT router, or cloud security group that blocks inbound connections on port 31337, other peers can't reach you, and you can't participate in the DHT.

**Diagnosis:**

```bash
# From another machine, test if the port is reachable
nc -zv YOUR_PUBLIC_IP 31337

# Check if the service is even listening locally
ss -tlnp | grep 31337

# Check if UFW is blocking it
sudo ufw status verbose

# Check iptables rules
sudo iptables -L -n | grep 31337
```

**Fixes:**

```bash
# AWS EC2: add inbound rule to your security group
#   Protocol: TCP, Port: 31337, Source: 0.0.0.0/0
# Also add port 31338 if running a model server
# Also add port 5000 if you want the web UI accessible externally

# UFW
sudo ufw allow 31337/tcp
sudo ufw allow 31338/tcp
sudo ufw allow 5000/tcp

# iptables (if not using UFW)
sudo iptables -A INPUT -p tcp --dport 31337 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 31338 -j ACCEPT
```

**Carrier-grade NAT (home ISP):** If your ISP puts you behind CGNAT, you won't have a real public IP. Options:
- Use a VPS/cloud server instead (EC2, DigitalOcean, Hetzner)
- Set up a WireGuard or Tailscale tunnel to a machine with a public IP
- Ask your ISP for a static public IP (sometimes available on request)

You can check for CGNAT by comparing `curl ifconfig.me` with your router's WAN IP. If they differ, you're behind CGNAT.

### GPU / CUDA Driver Issues

**Symptom:** `torch.cuda.is_available()` returns `False`, `RuntimeError: CUDA error`, or the model falls back to CPU.

**Diagnosis:**

```bash
# Is the NVIDIA driver installed and working?
nvidia-smi
# Should show your GPU model, driver version, and CUDA version

# Does PyTorch see the GPU?
python3 -c "import torch; print('CUDA available:', torch.cuda.is_available()); print('CUDA version:', torch.version.cuda); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"
```

**Common problems and fixes:**

| Problem | Fix |
|---------|-----|
| `nvidia-smi` not found | Install NVIDIA drivers: `sudo apt install nvidia-driver-535` then reboot |
| Driver installed but `nvidia-smi` shows error | Reboot. If still broken: `sudo dkms autoinstall` then reboot |
| PyTorch says CUDA not available but `nvidia-smi` works | PyTorch was installed without CUDA support. Reinstall: `pip install torch --index-url https://download.pytorch.org/whl/cu121` |
| CUDA version mismatch | The CUDA version in `nvidia-smi` is the *driver* capability. PyTorch needs a compatible version. Driver CUDA 12.x works with PyTorch CUDA 11.8 and 12.1. |
| `CUDA out of memory` | See VRAM section below |

### VRAM / Out of Memory Errors

**Symptom:** `torch.cuda.OutOfMemoryError`, `CUDA out of memory`, or the process gets killed by the OOM killer.

**Diagnosis:**

```bash
# Check current VRAM usage
nvidia-smi

# Check what's using GPU memory
sudo fuser -v /dev/nvidia*

# Check system RAM and swap
free -h
```

**Fixes:**

- **Other processes using VRAM:** Kill unused GPU processes. A common culprit is a previous crashed Python process still holding VRAM.
  ```bash
  # Find and kill zombie GPU processes
  nvidia-smi --query-compute-apps=pid --format=csv,noheader | xargs -I {} kill {}
  ```
- **Model too large:** Each Petals server only loads a few blocks, not the whole model. If you're running out of VRAM, reduce the number of blocks you serve:
  ```bash
  python -m petals.cli.run_server MODEL_NAME --num_blocks 4  # serve fewer blocks
  ```
- **Image generation competing for VRAM:** If `IMAGE_DEVICE=cuda`, the diffusion model also uses VRAM. Set `IMAGE_DEVICE=cpu` in `.env` to free up GPU memory for the language model, or disable image generation entirely with `IMAGE_ENABLED=false`.
- **Swap space:** Add swap as a safety net:
  ```bash
  sudo fallocate -l 8G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  ```

### Model Loading Failures

**Symptom:** `petals-server` or `petals-chat` crashes on startup. Errors about downloads, timeouts, or missing files.

| Error | Cause | Fix |
|-------|-------|-----|
| `HTTPError: 401` or `403` from HuggingFace | Model requires authentication (e.g. Llama 2) | Run `huggingface-cli login` with a token that has access to the model |
| `ConnectionError` during download | Network issue or HuggingFace is down | Check internet, retry. Partial downloads resume automatically. |
| `OSError: No space left on device` | Model weights filling disk | Clear cache: `du -sh ~/.cache/huggingface/` and remove old models |
| `KeyError` or `ValueError` on model load | Model not supported by Petals | Check [Petals supported models](https://github.com/bigscience-workshop/petals#supported-models) |
| Hangs on "Loading model..." for 10+ min | Normal for first load (downloading weights) | Watch `journalctl -u petals-chat -f` for progress. First load of Mixtral downloads ~90 GB of weights. |

### Web UI Not Loading

**Symptom:** Browser shows connection refused, timeout, or a blank page.

**Step-by-step diagnosis:**

```bash
# 1. Is the service running?
sudo systemctl status petals-chat

# 2. Is anything listening on the port?
ss -tlnp | grep 5000

# 3. Check for startup errors
sudo journalctl -u petals-chat -n 50 --no-pager

# 4. Can you reach it locally?
curl -s http://localhost:5000/api/status | python3 -m json.tool

# 5. Can you reach it from outside? (run from your local machine)
curl -s http://YOUR_SERVER_IP:5000/api/status
```

**Common fixes:**

- **Service not running:** `sudo systemctl restart petals-chat` — check logs for the error
- **Port blocked:** Add firewall rule (see NAT section above)
- **Model still loading:** The web UI won't respond until the distributed model connects to enough peers to cover all blocks. This can take a few minutes on first start.
- **Gunicorn crash loop:** Check if `gunicorn` binary exists: `ls -la /data/petals-env/bin/gunicorn`. If missing: `pip install gunicorn[gthread]`

### IRC / Matrix Bot Not Responding

**Symptom:** Bot joins the channel/room but ignores messages, or doesn't join at all.

**Diagnosis:**

```bash
# Check bot logs
sudo journalctl -u pollen-irc -f
sudo journalctl -u pollen-matrix -f

# Test the API the bots call
curl -s http://127.0.0.1:5000/api/v1/generate \
    -X POST -H "Content-Type: application/json" \
    -d '{"model": "default", "inputs": "Hello", "max_new_tokens": 10}'
```

**IRC-specific problems:**

| Problem | Fix |
|---------|-----|
| `Connection refused` | Wrong `IRC_SERVER` or `IRC_PORT`. Try `6697` for SSL, `6667` for plain. |
| Bot joins but doesn't speak | It only responds to mentions (its nickname) or commands. Try: `PollenBot: hello` |
| `NickServ` kills the bot | The nickname is registered. Change `IRC_NICKNAME` in `.env`. |
| SSL errors | If the server doesn't support SSL, set `IRC_SSL=false` and use port `6667`. |
| Channel requires invite | The bot can't join invite-only channels. Set the channel to open or invite the bot first. |

**Matrix-specific problems:**

| Problem | Fix |
|---------|-----|
| `M_UNKNOWN_TOKEN` | Access token is invalid or expired. Generate a new one. |
| Bot doesn't join the room | Invite the bot user to the room first, or set the room to public. |
| `M_FORBIDDEN` on send | Bot lacks permission to send messages in the room. Check room power levels. |
| No response to messages | Bot only responds to mentions or `!` commands. Try: `@pollenbot:server hello` |

### Watchdog Keeps Restarting Services

**Symptom:** Services restart every 5 minutes. Watchdog log shows `FAIL: Chat API unresponsive`.

**Why this happens:** The watchdog checks `http://localhost:5000/api/status` every 5 minutes. If the model is still loading (which can take several minutes on first boot), the API won't respond, and the watchdog restarts everything — which resets the loading process, creating an infinite loop.

**Fix:**

```bash
# 1. Temporarily disable the watchdog while things stabilize
crontab -l | grep -v watchdog | crontab -

# 2. Start services manually and wait for the model to load
sudo systemctl restart petals-server
sudo systemctl restart petals-chat
# Watch the logs — wait until you see "Loading complete" or similar
sudo journalctl -u petals-chat -f

# 3. Once the API responds, re-enable the watchdog
(crontab -l 2>/dev/null; echo "*/5 * * * * /bin/bash /data/chat-ui/watchdog.sh >> /var/log/pollen-watchdog.log 2>&1") | crontab -
```

### Common Error Messages

| Error | Meaning | Fix |
|-------|---------|-----|
| `No peers found for blocks X-Y` | No server in the network is serving those blocks | Wait for more peers to come online, or run `petals.cli.run_server` to serve those blocks yourself |
| `Session expired` | Inference session timed out (5 min idle) | Refresh the page and start a new chat |
| `Connection to remote peer lost` | A peer went offline mid-generation | Petals will retry automatically. If persistent, the network may be too small. |
| `RemoteExceptionDuringForward` | A peer crashed while processing your request | Usually transient. Retry the message. |
| `ValueError: too many values to unpack` | Config mismatch between client and server Petals versions | Make sure all peers run the same Petals version |

### Checking Logs

All services log to systemd journal:

```bash
sudo journalctl -u petals-dht -f       # DHT node
sudo journalctl -u petals-server -f     # model server
sudo journalctl -u petals-chat -f       # web UI
sudo journalctl -u pollen-irc -f        # IRC bot
sudo journalctl -u pollen-matrix -f     # Matrix bot
tail -f /var/log/pollen-watchdog.log    # watchdog

# Show last 100 lines of a service
sudo journalctl -u petals-chat -n 100 --no-pager

# Show logs since last boot
sudo journalctl -u petals-chat -b

# Show logs from a specific time
sudo journalctl -u petals-chat --since "2024-01-15 10:00:00"
```

## API Reference

### WebSocket — `ws://HOST:5000/api/v2/generate`

Real-time streaming text generation. Connect via WebSocket, send JSON to start a session, then send generation requests to receive tokens as they're generated.

### REST — `POST /api/v1/generate`

Single-shot text generation. Send a JSON body with `model`, `inputs`, `max_new_tokens`, `temperature`, `top_p`, and receive the full response.

### Image — `POST /api/generate-image`

Text-to-image generation. Send `{"prompt": "a sunset over mountains"}` and receive `{"ok": true, "filename": "abc123.png", "url": "/static/generated/abc123.png"}`.

### Image Status — `GET /api/image-status`

Returns whether image generation is enabled, loading, and ready.

### Network Status — `GET /api/status`

Returns JSON with network health data: peer count, block coverage map, tokens/second throughput, and uptime.

## Contributing

Contributions are welcome. Here's how to get started:

### Setting up for development

```bash
git clone https://github.com/nalamk/pollen.git
cd pollen
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your DHT peers, then:
gunicorn app:app --bind 0.0.0.0:5000 --worker-class gthread --threads 10 --timeout 600
```

### Project structure

```
pollen/
  app.py              # Flask app initialization, model loading
  config.py           # All configuration from .env
  utils.py            # Model loading, tokenizer utilities
  views.py            # HTML template rendering
  data_structures.py  # Config dataclasses
  websocket_api.py    # WebSocket streaming generation endpoint
  http_api.py         # REST generation endpoint
  status_api.py       # Network telemetry endpoint
  image_api.py        # Image generation endpoint
  image_gen.py        # Stable Diffusion pipeline
  speed_tracker.py    # Token generation speed tracking
  irc_bot.py          # IRC bot (standalone process)
  matrix_bot.py       # Matrix bot (standalone process)
  watchdog.sh         # Health check cron script
  install.sh          # One-click installer
  templates/          # Jinja2 HTML templates
  static/             # CSS, JS, generated images
  .env.example        # Configuration template
```

### Guidelines

- **Keep it simple.** Pollen is meant to be easy to understand and deploy. Avoid adding heavy frameworks or complex abstractions.
- **Test with a real Petals network.** The distributed model behaves differently from local inference. Test with at least 2 peers if possible.
- **Configuration via `.env`.** New features that need user configuration should add a variable to `.env.example` with a sensible default and document it in the README.
- **Bots are standalone processes.** `irc_bot.py` and `matrix_bot.py` talk to Pollen through its HTTP API, not by importing app internals. Keep it that way — it means bots can run on different machines.
- **Don't break the install script.** If you add a new dependency, add it to `requirements.txt`. If you add a new service, add it to `install.sh`.

### What to work on

- Bug fixes and reliability improvements
- New chat platform bots (Discord, Telegram, XMPP)
- Better error handling when peers go offline mid-generation
- Mobile UI improvements
- Documentation and guides
- Performance tuning

### Submitting changes

1. Fork the repository
2. Create a feature branch: `git checkout -b my-feature`
3. Make your changes and test them
4. Push and open a pull request with a clear description of what you changed and why

## License

[MIT](LICENSE)
