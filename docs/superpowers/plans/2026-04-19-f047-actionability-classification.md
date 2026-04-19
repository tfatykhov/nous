# Implementation Plan: F047 Actionability Classification

**Feature spec:** `docs/features/F047-actionability-classification.md`
**Date:** 2026-04-19
**Decision ID:** `8a8f98ac`
**Branch:** `feat/F047-actionability-classification`
**Estimated size:** ~700 LOC prod + ~400 LOC tests

---

## Architecture (recap)

Replace `_is_observation` read-time substring matching with a learn-time classifier that persists `actionable: bool | None` + `actionable_confidence: float | None` on `heart.facts`. Heartbeat becomes a SQL filter; legacy `_OBSERVATION_PATTERNS` stays as the Tier-0 heuristic *inside* the classifier (reused, not deleted) and as the fallback path for NULL rows.

---

## Dependency graph

```
[1] schema migration ───┐
[2] ORM + FactSummary ──┼─► [4] classifier unit tests ────┐
[3] classifier module ──┘                                  │
                                                           ├─► [6] FactManager integration ─► [8] heartbeat wiring ─► [10] integration tests
[5] settings + config defaults ────────────────────────────┘                                                              │
                                                                                                                          │
[7] backfill handler ◄──────────────────────────────────── depends on [3],[6]                                             │
[9] startup hook for backfill ◄────────────────────────── depends on [7]                                                  │
                                                                                                                          ▼
                                                                                                                 [11] CLAUDE.md + INDEX.md updates
                                                                                                                          │
                                                                                                                          ▼
                                                                                                                 [12] review cycle + PR
```

Steps 1–5 are leaves (can parallelize). Steps 6, 7, 8 serialize. Step 10 (tests) can start after 6.

---

## Step-by-step

### Step 1 — Schema migration

**File:** `sql/migrations/034_fact_actionability.sql` (new)

```sql
ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS actionable BOOLEAN DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS actionable_confidence REAL DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_facts_actionable_agent
    ON heart.facts(agent_id, actionable)
    WHERE actionable = TRUE;

COMMENT ON COLUMN heart.facts.actionable IS
    'F047: True=pending action, False=observation/resolved, NULL=unclassified';
COMMENT ON COLUMN heart.facts.actionable_confidence IS
    'F047: Classifier confidence 0.0-1.0';
```

**SQLite parity:** `tests/conftest_postgres.py` already strips PG-specific syntax. `ADD COLUMN IF NOT EXISTS` works in SQLite 3.35+. Partial indexes require translation — add a shim in the test conftest if the existing approach doesn't cover it.

**Verify:**
```bash
docker compose up -d postgres
docker compose exec postgres psql -U nous -d nous -c "\d heart.facts" | grep actionable
```

### Step 2 — ORM model update

**File:** `nous/storage/models.py` (`Fact` class ~line 376)

```python
class Fact(Base):
    ...
    actionable: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    actionable_confidence: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
```

**File:** `nous/heart/schemas.py` — add to `FactDetail` and `FactSummary`:

```python
class FactSummary(BaseModel):
    ...
    actionable: bool | None = None
    actionable_confidence: float | None = None
```

**Verify:** `uv run python -c "from nous.storage.models import Fact; print(Fact.actionable)"` prints a column descriptor.

### Step 3 — Classifier module

**File:** `nous/heart/actionability.py` (new, ~150 LOC)

