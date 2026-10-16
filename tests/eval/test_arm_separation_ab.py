from __future__ import annotations

import importlib.util
import re
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
    for name, resolve, want, outer in (
        ("POSCTRL_keyword_only", search._resolve_vector_weight, 0.0, base.vector_weight),
        ("or_fallback", search._resolve_keyword_or_fallback_enabled, True,
         base.hybrid_keyword_or_fallback_enabled),
    ):
        assert want != outer, name  # else the restore check below proves nothing
        with pinned_runtime(base):
            with pinned_runtime(base.model_copy(update=arm.ARMS[name])):
                assert resolve() == want, name
            # the next arm must not inherit this one's value
            assert resolve() == outer, name


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


# --- run() with the arm runner stubbed: no DB, no keys -----------------------

_WARM_MS = 99999.0  # only the warm-up pass reports this; it must never surface


def _stub_runner(calls, base_zero, fired_on):
    """Fake `run_arm`. The FIRST call is the warm-up and returns sentinel timings."""
    n = len(base_zero)
    by_flags = {
        "baseline": ([0.0, 0.5, 1.0, 0.0], [100.0] * n, [0] * n),
        "POSCTRL_keyword_only": ([1.0, 0.0, 0.5, 1.0], [101.0] * n, [0] * n),
        "or_fallback": ([0.5, 0.5, 1.0, 0.25], [110.0, 105.0, 300.0, 102.0],
                        [7 if i in fired_on else 0 for i in range(n)]),
    }

    async def fake_run_arm(flags, qrels, base, top_k, query_expansion=False):
        name = next(k for k, v in arm.ARMS.items() if v == flags)
        calls.append(name)
        rr, ms, fb = by_flags[name]
        if len(calls) == 1:
            ms = [_WARM_MS] * n
        return {"rr": list(rr), "hit": [x > 0 for x in rr], "served": [10] * n, "errors": [],
                "kw": [{"queries": 9, "zero": base_zero[i], "fallback": fb[i]} for i in range(n)],
                "ms": list(ms), "wall_s": 1.0, "cold_df_ms": {}}
    return fake_run_arm


def _args(tmp_path, out_json=None):
    from types import SimpleNamespace

    qrels = tmp_path / "q.jsonl"
    qrels.write_text("\n".join(f'{{"query": "q{i}", "gold_ids": ["g"]}}' for i in range(4)),
                     encoding="utf-8")
    return SimpleNamespace(qrels=str(qrels), db="synthetic", top_k=10, query_expansion=False,
                           arms="or_fallback", control="keyword_only", noise_floor=1,
                           defaults=False, out_json=out_json)


async def test_one_discarded_warm_up_pass_runs_before_any_timed_arm(tmp_path, capsys, monkeypatch):
    import json
    from types import SimpleNamespace

    calls: list[str] = []
    monkeypatch.setattr(arm, "run_arm", _stub_runner(calls, [2, 0, 1, 3], {0, 2, 3}))
    dump = tmp_path / "d.json"
    rc = await arm.run(_args(tmp_path, str(dump)), SimpleNamespace(openai_api_key="unused"))
    assert rc == 0
    # warm-up once, with the baseline settings, ahead of the three timed arms
    assert calls == ["baseline", "baseline", "POSCTRL_keyword_only", "or_fallback"]
    out = capsys.readouterr().out
    d = json.loads(dump.read_text(encoding="utf-8"))
    assert d["arms"]["baseline"]["ms"] == [100.0] * 4
    assert all(_WARM_MS not in a["ms"] for a in d["arms"].values())
    assert "99999" not in out


async def test_latency_report_carries_the_max_and_the_order_bias_check(tmp_path, capsys, monkeypatch):
    import json
    from types import SimpleNamespace

    monkeypatch.setattr(arm, "run_arm", _stub_runner([], [2, 0, 1, 3], {0, 2, 3}))
    dump = tmp_path / "d.json"
    assert await arm.run(_args(tmp_path, str(dump)), SimpleNamespace(openai_api_key="unused")) == 0
    lines = capsys.readouterr().out.splitlines()
    fb = next(x for x in lines if x.startswith("or_fallback ") and "added vs baseline" in x)
    assert re.search(r"max\s+300\.0", fb) and "max +200.0" in fb  # per-arm max, added max
    ctl = next(x for x in lines if x.startswith("POSCTRL_keyword_only") and "added" in x)
    assert "order-bias check" in ctl
    # the fired rate can be recomputed from the dump
    d = json.loads(dump.read_text(encoding="utf-8"))
    assert d["arms"]["or_fallback"]["kw"][0] == {"queries": 9, "zero": 2, "fallback": 7}
    assert d["arms"]["baseline"]["kw"][1]["zero"] == 0


async def test_a_qrel_set_with_no_zero_hit_query_is_named_as_such(tmp_path, capsys, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(arm, "run_arm", _stub_runner([], [0, 0, 0, 0], set()))
    assert await arm.run(_args(tmp_path), SimpleNamespace(openai_api_key="unused")) == 4
    assert ("no shared query had a zero-hit baseline sub-query; this qrel set cannot "
            "exercise the fallback") in capsys.readouterr().out
