"""A4: Leg carries optional keyword counts, serialized only when known."""

from __future__ import annotations

import json

from nous.observability.retrieval_trace import NULL_TRACE, Leg, RetrievalTrace

_TODAYS_KEYS = {
    "name", "attempted", "n_returned", "n_deduped", "n_dropped",
    "score_min", "score_max", "error", "skip_reason",
}


def test_a_leg_with_no_counts_serializes_to_exactly_todays_keys():
    """Old rows and every leg that never reports these must be byte-compatible."""
    assert set(Leg(name="brain").to_dict()) == _TODAYS_KEYS


def test_known_counts_are_serialized():
    d = Leg(name="heart_primary", n_keyword_queries=3, n_keyword_zero=2).to_dict()
    assert d["n_keyword_queries"] == 3 and d["n_keyword_zero"] == 2
    assert "n_keyword_fallback" not in d


def test_a_zero_count_is_serialized_not_dropped():
    """0 means ran-and-found-nothing; only None means unknown."""
    d = Leg(name="chunk", n_keyword_queries=1, n_keyword_zero=1, n_keyword_fallback=0).to_dict()
    assert d["n_keyword_zero"] == 1 and d["n_keyword_fallback"] == 0


def test_trace_leg_records_the_counts_and_they_survive_json():
    t = RetrievalTrace(query="q")
    t.leg("heart_primary", n_returned=4, n_keyword_queries=3, n_keyword_zero=3)
    leg = json.loads(json.dumps(t.to_dict()))["legs"][0]
    assert (leg["n_keyword_queries"], leg["n_keyword_zero"]) == (3, 3)


def test_a_later_rollup_pass_without_counts_does_not_erase_them():
    """The pipeline's attempted-legs rollup calls leg() again with only
    n_returned; None must leave the counts alone (same rule as n_dropped)."""
    t = RetrievalTrace(query="q")
    t.leg("heart_primary", n_keyword_queries=3, n_keyword_zero=1)
    t.leg("heart_primary", attempted=True, n_returned=9)
    leg = t.to_dict()["legs"][0]
    assert leg["n_returned"] == 9
    assert (leg["n_keyword_queries"], leg["n_keyword_zero"]) == (3, 1)


def test_a_leg_never_given_counts_stays_in_todays_shape_inside_a_trace():
    t = RetrievalTrace(query="q")
    t.leg("brain", n_returned=2)
    assert set(t.to_dict()["legs"][0]) == _TODAYS_KEYS


def test_the_null_trace_accepts_the_new_keywords():
    NULL_TRACE.leg("heart_primary", n_keyword_queries=1, n_keyword_zero=0)
