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
    # The series past the total budget must be an explicit empty series with a
    # reason — never a 2-point line pretending to be a 200-point trend. The
    # count is DERIVED from the live constants (a fixed "4th source" broke the
    # moment the budget was raised 16k→40k, because 4 then legitimately fit):
    # enough full per-source allocations to exceed the total, plus one.
    import math

    # Sized from the MEASURED series, not the per-source allocation: the test
    # series serializes well under its 12k allotment, so dividing by the
    # allocation under-counts and the "exhausted" source still fits.
    one = to_series(
        [{"d": f"2026-{i:04d}", "x": float(i)} for i in range(200)], "d", "x"
    )
    series_chars = len(json.dumps(one, default=str))
    n = math.ceil(_TOTAL_BUDGET_CHARS / series_chars) + 2
    keys = [f"s{i}" for i in range(n)]
    reg = await _series_registry(*keys)
    out = await reg.resolve([{"key": k, "source": k, "params": {}} for k in keys])
    last = out[keys[-1]]
    assert last["points"] == [] and "reason" in last["meta"]
    assert "budget" in last["meta"]["reason"]
    # ...and the early sources still carry real data.
    assert len(out[keys[0]]["points"]) > 100


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


# ---------------------------------------------------------------------------
# F095 — agent-authored dashboard sources (the "any domain, live, no code"
# path). The agent writes the code that makes the data; it is stored and
# re-run on refresh.
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Stands in for run_script_structured — the real one is the run_python
    sandbox, exercised by its own suite; here we assert the SOURCE contract."""

    def __init__(self, outcome):
        self.outcome = dict(outcome)
        # The real worker JSON-normalizes inside the deadline and reports the
        # encoded size; mirror that so the source sees the same contract.
        if self.outcome.get("ok"):
            encoded = json.dumps(self.outcome.get("result"), default=str, allow_nan=False)
            self.outcome["result"] = json.loads(encoded)
            self.outcome.setdefault("result_chars", len(encoded))
        self.calls: list[str] = []

    async def __call__(
        self, code: str, *, timeout: float | None = None, max_result_chars: int | None = None
    ):
        self.calls.append(code)
        self.last_timeout = timeout
        return self.outcome


async def test_agent_script_returns_the_scripts_result():
    runner = _FakeRunner({"ok": True, "result": [{"t": "a", "v": 1}], "output": "", "error": None})
    reg = build_default_registry(run_script=runner)
    assert "agent_script" in reg.names()
    out = await reg.resolve(
        [{"key": "d", "source": "agent_script", "params": {"code": "result = []"}}]
    )
    assert out["d"] == [{"t": "a", "v": 1}]
    assert runner.calls == ["result = []"]


async def test_agent_script_is_absent_when_no_runner_is_wired():
    # Same conditional-registration discipline as every other source: no
    # backing component ⇒ the source does not exist, rather than erroring.
    assert "agent_script" not in build_default_registry().names()


async def test_agent_script_failure_is_explicit_never_a_blank_box():
    runner = _FakeRunner({"ok": False, "result": None, "output": "", "error": "boom"})
    reg = build_default_registry(run_script=runner)
    out = await reg.resolve(
        [{"key": "d", "source": "agent_script",
          "params": {"code": "1/0", "shape": "series", "series_keys": ["v"]}}]
    )
    # An empty SERIES, not a bare marker: refresh does not re-run binding
    # validation, so a chart bound to a working series must not be left
    # pointing at a non-series object on a transient failure (codex P1).
    assert is_series(out["d"]) and out["d"]["points"] == []
    assert "boom" in out["d"]["meta"]["reason"]


async def test_agent_script_without_a_result_says_so():
    runner = _FakeRunner({"ok": True, "result": None, "output": "printed", "error": None})
    reg = build_default_registry(run_script=runner)
    out = await reg.resolve(
        [{"key": "d", "source": "agent_script",
          "params": {"code": "print('hi')", "shape": "series", "series_keys": ["v"]}}]
    )
    assert is_series(out["d"]) and "no `result`" in out["d"]["meta"]["reason"]


async def test_agent_script_requires_code_and_caps_its_size():
    reg = build_default_registry(run_script=_FakeRunner({"ok": True, "result": [], "error": None}))
    with pytest.raises(ValueError, match="requires params.code"):
        await reg.resolve([{"key": "d", "source": "agent_script", "params": {}}])
    with pytest.raises(ValueError, match="max"):
        await reg.resolve(
            [{"key": "d", "source": "agent_script", "params": {"code": "x" * 20_000}}]
        )


async def test_agent_script_series_flows_through_the_normal_budget_path():
    # A script returning a 500-point series must be downsampled by the SAME
    # _bound_series path as a first-party source — not exempt from the cap.
    big = to_series([{"d": f"2026-{i:04d}", "x": float(i)} for i in range(500)], "d", "x")
    reg = build_default_registry(
        run_script=_FakeRunner({"ok": True, "result": big, "output": "", "error": None})
    )
    out = await reg.resolve(
        [{"key": "d", "source": "agent_script",
          "params": {"code": "result = s", "shape": "series", "series_keys": ["v"]}}]
    )
    assert is_series(out["d"]) and len(out["d"]["points"]) <= 200


async def test_refresh_re_runs_the_script_so_the_app_is_live_not_a_snapshot():
    """The point of the whole source: an app whose domain has no first-party
    fetcher used to be refused a refresh (model-supplied data ⇒ replaying what
    the model last said), making it no better than emailed static HTML. A
    script source re-EXECUTES, so the data actually changes."""
    from nous.a2ui.compose import SurfaceComposer
    from nous.config import Settings

    runs = {"n": 0}

    async def runner(code: str, *, timeout: float | None = None, max_result_chars=None):
        runs["n"] += 1
        return {
            "ok": True,
            "result": to_series([{"d": "2026-08-30", "x": float(runs["n"])}], "d", "x"),
            "output": "",
            "error": None,
        }

    composer = SurfaceComposer(
        object(), Settings(_env_file=None), build_default_registry(run_script=runner)
    )
    spec = {
        "data_sources": [
            {"key": "live", "source": "agent_script",
             "params": {"code": "result = f()", "shape": "series", "series_keys": ["v"]}}
        ]
    }
    first = await composer.refresh_data(spec)
    second = await composer.refresh_data(spec)
    assert first["live"]["points"][0]["v"] == 1.0
    assert second["live"]["points"][0]["v"] == 2.0, "refresh replayed instead of re-running"


async def test_agent_script_failure_keeps_a_chart_binding_valid():
    """codex P1: refresh does not re-run binding validation, so a transient
    script failure must not swap a chart's series for a non-series object —
    the chart would render 'not a series (object)' and LOSE the reason."""
    runner = _FakeRunner({"ok": False, "result": None, "output": "", "error": "API timeout"})
    reg = build_default_registry(run_script=runner)
    out = await reg.resolve(
        [{"key": "trend", "source": "agent_script",
          "params": {"code": "result = f()", "shape": "series", "series_keys": ["v"]}}]
    )
    comps = [{"id": "c", "component": "Sparkline", "path": "/trend"}]
    errs = _binding_rules(comps, out, out, _collect_bindings(comps))
    assert not any("not a series" in e for e in errs), "failure broke the chart binding"
    assert "API timeout" in out["trend"]["meta"]["reason"]


async def test_agent_script_rejects_an_oversized_result_before_bounding_it():
    """codex P1: `_bound` trims a list by popping ONE entry and re-serializing
    the rest — quadratic, on the main event loop, outside the script deadline.
    An oversized result is rejected after ONE O(n) measurement instead."""
    huge = [{"i": i, "pad": "x" * 100} for i in range(5000)]
    reg = build_default_registry(
        run_script=_FakeRunner({"ok": True, "result": huge, "output": "", "error": None})
    )
    out = await reg.resolve(
        [{"key": "d", "source": "agent_script",
          "params": {"code": "result = big", "shape": "series", "series_keys": ["v"]}}]
    )
    assert is_series(out["d"]) and "max" in out["d"]["meta"]["reason"]


async def test_agent_script_normalizes_non_json_values_for_jsonb():
    """codex P2: the data model is persisted as JSONB with no custom
    serializer, and `_bound` only MEASURES with default=str — so a datetime or
    UUID would survive to the commit and fail there."""
    import uuid as _uuid
    from datetime import datetime as _dt
    from decimal import Decimal

    raw = [{"t": _dt(2026, 8, 30), "id": _uuid.uuid4(), "v": Decimal("1.5")}]
    reg = build_default_registry(
        run_script=_FakeRunner({"ok": True, "result": raw, "output": "", "error": None})
    )
    out = await reg.resolve(
        [{"key": "d", "source": "agent_script", "params": {"code": "result = rows"}}]
    )
    # Round-trips cleanly => JSONB-safe by construction.
    json.dumps(out["d"])
    assert all(isinstance(r["t"], str) and isinstance(r["id"], str) for r in out["d"])


async def test_agent_script_failure_preserves_a_RECORDS_shape(caplog):
    """codex P1: an unconditional empty-SERIES failure broke a table bound to
    list paths. A one-row [{_error}] marker was the next attempt and is also
    wrong — a repeat template's children bind fields like `name`, so the
    marker renders as a row of blanks with nothing bound to `_error`, and the
    table lies about having a row. The record schema cannot be reconstructed
    at failure time, so the honest value is no rows + a logged reason."""
    runner = _FakeRunner({"ok": False, "result": None, "output": "", "error": "429 rate limited"})
    reg = build_default_registry(run_script=runner)
    with caplog.at_level("WARNING"):
        out = await reg.resolve(
            [{"key": "rows", "source": "agent_script",
              "params": {"code": "result = f()", "shape": "records"}}]
        )
    assert out["rows"] == [], "records failure must stay a list, with no fake row"
    assert "429 rate limited" in caplog.text, "the reason must reach the operator"


async def test_agent_script_rejects_a_declared_shape_the_script_did_not_produce():
    # Catching it here keeps a chart from ever binding to a record list.
    reg = build_default_registry(
        run_script=_FakeRunner({"ok": True, "result": [{"a": 1}], "output": "", "error": None})
    )
    out = await reg.resolve(
        [{"key": "d", "source": "agent_script",
          "params": {"code": "result = rows", "shape": "series", "series_keys": ["v"]}}]
    )
    assert is_series(out["d"]) and "declared shape 'series'" in out["d"]["meta"]["reason"]


async def test_agent_script_rejects_an_unknown_shape():
    reg = build_default_registry(run_script=_FakeRunner({"ok": True, "result": [], "error": None}))
    with pytest.raises(ValueError, match="shape must be one of"):
        await reg.resolve(
            [{"key": "d", "source": "agent_script",
              "params": {"code": "result = []", "shape": "blob"}}]
        )


async def test_agent_script_rejects_a_series_with_non_list_points():
    """codex P1: is_series() checks only `kind`, so a refreshed
    {"kind":"series","points":{}} would replace a working chart with a value
    the renderer shows as "not a series" — refresh skips binding validation."""
    bad = {"kind": "series", "points": {}, "unit": "", "meta": {}}
    reg = build_default_registry(
        run_script=_FakeRunner({"ok": True, "result": bad, "output": "", "error": None})
    )
    out = await reg.resolve(
        [{"key": "d", "source": "agent_script",
          "params": {"code": "result = bad", "shape": "series", "series_keys": ["v"]}}]
    )
    assert is_series(out["d"]) and isinstance(out["d"]["points"], list)
    assert "declared shape 'series'" in out["d"]["meta"]["reason"]


async def test_source_resolution_has_a_total_wall_clock_budget():
    """codex P1: sources resolve sequentially and an agent_script may run for
    the full 90s programmatic timeout, but compose must still fit the LLM
    inside NOUS_TOOL_TIMEOUT. The budget is shared across ALL sources."""
    import asyncio as _asyncio

    from nous.a2ui import sources as _sources

    reg = SourceRegistry()

    async def slow(params):
        await _asyncio.sleep(5)
        return []

    reg.register("slow", slow)
    original = _sources._TOTAL_SOURCE_SECONDS
    _sources._TOTAL_SOURCE_SECONDS = 0.05
    try:
        with pytest.raises(TimeoutError, match="resolution budget"):
            await reg.resolve([{"key": "a", "source": "slow", "params": {}}])
    finally:
        _sources._TOTAL_SOURCE_SECONDS = original


def test_compose_schema_only_advertises_registered_sources():
    """codex P2: the flag defaults OFF, so a static schema telling the agent to
    use `agent_script` would send it straight into UnknownSourceError and fail
    the whole compose call."""
    from nous.a2ui.tools import _compose_schema_for

    class _C:
        def __init__(self, reg):
            self._sources = reg

    without = _compose_schema_for(_C(build_default_registry()))
    src = without["properties"]["data_sources"]["items"]["properties"]
    assert "agent_script" not in src["source"]["description"]
    assert "agent_script" not in src["params"]["description"]

    with_it = _compose_schema_for(_C(build_default_registry(run_script=object())))
    src2 = with_it["properties"]["data_sources"]["items"]["properties"]
    assert "agent_script" in src2["source"]["description"]


def test_source_budget_is_derived_from_the_enclosing_timeouts():
    """codex P1: a hardcoded 45s assumed NOUS_TOOL_TIMEOUT=120. At the
    supported NOUS_TOOL_TIMEOUT=60 it let sources burn 45s and THEN start a
    60s compose LLM, so the outer wrapper cancelled the tool anyway."""
    from types import SimpleNamespace

    from nous.a2ui.sources import (
        _MIN_SOURCE_SECONDS,
        _TOTAL_SOURCE_SECONDS,
        _source_budget_seconds,
    )

    # The composer may run MAX_REPAIRS+1 = 3 LLM rounds, and ALL of them are
    # reserved — so the DEFAULT 120s tool timeout leaves sources nothing and
    # they get the floor. That is the honest answer: enabling agent_script on
    # a 120s tool timeout genuinely has no room, and prod runs 2000.
    assert _source_budget_seconds(
        SimpleNamespace(tool_timeout=120, a2ui_compose_timeout_seconds=60)
    ) == _MIN_SOURCE_SECONDS
    # the tight config codex named: likewise the floor
    assert _source_budget_seconds(
        SimpleNamespace(tool_timeout=60, a2ui_compose_timeout_seconds=60)
    ) == _MIN_SOURCE_SECONDS
    # prod (tool_timeout=2000) has ample room and stays CAPPED, never unbounded
    assert _source_budget_seconds(
        SimpleNamespace(tool_timeout=2000, a2ui_compose_timeout_seconds=60)
    ) == _TOTAL_SOURCE_SECONDS
    # no settings wired -> documented fallback
    assert _source_budget_seconds(None) == _TOTAL_SOURCE_SECONDS


async def test_agent_script_records_must_contain_objects():
    """codex P2: ["offline"] passed a list-only check, and a Repeat child
    binding a relative `name` against a scalar resolves to undefined — blank
    rows, and never routed through _script_failure so nothing reports why."""
    reg = build_default_registry(
        run_script=_FakeRunner({"ok": True, "result": ["offline"], "output": "", "error": None})
    )
    out = await reg.resolve(
        [{"key": "rows", "source": "agent_script",
          "params": {"code": "result = x", "shape": "records"}}]
    )
    assert out["rows"] == []  # shape-preserving failure, reason logged


async def test_shared_deadline_is_pushed_into_the_script_worker():
    """codex P1: `wait_for` cancels only the AWAIT — the worker thread keeps
    its run slot until its own 90s deadline, so concurrent slow refreshes
    starve every later run_python of slots. The shared budget must reach the
    worker itself."""
    runner = _FakeRunner({"ok": True, "result": [], "output": "", "error": None})
    reg = build_default_registry(run_script=runner)
    await reg.resolve(
        [{"key": "d", "source": "agent_script",
          "params": {"code": "result = []", "shape": "records"}}]
    )
    from nous.a2ui.sources import _TOTAL_SOURCE_SECONDS

    assert runner.last_timeout is not None, "worker ran on its own longer deadline"
    assert 0 < runner.last_timeout <= _TOTAL_SOURCE_SECONDS


def test_refresh_is_not_charged_for_compose_rounds():
    """codex P1: app.refresh runs NO LLM, but the budget reserved three
    compose rounds anyway — leaving refresh the 5s floor at default settings
    and cutting off the long-running external-API script the feature exists
    to keep live."""
    from types import SimpleNamespace

    from nous.a2ui.sources import (
        _MIN_SOURCE_SECONDS,
        _TOTAL_SOURCE_SECONDS,
        _source_budget_seconds,
    )

    cfg = SimpleNamespace(tool_timeout=120, a2ui_compose_timeout_seconds=60)
    assert _source_budget_seconds(cfg, for_compose=True) == _MIN_SOURCE_SECONDS
    # refresh reserves nothing for the LLM -> 120-0-10 = 110, capped at 45
    assert _source_budget_seconds(cfg, for_compose=False) == _TOTAL_SOURCE_SECONDS


async def test_worker_deadline_is_shorter_than_the_registry_wait():
    """codex P1: run_python waits `timeout + grace`, so handing the worker the
    SAME deadline as the registry's wait_for let the outer cancel win — a
    routine script timeout aborted the whole resolve instead of returning the
    promised shape-preserving failure."""
    from nous.a2ui.sources import _WORKER_DEADLINE_MARGIN, _source_budget_seconds

    runner = _FakeRunner({"ok": True, "result": [], "output": "", "error": None})
    reg = build_default_registry(run_script=runner)
    await reg.resolve(
        [{"key": "d", "source": "agent_script",
          "params": {"code": "result = []", "shape": "records"}}]
    )
    outer = _source_budget_seconds(None)
    assert runner.last_timeout <= outer - _WORKER_DEADLINE_MARGIN + 0.5


async def test_agent_script_rejects_series_points_that_are_not_objects():
    """codex P2: _downsample_series does `for k in p` / `p.get(...)`, so a null
    or int point raises INSIDE _bound_series — a 500 instead of a failure."""
    bad = {"kind": "series", "points": [{"t": "a", "v": 1}, None], "unit": "", "meta": {}}
    reg = build_default_registry(
        run_script=_FakeRunner({"ok": True, "result": bad, "output": "", "error": None})
    )
    out = await reg.resolve(
        [{"key": "d", "source": "agent_script",
          "params": {"code": "result = bad", "shape": "series", "series_keys": ["v"]}}]
    )
    assert is_series(out["d"]) and "declared shape 'series'" in out["d"]["meta"]["reason"]


async def test_declared_series_keys_are_enforced_on_every_resolve():
    """codex P2: refresh skips binding validation, so a result that changes
    series MODE (single <-> multi) or drops a LineChart key would leave the
    existing chart rendering nothing. The declared contract is enforced."""
    single = to_series([{"d": "2026-08-30", "x": 1.0}], "d", "x")
    reg = build_default_registry(
        run_script=_FakeRunner({"ok": True, "result": single, "output": "", "error": None})
    )
    # a chart declared on multi-series keys must reject a single-value refresh
    out = await reg.resolve(
        [{"key": "d", "source": "agent_script",
          "params": {"code": "result = s", "shape": "series",
                     "series_keys": ["success", "failure"]}}]
    )
    assert "no longer carries declared key" in out["d"]["meta"]["reason"]
    # and the matching contract passes untouched
    ok = await reg.resolve(
        [{"key": "d", "source": "agent_script",
          "params": {"code": "result = s", "shape": "series", "series_keys": ["v"]}}]
    )
    assert ok["d"]["points"], "a conforming series must pass through"


async def test_oversized_result_is_rejected_before_it_is_decoded(monkeypatch):
    """codex P1: the size check lived DOWNSTREAM of json.loads, so a rejected
    100MB result was still decoded on the event loop first."""
    from nous.api import tools as api_tools

    seen = {"loads": 0}
    real_loads = api_tools.json.loads

    def counting_loads(s, *a, **kw):
        seen["loads"] += 1
        return real_loads(s, *a, **kw)

    monkeypatch.setattr(api_tools.json, "loads", counting_loads)
    tools = api_tools.create_programmatic_tools(object(), object(), _settings_stub())
    out = await tools["run_script_structured"](
        "result = ['x' * 100] * 500", max_result_chars=200
    )
    assert out["ok"] is False and "max 200" in out["error"]
    assert seen["loads"] == 0, "oversized result was decoded before rejection"


def _settings_stub():
    from nous.config import Settings

    return Settings(_env_file=None)


async def test_series_script_must_declare_its_key_contract():
    """codex P2: an OPTIONAL series_keys meant no check when omitted — a first
    single-value result validates a Sparkline and a later multi-series refresh
    is accepted unchecked, leaving the chart empty."""
    reg = build_default_registry(
        run_script=_FakeRunner({"ok": True, "result": {}, "output": "", "error": None})
    )
    with pytest.raises(ValueError, match="requires params.series_keys"):
        await reg.resolve(
            [{"key": "d", "source": "agent_script",
              "params": {"code": "result = s", "shape": "series"}}]
        )


async def test_dashboard_scripts_cannot_exhaust_every_run_python_slot():
    """codex P1: the deadline pushed into the worker stops PYTHON-level code,
    but a script blocked in a C call (urlopen/sleep) cannot be interrupted at
    all — a documented run_python property this source makes far more
    reachable. Killing the thread is impossible, so the blast radius is bounded
    instead: dashboard scripts can never take the whole pool."""
    from nous.a2ui.sources import _INTERACTIVE_SLOT_RESERVE
    from nous.config import Settings

    capacity = Settings(_env_file=None).programmatic_tools_max_concurrent
    assert 0 < _INTERACTIVE_SLOT_RESERVE < capacity, "interactive use must keep capacity"

    # The gate must consult the GLOBAL in-flight count, because that is what a
    # blocked C-call worker keeps held — a local semaphore would release as
    # soon as the await returned and let refreshes stack blocked threads.
    import nous.api.tools as api_tools

    calls = {"n": 0}
    real = api_tools.run_python_active_runs
    api_tools.run_python_active_runs = lambda: capacity  # pool fully busy
    try:
        runner = _FakeRunner({"ok": True, "result": [], "output": "", "error": None})
        reg = build_default_registry(run_script=runner)
        out = await reg.resolve(
            [{"key": "d", "source": "agent_script",
              "params": {"code": "result = []", "shape": "records"}}]
        )
    finally:
        api_tools.run_python_active_runs = real
    assert out["d"] == [], "a busy pool must yield, not queue behind blocked workers"
    assert runner.calls == [], "the script must not have been started at all"
    assert calls["n"] == 0


async def test_decimal_series_values_stay_numbers(run_script_structured=None):
    """codex P2: `default=str` turned a Decimal reading into "1.5" — it
    persisted fine and passed every shape check, but the renderer accepts only
    finite JS numbers, so the chart silently drew nothing."""
    from nous.api.tools import create_programmatic_tools
    from nous.config import Settings

    tools = create_programmatic_tools(object(), object(), Settings(_env_file=None))
    out = await tools["run_script_structured"](
        "from decimal import Decimal\nresult = {'v': Decimal('1.5')}"
    )
    assert out["ok"] is True
    assert out["result"]["v"] == 1.5 and isinstance(out["result"]["v"], float)


async def test_malformed_series_keys_do_not_500():
    """codex P2: set([["value"]]) raises TypeError: unhashable list, escaping
    _script_failure — one malformed result turned compose/refresh into a
    server error despite the source's failure-isolation contract."""
    bad = {"kind": "series", "points": [{"t": "a", "v": 1}],
           "keys": [["value"]], "unit": "", "meta": {}}
    reg = build_default_registry(
        run_script=_FakeRunner({"ok": True, "result": bad, "output": "", "error": None})
    )
    out = await reg.resolve(
        [{"key": "d", "source": "agent_script",
          "params": {"code": "result = bad", "shape": "series", "series_keys": ["v"]}}]
    )
    assert is_series(out["d"]) and "list of strings" in out["d"]["meta"]["reason"]


