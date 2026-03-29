# Procedure Full-Body Search Indexing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix procedure search to index the full body text (implementation_notes, goals, core_tools, core_concepts) in both vector embeddings and keyword search, closing the 91.7% signal gap identified in issue #197.

**Architecture:** Three-part fix — (1) expand Python embed_text in `_store()`, (2) SQL migration to rebuild the `search_tsv` generated column, (3) SQL migration to backfill existing procedure embeddings via a one-time re-embed. Hidden-body asymmetry: search indexes everything, agent context continues showing compact summaries.

**Tech Stack:** Python (SQLAlchemy async), PostgreSQL (tsvector generated columns, pgvector), pytest with real Postgres.

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `nous/heart/procedures.py:64-72` | Expand `embed_text` in `_store()` to include all body fields |
| Create | `sql/migrations/023_procedure_fullbody_search.sql` | Rebuild `search_tsv` generated column with full body text |
| Modify | `tests/test_procedures.py` | Add test for full-body keyword search + verify expanded embedding |

**No ORM changes needed** — `search_tsv` is `GENERATED ALWAYS` and not mapped in the ORM (confirmed at `models.py:435`).

**No backfill migration needed** — The SQL `ALTER COLUMN` for `search_tsv` automatically recomputes for all rows. For embeddings, we add a `reembed_all()` method that the migration documents as a manual step (embeddings require API calls that can't run in SQL).

---

### Task 1: SQL Migration — Rebuild search_tsv

**Files:**
- Create: `sql/migrations/023_procedure_fullbody_search.sql`

- [ ] **Step 1: Write the migration SQL**

The migration drops and recreates the `search_tsv` generated column to include `implementation_notes`, `goals`, and `core_tools`. PostgreSQL automatically recomputes all existing rows when altering a generated column.

```sql
-- 023: Expand procedure search_tsv to include full body text (issue #197)
--
-- Before: to_tsvector('english', name || ' ' || COALESCE(description, ''))
-- After:  includes implementation_notes, goals, core_tools, core_concepts
--
-- PostgreSQL recomputes all rows automatically on generated column rebuild.
-- The GIN index idx_procedures_fts is also rebuilt automatically.

ALTER TABLE heart.procedures DROP COLUMN search_tsv;

ALTER TABLE heart.procedures
ADD COLUMN search_tsv tsvector
GENERATED ALWAYS AS (
  to_tsvector('english',
    name || ' '
    || COALESCE(description, '') || ' '
    || COALESCE(array_to_string(implementation_notes, ' '), '') || ' '
    || COALESCE(array_to_string(goals, ' '), '') || ' '
    || COALESCE(array_to_string(core_tools, ' '), '') || ' '
    || COALESCE(array_to_string(core_concepts, ' '), '')
  )
) STORED;

-- Recreate GIN index (dropped with column)
CREATE INDEX idx_procedures_fts ON heart.procedures USING GIN(search_tsv);
```

- [ ] **Step 2: Verify migration numbering**

Check that `023` is the next sequential number after `022_rubric_outcome_signals.sql`.

Run: `ls sql/migrations/`
Expected: Files 006-022 present, no 023 yet.

- [ ] **Step 3: Commit**

```bash
git add sql/migrations/023_procedure_fullbody_search.sql
git commit -m "feat: expand procedure search_tsv to include full body text (#197)"
```

---

### Task 2: Python — Expand embed_text in _store()

**Files:**
- Modify: `nous/heart/procedures.py:64-72`

- [ ] **Step 1: Write the failing test — keyword search finds procedure by implementation_notes content**

Add to `tests/test_procedures.py`:

```python
async def test_search_by_implementation_notes(heart, session):
    """Issue #197: search should match on implementation_notes body text."""
    await heart.store_procedure(
        _procedure_input(
            name="Deploy checklist",
            description="Production deployment steps",
            core_patterns=["deployment"],
            implementation_notes=[
                "Run database migrations before deploying new code",
                "Verify health checks pass after rollout",
                "Monitor error rates for 15 minutes post-deploy",
            ],
        ),
        session=session,
    )

    # Search for content that only exists in implementation_notes
    results = await heart.search_procedures("health checks rollout", session=session)
    assert len(results) >= 1
    assert results[0].name == "Deploy checklist"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_procedures.py::test_search_by_implementation_notes -v`
Expected: FAIL — keyword search on `search_tsv` only indexes name+description, so "health checks rollout" won't match.

**Note:** This test will only fail if the migration (Task 1) has NOT been applied. If running against a DB with migration 023 applied, the keyword leg will pass but the test still validates the full pipeline. The embedding leg is tested separately.

- [ ] **Step 3: Write test for expanded embedding text**

Add to `tests/test_procedures.py`:

```python
async def test_embed_text_includes_body_fields(heart, session):
    """Issue #197: embedding should be generated from all fields, not just name+desc+patterns."""
    # Store two procedures with same name/desc but different implementation_notes
    await heart.store_procedure(
        _procedure_input(
            name="Generic procedure",
            description="A generic procedure",
            core_patterns=["generic"],
            implementation_notes=["kubernetes pod autoscaling configuration"],
            goals=["scale infrastructure"],
            core_tools=["kubectl", "helm"],
            core_concepts=["container orchestration"],
        ),
        session=session,
    )
    await heart.store_procedure(
        _procedure_input(
            name="Another procedure",
            description="Another generic procedure",
            core_patterns=["generic"],
            implementation_notes=["watercolor painting techniques for landscapes"],
            goals=["create art"],
            core_tools=["brushes", "palette"],
            core_concepts=["color theory"],
        ),
        session=session,
    )

    # Search for kubernetes — should rank first procedure higher due to body content
    results = await heart.search_procedures("kubernetes autoscaling", session=session)
    assert len(results) >= 1
    assert results[0].name == "Generic procedure"
```

- [ ] **Step 4: Add _build_embed_text helper and update _store()**

Add a shared helper to `nous/heart/procedures.py` (before `_store`) to avoid duplicating the formula:

```python
def _build_embed_text(
    name: str,
    description: str | None,
    core_patterns: list[str] | None,
    goals: list[str] | None,
    core_tools: list[str] | None,
    core_concepts: list[str] | None,
    implementation_notes: list[str] | None,
) -> str:
    """Build the text used for procedure embedding (issue #197).

    Includes all body fields, not just metadata, for full-body search accuracy.
    """
    return (
        f"{name} {description or ''} "
        f"{' '.join(core_patterns or [])} "
        f"{' '.join(goals or [])} "
        f"{' '.join(core_tools or [])} "
        f"{' '.join(core_concepts or [])} "
        f"{' '.join(implementation_notes or [])}"
    ).strip()
```

Then update `_store()` line 68 to use it:

```python
# Before:
embed_text = (f"{input.name} {input.description or ''} {' '.join(input.core_patterns)}").strip()

# After:
embed_text = _build_embed_text(
    input.name, input.description, input.core_patterns,
    input.goals, input.core_tools, input.core_concepts,
    input.implementation_notes,
)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_procedures.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add nous/heart/procedures.py tests/test_procedures.py
git commit -m "feat: expand procedure embed_text to include full body fields (#197)"
```

---

### Task 3: Add reembed_all() method for existing procedures

**Files:**
- Modify: `nous/heart/procedures.py`
- Modify: `tests/test_procedures.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_procedures.py`:

```python
async def test_reembed_all(heart, session):
    """reembed_all() recomputes embeddings for all active procedures."""
    # Store a procedure (embedding generated with old formula doesn't matter —
    # we just need to verify reembed updates it)
    detail = await heart.store_procedure(
        _procedure_input(
            name="Reembed test",
            description="Test procedure",
            implementation_notes=["specialized quantum computing algorithms"],
        ),
        session=session,
    )

    count = await heart.reembed_procedures(session=session)
    assert count >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_procedures.py::test_reembed_all -v`
Expected: FAIL — `reembed_procedures` method doesn't exist yet.

- [ ] **Step 3: Add reembed_all() to ProcedureManager**

Add to `nous/heart/procedures.py` after the `reactivate()` method:

```python
async def reembed_all(self, session: AsyncSession | None = None) -> int:
    """Recompute embeddings for all active procedures using expanded embed_text.

    Use after changing the embedding formula to backfill existing records.
    Returns the number of procedures re-embedded.
    """
    if session is None:
        async with self.db.session() as session:
            count = await self._reembed_all(session)
            await session.commit()
            return count
    return await self._reembed_all(session)

async def _reembed_all(self, session: AsyncSession) -> int:
    if not self.embeddings:
        return 0

    result = await session.execute(
        select(Procedure)
        .where(Procedure.agent_id == self.agent_id)
        .where(Procedure.active == True)  # noqa: E712
    )
    procedures = list(result.scalars().all())

    count = 0
    for proc in procedures:
        embed_text = _build_embed_text(
            proc.name, proc.description, proc.core_patterns,
            proc.goals, proc.core_tools, proc.core_concepts,
            proc.implementation_notes,
        )
        try:
            proc.embedding = await self.embeddings.embed(embed_text)
            count += 1
        except Exception:
            logger.warning("Re-embed failed for procedure %s", proc.id)

    await session.flush()
    return count
```

- [ ] **Step 4: Add delegation method to Heart facade**

Heart uses explicit delegation (no `__getattr__`). Add to `nous/heart/heart.py` after `list_inactive_skill_procedures()` (line ~435):

```python
async def reembed_procedures(self, session: AsyncSession | None = None) -> int:
    """Recompute embeddings for all active procedures (issue #197 backfill)."""
    return await self.procedures.reembed_all(session=session)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_procedures.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add nous/heart/procedures.py tests/test_procedures.py
git commit -m "feat: add reembed_all() for backfilling procedure embeddings (#197)"
```

---

### Task 4: Run full test suite

- [ ] **Step 1: Run all procedure tests**

Run: `uv run pytest tests/test_procedures.py -v`
Expected: All PASS.

- [ ] **Step 2: Run full test suite to check for regressions**

Run: `uv run pytest tests/ -v --timeout=120`
Expected: No regressions. Existing tests should pass unchanged since:
- The expanded `embed_text` produces different embeddings but MockEmbeddingProvider is deterministic-by-input, so existing test inputs still produce consistent results
- The `search_tsv` column change only adds more text to the index — existing keyword matches still work

- [ ] **Step 3: Fix any failures**

If any test fails, investigate and fix. Common issues:
- Test assertions on exact embedding values (unlikely — tests use mock provider)
- Test that relied on `search_tsv` NOT matching certain text (unlikely)

---

### Task 5: Update init.sql for fresh installs

**Files:**
- Modify: `sql/init.sql:342-344`

- [ ] **Step 1: Update the search_tsv definition in init.sql**

The `init.sql` is used for fresh database installs. Update the `heart.procedures` table definition to match the migration:

```sql
-- Before (line 342-344):
    search_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english', name || ' ' || COALESCE(description, ''))
    ) STORED,

-- After:
    search_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english',
            name || ' '
            || COALESCE(description, '') || ' '
            || COALESCE(array_to_string(implementation_notes, ' '), '') || ' '
            || COALESCE(array_to_string(goals, ' '), '') || ' '
            || COALESCE(array_to_string(core_tools, ' '), '') || ' '
            || COALESCE(array_to_string(core_concepts, ' '), '')
        )
    ) STORED,
```

- [ ] **Step 2: Commit**

```bash
git add sql/init.sql
git commit -m "feat: update init.sql procedure search_tsv for fresh installs (#197)"
```

---

### Task 6: Post-Deploy — Backfill existing procedure embeddings

This is a **manual post-deploy step**. The SQL migration (Task 1) automatically recomputes `search_tsv` for all rows, but embeddings require API calls and cannot be rebuilt in SQL.

- [ ] **Step 1: Document the backfill command**

After deploying the migration and code changes, run via the Nous Python shell or a one-off script:

```python
# Backfill existing procedure embeddings with expanded text
import asyncio
from nous.main import create_app

async def backfill():
    app = await create_app()
    count = await app.heart.reembed_procedures()
    print(f"Re-embedded {count} procedures")
    await app.shutdown()

asyncio.run(backfill())
```

Alternatively, if Nous is already running, use the REST API or MCP to trigger.

- [ ] **Step 2: Verify backfill completed**

Check that all active procedures have non-null embeddings:

```sql
SELECT count(*) AS total,
       count(embedding) AS embedded
FROM heart.procedures
WHERE active = true;
```

Expected: `total` = `embedded` (all active procedures have embeddings).