```python
"""F047: Actionability classification for facts.

Tiered classifier decides whether a fact represents a pending action
or an observation/resolved statement. Verdict is stored on heart.facts
at learn time to avoid the read-time substring arms race.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nous.handlers import LLMClient

logger = logging.getLogger(__name__)


_ACTION_PATTERNS: tuple[str, ...] = (
    "todo", "i need to", "action needed", "remind me",
    "follow-up on", "follow up on", "needs to",
    "should follow up", "must complete", "waiting for response",
    "hasn't been done", "not yet completed", "pending review",
    "pending approval",
)


def _get_observation_patterns() -> tuple[str, ...]:
    """Lazy-import to avoid circular deps (checks.py imports Heart, etc)."""
    from nous.heartbeat.checks import _OBSERVATION_PATTERNS
    return tuple(_OBSERVATION_PATTERNS)


_LLM_CLASSIFIER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "actionable": {
            "type": "boolean",
            "description": "True if this fact describes a pending task requiring action",
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reason": {"type": "string", "description": "One-line rationale"},
    },
    "required": ["actionable", "confidence", "reason"],
}


class ActionabilityClassifier:
    """Classify fact actionability via tiered heuristics + optional LLM."""

    _HARD_NO_CATEGORIES: frozenset[str] = frozenset({"person", "preference"})
    _HARD_NO_TAGS: frozenset[str] = frozenset({"resolved", "identity"})

    def __init__(
        self,
        llm: "LLMClient | None" = None,
        model: str = "claude-haiku-4-5-20251001",
        budget_check: Callable[[], bool] | None = None,
        default_when_unknown: bool = False,
    ) -> None:
        self._llm = llm
        self._model = model
        self._budget_check = budget_check
        self._default_when_unknown = default_when_unknown

    async def classify(
        self,
        content: str,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> tuple[bool, float, str]:
        """Return (actionable, confidence, tier) for a fact.

        tier is: hard_filter | heuristic_action | heuristic_observation | llm | default
        """
        # Tier 0: category/tag hard filters
        if category in self._HARD_NO_CATEGORIES:
            return (False, 1.0, "hard_filter")
        if tags and {t.lower() for t in tags} & self._HARD_NO_TAGS:
            return (False, 1.0, "hard_filter")

        lower = content.lower()
        has_action = any(p in lower for p in _ACTION_PATTERNS)
        has_observation = any(p in lower for p in _get_observation_patterns())

        # Tier 1: unambiguous heuristic match (positive-wins-over-negative)
        if has_action and not has_observation:
            return (True, 0.85, "heuristic_action")
        if has_observation and not has_action:
            return (False, 0.85, "heuristic_observation")

        # Tier 2: LLM disambiguation (both match or neither match)
        if self._llm and (self._budget_check is None or self._budget_check()):
            try:
                return await self._llm_classify(content, category, tags)
            except Exception:
                logger.debug("F047: LLM classify failed", exc_info=True)

        # Default: fail-closed
        return (self._default_when_unknown, 0.3, "default")

    async def _llm_classify(
        self,
        content: str,
        category: str | None,
        tags: list[str] | None,
    ) -> tuple[bool, float, str]:
        from nous.handlers import call_background_llm_structured

        prompt = (
            "Classify whether this fact describes a PENDING ACTION requiring "
            "future work, or an OBSERVATION/DESCRIPTION/RESOLVED statement.\n\n"
            f"Content: {content[:500]}\n"
            f"Category: {category or '<none>'}\n"
            f"Tags: {', '.join(tags or []) or '<none>'}"
        )
        result = await call_background_llm_structured(
            client=self._llm,
            model=self._model,
            system_prompt="You classify whether a fact is a pending action or an observation.",
            user_message=prompt,
            tool_name="classify_actionability",
            tool_description="Classify fact as actionable or not.",
            output_schema=_LLM_CLASSIFIER_SCHEMA,
            max_tokens=200,
        )
        if not result:
            return (self._default_when_unknown, 0.3, "default")
        return (
            bool(result.get("actionable", False)),
            float(result.get("confidence", 0.5)),
            "llm",
        )
```

**Verify:** unit tests in Step 4.

### Step 4 — Classifier unit tests

**File:** `tests/test_actionability.py` (new)

