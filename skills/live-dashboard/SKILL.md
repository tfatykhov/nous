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
version: "1.1"
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
- **Declare `shape`** — `"series"` for anything charted, `"records"` for a
  table or list. On a later failure the source returns a value of that shape,
  so the app's existing bindings survive; get it wrong and the chart breaks.
- **`series_keys` is required for `shape="series"`.** `["v"]` for a
  Sparkline or BarChart; the LineChart's own keys otherwise. It pins the
  contract so a refresh cannot silently change single↔multi series and leave
  the chart empty.
- **Charts need a series, not rows.** `from nous.a2ui.sources import to_series`
  then `result = to_series(rows, "t", "v", unit="…")`. Multi-series:
  `to_series(rows, "t", "v", value_keys=["ok", "bad"])`.
- **It must be self-contained and deterministic.** It runs again, unattended,
  with nobody watching — not once while you are here. Compute from scratch;
  never rely on a variable you set in this turn.
- **Reads only.** `recall_deep` / `recall_recent` / `list_tasks` are in scope;
  `learn_fact` raises. A write would repeat on every refresh.
- **Verify before shipping.** Run the same code through `run_python` first and
  look at the numbers. A dashboard that renders a wrong trend confidently is
  worse than no dashboard.

## Choosing the shape of the app

| The question | Component |
|---|---|
| how has this moved | `LineChart` (multi-series) or `Sparkline` (one, inline) |
| how do these compare | `BarChart` — zero-based, so bars never lie |
| where does it stand right now | `StatTile` in a `StatRow` |
| what happened, in order | `Timeline` |
| the flat facts | `KeyValueTable` |
| the same subject two ways | `Tabs` (2-5 alternative views; only one renders at a time) |
| a Nous decision needing review | `DecisionCard` (+ `ConfidenceMeter`), fed from `unreviewed_decisions` |
| a running DAG's shape | `DagGraph`, fed from the `dag` source |
| long secondary detail | `Modal` behind a button, or an `accordion` section |

Layouts: `hero` for the one thing that matters, `grid-2`/`grid-3` for peers,
`rail` for a scrollable strip, `accordion` for long secondary detail collapsed
until tapped, `stack` for the rest. Pick a `theme` that fits the subject —
`signal` for alerting, `paper` for reading, `harbor`/`alpine-dusk` for ambient,
`nous-default` when unsure.

You never author pixels or colors: the composer picks components from a closed
catalog and the renderer owns geometry, scale, and palette. The full component
list with required/optional props is generated into the compose prompt from
the catalog itself, so you do not need to memorize it — state the intent and
the data, and let compose lay it out.

## Honesty rules the renderer enforces (do not fight them)

- A gap in the data draws a **break in the line**, never a bridge. Do not
  fill missing readings with zero — drop the value and the renderer shows it.
- Bar axes always include zero.
- An empty or failed source renders its **reason**. Let it.
- Anything you supplied yourself rather than sourced renders **amber**. If you
  find yourself hand-writing numbers into `dataModel`, that section will be
  marked model-supplied — fold it into the script instead if it should be live.

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