async def test_script_cannot_hijack_the_json_decoder():
    """codex P1: the injected `json` is the shared MODULE, so a script can
    assign json.loads — and the post-worker decode would then run agent code on
    the event loop, after the deadline and run slot are gone."""
    from nous.api.tools import create_programmatic_tools
    from nous.config import Settings

    import json as _json_mod

    tools = create_programmatic_tools(object(), object(), Settings(_env_file=None))
    original_loads = _json_mod.loads
    try:
        out = await tools["run_script_structured"](
            "import json\n"
            "def _evil(*a, **k): raise RuntimeError('hijacked the decoder')\n"
            "json.loads = _evil\n"
            "result = {'ok': 1}"
        )
        # The script really did replace the module attribute...
        assert _json_mod.loads is not original_loads
        # ...and the decode still used the reference bound at import time.
        assert out["ok"] is True, out.get("error")
        assert out["result"] == {"ok": 1}
    finally:
        # The script mutates the REAL module in-process; leaving it patched
        # breaks pytest's own cache write at session end.
        _json_mod.loads = original_loads


async def test_script_cannot_hijack_the_decoder_internals():
    """codex P1: aliasing json.loads is not enough — the saved function still
    reads json._default_decoder at call time, so a script could swap THAT and
    have its decode() run on the event loop after the deadline is gone."""
    import json as _json_mod

    from nous.api.tools import create_programmatic_tools
    from nous.config import Settings

    tools = create_programmatic_tools(object(), object(), Settings(_env_file=None))
    original = _json_mod._default_decoder
    try:
        out = await tools["run_script_structured"](
            "import json\n"
            "class _Evil:\n"
            "    def decode(self, s): raise RuntimeError('hijacked internals')\n"
            "json._default_decoder = _Evil()\n"
            "result = {'ok': 2}"
        )
        assert _json_mod._default_decoder is not original
        assert out["ok"] is True, out.get("error")
        assert out["result"] == {"ok": 2}
    finally:
        _json_mod._default_decoder = original


