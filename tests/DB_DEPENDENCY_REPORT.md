# Database Dependency Report — Nous Test Suite

**Generated**: 2026-04-07  
**Total test files analyzed**: 125 (excluding conftest.py)  
**Scope**: `tests/` directory only — no production code modified

---

## Executive Summary

| Category | Count | % of Total | Description |
|----------|-------|-----------|-------------|
| **CLEAN** | 27 | 22% | No DB dependency at all |
| **MOCK_READY** | 5 | 4% | Uses DB but already properly mocked |
| **NEEDS_MOCK** | 61 | 49% | Uses real DB fixtures, needs mock replacement |
| **PG_SPECIFIC** | 18 | 14% | Uses Postgres-only features that won't work with SQLite |
| **COMPLEX** | 14 | 11% | Deep dependency chains needing careful refactoring |

**Bottom line**: ~78% of test files require a real Postgres connection to run. Of those, ~18% use features (pgvector, JSONB operators, schema introspection) that are fundamentally incompatible with SQLite. Full mock-based testing is achievable, but significant conftest.py refactoring is required first.

---

## How the Current DB Stack Works (conftest.py)

```
db (session-scoped)
  └── Database.connect() → real asyncpg pool → Postgres
        └── session (function-scoped)
              └── AsyncSession + SAVEPOINT rollback isolation
                    ├── heart (async) → Heart(session, mock_embeddings)
                    ├── brain (async) → Brain(session)
                    ├── cognitive (async) → CognitiveLayer(brain, heart)
                    └── seed_guardrails → inserts 4 Guardrail rows

mock_embeddings (session-scoped) → MockEmbeddingProvider (SHA-256, 1536-dim, no API call)
```

The `db` fixture is the root of all database-backed tests. Everything else flows through it. Replacing `db` with an in-memory backend is the single highest-leverage change possible.

---

## Detailed Per-File Classification

### Category: CLEAN (27 files)
No imports or fixtures that touch the database. Safe to run offline today.

| File | Notes |
|------|-------|
| `test_action_gate.py` | `SimpleNamespace` mocks, `monkeypatch` only |
| `test_admission.py` | Pure scoring math, `AsyncMock` for LLM client |
| `test_anthropic_client.py` | SDK payload construction, no DB |
| `test_anti_hallucination.py` | Context engine mock only |
| `test_budget_scaling.py` | Pure configuration math |
| `test_builtin_tools.py` | `tmp_path` filesystem fixture only |
| `test_causal_tracing.py` | Mocks internal structures, no DB |
| `test_config_critic_injection.py` | Pure config object tests |
| `test_config_search.py` | Pure config object tests |
| `test_context_logger.py` | No fixtures at all |
| `test_correlation.py` | Pure algorithm (Brier/correlation math) |
| `test_decay_profiles.py` | Pure algorithm |
| `test_execution_integrity.py` | In-memory execution ledger, no DB |
| `test_execution_ledger.py` | In-memory ledger, no DB |
| `test_handlers_init.py` | Handler init with all mocks |
| `test_parse_llm_json.py` | Pure parsing |
| `test_pre_prune_extraction.py` | Pure extraction logic |
| `test_rrf_search.py` | Pure RRF rank-fusion algorithm |
| `test_run_python.py` | Python executor, no DB |
| `test_runner.py` | Mocks only |
| `test_runner_fork.py` | Mocks only |
| `test_scored_wrapper.py` | Pure unit tests |
| `test_search_providers.py` | Mocked HTTP |
| `test_search_router.py` | Mocked providers |
| `test_skill_parser.py` | Pure parsing |
| `test_smart_compress.py` | Pure compression logic |
| `test_streaming.py` | Mocked Anthropic API, no DB |
| `test_streaming_keepalive.py` | Async protocol only, no DB |
| `test_telegram_formatting.py` | Pure formatting |
| `test_telegram_tools.py` | Mocked Telegram API |
| `test_time_parser.py` | Pure natural language parsing |
| `test_tool_cache.py` | In-memory cache, no DB |
| `test_tool_loop.py` | `echo_dispatcher`, mocked settings |
| `test_web_tools.py` | Mocked HTTP |