```python
import pytest
from nous.heart.actionability import ActionabilityClassifier

class TestHardFilter:
    @pytest.mark.asyncio
    async def test_person_category(self):
        c = ActionabilityClassifier()
        a, conf, tier = await c.classify("Tim's email", category="person")
        assert a is False and conf == 1.0 and tier == "hard_filter"

    @pytest.mark.asyncio
    async def test_resolved_tag_case_insensitive(self):
        c = ActionabilityClassifier()
        for tag in ["resolved", "Resolved", "RESOLVED"]:
            a, _, tier = await c.classify("foo", tags=[tag])
            assert a is False and tier == "hard_filter"

class TestHeuristicTier:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("content", [
        "I need to add idempotency and side-effect tests before merge",
        "TODO: handle timeouts, not an edge case anymore",
        "I need to update worker to should treat timeouts as retryable",
        "I need to review PR #231 before Monday",
        "Need to rebase branch feat/F040-densification",
    ])
    async def test_action_wins_over_observation_substring(self, content):
        """Codex P1 + 4 PR #335 review false-negatives must classify as actionable."""
        c = ActionabilityClassifier()
        a, conf, tier = await c.classify(content)
        assert a is True, f"{content!r} should be actionable"
        assert conf >= 0.8
        assert tier == "heuristic_action"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("content", [
        "The architecture should treat timeouts as a fundamental design constraint, not an edge case",
        "Idempotency and side-effect verification are required for reliable pipelines",
        "Renumbered from F032 (already used for Execution Ledger Dashboard)",
        "Check-type nodes never get that command executed because only subtask nodes transition",
    ])
    async def test_observation_without_action_classified_false(self, content):
        c = ActionabilityClassifier()
        a, conf, tier = await c.classify(content)
        assert a is False
        assert tier == "heuristic_observation"

class TestLLMTier:
    @pytest.mark.asyncio
    async def test_ambiguous_falls_to_llm_when_enabled(self):
        # Both patterns match → LLM decides
        class FakeLLM: pass
        calls = []
        async def fake_call(**kw):
            calls.append(kw); return {"actionable": True, "confidence": 0.7, "reason": "explicit todo"}
        import nous.heart.actionability as mod
        orig = mod.call_background_llm_structured if hasattr(mod, "call_background_llm_structured") else None
        # Patch at the import site used by _llm_classify
        import nous.handlers as handlers
        handlers.call_background_llm_structured = fake_call
        try:
            c = ActionabilityClassifier(llm=FakeLLM())
            # "todo" (action) AND "is resolved" (observation) both match
            a, conf, tier = await c.classify("TODO this bug is resolved by tomorrow")
            assert tier == "llm" and conf == 0.7 and a is True
        finally:
            if orig: handlers.call_background_llm_structured = orig

    @pytest.mark.asyncio
    async def test_no_llm_no_heuristic_returns_default(self):
        c = ActionabilityClassifier(llm=None, default_when_unknown=False)
        # "banana" matches nothing
        a, conf, tier = await c.classify("banana")
        assert a is False and tier == "default" and conf == 0.3

class TestBudgetGate:
    @pytest.mark.asyncio
    async def test_budget_exhausted_skips_llm(self):
        class FakeLLM: pass
        c = ActionabilityClassifier(llm=FakeLLM(), budget_check=lambda: False)
        a, _, tier = await c.classify("TODO this is resolved")  # ambiguous
        assert tier == "default"
```

**Verify:** `uv run pytest tests/test_actionability.py -v` → all pass.

### Step 5 — Settings

**File:** `nous/config.py` (append to existing Settings class, near other heartbeat/F034 settings)

```python
# F047: Actionability classification
actionability_enabled: bool = Field(
    True, validation_alias="NOUS_ACTIONABILITY_ENABLED",
    description="Enable F047 actionability classification at fact learn time",
)
actionability_llm_enabled: bool = Field(
    True, validation_alias="NOUS_ACTIONABILITY_LLM_ENABLED",
    description="Use LLM (Haiku) for ambiguous actionability cases",
)
actionability_model: str = Field(
    "claude-haiku-4-5-20251001", validation_alias="NOUS_ACTIONABILITY_MODEL",
    description="LLM model for actionability tier-2 classification",
)
actionability_default: bool = Field(
    False, validation_alias="NOUS_ACTIONABILITY_DEFAULT",
    description="Fallback verdict when classifier can't decide (False = fail-closed)",
)
actionability_backfill_on_startup: bool = Field(
    True, validation_alias="NOUS_ACTIONABILITY_BACKFILL_ON_STARTUP",
    description="Run backfill automatically on startup if NULL rows exist",
)
```

