# DB Dependency Report — Nous Test Suite

**Generated:** 2026-04-07  
**Total test files analyzed:** 125 (+ `conftest.py`)  
**Goal:** Run all tests without a real PostgreSQL connection.

---

## Executive Summary

| Category | Count | Status |
|----------|-------|--------|
| **CLEAN** | 92 | Already pass without DB — no changes needed |
| **NEEDS_MOCK** | 28 | Use `session`/`heart`/`db` fixtures; migrate to SQLite in-memory |
| **COMPLEX** | 2 | Use raw schema-qualified SQL that needs extra SQLite patches |
| **PG_SPECIFIC** | 3 | Explicitly test PostgreSQL features; keep as Postgres-only tests |
| **Total** | 125 | |

**Prior art already exists:** `tests/sqlite_compat.py` and `tests/sqlite_patches.py` implement the
SQLite in-memory backend and monkey-patches for PG-specific methods. The remaining work is to wire
them into a new `conftest.py` and handle the COMPLEX/PG_SPECIFIC edge cases.

---

## Existing Infrastructure (Don't Reinvent)

Three files already exist in `tests/` as untracked work-in-progress:

### `tests/sqlite_compat.py`
Provides:
- `TestDatabase` class matching the production `Database` interface
- `create_test_engine()` — async SQLite engine with schema remapping (`brain/heart/nous_system → None`)
- `create_tables()` — creates all ORM tables in SQLite
- SQLAlchemy `@compiles` overrides for `Vector → TEXT`, `JSONB → JSON`, `ARRAY → JSON`
- `install_sqlite_defaults()` — ORM event listener for PG server defaults (`gen_random_uuid`, `now()`)
- `install_array_deserializer()` — event listener to deserialize JSON strings back to lists on load
- `patch_model_columns_for_sqlite()` — replaces ARRAY/Vector columns with TypeDecorator versions
- SQL rewriting hook: strips schema prefixes, rewrites `NOW()`, `make_interval()`
- Pure-Python helpers: `cosine_similarity()`, `keyword_match_score()`

### `tests/sqlite_patches.py`
Monkey-patches the following PG-specific methods with pure-Python equivalents:
- `nous.heart.search.hybrid_search` → `sqlite_hybrid_search` (cosine + keyword, RRF merge)
- `nous.heart.search.batch_fetch_embeddings` → `sqlite_batch_fetch_embeddings`
- `FactManager._find_duplicate` → `sqlite_find_duplicate`
- `FactManager._find_contradiction` → `sqlite_find_contradiction`
- `FactManager._find_max_similarity` → `sqlite_find_max_similarity`
- `FactManager._search_all` → `sqlite_search_all`
- `FactManager._get_current` → `sqlite_get_current` (replaces recursive CTE)
- `FactManager._find_contradiction_candidates` → `sqlite_find_contradiction_candidates`
- `FactManager._check_domain_threshold` → `sqlite_check_domain_threshold`
- `CensorManager._semantic_search` → `sqlite_censor_semantic_search`
- `EpisodeManager._vector_temporal_search` → `sqlite_vector_temporal_search`
- `EpisodeManager._end` → `_end_tz_safe` (timezone-aware datetime subtraction)
- `WorkingMemoryManager._get_or_create` → `_sqlite_get_or_create` (no `pg_insert`)
- `FactManager._create_graph_edge` → `_sqlite_create_graph_edge` (no `pg_insert`)
- `Brain._query` → `_sqlite_query_inner` (pure-Python vector + keyword search)
- `Brain._auto_link` → `_sqlite_auto_link` (no `<=>` operator, no `pg_insert`)
- `Brain._delete_inner` → `_sqlite_delete_inner` (no raw schema-qualified SQL)

### `tests/conftest_postgres.py`
The original PostgreSQL-based conftest, preserved for reference. Identical to the current `conftest.py`.

---

## Conftest.py — Current State

The current `conftest.py` is fully PostgreSQL-dependent:

```python
@pytest_asyncio.fixture(scope="session")
async def db():
    """Session-scoped database connection pool."""
    settings = Settings()
    database = Database(settings)    # ← connects to real Postgres
    await database.connect()
    yield database
    await database.disconnect()

@pytest_asyncio.fixture
async def session(db):
    """Function-scoped session with transaction rollback isolation."""
    async with db.engine.connect() as conn:   # ← Postgres engine
        trans = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        yield session
        await session.close()
        await trans.rollback()                # ← SAVEPOINT isolation
```