async def test_a_pool_too_small_to_reserve_refuses_dashboard_scripts():
    """codex P1: at the supported MAX_CONCURRENT=1 the old threshold let an
    unattended script take the only slot; blocked in C code it would reject
    every interactive call indefinitely."""
    from types import SimpleNamespace

    runner = _FakeRunner({"ok": True, "result": [], "output": "", "error": None})
    reg = build_default_registry(
        run_script=runner,
        settings=SimpleNamespace(
            programmatic_tools_max_concurrent=1, tool_timeout=2000,
            a2ui_compose_timeout_seconds=60,
        ),
    )
    out = await reg.resolve(
        [{"key": "d", "source": "agent_script",
          "params": {"code": "result = []", "shape": "records"}}]
    )
    assert out["d"] == [] and runner.calls == [], "script ran on a pool with no reserve"


async def test_non_mapping_series_meta_is_rejected():
    """codex P2: `_downsample_series` does (meta or {}).get(...), so a truthy
    non-mapping meta raised AttributeError inside _bound_series — a 500."""
    bad = {"kind": "series", "points": [{"t": "a", "v": 1}], "unit": "", "meta": "bad"}
    reg = build_default_registry(
        run_script=_FakeRunner({"ok": True, "result": bad, "output": "", "error": None})
    )
    out = await reg.resolve(
        [{"key": "d", "source": "agent_script",
          "params": {"code": "result = bad", "shape": "series", "series_keys": ["v"]}}]
    )
    assert is_series(out["d"]) and "declared shape 'series'" in out["d"]["meta"]["reason"]