**Verify:** `uv run python -c "from nous.config import Settings; s = Settings(); print(s.actionability_enabled)"` → True.

### Step 6 — Wire classifier into FactManager

**File:** `nous/heart/facts.py`

1. Add to `__init__`:
```python
def __init__(
    self,
    db: Database,
    embeddings: EmbeddingProvider | None,
    agent_id: str,
    admission_controller: AdmissionController | None = None,
    actionability_classifier: "ActionabilityClassifier | None" = None,  # NEW
) -> None:
    ...
    self._actionability_classifier = actionability_classifier
```

2. In `_learn` (~line 360, right before `fact = Fact(...)`):
```python
# F047: Classify actionability at learn time
actionable: bool | None = None
actionable_conf: float | None = None
if self._actionability_classifier is not None:
    try:
        actionable, actionable_conf, _tier = await self._actionability_classifier.classify(
            input.content, input.category, input.tags or [],
        )
    except Exception:
        logger.warning("F047: actionability classifier failed for new fact", exc_info=True)

fact = Fact(
    ...,
    actionable=actionable,
    actionable_confidence=actionable_conf,
)
```

3. In `search()` signature and both `_search` / `_search_all`: add `actionable: bool | None = None` kwarg; if set, append `AND t.actionable = :actionable` to `extra_where`.

4. In `_to_detail` and `FactSummary` construction sites: copy `fact.actionable` and `fact.actionable_confidence`.

**Wire in `main.py`:** construct classifier at startup alongside `AdmissionController`. Pass to `FactManager(..., actionability_classifier=...)` if `settings.actionability_enabled` else None.

**Verify:** `uv run pytest tests/test_facts.py -v` (existing) — all pass. Then run a new test seeding a fact with actionable populated.

### Step 7 — Backfill handler

**File:** `nous/handlers/actionability_backfill.py` (new, ~120 LOC)

```python
class ActionabilityBackfillHandler:
    BATCH_SIZE = 100
    RATE_LIMIT_DELAY_S = 0.5

    def __init__(self, db, classifier, agent_id):
        self.db = db
        self.classifier = classifier
        self.agent_id = agent_id
        self._running = False

    async def run_once(self) -> dict:
        if self._running:
            logger.info("F047: backfill already running, skipping")
            return {"skipped": True}
        self._running = True
        try:
            return await self._run()
        finally:
            self._running = False

    async def _run(self) -> dict:
        total, classified, errors = 0, 0, 0
        start = time.monotonic()
        while True:
            batch = await self._fetch_batch()
            if not batch:
                break
            for fact_id, content, category, tags in batch:
                try:
                    a, c, _ = await self.classifier.classify(content, category, tags)
                    await self._update(fact_id, a, c)
                    classified += 1
                except Exception:
                    errors += 1
                total += 1
            await asyncio.sleep(self.RATE_LIMIT_DELAY_S)
        elapsed = time.monotonic() - start
        logger.info("F047 backfill: %d facts, %d classified, %d errors in %.1fs",
                    total, classified, errors, elapsed)
        return {"total": total, "classified": classified, "errors": errors, "elapsed_s": elapsed}

    async def _fetch_batch(self):
        async with self.db.session() as session:
            result = await session.execute(text("""
                SELECT id, content, category, tags FROM heart.facts
                WHERE agent_id = :aid AND actionable IS NULL AND active = true
                LIMIT :lim
            """), {"aid": self.agent_id, "lim": self.BATCH_SIZE})
            return [(r.id, r.content, r.category, r.tags or []) for r in result.all()]

    async def _update(self, fact_id, actionable, confidence):
        async with self.db.session() as session:
            await session.execute(text("""
                UPDATE heart.facts SET actionable = :a, actionable_confidence = :c
                WHERE id = :id
            """), {"a": actionable, "c": confidence, "id": fact_id})
            await session.commit()
```

