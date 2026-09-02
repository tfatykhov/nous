"""F096 report vocabulary — schema strictness and the whole-feature fixture.

Spec: docs/features/F096-report-vocabulary.md. The strictness tests pin §2:
the envelope's ``unevaluatedProperties: false`` (agent_to_renderer.json) is
what keeps F093 §2 honest for the five new components — a ``style`` or
``color`` key is a schema rejection, not a linter opinion — and the one
STATIC array (``DataTable.columns``) is closed by its own item schema.
"""

from __future__ import annotations

from typing import Any

import pytest

from nous.a2ui.validator import NOUS_CORE_CATALOG_ID, validate_envelope


def _envelope(components: list[dict], data_model: dict | None = None) -> dict:
    create: dict[str, Any] = {
        "surfaceId": "s1",
        "catalogId": NOUS_CORE_CATALOG_ID,
        "components": components,
    }
    if data_model is not None:
        create["dataModel"] = data_model
    return {"version": "v1.0", "createSurface": create}


_REPORT_MINIMAL: dict[str, dict] = {
    "MetricCard": {"label": "L", "value": "1"},
    "ScoreCard": {"title": "T", "status": "on track"},
    "DeltaList": {"rows": {"path": "/rows"}},
    "DataTable": {"columns": [{"key": "a", "label": "A"}], "rows": {"path": "/rows"}},
    "ChipRow": {"items": {"path": "/rows"}},
}


def _surface(comp: dict) -> list[dict]:
    return [{"id": "root", "component": "Column", "children": ["c"]}, comp]


@pytest.mark.parametrize("name", sorted(_REPORT_MINIMAL))
def test_report_components_validate_and_reject_styling(name: str) -> None:
    comp = {"id": "c", "component": name, **_REPORT_MINIMAL[name], "weight": 1}
    assert validate_envelope(_envelope(_surface(comp))) == []
    for bad in ("style", "color"):
        errors = validate_envelope(_envelope(_surface({**comp, bad: "red"})))
        assert errors, f"{name}.{bad} was accepted"


def test_datatable_column_entries_are_closed() -> None:
    comp = {
        "id": "c",
        "component": "DataTable",
        "rows": {"path": "/rows"},
        "columns": [{"key": "a", "label": "A", "color": "red"}],
    }
    assert validate_envelope(_envelope(_surface(comp)))
    seven = {**comp, "columns": [{"key": f"k{i}", "label": str(i)} for i in range(7)]}
    assert validate_envelope(_envelope(_surface(seven)))
    bad_align = {**comp, "columns": [{"key": "a", "label": "A", "align": "middle"}]}
    assert validate_envelope(_envelope(_surface(bad_align)))


def test_report_affordances_on_existing_components_validate() -> None:
    """Section.caption (DynamicString) + layout cards, AppHeader.note,
    Sparkline/MetricCard trendline — schema-level additions (spec §4)."""
    components = [
        {"id": "root", "component": "Column", "children": ["h", "s", "sp"]},
        {
            "id": "h",
            "component": "AppHeader",
            "title": "T",
            "composedAt": {"path": "/meta/composedAt"},
            "note": {"path": "/meta/reach"},
        },
        {
            "id": "s",
            "component": "Section",
            "title": "Goals",
            "child": "m",
            "layout": "cards",
            "caption": {"path": "/meta/window"},
        },
        {"id": "sp", "component": "Sparkline", "path": "/series", "trendline": True},
        {
            "id": "m",
            "component": "MetricCard",
            "label": "L",
            "value": "1",
            "trend": "trend",
            "trendline": True,
            "tone": "ok",
        },
    ]
    assert validate_envelope(_envelope(components)) == []
    bad_tone = [dict(c, tone="purple") if c["id"] == "m" else c for c in components]
    assert validate_envelope(_envelope(bad_tone))
    bad_layout = [dict(c, layout="masonry") if c["id"] == "s" else c for c in components]
    assert validate_envelope(_envelope(bad_layout))


