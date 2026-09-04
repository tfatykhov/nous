---
name: live-dashboard
description: Build a live, self-refreshing companion dashboard for ANY domain — including one with no registered data source — by writing the data-producing Python yourself as an agent_script source. Use when the user asks to see, track, monitor, or chart something over time rather than be told about it once.
domain: interface
triggers:
  - build me a dashboard
  - track this over time
  - show me a chart of
  - monitor
  - can I see
  - keep an eye on
  - instead of emailing me
frames:
  - conversation
  - task
  - debug
tools:
  - compose_surface
  - run_python
version: "1.8"
---

## When this beats prose

A dashboard is right when the answer is **a shape, not a sentence** — a trend, a
comparison, a standing status the user will look at again tomorrow. If the
answer is a single fact they need once, just say it.

The test: *would they ask this again next week?* If yes, build the app — it
refreshes; a message doesn't.

## First: is `agent_script` actually available?

It is **flag-gated and off by default** (`NOUS_A2UI_AGENT_SCRIPT_SOURCE_ENABLED`).
`compose_surface`'s own `source` description is generated from the LIVE registry,
so it lists exactly what exists right now — **read it before writing a script**.

If `agent_script` is not there, do not name it: compose rejects an unknown
source and you lose the whole app. Bind a registered source instead, or supply
the data yourself in `dataModel` and say plainly that it is a snapshot which
will not refresh. Then mention the flag, so the user can decide whether to
enable it.

## What makes it live, when it is available

Every other data source is a fetcher someone wrote in Python months ago. If the
user's domain has no fetcher, **write it yourself** with an `agent_script`
source. It is stored with the app and **re-run on every refresh** — which is
the whole difference between a dashboard and a screenshot.

Without a data source an app is refused a refresh entirely. Model-supplied
numbers are frozen at compose time and render amber. That is not a dashboard,
it is an email with better typography.

```jsonc
compose_surface(
  intent: "Track my portfolio drawdown daily",
  data_sources: [{
    "key": "dd",
    "source": "agent_script",
    "params": {
      "code": "import urllib.request, json\n"
              "from nous.a2ui.sources import to_series\n"
              // timeout= is not optional: a hung socket read cannot be
              // interrupted, and this runs again on every refresh.
              "raw = json.load(urllib.request.urlopen('https://api.example.com/nav', timeout=10))\n"
              "rows = [{'t': r['date'], 'v': r['drawdown']} for r in raw['series']]\n"
              "result = to_series(rows, 't', 'v', unit='%')",
      "shape": "series",
      "series_keys": ["v"]
    }
  }]
)
```

## Writing the script

- **Assign to `result`.** Nothing else is read.
- **Declare `shape`** — `"series"` ONLY when the top-level `result` is one
  series (one chart); `"records"` for a list — a table, a list, **or a metric
  grid whose records each embed their own `trend` series** (see the report
  recipe below). On a later failure the source returns a value of that shape,
  so the app's existing bindings survive; declare the metric list as
  `"series"` and the whole grid becomes one empty series.
- **`series_keys` is required for `shape="series"`** (and applies only there).
  `["v"]` for a Sparkline or BarChart; the LineChart's own keys otherwise. It
  pins the contract so a refresh cannot silently change single↔multi series
  and leave the chart empty.
- **A chart binds a series, not rows.** `from nous.a2ui.sources import to_series`
  then `result = to_series(rows, "t", "v", unit="…")`. Multi-series:
  `to_series(rows, "t", "v", value_keys=["ok", "bad"])`. A series nested
  inside a record (`{"label": …, "trend": to_series(...)}`) is chartable per
  card through a `MetricCard` template.
- **It must be self-contained and deterministic.** It runs again, unattended,
  with nobody watching — not once while you are here. Compute from scratch;
  never rely on a variable you set in this turn.
- **Reads only.** `recall_deep` / `recall_recent` / `list_tasks` are in scope;
  `learn_fact` raises. A write would repeat on every refresh.