### Step 8 — Heartbeat wiring

**File:** `nous/heartbeat/checks.py:241-296` (`SelfInitiatedCheck._embedding_search`)

Replace the short-circuit at line 281:

```python
# F047: Prefer persisted verdict. Fall back to legacy path for NULL (unclassified) rows.
actionable = getattr(fact, "actionable", None)
if actionable is True:
    is_pending = True
elif actionable is False:
    is_pending = False
else:
    # Legacy fallback — verdict not yet populated (pre-backfill row)
    is_pending = (not self._is_observation(fact.content)) and (
        score >= threshold or self._looks_like_pending(fact.content)
    )

if is_pending:
    findings.append(Finding(...))
```

Do **not** add `actionable=True` to the `fact.search()` call — let the Python branch own the decision, so legacy-path behavior is preserved even for new code paths that use search without filtering.

### Step 9 — Startup backfill hook

**File:** `nous/main.py`

After FactManager is constructed, add:

```python
if settings.actionability_enabled and settings.actionability_backfill_on_startup:
    backfill = ActionabilityBackfillHandler(db, classifier, settings.agent_id)
    asyncio.create_task(backfill.run_once())  # fire-and-forget
```

### Step 10 — Integration tests

**File:** `tests/test_heartbeat_actionability_integration.py` (new)

```python
class TestHeartbeatActionableFilter:
    """F047: heartbeat uses persisted actionable flag."""

    @pytest.mark.asyncio
    async def test_actionable_true_surfaces_finding(self, heart, classifier):
        # seed fact with actionable=True
        ...
        result = await check.run()
        assert any(f.source == "facts" for f in result.findings)

    @pytest.mark.asyncio
    async def test_actionable_false_suppresses_finding(self, heart):
        # seed fact with actionable=False, matching action pattern
        # (simulates Codex P1: "I need to X" but classifier said False)
        ...
        result = await check.run()
        assert not any(f.source == "facts" for f in result.findings)

    @pytest.mark.asyncio
    async def test_actionable_null_falls_back_to_legacy(self, heart):
        # seed fact with actionable=None and obvious action phrasing
        ...
        result = await check.run()
        # legacy _looks_like_pending should catch it
        assert any(f.source == "facts" for f in result.findings)
```

**File:** `tests/test_actionability_backfill.py` (new)

```python
class TestBackfill:
    @pytest.mark.asyncio
    async def test_backfill_processes_null_rows(self, db, classifier):
        # seed 5 facts with actionable=NULL
        handler = ActionabilityBackfillHandler(db, classifier, agent_id)
        result = await handler.run_once()
        assert result["total"] == 5
        assert result["classified"] == 5
        # verify DB rows populated
        ...

    @pytest.mark.asyncio
    async def test_backfill_idempotent(self, db, classifier):
        # run twice, second run processes 0 (already classified)
        ...
        r2 = await handler.run_once()
        assert r2["total"] == 0
```

**Migration test:** `tests/test_migrations.py` — assert columns + index present after migration.

### Step 11 — Docs updates

**File:** `CLAUDE.md` — update:
1. Features list table: add F047 row under "What's Shipped (v0.1.0)".
2. Environment Variables table: add the 5 `NOUS_ACTIONABILITY_*` vars.

**File:** `docs/features/INDEX.md` — add F047 row.

### Step 12 — Review cycle + PR