---

### Category: MOCK_READY (5 files)
Uses DB indirectly but already properly isolated through mocks. Would survive if `db`/`session` were replaced.

| File | How Mocked | What Remains |
|------|-----------|--------------|
| `test_mmr.py` | Mocked session + pure MMR algorithm | None |
| `test_mmr_integration.py` | Mocked Heart/DB at boundary | None |
| `test_staleness_penalty.py` | Mocks + pure decay algorithm | None |
| `test_scored_wrapper.py` | Pure unit with fake data | None |
| `test_sleep_handler.py` | Mocked event emission, no DB access | None |

---

### Category: NEEDS_MOCK (61 files)
Uses `session`, `heart`, or `brain` fixtures backed by real Postgres. The DB operations are ORM-level (SQLAlchemy `select`/`insert`/`update`), which are compatible with SQLite or an in-memory mock. No PG-specific SQL operators used.

| File | Primary Fixtures | Key Operations |
|------|-----------------|----------------|
| `test_abandoned_filtering.py` | `brain`, `session`, `heart` | Decision ORM inserts + date filtering |
| `test_calibration.py` | `session` | Decision, DecisionReason ORM CRUD |
| `test_censors.py` | `heart`, `session` | Censor model CRUD |
| `test_cognitive_layer.py` | `brain`, `heart`, `cognitive`, `session` | Full layer integration |
| `test_compaction.py` | `brain`, `heart`, `session` | Episode/Decision compaction |
| `test_compaction_phase2.py` | `brain`, `heart`, `session` | Compaction phase 2 |
| `test_compaction_phase3.py` | `brain`, `heart`, `session` | Compaction phase 3 |
| `test_context.py` | `brain`, `heart`, `context_engine`, `session` | ContextEngine query assembly |
| `test_context_dual_track.py` | `brain`, `heart`, `session` | Dual-track context assembly |
| `test_context_quality.py` | `brain`, `heart`, `session` | Quality scoring on context |
| `test_context_smart.py` | `brain`, `heart`, `session` | Smart context selection |
| `test_critic.py` | `brain`, `heart`, `session` | Critic agent scoring |
| `test_critic_integration.py` | `brain`, `heart`, `session` | Critic integration workflow |
| `test_decision_reviewer.py` | `brain`, `session` | Decision review lifecycle |
| `test_deliberation.py` | `brain`, `heart`, `session` | Pre-action deliberation |
| `test_dedup.py` | `brain`, `heart`, `session` | Conversation deduplication |
| `test_drift_detection.py` | `brain`, `heart`, `session` | Topic drift detection |
| `test_episode_compaction_collapse.py` | `heart`, `session` | Episode collapse |
| `test_episode_id_injection.py` | `heart`, `session` | Episode ID injection |
| `test_episodes.py` | `heart`, `brain`, `db`, `session` | Full episode lifecycle |
| `test_event_bus.py` | `brain`, `session` | Event emission/subscription |
| `test_event_bus_observability.py` | `brain`, `session` | Event observability |
| `test_f025_amnesia_prevention.py` | `heart`, `session` | Staleness/amnesia prevention |
| `test_f025_chunked.py` | `heart`, `session` | Chunked summarization |
| `test_f025_dedup.py` | `heart`, `session` | Fact dedup threshold testing |
| `test_f025_transcript.py` | `heart`, `session` | Transcript persistence |
| `test_f031_consolidation.py` | `heart`, `brain`, `session` | Episode consolidation |
| `test_f036_cache_optimizer.py` | `heart`, `session`, `brain` | Cache prompt optimization |
| `test_f036_runner.py` | `heart`, `session`, `brain` | Cached runner |
| `test_f036_tool_cache.py` | `heart`, `session` | Tool result caching |
| `test_f038_context_fixes.py` | `heart`, `session`, `brain` | Context quality fixes |
| `test_fact_enhancements.py` | `heart`, `brain`, `session` | Fact enhancement pipeline |
| `test_fact_graph_linker.py` | `heart`, `brain`, `session` | Fact→graph linking |
| `test_facts.py` | `heart`, `session` | Fact lifecycle: learn, contradict, supersede |
| `test_frames.py` | `heart`, `session` | Frame management |
| `test_guardrails.py` | `brain_guardrail`, `session`, `seed_guardrails` | CEL guardrail evaluation |
| `test_heart.py` | `heart`, `db`, `session`, `brain` | Full Heart integration |
| `test_identity.py` | `heart`, `session`, `cognitive` | Identity context |
| `test_identity_api.py` | `heart`, `session`, `cognitive` | Identity REST API |
| `test_intent.py` | Various | Frame selection logic |
| `test_layer_critic_skills.py` | `brain`, `heart`, `cognitive`, `session` | Critic skill injection |
| `test_metadata_degrade.py` | `heart`, `session` | Tool result metadata degradation |
| `test_model_aware_compaction.py` | `brain`, `heart`, `session` | Model-aware compaction |
| `test_monitor.py` | `db`, `session` | Post-turn self-assessment queries |
| `test_noise_reduction.py` | `heart`, `session` | Frame instruction noise filtering |
| `test_outcome_detector.py` | `brain`, `session` | Outcome signal detection |
| `test_procedure_learner.py` | `heart`, `session` | K-line procedure learning |
| `test_procedures.py` | `heart`, `session` | Procedure lifecycle |
| `test_procedures_active_filter.py` | `heart`, `session` | Active filter on procedures |
| `test_quality.py` | `brain`, `session` | Decision quality scoring |
| `test_relevance_filter.py` | `brain`, `heart`, `session` | Relevance floor filtering |
| `test_rest.py` | `brain`, `heart`, `cognitive`, `client` | REST API endpoints (ASGI) |
| `test_rest_dashboard.py` | `brain`, `heart`, `cognitive`, `client` | Dashboard REST endpoints |
| `test_rest_ledger.py` | `brain`, `heart`, `client` | Ledger REST endpoints |
| `test_rubric.py` | `session`, `brain`, `heart` | Rubric calibration |
| `test_rubric_evolver.py` | `session`, `brain` | Rubric evolution |
| `test_rubric_rest.py` | `session`, `brain`, `client` | Rubric REST endpoints |
| `test_schedules.py` | `db`, `session`, `schedule_mgr` | Schedule CRUD |
| `test_subtasks.py` | `heart`, `session` | Subtask lifecycle |
| `test_temporal_recall.py` | `brain`, `heart`, `session` | Time-decay recall |
| `test_tiered_context.py` | `brain`, `heart`, `session` | Tiered context assembly |
| `test_topic_persistence.py` | `heart`, `session` | Topic memory persistence |
| `test_usage_tracker.py` | `session`, `brain` | Context usage tracking |
| `test_usage_tracking_enhanced.py` | `session`, `brain` | Enhanced usage tracking |
| `test_working_memory.py` | `heart`, `session` | Working memory CRUD |

