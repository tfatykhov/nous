# F023 — Memory Admission Control: Implementation Design

> **Feature Spec:** docs/features/F023-memory-admission-control.md (v3)
> **Approach:** 2-PR plan (Approach B — single working gate + follow-up feedback loop)
> **Core architecture:** Follows spec. PR decomposition and shadow-mode default differ.

---

## Design Decisions (vs. spec)

1. **2 PRs instead of 4** — PR 1 delivers a complete working gate (controller + schema + wiring + settings + tests). No dead code between PRs. PR 2 adds feedback loop, sleep integration, and benchmark.
2. **Shadow mode defaults to `True` in settings** — safe first deploy, collects baseline data before rejecting anything. Note: `AdmissionConfig` dataclass defaults to `shadow_mode=False` — the safe-first-deploy default is enforced via `NOUS_ADMISSION_SHADOW_MODE=True` in `config.py`, which Heart.__init__() passes to AdmissionConfig. This is intentional: the dataclass is a neutral container, the settings layer controls defaults.
3. **LLM utility from day one** — model configurable via `NOUS_ADMISSION_UTILITY_MODEL`, falls back to `NOUS_BACKGROUND_MODEL`. Must use full Anthropic model ID (e.g., `claude-haiku-4-5-20251001`), not short names.
4. **Type priors and bypass sources not in env vars** — hardcoded defaults to avoid config sprawl. Tunable in code.
5. **No existing data backfill** — pre-F023 facts get NULL admission_score. Sleep Phase 2 (PR 2) re-scores gradually.
6. **`_supersede()` and `_contradict()` bypass the gate** — these internal callers of `_learn()` are intentional replacements/conflicts, not new extractions. They must bypass admission via `source="supersede"` / `source="contradict"` added to the bypass list. Without this, rejecting a replacement fact while the old one is already deactivated corrupts memory state.

---

## PR 1 — Working Admission Gate

### New Files

**`nous/heart/admission.py`** — Core admission control module:
- `AdmissionConfig` dataclass — weights, threshold, type priors, bypass sources, LLM settings, shadow mode flag. Constructed from Settings in Heart.__init__().
- `AdmissionResult` dataclass — admitted bool, composite_score, per-dimension scores dict, threshold, explanation, bypass/shadow flags.
- `AdmissionController` class — single public method: `async score(fact_input, embedding, max_existing_similarity, source_text, session) -> AdmissionResult`

5 scoring dimensions:
1. **Utility** (w=0.25) — LLM call to configurable model with calibration anchors. Heuristic fallback on failure.
2. **Confidence** (w=0.15) — ROUGE-L F1 between fact and source text. Source-penalty heuristic fallback.
3. **Novelty** (w=0.20) — `1 - max_existing_similarity` from dedup query.
4. **Recency** (w=0.10) — `e^(-λ * hours)`, λ=0.01/hr, uses FactInput.source_timestamp.
5. **Type Prior** (w=0.30) — category lookup. Most influential per Zhang et al. ablation.

ROUGE-L: whitespace tokenization + LCS DP. O(min(m,n)) space. No external deps.

Bypass: sources in bypass list (user_direct, user_stated, identity, censor, supersede, contradict) → admitted with score=1.0, no scoring.
Shadow: scores everything, admits everything, logs SHADOW_WOULD_REJECT/ADMIT.

**`sql/migrations/017_memory_admission_control.sql`:**
```sql
-- Safe for existing installations: IF NOT EXISTS + DEFAULT values
-- Existing facts get admission_score=NULL (no backfill), recall_count=0, last_recalled_at=NULL
ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS admission_score FLOAT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS recall_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_recalled_at TIMESTAMPTZ DEFAULT NULL;

COMMENT ON COLUMN heart.facts.admission_score IS
    'A-MAC composite score at time of admission. NULL for pre-F023 facts.';
COMMENT ON COLUMN heart.facts.recall_count IS
    'Number of times this fact was recalled and used in a response.';
COMMENT ON COLUMN heart.facts.last_recalled_at IS
    'Last time this fact was recalled and used.';
```