def test_card_tone_may_be_a_path_binding_but_the_literal_stays_closed() -> None:
    """A metric grid is ONE template under a Repeat, so a literal-only tone
    would paint every card the same; MetricCard/ScoreCard admit a bare
    {path} binding (closed at render by normalizeTone) while a literal must
    still be in the enum and anything else is rejected."""
    for name, base in (("MetricCard", {"label": "L", "value": "1"}), ("ScoreCard", {"title": "T", "status": "s"})):
        ok = {"id": "c", "component": name, **base, "tone": {"path": "tone"}}
        assert validate_envelope(_envelope(_surface(ok))) == [], name
        for bad in ("purple", {"path": ""}, {"path": "tone", "color": "red"}, {"call": "x"}, 3):
            assert validate_envelope(_envelope(_surface({**ok, "tone": bad}))), (name, bad)


# ---------------------------------------------------------------------------
# DSL helpers (spec §8.2)
# ---------------------------------------------------------------------------


def test_dsl_helpers_emit_the_schema_shape_and_drop_none() -> None:
    from nous.a2ui import dsl

    assert dsl.MetricCard("m", label="L", value="1") == {
        "id": "m", "component": "MetricCard", "label": "L", "value": "1"
    }
    full = dsl.MetricCard(
        "m", label="L", value="1", unit="s", delta="↓1", tone="ok", caption="c",
        trend="trend", trendline=True, footnote="f",
    )
    assert full["trend"] == "trend" and full["trendline"] is True
    assert dsl.ScoreCard("s", title="T", status="ok") == {
        "id": "s", "component": "ScoreCard", "title": "T", "status": "ok"
    }
    assert dsl.DeltaList("d", rows={"path": "/r"}, empty_text="none") == {
        "id": "d", "component": "DeltaList", "rows": {"path": "/r"}, "emptyText": "none"
    }
    cols = [{"key": "a", "label": "A", "align": "end"}]
    assert dsl.DataTable("t", columns=cols, rows={"path": "/r"}) == {
        "id": "t", "component": "DataTable", "columns": cols, "rows": {"path": "/r"}
    }
    assert dsl.ChipRow("c", items={"path": "/i"}) == {
        "id": "c", "component": "ChipRow", "items": {"path": "/i"}
    }
    assert dsl.Section("s", title="T", child="c", caption={"path": "/w"})["caption"] == {"path": "/w"}
    assert "caption" not in dsl.Section("s", title="T", child="c")
    assert dsl.AppHeader("h", title="T", composedAt={"path": "/m"}, note="n")["note"] == "n"
    assert dsl.Sparkline("sp", path="/s", trendline=True)["trendline"] is True
    assert "trendline" not in dsl.Sparkline("sp", path="/s")


# ---------------------------------------------------------------------------
# Prompt, summary, tool schema (spec §8.1 / §8.2 / §5)
# ---------------------------------------------------------------------------


def _composer():
    from types import SimpleNamespace

    from nous.a2ui.compose import SurfaceComposer
    from nous.a2ui.sources import SourceRegistry

    return SurfaceComposer(None, SimpleNamespace(), SourceRegistry())


def test_prompt_carries_the_report_block_and_both_tone_enums() -> None:
    prompt = _composer()._build_prompt("28-day trend report", "report", {})
    for needle in (
        "REPORT apps",
        "BARE STRING",
        "intent ∈ neutral|good|bad|warn",
        "tone ∈ neutral|ok|warn|crit",
        "layout cards",
        "- report —",
        '"report"',  # response-shape archetype union
        "- report: cool slate dark",
        "NEVER root children",
    ):
        assert needle in prompt, needle


def test_source_description_is_capped_per_source_and_tags_embedded_series() -> None:
    from nous.a2ui.sources import to_series

    metrics = [{"label": "m", "trend": to_series([{"t": "2026-08-01", "v": 1.0}], "t", "v")}]
    big = [{"k": "x" * 5000}] * 3
    prompt = _composer()._build_prompt("x", None, {"metrics": metrics, "big": big, "tail": [{"a": 1}]})
    assert '"metrics" [records; each carries a series at: trend]' in prompt
    assert '"tail" [records]' in prompt  # the last source survives the cap
    assert "…(truncated)" in prompt
    assert '"series — chartable"' not in prompt  # tags are unquoted markers
    series_only = _composer()._build_prompt("x", None, {"hr": to_series([{"t": "2026-08-01", "v": 1.0}], "t", "v")})
    assert '"hr" [series — chartable]' in series_only
    # ensure_ascii=False: the arrows the recipe writes stay one char each
    arrows = _composer()._build_prompt("x", None, {"m": [{"delta": "↓0.6 s · improving"}]})
    assert "↓0.6 s · improving" in arrows and "\\u2193" not in arrows


