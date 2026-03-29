# F024 Phase 3b — Review Fix Plan

> **Context:** 3-agent review of initial implementation found 4 P1s and 8 P2s. This plan fixes all issues, grouped by deployment phase.

---

## Fix Group A — Phase 0 Ship Blockers (fix now)

### Fix A1: FK + CHECK constraints (P1-4)

**Files:**
- `sql/migrations/022_rubric_outcome_signals.sql`
- `nous/storage/models.py`

**Changes:**
1. Add `REFERENCES nous_system.agents(id)` to `outcome_signals.agent_id` in SQL
2. Add `CHECK (confidence BETWEEN 0 AND 1)` to `outcome_signals.confidence` in SQL
3. Add `ForeignKey("nous_system.agents.id")` to both `RubricVersion.agent_id` and `OutcomeSignal.agent_id` in ORM
4. Add `CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_outcome_signals_confidence")` to `OutcomeSignal.__table_args__`
5. Fix `dimensions: Mapped[dict]` → `dimensions: Mapped[list]` on `RubricVersion` (P2-4) — ORM type annotation only, no migration needed

**Tests:** Existing 42 tests should still pass. Add constraint assertion to `test_rubric_schemas.py`.

---

### Fix A2: Seed race condition (P2-8)

**File:** `nous/main.py`

**Change:** Wrap `seed_v1()` in try/except IntegrityError — if two processes race, the loser silently continues (the other process already seeded).

```python
    if settings.rubric_enabled:
        from nous.cognitive.rubric import RubricManager
        rubric_manager = RubricManager(db=database, agent_id=settings.agent_id)
        existing = await rubric_manager.get_active()
        if not existing:
            try:
                await rubric_manager.seed_v1()
                logger.info("F024-3b: Seeded initial rubric v1.0.0")
            except IntegrityError:
                logger.debug("F024-3b: Rubric v1 already seeded by another process")
```

---

## Fix Group B — Phase 1 Blockers (fix before enabling evolution)

### Fix B1: Scores pipeline design (P1-1) — THE BIG ONE

**Problem:** `self_improvement_scores` on `OutcomeSignal` is always `None` because the self-improvement skill runs outside the session lifecycle. The evolver filters for non-null scores → zero rows → correlation never runs.

**Design options:**

**Option (a): Backfill scores from self-improvement skill output** (recommended)
- The self-improvement skill (ID `3497a6cc`) produces scores and stores them somewhere (likely as a fact or episode annotation)
- Add a method `RubricManager.backfill_scores()` that queries recent self-improvement results and updates matching `OutcomeSignal` rows
- Call this from the evolver before running correlation
- Pro: Decouples signal collection from score collection. Phase 0 collects signals, Phase 1 joins scores later.
- Con: Requires understanding the self-improvement skill's output format.

**Option (b): Remove `self_improvement_scores` filter entirely**
- Change evolver to correlate signal *types* with episode *outcomes* (success/partial/failure from `Episode.outcome`) instead of per-dimension scores
- Pro: Works immediately with no external dependency
- Con: Loses per-dimension granularity — can only tell "this dimension correlates with good outcomes" not "high Recall score predicts completion"

**Option (c): Have OutcomeDetector also classify per-dimension scores via LLM**
- Extend the LLM prompt to rate each dimension 1-10 based on the transcript
- Store these LLM-generated scores as `self_improvement_scores`
- Pro: Self-contained, no dependency on external skill
- Con: LLM-generated scores may not match the self-improvement skill's scoring criteria; adds cost per episode

**Recommendation:** Option (a) for accuracy, with option (b) as fallback. Implement both:
1. Remove the `isnot(None)` hard filter at line 73 — fetch all signals regardless of scores
2. Remove the second filter at line 97 (`if ep["scores"]`) — allow score-free episodes into correlation
3. Add a score-free correlation path: when `scores` is empty, use episode `outcome` (success/partial/failure) as a proxy numeric score (success=8, partial=5, failure=2) so correlations are non-degenerate
4. When real scores are available (via backfill), use them for richer per-dimension correlation
5. Phase 1 PR includes the backfill mechanism

