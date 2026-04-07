# Heartbeat Finding Quality Improvements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate false-positive "Pending action" findings from SelfInitiatedCheck and auto-resolve stale findings that are no longer detected.

**Architecture:** Two changes: (1) Tighten the detection heuristics in `SelfInitiatedCheck` — positive patterns required, negative patterns only reject when no positive match (positive wins over negative). (2) Add observation filtering to embedding search (apply `_looks_like_pending` as gate, keep high-score-only path for semantic discovery). (3) Add auto-resolution in `HeartbeatRunner._tick()` — track findings per successful check, resolve ACKNOWLEDGED findings absent for 2+ consecutive ticks.

**Tech Stack:** Python 3.12+, pytest, asyncio

**Review fixes incorporated:** P1-1 (inline test construction, not phantom helper), P1-2 (auto-resolve ACKNOWLEDGED-only), P1-3 (positive-wins-over-negative pattern logic), P1-4 (single-pass auto-resolve after all marks), P2-1 (keep tiered threshold instead of OR→AND), P2-2 (add `pending review` to positive patterns).

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `nous/heartbeat/checks.py` | Modify | Tighten `_looks_like_pending`, add observation filter to embedding search |
| `nous/heartbeat/schemas.py` | Modify | Add `absent_ticks: int` field to `TrackedFinding` |
| `nous/heartbeat/finding_store.py` | Modify | Add `get_active_by_check()`, `mark_absent_tick()`, `get_auto_resolvable()` |
| `nous/heartbeat/runner.py` | Modify | Add auto-resolve logic after successful check runs |
| `tests/test_heartbeat.py` | Modify | Update `_looks_like_pending` tests, add auto-resolve + integration tests |
| `tests/test_heartbeat_lifecycle.py` | Modify | Add auto-resolve tests for FindingStore |
| `tests/test_heartbeat_intelligent.py` | Modify | Add observation rejection test for embedding search |

---

### Task 1: Tighten `_looks_like_pending` heuristic

**Files:**
- Modify: `nous/heartbeat/checks.py:434-439`
- Modify: `tests/test_heartbeat.py:697-708`

- [ ] **Step 1: Write failing tests for false-positive rejection**

Add to `TestSelfInitiatedCheck` in `tests/test_heartbeat.py`:

```python
def test_looks_like_pending_rejects_observations(self):
    """_looks_like_pending rejects observational/descriptive facts."""
    assert SelfInitiatedCheck._looks_like_pending(
        "The team follows a pattern of draft → review → targeted improvements"
    ) is False
    assert SelfInitiatedCheck._looks_like_pending(
        "In general, the process is to review PRs before merging"
    ) is False
    assert SelfInitiatedCheck._looks_like_pending(
        "The system typically handles reconnections automatically"
    ) is False
    assert SelfInitiatedCheck._looks_like_pending(
        "Users need to authenticate before accessing the dashboard"
    ) is False
    assert SelfInitiatedCheck._looks_like_pending(
        "The pending state is used for unreviewed decisions"
    ) is False

def test_looks_like_pending_action_wins_over_observation(self):
    """When content has both action and observation markers, action wins."""
    # "the team" is a negative pattern but "need to review" is positive — positive wins
    assert SelfInitiatedCheck._looks_like_pending(
        "The team needs to review this PR by Friday"
    ) is True
    assert SelfInitiatedCheck._looks_like_pending(
        "The system needs to be restarted after the patch — action needed"
    ) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_heartbeat.py::TestSelfInitiatedCheck::test_looks_like_pending_rejects_observations tests/test_heartbeat.py::TestSelfInitiatedCheck::test_looks_like_pending_action_wins_over_observation -v`
Expected: FAIL — current heuristic matches "pending", "need to" in observation texts

- [ ] **Step 3: Rewrite `_looks_like_pending` with positive-wins logic**

Replace the method in `nous/heartbeat/checks.py`:

