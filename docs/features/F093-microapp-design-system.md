---
feature: F093
title: Micro-App Design System — themes, semantic tokens, computed components, layout
status: draft — for review
depends_on: F092 Phase 2 (catalog + renderers), F092.1 Phase 3 (compose/grammar/sources)
supersedes: nothing — additive to F092.1 §4 (the app grammar)
author: Nous
date: 2026-08-30
---

# F093 — Micro-App Design System

> **The problem in one line.** Every composed micro-app looks the same, and it looks
> machine-made. Not because the composer is bad, but because the grammar has no vocabulary
> for visual intent — the model can choose *what* to say and never *how it reads*.

---

## 1. Evidence — why this is a real gap, not taste

On 2026-08-30 Tim asked for "an app like the HTML page you built before" (the Italy
Departure Console: a hand-built 68 KB single-file page — Alpine-dusk palette, Fraunces
display over JetBrains Mono, computed countdowns, severity-ranked gates). The composer
cannot produce anything in that family. Measured, not assumed — and measured *by a script*:
every figure below is emitted by `docs/features/f093_evidence.py` against the working tree, not
hand-transcribed. Four adversarial review rounds on this document produced twelve P1 defects and
**every one of them was a stale or mis-scoped count in this table, never an error of argument**;
the counts are now generated so that class of defect cannot recur. Run it to re-derive the table:

| Observation | Measurement | Source |
|---|---|---|
| Component vocabulary | **24 names**, display primitives only. (A 5-name `BANNED_COMPONENTS` set of *input* widgets brings the declared total to 29; those are forbidden inside a micro-app, so they are not vocabulary.) | `grammar.py:44-73` (allowed), `grammar.py:38-40` (banned) |
| Escape hatch for custom styling | **none** — 0 hits for `dangerouslySetInnerHTML\|iframe\|rawHtml\|customCss` | `grep` across `nous/a2ui/`, `dashboard-app/src/companion/` |
| Theme tokens | **17 CSS variables**, single `:root` block | `companion.css` (45 lines) |
| Component references to those tokens | **189** `var(--…)` uses across **24 of 30** catalog views; **225** package-wide (the extra 36 sit in 4 shell/infra files: `Companion.svelte`, `companion.css`, `MarkdownInline.svelte`, `Renderer.svelte`) | `f093_evidence.py` |
| Structural caps | `MAX_COMPONENTS = 40`, `MAX_SECTIONS = 5`, `MAX_DEPTH = 5` | `grammar.py:32-34` |
| Token references with no `:root` definition | **1** — `var(--mono, monospace)`; `--mono` is not defined anywhere (the real name is `--font-mono`). It survives only because of its inline fallback. | `DecisionCardView.svelte:101` |
| Components that derive a *display value* (label, severity band) from bound data | **2 of 30** — `ConfidenceMeterView` (clamp + `low\|mid\|high` severity band), `AppHeaderView` (relative-time label + `stale` flag). Both are renderer-owned: the model places them, it cannot configure what they compute. | `ConfidenceMeterView.svelte:22-27`; `AppHeaderView.svelte:36-38` + `freshness.ts:12-38` |
| Components that compute *geometry* from bound data | **2 of 30** — `DagGraphView` (longest-path depth → column/row grid), `MemoryGraphView` (radial polar layout). Same rule: renderer owns the algorithm, model supplies only data. | `DagGraphView.svelte:69-104`; `MemoryGraphView.svelte:84-95` |

Two consequences fall out of that table.

**(a) The re-theming seam already exists and is unused.** 189 view-level token references
(225 package-wide) against exactly three hardcoded colour literals — `ButtonView.svelte:105`
(`color: #fff`), `MemoryGraphView.svelte:99` (`decision: '#a78bfa'`, sitting between three
siblings that correctly use tokens) and `ModalView.svelte:88` (`rgba(10, 10, 15, 0.72)`, the
dialog backdrop) — means swapping the 17 `:root` values re-skins every app
**bar those three lines**, which migrate to `--surface`, a new `--node-decision` token, and a
new `--scrim` token in the same PR. The `#a78bfa` case is the load-bearing one: it is a *semantic* colour (the
`MemoryGraph` node type `decision`), so today it renders identical purple under every theme. Theming is not an architecture change here — it is
a missing enum. Note also that `--accent` is `#7c6af7`, the default purple-on-dark that is
most of why the output reads as generated.

