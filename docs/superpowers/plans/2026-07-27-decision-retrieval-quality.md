# Decision Retrieval Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stop stale and junk decisions from outranking current ones in the "## Related Decisions" prompt section, and fix the corrupted `failure` labels that make that section misleading.

**Reported symptom:** two *superseded* vacation recommendations rendered ABOVE the current one, alongside `[failure]` rows.

## Measured evidence (prod probes, read-only, 2026-07-27)

- Live `brain.query("vacation ideas for September trip", limit=8)`: the two superseded rows rank **#1/#2 (.931/.908)** above the current one (**#3, .887**). Reproduced verbatim.
- 8-query sample (n=64 retrieved decisions): **superseded 12.5%, noise 7.8%, failure 28.1%, pending 1.6%**. Dropping superseded changes the rendered top-3 in **3 of 8** queries.
- `outcome='superseded'` = 24 rows, **15 (62%) carry `superseded_by`** — but all 3 rows of the reported chain are NULL, so lineage alone can't fix the report.
- `noise` = 128 rows, **all written by the `resolve_decision` tool with `reviewer='agent'`** (no automated writer) — sampled rows are ordinary conversational replies wrongly recorded as decisions.
- **`failure` labels are ~60% machine artifacts**: 86 rows say `"Description contains error keywords"` — from a branch **deleted as a bug** (HD-4, decision_reviewer.py:51-61) whose rows were never remediated (63 of them at confidence ≥0.7, e.g. *"Fixed runner.sh… Root cause:…"* at 0.92); a further 58 are `ErrorSignal` low-confidence auto-fails, of which **37 were pushed under the 0.4 threshold purely by F058's ×0.7627 calibration** (effective auto-fail line = raw < 0.5245). The reported Switzerland row is one of these (raw 0.5 → 0.38 → `failure`).
- Prod `NOUS_EPISODE_CHUNKS_ENABLED=true` ⇒ recall_deep's `rerank_by_score` can be true; the **pre-turn path never re-sorts**.

## Design decisions (locked, post 2-agent review)

1. **Demote, don't exclude** (devil-P1-2). Per-outcome multiplicative factors — `superseded ×0.3`, `noise ×0.1` — mirroring the facts path's ×0.3 recency demotion. Multiplication is the only operator correct across BOTH score spaces `_query` returns (normalized RRF, and raw `ts_rank_cd` on the keyword-only fallback). Exclusion was rejected: 9 of 24 superseded rows have no linked successor, so exclusion can render an empty section where demotion still shows something labeled `[superseded]`.
2. **THE RE-SORT IS THE FEATURE** (correctness-P1-1, verified). `_query` builds summaries *"preserving search order"* (brain.py:806-829) and **nothing downstream re-sorts**: `_apply_staleness_penalty` returns input order, `_enforce_diversity`/`_apply_relevance_filter`/`_format_decisions` all iterate in order. A multiplier alone is a **no-op on the pre-turn path** — and worse than nothing, because a low score injected mid-list desynchronizes `_apply_relevance_filter`'s monotonic `prev_score` walk (correctness-P1-3) and can truncate genuine results. Demotion + **stable re-sort by score desc**, both inside `_query`, both gated on the same setting.
   - Arithmetic check: ×0.3 turns .931/.908 into .279/.272 vs Switzerland's .887 — clears the ~0.04 gaps by an order of magnitude.
3. **Seam:** `brain.py:823`, the `DecisionSummary` construction loop — the only point where score and outcome coexist. `None`-guard the score; normalize outcome via `(d.outcome or "pending")` (the column is nullable).
4. **Explicit `outcome=` wins** (correctness-P1-4): no demotion when a caller requests a specific outcome, mirroring the existing abandoned-suppression `else` branch (brain.py:710-715). `test_abandoned_filtering.py:105` stays green.
5. **Graph path (correctness-P1-2 resolution): FILTER, not demote.** Superseded/noise decisions re-enter via Stage 3+4 through `Brain._resolve_node_descriptions`, which returns `(description, created_at)` tuples with **no score to demote**. Mirror the existing abandoned suppression already present at brain.py:1467-1478 with the same outcome set, gated on the same setting. The asymmetry (demote at query, filter at graph) is deliberate and documented — the alternative is plumbing `outcome` through `NeighborResult`, which is disproportionate.
6. **Failures are KEPT** — and the real fix is upstream. A failure is a lesson, the Switzerland row was the chain *head*, and ~60% of `failure` labels are artifacts. Retrieval filtering is the wrong layer; Task 3 fixes the labeler.
7. **Default-ON with an identity kill switch, NOT land-dark** (correctness-P2-4). `{}` = today's behavior byte-identically. Precedent: `NOUS_PROFILE_EXCLUDE_SOURCES` (correctness-fix class). Compensated with telemetry since no A/B precedes it.
8. **Out of scope, stated:** `superseded_by`/lineage-based dropping (inert on the reported chain); `event_date` rendering (0/12 populated); the **fact `category` label — DEFERRED** until PR #572's category guidance is deployed AND verified, because ~4-5 of the 12 most recent `rule` facts are mislabeled and `(rule)` reads to the model as a directive (devil-P1-3: it would amplify the exact harm we've been fixing); `noise` write-path root cause (file separately).
9. **Sequencing:** Nous itself recorded `pending` proposals today (10:46/10:59) to lower `DEFAULT_FETCH_LIMITS["decision"]` 8→4 + add a similarity floor — same section. Not implemented; do NOT change both variables at once. This plan ships first.