def test_catalog_summary_names_the_report_components() -> None:
    from nous.a2ui.catalog_summary import catalog_property_summary

    summary = catalog_property_summary()
    assert "- MetricCard: required label, value" in summary
    assert "- ScoreCard: required title, status" in summary
    assert "- DeltaList: required rows" in summary
    assert "- DataTable: required columns, rows" in summary
    assert "- ChipRow: required items" in summary
    assert "- Section: required title, child; optional provenance, caption, layout" in summary
    assert "- Sparkline: required path; optional label, tone, trendline" in summary
    assert "- AppHeader: required title, composedAt; optional subtitle, staleAfterS, note" in summary


def test_compose_surface_tool_offers_the_report_archetype_and_records_shape() -> None:
    from nous.a2ui.tools import _COMPOSE_SURFACE_SCHEMA

    arch = _COMPOSE_SURFACE_SCHEMA["properties"]["archetype"]
    assert "report" in arch["enum"] and "report (" in arch["description"]
    params = _COMPOSE_SURFACE_SCHEMA["properties"]["data_sources"]["items"]["properties"]["params"]
    assert "TOP-LEVEL" in params["description"] and "embed a `trend` series" in params["description"]


# ---------------------------------------------------------------------------
# Whole-feature fixture (spec §9 AC1) — every new component, cards layout,
# caption, note, a Repeat over metric records with embedded series (one
# record WITHOUT a trend), a ScoreCard record WITHOUT items, trendline,
# focus_from, archetype + theme `report`. Exported as JSON for the Svelte
# whole-app render test (`python -m tests.test_a2ui_report` prints it).
# ---------------------------------------------------------------------------


def report_app_components() -> list[dict]:
    from nous.a2ui import dsl

    return [
        {
            "id": "root",
            "component": "Column",
            "children": ["header", "goals", "movers", "retrieval", "sleep", "lanes", "footer"],
        },
        dsl.AppHeader(
            "header",
            title="Memory & decisions — trend report",
            subtitle="every metric judged over the last 28 days against the 28 before",
            composedAt={"path": "/meta/composedAt"},
            note={"path": "/meta/reach"},
        ),
        dsl.Section(
            "goals", title="Goals", child="goals-col", layout="cards",
            caption={"path": "/meta/window"}, provenance="source",
        ),
        {"id": "goals-col", "component": "Column", "children": {"componentId": "goal", "path": "/goals"}},
        dsl.ScoreCard(
            "goal",
            title={"path": "title"},
            status={"path": "status"},
            tone={"path": "tone"},
            value={"path": "value"},
            unit={"path": "unit"},
            caption={"path": "caption"},
            items={"path": "items"},
            note={"path": "note"},
        ),
        dsl.Section(
            "movers", title="Movers", child="movers-row", layout="grid-2",
            caption="significant moves", provenance="source",
        ),
        {"id": "movers-row", "component": "Row", "children": ["up", "down"]},
        dsl.DeltaList("up", rows={"path": "/up"}),
        dsl.DeltaList("down", rows={"path": "/down"}, empty_text="no significant adverse moves"),
        dsl.Section(
            "retrieval", title="Retrieval", child="ret-col", layout="cards",
            caption="retrieval_log", provenance="source",
        ),
        {"id": "ret-col", "component": "Column", "children": {"componentId": "metric", "path": "/retrieval"}},
        dsl.MetricCard(
            "metric",
            label={"path": "label"},
            value={"path": "value"},
            unit={"path": "unit"},
            delta={"path": "delta"},
            tone={"path": "tone"},
            caption={"path": "caption"},
            trend="trend",
            trendline=True,
            footnote={"path": "footnote"},
        ),
        dsl.Section(
            "sleep", title="Sleep cycles", child="sleep-table", layout="accordion",
            caption="last 4 nights", provenance="source",
        ),
        dsl.DataTable(
            "sleep-table",
            columns=[
                {"key": "night", "label": "Night"},
                {"key": "facts", "label": "Facts merged", "align": "end"},
                {"key": "edges", "label": "Edges", "align": "end"},
                {"key": "phases", "label": "Phases", "secondary": True},
            ],
            rows={"path": "/sleep"},
            empty_text="no sleep cycles in window",
        ),
        dsl.Section(
            "lanes", title="Data freshness", child="lanes-col",
            caption="a stale lane silently freezes its trend", provenance="source",
        ),
        {"id": "lanes-col", "component": "Column", "children": ["chips", "method"]},
        dsl.ChipRow("chips", items={"path": "/lanes"}),
        {
            "id": "method",
            "component": "Text",
            "text": "Trend method: last 28 days vs the 28 before; means for counts, medians for latency.",
            "variant": "caption",
        },
        dsl.AppFooter("footer"),
    ]


