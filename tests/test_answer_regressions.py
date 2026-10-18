"""Regression tests for the answer-pipeline bugs fixed on 2026-10-11.

Every test here is a real production bug that reached users once. Run before
any deploy that touches postprocessing.py, chat_service.py or fastapi_app.py:

    python -m unittest tests.test_answer_regressions -v

Runs anywhere: postprocessing imports cleanly; the three pure helpers that
live in chat_service.py sit behind torch/hivemind imports, so they are loaded
by extracting their source (a rename there fails these tests loudly, which is
the point).
"""
import re
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pollen.features.chat.postprocessing import (  # noqa: E402
    _filter_cited_sources,
    _renumber_citations,
    _strip_model_reference_list,
    _strip_orphan_markers,
    wants_reference_list,
)


def _load_chat_service_helpers():
    src = (REPO / "pollen" / "features" / "chat" / "chat_service.py").read_text(encoding="utf-8")
    ns = {"time": time}
    for name in ("_last_sentence_end", "_runon_limit", "_self_repeat_start"):
        m = re.search(r"^def %s\(.*?(?=^\S)" % name, src, re.S | re.M)
        if not m:
            raise AssertionError("helper %s not found in chat_service.py (renamed?)" % name)
        exec(m.group(0), ns)
    return ns["_last_sentence_end"], ns["_runon_limit"], ns["_self_repeat_start"]


_last_sentence_end, _runon_limit, _self_repeat_start = _load_chat_service_helpers()

SOURCES8 = ["S%d" % i for i in range(1, 9)]


class CitationNumbering(unittest.TestCase):
    """Markers in the text must match the pills shown (bug: [3] with 2 pills)."""

    def _final(self, text, sources):
        text = _strip_orphan_markers(text, len(sources))
        cited = _filter_cited_sources(text, sources)
        if cited:
            text = _renumber_citations(text, len(sources))
        return text, cited

    def test_subset_renumbered_to_pill_order(self):
        text, cited = self._final("A meta-analysis [3]. Another study [2].", SOURCES8)
        self.assertEqual(cited, ["S2", "S3"])
        self.assertEqual(text, "A meta-analysis [2]. Another study [1].")

    def test_already_ordered_unchanged(self):
        text, cited = self._final("First [1] then [2].", SOURCES8)
        self.assertEqual(cited, ["S1", "S2"])
        self.assertEqual(text, "First [1] then [2].")

    def test_compound_and_repeat_markers(self):
        text, cited = self._final("Compound [2, 5] single [8] repeat [5].", SOURCES8)
        self.assertEqual(cited, ["S2", "S5", "S8"])
        self.assertEqual(text, "Compound [1, 2] single [3] repeat [2].")

    def test_out_of_range_marker_removed(self):
        text, cited = self._final("Out of range [9] valid [4].", SOURCES8)
        self.assertEqual(cited, ["S4"])
        self.assertNotIn("[9]", text)
        self.assertIn("[1]", text)

    def test_year_in_brackets_dropped_as_orphan(self):
        text, cited = self._final("A study from [2021] cites [6].", SOURCES8)
        self.assertEqual(cited, ["S6"])
        self.assertNotIn("2021", text)

    def test_no_markers_untouched(self):
        text, cited = self._final("Plain answer, no citations.", SOURCES8)
        self.assertEqual(cited, [])
        self.assertEqual(text, "Plain answer, no citations.")


