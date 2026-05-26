# F071 — Cross-context dedup (recall_deep ↔ system prompt)

**Status:** 📝 Draft v3 (2026-05-26) — scope-cut after 3-agent spec review + verification review
**Proposed by:** Tim
**Depends on:** F067 (Episode Chunks), F051 (Eval Harness)
**Related:** F036 (Prompt Cache), F022 (Graph-Augmented Recall), F072 (deferred — chunks in cognitive context)
**Forge decision:** `911bb083`

---

## Problem

`ContextEngine.build` (`nous/cognitive/context.py`) populates the system prompt with `## Related Decisions`, `## Relevant Facts`, `## Recent Conversations`, `## Working Memory` every turn from Brain and Heart. These items are recorded in `BuildResult.recalled_ids` and flow into `TurnContext`.

When the agent later calls `recall_deep`, `run_recall_pipeline` (`nous/api/retrieval_pipeline.py`) runs the same searches with broader scope (graph expansion, RRF fusion, MMR, CE rerank) and **returns the same facts/episodes/decisions/procedures the system prompt already loaded** — the LLM pays for them twice (input tokens) and sees noise in the tool result that doesn't help reasoning.

Today's duplication tax is small in absolute terms because facts are ~30–80 tokens each. F067 chunks (~600 chars) and F069 document chunks will amplify this whenever F072 lands. Cleaning the wire **first** unblocks F072 without compounding the cost.

### What this is not

- **Not** the original F071 v1 design (chunks in cognitive context + adjacent stitching). Those are deferred to F072 pending (a) a prod-shape eval harness, (b) recall_deep call-rate telemetry, (c) F069 semantic-chunking outcome.
- **Not** a rewrite of `run_recall_pipeline` or `recall_deep`. One new optional parameter, one post-rerank filter.
- **Not** behavior change in *what* `recall_deep` searches. Only filtering of *what* it returns after rerank.

---

## Goals

