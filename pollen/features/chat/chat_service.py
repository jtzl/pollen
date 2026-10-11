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
from pollen.features.chat import prompt_format
from pollen.features.chat.retrieval import rag_search
from pollen.infrastructure import speed_tracker
from pollen.features.chat.utils import (
    safe_decode,
    strip_filler_phrases,
    could_match_fallback_prefix,
    safe_emit_len_for_citations,
    is_url_query,
)
from pollen.features.chat.postprocessing import _filter_cited_sources, _strip_orphan_markers, _renumber_citations

logger = hivemind.get_logger(__file__)


def _last_sentence_end(text):
    """Index just past the last '.', '!' or '?' that ends a sentence -- allowing
    trailing closing quotes/brackets, then whitespace or end-of-string. Returns -1
    if none. Unlike a plain '. ' search this catches period+quote (e.g. 'highways."'),
    which real run-on spirals contain right before they degrade."""
    last = -1
    n = len(text)
    for i, ch in enumerate(text):
        if ch in ".!?":
            j = i + 1
            while j < n and text[j] in "\"')]”’":
                j += 1
            if j >= n or text[j].isspace():
                last = j
    return last


def _self_repeat_start(text, window):
    """If the last `window` characters of `text` already occur earlier in it,
    return the index where that final repeat begins; otherwise -1. Compared on
    lowercased, whitespace-collapsed text so a loop is caught even when spacing
    drifts. Only the model's own answer is passed in, never the prompt, so
    quoting the sources or the question is unaffected."""
    if window <= 0 or len(text) < window * 2:
        return -1
    norm = []
    back = []          # norm index -> index in text
    prev_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            ch = " "
            prev_space = True
        else:
            prev_space = False
        norm.append(ch.lower())
        back.append(i)
    if len(norm) < window * 2:
        return -1
    norm = "".join(norm)
    tail = norm[-window:]
    if norm.find(tail, 0, len(norm) - window) < 0:
        return -1
    return back[len(norm) - window]