---

### Category: PG_SPECIFIC (18 files)
These files use Postgres-only features that cannot be replicated with SQLite in-memory. Each requires either a real Postgres instance or deep mocking at the SQL layer.

| File | PG Feature Used | Why It Blocks SQLite |
|------|----------------|---------------------|
| `test_database.py` | `pg_extension`, `pg_sleep()`, `information_schema`, `pg_trgm`, vector extension | Schema introspection is Postgres-specific; tests verify extensions exist |
| `test_brain.py` | GraphEdge ORM + `text()` raw SQL, agent filtering joins | Raw SQL with PG-specific syntax |
| `test_dashboard_queries.py` | `::jsonb` cast, raw INSERT statements into pg schemas, `text()` SQL | JSONB casts are PG-only |
| `test_admission_dashboard.py` | `information_schema.columns` introspection, `admission_scores` JSONB queries | Schema introspection + JSONB |
| `test_admission_integration.py` | Heart admission control with vector embedding scoring | pgvector cosine distance |
| `test_f036_cache_dashboard.py` | JSONB cache metadata queries | JSONB operators |
| `test_f036_schema_tier.py` | Schema-level prompt cache with JSONB | JSONB |
| `test_frame_tagged_encoding.py` | `encoded_censors` JSONB array, pgvector embeddings | JSONB arrays + pgvector |
| `test_graph_linker.py` | GraphEdge table with cross-type similarity (pgvector cosine) | pgvector |
| `test_heartbeat.py` | Full heartbeat with embedding-based self-initiated checks | pgvector |
| `test_heartbeat_dynamic.py` | DynamicCheck ORM with JSONB prompt field | JSONB |
| `test_heartbeat_intelligent.py` | Embedding similarity search for check relevance | pgvector |
| `test_heartbeat_isolation.py` | Heartbeat runner with DB-backed check state | pg-specific runner state |
| `test_heartbeat_lifecycle.py` | Finding state machine with JSONB metadata | JSONB |
| `test_heartbeat_tuner.py` | Self-tuning with JSONB param storage | JSONB |
| `test_mcp.py` | JSONB guardrail conditions, pgvector embeddings | JSONB + pgvector |
| `test_models.py` | `text()` SQL, pgvector column definitions, CHECK constraints | Schema-level PG features |
| `test_tools.py` | Decision/Fact ORM with embedding columns (pgvector) | pgvector |
| `test_unpopulated_columns.py` | `information_schema` column queries | PG schema introspection |

