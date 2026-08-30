"""F093 + F094 — dashboard vocabulary: series normalizer, chart grammar,
themes, Repeat, layout, and the data-aware compose rules.

Most of this is pure (no DB); the theme-wire tests use the Postgres service
fixtures like the other A2UI suites.
"""

from __future__ import annotations

import json
import math
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete

from nous.a2ui import grammar
from nous.a2ui.compose import _THEMES, SurfaceComposer, _binding_rules, _collect_bindings
from nous.a2ui.sources import (
    _TOTAL_BUDGET_CHARS,
    SourceRegistry,
    _bound_series,
    _pivot,
    build_default_registry,
    empty_series,
    is_series,
    to_series,
)
from nous.storage.models import A2uiAction, A2uiOutbox, A2uiSurface

# ---------------------------------------------------------------------------
# to_series (F094 §4) — the general normalizer
# ---------------------------------------------------------------------------


def test_to_series_sorts_counts_and_keeps_gap_placeholder():
    recs = [
        {"d": "2026-08-03", "x": 3.0},
        {"d": "2026-08-01", "x": 1.0},
        {"d": "2026-08-02", "x": float("nan")},
    ]
    s = to_series(recs, "d", "x", unit="bpm")
    assert is_series(s)
    # The dropped reading keeps its position as a t-only placeholder, so the
    # renderer breaks the line there instead of bridging the gap (codex P1).
    assert [p["t"] for p in s["points"]] == ["2026-08-01", "2026-08-02", "2026-08-03"]
    assert "v" not in s["points"][1] and s["points"][1] == {"t": "2026-08-02"}
    assert s["meta"]["dropped"] == 1
    assert s["unit"] == "bpm"
    # never coerces a dropped reading to zero — the finite points stay finite
    assert all(math.isfinite(p["v"]) for p in s["points"] if "v" in p)


def test_to_series_multi_series_all_missing_timestamp_is_a_placeholder():
    s = to_series(
        [
            {"d": "2026-08-01", "ok": 2, "bad": 1},
            {"d": "2026-08-02", "ok": float("nan"), "bad": float("inf")},
            {"d": "2026-08-03", "ok": 5, "bad": 0},
        ],
        "d",
        "v",
        value_keys=["ok", "bad"],
    )
    assert [p["t"] for p in s["points"]] == ["2026-08-01", "2026-08-02", "2026-08-03"]
    assert s["points"][1] == {"t": "2026-08-02"}  # gap kept, no keys
    assert s["meta"]["dropped"] == 1


def test_to_series_multi_series_keeps_keys():
    s = to_series(
        [{"d": "2026-08-01", "ok": 2, "bad": 1}], "d", "v", value_keys=["ok", "bad"]
    )
    assert s["keys"] == ["ok", "bad"]
    assert s["points"][0] == {"t": "2026-08-01", "ok": 2, "bad": 1}


def test_to_series_caps_and_downsamples():
    s = to_series([{"d": f"2026-{i:04d}", "x": i} for i in range(500)], "d", "x")
    assert len(s["points"]) == 200
    assert s["meta"]["downsampled_from"] == 500
    # endpoints preserved
    assert s["points"][0]["v"] == 0
    assert s["points"][-1]["v"] == 499


async def _series_registry(*keys):
    reg = SourceRegistry()

    def _mk(_k):
        async def fetch(params):
            return to_series(
                [{"d": f"2026-{i:04d}", "x": float(i)} for i in range(200)], "d", "x"
            )

        return fetch

    for k in keys:
        reg.register(k, _mk(k))
    return reg


async def test_three_chart_app_renders_three_real_series_within_budget():
    # rev-be/codex P1: at ~6k per 200-point series the old 12k total left the
    # THIRD chart ~0 chars, and _bound_series returned an unvalidated 2-point
    # stub stamped downsampled_from:200 — a fabricated trend AND a budget
    # overrun. All three must now render real data within the total.
    reg = await _series_registry("a", "b", "c")
    out = await reg.resolve([{"key": k, "source": k, "params": {}} for k in ("a", "b", "c")])
    total = sum(len(json.dumps(out[k], default=str)) for k in out)
    assert all(len(out[k]["points"]) > 100 for k in ("a", "b", "c")), "a chart lost its data"
    assert total <= _TOTAL_BUDGET_CHARS


