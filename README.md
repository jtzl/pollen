# Pollen

A modern, real-time chat interface for large language models running on the [Petals](https://petals.dev) decentralized network. Pollen provides a sleek, ChatGPT-style UI that connects to any model served through Petals' distributed inference.

## Features

- **Real-time streaming** — token-by-token generation via WebSocket
- **Beautiful dark UI** — modern chat interface with markdown rendering and syntax highlighting
- **Multi-conversation** — sidebar with chat history and easy switching
- **Adjustable parameters** — temperature, top-p, and max tokens via settings panel
- **Network dashboard** — live view of peer connectivity, block coverage, and throughput
- **Code blocks** — syntax highlighting with one-click copy
- **REST & WebSocket APIs** — programmatic access for integrations
- **Fully configurable** — model, branding, welcome message, and more via `.env`
- **Responsive design** — works on desktop and mobile

## Requirements

- Python 3.8+
- CUDA-capable GPU (recommended) or CPU
- Access to a [Petals](https://petals.dev) DHT network with peers serving your chosen model
- 4 GB+ RAM

## Installation

1. **Clone the repository:**

   ```bash
   git clone https://github.com/nalamk/pollen.git
   cd pollen
   ```

2. **Create a virtual environment:**

   ```bash
   python -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

4. **Configure your environment:**

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and set `DHT_INITIAL_PEERS` to your Petals network peers. See [Configuration](#configuration) for all options.

5. **Start the server:**

   ```bash
   gunicorn app:app --bind 0.0.0.0:5000 --worker-class gthread --threads 10 --timeout 600
   ```

6. **Open** `http://localhost:5000` in your browser.

## Configuration

Copy `.env.example` to `.env` and customize the values:

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_NAME` | `Pollen` | Application name displayed in the UI |
| `PORT` | `5000` | Server port |
| `MODEL_REPO` | `mistralai/Mixtral-8x7B-Instruct-v0.1` | HuggingFace model repository |
| `MODEL_DISPLAY_NAME` | `Mixtral 8x7B Instruct` | Model name shown in the UI |
| `MODEL_BADGE` | `8x7B` | Short label badge next to the app name |
| `DEFAULT_MAX_TOKENS` | `500` | Default maximum tokens per response |
| `DHT_INITIAL_PEERS` | *(required)* | Comma-separated Petals DHT peer multiaddresses |
| `SHOW_PETALS_BRANDING` | `true` | Show or hide Petals branding in the UI |
| `WELCOME_MESSAGE` | *(see .env.example)* | Welcome screen description text |
| `MODEL_CARD_URL` | *(auto from MODEL_REPO)* | Link to the model card |
| `MODEL_LICENSE_URL` | *(auto from MODEL_REPO)* | Link to the model license |

## Usage

### Web UI

Open `http://localhost:5000` in your browser. Type a message and press **Enter** to start chatting. Use **Shift+Enter** for a new line.

- Click the **gear icon** to adjust temperature, top-p, and max tokens.
- Click the **network icon** to view live peer status and block coverage.
- Use the **sidebar** to manage multiple conversations.

### API

**WebSocket** — `ws://localhost:5000/api/v2/generate`

Real-time streaming generation. Send JSON messages to open a session and generate tokens.

**REST** — `POST /api/v1/generate`

Single-shot generation with parameters: `model`, `inputs`, `temperature`, `top_p`, `max_new_tokens`, etc.

**Status** — `GET /api/status`

Returns network health: peer count, block coverage, throughput, and uptime.

## License

[MIT](LICENSE)
