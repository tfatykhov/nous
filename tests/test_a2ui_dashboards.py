"""F093 + F094 — dashboard vocabulary: series normalizer, chart grammar,
themes, Repeat, layout, and the data-aware compose rules.

Most of this is pure (no DB); the theme-wire tests use the Postgres service
fixtures like the other A2UI suites.
"""

from __future__ import annotations

import math
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete

from nous.a2ui import grammar
from nous.a2ui.compose import _THEMES, _binding_rules, _collect_bindings
from nous.a2ui.sources import (
    _bound_series,
    _pivot,
    empty_series,
    is_series,
    to_series,
)
from nous.storage.models import A2uiAction, A2uiOutbox, A2uiSurface

# ---------------------------------------------------------------------------
# to_series (F094 §4) — the general normalizer
# ---------------------------------------------------------------------------


def test_to_series_sorts_drops_nonfinite_and_counts():
    recs = [
        {"d": "2026-08-03", "x": 3.0},
        {"d": "2026-08-01", "x": 1.0},
        {"d": "2026-08-02", "x": float("nan")},
    ]
    s = to_series(recs, "d", "x", unit="bpm")
    assert is_series(s)
    assert [p["t"] for p in s["points"]] == ["2026-08-01", "2026-08-03"]
    assert s["meta"]["dropped"] == 1
    assert s["unit"] == "bpm"
    # never coerces a dropped reading to zero
    assert all(math.isfinite(p["v"]) for p in s["points"])


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


def test_repeat_template_binding_exempts_over_capacity():
    big = {"items": [{"i": i} for i in range(12)]}
    # a template binds /items (no index) → all render → no over-capacity error
    comps = [{"id": "list", "component": "Column", "children": {"componentId": "row", "path": "/items"}}]
    errs = _binding_rules(comps, big, big, _collect_bindings(comps))
    assert not any("only" in e for e in errs)


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