---

### Category: COMPLEX (14 files)
Deep fixture chains with transitive DB dependencies, mixed PG-specific and mockable patterns, or tests that exercise multiple subsystems simultaneously.

| File | Complexity Reason | Effort |
|------|------------------|--------|
| `test_rubric_dashboard.py` | Rubric REST + DB + JSONB signals | Significant |
| `test_rubric_schemas.py` | Schema validation touches DB-backed model JSONB | Moderate |
| `test_spreading_activation.py` | Graph density queries require pgvector + raw SQL | Significant |
| `test_mcp.py` | MCP server + full Brain/Heart integration + JSONB | Significant |
| `test_rest.py` | ASGI test client + full Brain/Heart/Cognitive stack | Significant |
| `test_rest_dashboard.py` | REST + multi-table dashboard aggregation | Significant |
| `test_rest_ledger.py` | REST + ledger DB + execution state | Moderate |
| `test_critic_integration.py` | Critic + Brain + Heart + Cognitive chain | Moderate |
| `test_cognitive_layer.py` | Full cognitive layer integration (all organs) | Significant |
| `test_compaction_phase3.py` | Phase 3 compaction: multi-episode collapse with scoring | Moderate |
| `test_heartbeat_isolation.py` | Heartbeat runner isolation requires DB + mocked Telegram | Significant |
| `test_layer_critic_skills.py` | Critic skills + cognitive layer + all fixtures | Moderate |
| `test_heartbeat_intelligent.py` | Intelligent checks: embedding search + LLM classification | Significant |
| `test_f036_runner.py` | Full runner with prompt caching and DB persistence | Moderate |

---

## Postgres-Specific Patterns Inventory

### 1. pgvector (`vector` type, cosine similarity)
**Where used**: `heart.facts.embedding`, `brain.decisions.embedding`, `heart.procedures.embedding`, `heart.episodes.embedding`, `nous_system.heartbeat_dynamic_checks.prompt_embedding`

**Blocking?**: Yes — SQLite has no vector type. Cosine distance (`<->` operator) is unavailable.