async def test_budget_exhaustion_returns_honest_empty_not_a_fake_trend():
    # The 4th series has no budget left — it must be an explicit empty series
    # with a reason, never a 2-point line pretending to be a 200-point trend.
    reg = await _series_registry("a", "b", "c", "d")
    out = await reg.resolve([{"key": k, "source": k, "params": {}} for k in ("a", "b", "c", "d")])
    d = out["d"]
    assert d["points"] == [] and "reason" in d["meta"]
    assert "budget" in d["meta"]["reason"]


def test_bound_series_downsamples_never_replaces_with_a_marker():
    big = to_series([{"d": f"2026-{i:04d}", "x": i} for i in range(500)], "d", "x")
    bounded, size = _bound_series(big, 400)
    # The advisor's P1: _bound would have swapped the whole series for a
    # {_truncated} marker and the series-shape rule would reject the app.
    assert is_series(bounded)
    assert size <= 400
    assert len(bounded["points"]) >= 2


def test_empty_series_carries_a_reason():
    e = empty_series("no db", unit="kg")
    assert is_series(e) and e["points"] == []
    assert e["meta"]["reason"] == "no db"
    assert e["unit"] == "kg"


def test_downsample_preserves_the_gap_placeholder():
    # A non-finite reading in the middle of a >200-point source: the gap must
    # survive the 200-point cap, or the renderer bridges it (codex P1).
    recs = [
        {"d": f"2026-{i:04d}", "x": (float("nan") if i == 250 else float(i))}
        for i in range(500)
    ]
    s = to_series(recs, "d", "x")
    assert len(s["points"]) <= 200
    assert s["meta"]["dropped"] == 1
    assert any(set(p) == {"t"} for p in s["points"]), "gap dropped by downsampling"
    # endpoints still pinned
    assert s["points"][0]["v"] == 0.0 and s["points"][-1]["v"] == 499.0


def test_downsample_preserves_per_key_gaps_in_multi_series():
    # A multi-series point that omits ONE key (the other still finite) is a gap
    # for that key's line; naive stride could skip it and bridge the gap (codex
    # P1). Series 'a' is absent at index 251 of 500.
    recs = []
    for i in range(500):
        rec = {"d": f"2026-{i:04d}", "a": float(i), "b": float(i)}
        if i == 251:
            rec["a"] = float("nan")
        recs.append(rec)
    s = to_series(recs, "d", "v", value_keys=["a", "b"])
    assert len(s["points"]) <= 200
    assert any("a" not in p and "b" in p for p in s["points"]), "per-key gap lost"


def test_downsample_preserves_extrema():
    # A flat series with a single spike: stride alone would sample the spike out
    # and the domain would hide the anomaly (codex P1). The peak must survive.
    recs = [{"d": f"2026-{i:04d}", "x": (100.0 if i == 251 else 5.0)} for i in range(500)]
    s = to_series(recs, "d", "x")
    assert len(s["points"]) <= 200
    assert max(p["v"] for p in s["points"] if "v" in p) == 100.0


def test_downsample_preserves_per_key_gap_representatives():
    # 'a' missing at 251, 'b' at 252 in the SAME sampled interval: keeping only
    # one global gap marker would bridge the other key's line (codex P1).
    recs = []
    for i in range(500):
        rec = {"d": f"2026-{i:04d}", "a": float(i), "b": float(i)}
        if i == 251:
            rec["a"] = float("nan")
        if i == 252:
            rec["b"] = float("nan")
        recs.append(rec)
    s = to_series(recs, "d", "v", value_keys=["a", "b"])
    pts = s["points"]
    assert any("a" not in p for p in pts), "a's break lost"
    assert any("b" not in p for p in pts), "b's break lost"


def test_chart_rejects_non_array_series_points():
    # {kind:series, points:{}} passes the kind check but readSeries needs an
    # array and renders "not a series" (codex P2).
    bad = {"kind": "series", "points": {}, "unit": "", "meta": {}}
    comps = [{"id": "s", "component": "Sparkline", "path": "/o"}]
    errs = _binding_rules(comps, {"o": bad}, {"o": bad}, _collect_bindings(comps))
    assert any("points" in e and "array" in e for e in errs)


