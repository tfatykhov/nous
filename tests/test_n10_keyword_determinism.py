"""N10 — keyword extraction must be deterministic across processes.

``IntentClassifier.classify`` deduped its keyword and entity lists with
``set``, whose iteration order for ``str`` depends on ``PYTHONHASHSEED``.
CPython randomises that per process, so identical user input produced a
different ``topic_keywords`` ORDER — and, past the ``[:10]`` cap, a
different keyword SET — on every run. Those keywords are joined into the
embedded retrieval query, so the same question retrieved differently
depending on which process served it.

THESE TESTS MUST SPAWN SUBPROCESSES. ``PYTHONHASHSEED`` is fixed for an
interpreter's lifetime, so a same-process assertion cannot observe the
defect at all and would pass just as happily against the broken code.
``test_the_guard_can_actually_fail`` exists to prove this suite has that
power, by running the OLD implementation and requiring it to disagree.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Deliberately long enough to blow past the [:10] cap so the test covers
# membership drift, not just ordering.
SENTENCE = (
    "Who is the mutual friend of our author and what country granted "
    "citizenship to their spouse after the Helsinki conference, and which "
    "organisation published the original manuscript?"
)

# Seeds chosen to disagree under the old implementation (verified: 5/5
# distinct). "0" disables randomisation, the rest force different tables.
SEEDS = ("0", "1", "2", "12345", "99999")

_CLASSIFY_SNIPPET = """
import json, sys
from nous.cognitive.schemas import FrameSelection
from nous.cognitive.intent import IntentClassifier

frame = FrameSelection(frame_id="conversation", frame_name="Conversation", confidence=1.0, match_method="default")
sig = IntentClassifier().classify(sys.argv[1], frame)
print(json.dumps({"kw": sig.topic_keywords, "ents": sig.entity_mentions}))
"""

# The pre-N10 implementation, reproduced verbatim so the guard's power can
# be demonstrated without reverting the source.
_OLD_SNIPPET = """
import json, re, sys
t = sys.argv[1]
words = re.findall(r"\\b[A-Z][a-z]+\\b|\\b\\w{6,}\\b|\\b[A-Z]{2,}\\b", t)
print(json.dumps({"kw": list(set(w.lower() for w in words))[:10]}))
"""


def _run(code: str, seed: str) -> str:
    """Run ``code`` in a fresh interpreter under ``PYTHONHASHSEED=seed``."""
    env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(REPO_ROOT))
    proc = subprocess.run(
        [sys.executable, "-c", code, SENTENCE],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if proc.returncode != 0:
        pytest.fail(f"subprocess failed (seed={seed}):\n{proc.stderr}")
    return proc.stdout.strip()


class TestKeywordDeterminism:
    def test_classify_is_identical_across_hash_seeds(self):
        outputs = {seed: _run(_CLASSIFY_SNIPPET, seed) for seed in SEEDS}
        distinct = set(outputs.values())
        assert len(distinct) == 1, (
            "N10: classify() must return identical signals regardless of "
            "PYTHONHASHSEED — topic_keywords are joined into the embedded "
            "retrieval query, so a varying order means the same question "
            "retrieves differently per process.\n"
            + "\n".join(f"  seed={s}: {o}" for s, o in outputs.items())
        )

    def test_the_guard_can_actually_fail(self):
        """Prove this suite would catch a reintroduction.

        A determinism test that cannot fail is decoration. Running the
        pre-N10 expression across the same seeds must produce MORE than one
        result — if it doesn't, these seeds no longer discriminate and the
        test above is silently vacuous.
        """
        distinct = {_run(_OLD_SNIPPET, seed) for seed in SEEDS}
        assert len(distinct) > 1, (
            "the chosen PYTHONHASHSEED values no longer expose set-ordering "
            "instability, so test_classify_is_identical_across_hash_seeds "
            "proves nothing — pick seeds that disagree"
        )


class TestOrderPreservation:
    """dict.fromkeys keeps first-appearance order; embeddings care."""

    def test_keywords_follow_source_word_order(self):
        from nous.cognitive.schemas import FrameSelection
        from nous.cognitive.intent import IntentClassifier

        text = "Zebra alpha Mikhail bandwidth zebra Mikhail"
        frame = FrameSelection(frame_id="conversation", frame_name="Conversation", confidence=1.0, match_method="default")
        kws = IntentClassifier().classify(text, frame).topic_keywords

        assert kws == ["zebra", "mikhail", "bandwidth"], (
            "first-appearance order, deduped, lowercased"
        )

    def test_cap_keeps_the_first_ten_not_an_arbitrary_ten(self):
        from nous.cognitive.schemas import FrameSelection
        from nous.cognitive.intent import IntentClassifier

        words = [f"keyword{i:02d}" for i in range(15)]
        frame = FrameSelection(frame_id="conversation", frame_name="Conversation", confidence=1.0, match_method="default")
        kws = IntentClassifier().classify(" ".join(words), frame).topic_keywords

        assert kws == words[:10], (
            "the [:10] cap must drop a STABLE tail; under set() it dropped a "
            "different keyword each run, changing query content not just order"
        )

    def test_entities_follow_source_order(self):
        from nous.cognitive.schemas import FrameSelection
        from nous.cognitive.intent import IntentClassifier

        text = "We met Yulia and Dmitri, then Yulia again in Prague."
        frame = FrameSelection(frame_id="conversation", frame_name="Conversation", confidence=1.0, match_method="default")
        ents = IntentClassifier().classify(text, frame).entity_mentions

        assert ents == ["Yulia", "Dmitri", "Prague"]


class TestBehaviourPreserved:
    """Dedup + lowercasing + cap semantics are unchanged."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("", []),
            ("ok", []),  # no token matches the regex
            ("Alpha alpha ALPHA", ["alpha"]),  # dedup is still case-folded
        ],
    )
    def test_dedup_semantics_unchanged(self, text, expected):
        from nous.cognitive.schemas import FrameSelection
        from nous.cognitive.intent import IntentClassifier

        frame = FrameSelection(frame_id="conversation", frame_name="Conversation", confidence=1.0, match_method="default")
        assert IntentClassifier().classify(text, frame).topic_keywords == expected

    def test_cap_still_ten(self):
        from nous.cognitive.schemas import FrameSelection
        from nous.cognitive.intent import IntentClassifier

        text = " ".join(f"distinct{i:02d}" for i in range(30))
        frame = FrameSelection(frame_id="conversation", frame_name="Conversation", confidence=1.0, match_method="default")
        assert len(IntentClassifier().classify(text, frame).topic_keywords) == 10