**(b) The console is literally unrepresentable.** It has 16 day cards, 5 bases, 4 gate
timers and 7 ledger rows — the day cards alone exceed `MAX_SECTIONS` and, once wrapped,
approach `MAX_COMPONENTS`. No amount of composer skill fits it through a 40/5 aperture.

### 1.1 A correctness finding that belongs in this spec

The Italy compose produced **12 components with exactly one path binding** (the mandatory
`/meta/composedAt`); all 7 resolved facts sat in `dataModel` unread, and every visible value
was an inlined string. Two failures follow: `app.refresh` becomes a no-op (nothing binds to
the data it patches), and provenance inverts — `model_supplied_keys` is derived from
*declared* dataModel keys, so a fully model-authored app reports **zero** model-supplied keys
and renders no amber. It published a false statement in that state ("free cancellation window
already closed Sep 5" against a source fact reading "free cancellation until midnight
Sep 5, 2026").

This is **not universal** — a re-run against `unreviewed_decisions` produced **36 bindings**
across `/pending/0..6`. The difference is source shape: record-array sources get bound,
prose-ish sources (`facts_search`) get inlined. `_validate` enforces neither.

It is in scope for F093 because the fix is the same fix: **components that cannot render
without a binding.** A `Countdown` with no `path` is meaningless; a `Text` with no `path` is
merely lazy. Aesthetics and correctness converge on one change.

---

## 2. Non-goals — the thing we must not build

**No raw CSS. No `rawHtml`. No `style` prop. No iframe escape hatch.**

The moment the model writes styles: (a) every app looks different-but-worse, because
per-app taste is exactly what a language model is weakest at and there is no reviewer in the
loop; (b) we inherit an injection surface on a path that already renders agent-authored
strings into the DOM; (c) the 190-reference token seam decays into 190 one-off overrides and
the next re-theme becomes impossible.

**The renderer owns taste; the model picks from it.** Every visual choice in F093 is a
*selection from a curated enum*, never free-form styling. This is the load-bearing
constraint of the whole spec — a reviewer who wants to relax it should argue against this
paragraph specifically.

### 2.1 Honest ceiling

Fully built, F093 yields **a well-designed dashboard, not a bespoke editorial page.** The
Departure Console is hand-authored HTML from a content model and stays that way. The routing
rule from 2026-08-30 stands: `compose_surface` for things you *press* (press = navigate, refine or close — never write;
see §9), bespoke HTML for
things you *read*. F093 moves the gap from *generic vs designed* to *systematic vs bespoke*.
That is the right place for the gap; it is not the elimination of it.

**Who decides an ambiguous request.** Nous decides, and defaults to `compose_surface`. If the
request names data Nous can source and the user will act on it, it is a micro-app. It is a
bespoke HTML deliverable only when the artifact is prose-shaped, leaves Nous (email, upload),
or Tim asks for a document by name. Tim overrides either way on request; the composer never
arbitrates this mid-compose, because by then the surface type is already chosen.

---

## 3. Change 1 — theme layer (P1)

### 3.1 Envelope

Add an optional `theme` to the app envelope, validated against a **closed enum**:

```
theme: "nous-default" | "alpine-dusk" | "harbor" | "paper" | "signal"
```

Unknown value ⇒ validation error (not silent fallback — a silently ignored theme is
indistinguishable from a broken one). Absent ⇒ `nous-default`, byte-identical to today's
render.

### 3.2 Renderer

Companion sets `data-theme="<id>"` on the **app root element, not `:root`** — two open apps
in the switcher (F092 Phase 4) must theme independently. `companion.css` grows one block per
theme overriding the same token set — 17 names today, 23 once §4 lands in this release
(`--ok`, `--warn`, `--crit`, `--locked`, `--soft`, `--font-display`):

```css
[data-theme='alpine-dusk'] {
  --bg: #0e1014;  --surface: #161a20;  --border: #232a33;
  --accent: #e8833a;   /* alpenglow */
  --locked: #5fb3a1;   /* glacier */
  ...
}
```

Themes are **hand-designed by a human, 4–5 of them, checked for contrast.** They are not
generated, not parameterized, not model-authored. Adding a theme is a PR.

### 3.3 Composer

`_build_prompt` gains a short theme menu (id + one line of intent — "alpine-dusk: warm
near-black, for trips and outdoor plans"). The model picks a name. It never sees or emits a
color.

---

## 4. Change 2 — semantic tokens (P1)

`--green/--red/--yellow` name a *color*, not a *meaning*, so a component hardcodes intent by
picking a hue. Add meaning-named tokens, each mapped per theme:

| Token | Means | default | alpine-dusk |
|---|---|---|---|
| `--ok` | healthy, done | `#34d399` | `#5fb3a1` |
| `--warn` | attention soon | `#fbbf24` | `#e8a33a` |
| `--crit` | acting now | `#f87171` | `#d9553f` |
| `--locked` | committed, immutable | `#5fb3a1` | `#5fb3a1` |
| `--soft` | provisional, changeable | `#8a8fa3` | `#8a8fa3` |

Every token in this table **must carry a `:root` default**, including the two that only
matter under a named theme. A token used by a view but absent from `:root` resolves to
nothing and the property silently drops — this is not hypothetical, it is already live at
`DecisionCardView.svelte:101` (`var(--mono, …)`, §1), which survives only because it happens
to carry an inline fallback. `--locked` and `--soft` therefore take the alpine-dusk values as
their universal defaults rather than a dash.

Also add **`--font-display`**. Today there is `--font-ui` and `--font-mono` only; the
console's character is substantially Fraunces-over-JetBrains-Mono, and the grammar cannot
express that split at all.

Existing `--green/--red/--yellow` stay as aliases for one release. Catalog views migrate to
semantic names in the same PR — 18 `--red` + 16 `--yellow` + 8 `--green` = **42 call sites**
across the 30 catalog views, mechanical. (Package-wide the three names appear 50 times; the
extra 8 are in `Companion.svelte` and the test suite, which migrate with the shell, not with
this change.)

---

## 5. Change 3 — computed components (P2)

Computation is not new to the renderer — it is new to the *model's* vocabulary. Four catalog
views already compute from bound data. Two derive a display *value*: `ConfidenceMeterView` clamps a bound
number and buckets it into a `low|mid|high` severity band (`:22-27`), and `AppHeaderView`
turns a bound ISO timestamp into a relative-time label and a `stale` flag
(`:36-38` + `freshness.ts:12-38`). Two more compute *geometry* — `DagGraphView` runs a
longest-path depth assignment to place nodes on a grid (`:69-104`) and `MemoryGraphView`
lays peers out on a circle by polar angle (`:84-95`). In both, the renderer owns the formula outright: the clamp bounds, the band
cutoffs and the relative-time format are literals in the component, reachable from no prop.
The model's reach is placement only. All four are freely placeable — `ConfidenceMeter`,
`MemoryGraph` and `DagGraph` are in `ALLOWED_COMPONENTS` (`grammar.py:44-73`) and `AppHeader`
is not merely allowed but *mandatory*: `_validate` rejects any app whose first child is not
one (`grammar.py:186`). The model may emit all four; it may program none of them.

So the gap is narrower and better-evidenced than "the grammar cannot compute": **every
computed component we have, the renderer owns the computation and the model cannot
reconfigure it.** Nothing general
exists, so "6 days to departure" can only be a string the model typed — which is exactly how
a stale or inverted value gets rendered with full confidence. F093 generalizes a pattern
already proven in production rather than introducing one; `ConfidenceMeterView` is the
working precedent for the severity-band behavior `GateTimer` needs.

Add five, each **binding-mandatory** (`path` required; no literal fallback):

| Component | Props | Behavior |
|---|---|---|
| `Countdown` | `path` (ISO 8601), `label`, `unit` | Client-side tick. Renders `6d 17h`. Correct tomorrow without recomposition. |
| `GateTimer` | `path`, `label`, `urgency`: `"relaxed"\|"standard"\|"tight"` (default `"standard"`) | Countdown + severity → `--ok`/`--warn`/`--crit`. Thresholds are **renderer-owned**, not model-supplied: relaxed = 14d/3d, standard = 7d/48h, tight = 48h/12h. |
| `PhaseBadge` | `path`, `stages[]` | Bound value located in an ordered stage list; renders current + position. |
| `DayCard` | `path` (date), `title`, `todayPath` | Timeline entry with a today marker. |
| `LinkStrip` | `path` (array of `{label,url}`) | Row of link pills. |

`Countdown`, `GateTimer` and `PhaseBadge` are what make an app **feel live**, and they do it
without `app.refresh` — the value is derived from a bound timestamp on every tick, so it is
correct on reopen by construction. The Departure Console's aliveness is exactly this and
nothing more.

**Client-side clock only.** No polling, no server round-trip, no timer that outlives the app.

**Why `urgency` is an enum and not `warnHours`/`critHours` numerics.** A free numeric that
drives a severity *color* is a visual choice made by the model, which §2 forecloses. Earlier
drafts of this spec took the numeric defaults (168/48) directly from the model — a real, if
narrow, breach of the load-bearing rule. Three curated bands cover every gate the Departure
Console actually has; if a fourth is genuinely needed, adding an enum member is a reviewed
one-line change, which is the point.

### 5.1 Binding enforcement (the correctness half)

Two `_validate` rules, both hard errors surfaced into the existing repair rounds:

1. **Unread-source rule.** If a `data_sources` key resolved non-empty and **no** component
   binds a path into it ⇒ reject: `source 'pending' resolved 7 records but no component binds
   /pending/*`. This is the direct fix for §1.1.
2. **Binding-mandatory components.** The five above with a missing/blank `path` ⇒ reject.

### 5.2 Prompt: ship the component property schema

`_build_prompt` currently passes intent + archetypes + grammar + a data sample + response
shape. The component list is **names only** — no props, no required fields, no types. The repair
rounds observed during F092.1 development were dominated by the model guessing props it was
never shown — three reproduced failures are `StatTile.icon` unexpected, `List.items` required,
`DecisionCard.decisionId` required. *(Provenance: these were counted by hand from a single
uncommitted compose session and no log survives in the repo. The three named failure modes are
reproducible from the schema; the ratio is not, so no ratio is claimed. Acceptance criterion 8
measures this properly.)* Meanwhile ~69 KB of
catalog JSON ships in the package unused.

F092.1 Phase 3 listed "catalog summary into the prompt (~800 tokens)" as a step; it was not
implemented. **This is a dropped step, not a model limitation.** Implement it: name +
required props + prop types, compact. This work is already delegated (job
`job-20260830-030756-525f1dad`); F093 absorbs it so the schema covers the five new
components in the same pass.

---

## 6. Change 4 — layout and caps (P3)

### 6.1 Section layout

Add `Section.layout`, closed enum: `stack` (default, today's behavior) | `hero` | `grid-2` |
`grid-3` | `rail`.

Every section renders as a uniform vertical stack today, so **nothing can be visually
primary**. No hero ⇒ no hierarchy ⇒ it reads as a dashboard regardless of content. This is
the single largest "looks generated" contributor after `--accent`.

### 6.2 Repeater

Bindings today are index-expanded: the verified decision-queue app emitted
`/pending/0/...` through `/pending/6/...` — 36 hand-written bindings for 7 records, and it
silently renders 7 when the source returns 12. Add `Repeat`:

```
{ "type": "Repeat", "path": "/pending", "max": 20, "template": { ...one component... } }
```

Counts as **one** component against the budget. Fixes correctness (all records render) and
budget pressure at once, and is a prerequisite for §6.3 being meaningful.

### 6.3 Caps

Raise for `ledger` and `briefing` archetypes only: `MAX_COMPONENTS 40 → 80`,
`MAX_SECTIONS 5 → 8`. `MAX_DEPTH` stays at 5 — depth is a complexity smell, not an
expressiveness need. With `Repeat`, 80 is generous; without it, even 80 does not fit a
16-day itinerary.

---

## 7. Phasing

| Phase | Contents | Rationale |
|---|---|---|
| **P1** | Themes (§3) + semantic tokens (§4) | Small, no grammar change, no new components. Visibly changes the next app Tim opens. |
| **P2** | Computed components (§5) + binding enforcement (§5.1) + catalog summary (§5.2) | Aesthetics and correctness in one pass; absorbs the in-flight job. |
| **P3** | `Section.layout` + `Repeat` + caps (§6) | **Smaller than it looks — the renderer half already exists.** `Children.svelte:24-52` already expands a `{componentId, path}` template over a bound data-model array with a per-item `Scope{base,index}`. The real gap is one layer up and it is *unreachable prior art*: `_children_of` (`grammar.py:79-90`) only collects `str` and `list[str]` children, so a template child is invisible to both the reference check (`:147`) and the depth walk (`:219`); and `_build_prompt` (`compose.py:270-290`) never teaches the composer the shape — `grep componentId compose.py` returns nothing. So P3 is grammar + compose work against a renderer that is already built and untested on this path. This **lowers** the P3 estimate. |

P1 ships alone and is worth shipping alone.

---

## 8. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | Theme enum grows into free-form styling by increments | §2 is normative. A PR adding a `style`/`css`/`color` prop is rejected on this section alone. |
| R2 | Hand-designed themes have contrast failures | Contrast check (WCAG AA on text pairs) in the theme test; themes are code, so they get tests. |
| R3 | Binding enforcement raises fallback rate | Repair rounds already absorb prop errors (observed: 1–2 rounds). Measure fallback rate before/after; if it rises, the catalog summary (§5.2) lands first. |
| R4 | Composer latency — an observed round took **44.6s** against a 60s timeout | Two slow rounds already risk timeout-fallback. Measure p50/p95 per round; consider raising the ceiling or streaming. Independent of F093 but it will bite harder with a longer prompt. |
| R5 | Raising caps raises token cost per compose | Caps raised only for two archetypes; `Repeat` cuts component count for the list-heavy cases that motivated the raise. |
| R6 | `--font-display` webfont adds load time | Self-host, subset, `font-display: swap`. Companion is a PWA — it caches. |

---

## 9. Out of scope / deliberately undecided

- **Per-app custom themes generated per intent.** Enum only. Revisit only with a human design pass.
- **Interactive/mutating components inside micro-apps.** F092.1 §3 keeps apps navigable-readonly; F093 adds no writes.
- **Replacing bespoke HTML deliverables.** See §2.1.
- **The censor-gate domain-scoping question** (`_censor_gate` → `check_censors_chunked` passes `where=` for *logging only*; `heart.check_censors(chunk)` receives no domain, so write-path censors block read-only display surfaces). Verified 2026-08-30 at `service.py:424-457` and `:1013-1043`. Real, adjacent, and **tracked separately** — it is a security-posture decision, not a design-system one.

---

## 10. Acceptance criteria

1. A composed app with `theme: "alpine-dusk"` renders in the alpine palette; the same app with the theme removed renders byte-identically to today's `nous-default`.
2. Two apps open simultaneously in the switcher with different themes render correctly and independently.
3. Every catalog view references semantic tokens; `grep` for `--green|--red|--yellow` in `catalog/` returns only the alias definitions.
4. A `Countdown` bound to a departure timestamp shows a value that decrements without recomposition, and is correct after the app is closed and rebuilt the next day.
5. A compose whose source resolves non-empty but binds nothing is **rejected** by `_validate`, with the source key named in the error.
6. The Italy status app, recomposed under F093, contains ≥1 `GateTimer` bound to the Tre Cime gate and **zero** inlined date literals contradicting the bound source.
7. A `ledger` app rendering a 12-record source shows all 12 via one `Repeat`, not 12 index-expanded binding sets — **and** a source whose record count exceeds the renderable capacity of the app's `Repeat`/bound slots is **hard-rejected by `_validate` with the source key and both counts named**, never silently truncated. A partial render of a complete source is the §1.1 failure in a new costume.
8. Fallback rate over 20 composes is **≤** the pre-F093 baseline.