```python
@staticmethod
def _looks_like_pending(content: str) -> bool:
    """Detect actionable pending items, rejecting observations/descriptions.

    Uses positive + negative patterns. Positive match is required.
    Negative patterns only reject when no positive match is found.
    """
    lower = content.lower()

    # Positive patterns — action-oriented language (checked first)
    action_patterns = [
        "todo",
        "follow-up on",
        "follow up on",
        "action needed",
        "remind me",
        "i need to",
        "need to finish",
        "need to complete",
        "need to send",
        "need to review",
        "need to check",
        "need to restart",
        "needs to be",
        "should follow up",
        "must complete",
        "waiting for response",
        "hasn't been done",
        "not yet completed",
        "pending review",
        "pending approval",
    ]
    has_action = any(p in lower for p in action_patterns)

    if has_action:
        return True

    # Negative patterns — observational/descriptive language
    # Only checked when no positive match (positive wins)
    reject_patterns = [
        "follows a pattern",
        "in general",
        "typically",
        "the process is",
        "is used for",
        "is designed to",
        "pattern of",
    ]
    has_observation = any(p in lower for p in reject_patterns)
    if has_observation:
        return False

    # No positive match and no negative match — not pending
    return False
```

- [ ] **Step 4: Update existing positive-match tests**

Update `test_looks_like_pending_matches` in `tests/test_heartbeat.py`:

```python
def test_looks_like_pending_matches(self):
    """49. _looks_like_pending detects known action markers."""
    assert SelfInitiatedCheck._looks_like_pending("TODO: check the report") is True
    assert SelfInitiatedCheck._looks_like_pending("I need to follow-up on Tim's request") is True
    assert SelfInitiatedCheck._looks_like_pending("Pending review of the PR") is True
    assert SelfInitiatedCheck._looks_like_pending("remind me about the meeting") is True
    assert SelfInitiatedCheck._looks_like_pending("Action needed on PR") is True
    assert SelfInitiatedCheck._looks_like_pending("waiting for response from vendor") is True
```

- [ ] **Step 5: Update existing no-match tests**

Update `test_looks_like_pending_no_match` in `tests/test_heartbeat.py`:

```python
def test_looks_like_pending_no_match(self):
    """50. _looks_like_pending rejects non-matching content."""
    assert SelfInitiatedCheck._looks_like_pending("The weather is nice today") is False
    assert SelfInitiatedCheck._looks_like_pending("Database schema updated") is False
    assert SelfInitiatedCheck._looks_like_pending("Committed changes to main branch") is False
```

- [ ] **Step 6: Run all SelfInitiatedCheck tests**

Run: `uv run pytest tests/test_heartbeat.py::TestSelfInitiatedCheck -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add nous/heartbeat/checks.py tests/test_heartbeat.py
git commit -m "fix: tighten _looks_like_pending to reject observational facts"
```

---

### Task 2: Add observation filter to embedding search

**Files:**
- Modify: `nous/heartbeat/checks.py:185-231` (`_embedding_search` method)
- Modify: `tests/test_heartbeat_intelligent.py`

The embedding search currently uses `score >= threshold OR _looks_like_pending`. Review found that AND is too aggressive (defeats semantic search). Instead: filter out observations using `_looks_like_pending` as a gate — reject facts that look observational, keep high-score semantic matches.

- [ ] **Step 1: Write failing test for observation rejection**

Add to `TestSelfInitiatedEmbedding` in `tests/test_heartbeat_intelligent.py`. Note: this class constructs mocks inline (no helper method):

