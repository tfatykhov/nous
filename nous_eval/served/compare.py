"""Paired comparison of two runs of one entry point, and the plan's win condition.

Every delta is B minus A over the queries both runs answered, with a paired bootstrap 95% CI.
Precision deltas use the queries fully labelled in both runs. The win condition (precision plan,
2026-11-04, with the architecture review's explicit gold-loss tolerance):

1. precision or tokens improve: the precision CI lies above 0, or the tokens CI lies below 0;
2. gold-in-served (recall_deep) or gold-in-context (pre-turn), pooled over gold types, is
   non-inferior: its CI lower bound >= -gold_loss_tolerance; and for recall_deep the same for
   relevant-in-top-10 with its own tolerance;
3. p95 latency grows by at most max_p95_regression (a fraction of A's p95).

A check that could not be measured (no gold rows, no query judged in both runs) fails the win
and is named in ``why_not``. Each run's labels come from its own data dir and judge model.

Before the numbers it lists what differs between the runs (settings, code, instrument, inputs),
because a comparison is one variable only when exactly one thing differs.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from nous_eval.served.judge import PRIMARY_MODEL, LabelCache, cache_path
from nous_eval.served.score import (
    load_run,
    ok_rows,
    preturn_gold_row,
    preturn_row,
    recall_gold_row,
    recall_row,
    row_labels,
)
from nous_eval.served.stats import mean, paired_bootstrap, pct, ratio


def differences(ma: dict, mb: dict) -> dict[str, Any]:
    """What differs between two runs' records."""
    sa, sb = ma.get("settings", {}), mb.get("settings", {})
    settings = {
        n: {"a": sa.get(n, {}).get("value", "absent"), "b": sb.get(n, {}).get("value", "absent")}
        for n in sorted(set(sa) | set(sb))
        if sa.get(n, {}).get("value") != sb.get(n, {}).get("value")
    }
    out: dict[str, Any] = {"settings": settings}
    ca, cb = ma["code"], mb["code"]
    for k in ("head", "dirty_files", "served_sha256"):
        if ca.get(k) != cb.get(k):
            out[f"code.{k}"] = {"a": ca.get(k), "b": cb.get(k)}
    for k in (
        "inputs_sha256",
        "as_of",
        "warm",
        "db_template",
        "judge_model",
        "agent",
        "base_files",
        "python_hash_seed",
    ):
        if ma.get(k) != mb.get(k):
            out[k] = {"a": ma.get(k), "b": mb.get(k)}
    # The effective runtime knobs (vector_weight, rrf_k, cross_encoder_enabled), counted only where
    # no settings difference already explains them: an arm that sets rrf_k shows one change, not two.
    # arm_wins (which stored override an arm beat) is provenance, not a variable.
    ea = (ma.get("runtime_config") or {}).get("effective") or {}
    eb = (mb.get("runtime_config") or {}).get("effective") or {}
    for k in sorted(set(ea) | set(eb)):
        if ea.get(k) != eb.get(k) and k not in settings:
            out[f"runtime.{k}"] = {"a": ea.get(k), "b": eb.get(k)}
    out["n_variables"] = len(settings) + sum(1 for k in out if k not in ("settings", "n_variables"))
    return out


def _delta(pairs: list[tuple[float, float]], stat=mean) -> dict[str, Any]:
    point, lo, hi = paired_bootstrap(pairs, stat)
    return {"a": stat([a for a, _ in pairs]), "b": stat([b for _, b in pairs]), "delta": point, "ci": [lo, hi]}


def _latency(pa: list[float], pb: list[float]) -> dict[str, Any]:
    pairs = list(zip(pa, pb))
    return {f"p{int(q * 100)}": _delta(pairs, lambda xs, q=q: pct(xs, q)) for q in (0.5, 0.95)}


def _paired(entry: str, rows_a: list[dict], rows_b: list[dict], labels: dict, keys: set[str] | None):
    a = {r["key"]: r for r in ok_rows(rows_a, "prod", keys)}
    b = {r["key"]: r for r in ok_rows(rows_b, "prod", keys)}
    shared = sorted(set(a) & set(b))
    la, lb = labels["a"], labels["b"]
    fn = recall_row if entry == "recall" else preturn_row
    every = [
        (fn(a[k], la.get(k) or [None] * len(a[k]["rendered"])), fn(b[k], lb.get(k) or [None] * len(b[k]["rendered"])))
        for k in shared
    ]
    judged = [
        k for k in shared if all(x is not None for x in la.get(k, [])) and all(x is not None for x in lb.get(k, []))
    ]
    full = [(fn(a[k], la[k]), fn(b[k], lb[k])) for k in judged]
    return shared, every, full


def _gold_pairs(entry: str, rows_a: list[dict], rows_b: list[dict], keys: set[str] | None) -> dict[str, list]:
    a = {r["key"]: r for r in ok_rows(rows_a, "gold", keys)}
    b = {r["key"]: r for r in ok_rows(rows_b, "gold", keys)}
    fn = recall_gold_row if entry == "recall" else preturn_gold_row
    by: dict[str, list] = defaultdict(list)
    for k in sorted(set(a) & set(b)):
        pair = (fn(a[k]), fn(b[k]))
        by[a[k]["gold"]["type"]].append(pair)
        by["all"].append(pair)
    return by


def _type_ratio(full: list[tuple[dict, dict]], field: str) -> dict[str, Any]:
    types = sorted({t for x, y in full for t in (*x[field], *y[field])})
    out = {}
    for t in types:
        pairs = [
            (
                (x[field].get(t, [0, 0])[0], x[field].get(t, [0, 0])[1]),
                (y[field].get(t, [0, 0])[0], y[field].get(t, [0, 0])[1]),
            )
            for x, y in full
        ]
        out[t] = _delta(pairs, ratio)
    return out


