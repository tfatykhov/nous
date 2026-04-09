# F037: Utility-Boosted Procedure Retrieval

**Status:** Draft  
**Proposed by:** Tim + Nous (inspired by Memento-Skills paper, arXiv:2603.18743)  
**Date:** 2026-04-08  
**Research basis:** Memento-Skills (Zhou et al., UCL/HKUST-GZ, March 2026) — behavioural skill routing via RL  
**Depends on:** F025 (RRF hybrid search — deployed), F012 (Procedure reinforcement — deployed)  
**Blocks:** None (additive, feature-flagged)

---

## Problem Statement

Nous already tracks procedure activation counts, success counts, and failure counts (F012), and computes effectiveness via Laplace smoothing. However, **this data is purely diagnostic** — it appears in the dashboard and health checks but is completely ignored during retrieval.

When `recall_deep` searches for procedures, results are ranked solely by hybrid search score (BM25 + cosine similarity via RRF). A procedure with 80% effectiveness over 50 activations ranks identically to one with 20% effectiveness over 50 activations, as long as their text similarity to the query is the same.

The Memento-Skills paper (2603.18743) demonstrated that **semantic similarity ≠ behavioural utility**. Their RL-trained skill router outperformed both BM25 and vanilla embeddings by incorporating empirical success signals. We don't need full RL — but we should at minimum blend effectiveness into retrieval scoring.

### Current state

- `activation_count`, `success_count`, `failure_count` exist on `Procedure` model ✓
- `_compute_effectiveness()` with Laplace smoothing: `(success + 1) / (success + failure + 2)` ✓
- `record_outcome()` called automatically per turn via cognitive layer (F012) ✓
- `get_low_effectiveness()` returns underperforming procedures for health checks ✓
- **Effectiveness NOT used in retrieval scoring** ✗
- **No task-type association** — we don't know which frame types a procedure succeeds at ✗
- **No automated evolution triggers** — no rewrite/retire signals from utility data ✗

### What's missing

1. **Utility boost in retrieval** — blend effectiveness into search ranking
2. **Task-type affinity tracking** — record which frame types each procedure activates for
3. **Evolution signals** — surface procedures that should be rewritten or retired

---

## Solution

### Part 1: Utility-Boosted Retrieval Scoring

Modify `ProcedureManager._search()` to apply a utility boost to the hybrid search score:

```
final_score = hybrid_score * (1 + α * utility_signal)
```

Where:
- `hybrid_score` = existing RRF score from `hybrid_search()`
- `α` (alpha) = weight of the utility boost (default: 0.15, configurable via env `NOUS_PROCEDURE_UTILITY_ALPHA`)
- `utility_signal` = normalized effectiveness centered at 0:
  - `utility_signal = effectiveness - 0.5` (range: -0.5 to +0.5)
  - A procedure with 50% effectiveness gets no boost (neutral)
  - A procedure with 80% effectiveness gets +0.045 boost (0.15 * 0.3)
  - A procedure with 20% effectiveness gets -0.045 penalty (0.15 * -0.3)

**Cold-start protection:** Only apply the utility boost when `activation_count >= MIN_ACTIVATIONS` (default: 5, configurable via env `NOUS_PROCEDURE_MIN_ACTIVATIONS_FOR_BOOST`). Below this threshold, effectiveness is too noisy to be useful. Use the existing Laplace smoothing which already handles this gracefully — but we add the min-activations gate as an extra safeguard.

**Feature flag:** `NOUS_PROCEDURE_UTILITY_BOOST` (default: `true`). Set to `false` to disable and revert to pure hybrid scoring.

### Part 2: Task-Type Affinity Tracking

Add a `procedure_task_affinity` table to track which frame types each procedure is activated for and how it performs:

```sql
CREATE TABLE heart.procedure_task_affinity (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    procedure_id UUID NOT NULL REFERENCES heart.procedures(id) ON DELETE CASCADE,
    frame_type TEXT NOT NULL,  -- 'task', 'research', 'conversation', 'decision', 'debug'
    activation_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_activated_at TIMESTAMPTZ,
    agent_id TEXT NOT NULL,
    UNIQUE(procedure_id, frame_type, agent_id)
);
CREATE INDEX idx_proc_task_affinity_proc ON heart.procedure_task_affinity(procedure_id);
```

When `record_outcome()` is called, also upsert into `procedure_task_affinity` with the current frame type from `turn_context`.

**Usage in retrieval:** When the current frame type is known, add a secondary affinity boost:

```
affinity_boost = β * (frame_effectiveness - 0.5)
final_score = hybrid_score * (1 + α * utility_signal + affinity_boost)
```

Where `β` = 0.10 (default, configurable via `NOUS_PROCEDURE_AFFINITY_BETA`), and `frame_effectiveness` is the Laplace-smoothed effectiveness for that specific frame type. Only applied when the procedure has >= `MIN_ACTIVATIONS` for that specific frame type.

### Part 3: Evolution Signals