```python
@pytest.mark.asyncio
async def test_rejects_high_score_observation(self):
    """Embedding match with high score but observational content is rejected."""
    heart = MagicMock()
    brain = AsyncMock()
    settings = _mock_settings()
    embeddings = AsyncMock()
    embeddings.embed_batch = AsyncMock(return_value=[[0.1] * 1536] * len(PENDING_PROTOTYPES))
    check = SelfInitiatedCheck(heart=heart, brain=brain, settings=settings, embeddings=embeddings)

    fact = MagicMock()
    fact.content = "The team follows a pattern of reviewing PRs before merging"
    fact.id = "fact-obs-1"
    fact.score = 0.85  # Above threshold
    heart.facts.search = AsyncMock(return_value=[fact])
    heart.schedules.get_due = AsyncMock(return_value=[])

    result = await check.run()

    # Should NOT produce findings — observational content rejected
    fact_findings = [f for f in result.findings if f.raw_data.get("fact_id") == "fact-obs-1"]
    assert len(fact_findings) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_heartbeat_intelligent.py::TestSelfInitiatedEmbedding::test_rejects_high_score_observation -v`
Expected: FAIL — current OR logic lets high-score observations through

- [ ] **Step 3: Add observation filter to `_embedding_search`**

In `nous/heartbeat/checks.py`, in `_embedding_search`, replace the condition at line 218-219:

Old:
```python
                    score = getattr(fact, "score", 0.0) or 0.0
                    if score >= threshold or self._looks_like_pending(fact.content):
```

New:
```python
                    score = getattr(fact, "score", 0.0) or 0.0
                    if not self._is_observation(fact.content) and (
                        score >= threshold or self._looks_like_pending(fact.content)
                    ):
```

And add the `_is_observation` helper method:

```python
@staticmethod
def _is_observation(content: str) -> bool:
    """Detect observational/descriptive content that should not be flagged."""
    lower = content.lower()
    observation_patterns = [
        "follows a pattern",
        "in general",
        "typically",
        "the process is",
        "is used for",
        "is designed to",
        "pattern of",
    ]
    return any(p in lower for p in observation_patterns)
```

- [ ] **Step 4: Run all embedding tests**

Run: `uv run pytest tests/test_heartbeat_intelligent.py::TestSelfInitiatedEmbedding -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add nous/heartbeat/checks.py tests/test_heartbeat_intelligent.py
git commit -m "fix: filter observations from embedding search results"
```

---

### Task 3: Add `absent_ticks` to TrackedFinding and FindingStore helpers

**Files:**
- Modify: `nous/heartbeat/schemas.py:113-127`
- Modify: `nous/heartbeat/finding_store.py`
- Modify: `tests/test_heartbeat_lifecycle.py`

- [ ] **Step 1: Write failing tests for new FindingStore methods**

Add to `tests/test_heartbeat_lifecycle.py`:

```python
class TestFindingStoreAutoResolve:
    """Tests for auto-resolution of stale findings."""

    def test_get_active_by_check_returns_matching(self):
        """get_active_by_check returns fingerprints for a specific check."""
        store = _make_store()
        f1 = _make_finding(summary="Issue A", check_name="health")
        f2 = _make_finding(summary="Issue B", check_name="self_initiated")
        f3 = _make_finding(summary="Issue C", check_name="health")
        store.ingest(f1)
        store.ingest(f2)
        store.ingest(f3)

        health_fps = store.get_active_by_check("health")
        assert len(health_fps) == 2
        assert f1.fingerprint() in health_fps
        assert f3.fingerprint() in health_fps

    def test_get_active_by_check_excludes_resolved(self):
        """get_active_by_check excludes resolved findings."""
        store = _make_store()
        f = _make_finding(summary="Resolved issue", check_name="health")
        store.ingest(f)
        store.resolve(f.fingerprint())

        assert len(store.get_active_by_check("health")) == 0

    def test_mark_absent_tick_increments_counter(self):
        """mark_absent_tick increments absent_ticks on tracked finding."""
        store = _make_store()
        f = _make_finding(summary="Disappearing issue", check_name="health")
        store.ingest(f)
        fp = f.fingerprint()

        store.mark_absent_tick(fp)
        assert store._findings[fp].absent_ticks == 1

        store.mark_absent_tick(fp)
        assert store._findings[fp].absent_ticks == 2

    def test_mark_absent_tick_resets_on_reingest(self):
        """Re-ingesting a finding resets absent_ticks to 0."""
        store = _make_store()
        f = _make_finding(summary="Intermittent issue", check_name="health")
        store.ingest(f)
        fp = f.fingerprint()

        store.mark_absent_tick(fp)
        store.mark_absent_tick(fp)
        assert store._findings[fp].absent_ticks == 2

        # Re-ingest same finding — absent_ticks should reset
        store.ingest(f)
        assert store._findings[fp].absent_ticks == 0

    def test_get_auto_resolvable_acknowledged_only(self):
        """get_auto_resolvable only returns ACKNOWLEDGED findings."""
        store = _make_store()

        # NEW finding with absent_ticks >= 2 — should NOT be auto-resolvable
        f_new = _make_finding(summary="New issue", check_name="health")
        store.ingest(f_new)
        fp_new = f_new.fingerprint()
        store.mark_absent_tick(fp_new)
        store.mark_absent_tick(fp_new)

        # ACKNOWLEDGED finding with absent_ticks >= 2 — SHOULD be auto-resolvable
        f_ack = _make_finding(summary="Acked issue", check_name="health")
        store.ingest(f_ack)
        fp_ack = f_ack.fingerprint()
        store.acknowledge(fp_ack)
        store.mark_absent_tick(fp_ack)
        store.mark_absent_tick(fp_ack)

        resolvable = store.get_auto_resolvable(threshold=2)
        assert fp_new not in resolvable
        assert fp_ack in resolvable

    def test_auto_resolve_after_threshold(self):
        """Findings absent for >= threshold ticks get auto-resolved."""
        store = _make_store()
        f = _make_finding(summary="Gone issue", check_name="health")
        store.ingest(f)
        fp = f.fingerprint()
        store.acknowledge(fp)

        store.mark_absent_tick(fp)
        resolvable = store.get_auto_resolvable(threshold=2)
        assert fp not in resolvable  # Only 1 absent tick, threshold is 2

        store.mark_absent_tick(fp)
        resolvable = store.get_auto_resolvable(threshold=2)
        assert fp in resolvable  # Now at threshold
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_heartbeat_lifecycle.py::TestFindingStoreAutoResolve -v`
Expected: FAIL — methods don't exist yet

- [ ] **Step 3: Add `absent_ticks` field to TrackedFinding**

In `nous/heartbeat/schemas.py`, add to `TrackedFinding` after `last_escalated_at`:

```python
    absent_ticks: int = 0  # consecutive ticks where check ran but didn't report this finding
```

- [ ] **Step 4: Add FindingStore methods**

In `nous/heartbeat/finding_store.py`, add these methods to `FindingStore`:

```python
    def get_active_by_check(self, check_name: str) -> set[str]:
        """Return fingerprints of active (non-resolved) findings for a check."""
        active = set()
        for fp, tracked in self._findings.items():
            if tracked.state == FindingState.RESOLVED:
                continue
            if tracked.finding.check_name == check_name:
                active.add(fp)
        return active

    def mark_absent_tick(self, fingerprint: str) -> None:
        """Increment absent_ticks counter for a finding not reported this tick."""
        if fingerprint in self._findings:
            self._findings[fingerprint].absent_ticks += 1

    def get_auto_resolvable(self, threshold: int = 2) -> set[str]:
        """Return fingerprints of ACKNOWLEDGED findings absent for >= threshold ticks.

        Only ACKNOWLEDGED findings are eligible — NEW findings haven't been
        triaged yet (auto-resolving them would silently drop issues), and
        SUPPRESSED findings were never actionable.
        """
        resolvable = set()
        for fp, tracked in self._findings.items():
            if tracked.state != FindingState.ACKNOWLEDGED:
                continue
            if tracked.absent_ticks >= threshold:
                resolvable.add(fp)
        return resolvable
```