1. `recall_deep` returns no item that the LLM already has in the current turn's system prompt.
2. Zero behavior change when the flag is off (byte-identical output).
3. Async-safe per-turn scope — concurrent turns don't bleed exclusion sets.
4. Type-keyed exclusion — same UUID across different types (defensive, won't happen in practice) doesn't cross-filter.

## Non-goals

- Pre-loading any new content into the system prompt (F072).
- Adjacent-chunk stitching (F072).
- Showing the LLM "(already in context)" markers — pure drop in v1; tagging is a v1.1 option.
- Dedup of recall_recent or other tools — `recall_deep` only.

---

## Design

### Component map

```
┌─────────────────────────────────────────────────────────────────────┐
│  Pre-turn (cognitive/context.py)                                    │
│    Build sections → collect recalled_ids dict                       │
│  Pre-turn (cognitive/layer.py)                                      │
│    BuildResult → TurnContext (already has 4 recalled_*_ids fields)  │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Turn start (api/runner.py::run_turn)                               │
│    CURRENT_TURN_EXCLUDE_IDS.set({type: set(ids), ...})  ← NEW       │
│    try: ... tool loop ...                                           │
│    finally: token.reset()  ← NEW (defensive)                        │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  recall_deep closure (api/tools.py)                                 │
│    exclude_ids = CURRENT_TURN_EXCLUDE_IDS.get() if flag else None   │
│    run_recall_pipeline(..., exclude_ids=exclude_ids)  ← NEW arg     │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  run_recall_pipeline (api/retrieval_pipeline.py)                    │
│    ... rank → rerank → MMR ...                                      │
│    if exclude_ids:                                                  │
│        results = [r for r in results                                │
│                   if str(r.id) not in                               │
│                   exclude_ids.get(r.type, set())]                   │
└─────────────────────────────────────────────────────────────────────┘
```

### 1. `contextvars.ContextVar` for turn-scoped exclusion set

Async-safe per-task. Both `run_turn` (non-streaming) and `stream_chat` (streaming) set it before their tool loops, reset in `finally`. Concurrent turns each get an isolated copy.

```python
# nous/api/runner.py (top-level)
from contextvars import ContextVar
CURRENT_TURN_EXCLUDE_IDS: ContextVar[dict[str, set[str]] | None] = ContextVar(
    "CURRENT_TURN_EXCLUDE_IDS", default=None,
)
```

Both paths use the same shape — but they have **distinct insertion points** because each holds its own try-block today:

**Non-streaming (`run_turn`, ~line 379):** wrap a new outer `try/finally` around the existing inner `try/except/else` that handles `_caught_exc`. The inner block stays unchanged; the new outer block only manages contextvar lifecycle. This guarantees `.reset(token)` fires even when `_caught_exc` is re-raised at the bottom of the inner branch.

```python
# nous/api/runner.py::run_turn — placement: after pre_turn (~line 351),
# wrap from here to just before the existing `return` (~line 494).
exclude_ids = _build_exclude_ids(self._settings, turn_context)
token = CURRENT_TURN_EXCLUDE_IDS.set(exclude_ids)
try:
    # ... existing code from line 353 onward (user message append,
    # ledger setup, the inner try/except/else, post_turn, etc.) ...
    return response_text, turn_context, usage
finally:
    CURRENT_TURN_EXCLUDE_IDS.reset(token)
```

**Streaming (`stream_chat`, ~line 887):** same pattern. The existing `try:` block at ~line 993 stays; a new outer `try/finally` wraps from after `pre_turn` (~line 894) through the end of the generator body.

The shared helper builds the dict so both paths stay in sync:

```python
def _build_exclude_ids(
    settings: Settings, turn_context: TurnContext | None,
) -> dict[str, set[str]] | None:
    if not settings.recall_exclude_context_ids or turn_context is None:
        return None
    return {
        "fact":      set(turn_context.recalled_fact_ids or []),
        "decision":  set(turn_context.recalled_decision_ids or []),
        "episode":   set(turn_context.recalled_episode_ids or []),
        "procedure": set(turn_context.recalled_procedure_ids or []),
    }
```

### 2. `recall_deep` reads the contextvar

**Import discipline:** module-level `from nous.api.runner import CURRENT_TURN_EXCLUDE_IDS` would create a circular import (runner imports tools indirectly). Mirror the existing `from nous.api.runner import FRAME_TOOLS` pattern at `tools.py:121,1646` — import lazily **inside the closure body**.

```python
# nous/api/tools.py::recall_deep closure
async def recall_deep(query, search=..., k=..., _session_id=None):
    ...
    from nous.api.runner import CURRENT_TURN_EXCLUDE_IDS  # lazy: avoid cycle
    exclude_ids = CURRENT_TURN_EXCLUDE_IDS.get()
    pipeline_results = await run_recall_pipeline(
        ..., exclude_ids=exclude_ids,
    )
```

No new dispatcher injection plumbing needed — contextvars are accessible anywhere the closure runs in the same task.

### 3. `run_recall_pipeline` post-rerank filter

**`PipelineStats` is `@dataclass(frozen=True)` at `retrieval_pipeline.py:72`** — attribute assignment after construction raises `FrozenInstanceError`. Compute the count in a local variable **before** constructing `PipelineStats`, then pass it as a constructor argument.

**Insertion point: between `rerank_by_score` (`retrieval_pipeline.py:265`) and the `PipelineStats(...)` constructor (`retrieval_pipeline.py:267`).**

```python
# nous/api/retrieval_pipeline.py::run_recall_pipeline signature
async def run_recall_pipeline(
    ...,
    exclude_ids: dict[str, set[str]] | None = None,
) -> tuple[list[PipelineResult], PipelineStats]:
    ...
    if rerank_by_score:
        results.sort(key=lambda r: r.score or 0.0, reverse=True)

    # F071: exclude items already loaded into the system prompt.
    # Filter applied AFTER all scoring (rerank, MMR, CE) so the LLM sees
    # the next-best alternatives — not whatever happened to rank highest
    # below the excluded items in the raw search.
    excluded_in_context = 0
    if exclude_ids:
        before = len(results)
        results = [
            r for r in results
            if str(r.id) not in exclude_ids.get(r.type, set())
        ]
        excluded_in_context = before - len(results)

    stats = PipelineStats(
        ...,  # existing fields unchanged
        excluded_in_context=excluded_in_context,  # new
    )
    return results, stats
```

### 4. Telemetry

Add `excluded_in_context: int = 0` to `PipelineStats`. Surface in `_session_id` end-of-turn telemetry the same way other stats are tracked. Allows post-rollout measurement of the duplication tax we're closing.

---

## Env knobs

| Variable | Default | Description |
|---|---|---|
| `NOUS_RECALL_EXCLUDE_CONTEXT_IDS` | `false` | Master switch — `recall_deep` filters out items already in the current turn's system prompt |

One flag. Defaults off. Operator opt-in via `.env`.

---

## Rollout

### Phase 1 — Land dark (this PR)

Ship flag off, code present + tested. **Note:** with the flag off, `_build_exclude_ids` returns `None` and the pipeline short-circuits — `excluded_in_context` is always 0. Phase 1 produces no measurement signal; it only confirms the wire is correct via the byte-identical snapshot test. The pre-flip measurement happens in Phase 2.

### Phase 2 — Flip in dev (1 week observation)

Set `NOUS_RECALL_EXCLUDE_CONTEXT_IDS=true` on the dev `nous-default` agent. Watch:

- `excluded_in_context` per-turn distribution
- `recall_deep` tool-call success / token-output deltas
- User-visible chat quality (manual review of dev sessions)

### Phase 3 — Default-on if dev clean

Flip default in `nous/config.py` after a week of clean dev behavior. Operators keep override via env.

### Eval gate

Two harness runs on `nous-eval-corpus` (F051):

1. **No-overlap regression test** — for queries where the system prompt and recall_deep would not have overlapped anyway, output is byte-identical. This is a unit + integration assertion, not a metric.
2. **Overlap measurement** — for queries where overlap exists, measure (a) MRR@10 of the de-duplicated `recall_deep` output (must non-regress vs current; ideally improve because previously-suppressed lower-ranked items now surface), (b) token reduction in tool result body.

---

## Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| **`recall_deep` returns empty when overlap is total** | Low | Acceptable — the LLM has the same content in the system prompt. The tool result body becomes a short "no additional results" message. Tested. |
| **contextvar leaks across turns** in long-running event-bus handlers | Low | `try/finally` with `.reset(token)`. Per-task isolation is the contextvar contract. |
| **F022 graph-expanded results filtered** when the seed is in context | Medium | **Keep them.** Graph neighbors are *new* signal even if the seed was in context. The filter only drops items whose `id` is in the exclusion set; graph expansion produces neighbors with different IDs. |
| **Stats counter race** under concurrent calls | Low | `excluded_in_context` is per-PipelineStats (per-call), not global. |
| **Snapshot test brittleness** — `byte-identical` claim breaks with future pipeline changes | Medium | The byte-identical test runs only with `exclude_ids=None` (no flag). Future pipeline changes that affect this output would be caught by other snapshots anyway. |
| **Type-key mismatch** — `PipelineResult.type` is `Literal["episode", "fact", "procedure", "censor", "decision", "chunk"]`; exclusion-set keys are `"fact" / "decision" / "episode" / "procedure"`. Missing keys (`censor`, `chunk`) silently no-op | Low (intended) | Document explicitly: v1 dedups the 4 types currently tracked in `TurnContext`. `chunk` and `censor` are unaffected by design until F072. |

---

## Test plan

### Unit

- `run_recall_pipeline(exclude_ids=None)` — output identical to pre-F071 (snapshot)
- `run_recall_pipeline(exclude_ids={})` — same as None (defensive)
- `run_recall_pipeline(exclude_ids={"fact": {"<uuid>"}})` — drops that fact, retains others
- Type-keying: `exclude_ids={"fact": {"<uuid>"}}` does **not** drop an episode whose id happens to equal that uuid
- Empty result after filter — returns empty list cleanly, `excluded_in_context` matches
- Multiple types in exclude_ids — independent filtering, counts sum correctly

### Integration

- `AgentRunner.run_turn` with flag on:
  - Seeds 5 facts in Heart, builds context (loads 3), calls `recall_deep` (would return 5)
  - Asserts: recall_deep result excludes the 3, returns the other 2
  - `excluded_in_context == 3`
- `AgentRunner.run_turn` with flag off:
  - Same seeds; recall_deep returns all 5
  - `excluded_in_context == 0`
- Concurrent turns (two `run_turn` calls on the same runner, different session_ids) — contextvar isolation holds; neither turn's exclusion bleeds into the other.

### Eval (post-merge, manual)

- F051 harness paired A/B (flag on/off) — confirm token reduction in tool result bodies; no MRR@10 regression.

---

## File touch list

Authoritative; mirrored in the implementation plan.

| File | Change | LOC est |
|---|---|---|
| `nous/config.py` | Add `recall_exclude_context_ids: bool = False` | +3 |
| `nous/api/runner.py` | Define `CURRENT_TURN_EXCLUDE_IDS` ContextVar; set/reset in `run_turn` | +20 |
| `nous/api/tools.py` | `recall_deep` reads contextvar, passes to pipeline | +5 |
| `nous/api/retrieval_pipeline.py` | New `exclude_ids` parameter; post-rerank filter; `PipelineStats.excluded_in_context` | +15 |
| `tests/test_retrieval_pipeline_exclusion.py` | 6 unit tests | +120 |
| `tests/test_runner_dedup_integration.py` | 3 integration tests | +90 |
| `tests/test_runner_dedup_concurrency.py` | 1 concurrency test | +60 |
| `CLAUDE.md` | Env var table entry | +1 |
| `docs/features/INDEX.md` | F071 status row | +1 |

Estimated total: ~315 LOC, of which 270 is tests.

---

## What gets deferred to F072

For posterity — when revisiting:

1. **`## Memory Chunks` section in cognitive context** — pre-turn chunk-vector-search, populated into system prompt
2. **Adjacent-chunk stitching** — `(episode_id, chunk_index ± N)` window, `source_kind`-aware (`dialogue`: ±1, `document`: ±2)
3. **Type-keyed exclusion for `chunk`** — once chunks land in the system prompt, the exclusion-set wired here gains a 5th key naturally

F072 prereqs:
- **Prod-shape eval harness** — current evals (LME per-question isolation, F051 synthetic) overstate chunk benefit by ~17pp vs shared-corpus. F072 must not flip on defaults until a benchmark that mirrors prod retrieval shape green-lights it.
- **`recall_deep` call-rate telemetry** — empirically measure how often each frame calls recall_deep before claiming "frames don't call recall_deep enough."
- **F069 semantic chunking outcome** — if document chunks get a proper semantic chunker, the stitching workaround may be unnecessary.

The F071 wire-up here (contextvar, exclusion-set type-keyed dict) is **forward-compatible** — F072 plugs the `chunk` key in without touching the wire.

---

## Open questions

1. **Should `excluded_in_context` count also include items filtered by MMR / CE before reaching the dedup step?** No — the filter only sees what the rest of the pipeline already chose. Keeping it scoped to "dedup-attributed" drops keeps the metric clean.
2. **Should the filter apply pre- or post-MMR?** Post. Confirmed in design above. Rationale: we want the LLM to see what the pipeline's full ranking would have surfaced after exclusion, not a different mix.

---

## Acceptance

- All unit + integration tests green
- Byte-identical snapshot regression test passes (flag off / `exclude_ids=None`)
- One concurrent-turn isolation test green
- CLAUDE.md env table updated
- INDEX.md status row added
- F072 stub spec exists capturing deferred work