async def test_script_is_not_started_when_it_cannot_finish_in_time():
    """codex P1: a max(1.0, ...) floor inverted the worker-first ordering near
    the shared deadline — run_python waits `timeout + 2s` grace while the
    registry waits only `remaining`, so the registry timed out first and
    aborted the whole compose/refresh instead of reaching _script_failure."""
    runner = _FakeRunner({"ok": True, "result": [], "output": "", "error": None})
    reg = build_default_registry(run_script=runner)
    fetcher = reg._fetchers["agent_script"]
    out = await fetcher(
        {"code": "result = []", "shape": "records", "_remaining_seconds": 0.5}
    )
    assert out == [], "must yield the shape-preserving value"
    assert runner.calls == [], "a worker that cannot land in time must not start"


async def test_decoding_never_happens_on_the_event_loop():
    """codex P1: the 'trusted' decoder was an importable module global, so a
    script could `import nous.api.tools` and replace it — then its code would
    run on the loop after the deadline and slot were gone. Both encode AND
    decode now happen inside the worker."""
    from nous.api.tools import create_programmatic_tools
    from nous.config import Settings

    import nous.api.tools as _api_tools

    tools = create_programmatic_tools(object(), object(), Settings(_env_file=None))
    # The script mutates the REAL module in-process — that IS the attack being
    # tested — so it must be restored, or every later test decodes through the
    # stub. Same pollution the json.loads test already had to handle; a script
    # that rebinds a module global is a test fixture with global reach.
    original_cls = _api_tools._JSON_DECODER_CLS
    try:
        out = await tools["run_script_structured"](
            "import nous.api.tools as T\n"
            "class _Evil:\n"
            "    def decode(self, s): raise RuntimeError('hijacked the module global')\n"
            "T._JSON_DECODER_CLS = lambda: _Evil()\n"
            "result = {'ok': 3}"
        )
        # The rebind really landed on the module...
        assert _api_tools._JSON_DECODER_CLS is not original_cls
        # ...and the decode still succeeded, because it ran inside the worker
        # against the class captured before the script executed.
        assert out["ok"] is True, out.get("error")
        assert out["result"] == {"ok": 3}
    finally:
        _api_tools._JSON_DECODER_CLS = original_cls


