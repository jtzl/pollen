import json
import time
from traceback import format_exc

import flask_sock
import hivemind
from petals.client.routing.sequence_manager import MissingBlocksError
from flask import request as http_request

from pollen.core import config
from pollen.infrastructure import speed_tracker
from pollen.features.chat import rag_pipeline
from pollen.features.chat.retrieval import rag_search
from pollen.core.extensions import sock
from pollen.features.chat.utils import safe_decode, strip_filler_phrases, could_match_fallback_prefix, safe_emit_len_for_citations, is_url_query
from pollen.features.chat.postprocessing import _filter_cited_sources, _strip_orphan_markers
from pollen.features.chat.chat_service import ChatService

logger = hivemind.get_logger(__file__)

# Models are injected by app.py after load_models() completes. flask_sock
# handlers run without a Flask app/request context, so we cannot use
# current_app here; a module-level setter provides the models mapping instead.
_models = None


def set_models(m):
    global _models
    _models = m


@sock.route("/api/v2/generate")
def ws_api_generate(ws):
    try:
        request = json.loads(ws.receive(timeout=config.STEP_TIMEOUT))
        assert request["type"] == "open_inference_session"
        model_name = request["model"]
        max_length = request["max_length"]
        logger.info(f"ws.generate.open(), {model_name=}, {max_length=}, {http_request.origin=}")

        model, tokenizer, backend_config = _models[model_name]
        if not backend_config.public_api and http_request.origin != f"{http_request.scheme}://{http_request.host}":
            raise ValueError(f"We do not provide public API for {model_name} due to license restrictions")

        with model.inference_session(max_length=max_length) as session:
            ws.send(json.dumps({"ok": True}))

            # ChatService owns the per-message generation body; the route keeps the
            # receive loop, the inference_session, and the multi-turn structure.
            service = ChatService(model, tokenizer, backend_config, config)

            while True:
                request = json.loads(ws.receive(timeout=config.STEP_TIMEOUT))
                assert request["type"] == "generate"
                for frame in service.generate_stream(request, session):
                    ws.send(json.dumps(frame))

            # ===== DISABLED: original inline streaming loop (superseded by ChatService above). =====
            # Kept verbatim for instant rollback. To restore: delete the while-loop just
            # above and strip the leading "# " from every line in this block.
