# F096 Report Vocabulary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give composed micro-apps the five element classes a periodic trend report is made of (metric card, scorecard, movers list, table, status chips), plus the section/header/sparkline affordances and the `report` theme + archetype, so any "how are things moving" report renders live in the companion instead of as emailed HTML.

**Architecture:** Five new closed-enum `nous-core` components follow the F093/F094 pattern exactly — the model supplies preformatted strings, array bindings and a `tone`; the Svelte renderer owns geometry, colour and every degenerate state; compose-time data-aware validation turns wrong shapes into repair-loop messages. One shared path-target resolver (walking every child key the grammar walks) serves charts and the new list components. A series-aware `_bound` keeps every metric record and shortens its embedded spark instead of dropping records.

**Tech Stack:** Python 3.12+ (jsonschema, pydantic-settings), Svelte 5 + TypeScript (vitest, @testing-library/svelte), JSON Schema 2020-12 catalogs.

**Spec:** `docs/features/F096-report-vocabulary.md` — every task below cites its section.

## Global Constraints

- No `style`, `color`, size or geometry prop on any component (spec §2). Tones are the closed enum `neutral | ok | warn | crit`; `StatTile.intent` is untouched.
- Every string value is preformatted by the agent; the renderer never rounds, re-signs or adds units (spec §6.3).
- `lint_micro_app` returns errors and **never raises** on malformed model output (spec §7.1).
- No area fill under a sparkline (spec §4.3). End dot is `<circle class="end">`, Sparkline partial only, skipped when coincident with an isolated-reading dot.
- Every enumeration site in spec §4.2 (layout), §5 (theme, archetype) is updated in the same task that adds the value.
- Worktree: `E:\Projects\nous\.claude\worktrees\f096-report-vocabulary`, branch `feat/F096-report-vocabulary`. All paths below are relative to it. Python: `uv run pytest …`; frontend: `cd dashboard-app && npx vitest run …`.
- Commit style: `feat(a2ui): …`, `test(a2ui): …`; trailers `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01DorWqbSbdq9dSKxCJuaQYr`.

---

## File map

| File | Responsibility in this feature |
|---|---|
| `nous/a2ui/catalogs/nous_core/catalog.json` | Schemas: 5 new components (+`weight`), `Section.caption`/`layout: cards`, `AppHeader.note`, `Sparkline.trendline`, `$defs.anyComponent.oneOf` |
| `nous/a2ui/grammar.py` | `ALLOWED_COMPONENTS`, `caps_for` third tier, DataTable columns lint (guarded), blank `trend` lint |
| `nous/a2ui/dsl.py` | Builders for the 5 components; `caption`/`note`/`trendline` kwargs |
| `nous/a2ui/sources.py` | `to_series(focus_from=)`, series-aware `_bound` |
| `nous/a2ui/compose.py` | `_resolve_path_targets` shared resolver, the four prop maps, array + column rules, prompt blocks, per-source sample cap, embedded-series tag, `_THEMES`/`_ARCHETYPES` |
| `nous/a2ui/catalog_summary.py` | `_COMPONENT_ORDER` |
| `nous/a2ui/tools.py` | archetype enum + gloss; `shape` description |
| `dashboard-app/src/companion/chart.ts` | `readSeries.focusFrom`, `rollingMean`, `focusStartIndex` |
| `dashboard-app/src/companion/catalog/SparkSvg.svelte` | NEW shared SVG partial (end dot, trendline, focus window) |
| `dashboard-app/src/companion/catalog/{Sparkline,Section,AppHeader}View.svelte` | consume the partial / new props |
| `dashboard-app/src/companion/catalog/{MetricCard,ScoreCard,DeltaList,DataTable,ChipRow}View.svelte` | NEW adapters |
| `dashboard-app/src/companion/catalog/index.ts` | registry |
| `dashboard-app/src/companion/companion.css` | `--font-numeric`, `report` theme |
| `skills/live-dashboard/SKILL.md` | v1.2: table rows, recipe, shape guidance |
| `CLAUDE.md`, `docs/features/INDEX.md` | F096 rows |
| Tests | `tests/test_a2ui_validator.py`, `tests/test_a2ui_dashboards.py`, `tests/test_a2ui_micro_apps.py`, `tests/test_a2ui_report.py` (NEW whole-feature), `dashboard-app/src/companion/chart.test.ts`, `catalog/charts.test.ts`, `catalog/report.test.ts` (NEW), `theme.test.ts` |

---

### Task 1: Catalog schemas + strictness tests

**Files:**
- Modify: `nous/a2ui/catalogs/nous_core/catalog.json` (components map, `$defs.anyComponent.oneOf`)
- Test: `tests/test_a2ui_validator.py`