1. Dispatch 3-agent review (architecture, implementation, devil's advocate) on the completed code.
2. Fix P1/P2 inline.
3. Create branch `feat/F047-actionability-classification`.
4. Commit with message referencing this plan + F047 spec + PR #335 supersession.
5. Open PR.
6. Mark F047 as Shipped in spec and INDEX.md after PR is created.

---

## Test plan checklist

- [ ] `uv run pytest tests/test_actionability.py -v` passes (unit)
- [ ] `uv run pytest tests/test_actionability_backfill.py -v` passes (integration)
- [ ] `uv run pytest tests/test_heartbeat_actionability_integration.py -v` passes (integration)
- [ ] `uv run pytest tests/test_heartbeat_intelligent.py -v` **still** passes (regression — legacy path intact)
- [ ] `uv run pytest tests/test_facts.py -v` **still** passes (regression)
- [ ] `uv run pytest tests/test_migrations.py -v` passes (schema)

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Existing tests break if FactSummary gains a required field | Make `actionable` and `actionable_confidence` `Optional[... ] = None` — purely additive |
| LLM tier 2 blows up latency on learn path | Keep `default_when_unknown=False` + budget_check; LLM only on ambiguous (~20%) |
| Backfill runs at startup in prod and hammers LLM | Rate-limit (0.5s between batches), fire-and-forget, idempotent |
| Partial index breaks SQLite tests | Guard with SQLite-parity conftest shim (already pattern in repo) |
| Circular import `actionability.py` ↔ `checks.py` | Lazy-import `_OBSERVATION_PATTERNS` inside `_get_observation_patterns()` function |

## Out of scope (tracked as follow-up)

- F047.1: Delete `_is_observation` call + `_OBSERVATION_PATTERNS` module-level (after 2 weeks stable).
- Decisions/episodes/procedures actionability — same pattern, different schemas.
- Dashboard widget for unclassified count.

---

## Review Findings Folded In (2026-04-19, 3 agents)

All P1/BLOCKING/CRITICAL items applied inline to the steps above. Summary:

### C1 (Devil) — Fire-and-forget startup task swallows exceptions

**Step 9 revision:** wrap in supervising coroutine.

```python
async def _backfill_with_logging():
    try:
        result = await backfill.run_once()
        logger.info("F047 backfill finished: %s", result)
    except asyncio.CancelledError:
        logger.info("F047 backfill cancelled at shutdown — will resume next startup")
        raise
    except Exception:
        logger.exception("F047 backfill failed")

asyncio.create_task(_backfill_with_logging())
```

### C2 / P1-3 (Devil + Arch) — Multi-process backfill race

**Step 7 revision:** wrap `_run()` body in PG advisory lock keyed on `agent_id` hash:

```python
async def _run(self) -> dict:
    lock_key = int.from_bytes(hashlib.sha256(self.agent_id.encode()).digest()[:8], "big", signed=True)
    async with self.db.session() as session:
        got_lock = (await session.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key}
        )).scalar()
        if not got_lock:
            logger.info("F047 backfill: lock held by another process, skipping")
            return {"skipped": True, "reason": "lock_held"}
        try:
            return await self._run_batches()
        finally:
            await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key})
```

SQLite-mode tests: advisory lock query no-op'd by conftest (returns True).

### H5 (Devil) — **CRITICAL**: NULL-fallback branch replicates the bug being fixed

**Step 8 revision:** the plan's original fallback copied the buggy observation-wins short-circuit. Rewrite fallback to match `_looks_like_pending`'s positive-first semantics:

```python
# F047 fallback for NULL (unclassified) rows — positive wins over negative
actionable = getattr(fact, "actionable", None)
if actionable is True:
    is_pending = True
elif actionable is False:
    is_pending = False
else:
    # Positive action wins — only consult _is_observation when no action signal
    if self._looks_like_pending(fact.content):
        is_pending = True  # Action phrasing wins — always surface
    elif self._is_observation(fact.content):
        is_pending = False
    else:
        is_pending = score >= threshold  # bare embedding-similarity signal
```

### P1-1 / M8 (Arch + Devil) — Move `_OBSERVATION_PATTERNS` to actionability module

**Steps 1b + 8b (new):** Move `_OBSERVATION_PATTERNS` from `nous/heartbeat/checks.py:135` to `nous/heart/actionability.py` as the canonical owner. `checks.py` imports from there (`from nous.heart.actionability import _OBSERVATION_PATTERNS`). Eliminates the lazy-import + circular-dep risk. F047.1 deletion of pattern usage at read time is then a pure call-site removal, not a cross-module refactor.

### P1-2 / BLOCKING-2 / M7 — FactSummary propagation (ALL 4 construction sites)

**Step 2 revision:** `FactSummary` gains three fields: `actionable`, `actionable_confidence`, `tags`. The `tags` field is also currently missing — existing tag guard at `checks.py:274` is silently broken for FactSummary rows; F047 fixes it as a side effect.

**Step 6.4 revision (expanded):** Update **all four** construction sites:
- `_search` (facts.py:1055-1068)
- `_search_all` (facts.py:1148-1161)
- `list_by_category._list_by_category` (facts.py:967-977)
- `_list_all` (facts.py:1244-1254)

Each must populate `actionable`, `actionable_confidence`, `tags`.

### BLOCKING-1 (Impl) — Wiring path via Heart, not main.py directly

**Step 6 + Step 9 revision:** `FactManager` is constructed inside `Heart.__init__` at `nous/heart/heart.py:102`. Two options:

1. **Preferred**: extend `Heart.__init__` to accept `actionability_classifier` kwarg, forward to `FactManager`.
2. Inject post-construction: `heart.facts._actionability_classifier = classifier` in main.py, parallel to the admission LLM wiring at `main.py:148`.

Plan uses option 1 — adds `actionability_classifier` parameter to `Heart.__init__` between `admission_controller` and any subsequent kwargs.

### H3 (Devil) — Budget cap on backfill

**Step 5 revision:** add one more setting:

```python
actionability_backfill_daily_token_budget: int = Field(
    10_000, validation_alias="NOUS_ACTIONABILITY_BACKFILL_TOKEN_BUDGET",
    description="Max Haiku tokens backfill may spend per day (approx 20k facts)",
)
```

Wire into `ActionabilityBackfillHandler.__init__`: `budget_check=lambda: self._tokens_used < self._daily_budget`. Pass through to classifier.

### H4 (Devil) — Narrow exception handling

**Step 3 + 6 + 7 revision:** replace bare `except Exception` with targeted handlers:
- `asyncio.CancelledError` must re-raise
- `_llm_classify` validates `"actionable"` key is present before `.get()` (otherwise missing key silently becomes False)
- DB errors (`SQLAlchemyError`) logged, fact falls through to legacy path

### P2-1 (Arch) — CancelledError logging in backfill

See C1 fix above; `CancelledError` now logged at INFO before re-raise.

### P2-2 (Arch) — INFO log when classifier tier=default

**Step 3 revision:** after `return (self._default_when_unknown, 0.3, "default")`, add:

```python
logger.info("F047: classifier defaulted for %r (no LLM or neither heuristic matched)",
            content[:60])
```

### M6 (Devil) — SQLite bool vs int

**Step 8 revision:** use `== True` / `== False` instead of `is True` / `is False` for the heartbeat branch — SQLAlchemy may return `1`/`0` on SQLite. (Alternative: cast `bool(actionable)`.)

### M9 (Devil) — Expanded test coverage

**Step 10 additions:**
- Test: classifier raises inside `_learn` → fact saved with `actionable=None`, no exception propagates
- Test: `_llm_classify` returns dict without `"actionable"` key → tier=default (not silent False)
- Test: heartbeat `actionable=None` fallback path surfaces action-phrased facts even when an observation substring matches

---

**Net effect on plan size:** ~100 LOC more than original estimate — mostly supervising coroutine, advisory-lock block, and added tests. Total estimate revised to ~800 LOC prod + ~500 LOC tests.
