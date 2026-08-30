---
feature: F094
title: Micro-App Visualization Vocabulary — series primitives and series-shaped sources
status: draft — for review
depends_on: F093 §4 (semantic tokens), F093 §5 (computed-component pattern), F092.1 Phase 3 (compose/grammar/sources)
supersedes: nothing — additive to F093
author: Nous
date: 2026-08-30
---

# F094 — Visualization Vocabulary

> **The problem in one line.** A micro-app cannot draw a trend. Not badly — *at all*. The
> component vocabulary has no primitive that turns a sequence of numbers into a shape, so
> every "how has this moved" question routes to hand-authored HTML that leaves the companion
> and cannot refresh.

---

## 1. Evidence

Every figure below was re-derived against `origin/main` @ `e05bbb7` on 2026-08-30.

| Observation | Measurement | Source |
|---|---|---|
| Chart primitives in the app grammar | **0** — `grep -ciE 'chart\|spark\|series\|axis\|plot'` over the grammar returns 0 | `nous/a2ui/grammar.py` |
| Component vocabulary | **24 names**; the two graph components (`MemoryGraph`, `DagGraph`) are *topology* — nodes and edges — not series over an axis | `grammar.py:44-73` |
| The target artifact | `health_dashboard.html`, **56,679 bytes**, **17** inline `<svg>` blocks, **23** `<polyline>`, **17** `<rect>`, **17** `<circle>`, **0** charting libraries | `/tmp/nous-workspace/health/health_dashboard.html` |
| Renderer-owned SVG precedent | **3 of 34** catalog files already emit `<svg>`: `DagGraphView`, `MemoryGraphView`, `IconView` | `dashboard-app/src/companion/catalog/` |
| Geometry already computed renderer-side from bound data | **2** — `DagGraphView` longest-path depth → grid, `MemoryGraphView` radial polar layout | F093 §1, §5 |
| Chart primitives added by F093 | **0 of 5** — `Countdown`, `GateTimer`, `PhaseBadge`, `DayCard`, `LinkStrip` | F093 §5 |

Three consequences.

**(a) The capability gap is a vocabulary gap, not an architecture gap.** The renderer already
draws SVG from data it computes itself, in two components, in production. A `Sparkline` is
strictly *less* work than `MemoryGraphView`'s polar layout — one axis instead of two, no edge
routing. Nothing new has to be invented; a shape has to be named.

**(b) The target is entirely hand-rolled.** 17 SVG blocks and zero libraries means the health
dashboard's charts are not a library integration we could adopt — they are bespoke markup
generated once, per artifact, by a model writing raw SVG into a file. That artifact is a dead
snapshot: no live source, no `app.refresh`, no reopen. Its charts are correct only on the day
it was written.

**(c) F093 deliberately stops short of this, and says so.** F093 §2.1 fixes its own ceiling at
"a well-designed dashboard, not a bespoke editorial page," and routes *read*-shaped artifacts
to bespoke HTML. That routing rule is right for prose. It is wrong for a **trend**, which is
the one read-shaped thing that is worth *more* live than static — a sparkline of the last 30
days is a different fact tomorrow. F094 moves exactly that class, and no more, across the line.

### 1.1 Why this is the blocking layer, not themes

The open question this spec closes was framed as three layers: viz, themes, interaction.
Themes are **already specified** — F093 §3 (closed `theme` enum, `data-theme` on the app root,
one `companion.css` block per theme) and F093 §4 (semantic tokens plus `--font-display`).
Re-specifying them would duplicate a merged document. Interaction is deliberately out of scope
(F093 §9). Viz is the only one of the three with no home, and it is the one the health
dashboard actually needs.

---

## 2. Non-goals — inherited normatively from F093 §2

**No raw SVG. No `style` prop. No `color` prop on a series. No point-level styling.**

F093 §2 is load-bearing here and is inherited verbatim: *the renderer owns taste; the model
picks from a curated enum*. A charting API is the single easiest place to breach that rule,
because every real charting library is a styling API wearing a data costume. The specific
foreclosures:

- The model supplies **data and meaning**, never geometry. No width, height, viewBox, margin,
  tick count, or path string.
- Series colour is **assigned by the renderer** from an ordered token ramp, or selected from
  the semantic enum (`ok` / `warn` / `crit` / `neutral`). The model may say *what a series
  means*; it may not say what colour it is.
- **Not a grammar of graphics.** Four primitives, closed set. No composable axes, scales,
  layers, or encodings. Adding a fifth chart is a reviewed PR, exactly as adding a theme is.

**Honest ceiling, restated for this spec.** Fully built, F094 yields *the health dashboard's
charts inside a live micro-app.* It does not yield the health dashboard: that page's character
is also its typography, its bespoke section rhythm, and its editorial copy. F093 §3/§4/§6.1
narrow that remainder; they do not erase it. The gap after F094 is **layout personality**, not
capability.