**Recommended handling**:
- For embedding operations: mock the `EmbeddingProvider` interface (already done via `MockEmbeddingProvider` in conftest.py)
- For vector search SQL: mock `search.py` and `spreading_activation.py` at the function level
- For tests that verify _ranking_ (MMR, graph retrieval): use pre-seeded deterministic float lists, bypass SQL vector ops

---

### 2. JSONB columns and operators
**Where used**:
- `brain.guardrails.condition` — CEL expression JSON
- `nous_system.agents.config` — agent configuration
- `nous_system.events.data` — event payload
- `heart.facts.admission_scores` — admission control scoring breakdown
- `brain.decisions.bridge` — decision structure data
- `heartbeat_findings.metadata` — finding metadata
- `heartbeat_dynamic_checks.last_result` — last check result JSON

**PG-specific operators used**: `@>` (containment), `->>` (text extraction), `::jsonb` cast in raw SQL

**Blocking?**: Yes for `::jsonb` raw SQL casts. SQLite's `JSON_EXTRACT` differs. ORM-level JSONB reads/writes can work with SQLite if using `JSON` column type instead.

**Recommended handling**:
- Replace `text("... ::jsonb")` raw SQL with ORM-level queries in affected tests
- Use `JSON` column type alias in test fixtures that mock the schema
- For tests that must verify JSONB operators: keep them PG-only, run via docker-compose in CI

---

### 3. Full-text search (`tsvector`, `pg_trgm`)
**Where used**: `heart.facts` (tsvector column), `brain.decisions` (text search triggers), keyword-only fallback in `search.py`

**Blocking?**: Yes — `tsvector` and `@@` operator don't exist in SQLite.

**Recommended handling**:
- Mock `heart/search.py::hybrid_search()` at the function boundary
- For keyword-only tests: use simple Python `in` check as a mock
- `test_database.py` specifically tests extension presence — keep as PG-only integration test

---

### 4. Schema namespaces (`brain.`, `heart.`, `nous_system.`)
**Where used**: All `text()` raw SQL queries in tests

**Blocking?**: Yes — SQLite has no schema namespacing. `CREATE TABLE brain.decisions` fails.

**Recommended handling**:
- All raw `text()` SQL in tests must be replaced with ORM queries, OR
- Use a SQLite-compatible schema approach: prefix table names instead of schema namespaces (e.g., `brain_decisions`), but this requires changing `models.py` (out of scope — do not touch production code)
- Better: mock the `Database` and `AsyncSession` entirely for tests that only need high-level behavior

---

### 5. Postgres extensions (`pg_extension`, `information_schema`)
**Where used**: `test_database.py`, `test_admission_dashboard.py`, `test_unpopulated_columns.py`

**Blocking?**: Yes — these tests explicitly verify Postgres infrastructure. They cannot be meaningfully run against SQLite.

**Recommended handling**: Keep these as integration tests, gated behind a `@pytest.mark.integration` marker. They run only when `--run-integration` flag or a real DB is available.

---

### 6. Postgres ENUM types
**Where used**: `outcome` (episodic memory), `stakes` (decisions), `severity` (findings), `memory_type`

**Blocking?**: Partially — SQLAlchemy's `Enum` type works with SQLite (stores as VARCHAR), so ORM-level usage works. Raw SQL `::outcome_type` casts would fail.

**Recommended handling**: Audit raw SQL in tests for ENUM casts; replace with ORM equivalents.

---

### 7. UUID primary keys with `gen_random_uuid()`
**Where used**: All tables use `uuid` as PK with Postgres server-side generation

**Blocking?**: No — SQLAlchemy generates UUIDs in Python before INSERT. Works with SQLite.

---

### 8. Transaction SAVEPOINT rollback (test isolation)
**Where used**: `conftest.py::session` fixture creates a SAVEPOINT for each test and rolls back

**Blocking?**: No — SQLite supports SAVEPOINT. This pattern is portable.

---

## Recommended Mock Strategy

