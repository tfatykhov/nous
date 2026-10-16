from __future__ import annotations

import importlib.util
import statistics
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "long_qrels", Path(__file__).resolve().parents[2] / "scripts" / "diag" / "long_qrels.py")
lq = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lq)

ROWS = [{"query": f"what is fact number {i}", "gold_ids": [f"g{i}"], "memory_types": ["fact"]}
        for i in range(20)]
TOPICS = ["alpha topic", "beta topic words", "gamma"]
LENGTHS = [10, 20, 40, 60, 68, 70, 80, 120, 150, 30]
FILLERS = ["lorem ipsum dolor sit amet consectetur", "one two three"]


def test_deterministic():
    assert lq.build_long_rows(ROWS, TOPICS, LENGTHS, FILLERS) == lq.build_long_rows(
        ROWS, TOPICS, LENGTHS, FILLERS)


def test_gold_and_other_fields_are_untouched_and_input_not_mutated():
    before = [dict(r) for r in ROWS]
    out = lq.build_long_rows(ROWS, TOPICS, LENGTHS, FILLERS)
    assert ROWS == before
    for o, r in zip(out, ROWS):
        assert o["gold_ids"] == r["gold_ids"] and o["memory_types"] == r["memory_types"]


def test_topic_assignment_is_index_modulo_and_prefix_matches_context_path():
    out = lq.build_long_rows(ROWS, TOPICS, LENGTHS, FILLERS)
    for i, o in enumerate(out):
        assert o["query"].startswith(f"{TOPICS[i % 3]}: what is fact number {i}")


def test_median_matches_the_real_distribution_and_original_words_are_never_cut():
    out = lq.build_long_rows(ROWS, TOPICS, LENGTHS, FILLERS)
    w = [lq.words(o["query"]) for o in out]
    assert abs(statistics.median(w) - statistics.median(LENGTHS)) <= 3
    for o, r in zip(out, ROWS):
        assert r["query"] in o["query"]


def test_a_topic_equal_to_the_query_is_not_prefixed():
    rows = [{"query": "Same Thing", "gold_ids": ["g"]}]
    out = lq.build_long_rows(rows, ["same thing"], [2], ["x"])
    assert out[0]["query"] == "Same Thing"


@pytest.mark.parametrize("topic", ["", None])
def test_an_empty_topic_is_not_prefixed_like_the_context_path(topic):
    # context.py guards with `if current_topic and ...`: a falsy topic adds no prefix.
    rows = [{"query": "hello there", "gold_ids": ["g"]}]
    out = lq.build_long_rows(rows, [topic], [2], ["x"])
    assert out[0]["query"] == "hello there"


def test_target_is_the_exact_midpoint_quantile_without_float_flooring():
    # n=775, L=100, i=449: (i + 0.5) / n * L is exactly 58, but float math gives
    # 57.99..., which int() floors to 57. Lengths 0..99 make each value its own rank.
    rows = [{"query": "q", "gold_ids": ["g"]} for _ in range(775)]
    out = lq.build_long_rows(rows, ["t"], list(range(100)), ["w"])
    assert out[449]["long_target_words"] == 58
    assert [o["long_target_words"] for o in out] == [(2 * i + 1) * 100 // (2 * 775) for i in range(775)]


def test_fillers_with_no_words_raise_instead_of_looping_forever():
    with pytest.raises(ValueError, match="fillers"):
        lq.build_long_rows(ROWS, TOPICS, LENGTHS, ["", "   "])
