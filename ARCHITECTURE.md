# Pollen Architecture

Pollen is a self-hosted chat interface for a decentralized Mixtral-8x7B model served over a Petals cluster, with a retrieval-augmented generation (RAG) pipeline for grounding answers in web and curated sources.

This document describes the current architecture and its known limitations. The codebase has completed its migration to a layered, feature-based package; this document is the reference for contributors.

## Current architecture

The backend is a Flask application, fronted by a second, model-free FastAPI process that serves the non-inference routes. The Python modules live in a pollen/ package organized by layer and feature (see Package layout below); only the process entry points remain at the repository root. Flask routes are registered explicitly: the API modules expose Flask blueprints, and app.py imports them and calls app.register_blueprint for each. The old circular import, where modules bound their routes by importing back from app, has been removed.

### Request paths

- Web chat streaming: browser WebSocket to /api/v2/generate, handled in pollen/features/chat/websocket_api.py. The route owns the socket receive loop and the Petals inference session; the per-message generation body lives in ChatService.generate_stream (pollen/features/chat/chat_service.py), which yields frames the route serializes and sends.
- HTTP generate one-shot: /api/v1/generate in pollen/features/chat/http_api.py, used by the mobile client and by the IRC and Matrix bots. Still on Flask.
- Status and sources: /api/status and /api/curated-sources, now served by the FastAPI process. The Flask implementations remain in pollen/features/status/status_api.py but no longer receive traffic.
- Images: /api/generate-image, /api/image-status and /static/generated/ now served by the FastAPI process; generation itself lives in pollen/infrastructure/image_gen.py.
- Bots: irc_bot.py and matrix_bot.py are standalone top-level processes. They do not create their own Petals client; they call /api/v1/generate over HTTP on port 5000 directly, bypassing the proxy.

### The RAG pipeline

1. Retrieval, in pollen/features/chat/retrieval/: rag_search.py together with rag_common, rag_curated, rag_ranking, rag_content, rag_fetchers and rag_retrieval. Together they fetch curated domains and sources, rank and filter results, fetch and score page text, and provide web search plus a direct Wikipedia fetch.
2. Prompt building, in pollen/features/chat/rag_pipeline.py: classifies the query, decides whether search is needed, fits the prompt to the token budget, formats context, and builds the augmented prompt.
3. Inference, in pollen/features/chat/chat_service.py: ChatService.generate_stream runs the Petals generation loop for the streaming path, applying stop sequences, a minimum-length floor and citation filtering. http_api.py still runs its own one-shot generation.
4. Post-processing, in pollen/features/chat/postprocessing.py: a chain of cleanup steps orchestrated by strip_filler_phrases that removes filler, hedging, malformed URLs, and duplicate content, and normalizes multi-section formatting. pollen/features/chat/utils.py re-exports these plus model loading from pollen/infrastructure/model_loader.py.

### State

There is no database. All state is in-process or in the browser. Conversation history lives only in the browser for the page's lifetime; the server does not log or store queries.

The one exception is generation telemetry. speed_tracker state (pollen/infrastructure/speed_tracker.py) is per-process, so the Flask worker publishes its rolling tokens-per-second average and its start time to an atomically written state file that the FastAPI process reads when serving /api/status. See the deployment section below.

## Package layout

The code is organized by layer and by feature under a single pollen/ package. The four process entry points stay at the repository root, outside the package, so the deployment targets are unchanged: gunicorn serves app:app and uvicorn serves fastapi_app:app, both from the repository root.

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

    (repository root -- entry points, intentionally not in the package)
      app.py             Flask WSGI app (gunicorn app:app): builds the app,
                         registers the feature blueprints, binds the WebSocket
      fastapi_app.py     model-free FastAPI app (uvicorn fastapi_app:app)
      irc_bot.py         IRC bot process
      matrix_bot.py      Matrix bot process
      views.py           index page rendering