**Score-free correlation logic** (new helper in `rubric_evolver.py`):
```python
def _build_episodes_for_correlation(
    signals: list,
    dim_names: list[str],
) -> list[dict]:
    """Build episode dicts for correlation, handling missing scores."""
    episode_signals: dict[UUID, dict] = defaultdict(lambda: {"scores": {}, "signals": []})
    for sig in signals:
        ep = episode_signals[sig.episode_id]
        ep["signals"].append(sig.signal_type)
        if sig.self_improvement_scores and not ep["scores"]:
            ep["scores"] = sig.self_improvement_scores

    # For episodes without scores, generate proxy scores from signal types
    # completed/praised → high proxy, corrected/reworked → low proxy
    _PROXY_SCORES = {"completed": 7, "praised": 8, "corrected": 3, "reworked": 2, "self_corrected": 5}
    for ep in episode_signals.values():
        if not ep["scores"]:
            proxy = sum(_PROXY_SCORES.get(s, 5) for s in ep["signals"]) / max(len(ep["signals"]), 1)
            ep["scores"] = {dim: proxy for dim in dim_names}

    return list(episode_signals.values())
```

**Import fix:** Add `detect_split_candidates, detect_merge_candidates` to imports at top of `rubric_evolver.py`.

**Files:**
- `nous/handlers/rubric_evolver.py` — remove both filters (lines 73 and 97), add `_build_episodes_for_correlation` helper, update imports
- `nous/cognitive/rubric.py` — add `backfill_scores()` method (for future Phase 1 integration)
- `nous/handlers/outcome_detector.py` — document that scores are optional for Phase 0

---

### Fix B2: Weight normalization bounds violation (P1-2)

**Files:**
- `nous/cognitive/correlation.py` — `suggest_weights()`
- `nous/handlers/rubric_evolver.py` — `execute_split()`, `execute_merge()`

**Change:** Replace single-pass clamp+normalize with iterative projection:

```python
def _normalize_weights(weights: dict[str, float], min_w: float = 0.10, max_w: float = 0.40) -> dict[str, float]:
    """Iteratively clamp and normalize until stable."""
    result = dict(weights)
    for _ in range(10):  # converges in 2-3 iterations
        total = sum(result.values())
        if total == 0:
            return result
        result = {d: w / total for d, w in result.items()}
        clamped = {d: max(min_w, min(max_w, w)) for d, w in result.items()}
        if clamped == result:
            return {d: round(w, 4) for d, w in result.items()}
        result = clamped
    return {d: round(w, 4) for d, w in result.items()}
```

Apply this in `suggest_weights()`, `execute_split()`, and `execute_merge()` — replacing all inline normalize blocks.

**Test:** Add test case that reproduces the bug (3 dims, one at 0.40 cap, others at 0.10).

---

### Fix B3: Wire split/merge detection into evolution cycle (P2-1)

**File:** `nous/handlers/rubric_evolver.py`

**Change:** After computing correlations and before returning the report, call:

```python
report.suggested_splits = detect_split_candidates(correlations)
# Build dimension profiles for merge detection
dim_profiles = {}
for dim_name in dim_names:
    dim_profiles[dim_name] = [
        c.pearson_r for c in correlations if c.dimension == dim_name
    ]
report.suggested_merges = detect_merge_candidates(dim_profiles)
```

---

### Fix B4: Wire evolver into sleep handler (P2-2)

**File:** `nous/main.py`

**Change:** After creating `rubric_evolver` (in the handler wiring block), wire it into the sleep handler:

```python
if sleep_handler is not None and rubric_evolver is not None:
    sleep_handler._rubric_evolver = rubric_evolver
```

**File:** `nous/handlers/sleep_handler.py`

**Change 1:** Add attribute in `__init__` (after `self._procedure_learner = None` at line 116):
```python
        self._rubric_evolver = None  # F024-3b: Set externally if enabled
```

**Change 2:** Add `_phase_evolve_rubric` method after `_phase_generalize` (line 374). This is Phase 6, runs after procedure learning:
```python
    async def _phase_evolve_rubric(self, sleep_stats: dict) -> bool:
        """Phase 6: Rubric evolution — adjust weights based on outcome correlations."""
        if self._rubric_evolver:
            try:
                report = await self._rubric_evolver.run_evolution_cycle()
                if report and report.suggested_weights:
                    sleep_stats["rubric_evolved"] = True
                    logger.info("Sleep rubric evolution: new weights suggested")
                else:
                    sleep_stats["rubric_evolved"] = False
                    logger.debug("Sleep rubric evolution: no changes")
                return True
            except Exception:
                logger.warning("Rubric evolution phase failed", exc_info=True)
                return False
        else:
            logger.debug("Sleep phase: rubric evolution (no evolver configured)")
            return True
```

