# F091 — Memory Retrieval Telemetry

**Date:** 2026-08-19
**Status:** design, pending approval
**Decision:** `96334656` (Forge)

## Problem

We cannot see what memory retrieval did.

Today's entire retrieval telemetry is one `logger.info` line at `nous/api/tools.py:1079`. It reports eight scalars from a `PipelineStats` object (`nous/api/retrieval_pipeline.py:134`) that carries twenty-plus diagnostic fields — the rest are computed and discarded when the tool call returns. Nothing about retrieval is persisted anywhere.

Two consequences:

1. **Graph expansion is opaque.** We record `graph_expansion_used: bool` and `n_graph_expanded: int`. We do not record which seed produced which neighbor, over which edge relation, at what hop depth, or what the composed score was — even though `NeighborResult` already carries every one of those fields in memory.
2. **"Why isn't this fact in context?" is unanswerable.** A candidate can be dropped at any of ~20 sites across two code paths. None of them leave a record.

Prior work established the framing. Decision `ac40336b` found that "every miss can be traced to the specific gate that dropped it, since the candidate path is a small set of filters and slices" — but that tracing was done by hand, offline, against a frozen clone. This feature makes it a lookup.

## Scope: two paths, not one

Memory retrieval happens on two independent paths. Both are in scope.

| | Path | Trigger | Telemetry today |
|---|---|---|---|
| **A** | `run_recall_pipeline` (`nous/api/retrieval_pipeline.py`) | agent calls `recall_deep` | one INFO line; in-memory `PipelineStats` |
| **B** | `ContextEngine.build` (`nous/cognitive/context.py`) | **every turn**, automatically | counts bullets in rendered prose (`context_logger.py:165-198`) |

Path B is what actually fills the system prompt on every turn, and its only instrumentation is a regex over formatted text. A pipeline-only design would leave the dominant path dark.

Note Path B does **not** call `run_recall_pipeline`. It issues direct `_brain.query` / `_heart.search_facts` / `_heart.search_episodes` / `_heart.search_procedures` / `_brain.neighbors` calls with its own filter chain. The two paths share `Heart`/`Brain` primitives but no orchestration.

## Organizing principle: drop attribution

Listing what survived tells you nothing the rendered prompt doesn't. The feature is the **terminal disposition** of every candidate that entered.

```
disposition ∈
  rendered            -- reached the model
  sliced_off          -- fell outside a top-K / max-K cut
  below_floor         -- failed a similarity or score floor
  filter_dropped      -- removed by a named filter (staleness, diversity, relevance gap)
  budget_truncated    -- dropped by token-budget truncation
  f071_excluded       -- already in this turn's system prompt
  deduped             -- same id already present from another leg
  superseded          -- demoted/tagged by the recency resolver
  replaced_at_merge   -- exemplar strip (records WHICH of the two strips fired)
  type_excluded       -- its whole type was removed from the pool before search
```

Each disposition carries the `stage` that assigned it, so `sliced_off` at `heart_recall_limit` is distinguishable from `sliced_off` at `formatter_top_k`.

## Architecture

### A side-channel collector — never a result mutation

`PipelineResult` is a frozen dataclass consumed by `nous_eval/` and guarded by a byte-identical text snapshot test (`tests/fixtures/recall_deep_text_snapshot.txt`). It will not be modified.

Instead, a `RetrievalTrace` object is threaded alongside:

```
RetrievalTrace
├── header      query, agent_id, session_id, turn_number, trace_id,
│               path ("pipeline" | "context"), started_at, duration_ms
├── legs[]      name, attempted, n_returned, score_min, score_max, error
├── candidates[] id, type, entry_leg, entry_score, entry_rank,
│               mutations[{stage, score_before, score_after, reason}],
│               final_rank, disposition, disposition_stage, snippet
└── expansions[] seed_id, seed_type, seed_score, hop, edge_relation,
                edge_weight, extraction_method, neighbor_id,
                neighbor_type, composed_score, won_best_path
```

The pipeline **writes into** the collector. Nothing reads back out. Result shape, ordering and rendered text are therefore byte-identical by construction — with the flag off there is no branch that consumes trace state, so there is no behavior to change.

Two invariants:

- **`NullTrace` when disabled.** A no-op object exposing the same methods, not `if trace is not None:` guards at ~30 call sites. The guard-everywhere pattern is how an `AttributeError` reaches production.
- **`ContextVar` handoff**, mirroring `nous/api/runner.py:70` (`CURRENT_TURN_EXCLUDE_IDS`) exactly: set before the tool loop, reset in `finally`, per-asyncio-Task isolated. This is already the proven seam for getting turn state into `recall_deep`; a second mechanism would be strictly worse.

### Instrumentation points

**Path A — `run_recall_pipeline`:**

| Site | File:line | Records |
|---|---|---|
| F080 coherent-ranking pool filter | `retrieval_pipeline.py:687` | `type_excluded` for censor/procedure |
| Heart boundary + limit slice | `heart.py:1235` | entry scores; `sliced_off` at `heart_recall_limit` |
| Exemplar similarity floor | `retrieval_pipeline.py:976` | `below_floor` |
| Adjacency boost | `:427` | mutation `{adjacency_boost, before, after}` |
| Keyed r2 pre-slice filter | `:478-484` | `deduped` / `f071_excluded` |
| Score-banded insertion (keyed, keyed_r2, exemplar) | `:449`, `:497`, `:557` | `entry_rank` |
| Exemplar fetched-set strip | `:534` | `replaced_at_merge` (strip=fetched) |
| Exemplar stray source strip | `:549` | `replaced_at_merge` (strip=stray) |
| Recency resolver | `:567` (floor `:2220`) | `superseded` + demotion mutation |
| `rerank_by_score` sort | `:586` | `final_rank` |
| F071 exclusion | `:597` | `f071_excluded` |
| Formatter top-K | `tools.py` `_format_pipeline_text` | `sliced_off` at `formatter_top_k` |

**Path B — `ContextEngine`,** per section (decisions `:744`, facts `:789`, session-profile leg `:887`, procedures `:1111`, episodes `:1266`):

| Site | File:line | Records |
|---|---|---|
| Budget / `skip_types` gate | `context.py:740`, `:784` | leg `attempted=False` + reason |
| `_resolve_recency` | `:801` | `superseded` |
| `_apply_staleness_penalty` | — | mutation |
| `_enforce_diversity` | `:753` | `filter_dropped` (diversity) |
| `_apply_relevance_filter` | `:1397` | `sliced_off` at `max_k`; `filter_dropped` at gap cut |
| `_truncate_to_budget` | `:769` | `budget_truncated` |
| `_brain.neighbors` procedure expansion | `:1812` | expansion rows |

`_apply_relevance_filter` deserves emphasis: line `:1420` does `results[:max_k]` with `max_k` defaulting to **5**. That cap is entirely invisible today and is precisely the kind of cliff this feature exists to expose.

**Heart.recall internals — deliberately NOT instrumented in v1.** CE rerank (`heart.py:1143`) and MMR (`:1214`) already emit their own reorder deltas at `:1155` and `:1224`. Duplicating that inside the collector means threading trace state through a primitive shared by both paths and callers not yet audited, for marginal gain. v1 captures Heart's input/output boundary and the `[:limit]` slice; per-candidate CE/MMR rank movement is deferred.

### Graph expansion

`NeighborResult` already carries `seed_score`, `edge_weight`, `edge_relation`, `extraction_method` (set at `retrieval_pipeline.py:1051`, `:1063-1066`). The expansion trace is therefore **pure capture — zero additional queries**.

Captured at Stage 2 (`:1033`), Stage 2b Path A (`:1130`), Stage 4 one-hop, spreading activation (`hop > 1`), and Path B's procedure expansion (`context.py:1812`).

`won_best_path` records the outcome of the multi-seed replacement logic at `:1058-1067` and `:1158`, so a neighbor reached from several seeds shows which seed won and which lost.

Because a row is `(seed, relation, neighbor)`, the expansion trace **is an edge list** — which determines the UI shape below.

## Storage

New table `nous_system.retrieval_log`, mirroring the conventions in `sql/migrations/026_observability.sql`:

```sql
CREATE TABLE IF NOT EXISTS nous_system.retrieval_log (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    session_id      TEXT,
    turn_number     INTEGER,
    trace_id        VARCHAR(12),
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    path            TEXT NOT NULL,          -- 'pipeline' | 'context'
    query           TEXT,
    duration_ms     REAL,

    legs            JSONB NOT NULL DEFAULT '[]',
    n_candidates    INTEGER NOT NULL DEFAULT 0,
    n_rendered      INTEGER NOT NULL DEFAULT 0,
    n_expansions    INTEGER NOT NULL DEFAULT 0,
    disposition_counts JSONB NOT NULL DEFAULT '{}',

    candidates      JSONB,                  -- NULL when not sampled
    expansions      JSONB NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_retrieval_log_time  ON nous_system.retrieval_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_retrieval_log_agent ON nous_system.retrieval_log(agent_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_log_trace ON nous_system.retrieval_log(trace_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_log_sess  ON nous_system.retrieval_log(session_id, turn_number);
```

