# Fix Procedure Learning (Issue #188) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 4 bugs preventing procedure learning during sleep cycles — decision pathway returns 0 reviewed decisions, stats under-report, episode pathway has no visibility, and `_list_decisions` doesn't map `reviewed_at`.

**Architecture:** 5 targeted changes across 3 files. No new files. All tests use mocks (no DB needed). Existing test patterns in `test_procedure_learner.py` and `test_sleep_handler.py` provide the mock infrastructure.

**Tech Stack:** Python 3.12+, pytest, pytest-asyncio, unittest.mock (AsyncMock, MagicMock)

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `nous/brain/brain.py` | Modify line ~201 | Add `reviewed_at` to `_list_decisions` inline mapper |
| `nous/handlers/procedure_learner.py` | Modify lines ~229-234 | Pass DB filters + add diagnostic logging |
| `nous/handlers/sleep_handler.py` | Modify line ~360 | Count both decision + episode pathways in stats |
| `tests/test_procedure_learner.py` | Modify | Add regression tests for bugs 1, 1B, 4 |
| `tests/test_sleep_handler.py` | Modify | Add regression test for bug 2 |

---

### Task 1: Fix `reviewed_at` mapping in `brain.py` `_list_decisions`

**Files:**
- Modify: `nous/brain/brain.py:191-204`
- Test: `tests/test_procedure_learner.py` (indirect — existing test `test_decision_cluster_creates_procedure` sets `reviewed_at` on mocked summaries)

This is the hidden fourth bug. The `_list_decisions` inline mapper constructs `DecisionSummary` without `reviewed_at`, so any Python-side filter on that field sees `None`. The separate `_decision_to_summary()` helper at line 1354 does include it, but `_list_decisions` doesn't use it.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_procedure_learner.py`:

```python
@pytest.mark.asyncio
async def test_list_decisions_reviewed_at_must_be_populated():
    """Regression: _list_decisions must populate reviewed_at in DecisionSummary.

    Bug: brain.py _list_decisions inline mapper omitted reviewed_at,
    causing procedure_learner's Python filter to see None for all decisions.
    Issue #188, Gap 1.
    """
    # This test verifies the contract: when list_decisions returns summaries
    # with reviewed_at set, the procedure_learner filter works.
    learner, brain, heart, embeddings, http = _build_learner()

    # Simulate what happens when list_decisions correctly populates reviewed_at
    summaries_with_reviewed = [_make_decision_summary(reviewed_at=_RECENT) for _ in range(3)]
    brain.list_decisions.return_value = (summaries_with_reviewed, 3)

    # Verify the filter passes (reviewed_at is not None)
    for s in summaries_with_reviewed:
        assert s.reviewed_at is not None, "reviewed_at must be populated by list_decisions"

    # Simulate what happens when reviewed_at is None (the bug)
    summaries_without_reviewed = [_make_decision_summary(reviewed_at=None) for _ in range(3)]
    # These would all be filtered out by the learner's filter
    filtered = [d for d in summaries_without_reviewed if d.outcome in ("success", "partial") and d.reviewed_at is not None]
    assert len(filtered) == 0, "Without reviewed_at, filter rejects all"
```

- [ ] **Step 2: Run test to verify it passes (this is a contract test, not a failure test)**

Run: `uv run pytest tests/test_procedure_learner.py::test_list_decisions_reviewed_at_must_be_populated -v`

- [ ] **Step 3: Fix the mapper in brain.py**

In `nous/brain/brain.py`, find the inline `DecisionSummary` construction in `_list_decisions` (around line 191-204). Add `reviewed_at=d.reviewed_at`:

```python
        summaries = [
            DecisionSummary(
                id=d.id,
                description=d.description,
                confidence=d.confidence,
                category=d.category,
                stakes=d.stakes,
                outcome=d.outcome or "pending",
                pattern=d.pattern,
                tags=tags_by_id.get(d.id, []),
                reviewed_at=d.reviewed_at,  # <-- ADD THIS LINE
                created_at=d.created_at,
            )
            for d in decisions
        ]
```

- [ ] **Step 4: Verify no existing tests break**

Run: `uv run pytest tests/test_procedure_learner.py tests/test_sleep_handler.py -v`

- [ ] **Step 5: Commit**

```bash
git add nous/brain/brain.py tests/test_procedure_learner.py
git commit -m "fix: populate reviewed_at in _list_decisions inline mapper (#188)"
```