def report_app_sources() -> dict:
    """The server-resolved sources: one record list per panel (spec §6.1)."""
    from datetime import date, timedelta

    from nous.a2ui.sources import to_series

    start = date(2026, 7, 7)

    def spark(values: list[float | None]) -> dict:
        rows = [{"t": start + timedelta(days=i), "v": v} for i, v in enumerate(values)]
        return to_series(rows, "t", "v", unit="s", focus_from=date(2026, 8, 4))

    latency = [6.0 - i * 0.02 + (0.3 if i % 7 == 0 else 0.0) for i in range(56)]
    failed: list[float | None] = [1.0 + (i % 5) * 0.4 for i in range(56)]
    failed[19] = failed[20] = None  # a real gap: the scheduler was down
    return {
        "goals": [
            {
                "title": "Decision quality", "status": "on track", "tone": "ok",
                "value": "0.24", "unit": "Brier", "caption": "28d mean · ↓0.03 vs prior 28d",
                "items": [
                    {"label": "Brier score", "value": "↓0.03", "tone": "ok"},
                    {"label": "Reviewed within 7d", "value": "↑6 %", "tone": "ok"},
                    {"label": "Avg confidence", "value": "↑0.02", "tone": "neutral"},
                ],
                "note": "Brier is the honest scoreboard — confidence alone can't tell calibration from bravado.",
            },
            {
                "title": "Reliability", "status": "slipping", "tone": "crit",
                "items": [
                    {"label": "DAG nodes failed", "value": "↑0.8 /day", "tone": "crit"},
                    {"label": "Reaper fires", "value": "↑3", "tone": "crit"},
                ],
                "note": "Latency is improving but the failure lanes moved the wrong way.",
            },
            # No evidence rows: the verdict alone is a legal card (spec §3.2).
            {"title": "Memory growth", "status": "no change", "tone": "neutral", "value": "41", "unit": "facts/day"},
        ],
        "up": [
            {"label": "Recall p50 latency", "delta": "↓0.6 s", "from": "5.9", "to": "5.3", "tone": "ok"},
            {"label": "Reviewed within 7d", "delta": "↑6 %", "from": "61", "to": "67", "tone": "ok"},
            {"label": "Brier score", "delta": "↓0.03", "from": "0.27", "to": "0.24", "tone": "ok"},
        ],
        "down": [],
        "retrieval": [
            {"label": "Recall p50", "value": "5.3", "unit": "s", "delta": "↓0.6 s · improving", "tone": "ok",
             "caption": "5.9 → 5.3 (28d median, n=28)", "footnote": "last 2026-09-01", "trend": spark(latency)},
            {"label": "Nodes failed", "value": "2", "unit": "/day", "delta": "↑0.8 · worsening", "tone": "crit",
             "caption": "1.1 → 1.9 (28d mean, n=26) · 2 gaps", "footnote": "last 2026-09-01", "trend": spark(failed)},
            {"label": "Rendered candidates", "value": "24", "delta": "↑1 · holding steady", "tone": "neutral",
             "caption": "23 → 24 (28d mean, n=28)", "footnote": "last 2026-09-01",
             "trend": spark([22.0 + (i % 4) for i in range(56)])},
            # A count mixed into the grid: no trend (spec §3.1).
            {"label": "Open DAGs", "value": "3", "delta": "", "tone": "neutral",
             "caption": "right now", "footnote": ""},
        ],
        "sleep": [
            {"night": "2026-08-31", "facts": "14", "edges": "212", "phases": "reflect, stale-scan, graph backfill"},
            {"night": "2026-08-30", "facts": "9", "edges": "188", "phases": "reflect, contradictions"},
            {"night": "2026-08-29", "facts": "21", "edges": "301", "phases": "reflect, stale-scan, key sweep"},
            {"night": "2026-08-28", "facts": "0", "edges": "40", "phases": "reflect"},
        ],
        "lanes": [
            {"label": "retrieval_log", "value": "today", "detail": "14d window", "tone": "ok"},
            {"label": "eval_runs", "value": "3d ago", "detail": "regression baseline", "tone": "warn"},
            {"label": "consolidation_cycles", "value": "today", "detail": "audit on", "tone": "ok"},
        ],
    }