def _runon_limit(tail, unpunct_limit, punct_limit):
    """Length a sentence-less tail may reach before the run-on backstop cuts it.
    A degenerate word chain has no internal punctuation and gets the short
    limit. A tail with several commas / parentheses / brackets is a long but
    structured sentence (e.g. "In a randomized trial (n=90) [1], researchers
    found ...") and gets the longer one, so real prose is not cut mid-sentence."""
    marks = sum(tail.count(c) for c in ",;:()[]")
    return punct_limit if marks >= 3 else unpunct_limit


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
        self._chat_stop_token = None
        self._chat_extra_stops = None
        for _family in config.MODEL_FAMILIES.values():
            for _model_config in _family:
                if _model_config.backend.key == backend_config.key:
                    self.generation_params = dict(_model_config.chat.generation_params or {})
                    self._chat_stop_token = _model_config.chat.stop_token
                    self._chat_extra_stops = list(_model_config.chat.extra_stop_sequences or [])
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
                search_results = rag_search.search_with_refine(user_msg)
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
        if stop_sequence is None:
            stop_sequence = self._chat_stop_token
        extra_stop_sequences = request.get("extra_stop_sequences")
        if extra_stop_sequences is None:
            extra_stop_sequences = self._chat_extra_stops
        if extra_stop_sequences is not None:
            cont_token = self.tokenizer(stop_sequence, return_tensors="pt")["input_ids"].to(self.config.DEVICE)
            if cont_token.shape != (1, 1):
                raise ValueError("extra_stop_sequences require stop_sequence length to be exactly 1 token")

        max_total_tokens = request.get("max_total_tokens", 512)
        # Quick-mode: cap generation length when the user message (text after
        # the last [INST] in the original request) starts with "quick:".
        _quick_portion = prompt_format.quick_portion(request.get("inputs"))
        if _quick_portion.startswith("quick:"):
            max_total_tokens = min(max_total_tokens, 100)
        # BACKSTOP 1: absolute server-side ceiling, independent of what the client
        # sent, so a generation that never emits a stop token cannot run forever.
        max_total_tokens = min(max_total_tokens, self.config.WS_MAX_OUTPUT_TOKENS)
        # DIAG: log the effective request/sampling params so we can see exactly what
        # the real browser sends vs. what a test client sends (why one spirals).
        logger.info(
            "ws.generate.params eff_temp=%s do_sample=%s top_p=%s top_k=%s rep_pen=%s "
            "no_repeat_ngram=%s stop_seq=%r extra_stops=%r max_total=%s min_floor=%s eos=%s"
            % (_eff_temp, request.get("do_sample"), request.get("top_p"), request.get("top_k"),
               request.get("repetition_penalty"), self._gen_param(request, "no_repeat_ngram_size", 0),
               stop_sequence, extra_stop_sequences, max_total_tokens,
               request.get("min_total_tokens", 120), self.tokenizer.eos_token_id))

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
                        # Only the cited subset is sent; renumber the markers to match.
                        _hard_final = _renumber_citations(_hard_final, len(rag_sources))
                        hard_stop_msg["final_text"] = _hard_final
                    # Same fallback as the normal-stop path: prefer the [N]-cited
                    # subset, else attach ALL retrieved sources (prose citations).
                    _srcs = _cited if _cited else rag_sources
                    logger.info("RAG sources-attach hardstop: path=%s cited=%d retrieved=%d attached=%d digit_marker=%s"
                             % ("cited" if _cited else "fallback", len(_cited), len(rag_sources), len(_srcs),
                                any(_hard_final[k + 1:k + 2].isdigit() for k in range(len(_hard_final)) if _hard_final[k] == "[")))
                    if _srcs:
                        hard_stop_msg["rag_sources"] = [{"title": r["title"], "url": r["url"], "authority": r.get("authority", 0)} for r in _srcs]
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
                eos_token_id=self.tokenizer.eos_token_id,
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
            _hit_extra = False
            _runon_cut = False
            if extra_stop_sequences is not None:
                for seq in extra_stop_sequences:
                    if seq in combined:
                        stop = True
                        _hit_extra = True
                        # Truncate output at the stop sequence
                        idx = combined.find(seq)
                        truncated = combined[:idx]
                        outputs = truncated[len(all_outputs):]
                        combined = truncated
                        session.last_token_id = cont_token
                        break
            # BACKSTOP 2: a hallucinated new turn (<|im_start|>) must terminate the
            # stream and be discarded, even if the client didn't list it as a stop.
            if not _hit_extra and "<|im_start|>" in combined:
                stop = True
                _hit_extra = True
                idx = combined.find("<|im_start|>")
                combined = combined[:idx]
                outputs = combined[len(all_outputs):]
            # BACKSTOP 3: degenerative run-on. Once a real sentence has formed, if the
            # model free-associates a long tail with no sentence-ending punctuation (a
            # word chain that never repeats a 3-gram and never emits <|im_end|>), cut it
            # and truncate at that last complete sentence. _last_sentence_end catches
            # period+quote ('highways."') that a plain '. ' search misses -- that miss
            # is why this never fired on the real spiral.
            if not stop and total_generated >= self.config.WS_RUNON_MIN_TOKENS:
                _lp = _last_sentence_end(combined)
                if _lp >= 0 and (len(combined) - _lp) >= _runon_limit(
                        combined[_lp:], self.config.WS_RUNON_MAX_UNPUNCT_CHARS,
                        getattr(self.config, "WS_RUNON_MAX_PUNCT_CHARS",
                                self.config.WS_RUNON_MAX_UNPUNCT_CHARS * 2)):
                    stop = True
                    _hit_extra = True   # bypass the min-tokens floor
                    _runon_cut = True   # attach sources even if inline cites were lost
                    # Truncate UNCONDITIONALLY at the sentence end so all_outputs ->
                    # filtered_all -> final_text is clean; the client replaces the shown
                    # text with final_text on stop, retracting the streamed run-on tail.
                    combined = combined[:_lp]
                    outputs = combined[len(all_outputs):] if len(combined) > len(all_outputs) else ""
                    logger.info("ws.generate.runon_backstop fired gen=%d cut_at=%d final_len=%d"
                                % (total_generated, _lp, len(combined)))
            # BACKSTOP 4: self-repetition. If the answer's tail repeats an earlier
            # part of the SAME answer, the model is looping; stop and keep only the
            # complete sentences before the repeat. Looks at the answer alone, so
            # (unlike no_repeat_ngram_size) it never blocks text copied from the prompt.
            if not stop:
                _rs = _self_repeat_start(combined, getattr(self.config, "WS_REPEAT_WINDOW_CHARS", 80))
                if _rs >= 0:
                    _keep = _last_sentence_end(combined[:_rs])
                    if _keep > 0:
                        stop = True
                        _hit_extra = True   # bypass the min-tokens floor
                        _runon_cut = True   # attach sources even if inline cites were lost
                        combined = combined[:_keep]
                        outputs = combined[len(all_outputs):] if len(combined) > len(all_outputs) else ""
                        logger.info("ws.generate.repeat_backstop fired gen=%d repeat_at=%d final_len=%d"
                                    % (total_generated, _rs, len(combined)))
            # HARD LIMIT check after generation
            if total_generated >= max_total_tokens:
                stop = True
            _min_floor = request.get("min_total_tokens", 120)
            # Use the hit flag (set BEFORE truncation) rather than re-scanning the
            # already-truncated `combined`, so a natural stop on a short answer is
            # not wrongly overridden by the min-tokens floor (that override caused
            # short answers to run past <|im_end|> and spiral).
            if stop and total_generated < _min_floor and total_generated < max_total_tokens:
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
                        # Only the cited subset is sent; renumber the markers to match.
                        filtered_all = _renumber_citations(filtered_all, len(rag_sources))
                        step_msg["final_text"] = filtered_all
                    # Prefer the precise [N]-cited subset when the model emitted
                    # markers; otherwise (it cited in prose at higher temp, or a run-on
                    # cut) fall back to ALL retrieved sources so the sources block still
                    # shows what was actually used -- on a normal stop too.
                    _srcs = _cited if _cited else rag_sources
                    logger.info("RAG sources-attach step: path=%s cited=%d retrieved=%d attached=%d digit_marker=%s"
                             % ("cited" if _cited else "fallback", len(_cited), len(rag_sources), len(_srcs),
                                any(filtered_all[k + 1:k + 2].isdigit() for k in range(len(filtered_all)) if filtered_all[k] == "[")))
                    if _srcs:
                        step_msg["rag_sources"] = [{"title": r["title"], "url": r["url"], "authority": r.get("authority", 0)} for r in _srcs]
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
            _quick_portion = prompt_format.quick_portion(inputs)
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
                    inputs = prompt_format.wrap_user(inputs)

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
                eos_token_id=self.tokenizer.eos_token_id,
            )
            outputs = safe_decode(self.tokenizer, outputs[0, n_input_tokens:])
            for _stp in (self._chat_extra_stops or []):
                if _stp:
                    outputs = outputs.split(_stp, 1)[0]
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