# ---------------------------------------------------------------------------
# Issue #620 — two accept-and-degrade gaps
# ---------------------------------------------------------------------------


def test_inline_child_objects_are_a_repair_error_not_a_silent_drop():
    """#620 gap 2: `children` is reference-based, so inline child OBJECTS were
    filtered out by _children_of and nothing complained — no dangling ref (it
    never existed), no depth accounting. The section the model believed it
    filled validated clean and rendered EMPTY, with repairs:0."""
    comps = _skel([
        {"id": "c", "component": "Column",
         "children": [{"component": "Text", "text": "invisible"}]},
    ])
    errs = grammar.lint_micro_app(comps)
    assert any("inline child object" in e for e in errs)

    # A mixed array is caught too — the id ref alone used to make it look fine.
    mixed = _skel([
        {"id": "c", "component": "Column", "children": ["t1", {"component": "Text"}]},
        {"id": "t1", "component": "Text", "text": "visible"},
    ])
    assert any("inline child object" in e for e in grammar.lint_micro_app(mixed))


def test_id_reference_children_still_pass():
    ok = _skel([
        {"id": "c", "component": "Column", "children": ["t1"]},
        {"id": "t1", "component": "Text", "text": "visible"},
    ])
    assert grammar.lint_micro_app(ok) == []


