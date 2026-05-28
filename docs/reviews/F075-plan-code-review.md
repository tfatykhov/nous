# F075 Implementation Plan — Code-Level Review

**Plan reviewed:** `docs/superpowers/plans/2026-05-28-f075-temporal-fact-extraction.md`
**Spec:** `docs/features/F075-temporal-fact-extraction.md` (v2.17)
**Reviewer focus:** Python correctness of each snippet, signature consistency with the repo, Pydantic v2 idioms, SQL/ORM agreement, test-naming convention, F047 structural precedent.
**Date:** 2026-05-28

The plan is well-scoped and the spec's 17 codex rounds have already eliminated most architectural risk. The findings below are tactical bugs in the plan's snippets and 2 documentation mislocations that will trip the implementer if copied verbatim.

---

## Critical (P1)

### 1. Phase 7 — wrong DB attribute on `GraphDensifier`
**File:** plan §Phase 7.1, lines 452-485
**Cite:** `nous/brain/graph_densifier.py:115` (`self.db = db`), `:446, 473, 500, 547` all use `self.db.session()` — NO underscore.

The plan's snippet opens:
```python
async with self._db.session() as session:
```
This is wrong. The constructor (line 115) assigns `self.db`, not `self._db`. The new method will `AttributeError` immediately on first call.

**Fix:** change to `async with self.db.session() as session:`. Confirmed `self._agent_id` (with underscore) IS correct — line 119 sets `self._agent_id = agent_id`. The inconsistency between `self.db` and `self._agent_id` is pre-existing repo state, not a plan bug.

### 2. Phase 5.5 — `_to_recall_result` lives in `heart.py`, not `facts.py`
**File:** plan §Phase 5.5, "(`nous/heart/facts.py` `_to_recall_result` path)"
**Cite:** `nous/heart/heart.py:1085` `def _to_recall_result(self, memory_type: str, item: object, score: float) -> RecallResult | None:` — only definition site in the repo.

Plan instructs editing `facts.py`, but `_to_recall_result` exists only in `heart.py`. The function receives a `FactSummary` instance (not ORM `Fact`) at the fact branch (heart.py:1100). So the plan's `fact.event_date.isoformat()` must read `item.event_date`. This works because Phase 5.3 adds `event_date: date | None = None` to `FactSummary`. Note: the **spec's wire-path row 10 also mislocates this to `facts.py`** — both docs inherit the same error.

**Fix:** rewrite Phase 5.5 to target `nous/heart/heart.py` at the `FactSummary` branch (~line 1100-1110):
```python
metadata = {
    "category": item.category,
    "subject": item.subject,
    "confidence": item.confidence,
}
if item.event_date is not None:
    metadata["event_date"] = item.event_date.isoformat()
return RecallResult(type="fact", id=item.id, summary=item.content, score=score, metadata=metadata)
```

### 3. Phase 7 — `INSERT` likely missing `extraction_method` column
**File:** plan §Phase 7.1, lines 452-485
**Cite:** `nous/storage/models.py` GraphEdge `__table_args__` declares `CheckConstraint("extraction_method IN ('deterministic','heuristic','inferred')", name="ck_edges_extraction_method")` (F065 provenance).

The Phase 7 INSERT lists columns `(source_id, source_type, target_id, target_type, agent_id, relation, weight, auto_linked)`. The `extraction_method` column is omitted. **The implementer must check the column's nullability/default in `sql/migrations/*edge_provenance*.sql` before Phase 7.** If `NOT NULL` without a server-default, every `happened_before` INSERT fails with a not-null violation. Recommended fix:
```sql
INSERT INTO brain.graph_edges
    (source_id, source_type, target_id, target_type,
     agent_id, relation, weight, auto_linked, extraction_method)
SELECT a.id, 'fact', b.id, 'fact',
       a.agent_id, 'happened_before', 1.0, TRUE, 'deterministic'
FROM ...
```
'deterministic' is semantically correct: the chain is a deterministic SQL traversal, not an LLM verdict. This is also a gap in the spec — codex rounds 1-17 didn't surface it because none of them exercised a fresh-DB INSERT.

---

## Important (P2)