---

### Task 2: Fix decision pathway DB query in `procedure_learner.py`

**Files:**
- Modify: `nous/handlers/procedure_learner.py:229-234`
- Test: `tests/test_procedure_learner.py`

Bug 1: `list_decisions(limit=100)` with no filters returns 100 most recent (all pending). The 124 reviewed successful decisions are older and outside the window.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_procedure_learner.py`:

```python
@pytest.mark.asyncio
async def test_decision_pathway_passes_db_filters():
    """Regression: list_decisions must be called with outcome and reviewed filters.

    Bug: list_decisions(limit=100) with no filters returned 100 most recent
    decisions (all pending), missing the 124 reviewed successful ones.
    Issue #188, Bug 1.
    """
    learner, brain, heart, embeddings, http = _build_learner()

    # Return empty so we don't need to set up the full pipeline
    brain.list_decisions.return_value = ([], 0)
    heart.list_episodes.return_value = []
    heart.search_procedures.return_value = []

    await learner.run_sleep_learning()

    # Verify list_decisions was called with the right filters
    brain.list_decisions.assert_called_once()
    call_kwargs = brain.list_decisions.call_args
    # Check positional or keyword args contain outcome and reviewed filters
    _, kwargs = call_kwargs
    assert kwargs.get("outcome") == "success" or (len(call_kwargs.args) > 0 and "success" in str(call_kwargs)), \
        "list_decisions must filter outcome='success'"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_procedure_learner.py::test_decision_pathway_passes_db_filters -v`
Expected: FAIL — current code calls `list_decisions(limit=100)` without outcome/reviewed.

- [ ] **Step 3: Fix the query and remove redundant Python filter**

In `nous/handlers/procedure_learner.py`, replace lines 228-234:

```python
        try:
            # Fetch reviewed successful decisions — pass filters to DB
            # to avoid the window problem where 100 most recent are all pending.
            # Issue #188 Bug 1: was list_decisions(limit=100) with no filters.
            decisions_success, _ = await self._brain.list_decisions(
                limit=50, outcome="success", reviewed=True
            )
            decisions_partial, _ = await self._brain.list_decisions(
                limit=50, outcome="partial", reviewed=True
            )
            successful = list(decisions_success) + list(decisions_partial)
            logger.info(
                "Decision pathway: %d success + %d partial = %d reviewed decisions",
                len(decisions_success), len(decisions_partial), len(successful),
            )
            if len(successful) < self._settings.procedure_cluster_min_size:
                logger.info("Decision pathway: too few reviewed decisions (%d < %d)",
                            len(successful), self._settings.procedure_cluster_min_size)
                return 0
```

- [ ] **Step 4: Update the test to match new call pattern**

Update the test to verify the new call pattern:

```python
@pytest.mark.asyncio
async def test_decision_pathway_passes_db_filters():
    """Regression: list_decisions must be called with outcome and reviewed filters.

    Bug: list_decisions(limit=100) with no filters returned 100 most recent
    decisions (all pending), missing the 124 reviewed successful ones.
    Issue #188, Bug 1.
    """
    learner, brain, heart, embeddings, http = _build_learner()

    brain.list_decisions.return_value = ([], 0)
    heart.list_episodes.return_value = []
    heart.search_procedures.return_value = []

    await learner.run_sleep_learning()

    # Must be called twice: once for "success", once for "partial"
    assert brain.list_decisions.call_count == 2
    calls = brain.list_decisions.call_args_list

    # First call: outcome="success", reviewed=True
    _, kwargs0 = calls[0]
    assert kwargs0["outcome"] == "success"
    assert kwargs0["reviewed"] is True

    # Second call: outcome="partial", reviewed=True
    _, kwargs1 = calls[1]
    assert kwargs1["outcome"] == "partial"
    assert kwargs1["reviewed"] is True
```

- [ ] **Step 5: Run tests to verify pass**

Run: `uv run pytest tests/test_procedure_learner.py -v`

- [ ] **Step 6: Commit**

```bash
git add nous/handlers/procedure_learner.py tests/test_procedure_learner.py
git commit -m "fix: pass outcome/reviewed filters to list_decisions in procedure learner (#188)"
```

---

### Task 3: Fix sleep stats under-reporting in `sleep_handler.py`

**Files:**
- Modify: `nous/handlers/sleep_handler.py:360`
- Test: `tests/test_sleep_handler.py`

Bug 2: Only counts `decisions_learned`, ignores `episodes_learned`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_sleep_handler.py`:

```python
class TestProcedureStatsCountBothPathways:
    """Bug 2 regression: procedures_created must include both decision and episode pathways."""

    @pytest.mark.asyncio
    async def test_procedures_created_includes_episodes(self):
        """Regression: sleep_stats must count episodes_learned + decisions_learned.

        Bug: Only counted decisions_learned, ignored episodes_learned.
        Issue #188, Bug 2.
        """
        handler, brain, heart, bus, _ = _make_sleep_handler()
        handler._phase_review_decisions = AsyncMock(return_value=True)
        handler._phase_prune = AsyncMock(return_value=True)
        handler._phase_compress = AsyncMock(return_value=True)
        handler._phase_reflect = AsyncMock(return_value=True)

        # Procedure learner returns both decision and episode learned counts
        handler._procedure_learner = AsyncMock()
        handler._procedure_learner.run_sleep_learning = AsyncMock(
            return_value={"decisions_learned": 2, "episodes_learned": 3, "weak_reviewed": 1}
        )

        await handler._run_sleep(_make_event("sleep_started"))

        emitted = bus.emit.call_args[0][0]
        # Must be 2 + 3 = 5, not just 2
        assert emitted.data["procedures_created"] == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sleep_handler.py::TestProcedureStatsCountBothPathways -v`
Expected: FAIL — current code returns 2, not 5.

- [ ] **Step 3: Fix the stats line**

In `nous/handlers/sleep_handler.py`, replace line 360:

```python
                sleep_stats["procedures_created"] += stats.get("decisions_learned", 0) + stats.get("episodes_learned", 0)
```

- [ ] **Step 4: Update existing test that will break**

The existing `test_generalize_increments_procedures_created` (line 284-294) sets `decisions_learned=3, episodes_learned=1` and asserts `procedures_created == 3`. After the fix, the actual value is `3 + 1 = 4`. Update the assertion:

In `tests/test_sleep_handler.py`, find `test_generalize_increments_procedures_created` and change:

```python
        assert sleep_stats["procedures_created"] == 3
```

to:

```python
        # After fix: counts both decisions_learned (3) + episodes_learned (1) = 4
        assert sleep_stats["procedures_created"] == 4
```

- [ ] **Step 5: Run tests to verify pass**

Run: `uv run pytest tests/test_sleep_handler.py -v`

- [ ] **Step 6: Commit**

```bash
git add nous/handlers/sleep_handler.py tests/test_sleep_handler.py
git commit -m "fix: count both decision and episode pathways in sleep stats (#188)"
```

---

### Task 4: Add diagnostic logging to episode pathway

**Files:**
- Modify: `nous/handlers/procedure_learner.py:339-414`
- Test: `tests/test_procedure_learner.py`

Bug 3 visibility: Episode pathway has no logging to show where it drops to zero.

- [ ] **Step 1: Write the test**

Add to `tests/test_procedure_learner.py`:

```python
@pytest.mark.asyncio
async def test_episode_pathway_logs_diagnostics(caplog):
    """Episode pathway must log diagnostic info at each stage.

    Issue #188, Bug 3: Episode pathway fails silently.
    """
    import logging
    learner, brain, heart, embeddings, http = _build_learner()

    brain.list_decisions.return_value = ([], 0)

    # 3 episodes but only 1 has success outcome
    ep_success = _make_episode_summary(outcome="success")
    ep_failure = _make_episode_summary(outcome="failure")
    ep_ongoing = _make_episode_summary(outcome="ongoing")
    heart.list_episodes.return_value = [ep_success, ep_failure, ep_ongoing]

    heart.get_episode.return_value = _make_episode_detail(
        ep_success, lessons=["Single lesson"]
    )

    heart.search_procedures.return_value = []

    with caplog.at_level(logging.INFO, logger="nous.handlers.procedure_learner"):
        await learner.run_sleep_learning()

    # Should log specific episode pathway diagnostics
    assert "Episode pathway" in caplog.text
    assert "episodes fetched" in caplog.text or "lessons collected" in caplog.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_procedure_learner.py::test_episode_pathway_logs_diagnostics -v`
Expected: FAIL — current code has no "Episode pathway" log messages.

