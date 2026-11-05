"""Per-query metrics and a run's summary, for both entry points.

recall_deep (rows of set ``prod``, judged): precision of the rendered list (mean of per-query
precision, and micro), relevant items in the top 10, P@10, MRR, items, rendered tokens (output
characters / 4), wall latency p50/p95, precision per entry leg and per rank band (ranks are
render positions). Rows of set
``gold``: gold-in-served, gold-in-top-10 and reciprocal rank, per gold type.

Pre-turn (rows of set ``prod``, judged): micro precision overall and per item type (= section),
items and relevant items per build, total and memory tokens, build latency. Rows of set
``gold``: gold-in-context (the gold item rendered, or its text in the system prompt) and
reciprocal rank among rendered items of its type, per gold type.

Precision metrics use only rows whose every rendered item has a label; token and latency
metrics use every row without an error.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from nous_eval.served.judge import (
    PRIMARY_MODEL,
    CostLedger,
    LabelCache,
    cache_path,
    read_jsonl,
    rendered_keys,
    units_for_rows,
)
from nous_eval.served.stats import kappa, mean, pct, ratio

BANDS = ((1, 10, "1-10"), (11, 30, "11-30"), (31, 60, "31-60"), (61, None, "61+"))


def band_of(rank: int) -> str:
    for lo, hi, name in BANDS:
        if rank >= lo and (hi is None or rank <= hi):
            return name
    return "61+"


def load_run(run_dir: Path) -> tuple[dict, list[dict]]:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    return meta, read_jsonl(run_dir / "rows.jsonl")


def row_labels(entry: str, rows: list[dict], cache: LabelCache) -> dict[str, list[bool | None]]:
    """Each row's labels, aligned with its rendered order."""
    return {u["row"]: [cache.get(it["key"]) for it in u["items"]] for u in units_for_rows(entry, rows)}


def recall_row(row: dict, labs: list[bool | None]) -> dict[str, Any]:
    """Ranks are render positions (1-based), what the model reads: the trace's final_rank has gaps
    (ranked candidates that were not rendered) and is empty for rows appended after ranking."""
    ren = row["rendered"]
    ranks = list(range(1, len(ren) + 1))
    top = [x for r, x in zip(ranks, labs) if r <= 10]
    rel = sum(1 for x in labs if x)
    first = next((i + 1 for i, x in enumerate(labs) if x), None)
    legs: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    bands: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for it, r, x in zip(ren, ranks, labs):
        for d, k in ((legs, it.get("leg") or "unknown"), (bands, band_of(r))):
            d[k][0] += 1 if x else 0
            d[k][1] += 1
    top10_legs: dict[str, int] = defaultdict(int)
    for it, r in zip(ren, ranks):
        if r <= 10:
            top10_legs[it.get("leg") or "unknown"] += 1
    return {
        "n": float(len(ren)),
        "rel": float(rel),
        "prec": rel / len(ren) if ren else 0.0,
        "rel_top10": float(sum(1 for x in top if x)),
        "n_top10": float(len(top)),
        "p_at_10": sum(1 for x in top if x) / len(top) if top else 0.0,
        "mrr": 1.0 / first if first else 0.0,
        "tokens": row["chars"] / 4.0,
        "ms": float(row["wall_ms"]),
        "unlab": sum(1 for x in labs if x is None),
        "legs": dict(legs),
        "bands": dict(bands),
        "top10_legs": dict(top10_legs),
        "relset": frozenset(k for k, x in zip(rendered_keys(row), labs) if x),
        "set": frozenset(rendered_keys(row)),
    }


def _gold_hit(row: dict) -> tuple[int | None, list[str]]:
    """1-based position of the first gold item among same-type rendered items, and those items."""
    g = row["gold"]
    gold = {f"{g['type']}:{i}" for i in g["ids"]}
    same = [k for k in rendered_keys(row) if k.startswith(f"{g['type']}:")]
    return next((i + 1 for i, k in enumerate(same) if k in gold), None), same