class ReferenceListStripping(unittest.TestCase):
    """Model-written bibliographies carry fabricated details and must go."""

    PROD_BRACKET = (
        "Several studies demonstrate the impact [1]. Another review [2] agrees.\n"
        '[1] Estruch, R., et al. "Primary Prevention." New England Journal of '
        "Medicine, vol. 368, no. 17, pp. 193-1204. DOI: 10.105/NEJMoa 12090,\n"
        "[2] Schwingshackl, Lukas, et al."
    )
    PROD_NUMBERED = (
        "Studies show a clear association [1]. Attributed to olive oil [2].\n\n"
        "References:\n"
        "1. Trichopoulou, A., Bes-Rastroll, M. (2019). Adherence to mediterranean "
        "diet. Public Health Nutrition, 22(1), 12-134.\n"
        "2. Estruch, R., Ros, E., Salas-Salvado, J."
    )

    def test_bracket_style_stripped(self):
        out = _strip_model_reference_list(self.PROD_BRACKET, 2)
        self.assertNotIn("Estruch", out)
        self.assertNotIn("Schwingshackl", out)
        self.assertTrue(out.endswith("agrees."))

    def test_numbered_style_with_header_stripped(self):
        out = _strip_model_reference_list(self.PROD_NUMBERED, 8)
        self.assertNotIn("Trichopoulou", out)
        self.assertTrue(out.endswith("[2]."))

    def test_plain_numbered_list_survives(self):
        text = ("Top diets:\n1. Mediterranean diet is rich in olive oil.\n"
                "2. DASH diet lowers blood pressure.\n3. Nordic diet uses rye.")
        self.assertEqual(_strip_model_reference_list(text, 3), text)

    def test_list_under_non_reference_heading_survives(self):
        text = ("Good options:\n1. Mediterranean diet is rich in olive oil.\n"
                "2. DASH diet lowers blood pressure.")
        self.assertEqual(_strip_model_reference_list(text, 2), text)

    def test_whole_answer_bibliography_survives(self):
        text = ("References:\n1. Smith, J., et al. Journal, vol. 1, 2020.\n"
                "2. Jones, K., et al. Journal, vol. 2, 2021.")
        self.assertEqual(_strip_model_reference_list(text, 2), text)

    def test_requested_reference_list_is_detected(self):
        for q in ("give me a bibliography on sleep research",
                  "list references in APA format",
                  "show me the reference list for this topic"):
            self.assertTrue(wants_reference_list(q), q)
        for q in ("what do studies show about the Mediterranean diet",
                  "what are good reference books for chemistry"):
            self.assertFalse(wants_reference_list(q), q)


class RunOnBackstop(unittest.TestCase):
    """The cut-off must never eat real prose (bug: NEJM sentence cut twice)."""

    NEJM_TAIL = (
        " A 2013 randomized controlled trial published in The New England Journal "
        "of Medicine found that among high-risk individuals without a previous "
        "cardiovascular event, those assigned to a Mediterranean diet supplemented "
        "with extra-virgin olive oil or nuts had a significantly reduced risk of "
        "developing"
    )
    WORD_CHAIN = " " + " ".join(["highway bridge travel road city transport vehicle route"] * 8)

    def test_single_comma_formal_sentence_not_cut(self):
        limit = _runon_limit(self.NEJM_TAIL, 300, 600)
        self.assertEqual(limit, 600)
        self.assertLess(len(self.NEJM_TAIL), limit)

    def test_unpunctuated_word_chain_cut(self):
        limit = _runon_limit(self.WORD_CHAIN, 300, 600)
        self.assertEqual(limit, 300)
        self.assertGreaterEqual(len(self.WORD_CHAIN), limit)

    def test_sentence_end_catches_period_quote(self):
        text = 'He said "it ends on highways." then drifted'
        pos = _last_sentence_end(text)
        self.assertGreater(pos, 0)
        self.assertTrue(text[:pos].endswith('"'))


class SelfRepeatBackstop(unittest.TestCase):
    """Loop guard sees only the answer, never the prompt (replaced
    no_repeat_ngram_size, which garbled years and split words)."""

    def test_loop_detected_and_cut_at_sentence(self):
        loop = ("Sleep matters. Studies show that sleep deprivation impairs memory "
                "consolidation in adults and children. Studies show that sleep "
                "deprivation impairs memory consolidation in adults and children. "
                "Studies show that sleep")
        start = _self_repeat_start(loop, 80)
        self.assertGreater(start, 0)
        kept = loop[:_last_sentence_end(loop[:start])]
        self.assertTrue(kept.endswith("children."))

    def test_normal_answer_with_repeated_citations_not_cut(self):
        good = ("Research indicates sleep consolidates memory. A 2012 study [1] "
                "showed impairment after one night awake. A 2018 study [2] found "
                "emotional effects. Sleep also affects mood [1], and a 2012 review "
                "agrees [1] on the broad picture of cognitive health.")
        self.assertEqual(_self_repeat_start(good, 80), -1)

    def test_numbered_list_not_cut(self):
        lst = "\n".join("%d. Item number %d is described here briefly." % (i, i)
                        for i in range(1, 9))
        self.assertEqual(_self_repeat_start(lst, 80), -1)

    def test_short_answer_not_cut(self):
        self.assertEqual(_self_repeat_start("Yes.", 80), -1)


if __name__ == "__main__":
    unittest.main()
