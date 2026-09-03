"""ChatService: transport-agnostic orchestration for the RAG + inference pipeline.

Extracted from websocket_api.ws_api_generate (streaming) and
http_api.http_api_generate (one-shot) so route handlers can become thin:

    for frame in service.generate_stream(req, session):
        ws.send(json.dumps(frame))

    return jsonify(service.generate_once(req))

The loop logic is moved verbatim; the only changes are mechanical:
model/tokenizer/config come from `self`, and `ws.send(json.dumps(msg))`
becomes `yield msg` (the caller serializes and sends). The inference_session
is owned by the caller and passed in; this module never opens or closes it.
"""
import time
from traceback import format_exc

import hivemind
from petals.client.routing.sequence_manager import MissingBlocksError

from pollen.features.chat import rag_pipeline
from pollen.features.chat.retrieval import rag_search
from pollen.infrastructure import speed_tracker
from pollen.features.chat.utils import (
    safe_decode,
    strip_filler_phrases,
    could_match_fallback_prefix,
    safe_emit_len_for_citations,
    is_url_query,
)
from pollen.features.chat.postprocessing import _filter_cited_sources, _strip_orphan_markers

logger = hivemind.get_logger(__file__)


class ChatService:
    def __init__(self, model, tokenizer, backend_config, config):
        self.model = model
        self.tokenizer = tokenizer
        self.backend_config = backend_config
        self.config = config
        # Configured generation defaults for this model. Kept so request
        # parameters can fall back to config rather than hardcoded literals -
        # important for values like no_repeat_ngram_size=0, where a falsy
        # default would otherwise be silently replaced.
        self.generation_params = {}
        for _family in config.MODEL_FAMILIES.values():
            for _model_config in _family:
                if _model_config.backend.key == backend_config.key:
                    self.generation_params = dict(_model_config.chat.generation_params or {})
                    break
        # model_name is used only by the falcon-180B stop-sequence guard below.
        # The constructor signature is fixed, so derive it from the backend key
        # (the same value used as the models[] dict key in the original handler).
        self.model_name = backend_config.key

    def _gen_param(self, request, name, fallback=None):
        """Resolve a generation parameter.

        An explicitly supplied request value wins - including falsy ones like
        0 - hence the `is not None` checks rather than truthiness. Otherwise
        fall back to the model's configured default, then to `fallback`.
        """
        value = request.get(name)
        if value is not None:
            return value
        value = self.generation_params.get(name)
        if value is not None:
            return value
        return fallback

    def generate_stream(self, request, session):
        """Generator owning one message's streaming generation. Yields the
        dicts the original ws_api_generate sent via ws.send(json.dumps(...)).
        The caller owns the inference_session and passes it in as `session`."""
        keep_urls = False  # set True below when the user asked for a URL
        rag_sources = []
        # Effective sampling temperature; may drop to FACTUAL_TEMPERATURE for a
        # factual query below (unless the caller set a non-default temperature).
        _eff_temp = request.get("temperature")

        inputs = request.get("inputs") or None
        # Short-circuit greetings: return a canned reply.
        if inputs is not None:
            _user_check = rag_pipeline.extract_user_message(inputs)
            if rag_pipeline.is_greeting(_user_check):
                greeting = rag_pipeline.random_greeting_response()
                logger.info(f"ws.generate greeting short-circuit -> {greeting!r}")
                yield {
                    "ok": True, "outputs": greeting, "stop": True,
                    "token_count": 0, "generated": 0,
                }
                return

        # RAG: augment user message with web search if needed
        if inputs is not None:
            user_msg = rag_pipeline.extract_user_message(inputs)
            keep_urls = is_url_query(user_msg)
            # Per-type temperature: factual queries generate cooler for better
            # grounding, unless the caller explicitly set a non-default value.
            if (_eff_temp is None or _eff_temp == self.config.DEFAULT_TEMPERATURE) \
                    and rag_pipeline.classify_query(user_msg) == "factual":
                _eff_temp = self.config.FACTUAL_TEMPERATURE
            if rag_pipeline.needs_search(user_msg):
                search_results = rag_search.search(user_msg)
                if search_results:
                    inputs = rag_pipeline.augment_prompt_in_place(inputs, search_results)
                    rag_sources = search_results
                    logger.info(f"ws.generate RAG augmented with {len(rag_sources)} results")
        logger.info(f"ws.generate.step(), inputs={repr(inputs)}")

        if inputs is not None:
            inputs = self.tokenizer(inputs, return_tensors="pt")["input_ids"].to(self.config.DEVICE)
            n_input_tokens = inputs.shape[1]
        else:
            n_input_tokens = 0

        stop_sequence = request.get("stop_sequence")
        extra_stop_sequences = request.get("extra_stop_sequences")
        if extra_stop_sequences is not None:
            cont_token = self.tokenizer(stop_sequence, return_tensors="pt")["input_ids"].to(self.config.DEVICE)
            if cont_token.shape != (1, 1):
                raise ValueError("extra_stop_sequences require stop_sequence length to be exactly 1 token")

        max_total_tokens = request.get("max_total_tokens", 512)
        # Quick-mode: cap generation length when the user message (text after
        # the last [INST] in the original request) starts with "quick:".
        _quick_portion = (request.get("inputs") or "").rsplit("[INST]", 1)[-1].strip().lower()
        if _quick_portion.startswith("quick:"):
            max_total_tokens = min(max_total_tokens, 100)

        all_outputs = ""
        sent_filtered_len = 0
        sent_text = ""  # exact text the client has accumulated so far
        delta_q = []
        total_generated = 0
        stop = False
        gen_start = time.time()
        while not stop:
            # HARD LIMIT: stop immediately if we've hit the token cap
            if total_generated >= max_total_tokens:
                stop = True
                hard_stop_msg = {
                    "ok": True, "outputs": "", "stop": True,
                    "token_count": 0, "generated": total_generated,
                }
                # Canonical cleaned text: strip filler, then remove any
                # orphaned [n] markers (no matching source) before display.
                _hard_final = strip_filler_phrases(all_outputs, max_citations=len(rag_sources), is_final=True, keep_urls=keep_urls)
                _hard_final = _strip_orphan_markers(_hard_final, len(rag_sources))
                hard_stop_msg["final_text"] = _hard_final
                if rag_sources:
                    _cited = _filter_cited_sources(_hard_final, rag_sources)
                    if _cited:
                        hard_stop_msg["rag_sources"] = [{"title": r["title"], "url": r["url"], "authority": r.get("authority", 0)} for r in _cited]
                    rag_sources = []
                yield hard_stop_msg
                break

            outputs = self.model.generate(
                inputs=inputs,
                do_sample=request.get("do_sample", False),
                temperature=_eff_temp,
                top_k=request.get("top_k"),
                top_p=request.get("top_p"),
                repetition_penalty=request.get("repetition_penalty"),
                max_length=request.get("max_length"),
                max_new_tokens=request.get("max_new_tokens"),
                no_repeat_ngram_size=self._gen_param(request, "no_repeat_ngram_size", 0),
                session=session,
            )
            delta = outputs[0, n_input_tokens:].tolist()
            outputs = safe_decode(self.tokenizer, delta_q + delta)
            inputs = None
            n_input_tokens = 0
            total_generated += 1
            combined = all_outputs + outputs
            stop = stop_sequence is None or (
                "falcon-180B" not in self.model_name and combined.endswith(stop_sequence)
            )
            if extra_stop_sequences is not None:
                for seq in extra_stop_sequences:
                    if seq in combined:
                        stop = True
                        # Truncate output at the stop sequence
                        idx = combined.find(seq)
                        truncated = combined[:idx]
                        outputs = truncated[len(all_outputs):]
                        combined = truncated
                        session.last_token_id = cont_token
                        break
            # HARD LIMIT check after generation
            if total_generated >= max_total_tokens:
                stop = True
            _min_floor = request.get("min_total_tokens", 120)
            if stop and total_generated < _min_floor and total_generated < max_total_tokens:
                _hit_extra = extra_stop_sequences is not None and any(
                    seq in combined for seq in extra_stop_sequences)
                if not _hit_extra:
                    stop = False
            if not stop and outputs[-10:].find("�") > -1:
                delta_q = delta_q + delta
                logger.info(f"ws.generate.append_retry(), all_outputs={repr(combined)}")
            else:
                all_outputs = combined
                token_count = len(delta_q + delta)
                delta_q = []
                # is_final=stop: only run the trailing-sentence/mid-word
                # repair on the final tick. Per-step trimming would
                # non-monotonically shrink the cumulative filtered text
                # and break the streaming delta computation below.
                filtered_all = strip_filler_phrases(all_outputs, max_citations=len(rag_sources), is_final=stop, keep_urls=keep_urls)
                # Hold streaming delta while filtered_all could still grow
                # into a known fallback-prefix match (e.g. "Based on the
                # search res" -> "Based on the search results provided,").
                # Without this, the prefix would land in an early delta
                # and a later strip-pass would shrink filtered_all but
                # the client would never see the retraction. Once the
                # text diverges from every fallback prefix or the final
                # tick arrives, emit normally.
                resync_text = None
                if not stop and could_match_fallback_prefix(filtered_all):
                    filtered_delta = ""
                else:
                    emit_len = len(filtered_all) if stop else safe_emit_len_for_citations(filtered_all)
                    # Desync guard. filtered_all is recomputed from scratch every
                    # tick, so a retroactive strip (filler / meta-sentence / hedge
                    # removal) can shorten it or rewrite an already-sent prefix.
                    # sent_filtered_len only ever grows, so slicing against a
                    # shifted string silently swallows characters - and once it
                    # points past the end, every later delta is empty and the
                    # stream stalls. When the canonical text no longer starts with
                    # what the client actually has, resend the whole thing with
                    # replace=True rather than emitting a broken delta.
                    if not stop and not filtered_all.startswith(sent_text):
                        resync_text = filtered_all[:emit_len]
                        filtered_delta = ""
                    else:
                        filtered_delta = filtered_all[sent_filtered_len:emit_len] if emit_len > sent_filtered_len else ""
                        sent_filtered_len = max(sent_filtered_len, emit_len)
                        sent_text = filtered_all[:sent_filtered_len]
                logger.info(f"ws.generate.step(), all_outputs={repr(all_outputs)}, stop={stop}, generated={total_generated}/{max_total_tokens}")
                step_msg = {
                    "ok": True, "outputs": filtered_delta, "stop": stop,
                    "token_count": token_count, "generated": total_generated,
                }
                if resync_text is not None:
                    # Mid-stream resync frame: carries the full canonical text and
                    # replace=True so the client swaps its accumulated content.
                    # stop stays False - generation continues normally after this.
                    step_msg["final_text"] = resync_text
                    step_msg["replace"] = True
                    sent_text = resync_text
                    sent_filtered_len = len(resync_text)
                # On the final tick, filtered_all is the full cleaned canonical
                # text (is_final=stop ran the complete strip). Send it so the
                # client can replace its append-streamed text and pick up any
                # retroactive cleaning the per-step delta protocol can't convey.
                if stop:
                    # Remove any [n] markers with no matching source so the
                    # reader never sees an orphaned citation marker.
                    filtered_all = _strip_orphan_markers(filtered_all, len(rag_sources))
                    step_msg["final_text"] = filtered_all
                if stop and rag_sources:
                    _cited = _filter_cited_sources(filtered_all, rag_sources)
                    if _cited:
                        step_msg["rag_sources"] = [{"title": r["title"], "url": r["url"], "authority": r.get("authority", 0)} for r in _cited]
                    rag_sources = []
                yield step_msg

        # Record generation speed for telemetry
        if total_generated > 0:
            speed_tracker.record_generation(total_generated, time.time() - gen_start)

    def _typed_arg(self, request, name, expected_type, default=None):
        # Dict-sourced mirror of http_api.get_typed_arg's coercion (the Flask
        # request has already been parsed into `request` by the caller).
        value = request.get(name)
        if value is None:
            return default
        if expected_type is bool:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if isinstance(value, expected_type):
            return value
        return expected_type(value)

    def generate_once(self, request):
        """Stateless one-shot generation mirroring http_api_generate. Returns
        the result dict (the caller jsonifies it)."""
        try:
            model_name = self._typed_arg(request, "model", str, self.config.MODEL_REPO)
            inputs = self._typed_arg(request, "inputs", str)
            do_sample = self._typed_arg(request, "do_sample", int, False)
            temperature = self._typed_arg(request, "temperature", float)
            # Per-type temperature: factual queries cooler unless the caller set
            # a non-default temperature (respects an explicit slider choice).
            if temperature is None or temperature == self.config.DEFAULT_TEMPERATURE:
                try:
                    if rag_pipeline.classify_query(rag_pipeline.extract_user_message(inputs or "")) == "factual":
                        temperature = self.config.FACTUAL_TEMPERATURE
                except Exception:
                    pass
            top_k = self._typed_arg(request, "top_k", int)
            top_p = self._typed_arg(request, "top_p", float)
            repetition_penalty = self._typed_arg(request, "repetition_penalty", float)
            max_length = self._typed_arg(request, "max_length", int)
            max_new_tokens = self._typed_arg(request, "max_new_tokens", int)
            # Quick-mode: cap generation length when the user message (text after
            # the last [INST] in the raw inputs) starts with "quick:". max_new_tokens
            # may be None here (caller supplied max_length instead), so guard before min().
            _quick_portion = (inputs or "").rsplit("[INST]", 1)[-1].strip().lower()
            if _quick_portion.startswith("quick:") and max_new_tokens is not None:
                max_new_tokens = min(max_new_tokens, 100)
            use_rag = self._typed_arg(request, "rag", str, "auto")
            logger.info(f"generate(), {model_name=}, {inputs=}")

            if not self.backend_config.public_api:
                raise ValueError(f"We do not provide public API for {model_name} due to license restrictions")

            # Short-circuit greetings with a canned reply before touching Mixtral.
            if inputs is not None and rag_pipeline.is_greeting(inputs):
                greeting = rag_pipeline.random_greeting_response()
                logger.info(f"generate() greeting short-circuit -> {greeting!r}")
                return {"ok": True, "outputs": greeting}

            # RAG augmentation + Mixtral [INST] wrapping.
            rag_results = []
            if inputs is not None:
                if use_rag != "off":
                    inputs, rag_results = rag_pipeline.build_augmented_prompt(inputs)
                    if rag_results:
                        logger.info(f"generate(), RAG augmented with {len(rag_results)} results")
                else:
                    inputs = f"[INST] {inputs} [/INST]"

            if inputs is not None:
                inputs = self.tokenizer(inputs, return_tensors="pt")["input_ids"].to(self.config.DEVICE)
                n_input_tokens = inputs.shape[1]
            else:
                n_input_tokens = 0

            # Petals requires exactly one of max_length/max_new_tokens. If the caller
            # supplied neither, default to DEFAULT_MAX_TOKENS.
            if max_length is None and max_new_tokens is None:
                max_new_tokens = self.config.DEFAULT_MAX_TOKENS

            outputs = self.model.generate(
                inputs=inputs,
                do_sample=do_sample,
                temperature=temperature,
                top_k=top_k,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
                max_length=max_length,
                max_new_tokens=max_new_tokens,
            )
            outputs = safe_decode(self.tokenizer, outputs[0, n_input_tokens:])
            outputs = strip_filler_phrases(outputs, max_citations=len(rag_results))
            logger.info(f"generate(), outputs={repr(outputs)}")

            resp = {"ok": True, "outputs": outputs}
            if rag_results:
                resp["rag_sources"] = [{"title": r["title"], "url": r["url"], "authority": r.get("authority", 0)} for r in rag_results]
            return resp
        except MissingBlocksError:
            logger.warning("generate cluster_unavailable:", exc_info=True)
            return {"ok": False, "code": "cluster_unavailable", "traceback": format_exc()}
        except Exception:
            return {"ok": False, "traceback": format_exc()}