Migration `070_retrieval_telemetry.sql`. Per project convention, every table carries `agent_id`; no `;` inside `--` comments.

Design notes:

- **One row per retrieval, not per candidate.** A candidate-per-row table would be ~40× the write volume for no query benefit at this scale. Aggregates that need to be queryable are hoisted to header columns (`disposition_counts`, `n_*`); detail stays in JSONB.
- **`trace_id` is the join key.** It is already the causal-chain identifier rooted at turn events (`layer.py:1326`) and already on `context_log` (`026_observability.sql:22`), so a retrieval row attaches to the turn that caused it.
- **`candidates` is nullable** so an unsampled row still yields header + legs + expansions.

### Write path

Reuses the existing `ContextLogger` mechanism (`nous/main.py:549-612`): fire-and-forget `db_writer` scheduled through a `_schedule_bg` helper that retains strong references in a `_pending_tasks` set. That set exists at `context_logger.py:345` because asyncio holds only weak references to tasks and discarded ones were being GC'd mid-flight, silently dropping writes. Rolling our own would reintroduce that bug.

A `RetrievalLogger` sits beside `ContextLogger` in `nous/observability/`, with the same in-memory ring buffer (for live dashboard reads before the DB write lands) plus the same retention-sweep loop pattern (`main.py:619`).

### Retention and cost control

| Setting | Default | Purpose |
|---|---|---|
| `NOUS_RETRIEVAL_TELEMETRY_ENABLED` | `true` | master switch |
| `NOUS_RETRIEVAL_TELEMETRY_CANDIDATE_SAMPLE_RATE` | `0.1` | fraction of retrievals capturing the full candidate array |
| `NOUS_RETRIEVAL_TELEMETRY_SNIPPET_CHARS` | `200` | per-candidate content truncation |
| `NOUS_RETRIEVAL_TELEMETRY_RETENTION_DAYS` | `14` | sweep threshold |
| `NOUS_RETRIEVAL_TELEMETRY_MAX_CANDIDATES` | `300` | hard per-row cap; truncation logs WARN, never silent |

**On the flag default.** The master flag ships **on**; candidate capture ships **sampled at 0.1**. This splits the cost decision from the visibility decision. Header rows, leg summaries and expansions are cheap and universally useful, so they should be present the day the code deploys — a telemetry system that lands dark measures nothing. The expensive part (per-candidate arrays on every turn) is what gets sampled, and the sample rate is the knob to raise once the cost is measured rather than assumed.

**Content and PII.** Snippets are truncated hard and the length is configurable. Full fact bodies are never stored — that is both unbounded growth and a needless copy of user content into a diagnostic table.

## UI — new `retrieval` dashboard route

Added to `ROUTES` in `dashboard-app/src/lib/router.ts`, served by `GET /dashboard/retrieval` and `GET /dashboard/retrieval/{id}` registered in `nous/api/rest.py` next to the existing observability routes (`:2946`).

Three levels, because the useful questions sit at different altitudes:

1. **List** — recent retrievals: time, path, query, duration, legs fired, candidates in → rendered out. Scannable; the in→out ratio is the at-a-glance health signal.
2. **Detail** — the candidate table for one retrieval, **grouped by disposition**, sorted by entry score within group. The dropped groups get equal visual weight to `rendered`; they are the point of the view, not a collapsed footer. Each row shows entry leg, entry score, mutations, final rank.
3. **Expansion** — the seed→neighbor subgraph for that retrieval, edges labelled by relation, weighted by composed score, hop depth encoded by distance from seed. Reuses the viz primitives under `dashboard-app/src/lib/viz/` already used by `GraphView.svelte`.

A summary strip at the top of the list view carries aggregate `disposition_counts` over the visible window — so a systemic problem (say, half of all candidates dying at `max_k`) is visible without opening a single detail view.

## Testing