Migration runs via `migrator.py` on startup. Uses `IF NOT EXISTS` so it's idempotent — safe to run on fresh installs and existing databases with data.

**`tests/test_admission.py`** — Unit tests for all scoring dimensions, ROUGE-L, bypass, shadow mode.
**`tests/test_admission_integration.py`** — End-to-end tests through FactManager with real Postgres.

### Modified Files

**`nous/heart/schemas.py`:**
- `FactInput`: add `source_timestamp: datetime | None = None`
- New `FactRejected(BaseModel)`: admitted=False, content, composite_score, threshold, scores, explanation

**`nous/storage/models.py`:**
- `Fact` model: add `admission_score: Mapped[float | None]`, `recall_count: Mapped[int | None]` (server_default="0"), `last_recalled_at: Mapped[datetime | None]`

**`nous/config.py`:**
- `admission_control_enabled: bool = True`
- `admission_shadow_mode: bool = True` (safe default)
- `admission_threshold: float = 0.55`
- `admission_w_utility: float = 0.25`
- `admission_w_confidence: float = 0.15`
- `admission_w_novelty: float = 0.20`
- `admission_w_recency: float = 0.10`
- `admission_w_type_prior: float = 0.30`
- `admission_recency_lambda: float = 0.01`
- `admission_utility_model: str = ""` (empty = fall back to background_model)
- `admission_utility_llm_enabled: bool = True`

**`nous/heart/heart.py`:**
- `Heart.__init__()`: if admission_control_enabled, construct AdmissionConfig from settings, create AdmissionController with LLM client, pass to FactManager.
- `Heart.learn()`: update return type to `-> FactDetail | FactRejected` to propagate union type