core holds configuration, shared dataclasses, and shared Flask extensions. infrastructure holds the things treated as external services: Petals model loading, image generation, and telemetry. features holds the vertical slices: chat, with its retrieval subpackage for the RAG providers, plus images and status. Each feature owns its own routes and logic. The entry points stay at the top level so the gunicorn (app:app) and uvicorn (fastapi_app:app) targets did not change when the modules moved.

### How the circular import was broken

The old app-to-routes circular import is gone. The three plain HTTP route modules -- image_api, status_api and http_api -- each expose a Flask blueprint, and app.py registers them with app.register_blueprint; none of them import back from app. The two that need the models mapping read it from current_app.config at request time rather than importing a module global.

The WebSocket route is the exception, because a flask_sock handler runs without a Flask application context and so cannot read current_app. Two pieces handle it. First, the shared flask_sock Sock instance now lives in pollen/core/extensions.py, so both app.py and websocket_api.py import it from there instead of from app. Second, websocket_api exposes a set_models() injector: app.py imports websocket_api so its @sock.route registers on the Sock, calls set_models(models) to hand it the mapping once at startup, then binds the Sock to the app with sock.init_app(app). Nothing back-imports app, so load order is no longer fragile.

## Deployment

Pollen currently runs as two processes behind Caddy, which routes by path matcher.

    Caddy (443)
      /api/status            -> FastAPI  127.0.0.1:5001
      /api/curated-sources   -> FastAPI  127.0.0.1:5001
      /api/generate-image    -> FastAPI  127.0.0.1:5001
      /api/image-status      -> FastAPI  127.0.0.1:5001
      /static/generated/*    -> FastAPI  127.0.0.1:5001
      everything else        -> Flask    127.0.0.1:5000

Flask and gunicorn on port 5000 serve the chat UI, the /api/v2/generate WebSocket, /api/v1/generate, and all remaining static assets. Only the /static/generated/ prefix moves to FastAPI; every other /static path stays on Flask.

The FastAPI process (uvicorn, systemd unit pollen-fastapi.service, bound to loopback only) loads no model. It never imports app.py, so it never triggers load_models, and it starts in under a second rather than the minute the Flask worker needs.

Serving /api/status without a model required replacing the one thing that route used the model for. The Flask implementation reaches the Petals sequence manager through the loaded model purely to obtain two values: a DHT handle and the list of block UIDs. Neither needs model weights. The FastAPI implementation instead builds its own client-mode hivemind DHT from the configured initial peers, and derives the block UIDs by naming convention, joining the DHT prefix (the model repository name with dots replaced by hyphens) to each block index. The block count comes from the model config read out of the Hugging Face cache offline. From there it calls the same get_remote_module_infos and compute_spans as the Flask route, so the response is byte-identical.

Because speed_tracker keeps its samples and start time in memory, per process, the FastAPI process cannot see the Flask worker's numbers directly. The writer publishes them to a small JSON state file, written to a temporary file and renamed into place so a reader can never observe a partial write. If the file is missing or unreadable the reader falls back to its own in-process values rather than failing.

## Known limitations

- Two processes now serve one site, so shared state (currently only generation telemetry) has to be passed through a file. Any further shared state will need a real mechanism.
- pollen/features/status/status_api.py and pollen/features/images/image_api.py still exist in the Flask app and duplicate logic now served by FastAPI. They are dormant but not yet removed.
- http_api.py still calls inference and RAG functions directly rather than through ChatService, so the one-shot path has no service layer.
- Synchronous Flask and WSGI holds a worker thread for the full duration of each generation.

## Design principles

The load-bearing rule: route handlers do not call the model or the RAG pipeline directly. They call a service such as ChatService that orchestrates retrieval, prompt building, inference, and post-processing. This is the seam where future concerns such as authentication and usage limits are enforced.

The inference engine is treated as an external service behind an interface, which reflects how Petals already runs as a separate cluster.

## Migration status

The migration to the layered, feature-based structure is complete. The remaining phases are product-contingent.

- Phase 1, complete: module splits and a service layer. The old utils module was split into model_loader and postprocessing, the old rag_search into six responsibility modules, and the streaming generation body moved into ChatService.
- Phase 2, complete as scoped: the non-streaming paths that can run without a model migrated to FastAPI. /api/status, /api/curated-sources, /api/generate-image, /api/image-status and /static/generated/ now serve from the model-free FastAPI process, with Caddy routing by path. /api/v1/generate deliberately stays on Flask -- it is the one non-streaming route that genuinely needs the loaded model -- see below.
- Package restructure, complete: all modules moved into the pollen/ package (core, infrastructure, features) and the app-to-routes circular import was removed. The entry points, the deployment targets and the systemd units did not change.
- Phase 3, investigated and deliberately deferred: migrating the streaming WebSocket path to FastAPI. See the reasoning below.
- Phase 4: add accounts, usage, and billing, with a persistence layer behind repository interfaces. Contingent on product direction, and in direct tension with the no-server-persistence privacy property; any such work must preserve or explicitly document a change to it.
- Phase 5: retire Flask and cut over. The bot entry points must continue to work.

### Why Phase 3 is deferred

This was investigated rather than skipped, and the conclusion was that the cost is high and the benefit is close to zero.

Petals exposes no public async API. Both model.generate and session.step are synchronous, and all network I/O is already funnelled through a single internal asyncio loop thread (RemoteExpertWorker), which every session shares within a process. Moving the WebSocket path to FastAPI would therefore not make generation asynchronous. It would still have to run in a threadpool, and it would still serialize through that same Petals event loop.

The threads generation occupies are also cheap. Measured, a generation is roughly ninety-nine percent I/O wait and one percent CPU: about three hundred milliseconds per token waiting on the swarm, against a few milliseconds of post-processing. Occupying a thread for that is not the bottleneck.

Against that, the streaming loop is the most delicate code in the system: stop sequences, the minimum-length floor, the multibyte retry guard, the fallback-prefix hold and the streaming delta arithmetic all interact. Rewriting it would risk real regressions in order to raise a concurrency ceiling of roughly ten simultaneous generations that the swarm's own throughput already makes moot.

One further constraint for whoever revisits this: InferenceSession holds no internal locks, so any threadpool bridge must guarantee exactly one thread per session. Sharing a session across threads is unsafe.

Revisit if concurrency demand ever exceeds the swarm's capacity, or if Petals gains a genuine async API.

### Why /api/v1/generate has not moved

It is the only non-streaming route that genuinely requires the loaded model object. It calls model.generate directly on the Petals client, which holds the token embeddings, the LM head and the tokenizer locally; only the transformer blocks run remotely on the swarm. So, unlike /api/status -- which reaches the swarm by building a bare client-mode DHT and needs no model at all -- generation cannot be done from the DHT alone.

Serving it from the FastAPI process would therefore mean loading a second full Petals client there, roughly 1.5 GB of resident memory on a host that has little free RAM and no swap, for zero functional benefit: it is a one-shot request/response route with nothing to gain from async.

It would also require repointing the IRC and Matrix bots, which POST to it directly on port 5000, bypassing Caddy entirely. Moving the route in the proxy would not move their traffic, and moving it in the application would silently break them.

So it should move only as part of a future full Flask retirement, when the bots are repointed in the same cutover. Phase 2 is marked done as scoped rather than loading a redundant model client to check a box.

## Notes for contributors

- The code lives in the pollen/ package, organized by layer (core, infrastructure) and feature (features/chat with its retrieval subpackage, features/images, features/status). The four entry points -- app.py, fastapi_app.py, irc_bot.py, matrix_bot.py -- and views.py stay at the repository root; gunicorn imports app:app and uvicorn imports fastapi_app:app from there.
- The inference engine (the Petals cluster) is external. The application depends on it through an interface, not through direct calls scattered across handlers.
- Web search depends on a residential node; when it is offline, retrieval falls back to Wikipedia only.
- No query logging or conversation persistence on the server is a deliberate privacy property. Any future persistence must preserve or explicitly document changes to it.
- Two processes now serve the site. When changing a route, check the Caddyfile to see which process actually receives it.
- The bots bypass the proxy and talk to port 5000 directly. Any change to /api/v1/generate must account for them.