- **Verify before shipping.** Run the same code through `run_python` first and
  look at the numbers. A dashboard that renders a wrong trend confidently is
  worse than no dashboard.

## The report recipe (how are things moving vs the prior window)

A trend report — goals with verdicts, what's working / what's slipping, a
metric grid per source, the raw lane, data freshness — is the `report`
archetype. The script computes the **judgement**; the renderer only draws it.
Per metric, produce one record; **one source per section panel** (4–6 metrics
each — a 56-point spark per record is ~2k chars, and a source has a 12k
budget), and return `shape: "records"`:

```python
from statistics import mean, pstdev
from nous.a2ui.sources import to_series

def metric(label, rows, unit, better="down", fmt="{:.1f}"):
    # rows: [{"t": date, "v": float}] over the last 56 days, ascending
    cur, prev = [r["v"] for r in rows[-28:]], [r["v"] for r in rows[-56:-28]]
    delta = mean(cur) - mean(prev)
    scatter = pstdev(cur + prev) or 1.0
    z = abs(delta) / scatter
    good = (delta < 0) == (better == "down")
    tone = "neutral" if z < 1 else ("ok" if good else "crit")
    word = "holding steady" if z < 1 else ("improving" if good else "worsening")
    arrow = "↑" if delta >= 0 else "↓"
    return {
        "label": label,
        "value": fmt.format(rows[-1]["v"]),
        "unit": unit,
        "delta": f"{arrow}{fmt.format(abs(delta))} {unit} · {word}",
        "tone": tone,
        "caption": f"{fmt.format(mean(prev))} → {fmt.format(mean(cur))} (28d mean, n={len(cur)})"
                   + (" · strong" if z >= 2 else ""),
        "footnote": f"last {rows[-1]['t']}",
        # the spark keeps its comparison window: the renderer shades from focus_from
        "trend": to_series(rows, "t", "v", unit=unit, focus_from=rows[-28]["t"]),
    }

result = [metric("Recall p50", latency_rows, "s"), metric("Nodes failed", fail_rows, "/day")]
```

Bind that source with `Section(layout: cards)` → a Column whose children is
a repeat template of ONE `MetricCard` (`trend: "trend"`, `tone: {"path":
"tone"}`, the other fields as `{"path": …}` bindings). Goals are `ScoreCard`s
the same way (`status`, `tone`, `items` = the evidence rows); movers are two
`DeltaList`s (`rows = [{label, delta, from, to, tone}]`, and give the adverse
one an `emptyText` — an empty list is good news, not a missing section); the
raw lane is a `DataTable` behind an `accordion`; lane health is a `ChipRow`.
Put source attribution in `Section.caption`, the data-reach line in
`AppHeader.note` — bound to a field of a small `summary` source your script
returns (`{"reach": "data through …", "window": "28d vs prior 28d"}`), never
to `/meta`, which the server owns and fills with `composedAt` only — and
pick the `report` theme.

**Tone is bindable on the cards and only on the cards.** `MetricCard.tone` and
`ScoreCard.tone` accept `{"path": "tone"}`, so one repeat template gives every
card its own verdict colour, resolved on every refresh. `StatTile.intent`
(`neutral|good|bad|warn` — a *different* vocabulary) and `Sparkline.tone` are
**literal props the renderer never resolves as bindings**: a StatRow's colour
has to be read at build time from the same source record the tiles bind, and
it is a snapshot until the next publish. Before F096 that limitation forced a
whole workaround — N hand-authored Sparklines with baked tones, kept honest by
a daily republish. Do not carry that pattern into a new app: use MetricCards.

**Indexed pointers are fine when the array is generated from a static table.**
`/summary/0/goals/2/status` is safe if the goals list comes from a constant in
your script, and it buys you N *authored* cards you can put in N tabs — which
a repeat template cannot do, since it is one component. Never index into a
list whose length depends on data availability.

## Choosing the shape of the app

