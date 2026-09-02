---
feature: F096
title: Report Vocabulary — scorecards, metric cards, movers, tables and chips for micro-apps
status: reviewed — 3-lens adversarial review 2026-09-01 (29 findings folded), ready to build
depends_on: F093 §2/§4 (renderer-owned taste, semantic tokens), F094 (series contract, Sparkline), F092.1 Phase 3 (compose/grammar/sources), F095 (agent_script sources)
supersedes: nothing — additive to F093/F094
author: Nous
date: 2026-09-01
---

# F096 — Report Vocabulary

> **The problem in one line.** A micro-app can draw a trend (F094) but cannot say what the
> trend *means*. The vocabulary has no element for "this metric moved ↓4.3 and that is good",
> no verdict card, no ranked list of movers, no real table, no status chip — so every
> "how are things going" report still routes to a hand-authored HTML page that leaves the
> companion and cannot refresh.

---

## 1. Evidence

The motivating artifact is a hand-authored health trend report (`health_dashboard_2026-09-01.html`,
56,917 bytes, 17 inline `<svg>` blocks, zero libraries). Its content decomposes into a small
number of repeated element classes. Counted from the file on 2026-09-01:

| Element class on the page | Count | Nearest thing in the catalog today | Gap |
|---|---|---|---|
| Goal card: title, verdict pill (`SLIPPING` / `NO CHANGE` / `ON TRACK`), headline value + unit, list of (label, tone-coloured delta), italic note | 3 | `Card` + `Text`s | no verdict pill, no per-row tone, no top-rule accent |
| Movers list: name · tone-coloured delta · `from → to` | 2 lists, 7 rows | `KeyValueTable` (2 columns, no tone) | no third column, no tone, no empty state |
| Metric card: label, tone pill `↑0.3 kg · worsening`, big value + unit, caption `91.0 → 91.3 (28d avg, n=16)`, sparkline with shaded recent window + raw/smoothed lines + end dot, footnote `last 2026-09-01` | 17 | `StatTile` (label/value/delta, value coloured by intent) + a separate `Sparkline` | no unit slot, no caption/footnote, no embedded trend, tone lands on the value not the delta |
| Table with numeric right-aligned columns and a de-emphasised column | 1 (6 rows × 4 cols) | `KeyValueTable` | two columns only, no alignment, no secondary column |
| Status chips: uppercase label, tone-coloured value, detail | 3 | none | — |
| Panel header with a right-aligned qualifier (`Garmin`, `Withings scale`, `significant favourable moves`) | 7 | `Section.title` | no caption slot |
| Header with a data-reach line (`data through 2026-09-01`) beside the build stamp | 1 | `AppHeader` stamp | no note slot |
| Serif display face, tabular monospace figures, cool slate palette | whole page | `paper` (light) / `harbor` (blue) themes | no dark serif/mono theme |

Three consequences.

**(a) It is a vocabulary gap, not an architecture gap.** Every element above is a
data-plus-tone component of exactly the kind F093 §5 and F094 §3 already ship
(`Timeline`, `Sparkline`): the model supplies preformatted strings, an array binding and a
tone from a closed enum; the renderer owns everything visual. Nothing new has to be invented.

**(b) The reference is health, the vocabulary must not be.** "Goal / movers / metric grid /
table / freshness" is the shape of *every* periodic trend report — a trading book, a project
burn-down, Nous's own decision calibration. The design canvas for this spec therefore mocks
the vocabulary on a Nous-ops domain (decisions, retrieval, DAG throughput); if it reads
naturally there, nothing is health-specific.

**(c) The remaining gap after F094 was "layout personality", and this is most of it.** F094 §2
named the honest ceiling: after charts, the difference between a composed app and the
reference page is typography, section rhythm and editorial copy. The `report` theme (§5)
and the two header/section affordances (§4) close the first two; copy stays the agent's.

---

## 2. Non-goals — inherited normatively from F093 §2