def recall_gold_row(row: dict) -> dict[str, Any]:
    g = row["gold"]
    gold = {f"{g['type']}:{i}" for i in g["ids"]}
    keys = rendered_keys(row)
    pos = next((i + 1 for i, k in enumerate(keys) if k in gold), None)
    return {
        "served": float(pos is not None),
        "top10": float(pos is not None and pos <= 10),
        "rr": 1.0 / pos if pos else 0.0,
        "tokens": row["chars"] / 4.0,
        "ms": float(row["wall_ms"]),
    }


def preturn_row(row: dict, labs: list[bool | None]) -> dict[str, Any]:
    keys = rendered_keys(row)
    by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for k, x in zip(keys, labs):
        t = k.split(":", 1)[0]
        by_type[t][0] += 1 if x else 0
        by_type[t][1] += 1
    return {
        "n": float(len(keys)),
        "rel": float(sum(1 for x in labs if x)),
        "tokens": float(row["tokens_total"]),
        "tokens_memory": float(row["tokens_memory"]),
        "ms": float(row["build_ms"]),
        "unlab": sum(1 for x in labs if x is None),
        "by_type": dict(by_type),
        "relset": frozenset(k for k, x in zip(keys, labs) if x),
        "set": frozenset(keys),
    }


def preturn_gold_row(row: dict) -> dict[str, Any]:
    pos, same = _gold_hit(row)
    return {
        "gic": float(pos is not None or bool(row.get("gold_text_in_prompt"))),
        "rr": 1.0 / pos if pos else 0.0,
        "rendered": float(pos is not None),
        "tokens": float(row["tokens_total"]),
        "n_same_type": float(len(same)),
    }


def ok_rows(rows: list[dict], which: str, keys: set[str] | None = None) -> list[dict]:
    out = [r for r in rows if not r.get("error") and r.get("set") == which]
    if which == "gold":
        out = [r for r in out if r["gold"].get("valid", True)]
    return [r for r in out if keys is None or r["key"] in keys]


def _micro(per: list[dict], field: str) -> dict[str, dict[str, float]]:
    tot: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for p in per:
        for k, (rel, n) in p[field].items():
            tot[k][0] += rel
            tot[k][1] += n
    return {k: {"precision": rel / n if n else 0.0, "items": n} for k, (rel, n) in sorted(tot.items())}


def summarize_recall(rows: list[dict], labels: dict[str, list], keys: set[str] | None = None) -> dict[str, Any]:
    prod = ok_rows(rows, "prod", keys)
    full = [r for r in prod if all(x is not None for x in labels.get(r["key"], []))]
    per = [recall_row(r, labels[r["key"]]) for r in full]
    every = [recall_row(r, labels.get(r["key"]) or [None] * len(r["rendered"])) for r in prod]
    out: dict[str, Any] = {
        "prod": {
            "n_rows": len(prod),
            "n_judged": len(per),
            "unlabelled_items": sum(p["unlab"] for p in every),
            "leg_error_rows": sum(1 for r in prod if r.get("leg_errors")),
            "tool_error_rows": sum(1 for r in prod if r.get("is_error")),
            "items_mean": mean([p["n"] for p in every]),
            "tokens_mean": mean([p["tokens"] for p in every]),
            "latency_ms": {
                "p50": pct([p["ms"] for p in every], 0.5),
                "p95": pct([p["ms"] for p in every], 0.95),
                "mean": mean([p["ms"] for p in every]),
            },
        }
    }
    if per:
        n_q = len(per)
        legs = _micro(per, "legs")
        top10: dict[str, int] = defaultdict(int)
        for p in per:
            for k, v in p["top10_legs"].items():
                top10[k] += v
        total_items = sum(v["items"] for v in legs.values()) or 1
        for k, v in legs.items():
            v.update(share=v["items"] / total_items, per_query=v["items"] / n_q, in_top10=top10.get(k, 0))
        out["prod"].update(
            {
                "precision_macro": mean([p["prec"] for p in per]),
                "precision_micro": ratio([(p["rel"], p["n"]) for p in per]),
                "rel_top10": mean([p["rel_top10"] for p in per]),
                "p_at_10": mean([p["p_at_10"] for p in per]),
                "mrr": mean([p["mrr"] for p in per]),
                "rel_mean": mean([p["rel"] for p in per]),
                "per_leg": legs,
                "by_band": _micro(per, "bands"),
            }
        )
    gold = ok_rows(rows, "gold", keys)
    out["gold"] = _gold_summary(
        gold,
        recall_gold_row,
        ("served", "top10", "rr"),
        n_invalid=sum(1 for r in rows if r.get("set") == "gold" and not r["gold"].get("valid", True)),
    )
    return out


