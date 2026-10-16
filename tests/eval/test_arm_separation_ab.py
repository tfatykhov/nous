from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from nous.config import Settings
from nous.observability.retrieval_trace import RetrievalTrace
from nous_eval.env_pin import pinned_runtime, pinned_settings

_SPEC = importlib.util.spec_from_file_location(
    "arm_separation",
    Path(__file__).resolve().parents[2] / "scripts" / "diag" / "arm_separation.py")
arm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(arm)


def test_baseline_and_the_chosen_control_always_run_first():
    assert list(arm.select_arms(["or_fallback"], "keyword_only")) == [
        "baseline", "POSCTRL_keyword_only", "or_fallback"]
    assert list(arm.select_arms(None, "graph_off"))[:2] == ["baseline", "POSCTRL_graph_off"]


def test_default_arms_are_the_original_set():
    assert list(arm.select_arms(None))[2:] == ["spread_off", "spread_on", "cs_parity", "cs_baseline"]


def test_unknown_arm_fails_loudly_and_duplicates_collapse():
    with pytest.raises(ValueError):
        arm.select_arms(["or_falback"])
    names = list(arm.select_arms(["baseline", "or_fallback", "or_fallback"], "graph_off"))
    assert names.count("baseline") == 1 and names.count("or_fallback") == 1


def test_an_arm_with_a_matched_control_brings_it_along():
    # cs_parity is paired against cs_baseline, not baseline; without it the
    # report would KeyError after every arm had already run.
    names = list(arm.select_arms(["cs_parity"]))
    assert "cs_baseline" in names and names.count("cs_baseline") == 1


def test_there_is_no_noise_arm_any_more():
    assert "baseline_repeat" not in arm.ARMS


def test_every_arm_override_is_a_real_settings_field():
    for name, flags in arm.ARMS.items():
        for key in flags:
            assert key in Settings.model_fields, (name, key)


def test_arm_overrides_reach_the_search_resolvers():
    """The bug this guards: model_copy alone never reaches nous.heart.search."""
    from nous.heart import search

    base = pinned_settings()
    for name, resolve, want in (
        ("POSCTRL_keyword_only", search._resolve_vector_weight, 0.0),
        ("or_fallback", search._resolve_keyword_or_fallback_enabled, True),
    ):
        with pinned_runtime(base):
            with pinned_runtime(base.model_copy(update=arm.ARMS[name])):
                assert resolve() == want, name


def test_kw_from_trace_sums_the_reporting_legs():
    t = RetrievalTrace(capture_candidates=False)
    t.leg("heart_primary", n_keyword_queries=9, n_keyword_zero=8, n_keyword_fallback=30)
    t.leg("chunk", n_keyword_queries=1, n_keyword_zero=1)
    t.leg("brain", n_returned=3)
    assert arm.kw_from_trace(t) == {"queries": 10, "zero": 9, "fallback": 30}


def test_fired_summary_counts_only_queries_a_fallback_could_act_on():
    base = [{"zero": 2}, {"zero": 0}, {"zero": 1}, {"zero": 3}]
    mine = [{"fallback": 5}, {"fallback": 9}, {"fallback": 0}, {"fallback": 1}]
    # index 1 had no zero-hit sub-query, so its rows do not count either way
    assert arm.fired_summary(base, mine, [0, 1, 2, 3]) == (3, 2)
    assert arm.fired_summary(base, mine, [1]) == (0, 0)