def test_over_capacity_rejects_zero_valid_index_coverage():
    # Bound only through a non-numeric child /pending/foo: bound per rule 1, but
    # zero records render (codex P2).
    big = {"pending": [{"i": i} for i in range(12)]}
    comps = [{"id": "k", "component": "KeyValueTable", "rows": {"path": "/pending/foo"}}]
    errs = _binding_rules(comps, big, big, _collect_bindings(comps))
    assert any("only 0" in e for e in errs)


def test_nested_repeat_chart_is_left_to_the_renderer():
    # A chart inside NESTED repeat scopes is composed per-scope by the renderer;
    # the single-level static resolver must not validate it against a wrong path
    # (false reject or false pass) — it defers instead (codex P2).
    comps = [
        {"id": "outer", "component": "Column", "children": {"componentId": "grp", "path": "/groups"}},
        {"id": "grp", "component": "Column", "children": {"componentId": "card", "path": "metrics"}},
        {"id": "card", "component": "Sparkline", "path": "trend"},
    ]
    model = {"groups": [{"metrics": [{"trend": [1, 2, 3]}]}]}  # inner is a bad array
    errs = _binding_rules(comps, model, model, _collect_bindings(comps))
    assert not any("not a series" in e for e in errs)


def test_relative_chart_path_validates_every_repeat_item():
    # Item 0 is a valid series but item 1 is an array — a heterogeneous repeat
    # must be caught, not passed on the strength of item 0 (codex P2).
    ser = {"kind": "series", "points": [{"t": "a", "v": 1}], "unit": "", "meta": {}}
    comps = [
        {"id": "list", "component": "Column", "children": {"componentId": "card", "path": "/items"}},
        {"id": "card", "component": "Sparkline", "path": "trend"},
    ]
    model = {"items": [{"trend": ser}, {"trend": [1, 2, 3]}]}
    errs = _binding_rules(comps, model, model, _collect_bindings(comps))
    assert any("not a series" in e for e in errs)


def test_validate_and_caps_tolerate_a_non_string_archetype(settings):
    from nous.a2ui.grammar import caps_for

    # caps_for must not hash a non-string archetype (TypeError escapes the
    # repair loop + fallback, codex P2); _validate returns a clean error.
    assert caps_for(["ledger"]) == caps_for(None)
    composer = SurfaceComposer(object(), settings, SourceRegistry())
    parsed = {"components": [{"id": "x", "component": "Text", "text": "hi"}], "archetype": ["ledger"]}
    assert any("archetype" in e for e in composer._validate(parsed, {}))


def test_downsample_keeps_breaks_when_gaps_exceed_the_cap():
    # 500-point alternating valid/missing series (250 gaps): the cap cannot keep
    # every gap, but must keep a representative break for each broken pair —
    # never stride them all away and re-bridge (codex P1 round 6).
    recs = [
        {"d": f"2026-{i:04d}", "x": (float(i) if i % 2 == 0 else float("nan"))}
        for i in range(500)
    ]
    s = to_series(recs, "d", "x")
    assert len(s["points"]) <= 200
    gaps = sum(1 for p in s["points"] if "v" not in p)
    assert gaps > 10, "representative breaks must survive, not be strided away"


def test_sparkline_and_barchart_reject_a_multi_series_source():
    ser = {
        "kind": "series",
        "points": [{"t": "a", "ok": 1, "bad": 2}],
        "keys": ["ok", "bad"],
        "unit": "",
        "meta": {},
    }
    for ctype in ("Sparkline", "BarChart"):
        comps = [{"id": "c", "component": ctype, "path": "/o"}]
        errs = _binding_rules(comps, {"o": ser}, {"o": ser}, _collect_bindings(comps))
        assert any("multi-series" in e for e in errs), ctype
    # LineChart is the multi-series consumer — it must NOT be flagged.
    lc = [{"id": "l", "component": "LineChart", "path": "/o", "series": [{"key": "ok"}]}]
    errs = _binding_rules(lc, {"o": ser}, {"o": ser}, _collect_bindings(lc))
    assert not any("multi-series" in e for e in errs)