| The question | Component |
|---|---|
| how has this moved | `LineChart` (multi-series) or `Sparkline` (one, inline) |
| how do these compare | `BarChart` — zero-based, so bars never lie |
| where does it stand right now | `StatTile` in a `StatRow` (≤4 headline tiles; `intent` ∈ neutral\|good\|bad\|warn) |
| one metric's full story — value, delta pill, caption, embedded trend | `MetricCard` (tone on the pill and trend, never the value; grid them with `layout: cards` + a repeat template) |
| a verdict on an objective with its evidence | `ScoreCard` (status pill + tone, evidence rows each with their own tone; no value needed) |
| what moved, ranked | `DeltaList` (`label · delta · from → to`; set `emptyText` — empty is a state) |
| a real table (numbers right-aligned, a de-emphasised column) | `DataTable` (≤6 literal `columns`, `align: end`, `secondary`) |
| lane health / data freshness at a glance | `ChipRow` |
| what happened, in order | `Timeline` |
| the flat facts | `KeyValueTable` |
| the same subject two ways | `Tabs` (2-5 alternative views; only one renders at a time) |
| a Nous decision needing review | `DecisionCard` (+ `ConfidenceMeter`), fed from `unreviewed_decisions` |
| a running DAG's shape | `DagGraph`, fed from the `dag` source |
| long secondary detail | `Modal` behind a tappable label, or an `accordion` section |

Layouts: `hero` for the one thing that matters, `grid-2`/`grid-3` for peers,
`cards` for a grid of `MetricCard`s / `ScoreCard`s (the child must be a
Column or Row — a list of cards, or a repeat template of one), `rail` for a
scrollable strip, `accordion` for long secondary detail collapsed until
tapped, `stack` for the rest. `Section.caption` carries source attribution
or the comparison window — never put it in the title. Pick a `theme` that
fits the subject — `signal` for alerting, `paper` for reading, `report` for
trend reports and scorecards, `harbor`/`alpine-dusk` for ambient,
`nous-default` when unsure.