**No raw SVG, no `style` prop, no `color` prop, no size or geometry prop.** Every new
component takes data and *meaning*; the renderer owns colour, type, spacing, the sparkline
window size and every empty/degenerate state. Enforced by construction at the component
object: the vendored envelope schema (`agent_to_renderer.json:84`) sets
`unevaluatedProperties: false` on every component object, so a `style` or `color` key on any
of the five new components is a schema rejection, not a linter opinion. **Where strictness
stops:** the objects *inside* a DynamicValue array (a `ScoreCard.items` row, a `DataTable`
row) are data, and `common_types.json` gives them no item schema — a row's `tone` is closed at
render time by `normalizeTone` (unknown → `neutral`), and a stray `color` key on a row is
ignored, never painted. The one static array, `DataTable.columns`, is closed by its own item
schema (`additionalProperties: false`, `align` enum, `secondary` boolean). Values are
**preformatted** — the StatTile contract ("the agent has already decided how many decimals
and what unit; the renderer must not reformat") applies to every string prop here and is
stated in every schema description.

**Not a grammar of graphics, not a spreadsheet.** Five components, closed. `DataTable` has
no sorting, filtering, pagination or cell formatting; a table that needs those is a ledger
archetype with a refine option.

**No analysis helper in this feature.** The delta pill, the verdict, the `from → to` caption
and the "strong" flag are *analysis outputs* the data script computes (window means,
z-scores, direction preference). A shared `window_summary()` helper is the right follow-up
once two real dashboards have written that code twice; shipping it here would double the
review surface. `skills/live-dashboard/SKILL.md` documents the recipe instead (§8.3).

---

## 3. Change 1 — five components (P1)

All five live in the `nous-core` catalog and the micro-app grammar's `ALLOWED_COMPONENTS`,
and every schema carries the universal `weight` property (the catalog summary promises it on
every component and the envelope would reject it otherwise). `tone` everywhere is the F094
enum `neutral | ok | warn | crit` (default `neutral`). **On `MetricCard` and `ScoreCard` the
tone may also be a bare `{path}` binding** — a metric grid or goal list is ONE template under
a Repeat, and a literal-only tone would paint every card in the grid the same colour, while
the reference page has good/bad/flat cards side by side. The schema keeps the literal closed
(enum) and admits only a `{path}` object (no `call`, no extra keys); the renderer closes the
resolved value with `normalizeTone` (unknown → `neutral`, never a literal colour). **Where a tone lands:** text and pills
map to `--soft` / `--ok` / `--warn` / `--crit`; a chart stroke maps through the existing
`toneVar` (`neutral` → `--chart-axis`), so a neutral card shows a soft pill over an axis-grey
spark — the two greys are deliberate (a pill is ink, a line is a mark). `StatTile` keeps its
older `intent` (`neutral | good | bad | warn`) unchanged; the prompt names both enums and
says which component takes which (§8.1).

### 3.1 `MetricCard` — one metric's full story

| Prop | Type | Req | Meaning |
|---|---|---|---|
| `label` | DynamicString | ✓ | what the number measures |
| `value` | DynamicString | ✓ | headline figure, preformatted |
| `unit` | DynamicString | | rendered small beside the value |
| `delta` | DynamicString | | the change pill text, e.g. `↓4.3 bpm · improving` |
| `tone` | enum | | colours the pill and the trend, never the value |
| `caption` | DynamicString | | the comparison line, e.g. `68.9 → 64.6 (28d avg, n=28)` |
| `trend` | string path | | a series-shaped value; renders an embedded sparkline |
| `trendline` | boolean | | draw the rolling mean over the faint raw series (§4.3); default `false` |
| `footnote` | DynamicString | | small right-aligned line, e.g. `last 2026-09-01` |

Renderer: label + pill on one row; value in `--font-numeric` at 1.55rem with the unit muted;
caption; 56px sparkline (§4.3 treatment, tone-coloured); footnote. **The value is never
coloured** — the reference page colours the judgement (the pill), not the reading, and that
is the right emphasis: a number is a fact, a direction is an opinion.

**The embedded frame renders no current-value head and never reformats.** The standalone
`Sparkline` shows `formatTick(last)` beside its label; the card already shows the agent's
preformatted `value` above the chart, so the MetricCard frame draws the SVG only. Its
single-reading state is the text `single reading` (not a renderer-rounded figure); its empty
state is `no data — <reason>`.

States: `trend` absent, **or present but resolving to nothing** (a count mixed into a grid of
trended metrics under one Repeat) ⇒ no chart region at all — "resolves to nothing" is the
no-trend state, not an error (§7.2). `trend: ""` is a grammar error ("omit `trend` for a
count"), because an empty path resolves to the root of the data model and would render a
bogus empty chart.

`trend` is a plain string path like a chart's `path` (absolute, or template-relative inside
a Repeat), so it is validated as one (§7.2).

### 3.2 `ScoreCard` — a verdict with its evidence

| Prop | Type | Req | Meaning |
|---|---|---|---|
| `title` | DynamicString | ✓ | the objective |
| `status` | DynamicString | ✓ | the verdict word(s), rendered as an uppercase pill |
| `tone` | enum | | drives the top rule and the pill |
| `value`, `unit`, `caption` | DynamicString | | optional headline figure + its comparison line |
| `items` | DynamicValue → `[{label, value, tone?}]` | | evidence rows; each row's `value` is preformatted and coloured by its own `tone` |
| `note` | DynamicString | | italic muted footer |

A card with no `value` is legal and common (the reference's third goal card has none); the
verdict plus evidence is the content. `items` is optional, so a ScoreCard under a Repeat
whose record has no `items` renders the verdict alone (§7.2 skips absent/None on optional
array props). Top accent is a **top** rule (the reference's own treatment), not a left border.

### 3.3 `DeltaList` — ranked movers

| Prop | Type | Req | Meaning |
|---|---|---|---|
| `rows` | DynamicValue → `[{label, delta, from?, to?, tone?}]` | ✓ | one row per mover, in the order given |
| `emptyText` | string | | rendered muted italic when `rows` is empty (default "nothing to report") |

Row: `label` · `delta` (bold `--font-numeric`, tone) · `from → to` (muted `--font-numeric`,
right-aligned, min-width so columns align across rows). An empty list is a **state**, never a
blank block — "no significant adverse moves" is the good news the page exists to show.

### 3.4 `DataTable` — a real table

| Prop | Type | Req | Meaning |
|---|---|---|---|
| `columns` | static `[{key, label, align?: start\|end, secondary?: bool}]`, 1–6 items, keys unique | ✓ | which record fields to show, in order |
| `rows` | DynamicValue → array of objects | ✓ | the records |
| `emptyText` | string | | muted italic when there are no rows |

`align: end` right-aligns in tabular figures (a readability choice, like `BarChart.orientation`);
`secondary` marks a de-emphasised supporting column (the reference's "types" column — the
renderer owns the treatment, today `--muted`). Long cells wrap (`overflow-wrap: anywhere`);
the table never widens the page. Six columns is the phone-width ceiling and is enforced (§7.1).

### 3.5 `ChipRow` — labelled status chips

| Prop | Type | Req | Meaning |
|---|---|---|---|
| `items` | DynamicValue → `[{label, value, detail?, tone?}]` | ✓ | one chip each |

Chip: uppercase muted label, tone-coloured value, muted detail. Wraps.

---

## 4. Change 2 — affordances on existing components (P1)

### 4.1 `Section.caption`
Optional **DynamicString** (consistent with `AppHeader.note`; a bound window like
`/meta/window` must not burn a repair round) rendered right-aligned, small and muted in the
section head — source attribution (`retrieval_log`), the comparison window, a count. The
reference uses one on every panel; without it the composer inlines the source name into the title.

### 4.2 `Section.layout: "cards"`
Adds one value to the F093 §6.1 enum: an auto-fit grid of `minmax(220px, 1fr)`. `grid-3` is
`minmax(140px, …)`, sized for tiles; a `MetricCard` with a 56px sparkline needs ~220px or its
caption wraps twice and the chart becomes a smear. Like `grid-2`/`grid-3`, `cards` reshapes the
section's **direct Column or Row child** — the child must be a Column or Row (a plain list of
cards, or a Repeat template of one card). On a 390px phone `cards` is one column; at the
companion's 720px maximum it is two. The renderer owns the breakpoints.

**Layout enumeration sites** (the renderer downgrades an unknown value to `stack` silently, so
a missed site is invisible): `catalog.json` Section.layout enum; `SectionView.svelte` `LAYOUTS`
allowlist + a `.app-section.cards > :global(.col/.row)` rule; `compose._GRAMMAR_RULES` SECTION
LAYOUT line; `SKILL.md` layouts line; a renderer test asserting `.app-section.cards`.

### 4.3 `Sparkline` treatment
- **End dot, always (renderer-owned, no prop):** a 3px `<circle class="end">` on the last
  finite point. Rendered only by the Sparkline partial (never LineChart), and **skipped when
  the last point is already an isolated-reading dot** (a one-coordinate segment), so a series
  ending in a gap-bounded reading shows one dot, not two. The two existing circle-count
  assertions in `charts.test.ts` (`:79`, `:89`) become `circle:not(.end)`.
- **No area fill.** An earlier draft tinted the region under the line; the review noted F094 §3
  makes zero-basing normative for *filled* charts precisely because a fill reads as magnitude
  from zero, and a sparkline is not zero-based. The focus window and trendline carry the look.
- **`trendline: boolean`** (default `false`, on `Sparkline` and `MetricCard`): draw a rolling
  mean as the main line with the raw series faint (1px, 35%) behind it. The window is
  renderer-owned (`max(3, round(n / 8))`) and is computed **per finite run** — never across a
  gap — so a break stays a break. The model can ask for "trend through the noise" but cannot
  tune a smoothing constant.
- **Focus window, source-declared:** a series may carry `meta.focus_from` (ISO string); the
  renderer shades the plot from the first point at or after it to the end. It is *series meta*,
  not a component prop, because the window boundary belongs to whoever computed the comparison
  ("last 28 days vs the 28 before" — the script knows the split; the model does not). Set with
  `to_series(..., focus_from=...)`, coerced through `_iso()` like `t` (a `date` must never reach
  JSONB); survives `_downsample_series` (which copies `meta`) and `_is_valid_series`; read by
  `readSeries` as `focusFrom`.
- The `~` zero-break marker keeps rendering exactly as today.

`MetricCard.trend` renders through the same SVG partial as `Sparkline` (one geometry, two
frames), so the treatment cannot drift between them. The partial is a Svelte component that
takes a resolved series + tone + trendline flag; `SparklineView` adds its head/foot around it,
`MetricCardView` embeds it bare.

### 4.4 `AppHeader.note`
Optional DynamicString under the freshness stamp, right-aligned and muted: `data through
2026-09-01`. The stamp says when the app was composed; the note says how far the data reaches.
They are different facts and the reference shows both.

### 4.5 `--font-numeric` token
New `:root` token `--font-numeric: var(--font-mono)`; every figure in the five components and
the standalone sparkline's current-value uses it with `font-variant-numeric: tabular-nums`.
The `var()` alias is safe here because no theme changes its mono stack (`signal` re-declares an
identical one); a theme that does must re-declare `--font-numeric` in its own block — stated as
a comment beside the token. **This changes the font of every existing sparkline's `.cur`**
from the UI face to the mono face; deliberate, so a figure looks the same everywhere.

---

## 5. Change 3 — `report` theme and `report` archetype (P1)

**Theme `report`** — "cool slate dark with a serif display — trend reports, scorecards,
periodic reviews." Palette lifted from the reference page: bg `#0a0c11`, surface `#131822`,
border `#212b3b`, text `#e9edf5`, muted `#8d99ae`, ok `#4ade80`, crit `#fb7185`, soft
`#94a3b8` (the reference's "flat" — deliberately LIGHTER than muted, so a neutral pill or
delta never reads weaker than its own caption); `--font-display: 'Iowan Old Style',
'Palatino Linotype', Palatino, Georgia, serif` (system faces — no webfont, no load cost);
series ramp led by the reference's `#f0abfc`. Contrast-checked by the existing `theme.test.ts`
guard, which grows from "all five" to "all six".

**Theme enumeration sites:** `compose._THEMES`; `companion.css` (new block); `theme.test.ts`
(`parsed all five` → six, and the per-theme AA loop); `tests/test_a2ui_dashboards.py::test_theme_enum_is_closed`
(exact-set assertion); `skills/live-dashboard/SKILL.md` theme line; `F093` is not edited.

**Archetype `report`** — "how are things moving vs the prior window": ScoreCards for the
objectives, DeltaLists of movers, MetricCard grids per source, a table of the raw lane, a
freshness ChipRow, a method note. Caps **80 components / 10 sections** — a third tier in
`caps_for` above `ledger`/`briefing`'s 80/8, because the reference already needs eight panels
(goals, movers, four source grids, table, freshness) and a method note makes nine; the prompt
also prescribes the packing (§8.1) so the model does not spend a section per list.

**Archetype enumeration sites:** `compose._ARCHETYPES` (recipe), `_GRAMMAR_RULES` budget line,
`_RESPONSE_SHAPE`, `tools.py` archetype enum **and its description gloss**,
`grammar.caps_for`, `tests/test_a2ui_dashboards.py` caps tests.

---

## 6. Data contracts

### 6.1 The metric grid is a record list with embedded series
The natural source shape for a MetricCard grid is one record per metric:

```
[{"label": "Recall p50", "value": "5.3", "unit": "s", "delta": "↓0.6 s · improving",
  "tone": "ok", "caption": "5.9 → 5.3 (28d median, n=28)", "footnote": "last 2026-09-01",
  "trend": {"kind": "series", "points": [...], "unit": "s", "meta": {"focus_from": "2026-08-04"}}}, …]
```

rendered by one `MetricCard` template under a Repeat, `trend: "trend"` resolving per item.
This is the first source shape that nests a series inside records, and it hits a budget hole:
`SourceRegistry.resolve` routes a **top-level** series through `_bound_series` (downsample to
fit) but a record list through `_bound` (pop tail records with a marker). Seventeen metrics
with 56-point sparks is ~34k chars (≈30 chars per date-stamped point; ≈49k with full ISO
timestamps) against a 12k per-source budget — `_bound` would keep four metrics and drop
thirteen, and the truncation marker would render as a blank card.

**Fix — `_bound` becomes series-aware for record lists.** When a list overflows: (1) collect
every direct record value for which `_is_valid_series` holds — **only those**; a malformed
nested series (`points: null`, `meta: "bad"`) is left untouched, because `_downsample_series`
would raise on it and a raise inside `resolve()` is a 500 where compose-time
`_chart_shape_errors` and render-time `readSeries` already degrade it honestly; (2) give each an
equal share of `budget − size_of_everything_else` and downsample it through `_bound_series`
(its exhausted-reason parameterised: "char budget exhausted for this record's series"); (3)
only if the list *still* overflows, fall back to the existing tail pop with its marker. Ordering
is the honest one: a metric with a coarser spark is still a true metric; a missing metric is a
missing fact. `meta.downsampled_from` stamps each shortened spark exactly as today, and the
helper returns the serialized size of what it kept so `spent` stays exact.

**Budget arithmetic the recipe must respect.** At the default 12k per-source budget a 17-metric
source with 56-point sparks lands at ~8–12 points per spark after fitting — too coarse for a
trendline or a focus window. So the recipe (§8.3) builds **one source per section panel**
(4–6 metrics each). Measured 2026-09-01: a fully-captioned record with a 56-point date-stamped
spark serializes to ~2.06k (`json.dumps` escapes `↓ → ·` to `\uXXXX`), so **five** metrics
(~10.3k) fit untouched and **six** (~12.4k) lose at most a couple of points per spark — never a
record. Four such panels ≈ 40k, the surface total. AC4 states the expected points.

### 6.2 `to_series(..., focus_from=)`
New keyword; stamps `meta.focus_from = _iso(focus_from)` when given. Documented in the skill recipe.

### 6.3 Preformatted strings
Every `value`/`delta`/`caption`/`footnote`/`status` is a string the script formatted. The
renderer never rounds, never adds units, never re-signs a delta.

---

## 7. Validation (P1) — every rule is a repair-loop input, and lint never raises

### 7.1 Grammar (`lint_micro_app`, data-free)
- The five names join `ALLOWED_COMPONENTS`; none joins `BINDING_MANDATORY` (their data props
  are DynamicValues that may legitimately be literals, exactly like `Timeline.items`).
- `MetricCard.trend`, when present, must be a non-blank string: `MetricCard 'x' has a blank
  trend — omit trend for a count`.
- `DataTable.columns` — with type guards, since lint runs BEFORE schema validation and a raise
  escapes both the repair loop and the fallback (the class fixed four times already in
  `grammar.py`): a non-list `columns` ⇒ one error; a non-dict entry ⇒ error; an entry without a
  non-empty string `key` ⇒ error; 1–6 entries; uniqueness computed over the string keys only.
  The clean message leads; the schema's `maxItems` backstops (so AC3 says "at least one error").
- `Section.layout`, `Sparkline.trendline`, `MetricCard.trendline` are schema-checked (enum / boolean).

### 7.2 Data-aware (`_binding_rules`) — ONE resolver, shared
The chart block resolves a component's path prop to its targets (absolute from root, or
per-item inside exactly one Repeat template, or left to the renderer when nested). That
resolution is factored into a single helper, `_resolve_path_targets(comp, prop, by_id,
full_model)`, and driven by three maps:

```
_SERIES_PATH_PROPS      = {"Sparkline": "path", "LineChart": "path", "BarChart": "path", "MetricCard": "trend"}
_SINGLE_VALUE_CONSUMERS = {"Sparkline", "BarChart", "MetricCard"}      # read the default `v` key
_ARRAY_VALUE_PROPS      = {"DeltaList": "rows", "DataTable": "rows", "ChipRow": "items",
                           "ScoreCard": "items", "Timeline": "items", "KeyValueTable": "rows"}
_OPTIONAL_DATA_PROPS    = {("MetricCard", "trend"), ("ScoreCard", "items")}
```

- **The subtree walk follows every child key.** Today's `_containing_templates` walks
  `_child_ids` (child/children/template only), while the grammar's `_children_of` also follows
  `trigger`, `content` and `tabs[].child`. Extending a resolver with that gap to six more
  components would false-reject a KeyValueTable inside a `Modal.content` inside a Repeat — a
  shape the prompt itself recommends. The shared helper uses `grammar._children_of`.
- **Series rule** (existing `_chart_shape_errors`) now runs for `MetricCard.trend`, and
  `MetricCard` joins the single-value-consumer check so a multi-series (`keys`) source is named
  rather than rendering "no data". `_collect_bindings` sees `trend` so the unread-source rule
  counts it.
- **Optional data props resolving to nothing are not errors.** For `(MetricCard, trend)` and
  `(ScoreCard, items)`, a target resolving to `None` is the "no trend / no evidence" state
  (§3.1, §3.2) — validated only when present. Chart `path` (BINDING_MANDATORY) stays strict.
- **Array rule** (new), precisely: a prop in `_ARRAY_VALUE_PROPS` that is absent ⇒ skip; a
  `{call}` FunctionCall ⇒ skip (renderer-owned); a `{path}` binding resolving to `None` ⇒ error
  for required props, skip for optional; otherwise the value (resolved, or the literal itself)
  must be a **list — possibly empty — whose entries are all objects**. `[]` passes (the primary
  compose fixture binds one). Under a Repeat every item is checked and the first failure
  rejects the component once. Error names the component, the path and what it resolved to:
  `DeltaList 'movers' binds /recall which resolved to a series, not an array of rows`.
  `Timeline` and `KeyValueTable` join the map because they are the same class (a Timeline
  bound to a series renders nothing today, silently); the rule only rejects a *wrong* shape, so
  every valid existing app passes unchanged.
- **Column rule** (new): when `DataTable.rows` passes the array rule and is non-empty, every
  `columns[].key` must be present in at least one row (`isinstance(row, dict) and key in row`)
  — an all-empty column is the F093 §1.1 "partial render of a complete source" failure in a
  new costume.

### 7.3 Schema (`catalog.json`)
Five component schemas with the props above (+ `weight`), `required` as marked, enums closed,
`columns` with `minItems: 1, maxItems: 6` and a closed item schema. `Section.layout` enum
gains `cards`; `Section` gains `caption` (DynamicString); `AppHeader` gains `note`;
`Sparkline` gains `trendline`. `$defs.anyComponent.oneOf` lists the five so the discriminator
resolves them. Tests assert a `style` and a `color` key on each new component, and a `color`
key inside a `columns` entry, fail `validate_envelope`.

---

## 8. Prompt, catalog summary, skill (P1)

### 8.1 Compose prompt
- `_COMPONENT_USAGE` gains one line each for the five, written data-contract-first (the
  #626 lesson), plus a `StatTile` line that spells its enum: `StatTile: headline tile, StatRow
  only (≤4); intent ∈ neutral|good|bad|warn (NOT tone — it colours the value); anything inside
  a Section is a MetricCard`. The MetricCard line spells `tone ∈ neutral|ok|warn|crit`.
- A `REPORT` block beside `CHARTS`, stating the shapes the grammar will otherwise reject:
  1. *Goals:* ONE Section (`layout: cards`) → Column → the ScoreCards (or a Repeat template
     over the goals list). *Metric grid:* ONE Section (`layout: cards`) → Column whose
     `children` is `{componentId: <one MetricCard>, path: "/<metrics>"}`. ScoreCards and
     MetricCards are never StatRow children and never root children.
  2. Inside that template `trend: "trend"` is a **bare string** like `Sparkline.path`, NOT
     `{"path": …}`, relative to the item. `DataTable.columns` is a literal array, never a binding.
  3. A `[records]` source whose records each carry a series is chartable **per item** through
     that template — `_build_prompt` tags such a source `records; each carries a series at:
     <key>` by scanning the first record for `is_series` values, so the model sees it.
  4. Tone colours the judgement, not the reading; `DeltaList` empty is a state — give it
     `emptyText`; `Section.caption` carries source attribution instead of polluting the title.
  5. Packing: both movers lists under one Section (`grid-2`, a Row of two DeltaLists); chips
     and the method note under one Section; one Section per source grid.
- `_ARCHETYPES`, `_RESPONSE_SHAPE`, `_GRAMMAR_RULES` (budget line, layout enum) updated.
- **Source description is capped per source, not globally.** `_build_prompt` truncates the
  whole source-sample JSON at 4000 chars; a report is the first archetype declaring 5–7
  sources, so the tail sources would be cut mid-JSON. Each source's sample is capped
  individually (≈800 chars, `ensure_ascii=False` so `↓ → ·` are not escaped to six bytes each)
  and the block is rebuilt from the capped parts, so every source stays visible.

### 8.2 Catalog summary
`_COMPONENT_ORDER` gets `MetricCard, ScoreCard, DeltaList, DataTable, ChipRow` after
`StatTile`. Measured against the current summary (~1.75k of the 4.8k-char budget), the five
lines add ~330 chars; no budget change, the guard test stays as-is. `dsl.py` gains a helper
per component, and `Section()`/`AppHeader()`/`Sparkline()` gain `caption`/`note`/`trendline`
kwargs so builders and fixtures can express the new props.

### 8.3 `skills/live-dashboard/SKILL.md` (version 1.1 → 1.2)
The component table gains five rows; the layouts line gains `cards`; the theme line gains
`report`; the script section gains the report recipe: build the per-metric record list in
Python (window means → delta string → tone by direction preference → caption), call
`to_series(rows, "t", "v", unit=…, focus_from=window_start)` for each spark with `t` as a
**date** (10-char stamps — a 56-point spark stays under 2k), one source per section panel,
return `shape: "records"`. The "Declare shape" and "Charts need a series" bullets — and the
matching `compose_surface` params description in `tools.py` — are corrected: `series` only
when the TOP-LEVEL `result` is one series; `records` for a list, **including a record list
whose records each embed a series**; `series_keys` applies to `series` only. (Declaring
`series` on the metric list today turns the whole grid into one empty series.)

**Propagation note (ops):** editing the file on disk does not reach a deployed agent —
`bootstrap.py` skips a procedure whose name already exists. After deploy, re-register with
`learn_skill(source=skills/live-dashboard/SKILL.md)` so the stored procedure body carries the
recipe.

---

## 9. Acceptance criteria

1. **Whole-feature fixture.** One app using every new component, `Section.layout: "cards"`,
   `Section.caption`, `AppHeader.note`, a Repeat over a metric record list with embedded
   `trend` series **where one record has no trend and one ScoreCard record has no items**,
   `trendline: true`, `meta.focus_from`, archetype `report`, theme `report` passes
   `lint_micro_app` → `_validate` → `validate_envelope`, and renders in the Svelte harness
   with every record present (`.metric` count equals the record count), `.app-section.cards`
   present and **no unknown-component placeholder** in the DOM.
2. **Strictness.** `style` / `color` on each new component, and `color` inside a `columns`
   entry ⇒ `validate_envelope` error.
3. **Named rejections.** A `MetricCard.trend` bound to a record list; a `MetricCard.trend`
   bound to a multi-series (`keys`) source; a `DeltaList.rows` bound to a series; a `DataTable`
   column key absent from every row; seven columns; `ScoreCard.items` resolving to a string —
   each yields **at least one** error naming the component, the grammar message first where
   both fire. `MetricCard.trend: ""` ⇒ grammar error.
4. **Budget honesty.** A record list with embedded series that overflows the per-source budget
   keeps every record with downsampled sparks (`meta.downsampled_from` set); a list that cannot
   fit even at the 2-point minimum pops records, with the marker; a record carrying a malformed
   nested series (`points: null`, `meta: "bad"`) leaves `resolve()` returning, never raising.
   A five-metric, 56-point, date-stamped, fully-captioned source fits the 12k per-source
   budget undownsampled; a six-metric one keeps all six records at ≥50 points each.
5. **Focus window.** `to_series(..., focus_from=date(...))` stores a string; it survives
   `_downsample_series`; `readSeries` yields `focusFrom`; the partial emits the shaded `<rect>`
   from the first point at or after it.
6. **Themes.** `theme.test.ts` parses six themes and every one meets AA on text/bg. An app
   without a theme, without any new prop **and without a Sparkline** renders the same DOM as
   before this change (asserted by the existing micro-app adapter tests, which are unchanged).
7. **Summary.** `catalog_property_summary()` names every new component with its required
   props and stays within `_TOKEN_BUDGET`.
8. **Sparkline treatment.** `charts.test.ts` `:79`/`:89` count `circle:not(.end)`; a series
   ending in an isolated reading shows one circle at that point; LineChart tests are untouched;
   a gap is never bridged by the trendline; the `~` marker still renders.
9. **Resolver parity.** A `KeyValueTable` inside `Modal.content` inside a Repeat with a
   relative `rows` path validates clean; the same shape bound to a series is named.
10. **Enumerations.** `grep -rn` for `briefing`, `alpine-dusk` and `grid-3` finds no list that
    lacks `report` / `cards`.

---

## 10. Phasing

| Phase | Contents |
|---|---|
| **P1 (this PR)** | §3 five components, §4 affordances, §5 theme + archetype, §6.1 `_bound`, §6.2 `focus_from`, §7 validation, §8 prompt/summary/skill, §9 tests |
| **P2 (follow-up)** | `window_summary()` analysis helper in `nous/a2ui/sources.py` once two dashboards have hand-written it; a `metric_records` source over `retrieval_log` so Nous's own trend report is one `compose_surface` call |

---

## 11. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | The composer reaches for `MetricCard` in the StatRow, or `StatTile` in a cards grid | StatRow already rejects non-StatTile children (grammar); the usage lines state the split; the whole-feature fixture uses both correctly |
| R2 | Two tone enums (`intent` vs `tone`) confuse the model | Both enums are spelled in the prompt on the component that takes them; `StatTile` is unchanged so no existing app moves; a later PR may alias `intent` |
| R3 | Series-aware `_bound` changes the size accounting `resolve()` relies on for `spent` | The helper returns the serialized size of what it kept, as `_bound_series` does; tested against `_TOTAL_BUDGET_CHARS` |
| R4 | End dot + `--font-numeric` change every existing sparkline | Deliberate, renderer-owned taste; the design canvas shows it; the two circle-count assertions are updated as §4.3 states |
| R5 | A 6-column DataTable on a 390px phone | Cells wrap; the cap is 6, and the skill says 4 is the comfortable maximum on a phone |
| R6 | The report needs more prompt than the composer's 60s round allows | The REPORT block is ~25 lines; the per-source sample cap (§8.1) keeps the data block bounded regardless of source count |