def test_relative_chart_path_validated_against_the_template_item():
    ser = {"kind": "series", "points": [{"t": "a", "v": 1}], "unit": "", "meta": {}}
    comps = [
        {"id": "list", "component": "Column", "children": {"componentId": "card", "path": "/items"}},
        {"id": "card", "component": "Sparkline", "path": "trend"},
    ]
    ok_model = {"items": [{"trend": ser}]}
    assert not any(
        "not a series" in e
        for e in _binding_rules(comps, ok_model, ok_model, _collect_bindings(comps))
    )
    # item.trend is an array, not a series → now CAUGHT (the blunt skip missed it)
    bad_model = {"items": [{"trend": [1, 2, 3]}]}
    assert any(
        "not a series" in e
        for e in _binding_rules(comps, bad_model, bad_model, _collect_bindings(comps))
    )


def test_pivot_materializes_the_full_calendar_window():
    cal = ["2026-08-01", "2026-08-02", "2026-08-03"]
    rows = [("2026-08-02", "success", 4)]  # only the middle day had rows
    recs = _pivot(rows, ["success", "failure"], calendar=cal)
    assert [r["t"] for r in recs] == cal, "quiet days must not vanish"
    assert recs[0] == {"t": "2026-08-01", "success": 0, "failure": 0}
    assert recs[1]["success"] == 4
    # a returned date outside the window is still kept (union, never dropped)
    recs2 = _pivot([("2026-07-31", "success", 1)], ["success"], calendar=cal)
    assert "2026-07-31" in [r["t"] for r in recs2]


async def test_health_series_bounds_rows_and_orders_ascending(tmp_path):
    import sqlite3

    db = tmp_path / "h.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE health_metrics(metric TEXT, ts TEXT, value REAL)")
    conn.executemany(
        "INSERT INTO health_metrics VALUES('hr', ?, ?)",
        [(f"2026-01-{i:02d}", float(i)) for i in range(1, 6)],
    )
    conn.commit()
    conn.close()

    reg = build_default_registry(health_db_path=str(db))
    out = await reg.resolve(
        [{"key": "h", "source": "health_series", "params": {"metric": "hr", "limit": 3}}]
    )
    s = out["h"]
    assert is_series(s)
    # limit=3 keeps the 3 most recent rows, re-ordered ascending for the series
    assert [p["v"] for p in s["points"]] == [3.0, 4.0, 5.0]


def test_pivot_fills_missing_categories_with_zero():
    rows = [("2026-08-01", "success", 3), ("2026-08-01", "failure", 1), ("2026-08-02", "success", 5)]
    recs = _pivot(rows, ["success", "failure", "noise"])
    assert recs == [
        {"t": "2026-08-01", "success": 3, "failure": 1, "noise": 0},
        {"t": "2026-08-02", "success": 5, "failure": 0, "noise": 0},
    ]


# ---------------------------------------------------------------------------
# Grammar (F094 charts, F093 Repeat + caps)
# ---------------------------------------------------------------------------


def _skel(extra):
    return [
        {"id": "root", "component": "Column", "children": ["header", "sec", "footer"]},
        {"id": "header", "component": "AppHeader", "title": "t", "composedAt": {"path": "/meta/composedAt"}},
        {"id": "sec", "component": "Section", "title": "S", "child": "c"},
        *extra,
        {"id": "footer", "component": "AppFooter"},
    ]


def test_charts_are_allowed_and_binding_mandatory():
    assert grammar.lint_micro_app(_skel([{"id": "c", "component": "Sparkline", "path": "/hr"}])) == []
    bad = grammar.lint_micro_app(_skel([{"id": "c", "component": "LineChart", "series": []}]))
    assert any("no `path`" in e for e in bad)


def test_linechart_over_arity_gets_a_clean_grammar_message():
    # >4 series is caught data-free in grammar so the repair loop leads with a
    # clean "max 4" instead of the schema's misleading maxItems error (rev-be P2).
    over = _skel([
        {
            "id": "c",
            "component": "LineChart",
            "path": "/o",
            "series": [{"key": f"k{i}"} for i in range(5)],
        }
    ])
    errs = grammar.lint_micro_app(over)
    assert any("max 4" in e and "LineChart" in e for e in errs)