**Interfaces:**
- Produces: component names `MetricCard`, `ScoreCard`, `DeltaList`, `DataTable`, `ChipRow`; props exactly as spec §3.1–§3.5 (+`weight`); `Section.caption` (DynamicString), `Section.layout` enum + `cards`; `AppHeader.note` (DynamicString); `Sparkline.trendline` (boolean); `MetricCard.trendline` (boolean).

- [ ] **Step 1: Write the failing strictness tests** (append to `tests/test_a2ui_validator.py`, reusing its `_envelope` helper and a `_TEXT_ROOT`-style root):

```python
_REPORT_MINIMAL = {
    "MetricCard": {"label": "L", "value": "1"},
    "ScoreCard": {"title": "T", "status": "ok"},
    "DeltaList": {"rows": {"path": "/rows"}},
    "DataTable": {"columns": [{"key": "a", "label": "A"}], "rows": {"path": "/rows"}},
    "ChipRow": {"items": {"path": "/rows"}},
}


@pytest.mark.parametrize("name", sorted(_REPORT_MINIMAL))
def test_report_components_validate_and_reject_styling(name: str) -> None:
    comp = {"id": "c", "component": name, **_REPORT_MINIMAL[name]}
    root = {"id": "root", "component": "Column", "children": ["c"]}
    assert validate_envelope(_envelope([root, comp], data_model={"rows": []})) == []
    for bad in ("style", "color"):
        errs = validate_envelope(_envelope([root, {**comp, bad: "red"}], data_model={"rows": []}))
        assert errs, f"{name}.{bad} accepted"


def test_datatable_column_entries_are_closed() -> None:
    comp = {"id": "c", "component": "DataTable", "rows": {"path": "/rows"},
            "columns": [{"key": "a", "label": "A", "color": "red"}]}
    root = {"id": "root", "component": "Column", "children": ["c"]}
    assert validate_envelope(_envelope([root, comp], data_model={"rows": []}))
```