## Global Constraints

- Branch `fix/decision-retrieval-quality`, fresh worktree off origin/main. cd + branch-verify before edits. Gate and commit are SEPARATE commands.
- **Baseline is red on the default (sqlite) backend** — `Brain._query` needs Postgres FTS. Measured on main: `test_brain.py` 4F/30P, `test_abandoned_filtering.py` 5F/3P, `test_context.py` 3F/28P. Do not attribute these to this change; verify under `NOUS_TEST_DB=postgres`.
- **Test strategy must survive the sqlite default** (correctness-P4-8): the demotion + ordering logic goes in a PURE helper unit-testable on any backend; only the e2e is `postgres_only`.
- All three `brain.query` consumers named in the PR: `context.py:744` (pre-turn), `retrieval_pipeline.py:1139` (recall_deep), `mcp.py:245` (MCP recall). `brain.list_decisions` is a separate ORM path (dashboard `GET /decisions`, procedure_learner, sleep_handler) — untouched. Calibration reads `Decision` directly and already restricts to success/partial/failure — provably untouched.

---

### Task 1: Outcome demotion + re-sort in `Brain._query`

**Files:** `nous/config.py`, `nous/brain/brain.py`, `tests/test_decision_demotion.py` (new, backend-agnostic), `tests/test_abandoned_filtering.py` (e2e).

**Interfaces:**
- Settings: `decision_outcome_score_factors: dict[str, float] = {"superseded": 0.3, "noise": 0.1}` (env `NOUS_DECISION_OUTCOME_SCORE_FACTORS`, JSON dict like `NOUS_CONTEXT_BUDGET_OVERRIDES`). **Validate every value in `(0, 1]` at Settings init** — a typo of `3` for `0.3` would PROMOTE the rows this fixes (correctness-P3-1).
- Pure helper in `brain.py` (module level, importable without a DB):

```python
def apply_outcome_demotion(
    scored: list[tuple[object, str | None, float | None]],
    factors: dict[str, float],
) -> list[tuple[object, float | None]]:
    """Multiply each score by its outcome's factor, then stable-sort desc.

    Returns (item, new_score) pairs. Empty ``factors`` is an exact no-op:
    no multiplication AND no re-sort, so merged order is preserved
    byte-identically (the kill switch).

    Multiplicative because _query returns TWO score spaces (normalized RRF,
    and raw ts_rank_cd on the keyword-only fallback) — a scale-free operator
    is correct in both. None scores pass through untouched and sort last.
    """
```

- [ ] **Step 1: Write the failing unit tests** (`tests/test_decision_demotion.py`, no DB):
  1. empty factors → returns input order unchanged AND scores untouched (kill-switch byte-identity);
  2. `superseded ×0.3` applied and the row **moves below** an undemoted lower-scored row — use the measured values `(.931 superseded, .908 superseded, .887 failure)` and assert the failure row comes first;
  3. `noise ×0.1`;
  4. unknown/`None` outcome → normalized to `"pending"` → untouched;
  5. `None` score → passes through, sorts last, no crash;
  6. equal scores keep input order (stable sort).
- [ ] **Step 2:** Red run: `uv run pytest tests/test_decision_demotion.py -q` → ImportError.
- [ ] **Step 3: Implement** the helper + wire into `_query`'s summary loop (brain.py:806-829): build `(orm_row, outcome, score)` triples, call the helper when `factors` is non-empty AND no explicit `outcome=` param was passed, then construct `DecisionSummary` in the returned order. Add a debug log with the demoted count.
- [ ] **Step 4:** Green run the unit file; then `NOUS_TEST_DB=postgres uv run pytest tests/test_brain.py tests/test_abandoned_filtering.py -q` → no regressions vs the stated baseline.
- [ ] **Step 5:** Add ONE `@pytest.mark.postgres_only` e2e in `test_abandoned_filtering.py`: seed a high-scoring superseded decision + a lower-scoring success on the same topic, assert the success ranks first with factors on, and that `outcome="superseded"` explicitly requested still returns undemoted rows.
- [ ] **Step 6:** Commit: `fix: demote superseded/noise decisions in retrieval + re-sort (Related Decisions ordering)`