def test_repeat_template_children_are_not_mistaken_for_inline_objects():
    """The {componentId, path} template is a DICT, not a list — it must keep
    working (F093 §6.2) rather than tripping the new list check."""
    comps = _skel([
        {"id": "c", "component": "Column",
         "children": {"componentId": "row", "path": "/items"}},
        {"id": "row", "component": "Text", "text": {"path": "name"}},
    ])
    assert not any("inline child" in e for e in grammar.lint_micro_app(comps))


def test_undeliverable_refine_options_are_rejected():
    """#620 gap 1: a refine option is not a dispatched action — app_refine
    appends its LABEL to the intent and re-composes against the SAME sources.
    A button promising a file or a message is unsatisfiable by construction,
    yet rendered pixel-identical to a real capability."""
    from nous.a2ui.compose import _refine_capability_errors

    for label in ("Export raw data", "Download CSV", "Email me a summary",
                  "Schedule a weekly digest"):
        errs = _refine_capability_errors([{"id": "x", "label": label}])
        assert errs, f"{label!r} should be rejected"
        assert "RE-RENDERS" in errs[0]


def test_legitimate_refine_options_still_pass():
    from nous.a2ui.compose import _refine_capability_errors

    for label in ("Compare periods", "Show only blockers", "Group by category",
                  "Last 7 days"):
        assert _refine_capability_errors([{"id": "x", "label": label}]) == [], label


def test_capability_gate_matches_commands_not_substrings():
    """codex P2: raw containment read "Group by sender" as send, "Email volume
    by week" as email and "Compare attachment types" as attach — all valid
    analytical labels for mail/file dashboards. Rejecting them is WORSE than
    the bug: repeated false matches burn the repair loop into a markdown
    fallback. Only command phrases count."""
    from nous.a2ui.compose import _refine_capability_errors

    for label in ("Group by sender", "Email volume by week",
                  "Compare attachment types", "Shared vs private items",
                  "Attachment size breakdown", "Sender leaderboard"):
        assert _refine_capability_errors([{"id": "x", "label": label}]) == [], label

    # ...while the phrasings that genuinely promise a capability still fail.
    for label in ("Send me the report", "Save to Drive", "Share via link",
                  "Subscribe to updates", "Notify me on change"):
        assert _refine_capability_errors([{"id": "x", "label": label}]), label


def test_leading_imperatives_without_a_pronoun_are_caught():
    """codex P2 round 2: requiring a pronoun ("email ME") missed the commonest
    imperative forms — "Email the report", "Notify the team", "Send report to
    Alice", "Schedule monthly digest" — which are just as undeliverable."""
    from nous.a2ui.compose import _refine_capability_errors

    for label in ("Email the report", "Notify the team", "Send report to Alice",
                  "Schedule monthly digest", "Post this to Slack",
                  "Deliver the digest to ops"):
        assert _refine_capability_errors([{"id": "x", "label": label}]), label

    # The same verbs used as NOUN MODIFIERS remain valid analytics.
    for label in ("Email volume by week", "Send rate by hour",
                  "Notifications per day", "Sender leaderboard"):
        assert _refine_capability_errors([{"id": "x", "label": label}]) == [], label