**Fixture dependency graph:**
```
db (root — real Postgres)
├── session (SAVEPOINT isolation per test)
├── mock_embeddings (pure Python, no DB)
├── heart (depends on db + mock_embeddings)
│   ├── heart_with_admission
│   ├── heart_with_strict_admission
│   └── heart_with_shadow_admission
└── seed_guardrails (depends on session)
```

---

## Recommended Mock Strategy

**Chosen approach: SQLite in-memory with monkey-patches (not full mocking)**

Rationale: The tests verify real business logic (learn_fact, recall_deep, episode lifecycle,
decision recording, etc.). Fully mocking `Heart` or `Brain` would hollow out the tests.
SQLite in-memory preserves the logic while removing the Postgres dependency.

**Why not SQLite natively?**
SQLite lacks: PostgreSQL schemas (`brain.*`, `heart.*`), pgvector `<->` / `<=>` operators,
`JSONB` operators, `::jsonb` casts, `pg_insert` ON CONFLICT with `DO UPDATE`, recursive CTEs,
`pg_sleep()`, `pg_extension`, `information_schema` catalog tables.

All of these except the last two have been addressed in `sqlite_compat.py` and `sqlite_patches.py`.

### Required conftest.py Changes

Replace the current `conftest.py` with a version that:

1. **Detects the backend** via env var `NOUS_TEST_DB=sqlite|postgres` (default: `sqlite`):

```python
import os
USE_POSTGRES = os.environ.get("NOUS_TEST_DB", "sqlite") == "postgres"
```

2. **For SQLite mode** — wire `TestDatabase` and install patches:

```python
@pytest_asyncio.fixture(scope="session")
async def db():
    if USE_POSTGRES:
        # original postgres path
        ...
    else:
        from tests.sqlite_compat import (
            create_test_engine, create_tables,
            install_sqlite_defaults, install_array_deserializer,
            patch_model_columns_for_sqlite, TestDatabase,
        )
        from tests.sqlite_patches import install_all_patches

        patch_model_columns_for_sqlite()   # must be before create_all
        install_sqlite_defaults()
        install_array_deserializer()
        install_all_patches()

        engine = await create_test_engine()
        await create_tables(engine)
        db = TestDatabase(engine)
        yield db
        await db.disconnect()
```

3. **session fixture** — works unchanged (uses `db.engine`). For SQLite the SAVEPOINT
rollback pattern still works with aiosqlite.

4. **heart / heart_with_* fixtures** — unchanged; they accept any `Database`-compatible object.

5. **seed_guardrails** — unchanged; uses ORM which works on both backends.

---

## Detailed Per-File Classification

### CLEAN (92 files) — No DB dependency, already pass without PostgreSQL