def test_repeat_template_is_visible_and_validated():
    ok = grammar.lint_micro_app(
        [
            {"id": "root", "component": "Column", "children": ["header", "sec", "footer"]},
            {"id": "header", "component": "AppHeader", "title": "t", "composedAt": {"path": "/m"}},
            {"id": "sec", "component": "Section", "title": "S", "child": "list"},
            {"id": "list", "component": "Column", "children": {"componentId": "row", "path": "/items"}},
            {"id": "row", "component": "KeyValueTable", "rows": {"path": "@index"}},
            {"id": "footer", "component": "AppFooter"},
        ]
    )
    assert ok == []
    # unknown template component + missing path both error
    bad = grammar.lint_micro_app(
        [
            {"id": "root", "component": "Column", "children": ["header", "sec", "footer"]},
            {"id": "header", "component": "AppHeader", "title": "t", "composedAt": {"path": "/m"}},
            {"id": "sec", "component": "Section", "title": "S", "child": "list"},
            {"id": "list", "component": "Column", "children": {"componentId": "ghost"}},
            {"id": "footer", "component": "AppFooter"},
        ]
    )
    assert any("unknown component" in e for e in bad)
    assert any("no `path`" in e for e in bad)


def test_repeat_template_component_counts_toward_reference_and_single_parent():
    # `row` referenced by both the template and a direct child → one-parent error.
    errs = grammar.lint_micro_app(
        [
            {"id": "root", "component": "Column", "children": ["header", "sec", "footer"]},
            {"id": "header", "component": "AppHeader", "title": "t", "composedAt": {"path": "/m"}},
            {"id": "sec", "component": "Section", "title": "S", "child": "list"},
            {"id": "list", "component": "Column", "children": {"componentId": "row", "path": "/items"}},
            {"id": "row", "component": "Text", "text": "x"},
            {"id": "extra", "component": "Column", "children": ["row"]},
            {"id": "footer", "component": "AppFooter"},
        ]
    )
    assert any("one parent per component" in e for e in errs)


def test_caps_are_archetype_aware():
    assert grammar.caps_for(None) == (40, 5)
    assert grammar.caps_for("status") == (40, 5)
    assert grammar.caps_for("ledger") == (80, 8)
    assert grammar.caps_for("briefing") == (80, 8)


def test_ledger_gets_more_sections():
    sections = [
        {"id": f"sec{i}", "component": "Section", "title": f"S{i}", "child": f"c{i}"}
        for i in range(7)
    ]
    bodies = [{"id": f"c{i}", "component": "Text", "text": "x"} for i in range(7)]
    comps = [
        {"id": "root", "component": "Column", "children": ["header", *[f"sec{i}" for i in range(7)], "footer"]},
        {"id": "header", "component": "AppHeader", "title": "t", "composedAt": {"path": "/m"}},
        *sections,
        *bodies,
        {"id": "footer", "component": "AppFooter"},
    ]
    # 7 sections: rejected as status (max 5), accepted as ledger (max 8).
    assert any("Section" in e for e in grammar.lint_micro_app(comps, archetype="status"))
    assert grammar.lint_micro_app(comps, archetype="ledger") == []


# ---------------------------------------------------------------------------
# Compose data-aware rules (F093 §5.1, F094 §5)
# ---------------------------------------------------------------------------


def test_unread_source_rule():
    sd = {"hr": {"kind": "series", "points": [{"t": "a", "v": 1}], "unit": "", "meta": {}}}
    comps = [{"id": "t", "component": "Text", "text": "62 bpm"}]
    errs = _binding_rules(comps, sd, {"hr": sd["hr"]}, _collect_bindings(comps))
    assert any("no component binds /hr" in e for e in errs)


def test_series_shape_rule_rejects_a_record_list():
    sd = {"rows": [{"a": 1}]}
    comps = [{"id": "c", "component": "Sparkline", "path": "/rows"}]
    errs = _binding_rules(comps, sd, {"rows": sd["rows"]}, _collect_bindings(comps))
    assert any("not a series" in e for e in errs)


def test_series_arity_rule():
    ser = {"kind": "series", "points": [{"t": "a", "success": 1}], "unit": "", "meta": {}}
    comps = [{"id": "l", "component": "LineChart", "path": "/o", "series": [{"key": "ghost"}]}]
    errs = _binding_rules(comps, {"o": ser}, {"o": ser}, _collect_bindings(comps))
    assert any("ghost" in e for e in errs)