def test_lint_never_raises_on_inline_children_in_a_statrow():
    """codex P2: the inline-child error was recorded and then execution fell
    into the StatRow loop, where `by_id.get(dict)` raises TypeError. Lint runs
    BEFORE schema validation, so that escaped _validate and took the repair
    loop and the guaranteed fallback with it — a crash strictly worse than the
    silent drop being fixed. A lint pass must always RETURN errors, never raise."""
    comps = [
        {"id": "root", "component": "Column",
         "children": ["header", "stats", "sec", "footer"]},
        {"id": "header", "component": "AppHeader", "title": "t",
         "composedAt": {"path": "/meta/composedAt"}},
        {"id": "stats", "component": "StatRow",
         "children": [{"component": "StatTile", "label": "x"}]},
        {"id": "sec", "component": "Section", "title": "S", "child": "b"},
        {"id": "b", "component": "Text", "text": "x"},
        {"id": "footer", "component": "AppFooter"},
    ]
    errs = grammar.lint_micro_app(comps)  # must not raise
    assert any("inline child object" in e for e in errs)


def test_delivery_target_only_counts_in_an_imperative_clause():
    """codex P2: matching a delivery target ANYWHERE rejected "Compare email
    volume to last month" — from `email` through `to l` — which is a
    comparison, not a send."""
    from nous.a2ui.compose import _refine_capability_errors

    assert _refine_capability_errors(
        [{"id": "x", "label": "Compare email volume to last month"}]
    ) == []
    assert _refine_capability_errors([{"id": "x", "label": "Send report to Alice"}])


def test_file_generation_imperatives_are_caught():
    """codex P2: no component emits a file, so "Generate CSV" / "Create a PDF"
    / "Open in Excel" are as undeliverable as "Export" — pressing one just
    recomposes the same data."""
    from nous.a2ui.compose import _refine_capability_errors

    for label in ("Generate CSV", "Create a PDF", "Open in Excel",
                  "Produce an xlsx", "Build a spreadsheet"):
        assert _refine_capability_errors([{"id": "x", "label": label}]), label


def test_share_matching_is_start_anchored():
    """codex P2: "share with" anywhere rejected "Compare market share with last
    month" — analysis, not delivery."""
    from nous.a2ui.compose import _refine_capability_errors

    for label in ("Compare market share with last month", "Market share by region",
                  "Share of voice trend"):
        assert _refine_capability_errors([{"id": "x", "label": label}]) == [], label
    for label in ("Share via link", "Share with team", "Share to Slack"):
        assert _refine_capability_errors([{"id": "x", "label": label}]), label


# The full capability matrix. Four review rounds each found the same defect in
# a different token, so the rule became categorical — a verb counts as a
# command only when it OPENS the label (the sole exception being a pronoun
# object, which is imperative in every reading). This table is the guard
# against the next token being added unanchored.
_UNDELIVERABLE = [
    "Export raw data", "Download CSV", "Email me a summary", "Email the report",
    "Notify the team", "Send report to Alice", "Schedule monthly digest",
    "Schedule a weekly digest", "Save to Drive", "Share via link",
    "Share with team", "Subscribe to updates", "Generate CSV", "Create a PDF",
    "Open in Excel", "Remind me tomorrow", "Notify me on change",
    # Leading action verb with a BARE object — enumerating the words that may
    # follow could never keep up, so a leading action verb is a command unless
    # the label reads analytically.
    "Email report", "Notify stakeholders", "Remind Alice", "Schedule digest",
    "Post to Slack", "Deliver weekly",
    # A deliverable OBJECT is not an analytical marker, and "by <weekday>" is a
    # deadline rather than a grouping.
    "Email summary", "Send overview", "Schedule review by Monday",
    # Calendar/reminder commands — no component writes a calendar either.
    "Add to calendar", "Set a reminder", "Create calendar event", "Book a slot",
    "Print this", "Upload the file",
    # Mutations — a micro-app is read-only by construction (the grammar bans
    # every input component), so these are as undeliverable as an export.
    "Archive completed tasks", "Delete old records", "Approve request",
    "Mark all as read", "Dismiss warnings", "Cancel the run", "Retry failed nodes",
]

_ANALYTICAL = [
    "Group by sender", "Email volume by week", "Send rate by hour",
    "Compare attachment types", "Compare market share with last month",
    "Market share by region", "Share of voice trend",
    "Compare subscribe vs purchase conversion", "Subscribe clicks by source",
    "Subscribe-to-purchase funnel", "On-call schedule this month",
    "Schedule adherence by team", "Compare save to disk latency",
    "Compare export as csv counts", "Compare email volume to last month",
    "Compare periods", "Last 7 days", "Notifications per day",
    "Sender leaderboard", "Create-time distribution",
    # ...and the same verbs reading analytically must still survive.
    "Subscribe clicks by source", "Subscribe-to-purchase funnel",
    "Email open rate", "Notification volume trend",
    "Schedule adherence by team", "Delivery rate by day",
    # The file/print verbs are metrics when the label reads analytically.
    "Print volume by department", "Download counts by file type",
    "Export trends by month", "Event volume by day", "Meeting count per team",
    "Calendar density heatmap",
    # The same mutation verbs reading analytically are metrics.
    "Approval rate by reviewer", "Delete volume per day", "Resolution time trend",
    "Close rate by team", "Retry rate by node",
]


@pytest.mark.parametrize("label", _UNDELIVERABLE)
def test_undeliverable_labels_are_rejected(label):
    from nous.a2ui.compose import _refine_capability_errors

    assert _refine_capability_errors([{"id": "x", "label": label}]), label


@pytest.mark.parametrize("label", _ANALYTICAL)
def test_analytical_labels_are_never_rejected(label):
    """A false positive is worse than a false negative here: repeated
    rejection exhausts the repair loop and replaces a valid dashboard with the
    markdown fallback — losing the whole app to police one button."""
    from nous.a2ui.compose import _refine_capability_errors

    assert _refine_capability_errors([{"id": "x", "label": label}]) == [], label