Also reset `absent_ticks` in `ingest()` when a finding is re-seen. Add `existing.absent_ticks = 0` right after `existing.last_seen = datetime.now(UTC)` (around line 66 of finding_store.py):

```python
        if fp in self._findings:
            existing = self._findings[fp]
            existing.last_seen = datetime.now(UTC)
            existing.absent_ticks = 0  # Reset — finding is still active
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_heartbeat_lifecycle.py::TestFindingStoreAutoResolve -v`
Expected: ALL PASS

- [ ] **Step 6: Run all lifecycle tests**

Run: `uv run pytest tests/test_heartbeat_lifecycle.py -v`
Expected: ALL PASS (no regressions)

- [ ] **Step 7: Commit**

```bash
git add nous/heartbeat/schemas.py nous/heartbeat/finding_store.py tests/test_heartbeat_lifecycle.py
git commit -m "feat: add absent_ticks tracking and auto-resolve helpers to FindingStore"
```

---

### Task 4: Wire auto-resolve into HeartbeatRunner._tick()

**Files:**
- Modify: `nous/heartbeat/runner.py:180-264`
- Modify: `tests/test_heartbeat.py`

- [ ] **Step 1: Write failing test for runner auto-resolve**

Add a new test class to `tests/test_heartbeat.py`:

```python
class TestRunnerAutoResolve:
    """Runner auto-resolves findings when checks stop reporting them."""

    @pytest.mark.asyncio
    async def test_auto_resolves_after_grace_period(self):
        """Findings not reported for 2 consecutive successful ticks get auto-resolved."""
        from nous.heartbeat.finding_store import FindingStore
        from nous.heartbeat.schemas import FindingState

        settings = _mock_settings()
        registry = CheckRegistry()
        store = FindingStore()

        runner = HeartbeatRunner(
            settings=settings,
            registry=registry,
            runner=MagicMock(),
            brain=AsyncMock(),
            heart=AsyncMock(),
            bus=MagicMock(),
            http_client=MagicMock(),
            finding_store=store,
        )

        f = Finding(
            source="facts",
            summary="Pending action: TODO fix the thing",
            urgency="normal",
            needs_action=True,
            check_name="self_initiated",
        )
        store.ingest(f)
        store.acknowledge(f.fingerprint())

        # Tick 1: absent — not yet resolved
        runner._auto_resolve_absent_findings(
            successful_checks={"self_initiated"},
            current_fingerprints={"self_initiated": set()},
        )
        assert store._findings[f.fingerprint()].state != FindingState.RESOLVED

        # Tick 2: still absent — now resolved
        runner._auto_resolve_absent_findings(
            successful_checks={"self_initiated"},
            current_fingerprints={"self_initiated": set()},
        )
        assert store._findings[f.fingerprint()].state == FindingState.RESOLVED

    @pytest.mark.asyncio
    async def test_no_auto_resolve_on_failed_check(self):
        """Failed checks don't trigger auto-resolve."""
        from nous.heartbeat.finding_store import FindingStore
        from nous.heartbeat.schemas import FindingState

        settings = _mock_settings()
        registry = CheckRegistry()
        store = FindingStore()

        runner = HeartbeatRunner(
            settings=settings,
            registry=registry,
            runner=MagicMock(),
            brain=AsyncMock(),
            heart=AsyncMock(),
            bus=MagicMock(),
            http_client=MagicMock(),
            finding_store=store,
        )

        f = Finding(
            source="facts",
            summary="Pending action: TODO fix it",
            urgency="normal",
            needs_action=True,
            check_name="self_initiated",
        )
        store.ingest(f)
        store.acknowledge(f.fingerprint())

        # self_initiated NOT in successful_checks (failed/timed out)
        runner._auto_resolve_absent_findings(
            successful_checks=set(),
            current_fingerprints={},
        )
        runner._auto_resolve_absent_findings(
            successful_checks=set(),
            current_fingerprints={},
        )
        assert store._findings[f.fingerprint()].state != FindingState.RESOLVED

    @pytest.mark.asyncio
    async def test_no_auto_resolve_for_new_findings(self):
        """NEW (un-triaged) findings are not auto-resolved."""
        from nous.heartbeat.finding_store import FindingStore
        from nous.heartbeat.schemas import FindingState

        settings = _mock_settings()
        registry = CheckRegistry()
        store = FindingStore()

        runner = HeartbeatRunner(
            settings=settings,
            registry=registry,
            runner=MagicMock(),
            brain=AsyncMock(),
            heart=AsyncMock(),
            bus=MagicMock(),
            http_client=MagicMock(),
            finding_store=store,
        )

        f = Finding(
            source="facts",
            summary="Pending action: TODO new thing",
            urgency="normal",
            needs_action=True,
            check_name="self_initiated",
        )
        store.ingest(f)
        # Do NOT acknowledge — stays in NEW state

        runner._auto_resolve_absent_findings(
            successful_checks={"self_initiated"},
            current_fingerprints={"self_initiated": set()},
        )
        runner._auto_resolve_absent_findings(
            successful_checks={"self_initiated"},
            current_fingerprints={"self_initiated": set()},
        )
        # NEW finding should NOT be resolved
        assert store._findings[f.fingerprint()].state != FindingState.RESOLVED
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_heartbeat.py::TestRunnerAutoResolve -v`
Expected: FAIL — `_auto_resolve_absent_findings` doesn't exist