def test_series_arity_uses_declared_keys_not_the_first_point():
    # A per-point non-finite reading is omitted from that point, so `success`
    # is absent from points[0] yet declared in `keys` — a points[0] check would
    # falsely reject this valid multi-series LineChart (codex P2).
    ser = {
        "kind": "series",
        "points": [{"t": "a", "failure": 2}, {"t": "b", "success": 1, "failure": 3}],
        "keys": ["success", "failure"],
        "unit": "",
        "meta": {},
    }
    comps = [
        {"id": "l", "component": "LineChart", "path": "/o", "series": [{"key": "success"}]}
    ]
    errs = _binding_rules(comps, {"o": ser}, {"o": ser}, _collect_bindings(comps))
    assert not any("absent" in e for e in errs)
    # A key in neither the declared list nor any point is still caught.
    comps[0]["series"] = [{"key": "ghost"}]
    errs = _binding_rules(comps, {"o": ser}, {"o": ser}, _collect_bindings(comps))
    assert any("ghost" in e for e in errs)


def test_series_arity_empty_series_is_a_valid_empty_state():
    # An empty single series declares its emptiness via reason/meta; it must not
    # be rejected as "series key absent" (points[0] would be {} → false error).
    empty = {"kind": "series", "points": [], "unit": "", "meta": {"reason": "no rows"}}
    comps = [{"id": "l", "component": "LineChart", "path": "/o", "series": [{"key": "v"}]}]
    errs = _binding_rules(comps, {"o": empty}, {"o": empty}, _collect_bindings(comps))
    assert not any("absent" in e for e in errs)


def test_linechart_requires_at_least_one_series():
    ser = {"kind": "series", "points": [{"t": "a", "v": 1}], "unit": "", "meta": {}}
    comps = [{"id": "l", "component": "LineChart", "path": "/o", "series": []}]
    errs = _binding_rules(comps, {"o": ser}, {"o": ser}, _collect_bindings(comps))
    assert any("no series" in e for e in errs)


def test_over_capacity_under_render_rule():
    big = {"pending": [{"i": i} for i in range(12)]}
    comps = [
        {"id": f"k{i}", "component": "KeyValueTable", "rows": {"path": f"/pending/{i}"}}
        for i in range(7)
    ]
    errs = _binding_rules(comps, big, big, _collect_bindings(comps))
    assert any("resolved 12 records but only 7" in e for e in errs)


def test_over_capacity_counts_distinct_indices_not_the_max():
    # Binding only the LAST record: max(indices)+1 == n, but coverage is 1
    # record — a partial source presented as complete (codex P2).
    big = {"pending": [{"i": i} for i in range(12)]}
    comps = [{"id": "k", "component": "KeyValueTable", "rows": {"path": "/pending/11"}}]
    errs = _binding_rules(comps, big, big, _collect_bindings(comps))
    assert any("resolved 12 records but only 1" in e for e in errs)


def test_over_capacity_ignores_out_of_range_indices():
    # n-1 real indices + one out-of-range /pending/999: the raw count is n, but
    # one real record is unrendered (codex P2). Only in-range indices count.
    big = {"pending": [{"i": i} for i in range(12)]}
    comps = [
        {"id": f"k{i}", "component": "KeyValueTable", "rows": {"path": f"/pending/{i}"}}
        for i in range(11)
    ] + [{"id": "kx", "component": "KeyValueTable", "rows": {"path": "/pending/999"}}]
    errs = _binding_rules(comps, big, big, _collect_bindings(comps))
    assert any("resolved 12 records but only 11" in e for e in errs)


def test_root_relative_chart_path_is_validated_from_root():
    # With no repeat scope, the renderer resolves a bare relative path from the
    # model root as `/trend` — so validation must too, not skip (codex round 6).
    ser = {"kind": "series", "points": [{"t": "a", "v": 1}], "unit": "", "meta": {}}
    comps = [{"id": "sp", "component": "Sparkline", "path": "trend"}]
    assert not any(
        "not a series" in e
        for e in _binding_rules(comps, {"trend": ser}, {"trend": ser}, _collect_bindings(comps))
    )
    # a root-relative path resolving to an array must still be caught
    bad = {"trend": [1, 2, 3]}
    assert any(
        "not a series" in e
        for e in _binding_rules(comps, bad, bad, _collect_bindings(comps))
    )


