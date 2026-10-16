from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "ab_stats", Path(__file__).resolve().parents[2] / "scripts" / "diag" / "ab_stats.py")
st = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(st)


def test_bootstrap_ci_is_deterministic_and_brackets_the_mean():
    d = [0.1, 0.0, 0.2, -0.05, 0.15, 0.0, 0.1, 0.3, 0.0, 0.05]
    a, b = st.bootstrap_ci(d, n_boot=2000), st.bootstrap_ci(d, n_boot=2000)
    assert a == b
    mean, lo, hi = a
    assert lo <= mean <= hi


def test_a_clear_gain_excludes_zero_and_pure_noise_does_not():
    gain = [0.2] * 30 + [0.0] * 30 + [-0.01] * 3
    assert st.bootstrap_ci(gain, n_boot=2000)[1] > 0
    noise = [0.1, -0.1] * 30
    _, lo, hi = st.bootstrap_ci(noise, n_boot=2000)
    assert lo < 0 < hi


def test_all_ties_gives_a_degenerate_zero_interval():
    assert st.bootstrap_ci([0.0] * 20, n_boot=500) == (0.0, 0.0, 0.0)


def test_sign_test():
    assert st.sign_test_p(0, 0) == 1.0
    assert st.sign_test_p(10, 0) < 0.01
    assert st.sign_test_p(5, 5) == 1.0
    assert abs(st.sign_test_p(8, 2) - 0.109375) < 1e-9


def test_pooling_uses_each_dumps_shared_population():
    d1 = {"shared": [0, 2], "arms": {"baseline": {"rr": [0.0, 9, 0.5]},
                                      "x": {"rr": [1.0, 0, 0.5]}}}
    d2 = {"shared": [0], "arms": {"baseline": {"rr": [0.2]}, "x": {"rr": [0.4]}}}
    assert st.pooled_deltas([d1, d2], "x") == [1.0, 0.0, 0.2]


def test_main_refuses_an_empty_shared_population(tmp_path, capsys, monkeypatch):
    import json
    import sys

    dump = tmp_path / "d.json"
    dump.write_text(json.dumps({"shared": [], "arms": {
        "baseline": {"rr": [], "ms": []}, "x": {"rr": [], "ms": []}}}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["ab_stats", "--arm", "x", str(dump)])
    assert st.main() == 2
    assert "nothing to pool" in capsys.readouterr().err


def test_percentile():
    assert st.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10
    assert st.percentile([], 0.95) == 0.0


def test_main_reports_the_max_added_latency_and_the_pooled_fired_rate(tmp_path, capsys, monkeypatch):
    import json
    import sys

    def kw(zero, fallback):
        return [{"queries": 9, "zero": z, "fallback": f} for z, f in zip(zero, fallback)]

    dump = tmp_path / "d.json"
    dump.write_text(json.dumps({"shared": [0, 1, 2], "arms": {
        "baseline": {"rr": [0.0, 0.5, 1.0], "ms": [10.0, 10.0, 10.0],
                     "kw": kw([1, 0, 2], [0, 0, 0])},
        "x": {"rr": [0.5, 0.5, 1.0], "ms": [12.0, 40.0, 11.0],
              "kw": kw([1, 0, 2], [3, 0, 0])}}}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["ab_stats", "--arm", "x", str(dump)])
    assert st.main() == 0
    out = capsys.readouterr().out
    assert "max +30.0" in out
    # eligible = baseline zero > 0 (queries 0 and 2); x appended rows on query 0 only
    assert "fired on 1/2" in out


def test_pooled_fired_sums_each_dumps_shared_population():
    def kw(zero, fallback):
        return [{"zero": z, "fallback": f} for z, f in zip(zero, fallback)]

    d1 = {"shared": [0, 2], "arms": {"baseline": {"kw": kw([1, 1, 0], [0, 0, 0])},
                                      "x": {"kw": kw([1, 1, 0], [4, 4, 4])}}}
    d2 = {"shared": [0], "arms": {"baseline": {"kw": kw([2], [0])}, "x": {"kw": kw([2], [0])}}}
    # d1: query 1 is outside shared, query 2 had no zero-hit sub-query -> 1/1; d2 -> 0/1
    assert st.pooled_fired([d1, d2], "x") == (2, 1)