### Task 2: Graph re-entry filter + recall_deep outcome visibility

**Files:** `nous/brain/brain.py` (`_resolve_node_descriptions` ~1467-1478), `nous/api/retrieval_pipeline.py` (`_decisions_to_pipeline` ~1971-1996), tests.

- [ ] **Step 1:** Test (postgres_only) that a superseded decision surfaced via the graph path is not returned by the resolver when factors are non-empty; and a unit assertion that `_decisions_to_pipeline` metadata now carries `outcome`.
- [ ] **Step 2:** Implement: extend the existing abandoned-suppression clause at brain.py:1467-1478 to also exclude the demotion-set outcomes (gated on the same setting being non-empty; comment the demote-vs-filter asymmetry and why). Add `"outcome": d.outcome` to the recall_deep decision metadata — currently recall_deep renders **no outcome at all**, so a superseded decision reaches the LLM unlabeled (correctness-P2-3).
- [ ] **Step 3:** Gate + commit: `fix: graph-path decision filter + outcome in recall_deep metadata`

### Task 3: ErrorSignal ↔ F058 (the corrupted `failure` labels)

**Files:** `nous/handlers/decision_reviewer.py` (~63-71), tests.

The auto-fail compares `decision.confidence` — the POST-F058 value — against `0.4`, so F058's ×0.7627 silently moved the real line to raw <0.5245 and auto-failed 37 appropriately-humble decisions. The code itself flags the circularity.

- [ ] **Step 1:** Test: a decision with `confidence_raw=0.5` (calibrated 0.38) is NOT auto-failed; one at raw 0.3 still is; a decision with no `confidence_raw` (legacy) uses the existing behavior.
- [ ] **Step 2:** Implement: compare against `confidence_raw` when present (falling back to `confidence`), so the threshold means what it says — an author-stated <0.4 confidence. Keep the threshold configurable via the existing settings if one exists; otherwise leave the constant and document.
- [ ] **Step 3:** Gate + commit: `fix: ErrorSignal auto-fail compares raw confidence, not F058-calibrated (37 false failures)`

### Task 4: `resolve_decision` requires `superseded_by` on supersession

**Files:** `nous/api/tools.py` (`_RESOLVE_DECISION_SCHEMA` ~1706-1721 + handler ~1531-1555), tests.

Complement, not substitute (devil-P2-4): it can't repair the 9 existing NULL rows, but it stops new lineage gaps.

- [ ] **Step 1:** Test: `outcome="superseded"` without `superseded_by` returns a clear error naming the requirement; with it, succeeds; other outcomes unaffected.
- [ ] **Step 2:** Implement (validation in the handler; sharpen the schema description). Do NOT make it a JSON-schema `required` conditional — the dispatcher validates required keys flatly.
- [ ] **Step 3:** Gate + commit: `fix: resolve_decision requires superseded_by when marking a decision superseded`

### Task 5: Docs + suites + PR + codex

- [ ] CLAUDE.md env row for `NOUS_DECISION_OUTCOME_SCORE_FACTORS`.
- [ ] Full-suite diff vs the origin/main baseline (sqlite default), plus `NOUS_TEST_DB=postgres` runs for the touched files.
- [ ] PR body: the measured evidence table; demote-not-exclude rationale + the re-sort-is-the-feature note; the ~20% wasted-slot cost of demotion vs exclusion and the explicit "no over-fetch in v1" (widening `_rrf_merge`'s limit changes `penalty_rank = limit + 1` and perturbs every single-list doc — correctness-P4-7); the graph filter/demote asymmetry; the failure-label finding with the follow-up remediation; deferrals (category label, event_date, lineage); sequencing note vs the in-flight fetch-limit proposal.
- [ ] Codex rounds until clean.

### Task 6 (post-merge, supervised, prod): remediate the corrupted failure labels

1. Capture: `CREATE TABLE nous_system._backfill_20260727_failure_labels AS SELECT id, outcome, outcome_result, reviewed_at FROM brain.decisions WHERE agent_id='nous-default' AND outcome='failure' AND (outcome_result LIKE 'Description contains error keywords%' OR (outcome_result LIKE 'Low confidence%' AND confidence_raw >= 0.4));`
2. Dry-run counts (expect ~86 + ~37).
3. Apply: reset those rows to `outcome=NULL, outcome_result=NULL, reviewed_at=NULL` (back to `pending`) so the reviewer/agent can re-judge them honestly. **Do NOT invent success labels.**
4. Verify: re-run the calibration report before/after and record both (Brier/accuracy will shift — that is the point; the old numbers were computed over corrupted labels).
5. Rollback: `UPDATE brain.decisions d SET outcome=b.outcome, outcome_result=b.outcome_result, reviewed_at=b.reviewed_at FROM nous_system._backfill_20260727_failure_labels b WHERE d.id=b.id;`