- [ ] **Step 3: Add `_auto_resolve_absent_findings` method to HeartbeatRunner**

In `nous/heartbeat/runner.py`, add this method to `HeartbeatRunner`:

```python
    def _auto_resolve_absent_findings(
        self,
        successful_checks: set[str],
        current_fingerprints: dict[str, set[str]],
        threshold: int = 2,
    ) -> None:
        """Auto-resolve ACKNOWLEDGED findings no longer reported by successful checks.

        For each check that ran successfully this tick, compare its current
        findings against tracked findings. Mark absent findings, then resolve
        any ACKNOWLEDGED findings absent for >= threshold consecutive ticks.
        """
        if self._finding_store is None:
            return

        # Phase 1: Mark absent findings for all successful checks
        all_absent: dict[str, set[str]] = {}
        for check_name in successful_checks:
            active_fps = self._finding_store.get_active_by_check(check_name)
            current_fps = current_fingerprints.get(check_name, set())
            absent_fps = active_fps - current_fps
            all_absent[check_name] = absent_fps

            for fp in absent_fps:
                self._finding_store.mark_absent_tick(fp)

        # Phase 2: Single pass — resolve ACKNOWLEDGED findings past threshold
        resolvable = self._finding_store.get_auto_resolvable(threshold=threshold)
        for fp in resolvable:
            # Only resolve if the finding's check ran successfully this tick
            tracked = self._finding_store._findings.get(fp)
            if tracked and tracked.finding.check_name in successful_checks:
                self._finding_store.resolve(fp)
                logger.info("Auto-resolved finding %s (absent for %d+ ticks)", fp, threshold)
```

- [ ] **Step 4: Wire into `_tick()`**

In `nous/heartbeat/runner.py`, modify `_tick()`:

**Before the check loop** (before line 200 `for check in due_checks:`), add:

```python
        successful_checks: set[str] = set()
        current_fingerprints: dict[str, set[str]] = {}
```

**Inside the check loop**, after `check.mark_success()` (line 206), add:

```python
                successful_checks.add(check.name)
```

**Inside the check loop**, after `all_findings.extend(result.findings)` (line 224), add:

```python
                    for f in result.findings:
                        current_fingerprints.setdefault(check.name, set()).add(f.fingerprint())
```