| File | Notes |
|------|-------|
| test_action_gate.py | Pure unit tests, monkeypatch only |
| test_admission.py | Pure scoring logic, mocks |
| test_anthropic_client.py | SDK payload tests, no DB |
| test_anti_hallucination.py | Prompt assembly, mocks |
| test_budget_scaling.py | Budget math, mocks |
| test_builtin_tools.py | bash/read/write tools, no DB |
| test_calibration.py† | *Actually uses `session`* — see NEEDS_MOCK |
| test_causal_tracing.py | Event ID logic, no DB |
| test_compaction.py | Token estimation, no DB |
| test_compaction_phase2.py | Message compression, mocks |
| test_config_critic_injection.py | Settings validation |
| test_config_search.py | Search config, mocks |
| test_context_dual_track.py | Context assembly, mocks |
| test_context_logger.py | Logging calls, no DB |
| test_context_quality.py | Text overlap metrics, mocks |
| test_correlation.py | Statistical math |
| test_critic.py | Critic routing, mocks |
| test_critic_integration.py | Critic LLM, mocks |
| test_decay_profiles.py | Score decay functions |
| test_decision_reviewer.py | Review logic, mocks |
| test_dedup.py | Dedup algorithms |
| test_drift_detection.py | Drift scoring, mocks |
| test_episode_compaction_collapse.py | Summary logic, mocks |
| test_episode_id_injection.py | Middleware logic |
| test_event_bus.py | Event bus with mocks |
| test_event_bus_observability.py | Trace IDs, mocks |
| test_execution_integrity.py | Action gate logic |
| test_execution_ledger.py | Ledger format, mocks |
| test_f025_amnesia_prevention.py | Dedup during learning, mocks |
| test_f025_chunked.py | Chunked processing |
| test_f025_dedup.py | Dedup heuristics, mocks |
| test_f025_transcript.py | Transcript parsing |
| test_f031_consolidation.py | Memory consolidation, mocks |
| test_f036_cache_dashboard.py | Cache stats |
| test_f036_cache_optimizer.py | Cache optimization |
| test_f036_runner.py | Runner instrumentation |
| test_f036_schema_tier.py | Schema tiering |
| test_f036_tool_cache.py | Tool cache, mocks |
| test_f038_context_fixes.py | Context assembly fixes, mocks |
| test_fact_graph_linker.py | Fact-decision linking, mocks |
| test_frames.py | Frame selection, mocks |
| test_guardrails.py | Guardrail logic, mocks |
| test_handlers_init.py | Handler registration, mocks |
| test_heartbeat.py | Heartbeat intervals, mocks |
| test_heartbeat_dynamic.py | Dynamic heartbeat, mocks |
| test_heartbeat_intelligent.py | Intelligent checks, mocks |
| test_heartbeat_isolation.py | Isolation modes, mocks |
| test_heartbeat_lifecycle.py | Lifecycle events |
| test_heartbeat_tuner.py | Tuning logic |
| test_identity.py | Identity manager, mocks |
| test_intent.py | Intent classification |
| test_layer_critic_skills.py | Critic skill injection, mocks |
| test_metadata_degrade.py | Metadata degradation |
| test_mmr.py | MMR algorithm, mocks |
| test_mmr_integration.py | MMR integration, mocks |
| test_model_aware_compaction.py | Compaction logic |
| test_noise_reduction.py | Dedup filtering, mocks |
| test_outcome_detector.py | Outcome classification, mocks |
| test_parse_llm_json.py | JSON parsing |
| test_pre_prune_extraction.py | Pre-pruning |
| test_procedure_learner.py | Procedure learning, mocks |
| test_quality.py | Quality scoring |
| test_relevance_filter.py | Relevance filtering, mocks |
| test_rest_ledger.py | Ledger API (no DB fixtures) |
| test_rrf_search.py | RRF algorithm, mocks |
| test_rubric.py | Rubric evaluation, mocks |
| test_rubric_dashboard.py† | *Uses `db`* — see NEEDS_MOCK |
| test_rubric_evolver.py | Rubric evolution, mocks |
| test_rubric_rest.py | Rubric REST, mocks |
| test_rubric_schemas.py | Schema validation |
| test_run_python.py | Python execution, mocks |
| test_runner.py | Agent runner, mocks |
| test_runner_fork.py | Fork mechanism, mocks |
| test_schedules.py | Schedule parsing, mocks |
| test_scored_wrapper.py | Score wrapper |
| test_search_providers.py | Search routing, mocks |
| test_search_router.py | Search routing, mocks |
| test_skill_parser.py | Skill spec parsing, mocks |
| test_sleep_handler.py | Sleep handler, mocks |
| test_smart_compress.py | Compression algorithm |
| test_staleness_penalty.py | Staleness penalties, mocks |
| test_streaming.py | Stream handling, mocks |
| test_streaming_keepalive.py | Keepalive, mocks |
| test_telegram_formatting.py | Message formatting, mocks |
| test_telegram_tools.py | Tool integration, mocks |
| test_time_parser.py | Time parsing, mocks |
| test_tool_cache.py | Tool caching |
| test_tool_loop.py | Tool invocation, mocks |
| test_tools.py | Tool registry, mocks |
| test_topic_persistence.py | Topic storage |
| test_usage_tracker.py | Usage tracking, mocks |
| test_usage_tracking_enhanced.py | Enhanced tracking |
| test_web_tools.py | Web tools, mocks |

*† Files marked with † are misclassified in the table header; see the correct category below.*

---

### NEEDS_MOCK (28 files) — Use real DB fixtures; migrate to SQLite

These files pass `session`, `heart`, `db`, or `heart_with_*` as fixture parameters. They test
real business logic via the Heart/Brain ORM layer. The SQLite backend + existing patches in
`sqlite_patches.py` should handle all of them.

**Effort: Trivial per file** once `conftest.py` is updated — no test code changes needed.