---

## 3. Change 1 — series primitives (P1)

Four components, each **binding-mandatory** in the F093 §5.1 sense: `path` is required, no
literal fallback, missing/blank `path` is a hard `_validate` error.

| Component | Props | Behavior |
|---|---|---|
| `Sparkline` | `path`, `label`, `tone`: `"neutral"\|"ok"\|"warn"\|"crit"` (default `neutral`) | Inline single-series line, no axes, no grid, no labels on points. Sized by its slot. The 80% case. |
| `LineChart` | `path`, `label`, `series[]` (`{key, label, tone}`), `xLabel`, `yLabel` | Multi-series line with axes and a legend. Max 4 series (renderer-enforced). |
| `BarChart` | `path`, `label`, `orientation`: `"vertical"\|"horizontal"` (default `vertical`), `tone` | Categorical bars. Horizontal is the readable choice for long category names; that is why it is an enum and not a CSS decision. |
| `AreaChart` | `path`, `label`, `tone`, `stacked`: bool (default `false`) | Line with filled region; stacked for part-to-whole over time. |

**Renderer-owned, model-unreachable, in every case:** scale selection and domain (including
whether the y-axis is zero-based — a correctness property, not a taste one), tick count and
format, downsampling, curve interpolation, margins, the empty state, the single-point state,
and the colour ramp. This is the F093 §5 pattern applied unchanged: *the model places the
component and supplies the data; the renderer owns the algorithm.*

**Axis zero-basing is normative.** `BarChart` and `AreaChart` always include zero in the
y-domain. `LineChart` and `Sparkline` do not, and render a `~` break marker when the domain
excludes zero. A truncated bar axis is the most common way a chart lies, and the model must
not be able to choose it.

### 3.1 Degenerate inputs are renderer-owned states, not errors

| Input | Render |
|---|---|
| 0 points | Empty state: label + "no data", muted. Not a blank box, not an error card. |
| 1 point | The value as a `StatTile`-style figure with a "single reading" note. Never a one-point line. |
| All-equal values | Flat line at mid-height, domain padded. Never a divide-by-zero collapse. |
| Non-finite (`NaN`, `null`, `±Inf`) | Point dropped, gap in the line, count of dropped points surfaced in the label. Never coerced to 0 — a dropped reading and a zero reading are different facts. |

---

## 4. Change 2 — series-shaped data sources (P1, the real work)

The primitives are cheap. **This is the load-bearing half of F094.**

Today's registered fetchers (`nous/a2ui/sources.py`, `SourceRegistry`) return *record lists* —
decisions, DAG nodes, findings, episodes. A chart needs an ordered `(t, v)` sequence, and no
fetcher produces one. Absent this change, `Sparkline` has nothing to bind.

**Add a series contract.** A source may declare itself series-shaped by returning:

```
{ "kind": "series",
  "points": [ { "t": "<ISO 8601>", "v": <number> }, ... ],
  "unit": "<string>",        // renderer displays, never parses
  "meta": { "dropped": <int>, "downsampled_from": <int|null> } }
```

Ordering, gap policy, and the `t` type are the **source's** responsibility, not the model's and
not the renderer's — the renderer receives a sorted, typed sequence or rejects it.

**Add a normalizer, `to_series(records, t_key, v_key)`,** so an existing record-list fetcher
becomes chartable without a rewrite. This is what makes the change small: `recent_episodes`,
`unreviewed_decisions` and `subtasks` all gain a `count-over-time` view through one helper
rather than three new fetchers.

**First-party series sources to register:**

- `health_series` — from `/tmp/nous-workspace/health/health.db`. The direct unlock for the
  motivating case.
- `decision_outcomes_series` — resolved decisions over time by outcome, which makes the
  calibration loop visible in a surface instead of a report.
- `dag_throughput_series` — completed/failed nodes per day.

**Budget interaction.** A series is *data*, not components: 200 points cost one component
against `MAX_COMPONENTS`, not 200. This is the same win `Repeat` (F093 §6.2) delivers for
records, and it is why F094 does not need the cap raise F093 §6.3 argues for.

### 4.1 Point cap and downsampling

Cap the wire payload at **200 points per series**; a source returning more is downsampled
**server-side** (largest-triangle-three-buckets, which preserves visual extrema) with
`meta.downsampled_from` set so the renderer can stamp it. Never truncate to the first 200 — a
partial series that looks complete is the F093 §1.1 failure in a new costume, and here it is
worse, because a truncated trend line reads as a *finished* trend.

---

## 5. Change 3 — validation and prompt (P1)

Three `_validate` rules, all hard errors surfaced into the existing repair rounds:

1. **Binding-mandatory** — the four components with a missing/blank `path` ⇒ reject.
2. **Series-shape rule** — a viz component whose `path` resolves to something that is not
   `kind: "series"` ⇒ reject, naming both the component and what the path actually resolved to.
   This is the failure the model will make most often: binding a chart to a record list.
3. **Series-arity rule** — `LineChart.series[]` longer than 4, or naming a `key` absent from
   the resolved points ⇒ reject.

**Prompt.** F093 §5.2 ships the component property schema into `_build_prompt`. F094 rides
that same pass: the four components enter the schema with their props, and the source menu
gains a `kind` marker so the model can see which sources are chartable *before* it binds. A
model that cannot tell a series from a record list will guess, and rule 2 will catch it every
time — which is a repair round we can avoid by showing it.

---

## 6. Phasing

| Phase | Contents | Rationale |
|---|---|---|
| **P1** | `Sparkline` + `LineChart` (§3) + series contract, `to_series`, `health_series` (§4) + validation (§5) | The narrowest slice that answers the motivating request. `Sparkline` alone covers most of what the health dashboard shows. |
| **P2** | `BarChart` + `AreaChart` + `decision_outcomes_series`, `dag_throughput_series` | Categorical and part-to-whole. Turns F094 inward — Nous's own calibration and throughput become surfaces. |
| **P3** | Downsampling (§4.1) beyond a naive stride, break markers, dropped-point stamps | Polish. Only bites at high point counts; a naive stride is acceptable in P1 provided `meta` is honest about it. |

**F094 P1 depends on F093 P1** (semantic tokens `--ok`/`--warn`/`--crit` must exist before
`tone` can resolve). It does **not** depend on F093 P2 or P3. If F093 P1 ships and F094 P1
follows, the companion can draw a live trend without `Repeat`, without the cap raise, and
without the computed components.

---

## 7. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | `tone`/`series` grows into a colour API by increments | §2 is normative and inherited from F093 §2. A PR adding `color`, `fill`, `stroke` or `palette` to a viz prop is rejected on this section alone. |
| R2 | The model binds a chart to a record list | §5 rule 2, plus the `kind` marker in the source menu (§5). Measure how often rule 2 fires; if it dominates repair rounds, the menu is not explicit enough. |
| R3 | Chart SVG inflates the app payload | Points are data, not components (§4). Cap at 200/series (§4.1). Measure payload before/after on a 3-chart app. |
| R4 | A chart renders a misleading axis | Zero-basing is normative (§3), break markers are renderer-drawn, and the model cannot reach the domain. |
| R5 | `health.db` schema drift silently empties a series | The fetcher validates shape and returns an explicit empty series with a reason; §3.1 renders an empty state, never a blank box. |
| R6 | Composer latency — F093 R4 records an observed **44.6s** round against a 60s timeout | F094 lengthens the prompt further (4 components + source `kind` markers). This compounds an existing risk; measure p50/p95 per round alongside F093 P2, not after. |

---

## 8. Out of scope

- **Interaction with a chart** — hover tooltips, brushing, zoom, click-to-filter. All are
  input, and micro-apps stay navigable-readonly (F093 §9). This is the L3 question and it is
  held deliberately: per-app capability grants are a security-posture decision, not a design
  one.
- **Pie and donut charts.** Excluded on merit, not scope — they encode poorly and the model
  will reach for them.
- **Scatter, heatmap, candlestick.** No motivating case. Adding one is a reviewed PR.
- **Replacing bespoke HTML deliverables.** F093 §2.1 stands; F094 narrows what routes to it,
  and the remaining gap is layout personality (§2), not capability.
- **Client-side aggregation.** The source aggregates. The renderer draws what it is given.

---

## 9. Acceptance criteria

1. A composed app binding `Sparkline` to `health_series` renders a line whose extrema match
   the underlying rows in `health.db`, verified by an independent query.
2. The same app, reopened the next day, shows a **different** line without recomposition —
   proving the live-vs-snapshot distinction over the hand-authored artifact.
3. A compose that binds `LineChart` to a record-list source is **rejected** by `_validate`,
   with both the component and the resolved shape named in the error.
4. A series of 5,000 points renders with `meta.downsampled_from = 5000` and visible extrema
   preserved; the wire payload carries ≤ 200 points.
5. A 0-point, a 1-point, and an all-equal series each render their §3.1 state — no blank box,
   no one-point line, no collapsed axis.
6. A `BarChart` y-axis includes zero in every composed app; grep of the renderer shows no path
   by which a model-supplied prop reaches the domain.
7. A 3-chart app's payload is within 2× the same app with the charts removed (points are data,
   not components).
8. Fallback rate over 20 composes is **≤** the post-F093 baseline.