**After the check loop**, after `self._last_tick = now` (line 249), add:

```python
        # Auto-resolve findings no longer reported by successful checks
        self._auto_resolve_absent_findings(successful_checks, current_fingerprints)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_heartbeat.py::TestRunnerAutoResolve -v`
Expected: ALL PASS

- [ ] **Step 6: Run full heartbeat test suite**

Run: `uv run pytest tests/test_heartbeat.py tests/test_heartbeat_lifecycle.py tests/test_heartbeat_intelligent.py tests/test_heartbeat_dynamic.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add nous/heartbeat/runner.py tests/test_heartbeat.py
git commit -m "feat: auto-resolve findings absent for 2+ consecutive successful ticks"
```

---

### Task 5: Integration test — full cycle

**Files:**
- Modify: `tests/test_heartbeat.py`

- [ ] **Step 1: Write integration test for full finding lifecycle**

Add to `tests/test_heartbeat.py`:

```python
class TestFindingQualityIntegration:
    """End-to-end: detection → triage → auto-resolve lifecycle."""

    @pytest.mark.asyncio
    async def test_observation_fact_never_becomes_finding(self):
        """An observational fact is not flagged as pending even with keyword match."""
        heart = MagicMock()
        brain = AsyncMock()
        settings = _mock_settings()
        check = SelfInitiatedCheck(heart=heart, brain=brain, settings=settings)

        fact = MagicMock()
        fact.content = "The pending state is used for decisions awaiting review"
        fact.id = "fact-obs"
        fact.score = 0.5

        heart.facts.search = AsyncMock(return_value=[fact])
        heart.schedules.get_due = AsyncMock(return_value=[])

        result = await check.run()
        fact_findings = [f for f in result.findings if f.raw_data.get("fact_id") == "fact-obs"]
        assert len(fact_findings) == 0

    @pytest.mark.asyncio
    async def test_action_fact_detected_and_auto_resolved(self):
        """An actionable fact is detected, then auto-resolved when no longer reported."""
        from nous.heartbeat.finding_store import FindingStore
        from nous.heartbeat.schemas import FindingState

        heart = MagicMock()
        brain = AsyncMock()
        settings = _mock_settings()
        check = SelfInitiatedCheck(heart=heart, brain=brain, settings=settings)

        # First run: actionable fact present
        fact = MagicMock()
        fact.content = "TODO: follow-up on deployment issue"
        fact.id = "fact-action"
        fact.score = 0.5
        heart.facts.search = AsyncMock(return_value=[fact])
        heart.schedules.get_due = AsyncMock(return_value=[])

        result = await check.run()
        action_findings = [f for f in result.findings if f.raw_data.get("fact_id") == "fact-action"]
        assert len(action_findings) == 1

        # Simulate FindingStore tracking
        store = FindingStore()
        for f in result.findings:
            f.check_name = check.name
            store.ingest(f)
            store.acknowledge(f.fingerprint())

        # Second run: fact gone
        heart.facts.search = AsyncMock(return_value=[])
        result2 = await check.run()
        assert result2.has_updates is False

        # Simulate auto-resolve (2 absent ticks)
        for _ in range(2):
            for fp in store.get_active_by_check("self_initiated"):
                store.mark_absent_tick(fp)

        resolvable = store.get_auto_resolvable(threshold=2)
        for fp in resolvable:
            store.resolve(fp)

        assert len(store.get_active_by_check("self_initiated")) == 0
```

- [ ] **Step 2: Run integration tests**

Run: `uv run pytest tests/test_heartbeat.py::TestFindingQualityIntegration -v`
Expected: ALL PASS

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/test_heartbeat.py tests/test_heartbeat_lifecycle.py tests/test_heartbeat_intelligent.py tests/test_heartbeat_dynamic.py -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_heartbeat.py
git commit -m "test: add integration tests for finding quality improvements"
```