| File | Fixtures Used | Key Operations | Estimated Effort |
|------|---------------|----------------|------------------|
| test_abandoned_filtering.py | `heart`, `session` | Brain query filters, episode filtering | Trivial |
| test_admission_integration.py | `heart`, `heart_with_*`, `session` | Fact admission gate behavior | Trivial |
| test_brain.py | `seed_guardrails`, `session` | Decision record/query/link; also uses `text()` for edge verification† | Moderate |
| test_calibration.py | `session` | Brier score computation, decision records | Trivial |
| test_censors.py | `heart`, `session` | Censor add/check/list | Trivial |
| test_cognitive_layer.py | `heart`, `session` | Pre-turn context building, deliberation | Trivial |
| test_compaction_phase3.py | `heart`, `session` | JSONB message persistence in episodes | Trivial |
| test_context.py | `heart`, `session` | Context budget allocation, vector recall | Trivial |
| test_context_smart.py | `heart`, `session` | Frame selection, context dedup | Trivial |
| test_deliberation.py | `session` | Deliberation lifecycle with decision | Trivial |
| test_episodes.py | `db`, `heart`, `session` | Episode CRUD, lifecycle | Trivial |
| test_fact_enhancements.py | `session` | Contradiction detection, provenance | Trivial |
| test_facts.py | `heart`, `session` | Fact learn/recall/update | Trivial |
| test_frame_tagged_encoding.py | `db`, `session` | Frame-specific memory encoding | Trivial |
| test_heart.py | `db`, `heart`, `session` | Full episode + fact integration | Trivial |
| test_identity_api.py | `db` | Identity REST endpoints (mocked HTTP) | Trivial |
| test_mcp.py | `heart`, `session` | MCP protocol, partly mocked | Trivial |
| test_models.py | `session` | ORM model relationships/cascading | Trivial |
| test_monitor.py | `heart`, `session` | Post-turn self-assessment queries | Trivial |
| test_procedures.py | `heart`, `session` | Procedure store/retrieve/search | Trivial |
| test_procedures_active_filter.py | `session` | Procedure active flag filtering | Trivial |
| test_rest.py | `heart`, `session` | REST API endpoints (Anthropic mocked) | Trivial |
| test_rest_dashboard.py | `db`, `heart` | Dashboard REST aggregations | Trivial |
| test_rubric_dashboard.py | `db` | Rubric dashboard DB queries | Trivial |
| test_spreading_activation.py | `session` | Graph activation spreading | Trivial |
| test_temporal_recall.py | `heart`, `session` | Time-based recall (partly mocked) | Trivial |
| test_tiered_context.py | `db`, `heart` | Context tiering | Trivial |
| test_working_memory.py | `heart`, `session` | Working memory CRUD | Trivial |

†`test_brain.py` has one `text()` call to verify auto-linked edges exist in `brain.graph_edges`.
This query uses schema-qualified table name and will need the SQL rewriting hook from
`sqlite_compat.py` or replacement with an ORM query. See COMPLEX section.

---

### COMPLEX (2 files) — DB fixtures + raw schema-qualified SQL

These files use `text()` with schema-qualified tables or PG-specific SQL that the generic SQL
rewriting hook in `sqlite_compat.py` may not fully handle. Each needs a targeted fix.

**Effort: Moderate per file.**

#### `test_brain.py`
- **Fixture:** `seed_guardrails`, `session`
- **PG pattern:** One `text()` call at line ~167:
  ```python
  text(
      "SELECT * FROM brain.graph_edges "
      "WHERE (source_id = :id1 AND target_id = :id2) "
      "   OR (source_id = :id2 AND target_id = :id1)"
  )
  ```
- **Fix:** The SQL rewriting hook in `sqlite_compat.py` already strips schema prefixes
  (`brain.graph_edges → graph_edges`). This query should work after schema remapping is active.
  Verify by running the test with SQLite backend. If it fails, replace with an ORM query using
  `select(GraphEdge).where(...)`.
- **Effort:** Trivial (rewriting hook likely handles it; verify only)

#### `test_graph_linker.py`
- **Fixture:** `session`
- **PG pattern:** `text("ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS graph_edges_relation_check")`
- **Fix:** SQLite does not support `DROP CONSTRAINT`. This test setup hack should be replaced:
  - Either skip this ALTER in SQLite mode: `if not USE_POSTGRES: skip_constraint_drop()`
  - Or use SQLAlchemy DDL: detect dialect and conditionally execute
  - The constraint being dropped is for testing invalid `relation` values; SQLite won't enforce it
    anyway, so the test body remains valid without the setup SQL