You never author pixels or colors: the composer picks components from a closed
catalog and the renderer owns geometry, scale, and palette. The full component
list with required/optional props, and WHEN to use the easy-to-miss ones, is
generated into the compose prompt from the catalog itself (#626) — so do not
memorize props here; state the intent and the data and let compose lay it out.
`Modal`'s trigger must be a `Text` or `Icon`: a `Button` is structurally
impossible (any component carrying an `action` is rejected).

## Fold instead of scroll — Tabs and accordion

A long stack is the default failure mode: everything equally sized, nothing
findable. Two ways to fold it, and they are not interchangeable.

**`Tabs` — alternative views of ONE subject.** `tabs: [{title, child}]`, where
each `child` is a component id in the flat list, exactly like a Section child.
**2-5 tabs, enforced by the grammar.** Only the active panel renders — an
inactive chart is unmounted, not hidden, so its bindings do not run. Reach for
Tabs when the panels answer the same question differently (temps / rain /
freezing level; by day / by base / by leg). Use separate Sections when the user
should see both at once — tabs hide things, and hidden things get forgotten.

**`Section.layout: "accordion"` — collapsed until tapped.** For the block that
must be *present* but not *loud*: the ledger, the raw rows, the appendix. Never
the headline. Like Tabs, a collapsed panel is not rendered at all.

**A report can be tabs the whole way down, and the depth arithmetic is exact.**
`root(1) > Section(2) > Tabs(3) > Column-repeat(4) > MetricCard(5)` lands on
`MAX_DEPTH` with nothing to spare — a card grid folds under Tabs precisely
because F096 made the card a LEAF. A two-component card (a Column of StatTile +
Sparkline) needs six and will not go. So: fold a metric grid, a movers pair
(Working / Slipping), goals (one ScoreCard per tab) and the supporting lane
(raw table / freshness chips / method note) into four tabbed Sections, and the
whole report is one card-height tall with a StatRow above it holding the
glance. That is ~32 components instead of ~50 stacked ones.

**A tab panel can be RICH, as long as every child is a leaf.** The depth rule
cuts both ways and the generous half is easy to miss: `root(1) > Section(2) >
Tabs(3) > Column(4) > leaf(5)` means one tab can hold an `Image` + a second
`Image` + a `ScoreCard` + a `Text` **side by side in one Column** — four
components, all at depth 5, all legal. What it cannot hold is a *repeat*
(`Column-repeat(5) > template(6)`). So the question is never "is this panel too
complex for a tab", it is **"does this panel contain a list?"** A five-card
photo gallery with a per-item verdict and links folds into five tabs
beautifully; the same five items as a repeat template does not fold at all and
has to stay its own Section one level shallower.

**Tab by the axis the reader will ACT on, not only by subject.** Tabs are for
alternative views of one subject — but "when can I do this" is a view. Twelve
open to-do items stacked as cards is a scroll nobody finishes; the same twelve
tiered into `Before you fly / At the car / In person / On the day` is four
short panels, and the first one is the only one that matters tonight. Group by
urgency, by owner, or by deadline whenever that is the question being asked.

**`Section.layout` does not reach through `Tabs`.** `cards` / `grid-2` reshape
a DIRECT Column or Row child only; a tabbed section's cards stack vertically.
That is the trade — fold *or* grid, per section. Fold when the panels are
alternatives (four metric groups), grid when they are peers you compare at a
glance (three goals side by side on a wide screen).

## Never let an app hold its own copy of a number

The most expensive bug in a long-lived dashboard is not a crash, it is a
**private copy of a figure that was corrected somewhere else**. An authored app
that hardcodes `"596 km / 8h31"` in its source file keeps rendering it,
confidently and forever, after the route it describes was rerouted and every
other artefact regenerated. Nothing errors. Nothing looks stale. The freshness
stamp is honest — the app really was composed a minute ago — and the number is
still wrong.

So: **derive every aggregate at refresh time from the one content model**, and
let the app own nothing but the layout. If two apps present the same subject,
they must call the SAME function for any verdict — a shared `wx_tone(window)`
rather than a copy in each source file — or one will eventually show amber
where the other shows green and neither will be provably wrong.

The corollary is a cheap, high-value component: a **`ChipRow` naming each data
lane and its age** pays for itself the first time it catches its own plumbing.
On the Italy rebuild the map-tile chip read *"re-mirroring"* forever because
the served-assets path in the freshness check was wrong by one directory level
— a bug invisible in the images themselves, which were fine. A lane indicator
that can catch its own wiring is worth five components.

## Budgets — what actually fits (raised, #623)

- **Components/sections: 40/5 by default — `ledger` and `briefing` get 80/8,
  `report` gets 80/10** (#630: goals, movers, four source grids, table,
  freshness and a method note is nine panels before you have added anything). A 16-day itinerary or a multi-chart dashboard does not fit 40/5;
  pick the archetype that matches and the caps follow. `MAX_DEPTH` stays 5 for
  everything (depth is a complexity smell, not expressiveness).
- **Sources: 40k total, 12k per source.** A 200-point ISO-stamped series
  (~9.9k) now arrives whole instead of being silently downsampled to ~120
  points — a full-resolution series is worth asking for. Three series plus
  records still fit.
- **Censor scan 48k; compose output 16k tokens** (~80 components). If an app
  still falls back, it is a contract error, not a size limit — read the lint
  message rather than trimming content on a guess.

## Name it short — the title becomes the chip

The `AppHeader` title is structurally mandatory (lint requires it as the first
top-level child) and it is now what **names the app's switcher chip**, so
several live apps are distinguishable instead of all reading `app`. Precedence:
AppHeader title → the record title → the curated kind label.

Write it as a **label, not a headline**: `Crypto Note`, not
`Crypto Note: Six Months, Forward View`. It truncates on a word boundary with
the full text on hover, so a long title is simply a worse chip. A dynamic title
(a `{path}` binding) resolves against the app's own data model and works fine —
the constraint is length, not form.

## Refine options are analytical angles, never commands

A refine option is **not a dispatched action**. Pressing it appends the
option's LABEL to the intent and re-composes against the **same stored
`data_sources`**. Nothing else happens. So the only satisfiable option is a
*different view of data the app already has*:

- Good: `Group by sender` · `Email volume by week` · `Compare attachment types` ·
  `Shared vs private items` · `Compare market share with last month`
- Undeliverable: `Export raw data` · `Download CSV` · `Email me a summary` ·
  `Schedule a weekly digest` · `Save to Drive` · `Share via link` ·
  `Add to calendar` · `Remind me tomorrow` · `Print this`
- Also undeliverable **as a refine option** — anything that ACTS. `Export raw
  data`, `Email me a summary`, `Retry failed nodes`, `Rebalance` are not
  refinements of a view. That does not mean they are impossible: since F092.2
  they belong in `agent_actions` (next section). Refine = re-render what is
  already here; agent_actions = ask the agent to go do something.

The classifier matches command *phrases* — the verb anchored at the start of
the label, or a construction with no noun reading (`email me`, `save to`,
`share via`, `schedule a`) — precisely so that legitimate labels containing
those words survive.

**It drops the option; it does not fail the app.** That is deliberate: no
lexical rule separates `Print volume by department` (a metric) from
`Schedule report distribution` (a command), and wiring a heuristic into
validation made every misjudgement cost the whole app via the repair loop. The
consequence for you: a bad label costs a button **silently**. Getting refine
labels right is the author's job, not the validator's.

## `agent_actions` — when a tap should DO something (F092.2)

`compose_surface(agent_actions=[{id, label, instruction}])` puts up to **4**
accent buttons in the footer. A tap spawns a **background agent turn** that
runs your stored `instruction` and then recomposes the app. This is the only
route from the surface back to you — the composed tree itself remains read-only
(the grammar still bans every input component, and any component carrying an
`action` is still rejected).

**Flag-gated** (`NOUS_A2UI_AGENT_ACTIONS_ENABLED`, land-dark by default). The
parameter is advertised in the tool schema **only when the flag is on** — if
you cannot see `agent_actions` in your own `compose_surface` schema, do not
send it. On this deployment it is ON.

Writing the instruction — it is the whole contract:

- **Self-contained.** It executes later, in a fresh context, **with nobody in
  the loop**. It cannot ask a question, and it cannot lean on anything from
  the turn that composed the app. Name files, ids, and addresses in full.
- **≤500 chars, label ≤40, slug ids** — validated at the tool layer. The cap is
  load-bearing: composition is the only moment you are deliberate about what a
  future unattended turn will do. Say the action, the object, and the finish.
- **End it by updating this app.** The turn is handed the `dedup_key`, the
  `data_sources`, and the `agent_actions` to re-declare; recomposing on that
  same `dedup_key` preserves `surface_id`, replaces the data model wholesale,
  and **is what clears the pending stamp**. An instruction that acts but never
  recomposes reads as a failure to the user.
- **Only declare what you can actually do** with your tools. A button whose
  turn cannot succeed is worse than no button.
- **Same trust split as `data_sources`:** actions enter only through YOUR tool
  call. The inner compose LLM never sees or authors them, and the server stamps
  only `{id, label}` onto the footer — the instruction never reaches the client.

What the user sees while it runs: buttons disable, the tapped one gets an
ellipsis, and a second tap is refused server-side (each tap is an LLM turn
against a 3-slot subtask pool). On failure, timeout, or a turn that finishes
without recomposing, the footer shows an honest error; and if the process dies
mid-flight, the client re-enables past the freshness window with "no update
arrived" rather than spinning forever. You do not have to build any of that —
but do not paper over it by writing an instruction that "succeeds" without
recomposing.

Actions survive `refine` and even a `fallback` render (a degraded app most
needs its "ask the agent" button — a tap can recompose it into a real one).
They are footer-level only; per-row actions and free-text input are v2.

**On a hand-authored (literal) app the instruction MUST NOT say
`compose_surface`.** The server's stock action prompt tells the turn to
recompose that way — correct for an LLM-composed app, destructive for an
authored tree, which it re-derives from a prose intent (this is how `app.refine`
destroyed the Italy Trip app on 2026-08-31). Point the instruction at your own
builder instead:

    run_python: import sys;sys.path.insert(0,'/tmp/nous-workspace/a2ui');
    import publish_literal as P;P.publish('<path>','<dedup_key>')
    NEVER call compose_surface - it would replace this authored tree.

The republish still replaces components + dataModel wholesale on the dedup_key,
so the pendingAction stamp clears exactly as the honesty contract requires.

Declare the actions in the app **module** — an `agent_actions()` beside
`build()`/`data_sources()` — not at the publish call. A republish then restores
the buttons from the file, which a 500-char instruction could never re-declare.
`publish_literal` picks it up and stamps `allowed_actions`, the footer
`{id,label}` and `app_spec.agent_actions` (instructions stay server-side).

Keep the instruction short by putting the real work in a **versioned script**
(`sync_only.sh`, `warm_wx.sh`) and naming it — the rationale lives in the
script's comments where 500 chars cannot reach.

## Honesty rules the renderer enforces (do not fight them)

- A gap in the data draws a **break in the line**, never a bridge. Do not
  fill missing readings with zero — drop the value and the renderer shows it.
  A `trendline` (rolling mean) breaks at the same gaps.
- Bar axes always include zero. A sparkline is not zero-based and therefore
  never filled — a fill would read as magnitude from zero.
- Every string you hand a card renders **verbatim**: the renderer never
  rounds, re-signs or adds a unit. Format the value, the delta and the
  caption in the script.
- An empty or failed source renders its **reason**. Let it.
- Anything you supplied yourself rather than sourced renders **amber**. If you
  find yourself hand-writing numbers into `dataModel`, that section will be
  marked model-supplied — fold it into the script instead if it should be live.
- **Children are references, not inline objects.** The grammar is
  reference-based: a `children` array holds component *ids* (strings). A
  non-string entry is now a hard lint error naming the count and the key —
  because before that, a Section whose body held only inline children linted
  CLEAN and rendered **EMPTY**, reporting `fallback: false, repairs: 0`. The
  pipeline called a silently truncated app a success. The check lives in
  `grammar.lint_micro_app`, so it holds on every `push_built` and every
  recomposition, not just on compose. (The `Repeat` template
  `{componentId, path}` is a dict, not a child list — unaffected.)

## Shape the source to the component, not the other way round

The bound components have **strict item contracts**, and a source whose records
use your own field names cannot be bound at all — the model has no reshape
operator, so it burns all three repair attempts and you get
`fallback: true`, a markdown degradation:

- `Timeline.items` → `[{at, label, detail?, flag?}]` (`at` is a preformatted
  string, `flag: true` highlights the row)
- `KeyValueTable.rows` → `[{key, value}]`, both strings
- `StatTile.label/value` → preformatted DynamicStrings; bind
  `/src/0/field` and keep the record count at 1
- `LineChart.path` → a multi-series object; `BarChart`/`Sparkline.path` → a
  series whose `t` is the category label

Emit those exact keys **from the script**. Formatting (units, `25° / 15°C`,
thousands separators) belongs in the producer too — the components render
preformatted strings and will not do it for you.

Two binding rules bite in practice: every non-empty source **must** be bound by
something, and a record-list source must be bound by a *template* path
(`/days`), not fixed indices — indices covering fewer than all records is
rejected as rendering a partial source as if complete.

## Validate locally before composing

`_validate` is a pure method — run the real thing against real source data and
iterate until clean, instead of paying an LLM round-trip per mistake:

```python
from nous.a2ui.compose import SurfaceComposer
c = SurfaceComposer.__new__(SurfaceComposer)      # no __init__ needed
errs = c._validate(candidate_app_dict, source_data)
```

It reports skeleton violations verbatim. The caps that are easy to trip:
**root must be a Column** with id `root` listing every top-level child;
**Sections** — the allowed count is archetype-specific: default 1–5, ledger/briefing
1–8, report 1–10 (merge extras into one Section wrapping a `Column` if you exceed
the cap for your archetype — don't switch archetype just to gain headroom);
**StatRow takes at most 4 children**; `Section.child` is **singular** (one
component id) while `Row`/`Column`/`List`/`StatRow` take `children`.

Get the structure clean locally, then write the intent to *describe that
structure* — naming each section, path and series key. The composer reproduces
it in one or two repairs instead of failing out.

## Images and links: possible, but not the obvious way

Both work in a micro-app. Neither works the way you would first reach for.

**Images** — the `Image` component is real and registered in the renderer.
Props: `url` (DynamicString, so bind it), `description` (alt text; omit and the
image is treated as decorative), `fit` (object-fit), `variant`
(`icon|avatar|smallFeature|mediumFeature|largeFeature|header`). `src` is passed
through unfiltered and there is no CSP, so any https URL renders.

**Links** — put them in `Text` as markdown, NOT on a `Button`.
- A `Button` CANNOT be a hyperlink here: `_validate` rejects ANY component
  carrying an `action` ("micro-app controls live in the AppFooter only").
  `openUrl` exists as a renderer function but no composed component may invoke it.
- The catalog's `Text` description claims markdown "without HTML, images, or
  links". **That description is wrong** — `markdown.ts` parses inline links
  deliberately (its comment: "The fixture wins — we parse links") and
  `MarkdownInline.svelte` emits a real `<a target="_blank" rel="noopener">`.
  Scheme is gated at PARSE time to `http/https/mailto`; a `javascript:` or
  `data:` href silently degrades to plain text, keeping the label.

So: build the link into the record's markdown field server-side and bind a
`Text` to it. Google Maps URLs API is stable and constructible from coords:
`https://www.google.com/maps/search/?api=1&query=LAT,LNG` and
`.../maps/dir/?api=1&origin=..&destination=..&travelmode=driving[&waypoints=..]`.

**Never hand-assemble an image URL.** Wikimedia thumb URLs embed an
unguessable md5 shard (`/5/52/`, not the `/6/6e/` you would infer). Resolve via
the Commons API, then GET each URL and assert `200` + `image/*` BEFORE composing.

## agent_script: exec the file, never `import` it

The script sandbox is a LONG-LIVED process, so `sys.modules` persists between
runs. `import my_sources` binds whatever was cached the first time — edit the
file, add a function, and every later run still gets the stale module
(`AttributeError: module has no attribute 'get'`). Module name collisions are
possible too.

Use a cache-proof loader instead:

    ns = {}
    exec(open('/tmp/nous-workspace/<app>/<uniquely_named>.py').read(), ns)
    result = ns['get']('bases')

Short stored script, canonical logic in one versioned file, always fresh.

## A failing source is SILENT — verify the persisted dataModel

`_script_failure` is deliberately shape-preserving: a broken `records` source
returns an **empty list**, a broken `series` source an **empty series dict**.
It never raises and never fails the compose. Therefore:

**`fallback: false, repairs: 0` does NOT mean the app has data.** An app can
compose perfectly and render entirely empty boxes.

Always close the loop by re-fetching the surface and asserting on lengths:

    curl -s "http://localhost:8000/a2ui/surfaces/<surface_id>"
    # then assert every records key len > 0 and every series key points > 0

Only then report it as done.

### The commonest cause of a wholly-empty app: script-slot contention

An `agent_script` source will not start when
`run_python_active_runs() >= max_concurrent - INTERACTIVE_RESERVE`
(live: `4 - 2 = 2`). It yields to interactive use and returns its empty value
with the reason `"N script slots already in use"`.

This bites hardest on `publish_literal`, which **runs inside a `run_python`
worker and blocks it for the whole resolve** — so it holds one of the two
slots itself, leaving exactly one for its own sources. One other concurrent
script and every source on the surface comes back empty, while the publish
still reports `lint [] / validate [] / published True`.

Observed twice on `app:italy-vacation` (2026-09-01): both blank republishes
were caused by *polling the surface with `run_python`* while the action turn
was republishing. **Never poll with `run_python` during a publish — poll with
`bash`/`curl`.**

Only a `series` failure carries its reason (records fail to a bare `[]`, reason
log-only), so one contended series identifies a contended *pass*. `publish_literal`
now retries the resolve 3× and, if contention persists, **refuses to publish**:
leaving yesterday's data with an honest upstream failure beats replacing a
populated app with a blank one. Check `report["empty_sources"]` and
`report["resolve_attempts"]`.

## Depth 5 counts from root — a Card can cost you the image

Repeat templates nest deeper than they look. `root > Section > Column(repeat)
> template Column > Image` is already depth 5. Wrapping the template in a
`Card` makes it 6 and fails `nesting depth 6 exceeds 5`. The `Section` is
already a visual container — drop the Card, keep the image.

Also: caps are per-archetype (see Budgets above). Prefer trimming redundant
sections to switching archetype just to buy headroom.

## When the composer falls back on a big app

The composer is an LLM transcribing your spec under `max_tokens=16000` and
`MAX_REPAIRS=2`; a truncated response reads as unparseable JSON and burns a
round. If it falls back, do NOT just retry — first prove your own tree is legal
by running the real `_validate` against live source data (see "Validate locally").
If your tree passes, simplify the nesting structure (fewer depth levels) rather
than cutting content — a deeply nested tree is harder for the LLM to reproduce
faithfully than a flatter one of equivalent component count.


## Suppress refine options on any hand-shaped app

`app.refine` is not a view toggle. The server takes the app's **stored intent**,
appends `\n\nRefine request: <label>`, and runs a **full LLM recompose**, then calls
`update_components` **unconditionally**.

If your intent is a literal build spec — explicit components, ids, binding paths, the
style that makes the first compose reliable — the appended narrowing instruction is
self-contradictory and overruns the composer budget. The recompose degrades to the
markdown fallback, and because there is **no `if composed.fallback` guard**, that dump
**replaces your working tree**:

- body becomes a markdown copy of your own build spec
- `AppHeader.title` becomes the **entire intent string** → the switcher chip turns to garbage
- `refine_options` is wiped to `[]` → the app cannot be refined back

It is irreversible from the UI. Only a fresh `compose_surface` on the same `dedup_key`
restores it. (Observed live, Italy Trip app, 2026-08-31.)

**Rule:** every hand-shaped app's intent ends with

```
Return "refine_options": [] — this app offers NO refine options. Do not invent any.
```

Verify it landed: the composed `AppFooter` must show `"refineOptions": []`.

`refresh` and `close` are safe and should stay. `app.refresh` re-runs the declared
fetchers only — no LLM, no tree replacement. Offer refine options **only** when the
intent is a short semantic description the composer can legitimately re-render from.


## Cost and limits

- Scripts share the `run_python` pool and yield to interactive use. **Always
  pass a `timeout=` to any network call.** The executor's deadline stops
  Python-level code but CANNOT interrupt a thread blocked in a C-level
  socket read, so a hung fetch holds its slot until the peer replies —
  and an unattended refresh repeats that every time.
- Result cap 256k chars — aggregate in the script, don't return raw rows.
- Compose-time budget is tight on a default `NOUS_TOOL_TIMEOUT`; the
  **refresh** path gets the full budget, so prefer computing on refresh.
- Use one `dedup_key` per recurring dashboard so it updates in place instead
  of stacking new cards.
