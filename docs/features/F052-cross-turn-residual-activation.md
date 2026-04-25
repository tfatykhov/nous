# F052: Cross-Turn Residual Activation

**Status:** 📝 Draft (v4 — address Codex P1 #3 session_id injection)
**Proposed by:** Tim (human-brain association framing — "train of thought between turns")
**Date:** 2026-04-21
**Depends on:** F002 (Heart — shipped), F022 (Graph-Augmented Recall & Spreading Activation — shipped), F025 (RRF hybrid search — shipped), F042 (CE reranking — shipped)
**Blocks:** None
**Related:** F027 (access tracking / Hebbian reinforcement — specced, not built), F037 (Utility-Boosted Retrieval — Draft), F041 (SNN Sleep Densification — shipped)

---

## Changelog

- **v4 (2026-04-24):** Addressed Codex P1 #3 (missed in v3): `recall_deep` has no
  `session_id` in its closure. `ToolDispatcher.dispatch` only injects session identity for
  `spawn_task`, `cache_retrieve`, and `run_python` — not `recall_deep`. Fix:
  1. **`ToolDispatcher.dispatch`** — add `recall_deep` injection (mirrors the `run_python` pattern).
  2. **`recall_deep` signature** — add `_session_id: str | None = None` parameter.
  3. Guard added: if `_session_id is None`, residual activation is silently skipped (fail-open;
     sessions without an ID get cold recall, not an error).
  4. All `session_id` bare references in the diff blocks replaced with `_session_id`.
- **v3 (2026-04-21):** Folded 2 Codex review findings on PR #343:
  1. **WorkingMemory item schema** — actual `WorkingMemoryItem` contract (`nous/heart/schemas.py:289`) is `{type, ref_id: UUID, summary, relevance, loaded_at}` — NOT `{id, activation_count, ...}` as v2 wrote. Spec now shows correct base schema; residual adds are two optional JSONB-only fields.
  2. **Session plumbing for post-fusion boost** — v2 showed boost applied in `Heart._recall` but `Heart.recall()` has no `agent_id`/`session_id` parameters (`nous/heart/heart.py:736`). v3 plumbs `residual_activations: dict[UUID, float] | None = None` through `Heart.recall`/`_recall` so the boost can apply before MMR. The tool layer computes activations and passes them in. NOTE: `agent_id` is available via `brain.agent_id` inside the closure; `session_id` requires explicit injection via `ToolDispatcher.dispatch` — addressed in v4 (P1 #3).
- **v2 (2026-04-21):** Folded 5 fixes from multi-agent review:
  1. Reuse existing `ConversationState.turn_count` — drop the proposed `working_memory.turn_counter` migration
  2. Mandate isolated DB session in `record_surfaced` — prevents AsyncSession corruption from fire-and-forget task
  3. Added concrete call-site diff showing `extra_seeds` flow into the `spreading_activation_search` invocation in `nous/api/tools.py`
  4. Made decay configurable: `residual_decay_mode: Literal["geometric", "power_law"]`, added power-law as 5th ablation
  5. Added Topic Chain Success Rate to eval harness; filed Phase 2 lateral-inhibition follow-up (SYNAPSE-style)
- **v1 (2026-04-21):** Initial draft.

---

## Problem

Every call to `Heart.recall` today is **memoryless with respect to recent recalls**. The pipeline is:

1. `hybrid_search` → vector + keyword → RRF fusion
2. F042 cross-encoder rerank (optional)
3. F030 MMR diversification
4. F022 graph spreading activation (seeded from the current query's top hits)

Nothing in this chain reads **what was recalled in the previous turn(s)**. Each recall starts from a cold activation state. That contradicts how biological associative memory works — a memory surfaced a moment ago is primed, and related memories are more retrievable for seconds to minutes after.

Verified against repo at HEAD `db635d4`:

- `grep -i "residual|cross.turn|carryover|persistent.activation|priming"` → zero hits in source
- `nous/brain/spreading_activation.py::spreading_activation_search` (line 56) seeds **only** from the caller's `seed_nodes` argument, which is built from the current query's vector hits at `nous/api/tools.py:420-423`. No prior-turn state enters.
- `Fact.last_recalled_at` and `Fact.recall_count` exist on the model (`nous/storage/models.py:405-406`), are **written** by `facts.py:152`, and are **only read** by the sleep-handler staleness scan. Never read at query time.
- `WorkingMemoryItem` (`nous/heart/schemas.py:289`) has the contract `{type: MemoryType, ref_id: UUID, summary: str, relevance: float, loaded_at: datetime}`. `WorkingMemoryManager` persists these as JSONB in `WorkingMemory.items`. `grep "working_memory|loaded_items" nous/brain/` returns zero hits — working memory is a display/context buffer, not a retrieval signal.
- `ConversationState.turn_count` (`nous/storage/models.py:634`) already persists per-session turn count and is already incremented by the conversation pipeline. **No new turn counter needed** (v2 correction).

### Symptom examples

- Tim asks in turn N: *"what's F022's spreading activation doing at phase 4?"* → recall surfaces the F022 spec fact. In turn N+1: *"and what gates its density?"* → recall has to rediscover the F022 spec from scratch via lexical/vector match, and may miss if the phrasing drifts.
- A debug session pulls a cluster of related facts about DAG completion checks. Three turns later, a follow-up question that rephrases the topic ("why doesn't my check-type node run its shell command?") fails to re-surface the same cluster because nothing remembers the cluster was just hot.

The "current frame" doesn't bias the next recall. There is no *train of thought* — each recall is an independent query.

---

## Goals

- Memories surfaced in recent turns receive a **decaying score boost** in subsequent recalls within the same session.
- Recently-surfaced nodes are **seeded into F022 spreading activation** alongside the current query's vector hits, so their 1-hop neighbors also get pulled in.
- Boost is **bounded** (cannot dominate fresh relevance) and **decays to zero** within a small number of turns.
- **Zero new tables, zero migrations.** Reuse `ConversationState.turn_count` (already populated) and `WorkingMemory.items` JSONB (extend in-doc with two new keys).
- Fail open: any error in residual-activation scoring returns the uncorrected ranking. Pipeline runs unchanged.
- Land behind a flag, disabled by default until the retrieval eval harness says it helps.

## Non-goals

- **No global persistence** beyond session. Residual activation is session-scoped. Cross-session priming is F037 territory (utility over time), not this feature.
- **No edge reinforcement.** That's F027 / Hebbian — a separate feature. This spec only modulates **node** activation.
- **No change to `hybrid_search` signature.** Residual activation is applied as seed injection at the F022 call-site and as a post-fusion re-score step.
- **No change to graph schema or edge types.** F022 CTE is reused as-is; we just feed it extra seeds.
- **No LLM calls.** Pure arithmetic on existing signals.
- **No learning of decay rates.** Phase 2 concern.
- **No lateral inhibition.** Filed as Phase 2 follow-up (see Open Questions Q4) based on SYNAPSE findings for monotopic sessions.

---

## Design

### 1. Conceptual model

Each memory item has an **activation level** `a(m, t)` at turn `t`:

**Geometric decay (default):**
```
a(m, t) = Σ over prior turns s ≤ t : surfaced(m, s) * decay^(t - s)
```

**Power-law decay (ACT-R style, configurable):**
```
a(m, t) = Σ over prior turns s ≤ t : surfaced(m, s) * (t - s + 1)^(-α)
```

Where:
- `surfaced(m, s)` = the rank-normalized score of `m` at turn `s` (0 if not surfaced), capped at 1.0
- `decay` (geometric) = configurable, default **0.5 per turn**
- `α` (power-law) = configurable, default **0.5** (empirical ACT-R activation decay exponent)
- `a(m, t)` truncated to 0 when below `activation_floor` (default 0.05) — prunes the long tail
- Only the **top K activated items** are carried forward (default K=20), bounding memory cost per session

**Why two modes:** geometric (0.5/turn) kills a single-turn impression at turn 4–5. Power-law (α=0.5) sustains it out to turn 8+. ACT-R's fit to human recency data uses power-law; review flagged geometric as ~2× too fast for longer threads. Both ship; ablation picks the default for the flag flip.

Two places in the recall pipeline consume `a(m, t)`:

**A. Seed injection into F022 spreading activation.**
Top-N residually-activated nodes (default N=5) are added to `seed_nodes` alongside the current query's vector hits, each with seed score = `a(m, t) * residual_seed_weight` (default 0.3). Their 1-hop neighbors then get pulled by the existing CTE — no CTE changes.

**B. Post-fusion score boost.**
After RRF fusion (and graph merge) but before MMR, each candidate's score gets `+ a(m, t) * residual_boost_weight` (default 0.15). Bounded so a cold, highly-relevant hit still beats a hot-but-irrelevant one. Boost is applied additively in RRF-normalized score space.

### 2. State — reuse `ConversationState.turn_count` + extend `WorkingMemory.items`

**Turn counter:** `ConversationState.turn_count` (`heart.conversation_state`, `nous/storage/models.py:634`) is already an `Integer NOT NULL DEFAULT 0`, already incremented per user-facing turn by the conversation pipeline. **Reuse as-is. No migration.**

**Per-item activation state:** `WorkingMemory.items` is a JSONB list. The Pydantic contract `WorkingMemoryItem` (`nous/heart/schemas.py:289`) is:
```json
{"type": "fact|episode|decision|procedure",
 "ref_id": "UUID",
 "summary": "str",
 "relevance": 0.0-1.0,
 "loaded_at": "ISO8601"}
```

Extend **in-JSONB, no migration, no schema change to `WorkingMemoryItem`**. Two new optional keys on the JSONB payload only (not on the Pydantic model in v1):
```json
{..., "activation": 0.0-1.0, "last_surfaced_turn": int}
```

`ResidualActivator` reads/writes these extra keys directly against the JSONB via `WorkingMemoryManager`'s raw item access path (see `working_memory.py:427` where `items = [WorkingMemoryItem(**i) for i in raw_items]`). Residual activation reads `raw_items` pre-parse to access the extra fields, leaving the typed model untouched.

**Important (v3 fix #1):** the spec's v2 example used `id` instead of `ref_id`; any implementation following v2 literally would have written payloads that fail `WorkingMemoryItem(**i)` parsing. Do not drop or rename `ref_id`. `activation` and `last_surfaced_turn` are **additive only**.

### 3. Module — `nous/heart/residual_activation.py` (new, ~150 LOC)

```python
class ResidualActivator:
    """Compute cross-turn residual activation and apply it to recall scoring.

    Stateless — reads/writes WorkingMemory.items and ConversationState.turn_count
    via an injected db factory. Fails open."""

    def __init__(
        self,
        settings: Settings,
        wm: WorkingMemoryManager,
        db: DatabaseFactory,
    ) -> None: ...

    async def current_turn(self, agent_id: str, session_id: str) -> int:
        """Read ConversationState.turn_count. Returns 0 if no row."""

    async def compute_activations(
        self, agent_id: str, session_id: str, current_turn: int
    ) -> dict[UUID, float]:
        """Return {node_id: activation} for items still above the floor.

        Applies geometric or power-law decay based on
        settings.residual_decay_mode."""

    def seed_for_spreading(
        self, activations: dict[UUID, float]
    ) -> list[tuple[UUID, str, float]]:
        """Return top-N seeds for F022 spreading activation CTE.

        Pure function on the activations dict + a local WM snapshot
        for node_type lookup."""

    def boost_scores(
        self,
        candidates: list[ScoredCandidate],
        activations: dict[UUID, float],
    ) -> list[ScoredCandidate]:
        """Additive boost on post-RRF scores. Clamped to [0, 1]."""

    async def record_surfaced(
        self,
        agent_id: str,
        session_id: str,
        current_turn: int,
        surfaced: list[tuple[UUID, str, float]],
    ) -> None:
        """Write this turn's surfaced items back to WM.items with
        activation = rank-normalized score, last_surfaced_turn = current_turn.

        CRITICAL (v2 fix #2): opens its OWN DB session via self.db.session().
        Must NOT reuse the caller's session — this method is invoked via
        asyncio.create_task() and will outlive the caller's request context.
        Reusing the caller's AsyncSession corrupts connection state."""
```

### 4. Integration — call-site diff in `nous/api/tools.py`

The real call site for `spreading_activation_search` lives in `nous/api/tools.py:424` inside the `recall` tool. That's where residual seeds merge in. (v2 fix #3: concrete diff, not pseudocode.)


**v4 fix #3 — ToolDispatcher injection for `recall_deep`.**
`session_id` is not captured in the `recall_deep` closure. `ToolDispatcher.dispatch`
injects it per-call only for `spawn_task`, `cache_retrieve`, and `run_python`
(see `nous/api/tools.py:74-79`). The same pattern must extend to `recall_deep`:

```diff
# nous/api/tools.py — ToolDispatcher.dispatch, around line 74-80
            if session_id is not None and name == "run_python":
                args = {**args, "_session_id": session_id}
+           if session_id is not None and name == "recall_deep":
+               args = {**args, "_session_id": session_id}
```

And `recall_deep`'s function signature receives it:

```diff
  async def recall_deep(
      query: str,
      limit: int = 10,
      memory_types: list[str] | None = None,
+     _session_id: str | None = None,
  ) -> dict[str, Any]:
```

If `_session_id is None` (heartbeat, direct CLI calls, any dispatch without a session),
residual activation is skipped via the `if not _session_id: raise ValueError(...)` guard
already present in the diff below. The result is cold recall — no error, no degradation.

```diff
# nous/api/tools.py — recall tool, around line 395-440
+ residual_activations: dict[UUID, float] = {}
+ current_turn = 0
+ if settings.residual_activation_enabled:
+     try:
+         if not _session_id:
+             raise ValueError("no session_id — skip residual")
+         current_turn = await residual.current_turn(brain.agent_id, _session_id)
+         residual_activations = await residual.compute_activations(
+             brain.agent_id, _session_id, current_turn
+         )
+     except Exception:
+         logger.warning("residual_activation: compute failed, continuing cold")

  # F022: Graph expansion — expand top decisions
  graph_expanded = []
  if decision_results and settings.graph_recall_enabled:
      seen_ids = {d.id for d in decision_results}
      # ... density check unchanged ...

      if use_spreading:
          try:
              async with brain.db.session() as sa_session:
                  seeds = [
                      (d.id, "decision", d.score or 0.5)
                      for d in decision_results[:settings.graph_recall_max_expand]
                  ]
+                 if residual_activations:
+                     seeds += residual.seed_for_spreading(residual_activations)
                  activated = await spreading_activation_search(
                      sa_session, brain.agent_id, seeds, settings
                  )
```

**Post-fusion boost — plumbed through `Heart.recall` (v3 fix #2).**

`Heart.recall()` currently has no `agent_id`/`session_id` parameters (`nous/heart/heart.py:736`), so the tool layer must pass the already-computed activations in. This keeps the boost inside `_recall` where per-type merging and MMR happen, while leaving session identity out of Heart.

```diff
# nous/heart/heart.py
  async def recall(
      self,
      query: str,
      limit: int = 10,
      types: list[str] | None = None,
      session: AsyncSession | None = None,
+     residual_activations: dict[UUID, float] | None = None,
  ) -> list[RecallResult]:
      if session is None:
          async with self.db.session() as session:
-             return await self._recall(query, limit, types, session)
+             return await self._recall(query, limit, types, session, residual_activations)
-     return await self._recall(query, limit, types, session)
+     return await self._recall(query, limit, types, session, residual_activations)

  async def _recall(
      self,
      query: str,
      limit: int,
      types: list[str] | None,
      session: AsyncSession,
+     residual_activations: dict[UUID, float] | None = None,
  ) -> list[RecallResult]:
      ...
      # after per-type merge, before MMR:
+     if residual_activations and self.residual is not None:
+         merged = self.residual.boost_scores(merged, residual_activations)
      final = mmr(merged, lambda_=settings.mmr_lambda)
      return final
```

And the tool layer (`nous/api/tools.py::recall_deep`) becomes:

```diff
  # after residual_activations is computed (see earlier diff)
- results = await heart.recall(query=query, limit=limit, types=search_types)
+ results = await heart.recall(
+     query=query, limit=limit, types=search_types,
+     residual_activations=residual_activations or None,
+ )

  # ...later, after graph expansion and final response assembly:
+ if settings.residual_activation_enabled and residual is not None:
+     surfaced = [
+         (r.id, r.type, r.score or 0.0) for r in results
+     ]
+     # Fire-and-forget; record_surfaced opens its OWN AsyncSession (v2 fix #2)
+     asyncio.create_task(
+         residual.record_surfaced(
+             brain.agent_id, _session_id, current_turn, surfaced
+         )
+     )
```

**Why not apply the boost in `tools.py` instead of plumbing?** Because MMR runs inside `_recall`. Boosting only after MMR would mean the diversity step never sees residual-activated items' promoted scores — defeating the seed injection's purpose for ranking. Applying inside `_recall` before MMR preserves both behaviors.

**Fail-open guarantees:**
- `residual_activations=None` or `{}` → `_recall` skips the boost branch entirely, returning v1 behavior.
- `ResidualActivator.compute_activations` exception → caught in `tools.py`, logged, `residual_activations` stays `{}`.
- `ResidualActivator` not wired into Heart → `self.residual is None` short-circuit makes `_recall` a no-op on this axis.

### 5. Settings

```python
# nous/config.py additions
residual_activation_enabled: bool = False  # flagged off until eval'd
residual_decay_mode: Literal["geometric", "power_law"] = "geometric"
residual_decay_per_turn: float = 0.5       # geometric base
residual_power_law_alpha: float = 0.5      # power-law exponent (ACT-R default)
residual_activation_floor: float = 0.05
residual_top_k_carried: int = 20
residual_top_n_seeds: int = 5
residual_seed_weight: float = 0.3          # seed score multiplier into F022 CTE
residual_boost_weight: float = 0.15        # additive boost on post-RRF score
```

### 6. Migrations

**None.** (v2 fix #1 — previous draft proposed adding `turn_counter` to `working_memory`; repo already has `ConversationState.turn_count` which serves the same purpose.)

### 7. Observability

Emit a single structured log line per recall when flag is on:

```
residual_activation: turn=N carried=K seeded=N_s boosted_hits=M mode=geometric|power_law
  top_contributor={id, type, activation}
```

Add metrics counter `nous_residual_activation_hits_total` and histogram `nous_residual_activation_score_delta`.

---

## Evaluation

Cannot ship without measurement. Plan:

- **Eval harness:** reuse the retrieval eval harness referenced in F022/F045/F050 (sparklingdataocean ROC / Cohen's d / Lift@q). Add a **multi-turn test set** — sequences of 3–5 related queries — which the existing single-query harness doesn't cover.
- **Primary metrics:**
  - recall@10 and nDCG@10 on turn N+1 queries, conditional on turn N having surfaced the gold item. Target: ≥ +5% recall@10 on follow-up queries without degrading cold queries by more than 1%.
  - **Topic Chain Success Rate (TCSR) — new, v2 fix #5.** For a 5-turn topic-coherent sequence, fraction of turns 2–5 where recall@5 contains ≥ 1 item that was also in the previous turn's recall@10. Measures "train of thought" continuity, which recall@10 alone misses. Target: ≥ +15% vs. flag-off baseline on topic-coherent sequences, ≤ 2% degradation on topic-drift sequences.
  - Subset of LoCoMo (long-conversation memory benchmark) for external validity once TCSR shows signal.
- **Ablations:**
  1. Seed injection only (A), no post-fusion boost
  2. Post-fusion boost only (B), no seed injection
  3. Both (A+B) — expected winner
  4. Geometric decay sweep: {0.3, 0.5, 0.7, 0.9}
  5. **Power-law decay sweep: α ∈ {0.3, 0.5, 0.7, 1.0}** (v2 fix #4) — compared against best geometric
- **Guardrail:** fresh-query regression test — same test set as F022/F050, must not degrade beyond 1% recall@10.

---

## Phases

**Phase 1 — Core (ships behind flag, default off)**
1. `ResidualActivator` module with geometric + power-law decay modes
2. Call-site integration in `nous/api/tools.py::recall` (seed injection) and `Heart._recall` (post-fusion boost + record_surfaced)
3. Metrics + structured log
4. Unit tests: geometric decay math, power-law decay math, floor pruning, top-K truncation, record→compute round-trip, AsyncSession isolation in `record_surfaced`
5. Integration test: 3-turn sequence, assert gold-item boost on turn 2–3 under both decay modes

**Phase 2 — Calibration + lateral inhibition**
1. Multi-turn eval set extension to harness (topic-coherent + topic-drift)
2. Ablation runs (5 sets)
3. **Lateral inhibition follow-up** (v2 fix #5, separate GitHub issue to file on PR merge): SYNAPSE-style suppression of already-dominant items to prevent monotopic collapse. Triggered when top-K carried items all belong to a single cluster (cosine similarity > τ among carried items).
4. Flag default flip when TCSR + recall@10 bars are cleared

**Phase 3 — Deferred (separate features)**
- Per-edge Hebbian reinforcement (F027 territory)
- Cross-session residual (F037 territory)
- Adaptive decay learned from outcome signals

Phase 1 is ~1–1.5 days of work. Phase 2 is eval-bound.

---

## Risks

- **R1 — Echo chamber.** Hot items stay hot, topic drift suppressed. **Mitigation:** aggressive decay (geometric 0.5/turn or power-law α=0.5), floor=0.05 kills the tail within a few turns, top-K=20 bounds carryover, MMR still runs last. **Phase 2 adds lateral inhibition** as a stronger fix if monotopic sessions show degradation in ablation.
- **R2 — Stale context stickiness.** Session spans days, ancient activations still alive. **Mitigation:** turn-based decay (not wall-clock) naturally expires idle sessions; additionally clear activations > 24h old on session re-open.
- **R3 — Boost drowns fresh relevance.** `residual_boost_weight=0.15` additive on [0,1] scores means a cold hit with relevance 0.8 still beats a hot hit with relevance 0.3+0.15=0.45. Validated in ablation #2.
- **R4 — WM.items write contention.** `record_surfaced` writes on every recall. **Mitigation:** fire-and-forget via `asyncio.create_task`; `record_surfaced` opens its **own** isolated DB session (v2 fix #2); single-row UPDATE; already how `focus()` and `add_item()` work.
- **R5 — AsyncSession corruption.** `asyncio.create_task` outlives the request; reusing the caller's session causes "session is already closed" / connection pool corruption. **Mitigation (v2 fix #2):** `record_surfaced` takes a `db_factory`, opens `async with self.db.session()` internally, never accepts an external session. Unit-tested with a mock that verifies a fresh session is opened.
- **R6 — Wrong turn counter semantics.** What counts as "a turn"? **Decision:** use `ConversationState.turn_count` verbatim — whatever the conversation pipeline calls a turn is a turn here. Sleep-phase recalls and subtask recalls go through different code paths and do not touch this counter.
- **R7 — Geometric decay too aggressive.** Review flagged this — ACT-R's power-law fits human recency better and survives to turn 8+. **Mitigation (v2 fix #4):** both modes ship, ablation picks default. Worst case, we flip the default before flag flip.

---

## Open Questions

- **Q1:** Should residual activation flow across frames (debug → task) or reset at frame boundaries? Defaulting to **flows across frames** — frames are Nous's concern, not the user's train of thought.
- **Q2:** Should seed injection use the same decay constant as the score boost, or independent? Spec says same; open to splitting if ablations suggest it.
- **Q3:** Do we need a per-agent decay override, or is one global constant enough for v1? Starting global.
- **Q4 — v2 new:** Is lateral inhibition needed in v1 or can it wait for Phase 2? Deferring to Phase 2 per review — ship passive decay first, measure monotopic failure mode in the topic-drift eval set, add inhibition if TCSR degrades on drift sequences.
- **Q5 — v2 new:** For power-law mode, should α be a single constant or vary by memory type? Starting with single α; revisit if ablation shows per-type patterns.

---

## Out of scope explicitly

- Edge-level Hebbian reinforcement
- Frame-primed activation (new: "current frame seeds memories tagged with this frame") — strong candidate for **F052** as a follow-up, but conceptually distinct
- Emotional/salience-weighted activation
- Consolidation of hot clusters into abstraction nodes during sleep (A-MEM / H-MEM direction)
- Lateral inhibition (Phase 2 follow-up)

---

## Traceability

- FR-052.1 — Turn counter sourced from `ConversationState.turn_count` (no new column)
- FR-052.2 — `ResidualActivator.compute_activations` returns a bounded, decayed activation map under geometric or power-law mode
- FR-052.3 — Residual seeds flow into F022 spreading activation alongside current-query seeds at the `nous/api/tools.py::recall` call-site
- FR-052.4 — Post-fusion score boost is additive, bounded, and clamped
- FR-052.5 — `record_surfaced` persists to `WorkingMemory.items` without blocking the recall return **and uses its own isolated DB session**
- FR-052.6 — Decay mode selectable via `residual_decay_mode` setting; both geometric and power-law implemented and unit-tested
- NFR-052.1 — Zero added LLM calls
- NFR-052.2 — Overhead per recall ≤ 5ms p95 (measured: one WM read, one turn-count read, one WM write, O(K) math)
- NFR-052.3 — Fails open — any exception returns uncorrected ranking and logs WARN
- NFR-052.4 — Behind `residual_activation_enabled` flag, default off
- NFR-052.5 — Zero migrations