- **Effort:** Trivial (one conditional skip)

---

### PG_SPECIFIC (3 files) — Cannot run without PostgreSQL

These files test PostgreSQL-specific features or use syntax that has no SQLite equivalent.
They should be **marked `@pytest.mark.postgres_only`** and skipped in SQLite mode.

**Effort: Low (mark + skip decorator; no test logic changes).**

#### `test_database.py`
- **Fixture:** `db`, `session`
- **PG patterns:**
  - `SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')` — Postgres system catalog
  - `SELECT schema_name FROM information_schema.schemata` — Postgres information schema
  - `SELECT table_schema, table_name FROM information_schema.tables` — ditto
  - `'{}' ::jsonb` — explicit Postgres type cast syntax
  - `SELECT pg_sleep(0.05)` — Postgres sleep function
  - Tests `updated_at` trigger auto-update (Postgres-specific triggers)
- **Purpose:** Verifies that the Postgres schema, extensions, and triggers are correctly installed.
  This is a schema validation test — inherently Postgres-only.
- **Recommendation:** Mark `@pytest.mark.postgres_only`. Run in CI with real Postgres only.

#### `test_admission_dashboard.py`
- **Fixture:** `db`, `heart_with_shadow_admission`
- **PG patterns:**
  - `text("SELECT ... FROM information_schema.columns WHERE ...")` — Postgres information schema
  - `assert row.data_type == "jsonb"` — asserts JSONB column type
  - `::vector` cast in raw SQL queries
- **Purpose:** Verifies the `admission_scores` JSONB column exists and is typed correctly.
  Also tests vector-similarity query paths.
- **Recommendation:** Mark `@pytest.mark.postgres_only`. The JSONB/vector type assertions are
  meaningless on SQLite. The logic under test (scoring) is covered by `test_admission.py` (CLEAN).

#### `test_dashboard_queries.py`
- **Fixture:** `session`
- **PG patterns:**
  - `::jsonb` cast: `':data::jsonb'` in INSERT statements
  - All inserts use schema-qualified tables (`nous_system.agents`, `brain.decisions`,
    `heart.facts`, `heart.episodes`, `brain.graph_edges`, `nous_system.events`)
  - Complex `GROUP BY` and window function queries
- **Purpose:** Tests dashboard analytics aggregation queries by inserting known data and
  verifying query results. The `::jsonb` cast makes raw SQL non-portable.
- **Recommendation:** Mark `@pytest.mark.postgres_only`. Alternatively, refactor inserts
  to use ORM models (which work on SQLite) and replace `::jsonb` cast with `cast(:data, JSON)`.
  This would make it fully SQLite-compatible but requires moderate refactoring effort.
- **Effort (if keeping PG_SPECIFIC):** Low. **Effort (if migrating):** Moderate.

---

## Postgres-Specific Patterns Reference

| Pattern | Files | SQLite Equivalent |
|---------|-------|-------------------|
| `<->` (L2 distance operator) | Source code | `cosine_similarity()` in `sqlite_patches.py` |
| `<=>` (cosine distance) | Source code | `cosine_similarity()` in `sqlite_patches.py` |
| `::vector` cast | `test_admission_dashboard.py` | Not applicable (test is PG-only) |
| `::jsonb` cast | `test_dashboard_queries.py` | `cast(:val, JSON)` or ORM insert |
| `pg_insert` (INSERT ON CONFLICT) | Source code | ORM-based upsert in `sqlite_patches.py` |
| `hybrid_search()` (raw SQL) | Source code (heart.search) | `sqlite_hybrid_search()` in `sqlite_patches.py` |
| `batch_fetch_embeddings()` | Source code | `sqlite_batch_fetch_embeddings()` in `sqlite_patches.py` |
| Recursive CTE (`WITH RECURSIVE`) | Source code (facts) | Iterative chase in `sqlite_patches.py` |
| `gen_random_uuid()` server default | ORM models | `install_sqlite_defaults()` in `sqlite_compat.py` |
| `now()` / `NOW()` server default | ORM models | `install_sqlite_defaults()` + SQL rewriter |
| `make_interval(hours => :n)` | Source code | SQL rewriter in `sqlite_compat.py` |
| `pg_extension` catalog | `test_database.py` | No equivalent — PG-only test |
| `information_schema.*` | `test_database.py`, `test_admission_dashboard.py` | No equivalent — PG-only test |
| `pg_sleep()` | `test_database.py` | No equivalent — PG-only test |
| DB triggers (`updated_at`) | `test_database.py` | No equivalent — PG-only test |
| Schema prefixes (`brain.`, `heart.`, `nous_system.`) | Source code + some tests | SQL rewriter strips them |
| `ARRAY` type | ORM models | `JSONEncodedList` in `sqlite_compat.py` |
| `JSONB` type | ORM models | `@compiles(JSONB, "sqlite")` → JSON |
| `Vector(1536)` | ORM models | `JSONEncodedVector` in `sqlite_compat.py` |
| `stddev()` aggregate | Source code | `StddevAggregate` class in `sqlite_compat.py` |
| `power()` function | Source code | Registered in `sqlite_compat.py` |
| HNSW/ivfflat indexes | ORM models (`create_indexes`) | Not created in SQLite (no-op) |
| GIN full-text indexes | ORM models | Not created in SQLite (no-op) |