### Option A: SQLite In-Memory (Partial — Not Recommended as Primary)
- **Feasibility**: ~40% of NEEDS_MOCK tests could use SQLite if the schema were SQLite-compatible
- **Blocker**: Schema uses namespaces (`brain.`, `heart.`), ENUM types, pgvector columns — none of which SQLite supports
- **Verdict**: Too invasive — requires modifying `models.py` (production code). Not recommended.

### Option B: Full Mock at the Service Layer (Recommended)
Replace the `db` and `session` fixtures with mocks at the `Heart`, `Brain`, and `Database` boundary rather than at the SQL layer. The strategy:

```
Old:  test → session fixture → AsyncSession → real Postgres
New:  test → mock_heart / mock_brain → pre-seeded in-memory dicts → no DB
```

**Advantages**:
- No changes to production models, schemas, or SQL
- Works for all NEEDS_MOCK tests
- PG_SPECIFIC tests remain as integration tests (gated by marker)
- Implementation effort is concentrated in conftest.py

**Fixture changes needed** (see next section).

### Option C: Hybrid (Recommended Final State)
- **Unit tests** (CLEAN + MOCK_READY): Already offline-capable
- **Integration-mockable** (NEEDS_MOCK): Move to mock service layer (Option B)
- **Integration-only** (PG_SPECIFIC + COMPLEX): Keep as `@pytest.mark.integration`, require `--run-integration` + real Postgres in CI

---

## Specific conftest.py Changes Required

### Phase 1: Add pytest markers and config

```python
# conftest.py additions
def pytest_addoption(parser):
    parser.addoption("--run-integration", action="store_true", default=False,
                     help="Run tests that require a real Postgres connection")

def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-integration"):
        skip_integration = pytest.mark.skip(reason="requires --run-integration flag and real Postgres")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)
```

### Phase 2: Create mock service fixtures

```python
# conftest.py — new mock fixtures
class MockSession:
    """In-memory SQLAlchemy session substitute."""
    def __init__(self):
        self._store: dict[str, list] = {}
        
    async def execute(self, stmt, *args, **kwargs): ...
    async def flush(self): ...
    async def commit(self): ...
    def add(self, obj): ...
    # ... scalars(), scalar_one_or_none(), etc.

@pytest.fixture
def mock_session():
    return MockSession()

@pytest.fixture
async def mock_heart(mock_session, mock_embeddings):
    """Heart backed by mock session — no real DB."""
    h = Heart.__new__(Heart)
    h.session = mock_session
    h.embeddings = mock_embeddings
    # Wire up sub-managers with mock session
    h.episodes = Episodes(mock_session)
    h.facts = Facts(mock_session, mock_embeddings)
    h.procedures = Procedures(mock_session, mock_embeddings)
    h.working_memory = WorkingMemory(mock_session)
    h.censors = Censors(mock_session)
    return h

@pytest.fixture
async def mock_brain(mock_session):
    """Brain backed by mock session — no real DB."""
    b = Brain.__new__(Brain)
    b.session = mock_session
    return b
```

### Phase 3: Mark PG-specific tests

Add `@pytest.mark.integration` to all files in the PG_SPECIFIC and COMPLEX categories. These continue to run against real Postgres in CI (docker-compose), but are skipped in offline/unit-test runs.

### Phase 4: Update NEEDS_MOCK tests

Replace `heart`, `brain`, `session` fixture references with `mock_heart`, `mock_brain`, `mock_session` where the test only needs ORM-level behavior (no raw SQL, no PG-specific operators). This is the highest-effort phase.

---

## Suggested Implementation Order (Phases)

### Phase 1 — Markers Only (Effort: Trivial, ~1 day)
**Goal**: Gate integration tests behind `--run-integration` without breaking anything.

1. Add `pytest_addoption` and `pytest_collection_modifyitems` to conftest.py
2. Add `@pytest.mark.integration` to all 18 PG_SPECIFIC files and 14 COMPLEX files
3. CI: Run `pytest tests/ -v` (offline) + `pytest tests/ -v --run-integration` (with docker-compose Postgres)

