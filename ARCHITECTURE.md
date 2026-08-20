# Pollen Architecture

Pollen is a self-hosted chat interface for a decentralized Mixtral-8x7B model served over a Petals cluster, with a retrieval-augmented generation (RAG) pipeline for grounding answers in web and curated sources.

This document describes the current architecture and its known limitations. The codebase has completed its migration to a layered, feature-based package served by a single FastAPI process; this document is the reference for contributors.

## Current architecture

The backend is a single FastAPI application (uvicorn, systemd unit pollen-fastapi.service, bound to loopback on 127.0.0.1:5001) that holds the Petals model client and serves every route: the streaming chat WebSocket, the one-shot HTTP generate, status and curated-sources, the image routes, the main page, and the static assets. The Python modules live in a pollen/ package organized by layer and feature (see Package layout below); the process entry points remain at the repository root. The earlier Flask/gunicorn process has been retired.

### Request paths

- Web chat streaming: browser WebSocket to /api/v2/generate, served by an async route in fastapi_app.py. It owns the socket receive loop and the Petals inference session; the per-message generation body lives in ChatService.generate_stream (pollen/features/chat/chat_service.py), which yields frames the route serializes and sends. See Streaming below for how the blocking generator is bridged to the async socket.
- HTTP generate one-shot: /api/v1/generate in fastapi_app.py, via ChatService.generate_once. Used by the mobile client and by the IRC and Matrix bots.
- Status and sources: /api/status and /api/curated-sources, served by fastapi_app.py.
- Images: /api/generate-image, /api/image-status and /static/generated/ served by fastapi_app.py; generation itself lives in pollen/infrastructure/image_gen.py.
- Main page and static assets: the pre-rendered index page and /static/* served by fastapi_app.py; the template is rendered once at startup.
- Bots: irc_bot.py and matrix_bot.py are standalone top-level processes. They do not create their own Petals client; they call /api/v1/generate over HTTP on the FastAPI process (127.0.0.1:5001) directly, bypassing the proxy.

### The RAG pipeline

1. Retrieval, in pollen/features/chat/retrieval/: rag_search.py together with rag_common, rag_curated, rag_ranking, rag_content, rag_fetchers and rag_retrieval. Together they fetch curated domains and sources, rank and filter results, fetch and score page text, and provide web search plus a direct Wikipedia fetch.
2. Prompt building, in pollen/features/chat/rag_pipeline.py: classifies the query, decides whether search is needed, fits the prompt to the token budget, formats context, and builds the augmented prompt.
3. Inference, in pollen/features/chat/chat_service.py: ChatService.generate_stream runs the Petals generation loop for the streaming path (stop sequences, a minimum-length floor and citation filtering); ChatService.generate_once serves the one-shot path.
4. Post-processing, in pollen/features/chat/postprocessing.py: a chain of cleanup steps orchestrated by strip_filler_phrases that removes filler, hedging, malformed URLs, and duplicate content, and normalizes multi-section formatting. pollen/features/chat/utils.py re-exports these plus model loading from pollen/infrastructure/model_loader.py.

### State

There is no database. All state is in-process or in the browser. Conversation history lives only in the browser for the page's lifetime; the server does not log or store queries.

The one exception is generation telemetry. speed_tracker (pollen/infrastructure/speed_tracker.py) records a rolling tokens-per-second average and the process start time, which /api/status reports. It still writes an atomically-updated JSON state file (a temporary file renamed into place, so a reader never sees a partial write). With a single process the writer and reader are now the same process, so the file hop is vestigial; it is retained because it is harmless and keeps working if a second reader process is ever reintroduced.

## Streaming

The /api/v2/generate WebSocket streams tokens as they are produced. ChatService.generate_stream is a synchronous, blocking generator -- each step waits on the swarm, roughly three hundred milliseconds per token. To keep the async event loop free, the route runs that generator in a threadpool executor and bridges each yielded frame onto an asyncio.Queue via call_soon_threadsafe; the async handler awaits the queue and sends each frame with websocket.send_text as it arrives. Frames therefore stream incrementally rather than being collected and flushed at the end.

The core generation logic (ChatService.generate_stream) is framework-agnostic and unchanged from the earlier flask_sock implementation; only the transport shell differs. One constraint governs the bridge: Petals InferenceSession holds no internal locks, so exactly one thread may drive a session at a time. The route satisfies this by running one generator per connection and awaiting each turn before receiving the next message, so a session is never touched by two threads concurrently.

## Package layout

The code is organized by layer and by feature under a single pollen/ package. The process entry points stay at the repository root, outside the package.

    pollen/
      core/              config.py, data_structures.py, extensions.py
      infrastructure/    model_loader.py, image_gen.py, speed_tracker.py
      features/
        chat/            chat_service.py, rag_pipeline.py, postprocessing.py,
                         utils.py, websocket_api.py, http_api.py
          retrieval/     rag_search.py, rag_common.py, rag_curated.py,
                         rag_content.py, rag_fetchers.py, rag_ranking.py, rag_retrieval.py
        images/          image_api.py
        status/          status_api.py

    (repository root -- entry points)
      fastapi_app.py     the FastAPI app (uvicorn fastapi_app:app): loads the model
                         and serves every route
      irc_bot.py         IRC bot process
      matrix_bot.py      Matrix bot process
      views.py           index page rendering
      app.py             retired Flask WSGI app, kept in git for reference/rollback

core holds configuration, shared dataclasses, and shared extensions. infrastructure holds the things treated as external services: Petals model loading, image generation, and telemetry. features holds the vertical slices: chat, with its retrieval subpackage for the RAG providers, plus images and status.

A note on the feature route modules: the Flask blueprint and flask_sock modules under features (http_api.py, websocket_api.py, status_api.py, image_api.py) date from the Flask deployment. Their route handlers are no longer the serving path -- fastapi_app.py now serves those routes directly -- but the service and helper logic they were built around (ChatService, rag_pipeline, postprocessing, utils, image_gen) is shared and live. The blueprint modules are retained, alongside app.py, as the rollback path.

## Deployment

Pollen runs as a single process behind Caddy.

    Caddy (443)  ->  FastAPI  127.0.0.1:5001   (everything)

The FastAPI process (uvicorn, pollen-fastapi.service) loads the Petals model client at startup. The load is gated behind an environment variable so the same code can also run model-free: POLLEN_LOAD_MODEL=1 triggers it, alongside the offline Hugging Face environment (HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1, and HF_HOME/TRANSFORMERS_CACHE pointing at the local cache) so weights come from disk rather than the network, CUDA_VISIBLE_DEVICES empty to keep the client on CPU, and DHT_INITIAL_PEERS naming the local bootstrap peer. The client holds the token embeddings, LM head and tokenizer locally (~1.5 GB resident); only the transformer blocks run remotely on the swarm. Startup therefore takes tens of seconds rather than the sub-second of the earlier model-free process.

Caddy proxies all paths, including the /api/v2/generate WebSocket, to 127.0.0.1:5001; it upgrades WebSocket connections natively, so streaming needs no special configuration. 127.0.0.1 (not localhost) is used because uvicorn binds IPv4 only.

A health watchdog (watchdog.sh, run every five minutes by cron) checks /api/status on the FastAPI process and restarts pollen-fastapi if it is unresponsive; it also independently checks the local Petals block server (petals-server) and the DHT.

The /api/status route reaches the swarm to report coverage. It could read the loaded model's sequence manager, but the model-free implementation is retained: it builds its own client-mode hivemind DHT from the configured initial peers and derives the block UIDs by naming convention (the DHT prefix -- the model repository name with dots replaced by hyphens -- joined to each block index), reading the block count from the model config in the offline cache. From there it calls the same get_remote_module_infos and compute_spans, so the response is unchanged.

## Known limitations

- The Flask blueprint and flask_sock modules (http_api.py, websocket_api.py, status_api.py, image_api.py) and app.py are retained but unused by the running server; they are the rollback path, not live code.
- A single process holds the model and serves all traffic, so a restart (deploy or crash) drops chat for the tens of seconds the model takes to reload. There is one model client and no hot standby.
- Generation is synchronous per session (Petals exposes no async API); the threadpool bridge keeps the event loop free but does not raise the swarm-bounded concurrency ceiling. See Streaming.
- No swap is configured on the host. The ~1.5 GB model client sits comfortably within RAM, but there is no cushion for a spike.

## Design principles

The load-bearing rule: route handlers do not call the model or the RAG pipeline directly. They call a service such as ChatService that orchestrates retrieval, prompt building, inference, and post-processing. This is the seam where future concerns such as authentication and usage limits would be enforced.

The inference engine is treated as an external service behind an interface, which reflects how Petals already runs as a separate cluster.

## Migration history

The backend reached its current shape through a sequence of completed steps, each of which left the application running:

- Focused modules and a service layer: the old utils module was split into model_loader and postprocessing, the old rag_search into six responsibility modules, and the streaming generation body was extracted into ChatService.
- The pollen/ package: all modules moved into a layered, feature-based package (core, infrastructure, features), and the old app-to-routes circular import was removed -- blueprints for the plain HTTP routes plus a shared extensions module for the flask_sock instance, with the models mapping injected at startup.
- Non-streaming routes to FastAPI: status, curated-sources, the image routes, the main page and static assets moved to a then-model-free FastAPI process, byte-for-byte compatible with the Flask responses.
- Streaming and one-shot to FastAPI: the /api/v2/generate WebSocket (via the threadpool bridge above) and /api/v1/generate (via ChatService.generate_once) moved to FastAPI, which now loads the model.
- Flask retired: gunicorn / app:app (the petals-chat unit) was stopped, disabled and removed; the bots were repointed to the FastAPI process in the same cutover; the watchdog was repointed to guard it. The Flask code remains in git for rollback.

The result is a single framework and a single serving process.

On the streaming migration specifically: it was done carefully because the streaming loop is the most delicate code in the system -- stop sequences, the minimum-length floor, the multibyte retry guard, the fallback-prefix hold and the streaming delta arithmetic all interact. The generation logic was reused unchanged; only the transport was rewritten, and the one-thread-per-session invariant was preserved (see Streaming). Because Petals is synchronous and every session already serializes through a single internal event-loop thread per process, the move did not make generation asynchronous -- a generation is roughly ninety-nine percent I/O wait -- so the benefit is a single framework, not higher concurrency. Revisit the concurrency ceiling only if demand ever exceeds the swarm's capacity, or if Petals gains a genuine async API.

Accounts, usage and billing (a persistence layer, auth, usage limits) were considered and deliberately not built: they are in direct tension with the no-server-persistence privacy property below, and are contingent on product direction. The service-layer seam (ChatService) is where such concerns would attach if that ever changes.

## Notes for contributors

- The code lives in the pollen/ package, organized by layer (core, infrastructure) and feature (features/chat with its retrieval subpackage, features/images, features/status). The entry points -- fastapi_app.py, irc_bot.py, matrix_bot.py -- and views.py stay at the repository root; uvicorn imports fastapi_app:app. app.py is the retired Flask entry point, kept for rollback.
- The inference engine (the Petals cluster) is external. The application depends on it through an interface, not through direct calls scattered across handlers.
- Web search depends on a residential node; when it is offline, retrieval falls back to Wikipedia only.
- No query logging or conversation persistence on the server is a deliberate privacy property. Any future persistence must preserve or explicitly document changes to it.
- One process serves everything on 127.0.0.1:5001, behind Caddy. The bots talk to /api/v1/generate on that process directly; any change to /api/v1/generate must account for them.