---

## Specific conftest.py Changes Needed

### New `conftest.py` skeleton

```python
"""Test fixtures — supports SQLite in-memory (default) or real Postgres."""

import hashlib
import os
import random

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from nous.config import Settings
from nous.storage.models import Guardrail

USE_POSTGRES = os.environ.get("NOUS_TEST_DB", "sqlite") == "postgres"


class MockEmbeddingProvider:
    # ... (unchanged from current conftest.py)


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def db():
    if USE_POSTGRES:
        from nous.storage.database import Database
        settings = Settings()
        database = Database(settings)
        await database.connect()
        yield database
        await database.disconnect()
    else:
        from tests.sqlite_compat import (
            create_test_engine, create_tables,
            install_sqlite_defaults, install_array_deserializer,
            patch_model_columns_for_sqlite, TestDatabase,
        )
        from tests.sqlite_patches import install_all_patches
        patch_model_columns_for_sqlite()
        install_sqlite_defaults()
        install_array_deserializer()
        install_all_patches()
        engine = await create_test_engine()
        await create_tables(engine)
        test_db = TestDatabase(engine)
        yield test_db
        await test_db.disconnect()


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest_asyncio.fixture
async def session(db):
    async with db.engine.connect() as conn:
        trans = await conn.begin()
        sess = AsyncSession(bind=conn, expire_on_commit=False)
        yield sess
        await sess.close()
        await trans.rollback()


# ---------------------------------------------------------------------------
# heart / brain / admission fixtures (unchanged)
# ---------------------------------------------------------------------------
# ... all existing heart fixtures remain the same
```

### pytest.ini / pyproject.toml — add marker

```toml
[tool.pytest.ini_options]
markers = [
    "postgres_only: requires a real PostgreSQL connection (skip in sqlite mode)",
]
```

### conftest.py — auto-skip postgres_only tests in SQLite mode

```python
def pytest_runtest_setup(item):
    if not USE_POSTGRES and item.get_closest_marker("postgres_only"):
        pytest.skip("Requires PostgreSQL (set NOUS_TEST_DB=postgres)")
```

---

## Suggested Implementation Phases

### Phase 1 — Wire SQLite backend into conftest.py (1–2 hours)
**Impact:** Unlocks ~28 NEEDS_MOCK files immediately.

1. Copy `tests/sqlite_compat.py` and `tests/sqlite_patches.py` into version control (they're
   currently untracked — verify they match the analysis above before committing).
2. Update `conftest.py`:
   - Add `USE_POSTGRES` env var check
   - Rewrite `db` fixture with SQLite path using `TestDatabase`
   - Add `pytest_runtest_setup` skip hook for `postgres_only` marker
   - Add `postgres_only` marker to `pyproject.toml`
3. Mark `test_database.py` and `test_admission_dashboard.py` with `@pytest.mark.postgres_only`.
4. Run the full suite: `uv run pytest tests/ -v --ignore=tests/test_dashboard_queries.py`
5. Fix any remaining failures.

### Phase 2 — Fix COMPLEX files (1–2 hours)
**Impact:** Migrates `test_brain.py` and `test_graph_linker.py`.