FIXTURE_JSON = "dashboard-app/src/companion/catalog/__fixtures__/f096-report-app.json"

REPORT_APP_META = {
    "composedAt": "2026-09-01T13:00:00Z",
    "reach": "data through 2026-09-01",
    "window": "28d vs prior 28d",
}


def report_app_fixture_json() -> str:
    """The Svelte whole-app test's input: components + the FULL data model."""
    import json

    return json.dumps(
        {
            "components": report_app_components(),
            "dataModel": {"meta": REPORT_APP_META, **report_app_sources()},
        },
        ensure_ascii=False,
        indent=1,
    )


def test_whole_feature_report_app_passes_every_gate() -> None:
    """AC1: lint → _validate (grammar + data-aware rules + probe schema) →
    validate_envelope on the built surface, with every record kept by the
    per-source budget."""
    from nous.a2ui import sources as src
    from nous.a2ui.compose import SurfaceComposer
    from nous.a2ui.grammar import lint_micro_app
    from nous.a2ui.sources import SourceRegistry
    from nous.config import Settings

    comps = report_app_components()
    sources = report_app_sources()
    assert lint_micro_app(comps, archetype="report") == []

    composer = SurfaceComposer(None, Settings(_env_file=None), SourceRegistry())
    parsed = {
        "title": "Memory & decisions — trend report",
        "archetype": "report",
        "theme": "report",
        "components": comps,
        "dataModel": {},
        "refine_options": [{"id": "last-7", "label": "Last 7 days"}],
    }
    assert composer._validate(parsed, sources) == []

    envelope = _envelope(comps, data_model={"meta": REPORT_APP_META, **sources})
    assert validate_envelope(envelope) == []

    # Every panel source fits its budget with every record intact (§6.1).
    for key, value in sources.items():
        bounded, size = src._bound(value, src._PER_SOURCE_BUDGET_CHARS)
        assert size <= src._PER_SOURCE_BUDGET_CHARS, key
        assert len(bounded) == len(value) and not any(
            isinstance(r, dict) and r.get("_truncated") for r in bounded
        ), key
    for rec in sources["retrieval"][:3]:
        assert rec["trend"]["meta"]["focus_from"] == "2026-08-04"
        assert rec["trend"]["meta"]["downsampled_from"] is None


def test_whole_feature_fixture_json_is_current() -> None:
    """The Svelte whole-app test renders the exported JSON; a stale export
    would test yesterday's fixture. Regenerate with
    `PYTHONPATH=. uv run python tests/test_a2ui_report.py > <FIXTURE_JSON>`."""
    from pathlib import Path

    exported = Path(__file__).resolve().parents[1] / FIXTURE_JSON
    assert exported.exists(), "fixture JSON not exported"
    assert exported.read_text(encoding="utf-8").strip() == report_app_fixture_json().strip()


if __name__ == "__main__":  # pragma: no cover — fixture export for vitest
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    print(report_app_fixture_json())