#             rag_sources = []
# 
#             while True:
#                 request = json.loads(ws.receive(timeout=config.STEP_TIMEOUT))
#                 assert request["type"] == "generate"
#                 inputs = request.get("inputs") or None
#                 keep_urls = False  # set True below when the user asked for a URL
#                 # Short-circuit greetings: return a canned reply and wait for next ws message.
#                 if inputs is not None:
#                     _user_check = rag_pipeline.extract_user_message(inputs)
#                     if rag_pipeline.is_greeting(_user_check):
#                         greeting = rag_pipeline.random_greeting_response()
#                         logger.info(f"ws.generate greeting short-circuit -> {greeting!r}")
#                         ws.send(json.dumps({
#                             "ok": True, "outputs": greeting, "stop": True,
#                             "token_count": 0, "generated": 0,
#                         }))
#                         continue
# 
#                 # RAG: augment user message with web search if needed
#                 if inputs is not None:
#                     user_msg = rag_pipeline.extract_user_message(inputs)
#                     keep_urls = is_url_query(user_msg)
#                     if rag_pipeline.needs_search(user_msg):
#                         search_results = rag_search.search(user_msg)
#                         if search_results:
#                             inputs = rag_pipeline.augment_prompt_in_place(inputs, search_results)
#                             rag_sources = search_results
#                             logger.info(f"ws.generate RAG augmented with {len(rag_sources)} results")
#                 logger.info(f"ws.generate.step(), inputs={repr(inputs)}")
# 
#                 if inputs is not None:
#                     inputs = tokenizer(inputs, return_tensors="pt")["input_ids"].to(config.DEVICE)
#                     n_input_tokens = inputs.shape[1]
#                 else:
#                     n_input_tokens = 0
# 
#                 stop_sequence = request.get("stop_sequence")
#                 extra_stop_sequences = request.get("extra_stop_sequences")
#                 if extra_stop_sequences is not None:
#                     cont_token = tokenizer(stop_sequence, return_tensors="pt")["input_ids"].to(config.DEVICE)
#                     if cont_token.shape != (1, 1):
#                         raise ValueError("extra_stop_sequences require stop_sequence length to be exactly 1 token")
# 
#                 max_total_tokens = request.get("max_total_tokens", 512)
#                 # Quick-mode: cap generation length when the user message (text after
#                 # the last [INST] in the original request) starts with "quick:".
#                 _quick_portion = (request.get("inputs") or "").rsplit("[INST]", 1)[-1].strip().lower()
#                 if _quick_portion.startswith("quick:"):
#                     max_total_tokens = min(max_total_tokens, 100)
# 
#                 all_outputs = ""
#                 sent_filtered_len = 0
#                 delta_q = []
#                 total_generated = 0
#                 stop = False
#                 gen_start = time.time()
#                 while not stop:
#                     # HARD LIMIT: stop immediately if we've hit the token cap
#                     if total_generated >= max_total_tokens:
#                         stop = True
#                         hard_stop_msg = {
#                             "ok": True, "outputs": "", "stop": True,
#                             "token_count": 0, "generated": total_generated,
#                         }
#                         # Canonical cleaned text: strip filler, then remove any
#                         # orphaned [n] markers (no matching source) before display.
#                         _hard_final = strip_filler_phrases(all_outputs, max_citations=len(rag_sources), is_final=True, keep_urls=keep_urls)
#                         _hard_final = _strip_orphan_markers(_hard_final, len(rag_sources))
#                         hard_stop_msg["final_text"] = _hard_final
#                         if rag_sources:
#                             _cited = _filter_cited_sources(_hard_final, rag_sources)
#                             if _cited:
#                                 hard_stop_msg["rag_sources"] = [{"title": r["title"], "url": r["url"]} for r in _cited]
#                             rag_sources = []
#                         ws.send(json.dumps(hard_stop_msg))
#                         break
# 
#                     outputs = model.generate(
#                         inputs=inputs,
#                         do_sample=request.get("do_sample", False),
#                         temperature=request.get("temperature"),
#                         top_k=request.get("top_k"),
#                         top_p=request.get("top_p"),
#                         repetition_penalty=request.get("repetition_penalty"),
#                         max_length=request.get("max_length"),
#                         max_new_tokens=request.get("max_new_tokens"),
#                         no_repeat_ngram_size=request.get("no_repeat_ngram_size") or 3,
#                         session=session,
#                     )
#                     delta = outputs[0, n_input_tokens:].tolist()
#                     outputs = safe_decode(tokenizer, delta_q + delta)
#                     inputs = None
#                     n_input_tokens = 0
#                     total_generated += 1
#                     combined = all_outputs + outputs
#                     stop = stop_sequence is None or (
#                         "falcon-180B" not in model_name and combined.endswith(stop_sequence)
#                     )
#                     if extra_stop_sequences is not None:
#                         for seq in extra_stop_sequences:
#                             if seq in combined:
#                                 stop = True
#                                 # Truncate output at the stop sequence
#                                 idx = combined.find(seq)
#                                 truncated = combined[:idx]
#                                 outputs = truncated[len(all_outputs):]
#                                 combined = truncated
#                                 session.last_token_id = cont_token
#                                 break
#                     # HARD LIMIT check after generation
#                     if total_generated >= max_total_tokens:
#                         stop = True
#                     _min_floor = request.get("min_total_tokens", 120)
#                     if stop and total_generated < _min_floor and total_generated < max_total_tokens:
#                         _hit_extra = extra_stop_sequences is not None and any(
#                             seq in combined for seq in extra_stop_sequences)
#                         if not _hit_extra:
#                             stop = False
#                     if not stop and outputs[-10:].find("\ufffd") > -1:
#                         delta_q = delta_q + delta
#                         logger.info(f"ws.generate.append_retry(), all_outputs={repr(combined)}")
#                     else:
#                         all_outputs = combined
#                         token_count = len(delta_q + delta)
#                         delta_q = []
#                         # is_final=stop: only run the trailing-sentence/mid-word
#                         # repair on the final tick. Per-step trimming would
#                         # non-monotonically shrink the cumulative filtered text
#                         # and break the streaming delta computation below.
#                         filtered_all = strip_filler_phrases(all_outputs, max_citations=len(rag_sources), is_final=stop, keep_urls=keep_urls)
#                         # Hold streaming delta while filtered_all could still grow
#                         # into a known fallback-prefix match (e.g. "Based on the
#                         # search res" -> "Based on the search results provided,").
#                         # Without this, the prefix would land in an early delta
#                         # and a later strip-pass would shrink filtered_all but
#                         # the client would never see the retraction. Once the
#                         # text diverges from every fallback prefix or the final
#                         # tick arrives, emit normally.
#                         if not stop and could_match_fallback_prefix(filtered_all):
#                             filtered_delta = ""
#                         else:
#                             emit_len = len(filtered_all) if stop else safe_emit_len_for_citations(filtered_all)
#                             filtered_delta = filtered_all[sent_filtered_len:emit_len] if emit_len > sent_filtered_len else ""
#                             sent_filtered_len = max(sent_filtered_len, emit_len)
#                         logger.info(f"ws.generate.step(), all_outputs={repr(all_outputs)}, stop={stop}, generated={total_generated}/{max_total_tokens}")
#                         step_msg = {
#                             "ok": True, "outputs": filtered_delta, "stop": stop,
#                             "token_count": token_count, "generated": total_generated,
#                         }
#                         # On the final tick, filtered_all is the full cleaned canonical
#                         # text (is_final=stop ran the complete strip). Send it so the
#                         # client can replace its append-streamed text and pick up any
#                         # retroactive cleaning the per-step delta protocol can't convey.
#                         if stop:
#                             # Remove any [n] markers with no matching source so the
#                             # reader never sees an orphaned citation marker.
#                             filtered_all = _strip_orphan_markers(filtered_all, len(rag_sources))
#                             step_msg["final_text"] = filtered_all
#                         if stop and rag_sources:
#                             _cited = _filter_cited_sources(filtered_all, rag_sources)
#                             if _cited:
#                                 step_msg["rag_sources"] = [{"title": r["title"], "url": r["url"]} for r in _cited]
#                             rag_sources = []
#                         ws.send(json.dumps(step_msg))
# 
#                 # Record generation speed for telemetry
#                 if total_generated > 0:
#                     speed_tracker.record_generation(total_generated, time.time() - gen_start)
            # ===== END DISABLED =====

    except flask_sock.ConnectionClosed:
        pass
    except MissingBlocksError:
        # Cluster has no node currently serving the requested blocks.
        # Treat as a transient infrastructure outage so the frontend
        # can show a friendly "cluster restarting" message instead of
        # a generic generation error.
        logger.warning("ws.generate cluster_unavailable:", exc_info=True)
        ws.send(json.dumps({"ok": False, "code": "cluster_unavailable", "traceback": format_exc()}))
    except Exception:
        logger.warning("ws.generate failed:", exc_info=True)
        ws.send(json.dumps({"ok": False, "traceback": format_exc()}))
    finally:
        logger.info(f"ws.generate.close()")