1. **test_brain.py:** Verify the `brain.graph_edges` text() query works via the SQL rewriting
   hook. If not, replace with:
   ```python
   from nous.storage.models import GraphEdge
   from sqlalchemy import select, or_
   result = await session.execute(
       select(GraphEdge).where(
           or_(
               (GraphEdge.source_id == id1) & (GraphEdge.target_id == id2),
               (GraphEdge.source_id == id2) & (GraphEdge.target_id == id1),
           )
       )
   )
   ```
2. **test_graph_linker.py:** Skip the `ALTER TABLE ... DROP CONSTRAINT` in SQLite mode:
   ```python
   if USE_POSTGRES:
       await conn.execute(text("ALTER TABLE brain.graph_edges DROP CONSTRAINT ..."))
   ```

### Phase 3 — Decide on test_dashboard_queries.py (1–3 hours)
**Options:**
- **Option A (fast):** Mark `@pytest.mark.postgres_only`. Zero code changes.
- **Option B (thorough):** Refactor inserts to use ORM models; replace `::jsonb` cast;
  verify window functions work in SQLite. Migrates this coverage to run without Postgres.

Recommendation: Start with Option A, revisit if dashboard regression coverage becomes important.

### Phase 4 — CI integration
1. Add a CI job that runs `uv run pytest tests/` without any Postgres service (SQLite mode).
2. Keep the existing Postgres CI job for `@pytest.mark.postgres_only` tests.
3. Use `NOUS_TEST_DB=postgres pytest tests/ -m postgres_only` for the PG-only job.

---

## Known Risks and Caveats

1. **Semantic search quality differs:** SQLite uses cosine similarity over full table scans;
   Postgres uses HNSW index. Test assertions that depend on top-k ranking may need tolerance
   adjustments (e.g., `assert result[0].id in {id1, id2}` instead of `== id1`).

2. **Transaction isolation:** The SAVEPOINT-based rollback pattern works in SQLite via aiosqlite,
   but SQLite has weaker isolation guarantees. Rare test-order dependencies could emerge.

3. **Timezone handling:** SQLite returns naive datetimes; `sqlite_patches.py` already patches
   `EpisodeManager._end` and `sqlite_compat.py` provides `ensure_aware()`. Watch for any
   datetime comparison failures on other managers.

4. **JSONB field access:** Postgres allows `model.jsonb_col["key"]` via the `->` operator;
   SQLAlchemy ORM translates this. In SQLite, JSONB is stored as JSON text — JSON path queries
   may fail. Check any ORM queries that use JSONB operators.

5. **UUIDs:** Stored as 32-char hex strings in SQLite (no dashes). The `sqlite3.register_adapter`
   in `sqlite_compat.py` handles serialization; ensure UUID comparisons use `str(uuid)` or the
   ORM's UUID type.

6. **`patch_model_columns_for_sqlite()` must run before `create_all`:** It modifies SQLAlchemy
   Table metadata in-place. If models are imported before this is called, the column types won't
   be patched. Keep this as the first call in the `db` fixture.

---

## Files That Need `@pytest.mark.postgres_only`

```
tests/test_database.py
tests/test_admission_dashboard.py
tests/test_dashboard_queries.py   (unless refactored in Phase 3 Option B)
```

Add the marker at the **module level**:
```python
import pytest
pytestmark = pytest.mark.postgres_only
```

---

## Summary of Work Required

| Task | Files Affected | Effort | Phase |
|------|---------------|--------|-------|
| Commit `sqlite_compat.py` + `sqlite_patches.py` | 2 new files | Trivial | 1 |
| Update `conftest.py` with SQLite path | `conftest.py` | Low | 1 |
| Add `postgres_only` marker infra | `pyproject.toml`, `conftest.py` | Trivial | 1 |
| Mark PG-only tests | 2–3 files | Trivial | 1 |
| Fix `test_brain.py` text() query | 1 file | Trivial | 2 |
| Fix `test_graph_linker.py` ALTER TABLE | 1 file | Trivial | 2 |
| Decide on `test_dashboard_queries.py` | 1 file | Low–Moderate | 3 |
| Add SQLite-mode CI job | CI config | Low | 4 |

**Total estimated effort: 4–8 hours** to have 122 of 125 tests pass without PostgreSQL.
The remaining 3 are legitimately PostgreSQL-specific tests that verify schema installation.