def test_repeat_template_binding_exempts_over_capacity():
    big = {"items": [{"i": i} for i in range(12)]}
    # a template binds /items (no index) → all render → no over-capacity error
    comps = [{"id": "list", "component": "Column", "children": {"componentId": "row", "path": "/items"}}]
    errs = _binding_rules(comps, big, big, _collect_bindings(comps))
    assert not any("only" in e for e in errs)


def test_validate_rejects_a_non_string_theme_without_crashing(settings):
    # `theme not in _THEMES` would hash a dict/list and raise TypeError inside
    # _validate, bypassing the repair loop and the fallback (codex P2).
    composer = SurfaceComposer(object(), settings, SourceRegistry())
    parsed = {
        "components": [{"id": "x", "component": "Text", "text": "hi"}],
        "theme": {"unhashable": True},
    }
    errs = composer._validate(parsed, {})
    assert any("theme" in e for e in errs)


# ---------------------------------------------------------------------------
# Themes (F093 §3) — the wire
# ---------------------------------------------------------------------------


@pytest.fixture
def a2ui_agent_id() -> str:
    return f"test-a2ui-dash-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def a2ui_settings(settings, a2ui_agent_id: str):
    return settings.model_copy(
        update={"agent_id": a2ui_agent_id, "telegram_bot_token": None, "telegram_chat_id": None}
    )


@pytest_asyncio.fixture
async def service(db, a2ui_settings, a2ui_agent_id: str):
    from nous.a2ui.service import SurfaceService

    svc = SurfaceService(db, a2ui_settings)
    yield svc
    async with db.session() as session:
        await session.execute(delete(A2uiAction).where(A2uiAction.agent_id == a2ui_agent_id))
        await session.execute(delete(A2uiOutbox).where(A2uiOutbox.agent_id == a2ui_agent_id))
        await session.execute(delete(A2uiSurface).where(A2uiSurface.agent_id == a2ui_agent_id))
        await session.commit()


def _micro_app(theme: str | None = None):
    from nous.a2ui.dsl import AppFooter, AppHeader, BuiltSurface, Column, Section, Text

    built = BuiltSurface(
        kind="micro_app",
        origin="chat",
        title="dash",
        priority=0,
        allowed_actions=["app.close"],
        components=[
            Column("root", children=["header", "sec", "footer"], align="stretch"),
            AppHeader("header", title="dash", composedAt={"path": "/meta/composedAt"}),
            Section("sec", title="S", child="body"),
            Text("body", "hi"),
            AppFooter("footer"),
        ],
        data_model={"meta": {"composedAt": "2026-08-30T00:00:00+00:00"}},
        expires_in=None,
    )
    built.app_spec = {"intent": "x", "archetype": "status", "refine_options": [], "data_sources": []}
    if theme:
        built.app_spec["theme"] = theme
    return built


pytestmark_theme = pytest.mark.postgres_only


@pytest.mark.postgres_only
async def test_theme_travels_in_createsurface_metadata(service, db, a2ui_agent_id: str):
    surface_id = await service.push_built(_micro_app(theme="alpine-dusk"))
    snap, _ = await service.snapshot(surface_id)
    ext = snap["createSurface"]["metadata"]["extensions"]
    assert ext["com_nous_theme"] == "alpine-dusk"


@pytest.mark.postgres_only
async def test_default_theme_omits_the_extension(service, db):
    surface_id = await service.push_built(_micro_app(theme=None))
    snap, _ = await service.snapshot(surface_id)
    ext = snap["createSurface"]["metadata"]["extensions"]
    assert "com_nous_theme" not in ext


@pytest.mark.postgres_only
async def test_theme_survives_dedup_replacement(service, db):
    first = await service.push_built(_micro_app(theme="harbor"), dedup_key="app:themed")
    replaced = await service.push_built(_micro_app(theme="signal"), dedup_key="app:themed")
    assert replaced == first
    snap, _ = await service.snapshot(first)
    assert snap["createSurface"]["metadata"]["extensions"]["com_nous_theme"] == "signal"


def test_theme_enum_is_closed():
    assert set(_THEMES) == {"nous-default", "alpine-dusk", "harbor", "paper", "signal"}