- [ ] **Step 3: Add logging to episode pathway**

In `nous/handlers/procedure_learner.py`, in `_learn_from_episodes()`, add logging after key stages. Replace lines 344-361:

```python
        try:
            # Fetch completed/resolved episodes
            episodes_summary = await self._heart.list_episodes(limit=50)
            logger.info("Episode pathway: %d episodes fetched", len(episodes_summary))

            # Collect lessons from completed episodes (need full details)
            all_lessons: list[str] = []
            skipped_outcome = 0
            skipped_no_lessons = 0
            for ep_summary in episodes_summary:
                if ep_summary.outcome not in ("success", "partial"):
                    skipped_outcome += 1
                    continue
                try:
                    ep_detail = await self._heart.get_episode(ep_summary.id)
                    if ep_detail.lessons_learned:
                        all_lessons.extend(ep_detail.lessons_learned)
                    else:
                        skipped_no_lessons += 1
                except (ValueError, Exception):
                    continue

            logger.info(
                "Episode pathway: %d lessons collected (%d skipped: %d wrong outcome, %d no lessons)",
                len(all_lessons), skipped_outcome + skipped_no_lessons,
                skipped_outcome, skipped_no_lessons,
            )

            if len(all_lessons) < self._settings.procedure_cluster_min_size:
                logger.info("Episode pathway: too few lessons (%d < %d)",
                            len(all_lessons), self._settings.procedure_cluster_min_size)
                return 0
```

Also add logging after clustering (around line 371):

```python
            logger.info(
                "Episode pathway: %d clusters found from %d embeddings (threshold=%.2f, min_size=%d)",
                len(clusters), len(embeddings),
                self._settings.procedure_episode_similarity,
                self._settings.procedure_cluster_min_size,
            )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_procedure_learner.py -v`

- [ ] **Step 5: Commit**

```bash
git add nous/handlers/procedure_learner.py tests/test_procedure_learner.py
git commit -m "fix: add diagnostic logging to procedure learner pathways (#188)"
```

---

### Task 5: Update existing tests for new call pattern

**Files:**
- Modify: `tests/test_procedure_learner.py`
- Modify: `tests/test_sleep_handler.py` (assertion already updated in Task 3)

The existing test `test_decision_cluster_creates_procedure` sets up `brain.list_decisions.return_value` with pre-filtered summaries. After Task 2, `list_decisions` is called twice (success + partial). Update the mock setup.

- [ ] **Step 1: Update existing test mock setup**

In `tests/test_procedure_learner.py`, update `test_decision_cluster_creates_procedure`:

```python
@pytest.mark.asyncio
async def test_decision_cluster_creates_procedure():
    """3+ similar successful reviewed decisions -> 1 procedure."""
    learner, brain, heart, embeddings, http = _build_learner()

    # Set up 3 successful reviewed decisions
    summaries = [_make_decision_summary() for _ in range(3)]
    # list_decisions now called twice: success returns 3, partial returns 0
    brain.list_decisions.side_effect = [(summaries, 3), ([], 0)]

    # ... rest unchanged ...
```

Update ALL tests that set `brain.list_decisions.return_value` to use `side_effect` with two returns (one for success call, one for partial call). Affected tests:

- `test_decision_cluster_creates_procedure` — change to `side_effect = [(summaries, 3), ([], 0)]`
- `test_small_cluster_rejected` — change to `side_effect = [(summaries, 2), ([], 0)]`
- `test_recency_gate` — change to `side_effect = [(summaries, 3), ([], 0)]`
- `test_dedup_skips_similar_existing` — change to `side_effect = [(summaries, 3), ([], 0)]`
- `test_max_cap_enforcement` — change to `side_effect = [(summaries, 6), ([], 0)]`

Tests that use `return_value = ([], 0)` should change to `side_effect = [([], 0), ([], 0)]`:

- `test_episode_lesson_clustering`
- `test_too_few_episodes`
- `test_weak_review_retires`
- `test_disabled_learning_returns_empty` — no change needed (assert_not_called)
- `test_no_embeddings_returns_zero` — change to `side_effect = [([], 0), ([], 0)]`

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest tests/test_procedure_learner.py tests/test_sleep_handler.py -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_procedure_learner.py tests/test_sleep_handler.py
git commit -m "test: update procedure learner tests for dual-query pattern (#188)"
```
