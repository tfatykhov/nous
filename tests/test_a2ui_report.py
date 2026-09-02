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