def compare(
    run_a: Path,
    run_b: Path,
    *,
    gold_loss_tolerance: float,
    top10_loss_tolerance: float = 0.0,
    max_p95_regression: float = 0.20,
    keys: set[str] | None = None,
) -> dict[str, Any]:
    ma, rows_a = load_run(run_a)
    mb, rows_b = load_run(run_b)
    if ma["entry"] != mb["entry"]:
        raise SystemExit(f"runs of different entry points: {ma['entry']} vs {mb['entry']}")
    entry = ma["entry"]
    labels = {
        side: row_labels(
            entry, rows, LabelCache(cache_path(Path(meta["data_dir"]), meta.get("judge_model", PRIMARY_MODEL)))
        )
        for side, meta, rows in (("a", ma, rows_a), ("b", mb, rows_b))
    }
    shared, every, full = _paired(entry, rows_a, rows_b, labels, keys)
    rep: dict[str, Any] = {
        "entry": entry,
        "a": ma["tag"],
        "b": mb["tag"],
        "differences": differences(ma, mb),
        "n_shared": len(shared),
        "n_judged_both": len(full),
    }
    if not shared:
        raise SystemExit("the runs share no answered query")
    lat_field = "ms"
    m: dict[str, Any] = {}
    if full:
        m["precision_micro"] = _delta([((x["rel"], x["n"]), (y["rel"], y["n"])) for x, y in full], ratio)
        for f in ("rel", "n") + (("prec", "rel_top10", "p_at_10", "mrr") if entry == "recall" else ()):
            m[f] = _delta([(x[f], y[f]) for x, y in full])
        m["by_type" if entry == "preturn" else "per_leg"] = _type_ratio(
            full, "by_type" if entry == "preturn" else "legs"
        )
        if entry == "recall":
            m["by_band"] = _type_ratio(full, "bands")
        m["identical_sets"] = sum(1 for x, y in full if x["set"] == y["set"])
        m["relevant_gained"] = sum(len(y["relset"] - x["set"]) for x, y in full)
        m["relevant_lost"] = sum(len(x["relset"] - y["set"]) for x, y in full)
    m["tokens"] = _delta([(x["tokens"], y["tokens"]) for x, y in every])
    if entry == "preturn":
        m["tokens_memory"] = _delta([(x["tokens_memory"], y["tokens_memory"]) for x, y in every])
    m["latency_ms"] = _latency([x[lat_field] for x, _ in every], [y[lat_field] for _, y in every])
    rep["prod"] = m
    gold = _gold_pairs(entry, rows_a, rows_b, keys)
    gm = ("served", "top10", "rr", "served_summary") if entry == "recall" else ("gic", "rr")
    rep["gold"] = {
        t: {"n": len(ps), **{f: _delta([(x[f], y[f]) for x, y in ps]) for f in gm}} for t, ps in gold.items()
    }
    rep["win"] = win_condition(
        entry,
        rep,
        gold_loss_tolerance=gold_loss_tolerance,
        top10_loss_tolerance=top10_loss_tolerance,
        max_p95_regression=max_p95_regression,
    )
    return rep


def win_condition(
    entry: str,
    rep: dict[str, Any],
    *,
    gold_loss_tolerance: float,
    top10_loss_tolerance: float,
    max_p95_regression: float,
) -> dict[str, Any]:
    m = rep["prod"]
    prec = m.get("prec" if entry == "recall" else "precision_micro")
    checks: dict[str, Any] = {}
    prec_up = bool(prec) and prec["ci"][0] > 0
    tok_down = m["tokens"]["ci"][1] < 0
    checks["precision_or_tokens_improve"] = {
        "pass": prec_up or tok_down,
        "precision_ci": prec["ci"] if prec else None,
        "tokens_ci": m["tokens"]["ci"],
        "rule": "precision CI lower bound > 0, or tokens CI upper bound < 0",
    }
    gold_field = "served" if entry == "recall" else "gic"
    g = rep["gold"].get("all", {}).get(gold_field)
    checks["gold_non_inferior"] = {
        "pass": None if g is None else g["ci"][0] >= -gold_loss_tolerance,
        "metric": f"gold {gold_field}",
        "delta": None if g is None else g["delta"],
        "ci": None if g is None else g["ci"],
        "tolerance": gold_loss_tolerance,
        "rule": "CI lower bound >= -tolerance (no gold rows: not checked)",
    }
    if entry == "recall":
        t = m.get("rel_top10")
        checks["rel_top10_non_inferior"] = {
            "pass": None if t is None else t["ci"][0] >= -top10_loss_tolerance,
            "delta": None if t is None else t["delta"],
            "ci": None if t is None else t["ci"],
            "tolerance": top10_loss_tolerance,
            "rule": "CI lower bound >= -tolerance",
        }
    p95 = m["latency_ms"]["p95"]
    growth = (p95["b"] - p95["a"]) / p95["a"] if p95["a"] else None
    checks["p95_latency"] = {
        "pass": growth is not None and growth <= max_p95_regression,
        "a": p95["a"],
        "b": p95["b"],
        "growth": growth,
        "ci_ms": p95["ci"],
        "max_growth": max_p95_regression,
    }
    # Nothing measured is not a pass: plan lesson 1 measures gold on every arm, and a precision
    # claim needs judged queries.
    missing = [name for name, c in checks.items() if c["pass"] is None]
    if not prec:
        missing.append("no query fully judged in both runs")
    reasons = [f"not measured: {x}" for x in missing] + [n for n, c in checks.items() if c["pass"] is False]
    return {
        "checks": checks,
        "win": not reasons,
        "why_not": reasons,
        "one_variable": rep["differences"]["n_variables"] == 1,
    }
