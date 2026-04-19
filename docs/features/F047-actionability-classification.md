# F047: Actionability Classification at Learn Time

**Status:** ✅ Shipped
**Proposed by:** Tim (via PR #335 review)
**Date:** 2026-04-19
**Depends on:** F002 (Heart Module — shipped), F034 (Heartbeat — shipped), F023 (Admission Control — shipped)
**Blocks:** None
**Supersedes:** PR #335 (`fix/observation-patterns-design-rules`)

---

## Problem

`SelfInitiatedCheck._is_observation` in `nous/heartbeat/checks.py:546` classifies facts as actionable-or-not by running every fact through a list of lowercase substring patterns (`_OBSERVATION_PATTERNS`, 40+ entries). Four PRs have grown this list — #277, #323, #328, and proposed #335 — each adding 10–12 patterns to suppress new false positives.

Four failure modes are structural, not fixable by adding more patterns:

1. **Read-time decision, every tick.** Classification runs in the heartbeat loop against raw strings, with no persisted verdict. Every future tick re-derives the same answer.
2. **Contradictory suppression policies in the same module.** `_embedding_search:281` short-circuits on `_is_observation` *before* `_looks_like_pending`'s positive-first logic gets to run. `_looks_like_pending` (line 498) says action wins over observation; `_embedding_search` says observation wins over action. Any substring pattern that happens to appear in a legitimate TODO (e.g. `"I need to add idempotency and side-effect tests"`) is silently dropped.
3. **Arms-race growth.** Each PR adds more substrings, which adds more collision risk with action phrasing, which creates more false negatives, which… PR #328 explicitly flagged this and recommended a spike on a learn-time classifier.
4. **No observability.** Suppression is silent — no log, no metric. A user's explicit TODO that happens to match a substring vanishes from the heartbeat with no trace.

### Symptom (PR #335 motivating facts)

Four facts flagged in 5+ consecutive heartbeat sweeps (Apr 13, 14, 18, 19):

- `b517546d` — task-completion rule (encoded as censor, still surfaces as "pending")
- `609b6be1` — filed issue with PR#/branch references
- `fc8906a5` — architectural design rule, not a task
- `410df5f1` — resolved bug description (fixed in PR #324)

None are actionable. All get re-classified every 30 s because the verdict isn't stored.

---

## Goals

- Classify each fact's **actionability** once, at learn time, and persist the verdict on `heart.facts`.
- Replace the `_is_observation` short-circuit in `SelfInitiatedCheck._embedding_search` with a SQL filter (`actionable = TRUE`).
- Keep `_OBSERVATION_PATTERNS` and `_looks_like_pending` as a Tier-0 heuristic *inside the classifier* — they're still useful signal, just at the right layer.
- Make the decision observable: persist confidence + tier + reason; log classifications at DEBUG; surface a small set of metrics for tuning.
- Backfill existing facts so the switchover is seamless.

## Non-goals

- **No deletion of `_OBSERVATION_PATTERNS` in this PR.** Kept as Tier-0 heuristic and as rollback safety. Separate PR (F047.1) removes the read-time `_is_observation` call once backfill coverage is ≥99% and production has been stable for 2 weeks.
- **No change to other memory types.** Decisions/episodes/procedures have their own classification needs; out of scope here.
- **No change to heartbeat scheduling, escalation, or finding lifecycle** (F034.1–F034.5).
- **No REST API for manual override.** Users can retag via existing fact endpoints if needed.

---

## Design

### 1. Schema — two new nullable columns on `heart.facts`

```sql
-- sql/migrations/034_fact_actionability.sql
ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS actionable BOOLEAN DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS actionable_confidence REAL DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_facts_actionable_agent
    ON heart.facts(agent_id, actionable)
    WHERE actionable = TRUE;

COMMENT ON COLUMN heart.facts.actionable IS
    'F047: True = pending action, False = observation/resolved, NULL = not yet classified';
COMMENT ON COLUMN heart.facts.actionable_confidence IS
    'F047: Classifier confidence 0.0–1.0';
```

Both nullable — NULL means "not yet classified" (legacy rows pre-backfill; the heartbeat falls back to the legacy path for these).

Partial index so lookups scan only the small actionable set, not all facts. Matches the existing `idx_facts_` index style in the schema.

### 2. Classifier module — `nous/heart/actionability.py` (new)

```python
class ActionabilityClassifier:
    """Classify fact actionability using tiered heuristics + LLM fallback."""

    # Tier 0: category/tag hard-filters (cheap, deterministic, no LLM)
    _HARD_NO: set[str] = {"person", "preference"}
    _HARD_NO_TAGS: set[str] = {"resolved", "identity"}

    # Tier 1: positive-action heuristics (reused from _looks_like_pending)
    _ACTION_PATTERNS: tuple[str, ...] = (
        "todo", "i need to", "action needed", "remind me",
        "follow-up on", "follow up on", "needs to",
        "should follow up", "must complete", "waiting for response",
        "hasn't been done", "not yet completed", "pending review",
        "pending approval",
    )

    # Tier 1: observation heuristics (reused from _OBSERVATION_PATTERNS)
    # Imported from checks.py to avoid duplicating the list.
    _OBSERVATION_PATTERNS: tuple[str, ...] = ()  # populated at import

    def __init__(
        self,
        llm: LLMClient | None = None,
        model: str = "claude-haiku-4-5-20251001",
        budget_check: Callable[[], bool] | None = None,
        default_when_unknown: bool = False,
    ) -> None: ...

    async def classify(
        self,
        content: str,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> tuple[bool, float, str]:
        """Return (actionable, confidence, tier) for a fact.

        tier is one of: "hard_filter", "heuristic_action",
        "heuristic_observation", "llm", "default".
        """
        # Tier 0: hard filters
        if category in self._HARD_NO:
            return (False, 1.0, "hard_filter")
        if tags and {t.lower() for t in tags} & self._HARD_NO_TAGS:
            return (False, 1.0, "hard_filter")

        lower = content.lower()
        has_action = any(p in lower for p in self._ACTION_PATTERNS)
        has_observation = any(p in lower for p in self._OBSERVATION_PATTERNS)

        # Tier 1: positive-wins-over-negative (matches _looks_like_pending)
        if has_action and not has_observation:
            return (True, 0.85, "heuristic_action")
        if has_observation and not has_action:
            return (False, 0.85, "heuristic_observation")

        # Tier 2: LLM disambiguation (only for ambiguous or neither-match cases)
        if self._llm and (self._budget_check is None or self._budget_check()):
            try:
                return await self._llm_classify(content, category, tags)
            except Exception:
                logger.debug("F047: LLM classify failed", exc_info=True)

        # Default: fail closed
        return (self._default_when_unknown, 0.3, "default")
```

Key design points:
- **Positive-wins-over-negative logic** (§2 of existing `_looks_like_pending`) is the authority. The classifier keeps that contract.
- **LLM only fires when tier 0/1 is ambiguous** — both patterns match, or neither. Empirically expect ~15–25 % of facts.
- **`default_when_unknown=False`** is fail-closed: don't page the user on uncertain facts.
- **No state.** Pure classification function; no DB access. Facts.py does the persistence.

### 3. Wire into `FactManager._learn` (`nous/heart/facts.py:297`)

One call site — right after admission, before `Fact()` construction:

```python
# F047: Classify actionability at learn time
actionable: bool | None = None
actionable_conf: float | None = None
if self._actionability_classifier:
    try:
        actionable, actionable_conf, tier = await self._actionability_classifier.classify(
            input.content, input.category, input.tags or [],
        )
        logger.debug(
            "F047: actionable=%s conf=%.2f tier=%s for %s",
            actionable, actionable_conf, tier, input.content[:60],
        )
    except Exception:
        logger.warning("F047: actionability classifier failed", exc_info=True)

fact = Fact(
    ...,
    actionable=actionable,
    actionable_confidence=actionable_conf,
)
```

Classifier is injected on `FactManager` construction in `main.py`, alongside `admission_controller` and `set_llm_client`. Disabled classifier → both columns stay NULL → legacy fallback path runs.

### 4. Add `actionable` filter to `FactManager.search` (`nous/heart/facts.py:983`)

New optional kwarg `actionable: bool | None = None`. Pushed into `extra_where` for `hybrid_search` and `_search_all`:

```python
if actionable is not None:
    extra_where += " AND t.actionable = :actionable"
    extra_params["actionable"] = actionable
```

**Critical**: NULL rows (unclassified) are *excluded* when `actionable=True` is passed. That's intentional — once backfill runs, no rows should be NULL. During the migration window, the heartbeat's fallback branch picks up NULLs.

### 5. Replace `_is_observation` short-circuit in heartbeat

`nous/heartbeat/checks.py:241-296` — `_embedding_search`:

```python
# Before: 
# if not self._is_observation(fact.content) and (
#     score >= threshold or self._looks_like_pending(fact.content)
# ):

# After (F047):
# Prefer persisted actionable flag; fall back to legacy path for unclassified rows.
if getattr(fact, "actionable", None) is True:
    is_pending = True
elif getattr(fact, "actionable", None) is False:
    is_pending = False
else:
    # Legacy fallback for NULL (unclassified) rows — same logic as before
    is_pending = (not self._is_observation(fact.content)) and (
        score >= threshold or self._looks_like_pending(fact.content)
    )

if is_pending:
    findings.append(...)
```

Also add `actionable=True` kwarg to the fact search (`fact.search(..., actionable=True)`) once backfill coverage is verified — but keep the in-Python fallback as defense-in-depth.

**Push `actionable` onto `FactSummary`** so `getattr(fact, "actionable", None)` returns the real value from `search()` — otherwise we're always hitting the NULL fallback path.

### 6. Backfill — `nous/handlers/actionability_backfill.py` (new)

Event-bus handler triggered at startup (and via REST) if unclassified rows exist. Follows `fact_extractor` pattern.

```python
class ActionabilityBackfillHandler:
    """Backfill heart.facts.actionable for NULL rows. Idempotent, batched."""

    BATCH_SIZE = 100
    RATE_LIMIT_DELAY_S = 0.5  # between batches

    async def run_once(self) -> BackfillResult:
        """Process all unclassified facts in batches until done or cancelled."""
        total, classified, errors = 0, 0, 0
        while True:
            batch = await self._fetch_unclassified_batch(self.BATCH_SIZE)
            if not batch:
                break
            for fact in batch:
                try:
                    a, c, _tier = await self.classifier.classify(
                        fact.content, fact.category, fact.tags or [],
                    )
                    await self._update_actionable(fact.id, a, c)
                    classified += 1
                except Exception:
                    logger.warning("F047: backfill failed for %s", fact.id, exc_info=True)
                    errors += 1
                total += 1
            await asyncio.sleep(self.RATE_LIMIT_DELAY_S)
        return BackfillResult(total=total, classified=classified, errors=errors)
```

Triggered on startup (fire-and-forget task) when `NOUS_ACTIONABILITY_BACKFILL_ON_STARTUP=True` (default). Idempotent — only processes NULL rows.

### 7. Settings — `nous/config.py`

```python
# F047: Actionability classification
actionability_enabled: bool = Field(
    True,
    validation_alias="NOUS_ACTIONABILITY_ENABLED",
    description="Enable F047 actionability classification at fact learn time",
)
actionability_llm_enabled: bool = Field(
    True,
    validation_alias="NOUS_ACTIONABILITY_LLM_ENABLED",
    description="Use LLM (Haiku) for ambiguous actionability cases",
)
actionability_model: str = Field(
    "claude-haiku-4-5-20251001",
    validation_alias="NOUS_ACTIONABILITY_MODEL",
    description="LLM model for actionability tier-2 classification",
)
actionability_default: bool = Field(
    False,
    validation_alias="NOUS_ACTIONABILITY_DEFAULT",
    description="Fallback verdict when classifier can't decide (False = fail-closed)",
)
actionability_backfill_on_startup: bool = Field(
    True,
    validation_alias="NOUS_ACTIONABILITY_BACKFILL_ON_STARTUP",
    description="Run backfill automatically on startup if NULL rows exist",
)
```

---

## Observability

- **Logs:** `F047: actionable=True conf=0.85 tier=heuristic_action for "I need to..."` at DEBUG on every classification.
- **Backfill result** logged at INFO on completion: `F047 backfill: 342 facts, 340 classified, 2 errors in 45.2s`.
- **Dashboard** addition (optional follow-up): new field on `/dashboard/health` showing unclassified fact count + latest backfill timestamp.
- **No new REST endpoints in this PR.** The backfill auto-runs and logs; manual trigger can come later if needed.

---

## Tests

Covered in the implementation plan. Headline tests:

1. **Unit — classifier:** one test per tier (hard_filter, heuristic_action, heuristic_observation, llm, default). Each Codex/review false-negative from PR #335 becomes a regression test: *"I need to add idempotency and side-effect tests"* asserts `(True, ≥0.8, "heuristic_action")`.
2. **Unit — facts.learn:** fact created with actionable column populated.
3. **Integration — heartbeat:** mock a fact with `actionable=True` → finding surfaces; `actionable=False` → no finding; `actionable=NULL` → falls back to legacy `_is_observation` path.
4. **Integration — backfill:** seed NULL rows, run handler, verify all are classified.
5. **Migration test:** new columns present, default NULL, index created. SQLite parity shim for CI.

---

## Rollout

Single PR, three internal phases (not separate PRs):

| Phase | Change | Risk |
|---|---|---|
| A | Schema + classifier module + tests (classifier alone) | Zero — dead code until wired |
| B | `FactManager` integration + `search()` filter + heartbeat fallback branch | Low — additive, legacy path still runs |
| C | Backfill handler + startup trigger + settings + CLAUDE.md update | Low — idempotent, gated by env var |

Rollback: set `NOUS_ACTIONABILITY_ENABLED=False`. All new facts stay `actionable=NULL`, heartbeat reverts to legacy path. Schema columns stay — removal is a separate migration.

---

## Success Criteria

- All 12 Codex/review false-negative examples from PR #335 review correctly classified (≥80 % confidence).
- The 4 PR-#335 motivating facts classified `actionable=False` at backfill time.
- Heartbeat no longer surfaces those 4 facts after 1 tick post-backfill.
- No regression in existing `test_heartbeat_intelligent.py` (all prior tests pass).
- LLM tier fires on <25 % of new facts (measured via log counter).

---

## Deferred to F047.1

- Delete `_is_observation` call + `_OBSERVATION_PATTERNS` from read path (after 2 weeks of stable production).
- Classify decisions, episodes, procedures similarly if we see the same pattern emerge there.
- Online feedback loop: when a user dismisses a heartbeat finding, retrain classifier bias.