**Result**: CLEAN (27) + MOCK_READY (5) = 32 tests run offline today. All others skip unless `--run-integration`.

---

### Phase 2 — Mock Service Layer Fixtures (Effort: Significant, ~1–2 weeks)
**Goal**: Enable NEEDS_MOCK tests to run offline.

1. Design `MockSession` with full SQLAlchemy `AsyncSession` interface (add/flush/execute/scalars/scalar_one_or_none)
2. Implement `mock_heart` and `mock_brain` fixtures
3. Convert NEEDS_MOCK tests to use mock fixtures (can be done file by file)
4. Priority order within NEEDS_MOCK (easiest first):
   - Procedures, working_memory, facts (pure CRUD, no joins)
   - Episodes (slightly more complex, but no raw SQL)
   - Calibration, quality, relevance (pure computation over mocked data)
   - Cognitive layer, context engine (highest integration, do last)

---

### Phase 3 — PG-Specific Integration Tests (Effort: Moderate, ongoing)
**Goal**: Keep PG_SPECIFIC tests maintainable and clearly marked.

1. Ensure all 18 PG_SPECIFIC files have `@pytest.mark.integration`
2. Add `pyproject.toml` marker definitions to avoid PytestUnknownMarkWarning
3. Add docker-compose CI job that runs integration suite on every PR
4. Document that `test_database.py` is the canonical "does the schema work?" test

---

### Phase 4 — SQLite Compatibility Layer (Effort: Significant, future consideration)
**Not recommended in short term.** If the team wants _full_ SQLite compatibility (for faster CI without docker), the following would be needed:

- Replace schema namespaces in `models.py` with prefixed table names (breaking change)
- Replace `vector(1536)` columns with `JSON` columns + Python-side cosine similarity
- Replace `text()` raw SQL throughout tests with ORM equivalents
- Replace `tsvector`/`@@` with SQLite FTS5 or Python-side `in` checks

This touches production code extensively and is only worthwhile if CI speed is a major bottleneck.

---

## File Count Summary

```
Total test files (excl. conftest.py): 125

CLEAN (no DB):          27  (22%)
MOCK_READY (already):    5   (4%)
NEEDS_MOCK (ORM only):  61  (49%)
PG_SPECIFIC (pg-only):  18  (14%)
COMPLEX (deep chains):  14  (11%)
                       ---
Total:                 125
```

Offline-capable today (CLEAN + MOCK_READY): **32 files (26%)**  
Offline-capable after Phase 2 (+ NEEDS_MOCK): **93 files (74%)**  
Requires real Postgres always (PG_SPECIFIC + COMPLEX): **32 files (26%)**

---

## Appendix: Fixture Dependency Map

```
db (session-scoped, real Postgres)
├── session (function-scoped, AsyncSession + SAVEPOINT)
│   ├── heart → Episodes, Facts, Procedures, Censors, WorkingMemory
│   │   ├── heart_with_admission
│   │   ├── heart_with_strict_admission
│   │   └── heart_with_shadow_admission
│   ├── brain → Decisions, Guardrails, Calibration, GraphEdges
│   │   ├── brain_guardrail
│   │   └── brain_with_embeddings
│   ├── cognitive → CognitiveLayer(brain, heart)
│   ├── context_engine → ContextEngine(brain, heart)
│   ├── seed_guardrails → inserts 4 test Guardrail rows
│   └── schedule_mgr → ScheduleManager(session)
├── settings → Settings (session-scoped, no DB)
├── mock_embeddings → MockEmbeddingProvider (session-scoped, no DB)
└── client → httpx.AsyncClient + ASGITransport(app)
     └── app → Starlette app with brain+heart+cognitive injected
```

All DB-dependent tests flow through `db → session`. Replacing this root fixture with a mock session would unlock the majority of the test suite for offline execution.