**`nous/heart/facts.py`:**
- `FactManager.__init__()`: accept `admission_controller: AdmissionController | None = None`
- New `_find_max_similarity(embedding, exclude_ids, session) -> float | None`: HNSW top-1 lookup
- New `_get_source_text(fact_input, session) -> str | None`: fetch episode.content by PK. Import from `nous.storage.models` (not `nous.heart.models` which doesn't exist).
- `_learn()` return type: `-> FactDetail | FactRejected`. Insert gate after dedup, before session.add(). Initialize `admission_result = None` before conditional. If rejected: log, emit event, return FactRejected. If admitted: store admission_score on Fact.
- `learn()` public method: update return type to `-> FactDetail | FactRejected`
- `_supersede()`: set `source="supersede"` on the new FactInput before calling `_learn()` (bypass gate)
- `_contradict()`: set `source="contradict"` on the new FactInput before calling `_learn()` (bypass gate)

**`nous/api/tools.py`:**
- `learn_fact` tool: set `source="user_direct"` on FactInput (guarantees bypass)
- Handle FactRejected return: inform user with score breakdown, suggest override

**`nous/handlers/knowledge_extractor.py`:**
- Populate `source_timestamp` from compacted episode's timestamp on FactInput
- Handle `FactRejected` return from `heart.learn()` — log rejection, don't count as stored

**`nous/handlers/fact_extractor.py`:**
- Handle `FactRejected` return from `heart.learn()` — log rejection, don't count as stored

**`nous/handlers/sleep_handler.py`:**
- Handle `FactRejected` return from `heart.learn()` in reflection phase — log rejection

**`nous/storage/migrator.py`:**
- Register migration 017

### _learn() Flow (updated)

```
1. Generate embedding          (existing)
2. Dedup check                 (existing) → early return if dupe
3. *** Admission gate ***      (NEW)
   - Skip if controller is None
   - _find_max_similarity() for novelty
   - _get_source_text() for ROUGE-L
   - controller.score() → AdmissionResult
   - If rejected: log, emit fact_rejected event, return FactRejected
4. Create Fact ORM object      (existing, now stores admission_score)
5. Subject supersession        (existing)
6. Emit fact_learned event     (existing)
7. Contradiction detection     (existing)
```

---

## PR 2 — Feedback Loop + Sleep + Benchmark

### Changes

**`nous/heart/facts.py` or `nous/cognitive/layer.py`:**
- On every recall_deep that returns facts used in a response: increment `recall_count`, update `last_recalled_at`

**`nous/handlers/sleep_handler.py`:**
- Sleep Phase 2 integration: re-score existing facts using AdmissionController, deactivate facts below a prune threshold (lower than admission threshold)
- Use `recall_count` + `last_recalled_at` as signals: facts admitted 2+ weeks ago with recall_count=0 → prune candidates

**Benchmark script:**
- Sample 100 existing facts, run admission scorer, measure precision/recall/F1
- Target: F1 >= 0.55 (matching Zhang et al.)
- Tune weights/threshold if below target

**Observability:**
- Dashboard queries: rejection rate by source, by category, score distributions
- Weekly report template

---

## Testing Strategy

### Unit Tests (`test_admission.py`)
- Each scoring dimension independently with known inputs/outputs
- Composite scoring: weighted sum math, threshold boundary (0.549 rejects, 0.551 admits)
- Bypass: all bypass sources skip scoring, return score=1.0
- Shadow mode: always admits, explanation contains SHADOW_WOULD_REJECT
- ROUGE-L: exact match=1.0, no overlap=0.0, partial=known value, empty=0.5
- LCS edge cases: empty, single element, all matching
- Config construction: weights, threshold, model fallback to background_model
- LLM utility: mock httpx, verify prompt has anchors, verify fallback on error

### Integration Tests (`test_admission_integration.py`)
- End-to-end through FactManager._learn() with real Postgres
- Admitted fact: stored with admission_score populated
- Rejected fact: NOT stored, returns FactRejected, fact_rejected event emitted
- User bypass: source="user_direct" always stored
- Shadow mode: low-quality fact stored, score recorded, SHADOW_WOULD_REJECT logged
- Disabled: no scoring, facts stored as before, admission_score=NULL
- Source text retrieval: source_episode_id → episode content for ROUGE-L
- KnowledgeExtractor source_timestamp propagation
- Supersede bypass: `_supersede()` stores replacement fact even with low admission score
- Contradict bypass: `_contradict()` stores contradiction fact even with low admission score
- Handler rejection handling: KnowledgeExtractor/FactExtractor log but don't crash on FactRejected

---

## Configuration Summary

| Setting | Default | Notes |
|---------|---------|-------|
| `NOUS_ADMISSION_CONTROL_ENABLED` | `true` | Master switch |
| `NOUS_ADMISSION_SHADOW_MODE` | `true` | Safe first deploy |
| `NOUS_ADMISSION_THRESHOLD` | `0.55` | Zhang et al. validated |
| `NOUS_ADMISSION_W_UTILITY` | `0.25` | |
| `NOUS_ADMISSION_W_CONFIDENCE` | `0.15` | |
| `NOUS_ADMISSION_W_NOVELTY` | `0.20` | |
| `NOUS_ADMISSION_W_RECENCY` | `0.10` | |
| `NOUS_ADMISSION_W_TYPE_PRIOR` | `0.30` | |
| `NOUS_ADMISSION_RECENCY_LAMBDA` | `0.01` | Half-life ~3 days |
| `NOUS_ADMISSION_UTILITY_MODEL` | `""` | Falls back to NOUS_BACKGROUND_MODEL |
| `NOUS_ADMISSION_UTILITY_LLM_ENABLED` | `true` | Can disable LLM scoring |

## Rollout

1. Deploy PR 1 with shadow mode on (default). Collect data.
2. Review score distributions, validate threshold on real data.
3. Set `NOUS_ADMISSION_SHADOW_MODE=false` to activate gate.
4. Deploy PR 2 for feedback loop and sleep pruning.