Add a method `get_evolution_candidates()` to `ProcedureManager` that returns procedures needing attention:

```python
async def get_evolution_candidates(self, session=None) -> list[EvolutionCandidate]:
    """Return procedures that should be rewritten, retired, or investigated.
    
    Categories:
    - 'retire': effectiveness < 0.3 AND activation_count >= 10
    - 'rewrite': effectiveness < 0.5 AND activation_count >= 15
    - 'investigate': high activation_count (>= 30) but declining effectiveness
    - 'star': effectiveness >= 0.85 AND activation_count >= 10 (candidates for templates)
    """
```

This feeds into the existing health check system (F034) and the self-improvement loop.

**New Pydantic model:**

```python
class EvolutionCandidate(BaseModel):
    id: UUID
    name: str
    category: Literal['retire', 'rewrite', 'investigate', 'star']
    effectiveness: float
    activation_count: int
    reason: str
```

---

## Architecture

### Files to modify

1. **`nous/heart/procedures.py`** — Core changes:
   - `_search()`: Apply utility boost after hybrid_search
   - `_record_outcome()`: Upsert task affinity data
   - New: `get_evolution_candidates()`
   - New: `_compute_affinity_boost()`

2. **`nous/storage/models.py`** — New SQLAlchemy model:
   - `ProcedureTaskAffinity` SQLAlchemy model

   **`nous/heart/schemas.py`** — New Pydantic model:
   - `EvolutionCandidate` Pydantic model

3. **`nous/cognitive/layer.py`** — Pass frame_type to record_outcome:
   - Line ~859: Include `turn_context.frame_type` when calling `record_procedure_outcome()`

4. **`nous/heart/heart.py`** — Passthrough:
   - `record_procedure_outcome()`: Accept optional `frame_type` parameter
   - New: `get_evolution_candidates()` passthrough

5. **`nous/config.py`** (or settings) — New settings:
   - `NOUS_PROCEDURE_UTILITY_BOOST`: bool, default True
   - `NOUS_PROCEDURE_UTILITY_ALPHA`: float, default 0.15
   - `NOUS_PROCEDURE_AFFINITY_BETA`: float, default 0.10
   - `NOUS_PROCEDURE_MIN_ACTIVATIONS_FOR_BOOST`: int, default 5

6. **Migration** — New SQL migration (`sql/migrations/030_procedure_task_affinity.sql`):
   - Create `heart.procedure_task_affinity` table

### What NOT to change

- `hybrid_search()` function — stays untouched
- RRF scoring — stays untouched
- MMR re-ranking (F030) — stays untouched (utility boost happens before MMR)
- Procedure activation flow — just adds affinity tracking alongside existing counts
- Dashboard — can use existing effectiveness display; affinity data is bonus

---

## Testing Strategy

### Unit tests

1. **Utility boost calculation:**
   - Procedure with 80% effectiveness should score higher than 50% effectiveness (same hybrid score)
   - Procedure with < MIN_ACTIVATIONS should get no boost
   - α=0 should produce identical ranking to current behaviour
   - Negative boost for low-effectiveness procedures

2. **Task affinity:**
   - Upsert correctly increments counts per frame_type
   - Affinity boost only applies when frame_type is known
   - Cold-start protection per frame_type

3. **Evolution candidates:**
   - Procedures below thresholds flagged correctly
   - Star procedures identified
   - Empty result when all procedures healthy

4. **Feature flag:**
   - `NOUS_PROCEDURE_UTILITY_BOOST=false` produces identical results to current behaviour

### Integration tests

1. Full retrieval pipeline with utility boost enabled
2. Record outcome with frame_type, verify affinity table updated
3. Evolution candidates with mixed procedure health states

---

## Rollout Plan

1. **Phase 1:** Utility boost in retrieval (Part 1) — feature-flagged, default ON
2. **Phase 2:** Task affinity tracking (Part 2) — requires migration, can deploy independently
3. **Phase 3:** Evolution signals (Part 3) — wire into health checks

All phases are independently deployable and independently valuable.

---

## Configuration Defaults

| Setting | Default | Description |
|---------|---------|-------------|
| `NOUS_PROCEDURE_UTILITY_BOOST` | `true` | Enable/disable utility boosting |
| `NOUS_PROCEDURE_UTILITY_ALPHA` | `0.15` | Weight of global effectiveness boost |
| `NOUS_PROCEDURE_AFFINITY_BETA` | `0.10` | Weight of frame-type affinity boost |
| `NOUS_PROCEDURE_MIN_ACTIVATIONS_FOR_BOOST` | `5` | Minimum activations before boost applies |

---

## Success Metrics

- Procedures with high effectiveness should appear higher in recall results for matching queries
- Low-effectiveness procedures should naturally sink in ranking
- Health check should surface evolution candidates
- No regression in retrieval quality when boost is disabled (feature flag off)
- Measurable via A/B comparison: same queries with boost on/off, track which procedures get selected and their outcomes