def test_undeliverable_options_are_dropped_not_failed():
    """#620 + 8 review rounds: no lexical rule separates "Print volume by
    department" (a metric) from "Schedule report distribution across teams" (a
    command) — same shape, semantic difference. So the classifier is treated as
    a HEURISTIC: it drops the option instead of failing validation. Wiring a
    heuristic into _validate made every misjudgement cost the whole app, since
    repeated rejections exhaust the repair loop into the markdown fallback."""
    from nous.a2ui.compose import _clean_refine_options

    kept = _clean_refine_options([
        {"id": "a", "label": "Export raw data"},
        {"id": "b", "label": "Compare periods"},
        {"id": "c", "label": "Email me a summary"},
        {"id": "d", "label": "Group by category"},
    ])
    assert [o["label"] for o in kept] == ["Compare periods", "Group by category"]


def test_capability_heuristic_never_reaches_the_repair_loop(settings):
    """A misjudged label must never be able to cost the whole app."""
    from nous.a2ui.compose import SurfaceComposer
    from nous.a2ui.sources import SourceRegistry

    composer = SurfaceComposer(object(), settings, SourceRegistry())
    parsed = {
        "components": [{"id": "x", "component": "Text", "text": "hi"}],
        "refine_options": [{"id": "a", "label": "Email me the report"}],
    }
    assert not any("refine" in e.lower() for e in composer._validate(parsed, {}))


def test_command_pattern_never_matches_the_empty_string():
    """A regex assembled from `|`-joined fragments will match EVERYTHING if a
    branch is ever removed and leaves a leading `|` — an empty first
    alternative. That happened while editing and silently rejected all 22
    analytical labels; this asserts the shape rather than the behaviour."""
    from nous.a2ui.compose import _REFINE_COMMAND_RE

    assert _REFINE_COMMAND_RE.search("") is None
    assert not _REFINE_COMMAND_RE.pattern.startswith("|")


@pytest.mark.parametrize("victim", ["root", "stats", "body", "sec"])
def test_lint_never_raises_for_inline_children_anywhere(victim):
    """CATEGORY test. An inline child object was fixed once for StatRow and
    then crashed again at the root skeleton lookup — `x in by_id` HASHES x, so
    every by_id access keyed by a child value is the same latent TypeError.
    Lint runs before schema validation, so such a crash escapes _validate and
    takes the repair loop AND the guaranteed fallback with it. This sweeps
    every child-bearing position instead of chasing them one at a time."""
    inline = {"component": "Text", "text": "inline"}
    comps = [
        {"id": "root", "component": "Column",
         "children": ["header", "stats", "sec", "footer"]},
        {"id": "header", "component": "AppHeader", "title": "t",
         "composedAt": {"path": "/meta/composedAt"}},
        {"id": "stats", "component": "StatRow", "children": ["t1"]},
        {"id": "t1", "component": "StatTile", "label": "x", "value": "1"},
        {"id": "sec", "component": "Section", "title": "S", "child": "body"},
        {"id": "body", "component": "Column", "children": ["t2"]},
        {"id": "t2", "component": "Text", "text": "x"},
        {"id": "footer", "component": "AppFooter"},
    ]
    for comp in comps:
        if comp["id"] == victim:
            comp["children"] = [*comp.get("children", []), inline]

    errs = grammar.lint_micro_app(comps)  # must RETURN, never raise
    assert any("inline child object" in e for e in errs)


async def test_full_iso_series_survives_undownsampled():
    """The point of the 6k→12k per-source raise: a 200-point ISO-stamped
    series (~9.9k chars) was ALWAYS downsampled to ~120 points even as the
    only source on the surface. It must now arrive intact."""
    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 3, 1, tzinfo=UTC)
    recs = [{"d": base + timedelta(days=i), "x": float(i)} for i in range(200)]
    reg = SourceRegistry()

    async def fetch(params):
        return to_series(recs, "d", "x", unit="bpm")

    reg.register("hr", fetch)
    out = await reg.resolve([{"key": "hr", "source": "hr", "params": {}}])
    assert len(out["hr"]["points"]) == 200, "still being downsampled below the cap"
    assert out["hr"]["meta"]["downsampled_from"] is None


def test_tabs_pass_the_grammar_with_referenced_children():
    """Tabs were allowed but never prompted; the grammar must accept the shape
    the new prompt teaches — tabs: [{title, child-id}] — with the one-parent
    and reference rules applying to tab children like any other child."""
    comps = _skel([
        {"id": "c", "component": "Tabs", "tabs": [
            {"title": "By day", "child": "t_day"},
            {"title": "By category", "child": "t_cat"},
        ]},
        {"id": "t_day", "component": "Text", "text": "day view"},
        {"id": "t_cat", "component": "Text", "text": "category view"},
    ])
    assert grammar.lint_micro_app(comps) == []

    # A tab child claimed by ANOTHER parent must still be rejected.
    double = _skel([
        {"id": "c", "component": "Column", "children": ["tabs1", "t_day"]},
        {"id": "tabs1", "component": "Tabs", "tabs": [{"title": "d", "child": "t_day"}]},
        {"id": "t_day", "component": "Text", "text": "x"},
    ])
    assert any("parent" in e or "referenced" in e for e in grammar.lint_micro_app(double))


def test_accordion_is_a_valid_section_layout():
    comps = _skel([{"id": "c", "component": "Text", "text": "detail"}])
    for comp in comps:
        if comp["id"] == "sec":
            comp["layout"] = "accordion"
    assert grammar.lint_micro_app(comps) == []


def test_prompt_teaches_tabs_and_accordion(settings):
    from nous.a2ui.compose import _GRAMMAR_RULES

    assert "accordion" in _GRAMMAR_RULES
    assert "TABS" in _GRAMMAR_RULES
    assert '"child": "<component id>"' in _GRAMMAR_RULES