(Adapt `_envelope`'s signature to how the file builds envelopes — read its helper first; if it takes only components, seed the data model through the existing `dataModel` kwarg or fixture.)

- [ ] **Step 2: Run to verify they fail**: `uv run pytest tests/test_a2ui_validator.py -k report -q` → FAIL ("is not valid under any of the given schemas" for unknown components).

- [ ] **Step 3: Add the schemas.** In `catalog.json` `components`, after `BarChart`, add (descriptions must state the preformatted contract; `DS` = `{"$ref": "https://a2ui.org/specification/v1_0/common_types.json#/$defs/DynamicString"}`, `DV` = same with `DynamicValue`, `TONE` = `{"type":"string","enum":["neutral","ok","warn","crit"]}`, `WEIGHT` = the existing weight object):

```json
"MetricCard": {"type":"object","description":"One metric's full story inside a section: label, preformatted value (+unit), a tone-coloured delta pill, the comparison caption, an optional embedded trend sparkline and a footnote. Values arrive PREFORMATTED — the renderer never rounds or adds units. The value is never coloured; tone lands on the pill and the trend.",
  "properties":{"component":{"const":"MetricCard"},"label":DS,"value":DS,"unit":DS,"delta":DS,"tone":TONE,"caption":DS,
    "trend":{"type":"string","description":"Data-model path (a bare string like Sparkline.path, relative inside a repeat template) to a series object. Omit for a count — a count is not a series."},
    "trendline":{"type":"boolean","description":"Draw the renderer-owned rolling mean over the faint raw series (default false)."},
    "footnote":DS,"weight":WEIGHT},
  "required":["component","label","value"]},
"ScoreCard": {... "properties": component/title/status/tone/value/unit/caption/items(DV)/note/weight, "required":["component","title","status"]},
"DeltaList": {... rows(DV), "emptyText":{"type":"string"}, weight; "required":["component","rows"]},
"DataTable": {... "columns":{"type":"array","minItems":1,"maxItems":6,"items":{"type":"object","properties":{"key":{"type":"string","minLength":1},"label":{"type":"string"},"align":{"type":"string","enum":["start","end"]},"secondary":{"type":"boolean"}},"required":["key","label"],"additionalProperties":false}}, rows(DV), emptyText, weight; "required":["component","columns","rows"]},
"ChipRow": {... items(DV), weight; "required":["component","items"]}
```

Then: `Section.properties.caption = DS` (description: "Right-aligned qualifier in the section head — source attribution, the comparison window, a count."), `Section.properties.layout.enum` += `"cards"` (description += "cards = auto-fit grid of ≥220px cards (MetricCard/ScoreCard); like grid-N it reshapes a direct Column/Row child"), `AppHeader.properties.note = DS` ("A data-reach line under the freshness stamp, e.g. 'data through 2026-09-01'."), `Sparkline.properties.trendline = boolean`. Append the five `{"$ref": "#/components/<Name>"}` entries to `$defs.anyComponent.oneOf`.

- [ ] **Step 4: Run** `uv run pytest tests/test_a2ui_validator.py -q` → PASS. Also `uv run pytest tests/test_a2ui_conformance.py -q` (catalog conformance) → PASS.
- [ ] **Step 5: Commit** `feat(a2ui): F096 catalog schemas for report components`.

---

### Task 2: Grammar — allowlist, caps tier, DataTable columns lint, blank trend lint

**Files:**
- Modify: `nous/a2ui/grammar.py` (`ALLOWED_COMPONENTS`, `_ROOMY_ARCHETYPES`/`caps_for`, `lint_micro_app` loop)
- Test: `tests/test_a2ui_dashboards.py`

**Interfaces:**
- Produces: `caps_for("report") == (80, 10)`; lint error strings: `DataTable {id!r} columns must be a list of {{key, label}} objects`, `DataTable {id!r} column #{i} has no non-empty string key`, `DataTable {id!r} has {n} columns — max 6 (a phone-width table)`, `DataTable {id!r} has duplicate column keys: [...]`, `MetricCard {id!r} has a blank trend — omit trend for a count`.

- [ ] **Step 1: Failing tests**:

```python
def test_report_archetype_gets_the_widest_caps():
    assert grammar.caps_for("report") == (80, 10)
    assert grammar.caps_for("ledger") == (80, 8)

@pytest.mark.parametrize("columns", ["a,b", [{"key": "a"}, "b"], [{"label": "x"}], [{"key": ""}, {"key": "b", "label": "B"}]])
def test_datatable_columns_lint_returns_errors_never_raises(columns):
    comps = _report_skeleton_with({"id": "t", "component": "DataTable", "columns": columns, "rows": {"path": "/rows"}})
    errs = grammar.lint_micro_app(comps, archetype="report")
    assert any("DataTable 't'" in e for e in errs)

def test_datatable_seven_columns_and_duplicates_are_named():
    seven = [{"key": f"k{i}", "label": str(i)} for i in range(7)]
    dup = [{"key": "k", "label": "1"}, {"key": "k", "label": "2"}]
    for cols, needle in ((seven, "max 6"), (dup, "duplicate column keys")):
        comps = _report_skeleton_with({"id": "t", "component": "DataTable", "columns": cols, "rows": {"path": "/rows"}})
        assert any(needle in e for e in grammar.lint_micro_app(comps))

def test_metriccard_blank_trend_is_named():
    comps = _report_skeleton_with({"id": "m", "component": "MetricCard", "label": "L", "value": "1", "trend": " "})
    assert any("blank trend" in e for e in grammar.lint_micro_app(comps))
```

with a module helper `_report_skeleton_with(comp)` returning `[root Column(header, section, footer), AppHeader(composedAt binding), Section(child=comp id), AppFooter, comp]`.

- [ ] **Step 2: Run** `uv run pytest tests/test_a2ui_dashboards.py -k "report_archetype or datatable or blank_trend" -q` → FAIL.
- [ ] **Step 3: Implement.** Add the five names to `ALLOWED_COMPONENTS` (new `# F096 report vocabulary` group). Replace `_ROOMY_ARCHETYPES` handling in `caps_for` with a tier map: `_ARCHETYPE_CAPS = {"ledger": (80, 8), "briefing": (80, 8), "report": (80, 10)}`; keep the non-string guard. In the lint loop add, after the LineChart branch:

```python
        if ctype == "DataTable":
            cols = comp.get("columns")
            if not isinstance(cols, list):
                errors.append(f"DataTable {comp.get('id')!r} columns must be a list of {{key, label}} objects")
            else:
                keys: list[str] = []
                for i, col in enumerate(cols):
                    key = col.get("key") if isinstance(col, dict) else None
                    if not isinstance(key, str) or not key.strip():
                        errors.append(f"DataTable {comp.get('id')!r} column #{i} has no non-empty string key")
                    else:
                        keys.append(key)
                if len(cols) > 6:
                    errors.append(f"DataTable {comp.get('id')!r} has {len(cols)} columns — max 6 (a phone-width table)")
                dupes = sorted({k for k in keys if keys.count(k) > 1})
                if dupes:
                    errors.append(f"DataTable {comp.get('id')!r} has duplicate column keys: {dupes}")
        if ctype == "MetricCard" and "trend" in comp:
            trend = comp.get("trend")
            if not isinstance(trend, str) or not trend.strip():
                errors.append(f"MetricCard {comp.get('id')!r} has a blank trend — omit trend for a count")
```

- [ ] **Step 4: Run** the four tests + the whole file → PASS. **Step 5: Commit** `feat(a2ui): F096 grammar — report caps tier, DataTable columns lint, blank trend lint`.

---

### Task 3: DSL helpers

**Files:** Modify `nous/a2ui/dsl.py`; Test `tests/test_a2ui_builders.py` (or the dsl test file if one exists — `grep -l "from nous.a2ui.dsl" tests/`).

**Interfaces (produces):**
```python
def MetricCard(id, *, label, value, unit=None, delta=None, tone=None, caption=None, trend=None, trendline=None, footnote=None) -> dict
def ScoreCard(id, *, title, status, tone=None, value=None, unit=None, caption=None, items=None, note=None) -> dict
def DeltaList(id, *, rows, empty_text=None) -> dict          # emits "emptyText"
def DataTable(id, *, columns, rows, empty_text=None) -> dict
def ChipRow(id, *, items) -> dict
# existing helpers gain kwargs:
Section(..., caption=None)   AppHeader(..., note=None)   Sparkline(..., trendline=None)
```
All None-valued keys dropped via the module's `_clean`.

- [ ] Steps: failing test asserting each helper's dict equals the spec shape and that `None` kwargs are absent → run → implement → run → commit `feat(a2ui): F096 dsl helpers`.

---

### Task 4: Sources — `focus_from` and series-aware `_bound`

**Files:** Modify `nous/a2ui/sources.py` (`to_series`, `_bound`, `_bound_series` reason param); Test `tests/test_a2ui_dashboards.py`.

**Interfaces (produces):**
- `to_series(records, t_key, v_key, *, unit="", value_keys=None, focus_from=None)`; when `focus_from` is not None, `result["meta"]["focus_from"] = _iso(focus_from)`.
- `_bound_series(series, budget, *, exhausted_reason=None)`; default reason unchanged.
- `_bound(value, budget)` for a **list** that overflows: first `_shrink_embedded_series(list, budget)`, then the existing tail pop.

- [ ] **Step 1: Failing tests**:

```python
def test_to_series_focus_from_is_iso_and_survives_downsampling():
    from datetime import date
    rows = [{"t": f"2026-08-{d:02d}", "v": float(d)} for d in range(1, 29)]
    s = to_series(rows * 10, "t", "v", focus_from=date(2026, 8, 4))  # 280 pts → downsampled
    assert s["meta"]["focus_from"] == "2026-08-04"
    assert s["meta"]["downsampled_from"] == 280

def _metric_records(n, points):
    rows = [{"t": f"2026-0{1 + i // 28}-{1 + i % 28:02d}", "v": float(i)} for i in range(points)]
    return [{"label": f"m{i}", "value": "1", "trend": to_series(rows, "t", "v")} for i in range(n)]

def test_bound_shortens_embedded_series_before_dropping_records():
    recs = _metric_records(6, 200)
    out, size = sources._bound(recs, 12_000)
    assert size <= 12_000
    assert len(out) == 6 and not any(r.get("_truncated") for r in out)
    assert all(r["trend"]["meta"]["downsampled_from"] == 200 for r in out)

def test_bound_pops_records_only_when_sparks_cannot_shrink_enough():
    recs = _metric_records(200, 3)              # sparks already minimal
    out, size = sources._bound(recs, 3_000)
    assert size <= 3_000 and out[-1]["_truncated"] is True

def test_bound_leaves_malformed_embedded_series_untouched_and_never_raises():
    recs = _metric_records(6, 200)
    recs[0]["trend"] = {"kind": "series", "points": None}
    recs[1]["trend"] = {"kind": "series", "points": [{"t": "x", "v": 1}], "meta": "bad"}
    out, _ = sources._bound(recs, 12_000)
    assert out[0]["trend"] == {"kind": "series", "points": None}
    assert out[1]["trend"]["meta"] == "bad"

def test_six_metric_56_point_source_fits_the_per_source_budget_undownsampled():
    recs = _metric_records(6, 56)
    for r in recs:
        r.update({"unit": "s", "delta": "↓0.6 s · improving", "tone": "ok",
                  "caption": "5.9 → 5.3 (28d median, n=28)", "footnote": "last 2026-09-01"})
    out, size = sources._bound(recs, sources._PER_SOURCE_BUDGET_CHARS)
    assert size <= sources._PER_SOURCE_BUDGET_CHARS
    assert all(r["trend"]["meta"]["downsampled_from"] is None for r in out)
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement**:

```python
def _shrink_embedded_series(items: list, budget: int) -> list:
    """Downsample every VALID {kind:series} nested one level inside list
    records so the list fits ``budget`` — before any record is dropped
    (spec §6.1). Malformed nested series are left untouched: _downsample_series
    would raise on them, and compose/readSeries already degrade them honestly."""
    slots = [(i, k) for i, rec in enumerate(items) if isinstance(rec, dict)
             for k, v in rec.items() if _is_valid_series(v)]
    if not slots:
        return items
    out = [dict(r) if isinstance(r, dict) else r for r in items]
    fixed = len(json.dumps(out, default=str)) - sum(
        len(json.dumps(items[i][k], default=str)) for i, k in slots)
    share = max(0, (budget - fixed) // len(slots))
    for i, k in slots:
        out[i][k], _ = _bound_series(items[i][k], share,
                                     exhausted_reason="char budget exhausted for this record's series")
    return out
```
and in `_bound`, inside `if isinstance(value, list):` before the pop loop: `value = _shrink_embedded_series(value, budget); if len(json.dumps(value, default=str)) <= budget: return value, len(...)`. Thread `exhausted_reason` through `_bound_series` (default = today's string). Add `focus_from` to `to_series`.

- [ ] **Step 4: Run** the tests + whole `test_a2ui_dashboards.py` → PASS. **Step 5: Commit** `feat(a2ui): F096 sources — focus_from and series-aware _bound`.

---

### Task 5: Compose validation — shared resolver, array + column rules, MetricCard.trend

**Files:** Modify `nous/a2ui/compose.py` (`_CHART_COMPONENTS` → maps, `_collect_bindings`, `_containing_templates`, new `_resolve_path_targets`, `_chart_shape_errors` consumer set, `_binding_rules`); Test `tests/test_a2ui_dashboards.py`.

**Interfaces (produces):**
```python
_SERIES_PATH_PROPS = {"Sparkline": "path", "LineChart": "path", "BarChart": "path", "MetricCard": "trend"}
_SINGLE_VALUE_CONSUMERS = frozenset({"Sparkline", "BarChart", "MetricCard"})
_ARRAY_VALUE_PROPS = {"DeltaList": "rows", "DataTable": "rows", "ChipRow": "items", "ScoreCard": "items", "Timeline": "items", "KeyValueTable": "rows"}
_OPTIONAL_DATA_PROPS = frozenset({("MetricCard", "trend"), ("ScoreCard", "items")})

def _resolve_path_targets(comp: dict, path: str, by_id: dict[str, dict], full_model: dict) -> list[tuple[str, Any]] | None:
    """Targets for a component's path prop: [(abs_path, resolved)] — one from
    root for an absolute path or no enclosing template, one per item inside
    exactly one Repeat template; None when nested (left to the renderer) or
    when the template has no items yet."""
```
`_containing_templates` walks `grammar._children_of` (import it) instead of `_child_ids`.

- [ ] **Step 1: Failing tests** (fixtures built with the Task 3 DSL; `_validate` reached through the existing `composer` fixture the way `test_compose_*` tests do — read one first):

```python
def test_metriccard_trend_bound_to_records_is_named(...)          # error contains "MetricCard 'm' binds /metrics" and "not a series"
def test_metriccard_trend_bound_to_multi_series_is_named(...)     # keys:[...] source → "multi-series"
def test_metriccard_without_trend_in_a_mixed_grid_passes(...)     # Repeat over [{trend: series}, {no trend}] → no error
def test_deltalist_bound_to_series_is_named(...)                  # "resolved to a series, not an array of rows"
def test_scorecard_items_absent_or_none_pass_but_string_is_named(...)
def test_array_rule_accepts_empty_list_and_skips_function_calls(...)
def test_datatable_column_absent_from_every_row_is_named(...)     # "column 'x' is absent from every row"
def test_keyvaluetable_inside_modal_content_inside_repeat_resolves_per_item(...)  # AC9: clean; same bound to series → named
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** Replace the chart block in `_binding_rules` with:

```python
    for comp in components:
        ctype = comp.get("component")
        cid = comp.get("id", "")
        prop = _SERIES_PATH_PROPS.get(ctype)
        if prop is not None:
            path = comp.get(prop)
            if isinstance(path, str) and path.strip():
                targets = _resolve_path_targets(comp, path, by_id, full_model)
                for tpath, resolved in targets or []:
                    if resolved is None and (ctype, prop) in _OPTIONAL_DATA_PROPS:
                        continue  # no trend for this item — a legal state (§3.1)
                    errs = _chart_shape_errors(ctype, comp, tpath, resolved)
                    if errs:
                        errors.extend(errs); break
        aprop = _ARRAY_VALUE_PROPS.get(ctype)
        if aprop is not None and aprop in comp:
            errors.extend(_array_rule_errors(ctype, comp, aprop, by_id, full_model))
        if ctype == "DataTable":
            errors.extend(_column_rule_errors(comp, by_id, full_model))
```
`_array_rule_errors`: value = `comp[aprop]`; if dict with `call` → `[]`; if dict with str `path` → resolve targets (relative-aware, same helper; `None` targets → `[]`); else literal → `[(f"{cid}.{aprop}", value)]`. For each `(tpath, resolved)`: `None` → error unless optional; not a list, or any entry not a dict → `f"{ctype} {cid!r} binds {tpath} which resolved to {shape}, not an array of rows"` where shape names `a series` when `is_series(resolved)`. First failure breaks. `_column_rule_errors`: only when rows resolve (via the same helper) to a non-empty list of dicts; for each `columns[].key` (str) absent from every row → `f"DataTable {cid!r} column {key!r} is absent from every row — an empty column is a partial render"`. `_chart_shape_errors`: replace the `("Sparkline", "BarChart")` tuple with `_SINGLE_VALUE_CONSUMERS`. `_collect_bindings`: use `_SERIES_PATH_PROPS` for the string-path props.

- [ ] **Step 4: Run** new tests + full `tests/test_a2ui_dashboards.py tests/test_a2ui_micro_apps.py` → PASS (existing fixtures must stay green — the array rule only rejects wrong shapes). **Step 5: Commit** `feat(a2ui): F096 compose validation — shared resolver, array/column rules, MetricCard.trend`.

---

### Task 6: Prompt, themes, archetypes, catalog summary, tools

**Files:** Modify `nous/a2ui/compose.py` (`_THEMES`, `_ARCHETYPES`, `_GRAMMAR_RULES`, `_COMPONENT_USAGE`, new `_REPORT_RULES`, `_RESPONSE_SHAPE`, `_build_prompt` source description), `nous/a2ui/catalog_summary.py` (`_COMPONENT_ORDER`), `nous/a2ui/tools.py` (archetype enum + description; `shape` description); Test `tests/test_a2ui_dashboards.py` (`test_theme_enum_is_closed` grows), `tests/test_a2ui_micro_apps.py`.

**Interfaces (produces):** `_THEMES["report"]`; `_ARCHETYPES` mentions `report`; `_REPORT_RULES` string inserted in `_build_prompt` after `_GRAMMAR_RULES`; `_source_description(marked: dict) -> str` (per-source cap 800 chars, `ensure_ascii=False`, tag `records; each carries a series at: <keys>`).

- [ ] **Step 1: Failing tests**:

```python
def test_theme_enum_is_closed():  # existing — extend the expected set with "report"
def test_prompt_carries_the_report_block_and_stattile_intent(composer):
    prompt = composer._build_prompt("28-day trend report", "report", {})
    for needle in ("REPORT", "bare string", "intent ∈ neutral|good|bad|warn", "tone ∈ neutral|ok|warn|crit", "layout cards", "report:"):
        assert needle in prompt
def test_source_description_is_capped_per_source_and_tags_embedded_series(composer):
    metrics = [{"label": "m", "trend": to_series([{"t": "2026-08-01", "v": 1.0}], "t", "v")}]
    big = [{"k": "x" * 5000}] * 3
    prompt = composer._build_prompt("x", None, {"metrics": metrics, "big": big, "tail": [{"a": 1}]})
    assert "each carries a series at: trend" in prompt
    assert '"tail"' in prompt            # the last source survives the cap
def test_catalog_summary_names_report_components():
    s = catalog_property_summary()
    assert "- MetricCard: required label, value" in s and "- DataTable: required columns, rows" in s
def test_compose_surface_tool_offers_the_report_archetype(...):   # tools.py enum contains "report" and the description glosses it
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** per spec §5 and §8.1 (the REPORT block text is in the spec, items 1–5; the StatTile and five component usage lines; `_ARCHETYPES` gains `- report — "how are things moving vs the prior window": ScoreCards for goals, DeltaLists of movers, MetricCard grids per source, a DataTable of the raw lane, a freshness ChipRow.`; budget line `80 for ledger/briefing, 80 components / 10 sections for report`; layout line gains `cards`; `_RESPONSE_SHAPE` archetype union gains `"report"`; tools.py enum + gloss + `shape` text: "`series` only when the TOP-LEVEL result is one series; `records` for a list — including a record list whose records each embed a series").

- [ ] **Step 4: Run** the a2ui python suites → PASS. **Step 5: Commit** `feat(a2ui): F096 prompt — report archetype/theme, usage lines, per-source sample cap`.

---

### Task 7: Renderer — chart helpers, shared partial, five adapters, section/header props, theme

**Files:**
- Modify: `dashboard-app/src/companion/chart.ts` (+`focusFrom` on `ReadSeries`; `rollingMean(finite, window)`; `focusStartIndex(points, focusFrom)`), `catalog/SparklineView.svelte`, `catalog/SectionView.svelte`, `catalog/AppHeaderView.svelte`, `catalog/index.ts`, `companion.css`, `catalog/charts.test.ts` (`:79`, `:89` → `circle:not(.end)`), `theme.test.ts` (six themes)
- Create: `catalog/SparkSvg.svelte`, `catalog/MetricCardView.svelte`, `catalog/ScoreCardView.svelte`, `catalog/DeltaListView.svelte`, `catalog/DataTableView.svelte`, `catalog/ChipRowView.svelte`, `catalog/report.test.ts`
- Test: `chart.test.ts` (new helpers), `charts.test.ts`, `report.test.ts`, `theme.test.ts`

**Interfaces (produces):**
```ts
// chart.ts
export interface ReadSeries { …; focusFrom: string | null }          // from meta.focus_from
export function rollingMean(finite: {i:number; v:number}[], window: number): {i:number; v:number}[]
  // window applied within each run of consecutive indices ONLY — a gap resets the window
export function focusStartIndex(points: SeriesPoint[], focusFrom: string | null): number | null
  // first index whose t >= focusFrom (string compare on ISO), null when none/absent
export function trendWindow(n: number): number  // max(3, round(n / 8))
// SparkSvg.svelte props: { series: ReadSeries; tone: Tone; trendline: boolean; height: number; label: string }
//   renders <svg> only: focus <rect class="focus">, raw polylines (class="raw" when trendline), main polylines/dots, <circle class="end"> (skipped if last point is an isolated dot), <text class="brk">
```
Tokens in `companion.css :root`: `--font-numeric: var(--font-mono);` with the comment from spec §4.5; `[data-theme='report']` block with the §5 values (`--soft: #94a3b8`, `--series-1: #f0abfc`, `--font-display: 'Iowan Old Style', 'Palatino Linotype', Palatino, Georgia, serif`, `--on-accent: #0a0c11`, `--scrim: rgba(10,12,17,0.74)`, `--node-decision: #c4b5fd`, `--accent: #7c9cd0`, `--accent-dim: #4f6fa0`, `--accent-glow: rgba(124,156,208,0.15)`, `--surface-hover: #182030`, `--locked: #94a3b8`, `--warn: #fbbf24`, `--series-2: #4ade80`, `--series-3: #fbbf24`, `--series-4: #60a5fa`, `--chart-axis: #8d99ae`, `--chart-grid: #212b3b`).

Adapter contracts (all read `comp` via `resolveDynamic`/`toDisplayString`, never reformat, `flexGrow(comp.weight)` on the root element, non-array binds → no rows, never throw):
- `MetricCardView`: `.metric` root; `.top` = `.label` + `<span class="pill {tone}">` (only when `delta`); `.value` (+`.unit`); `.caption`; if `comp.trend` is a non-blank string → `readSeries(resolveDynamic({path}, ctx))`: `!ok`/empty → `.state` "no data — reason"; single → `.state` "single reading"; else `<SparkSvg …>`; `.foot`. Pill background: `color-mix(in srgb, var(--tone) 13%, transparent)` where `--tone` is set from `toneTextVar(tone)` = `var(--soft)` for neutral else `var(--<tone>)`.
- `ScoreCardView`: `.score {tone}` with `border-top: 3px solid var(--tone)`; `.head` = `h4` + `.pill.status`; optional `.value`/`.unit`/`.caption`; `<ul>` of `li.{tone}` from `items` (label span + `<b>` value); `.note`.
- `DeltaListView`: `<ul class="deltas">`; rows `li.{tone}` with `.n`, `.d`, `.r` (`from → to` only when either is present); empty → `<li><span class="empty">{emptyText || 'nothing to report'}</span></li>`.
- `DataTableView`: `<table class="dtable">` with `<thead>` from `columns` (skips non-object entries), `<tbody>` rows; `td.end`/`th.end` for `align: end`; `td.secondary`; empty rows → `.empty` text.
- `ChipRowView`: `.chips` of `.chip.{tone}` with `.l`, `.v`, `.dt`.
- `SectionView`: `LAYOUTS` gains `'cards'`; head renders `.caption` (resolved DynamicString) when present; CSS `.app-section.cards > :global(.col), .app-section.cards > :global(.row) { display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0.6rem; }`.
- `AppHeaderView`: `.note` under the stamp when present.
- `SparklineView`: keeps head/foot; replaces its inline `<svg>` with `<SparkSvg>`; `.cur` gains `font-family: var(--font-numeric)`.

- [ ] **Step 1: Failing tests** — `chart.test.ts`: `rollingMean` never bridges a gap (`[{i:0,v:1},{i:1,v:3},{i:3,v:5}]`, window 2 → i:1 = 2, i:3 = 5); `focusStartIndex` picks the first `t >= focusFrom`; `readSeries` exposes `focusFrom`. `charts.test.ts`: change `:79`/`:89` to `circle:not(.end)` and add `renders one end dot when the last point is not isolated` + `does not double-dot an isolated final reading` + `shades the focus window from meta.focus_from` + `trendline draws a faint raw line behind the mean`. `report.test.ts` (seed helper copied from `microapp.test.ts`): one `it` per adapter for the happy path, the empty state, and the preformatted-value invariant (`value: "1234.5678"` renders verbatim); `SectionView` `cards` class + caption; `AppHeaderView` note; a whole-app render via `Renderer` with `componentId="root"` over the fixture from Task 8 asserting `.metric` count and no `.ph` placeholder. `theme.test.ts`: expected set gains `report`; the AA loop runs over six.
- [ ] **Step 2: Run** `cd dashboard-app && npx vitest run src/companion` → FAIL on the new files.
- [ ] **Step 3: Implement** in the order: `chart.ts` → `SparkSvg.svelte` → `SparklineView` refactor (charts.test.ts green again) → five adapters + registry → Section/AppHeader → css. Svelte 5 runes (`$props`, `$derived`), same `let { surfaceId, comp, scope = null } = $props()` signature as `StatTileView`. Keyed each by index (`(i)`) — model data can repeat labels.
- [ ] **Step 4: Run** `npx vitest run src/companion` and `npm run check` (svelte-check) → PASS. Run `npm run build` once → succeeds. **Step 5: Commit** `feat(a2ui): F096 renderer — report components, spark partial, report theme`.

---

### Task 8: Whole-feature fixture, skill, docs

**Files:**
- Create: `tests/test_a2ui_report.py` (python whole-feature), fixture module `tests/fixtures/f096_report_app.py` exporting `components()`, `data_model()`, `source_data()` used by BOTH the python test and (as JSON via a small `python -m tests.fixtures.f096_report_app > dashboard-app/src/companion/catalog/__fixtures__/f096-report-app.json` step, committed) the TS whole-app render test from Task 7.
- Modify: `skills/live-dashboard/SKILL.md` (version 1.2), `CLAUDE.md` (F096 row + `report` mentions in the F093/F094 row are NOT edited), `docs/features/INDEX.md` (F096 row after F093), `docs/features/F096-report-vocabulary.md` status → `shipped`.

- [ ] **Step 1: Fixture + failing python test**: app = `AppHeader(note binding)`, `Section("Goals", layout cards, caption) → Column(children template over /goals → ScoreCard(items: {path: "items"}))` with one goal record lacking `items`; `Section("Movers", grid-2) → Row(DeltaList /up, DeltaList /down with emptyText)` where `/down` is `[]`; `Section("Retrieval", cards, caption) → Column(template over /retrieval → MetricCard(trend: "trend", trendline: true))` where one record has no `trend`; `Section("Sleep", accordion) → DataTable(4 columns, rows /sleep)`; `Section("Freshness") → ChipRow(/lanes)`; `AppFooter`; archetype `report`, theme `report`. Test: `lint_micro_app(..., archetype="report") == []`; `composer._validate(...)` (or the module-level equivalent the compose tests use) returns no errors; `validate_envelope` of the built surface `== []`; and the `_bound`-resolved retrieval source keeps all records.
- [ ] **Step 2: Run** → FAIL (until the fixture is right) → fix → PASS. **Step 3:** SKILL.md edits per spec §8.3 (rows for the five, `cards`, `report`, the recipe with `to_series(..., focus_from=window_start)`, one source per panel, `shape: "records"`, the corrected shape bullets, version 1.2). CLAUDE.md row: one `| F096 | [Report Vocabulary](docs/features/F096-report-vocabulary.md) (…) | — |` line in the What's Shipped table after F093+F094, ≤ 12 lines of prose in the house style; INDEX row after F093.
- [ ] **Step 4:** Full suites: `uv run pytest tests/test_a2ui_*.py -q` and `cd dashboard-app && npx vitest run && npm run build` → PASS. **Step 5: Commit** `feat(a2ui): F096 whole-feature fixture, live-dashboard skill 1.2, docs`.

---

### Task 9: Review, PR, codex loop

- [ ] Self-review diff against spec §9 (all ten ACs — tick each with the test name that proves it).
- [ ] `git push -u origin feat/F096-report-vocabulary`; `gh pr create` with the spec summary, the design-canvas link, and the AC→test table; body ends with the Claude Code trailer.
- [ ] Monitor CI (`gh pr checks`) and codex (reactions on the trigger comment; re-comment `@codex review` if no run after ~45 min). Fix every finding with a regression test; converge when a round returns zero new findings.
- [ ] Merge with `gh pr merge --squash --delete-branch` once CI green + codex clean; `review_outcome` on FORGE decision `651c6b20`; memory note.