def _gold_summary(gold: list[dict], fn, metrics: tuple[str, ...], *, n_invalid: int) -> dict[str, Any]:
    out: dict[str, Any] = {"n_invalid_gold": n_invalid}
    by: dict[str, list[dict]] = defaultdict(list)
    for r in gold:
        by[r["gold"]["type"]].append(fn(r))
    for t, ps in sorted(by.items()):
        out[t] = {
            "n": len(ps),
            **{m: mean([p[m] for p in ps]) for m in metrics},
            "tokens_mean": mean([p["tokens"] for p in ps]),
        }
    return out


def summarize_preturn(rows: list[dict], labels: dict[str, list], keys: set[str] | None = None) -> dict[str, Any]:
    prod = ok_rows(rows, "prod", keys)
    full = [r for r in prod if all(x is not None for x in labels.get(r["key"], []))]
    per = [preturn_row(r, labels[r["key"]]) for r in full]
    every = [preturn_row(r, labels.get(r["key"]) or [None] * len(r["rendered"])) for r in prod]
    out: dict[str, Any] = {
        "prod": {
            "n_rows": len(prod),
            "n_judged": len(per),
            "unlabelled_items": sum(p["unlab"] for p in every),
            "items_mean": mean([p["n"] for p in every]),
            "tokens_total_mean": mean([p["tokens"] for p in every]),
            "tokens_memory_mean": mean([p["tokens_memory"] for p in every]),
            "build_ms": {"p50": pct([p["ms"] for p in every], 0.5), "p95": pct([p["ms"] for p in every], 0.95)},
            "zero_item_builds": sum(1 for p in every if not p["n"]),
        }
    }
    if per:
        by_type = _micro(per, "by_type")
        for v in by_type.values():
            v["per_build"] = v["items"] / len(per)
        out["prod"].update(
            {
                "precision_micro": ratio([(p["rel"], p["n"]) for p in per]),
                "rel_mean": mean([p["rel"] for p in per]),
                "precision_by_type": by_type,
            }
        )
    gold = ok_rows(rows, "gold", keys)
    out["gold"] = _gold_summary(
        gold,
        preturn_gold_row,
        ("gic", "rr", "rendered"),
        n_invalid=sum(1 for r in rows if r.get("set") == "gold" and not r["gold"].get("valid", True)),
    )
    return out


def summarize(run_dir: Path, *, alt_model: str | None = None, keys: set[str] | None = None) -> dict[str, Any]:
    """The run's summary (written to scores.json by the CLI)."""
    meta, rows = load_run(run_dir)
    entry = meta["entry"]
    data_dir = Path(meta["data_dir"])
    cache = LabelCache(cache_path(data_dir, meta.get("judge_model", PRIMARY_MODEL)))
    labels = row_labels(entry, rows, cache)
    fn = summarize_recall if entry == "recall" else summarize_preturn
    out = {
        "run": meta["tag"],
        "entry": entry,
        "arm": meta.get("arm"),
        "code": meta["code"],
        "errors": sum(1 for r in rows if r.get("error")),
        **fn(rows, labels, keys),
    }
    if alt_model:
        alt = LabelCache(cache_path(data_dir, alt_model))
        keys_here = {it["key"] for u in units_for_rows(entry, rows) for it in u["items"]}
        prim = {k: v for k, v in cache.labels.items() if k in keys_here}
        out["agreement"] = {
            "alt_model": alt_model,
            **kappa(prim, {k: v for k, v in alt.labels.items() if k in keys_here}),
        }
    out["cost"] = CostLedger(data_dir / "cost_ledger.jsonl").summary(run=meta["tag"], purpose=f"judge:{entry}")
    return out