**Change 3:** Call it from `_run_sleep` (after `_phase_generalize` call at line 175):
```python
                success = await self._phase_evolve_rubric(sleep_stats)
                if not success:
                    logger.warning("Sleep rubric evolution phase failed, continuing")
```

---

### Fix B5: Rate limit excludes rollback versions (P2-5)

**File:** `nous/handlers/rubric_evolver.py`

**Change:** Add status filter to the rate limit query:

```python
recent_result = await session.execute(
    select(RubricVersion).where(
        RubricVersion.agent_id == self._agent_id,
        RubricVersion.created_at >= week_ago,
        RubricVersion.status != "rollback",  # Don't count rollbacks
    )
)
```

---

## Fix Group C — Phase 3 Blockers (fix before enabling new dimensions)

### Fix C1: Proposal REST endpoints (P1-3)

**File:** `nous/api/rest.py`

**Add 3 endpoints** (from the original plan, Task 10):

1. `POST /rubric/propose-dimension` — store proposal as tagged fact
2. `GET /rubric/proposals` — list pending proposals
3. `POST /rubric/proposals/{id}/approve` — approve and add dimension to active rubric

These were in the plan but the implementation subagent skipped them. Copy from the plan document (`docs/plans/2026-03-26-f024-phase3b-self-modifying-rubrics.md`, Task 10).

---

### Fix C2: Manual rollback endpoint + degradation reporting (P2-3)

**File:** `nous/api/rest.py`

**Add rollback endpoint:**
```python
async def rollback_rubric(request: Request) -> JSONResponse:
    """POST /rubric/rollback — rollback to a previous version."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    target = body.get("target_version")
    if not target:
        return JSONResponse({"error": "Missing target_version"}, status_code=400)
    result = await rubric_manager.rollback(target)
    if result:
        return JSONResponse({"status": "rolled_back", "new_version": result.version})
    return JSONResponse({"error": "Target version not found"}, status_code=404)
```

Route: `Route("/rubric/rollback", rollback_rubric, methods=["POST"])`

**File:** `nous/handlers/rubric_evolver.py`

**Add degradation check to `run_evolution_cycle` report output** (after computing correlations, before applying weights). This makes degradation visible in the report without auto-triggering rollback (auto-rollback deferred to a future PR when we have enough data to validate thresholds):

```python
# Check degradation: split episodes into before/after midpoint for trend detection
if len(episodes) >= 10:
    midpoint = len(episodes) // 2
    before = episodes[:midpoint]
    after = episodes[midpoint:]
    if self.check_degradation(before, after):
        logger.warning("F024-3b: Outcome degradation detected — flagging in report")
        # Don't auto-rollback yet, just surface in report
```

Note: Full auto-rollback (spec guardrail 8) deferred to Phase 2 when we have sufficient episode volume and validated thresholds. The `check_degradation()` method and `rollback()` method are both implemented and available for manual use.

---

## Fix Group D — Code Quality (fix anytime)

### Fix D1: REST proxy-check cleanup (P2-6)

Remove redundant try/except proxy resolution pattern from rubric endpoints. Use simple `if not rubric_manager:` guard consistent with other endpoints.

### Fix D2: Move signal query to RubricManager (P2-7)

Move the inline `select(OutcomeSignal)` from `get_outcome_signals` REST handler into a `RubricManager.get_signals()` method.

---

## Summary

| Group | When | Fixes | Effort |
|-------|------|-------|--------|
| **A** | Now (Phase 0 ship) | FKs, CHECK, type annotation, seed race | ~30 min |
| **B** | Before Phase 1 enable | Scores pipeline, normalization bug, wire split/merge + sleep handler, rate limit | ~2-3 hrs |
| **C** | Before Phase 3 enable | Proposal endpoints, rollback endpoint | ~1 hr |
| **D** | Anytime | Code quality cleanup | ~30 min |