### 4. Phase 4 — `hasattr` dedup-bypass check is dead code
**File:** plan §Phase 4.2, lines 308-321
**Cite:** plan Phase 2.3 mandates `event_date: date | None = None` on **both** `FactSummary` and `FactDetail`. `search_facts` returns `list[FactSummary]` (`nous/heart/heart.py:351-361`).

After Phase 2 ships, every `FactSummary` instance has `event_date` (defaulting to `None`). The plan's:
```python
existing_event_date = existing[0].event_date if hasattr(existing[0], "event_date") else None
```
is dead defensive code — `hasattr(existing[0], "event_date")` is always True post-Phase-2. The user's specific question: "is `hasattr` right here, or should `FactSummary` always have the field?" — spec wire-path row 7 mandates it always has the field, so `hasattr` is contradictory with the spec. Drop the guard:
```python
existing_event_date = existing[0].event_date
```

### 5. Phase 5.2 — `_learn` dedup-bypass is under-specified
**File:** plan §Phase 5.2, lines 360-373
**Cite:** `nous/heart/facts.py:376-381` — actual `_learn` dedup is:
```python
if embedding is not None:
    dupe = await self._find_duplicate(embedding, exclude_ids, session)
    if dupe is not None:
        return await self._confirm(dupe.id, session)
```
The plan's snippet references `existing_fact.event_date` but `_find_duplicate` returns an opaque shape — plan doesn't specify whether to (a) push the date filter into `_find_duplicate`'s SQL, or (b) wrap the call site. Option (a) avoids redundant fetches and is cleaner; option (b) is simpler. Plan needs to pick one and pseudo-code it. Without that, the implementer will improvise. Concretely the cleanest path is:
```python
if dupe is not None:
    if not (input.event_date is not None
            and dupe.event_date is not None
            and input.event_date != dupe.event_date):
        return await self._confirm(dupe.id, session)
    # else fall through to insert — distinct event dates
```
This requires `_find_duplicate` to return an object exposing `event_date`. Since it currently returns the ORM `Fact` (post-Phase 1 has the column), this is free.

**Sink-semantics audit (user Q3):** verified clean. `_learn` (lines 348-449) does: length gate (line 359), embedding generation (370), `_find_duplicate`/`_confirm` early return (377-381), `_admission_controller` reject (383-409), F047 classifier (414-426), then `Fact(...)` construction (428-449). None of these mutate `input` or write event-date columns. Adding `event_date=input.event_date, event_date_classified_at=input.event_date_classified_at` to the constructor at line 428 is a pure passthrough — Phase 5.1's "pure sink" framing is correct. Only Phase 5.2 (dedup-bypass) introduces real new behavior.

### 6. Phase 7 — `LATERAL` syntax + asyncpg verdict (user Q4)
**Verified correct.** `JOIN LATERAL (SELECT ... LIMIT 1) b ON TRUE` is standard PostgreSQL ≥9.3, fully supported by asyncpg + pgvector. The deterministic tiebreaker `ORDER BY b.event_date ASC, b.id ASC` is safe (same-date facts won't all collapse to one target — but per spec §Layer 2, that's the intended O(N) edge count semantics).

**ON CONFLICT confirmed against ORM.** `nous/storage/models.py:239` declares `UniqueConstraint("source_id", "target_id", "relation", name="uq_edges_src_tgt_rel")`. The plan's `ON CONFLICT (source_id, target_id, relation) DO NOTHING` matches the unique constraint correctly.

### 7. Phase 7 — `result.fetchall()` shape with `RETURNING id`
**File:** plan §Phase 7.1, lines 482-484

With `await session.execute(text("... RETURNING id"))`, the canonical SQLAlchemy AsyncSession idiom for a single-column RETURNING is `result.scalars().all()` (returns `list[UUID]`) or just use `result.rowcount`. `result.fetchall()` works but returns `Sequence[Row]` of single-column rows — needs `len(...)` which the plan does. Recommend:
```python
inserted_ids = result.scalars().all()
await session.commit()
return len(inserted_ids)
```
or just `return result.rowcount` (verify against existing densifier INSERT methods like `link_episode_deterministic`).

### 8. Test-file naming inconsistency (user Q6)
**File:** plan §Phases 1.4–9
**Cite:** `tests/test_temporal_recall.py` ALREADY EXISTS (spec 008.6, unrelated to F075). `tests/test_f061_migration_041.py` is the existing standalone migration test — it uses F-prefix.