- **Snapshot invariance.** The existing `recall_deep` byte-identical snapshot test must pass unchanged with the flag both on and off. This is the primary regression gate.
- **Null-object contract.** `NullTrace` accepts every method `RetrievalTrace` exposes; a test asserts the two share a method set, so a new capture call can't be added to one without the other.
- **Disposition completeness.** For a synthetic pipeline run, every candidate that entered has exactly one terminal disposition, and `n_rendered + Σ(dropped) == n_candidates`.
- **Per-drop-site tests.** One test per site in the two tables above, asserting the expected disposition and stage.
- **Expansion fidelity.** A seeded graph fixture yields expansion rows whose `seed_id`/`edge_relation`/`composed_score` match the `NeighborResult` values the pipeline saw.
- **Fire-and-forget correctness.** The write task is retained in `_pending_tasks` and survives to completion (the `context_logger.py:345` bug class).
- **Sampling.** At `sample_rate=0`, `candidates` is NULL but header/legs/expansions still persist.

Tests use real Postgres per project convention. Note CI is the gate: the local full suite is not green on `main` and cannot catch dialect bugs.

## Risks

- **Path B hot-path cost.** `ContextEngine.build` runs every turn. The collector is in-memory dict appends with the DB write deferred, but candidate capture is not free. Sampling at 0.1 is the mitigation; the rate should be raised only after measurement.
- **Concurrent-turn safety.** The collector is mutable state carried on a `ContextVar`. `ContextVar` gives per-asyncio-Task isolation, so concurrent turns each get their own — but the trace object must be finalized (serialized) before the fire-and-forget write is scheduled, never handed to the writer by reference while the pipeline can still mutate it.
- **Drop-site drift.** New filters added later won't record a disposition and will silently show up as accounting gaps. The completeness test above turns that into a test failure rather than a quiet hole.
- **`Heart.recall` boundary-only capture** means CE/MMR rank movement stays invisible in v1. Accepted; both already log their own reorder deltas.

## v1 / deferred

**v1:** collector + `NullTrace`; both paths instrumented at the sites tabled above; migration 070; `RetrievalLogger` + retention sweep; two REST endpoints; the three-level dashboard route; the test set above.

**Deferred:** per-candidate CE/MMR rank movement inside `Heart.recall`; cross-retrieval trend analytics (disposition rates over time); replay ("re-run this query with flag X off and diff"); export to the `nous_eval` harness; alerting on disposition-rate drift.

## Implementation notes (2026-08-19)

Shipped in `2de1141` + `e5f6a5e`. Two corrections to the design above, both
found by running live probes against the real pipeline rather than trusting
the instrumentation by inspection:

**Stage 4 was missing from the instrumentation table.** The table listed
Stage 2 and Stage 2b but not Stage 4's 1-hop decision expansion or spreading
activation — which between them produce most graph expansion in practice. The
probe caught it immediately: `brain_graph` reported attempted while the trace
showed zero edges on every query. Both are now captured. 1-hop edges are
recorded *before* the dedup guards so an edge that loses best-path is still
shown as traversed. Spreading records `hop=2` with `seed_type="multi"`,
because the CTE returns an activation rather than a `(seed, edge)` pair —
attributing it to a single seed would be a fabrication.

**A dropped candidate can legitimately reach the prompt.** `_reinsert_pinned`
(F083 pinning) exists specifically to rescue facts past diversity/dedup/
relevance demotion. With `NOUS_FACT_PIN_TOP_K=5`, a fact dropped at
`diversity` came back and rendered, while the trace still counted it as
dropped — `n_rendered=8` against 10 ids actually in the prompt. `finalize()`
is now authoritative: presence in the final result set overrides an earlier
drop, and the overridden gate is preserved on a new `restored_from` field
rather than discarded. Both facts matter — the drop really happened, and the
item really reached the model. The design's completeness invariant
(`n_rendered + Σ(dropped) == n_candidates`) was what surfaced this.

Also as planned: `Heart.recall` internals stayed out of v1 (boundary + slice
only), and the flag split shipped as designed — master ON, candidate capture
sampled at 0.1.

Three probes ship as executable verification under `scripts/diag/`:
`f091_e2e_check.py` (write→Postgres→dashboard-query round trip),
`f091_pipeline_probe.py` (results byte-identical traced vs untraced),
`f091_context_probe.py` (system prompt byte-identical; `n_rendered`
reconciles exactly with `recalled_ids`).

### Adviser input

Fable was dispatched as adviser on this design. It had not returned by the
time implementation completed, so the design was validated by direct code
reading plus the three probes instead. Any correction it raises later should
be checked against the shipped instrumentation tables above.