Plan proposes a mix:
- `test_temporal_extractor.py`, `test_temporal_edges.py`, `test_temporal_backfill.py`, `test_temporal_schemas.py` (component-named)
- `test_migration_053.py` (no prefix)
- `test_f075_end_to_end.py` (F-prefix)

Repo convention for recent feature work is `test_f<NNN>_<descriptor>.py` — see all `test_f061_*` (16 files), `test_f065_*`, `test_f066_*`, `test_f067_*`. Component-named files exist (`test_facts.py`, `test_heart.py`) but are pre-F-prefix-era core-module tests.

**Recommend:**
- `test_f075_extractor.py`, `test_f075_edges.py`, `test_f075_backfill.py`, `test_f075_migration_053.py`, `test_f075_schemas.py`, `test_f075_end_to_end.py`.
- Avoids any future name collision (`test_temporal_recall.py` is taken) and makes `pytest tests/ -k f075` cleanly enumerate the feature's suite.

---

## Minor (P3)

### 9. Phase 8 — module split: F070-chunks precedent, not F047 (user Q5)
**Cite:** `nous/handlers/actionability_backfill.py` (F047, NO CLI script; startup-driven from `nous/main.py`). `scripts/backfill_f070_chunks.py` (CLI flags; logic in `nous/brain/graph_densifier.py`).

F047 is invoked via `ActionabilityBackfillHandler.run_once()` at startup — no `scripts/` entry. F075 backfill per spec §Layer 4 is **operator-invoked** with `--agent-id --token-budget --dry-run` flags. The plan's handler+thin-CLI split therefore correctly follows the **F070-chunks-backfill** precedent (which is the right shape for operator-invoked work), NOT F047. No change needed — but recommend a footnote in the plan so a reviewer doesn't expect F047-shape.

### 10. Phase 2 — `field_validator` import + `from __future__ import annotations`
**File:** plan §Phase 2.1
**Cite:** `nous/heart/schemas.py:1` has `from __future__ import annotations`; line 8 already imports `datetime`; line 12 imports `BaseModel, Field`.

Plan correctly adds `from pydantic import field_validator` and `from datetime import date, datetime`. The `datetime` is already imported at line 8 — implementer should merge, not duplicate. Pydantic v2 with `from __future__ import annotations` resolves type hints at model-build time using the module's globals, so `date` must be runtime-importable (covered by the plan).

### 11. pytest-asyncio mode (user Q7)
**Cite:** `pyproject.toml:59` `asyncio_mode = "auto"`.

No `@pytest.mark.asyncio` decorators needed for any new test. Plan doesn't mention this — recommend a one-liner in §Phase 11's acceptance gate so the implementer doesn't add decorators by reflex.

---

## Sanity-check confirmations (no action needed)

- `ON CONFLICT (source_id, target_id, relation) DO NOTHING` matches `UniqueConstraint("source_id","target_id","relation", name="uq_edges_src_tgt_rel")` at `nous/storage/models.py:239`.
- `LATERAL ... LIMIT 1` is standard PostgreSQL; asyncpg + pgvector compose with it without quirks.
- `call_background_llm_structured` confirmed at `nous/handlers/__init__.py:86`.
- `PipelineResult.metadata` field at `nous/api/retrieval_pipeline.py:69` confirms Phase 6 plumbing target.
- `_heart_results_to_pipeline` (lines 659-671) does NOT carry metadata today — Phase 6.1 fix is necessary.
- `episode.started_at` is in scope at `episode_summarizer.py:135` for Phase 3.1's wiring.
- `Fact` ORM at `nous/storage/models.py:469-511` — adding two `Mapped[...]` columns is mechanical; no relationship updates needed.

---

## Recommended pre-flight diff before Phase 1

Five-minute sanity pass before any code lands:
1. `grep -n "self._db\|self\.db" nous/brain/graph_densifier.py` — confirm idiom for Phase 7.
2. `grep -n "_to_recall_result" nous/heart/heart.py` — confirm Phase 5.5 target.
3. `psql ... -c "\d brain.graph_edges"` — confirm `extraction_method` nullability/default for Phase 7.
4. `pytest --collect-only tests/test_temporal_*.py` — confirm no existing-name collisions.

Steps 1-3 each take 30 seconds and would have caught P1 #1, #2, #3.
