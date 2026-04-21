# F051 Retrieval Evaluation Harness — Implementation Plan

**Date:** 2026-04-20
**Author:** orchestrator (Nous forge)
**Spec:** `docs/features/F051-retrieval-eval-harness.md`
**Status:** **v2.1 — approved after re-review** (arch `2a008453`, devil `0a22d40d`, python-pro `c43a8ca0`). Minor v2.1 deltas folded in; ready for implementation dispatch.

**Original review decisions:** arch `fd69ffe1`, devil `f5b5006e`, python-pro `5675e0ed`.

## v2.1 minor revisions (from re-review)

1. **[devil D]** `tasks.py::_rebuild` drops the `-v` flag on `docker compose down` — `down -v` could sweep the main `postgres_data` volume on some Compose versions. Use targeted `docker volume rm -f nous_eval_db_data` only.
2. **[arch + python-pro]** `_settings_for_eval_db` disable list expanded to include: `decision_review_enabled`, `correction_extraction_enabled`, `graph_backfill_enabled`, `rubric_outcome_detection_enabled`, `actionability_enabled`.
3. **[devil C]** Smoke corpus (`tests/fixtures/eval_smoke_corpus.jsonl`) MUST contain ≥1 item of each of the 5 memory types (fact, decision, episode, procedure, censor) so smoke exercises every pipeline branch.
4. **[python-pro]** `_build_heart_for_eval` is decorated `@asynccontextmanager` so `async with _build_heart_for_eval(db, settings) as heart:` works. Otherwise the `async with` needs an extra `await`, which is awkward.
5. **[devil A]** Refactor snapshot protocol: BEFORE the refactor commit, implementer must (a) run existing `tests/test_tools.py::test_recall_deep` on the current code, capture the formatted text output, (b) save to `tests/fixtures/recall_deep_text_snapshot.txt`, (c) commit this file FIRST. Then refactor commit adds `test_recall_deep_text_format_unchanged` that compares live output against the committed snapshot. This makes the "byte-identical" invariant verifiable at review time, not an implementer claim.
6. **[arch]** Known limitation note: `nous/heart/search.py:34-55` currently rebuilds `Settings()` from `os.environ` internally for `vector_weight` + `rrf_k` — so per-config A/B of those two knobs won't propagate through `run_matrix`'s `model_copy`-based override. **Phase 1 config matrix does not touch those knobs** (only `cross_encoder_enabled`, `mmr_enabled`, `graph_recall_enabled`, `query_expansion_enabled`), so this is acceptable. Documented as a known limitation in the report header. Fix is out of scope for F051 — a follow-up would refactor `search.py` to accept Settings as a parameter.
7. **[arch]** `decide_gate_f050` docstring documents the N=2-sources edge case: `require_majority_positive=True` with 2 gate-eligible sources reduces to "both sources must have positive delta", which is stricter than majority. Documented, not a bug.
8. **[arch]** `PipelineStats` gets a new field `n_per_type_errors: dict[str, int]` — Heart's per-type try/except at `heart.py:783-799` suppresses sub-search exceptions into warnings; surfacing the count in stats exposes silent-failures in eval reports.
9. **[python-pro]** Add `tests/eval/test_no_utcnow.py` as a grep-linter — `git ls-files nous/eval/ | xargs grep -l 'utcnow'` must return empty. Prevents regression to deprecated API.

## Review findings incorporated

**Convergent P1s across all three reviewers — all addressed below:**

1. **[arch P1-1 / devil P1-1] Heart.recall signature phantom.** Real signature at `nous/heart/heart.py:736` is `recall(query, limit, types, session)` — `session` is an `AsyncSession`, not `session_id`. The plan's `heart.recall(query=..., limit=..., session_id=None)` would TypeError on first call. **Fix:** use `types=None, session=None` kwargs.

2. **[arch P1-1 / devil P1-2] Heart.recall does NOT search `brain.decisions`.** `Heart._recall` at `heart.py:763` only searches `["episode", "fact", "procedure", "censor"]`. Brain decisions are queried in `nous/api/tools.py::recall_deep` at line 394 via `brain.query(query, limit=limit)`. **Fix:** extract the full `recall_deep` pipeline into a shared pure function `nous/api/retrieval_pipeline.py::run_recall_pipeline` that the harness calls directly (see new Files §A).

3. **[arch P1-1] Graph expansion / spreading activation / contradiction detection all live in `recall_deep`, not Heart.recall.** `settings.graph_recall_enabled`, `settings.cross_type_linking_enabled`, `settings.spreading_activation_enabled`, `settings.contradiction_detection` are read in `tools.py:365, 398, 403, 482`. Measuring against `Heart.recall` silently measures an incomplete pipeline — `graph_off` config becomes a no-op. **Fix:** same as above — extract pipeline.

4. **[arch P1-3 / devil P1-3] RuntimeConfig singleton hazard.** `vector_weight`, `rrf_k`, `cross_encoder_enabled` route through `RuntimeConfig.get()` which is a process-wide singleton. Per-config `Settings` overrides won't take effect for these three knobs when `_overrides` is populated. **Fix:** (a) harness startup calls `RuntimeConfig.reset()` to ensure empty `_overrides`; (b) harness never calls `RuntimeConfig.load_from_db(eval_db)` — the eval DB has no `nous_system.config` rows anyway, but we make this explicit in code. (c) `run_matrix` calls `RuntimeConfig.reset()` between configs.

5. **[arch P1-4] `agent_id` mismatch → silent all-zero metrics.** Corpus inherits prod `agent_id`; harness defaults were `"nous-eval"`; `WHERE agent_id = self.agent_id` returns `[]`. **Fix:** standardize on `agent_id="nous-eval-corpus"` for all ingested corpus rows; harness `EvalSettings.agent_id` defaults to the same value; verified at startup.

6. **[arch P1-5 / python-pro P1-related] Paired-delta math claim was wrong.** For per-qrel scalar metrics, paired-average ≡ mean-of-means (linearity of expectation). True for MRR, P@K, R@K, and nDCG@10 when aggregated by mean. What differs is variance/CI: paired CIs are tighter when conditions are correlated across qrels. **Fix:** remove "paired averaging reduces variance from per-qrel noise" claim from plan + spec; replace with accurate statement about tighter paired confidence intervals. Math functions unchanged — compute point estimates as mean-of-means (simpler, equivalent); add paired CI via bootstrap in Phase 2 if wanted.

7. **[arch P1-6] `EvalSettings.dsn()` incompatible with `Database(settings)`.** `database.py:19` reads `settings.db_url` (property). **Fix:** EvalSettings exposes `db_url` as a `@property`, not a `dsn()` method. Matches main Settings at `config.py:577`.

8. **[devil P1-4] Dockerfile.eval-db uses `postgres:17` but `sql/init.sql:10` runs `CREATE EXTENSION vector`.** Image build crashes at entrypoint. **Fix:** base image is `pgvector/pgvector:pg17`, matching docker-compose.yml:105.

9. **[devil P1-5] Named volume + baked image = stale fixtures on version bump.** Docker only copies image data INTO an empty named volume. Version bumping `NOUS_EVAL_FIXTURE_VERSION=v2026-Q3` silently keeps v2026-Q2 data. **Fix:** (a) `tasks.py::rebuild` command runs `docker volume rm nous_eval_db_data && docker compose --profile eval up -d nous-eval-db`; (b) on startup, the harness queries `nous_eval_meta.fixture_version` and compares to `NOUS_EVAL_FIXTURE_VERSION`; mismatch → error instructs operator to run `python -m nous.eval.tasks rebuild`.

10. **[devil P1-6] CRLF on `Dockerfile.eval-db.load.sh` breaks on Windows clone.** Fresh Windows clone without `.gitattributes` converts `*.sh` to CRLF; bash errors with `$'\r': command not found`. **Fix:** add `.gitattributes` declaring `*.sh text eol=lf`.

11. **[devil P1-7] `eval_runs` INSERT can block 30+s.** asyncpg connection without timeout. **Fix:** wrap in `asyncio.wait_for(_persist_run(...), timeout=5.0)`; on TimeoutError, log WARN and continue. Spec promise "never blocks" becomes enforceable.

12. **[devil P1-8] Integration-test smoke corpus with random embeddings.** Tests pass even when retrieval is broken because MRR is ~0 regardless. **Fix:** smoke corpus uses **hand-crafted OpenAI embeddings** for 10 items (captured once via actual embedding call, committed to the fixture JSONL as literal floats). Smoke tests then assert specific rank positions, not just "non-crash".

13. **[devil P1-9] Security: default password + `0.0.0.0` bind.** `NOUS_EVAL_DB_PASSWORD=nous_eval` + `ports: ["5433:5432"]` (binds all interfaces) = LAN-reachable. **Fix:** bind to `127.0.0.1:5433:5432` explicitly; startup warns if password is still the default `nous_eval`; docs call this out.

14. **[devil P1-10] `NOUS_PROD_DB_*` undeclared → ingest falls back to libpq defaults.** Risk of reading local dev DB as "prod". **Fix:** ingest.py fails fast if `NOUS_PROD_DB_HOST/PORT/USER/PASSWORD/NAME` are unset; mandatory check before any query.

15. **[devil P1-11] Ingest shares process-wide EventBus.** fact_extractor/episode_summarizer handlers could cascade writes to prod DB via singleton. **Fix:** ingest disables the EventBus entirely via `settings.event_bus_enabled=False` + passes a null EventBus instance to the fact_extractor handler (it will process inline, not via bus).

16. **[python-pro P1-1] `python -m nous.eval.retrieval` crashes — no such module.** Plan defined `cli.py` but invoked `python -m nous.eval.retrieval`. **Fix:** rename `cli.py` → `retrieval.py`; `ingest.py` and `rebuild.py` become top-level modules in `nous/eval/` with `if __name__ == "__main__": raise SystemExit(main())`.

17. **[python-pro P1-2] numpy is not a dep; over-engineering for 500 data points.** **Fix:** use `statistics.mean` + list comprehensions. Subsecond. Drop numpy from plan.

**P2s rolled into plan:**

- **[python-pro P2]** Pydantic-settings: use `SettingsConfigDict(env_prefix=...)` not plain dict (match main `nous/config.py:15`).
- **[python-pro P2]** Use `async with heart: ...` in `run_matrix` — Heart implements `__aenter__/__aexit__` at `heart.py:136-140`.
- **[python-pro P2]** Disable background handlers when building Heart for eval: `event_bus_enabled=False`, `fact_extraction_enabled=False`, `episode_summary_enabled=False`, `sleep_enabled=False`, `actionability_backfill_on_startup=False`, `heartbeat_enabled=False`, `schedule_enabled=False`, `subtask_enabled=False`.
- **[python-pro P2]** `datetime.now(tz=timezone.utc)` not `datetime.utcnow()` (deprecated in 3.12+).
- **[python-pro P2]** Drop `pytest-docker` dep — use existing pytest fixtures + socket preflight.
- **[python-pro P2]** `@pytest.mark.integration` needs registration in `pyproject.toml [tool.pytest.ini_options].markers` (check if already exists).
- **[arch P2 / devil P2]** Single-source regression check at N=20 per-source can noise-flap. **Fix:** tighten gate to require aggregate delta AND majority-of-sources delta positive. Explicit rule in report.decide_gate_f050.
- **[devil P2]** `NOUS_EVAL_FIXTURE_VERSION=latest` is a reproducibility hazard — change default to an explicit version string, but allow `latest` if operator opts in.
- **[arch P2]** LongMemEval replay write target — ingest must write replayed episodes/facts to the **eval DB scratch**, not the prod DB. ingest.py uses TWO separate DBs: reads from prod, writes replayed state to a fresh scratch DB which then becomes the image source.

---

## Scope (Phase 1 only — unchanged)

Ship harness dark: all code, `retrieval_pipeline` refactor, `Dockerfile.eval-db`, `docker-compose` profile, config, tests, docs. Operator runs fixture ingest + image push + hand-label review in Phase 2.

---

## Pipeline refactor (new from v2)

**Files §A.1 — NEW: `nous/api/retrieval_pipeline.py` (~250 LOC)**

Extracts the full recall pipeline from `nous/api/tools.py::recall_deep` into a pure async function returning structured results.

```python
from dataclasses import dataclass, field
from uuid import UUID
from typing import Any, Literal

@dataclass(frozen=True)
class PipelineResult:
    """Structured result from the full recall pipeline.

    Unlike the text output of recall_deep, this is machine-consumable.
    Caller can format it for LLM display OR score it against qrels.
    """
    id: UUID
    type: Literal["episode", "fact", "procedure", "censor", "decision"]
    description: str              # for display; summary for heart types, description for brain
    score: float                  # final post-pipeline score (CE-adjusted if CE on, else hybrid score)
    source: Literal["heart", "brain", "graph_expanded", "spreading_activation"] = "heart"
    edge_relation: str | None = None   # only set when source in ("graph_expanded","spreading_activation")
    contradicts: list[UUID] = field(default_factory=list)   # IDs this result contradicts (F022 Phase 3)

@dataclass(frozen=True)
class PipelineStats:
    """Metadata about which stages fired, for report diagnostics."""
    ce_reranked: bool
    mmr_applied: bool
    graph_expansion_used: bool
    spreading_activation_used: bool
    contradiction_checks_ran: bool
    n_heart_results: int
    n_brain_results: int
    n_graph_expanded: int

async def run_recall_pipeline(
    query: str,
    heart: Heart,
    brain: Brain,
    settings: Settings,
    limit: int = 10,
    memory_types: list[str] | None = None,
) -> tuple[list[PipelineResult], PipelineStats]:
    """Run the full retrieval pipeline.

    This is what the production `recall_deep` tool runs, but returning
    structured results instead of formatted text. Used by:
      - `tools.py::recall_deep` (formats to text for LLM)
      - `nous/eval/retrieval_runner.py` (scores against qrels)
    """
    # Mirror tools.py:320-509 logic but accumulate into PipelineResult objects.
    # No code duplication: tools.py:recall_deep becomes a thin wrapper that
    # calls run_recall_pipeline and formats the output.
```

**Refactor contract** — production behavior MUST be byte-identical:

1. Text output of `recall_deep` after refactor matches text output before refactor for the same inputs (regression tested via `tests/test_tools.py::test_recall_deep_text_format_unchanged`).
2. All `settings.*_enabled` reads happen inside `run_recall_pipeline`.
3. `RuntimeConfig.get()` reads move into `run_recall_pipeline`.
4. No new logs; no new exceptions surfaced.
5. `tools.py::recall_deep` becomes ~30 LOC: call pipeline, format results into the existing `{"content": [{"type": "text", "text": ...}]}` shape.

**Existing tests that exercise recall_deep:** `tests/test_tools.py`, `tests/test_heart.py`, `tests/test_action_gate.py`, `tests/test_execution_integrity.py`, `tests/test_execution_ledger.py`, `tests/test_streaming.py`, `tests/test_noise_reduction.py`, `tests/test_context_quality.py`, `tests/test_heartbeat_dynamic.py`, `tests/test_api_streaming_aggregated.py`.

**Refactor verification:** all 10 tests must pass unchanged post-refactor. One new test added: `test_recall_deep_delegates_to_pipeline`.

---

## Subagent assignment (3 parallel + 1 pre-requisite)

| Agent | agent_id | Files | LOC est. |
|---|---|---|---|
| **PREREQ: Refactor** | `nous-eval-impl-refactor` | `nous/api/retrieval_pipeline.py` (new), `nous/api/tools.py` (edit recall_deep) | ~280 net |
| **Core** | `nous-eval-impl-core` | `nous/eval/{config,source_registry,corpus_loader,qrels_loader,retrieval_runner,metrics,report,retrieval,rebuild,ingest_entry}.py` | ~1200 |
| **Infra** | `nous-eval-impl-infra` | `Dockerfile.eval-db`, `Dockerfile.eval-db.load.sh`, `docker-compose.yml` (edit), `.gitattributes` (new), `nous/eval/{tasks,ingest,ingest_longmemeval,probe_gen,hand_labels_draft}.py`, `sql/migrations/037_eval_runs.sql`, `nous/config.py` (edit) | ~900 |
| **Tests** | `nous-eval-impl-tests` | `tests/eval/*.py`, `tests/integration/test_eval_harness.py`, `tests/fixtures/{eval_smoke_corpus,eval_smoke,eval_probes}.jsonl` | ~900 |

**Sequencing:**

```
T=0 to T=1:    Refactor agent alone (all other agents wait)
               Completes: run_recall_pipeline + recall_deep refactor + existing tests pass
T=1 to T=end:  Core + Infra + Tests in parallel
T=end:         Orchestrator runs impl review team (3 agents parallel)
               P1 iteration loop (if any)
               Docs + branch + PR
```

Refactor must complete first because `retrieval_runner.py` depends on `run_recall_pipeline` existing.

---

## Files — v2

### A. Pipeline refactor (`nous-eval-impl-refactor`)

#### A.1 NEW: `nous/api/retrieval_pipeline.py` (~250 LOC)

Per §"Pipeline refactor" above.

#### A.2 MODIFY: `nous/api/tools.py` (~170 LOC removed, ~30 added)

- Replace `recall_deep` body (lines ~320-515) with ~30 LOC wrapper:
  ```python
  async def recall_deep(query, limit=10, memory_types=None):
      try:
          results, stats = await run_recall_pipeline(
              query=query,
              heart=heart,
              brain=brain,
              settings=settings,
              limit=limit,
              memory_types=memory_types,
          )
          return {"content": [{"type": "text", "text": _format_pipeline_text(results, stats, search_types)}]}
      except Exception as e:
          logger.exception("recall_deep failed")
          return {"content": [{"type": "text", "text": f"Error: {e}"}]}
  ```
- Add `_format_pipeline_text(results, stats, search_types) -> str` helper (~80 LOC) that reproduces the current text format exactly (grouped by type, existing headings, score formatting).

### B. Core (`nous-eval-impl-core`)

#### B.1 NEW: `nous/eval/__init__.py` (~25 LOC)
Public API re-exports.

#### B.2 NEW: `nous/eval/config.py` (~130 LOC)

```python
from pathlib import Path
from typing import Literal
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EvalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NOUS_EVAL_", env_file=".env", case_sensitive=False, extra="ignore",
    )

    # Eval DB — uses different env vars than main Settings to avoid collision
    db_host: str = "localhost"
    db_port: int = 5433
    db_user: str = "nous"
    db_password: str = "nous_eval"
    db_name: str = "nous_eval"
    db_pool_size: int = 5
    db_max_overflow: int = 2

    # Must match agent_id embedded in the ingested corpus
    agent_id: str = "nous-eval-corpus"

    log_level: str = "info"   # Database reads this for engine echo

    # Fixture management
    fixtures_dir: Path | None = None
    fixture_version: str = "v2026-Q2"   # Changed from "latest" (arch P2)
    top_k: int = 10
    report_dir: Path = Path("reports")
    run_history_enabled: bool = True
    run_history_insert_timeout_s: float = 5.0

    # F050 gate
    f050_gate_threshold: float = 0.07
    f050_gate_max_single_regression: float = 0.03
    f050_gate_require_majority_positive: bool = True   # new (arch P2)

    @field_validator("fixtures_dir", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        if v in (None, "", "None"):
            return None
        return Path(v)

    @property
    def smoke_mode(self) -> bool:
        return self.fixtures_dir is None or not self.fixtures_dir.exists()

    @property
    def db_url(self) -> str:
        """Match main Settings.db_url contract so Database(evalsettings) works."""
        return (f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}")

    def warn_if_default_password(self) -> None:
        import warnings
        if self.db_password == "nous_eval":
            warnings.warn(
                "EvalSettings using default db_password='nous_eval'. "
                "For shared machines, set NOUS_EVAL_DB_PASSWORD to a stronger value.",
                UserWarning, stacklevel=2,
            )
```

#### B.3 NEW: `nous/eval/source_registry.py` (~260 LOC)

Same design as v1 but with explicit resolution logging (`_skip_reason` field on `ResolvedSource` for the report).

#### B.4 NEW: `nous/eval/qrels_loader.py` (~180 LOC)

Same as v1 but `memory_types` Literal includes `"decision"` (now valid because pipeline covers decisions).

#### B.5 NEW: `nous/eval/corpus_loader.py` (~220 LOC)

Bulk-COPY from JSONL → Postgres. Used by the eval DB image build pipeline (inside Dockerfile) AND by the smoke-mode test fixtures. Key functions:

```python
async def load_corpus_from_jsonl(db: Database, jsonl_dir: Path, agent_id: str) -> CorpusStats:
    """Bulk COPY facts/decisions/episodes/procedures from JSONL dumps into a fresh DB.

    - Idempotent on agent_id: re-running does nothing if corpus already loaded for this agent
    - Validates embedding dimension = 1536 per row
    - Persists ingest timestamp into nous_eval_meta
    """
```

#### B.6 NEW: `nous/eval/retrieval_runner.py` (~320 LOC)

```python
from nous.api.retrieval_pipeline import run_recall_pipeline, PipelineResult
from nous.runtime_config import RuntimeConfig

@dataclass(frozen=True)
class RetrievalConfig:
    name: str
    flags: dict[str, Any] = field(default_factory=dict)
    description: str = ""

@dataclass(frozen=True)
class QrelResult:
    qrel_index: int
    qrel_query: str
    qrel_source: str
    retrieved_ids: list[UUID]
    retrieved_types: list[str]
    rank_of_first_gold: int | None          # 1-based
    n_gold_in_top_k: int
    n_gold_total: int
    error: str | None = None                # populated if recall raised

@dataclass(frozen=True)
class RunResult:
    config: RetrievalConfig
    per_qrel: list[QrelResult]
    duration_seconds: float

async def run_matrix(
    configs: list[RetrievalConfig],
    qrels: list[Qrel],
    eval_settings: EvalSettings,
    main_settings_template: Settings,
    top_k: int = 10,
) -> list[RunResult]:
    """Iterate configs × qrels. Per-config: reset RuntimeConfig, override Settings,
    construct fresh Heart+Brain, run pipeline, score.
    """
    results: list[RunResult] = []
    for cfg in configs:
        RuntimeConfig.reset()   # critical: per-config clean slate
        overridden = main_settings_template.model_copy(update=cfg.flags)
        # Construct eval DB's Database + Heart + Brain with overridden Settings
        eval_db = Database(_settings_for_eval_db(eval_settings, overridden))
        await eval_db.connect()
        try:
            async with _build_heart_for_eval(eval_db, overridden) as heart:
                brain = _build_brain_for_eval(eval_db, overridden, heart.embeddings)
                per_qrel: list[QrelResult] = []
                for idx, qrel in enumerate(qrels):
                    per_qrel.append(await _run_one(heart, brain, overridden, qrel, idx, top_k))
                results.append(RunResult(config=cfg, per_qrel=per_qrel,
                                         duration_seconds=_elapsed()))
        finally:
            await eval_db.engine.dispose()
    return results

def _settings_for_eval_db(eval_settings, base: Settings) -> Settings:
    """Clone `base` (production Settings) but swap DB connection fields to the eval DB.

    This is what Database + Heart + Brain see: production model/embedding/feature
    flags, eval DB connection.
    """
    return base.model_copy(update={
        "db_host": eval_settings.db_host,
        "db_port": eval_settings.db_port,
        "db_user": eval_settings.db_user,
        "db_password": eval_settings.db_password,
        "db_name": eval_settings.db_name,
        "db_pool_size": eval_settings.db_pool_size,
        "db_max_overflow": eval_settings.db_max_overflow,
        "agent_id": eval_settings.agent_id,
        # Disable ALL background handlers + derived pipelines (v2.1 expansion)
        "event_bus_enabled": False,
        "fact_extraction_enabled": False,
        "episode_summary_enabled": False,
        "sleep_enabled": False,
        "actionability_backfill_on_startup": False,
        "actionability_enabled": False,
        "heartbeat_enabled": False,
        "schedule_enabled": False,
        "subtask_enabled": False,
        "dag_enabled": False,
        "decision_review_enabled": False,
        "correction_extraction_enabled": False,
        "graph_backfill_enabled": False,
        "rubric_outcome_detection_enabled": False,
    })
```

`_build_heart_for_eval` + `_build_brain_for_eval` mirror the test conftest pattern; they do NOT start background tasks.

`_run_one`:
```python
async def _run_one(heart, brain, settings, qrel, idx, top_k) -> QrelResult:
    try:
        results, stats = await run_recall_pipeline(
            query=qrel.query, heart=heart, brain=brain, settings=settings,
            limit=top_k, memory_types=[mt.value for mt in (qrel.memory_types or [])],
        )
        retrieved_ids = [r.id for r in results]
        retrieved_types = [r.type for r in results]
        rank, n_gold = _score_rank(retrieved_ids, qrel.gold_ids)
        return QrelResult(qrel_index=idx, qrel_query=qrel.query,
                          qrel_source=qrel.source.value,
                          retrieved_ids=retrieved_ids, retrieved_types=retrieved_types,
                          rank_of_first_gold=rank,
                          n_gold_in_top_k=len(set(retrieved_ids) & set(qrel.gold_ids)),
                          n_gold_total=len(qrel.gold_ids))
    except Exception as exc:
        logger.exception("recall_pipeline raised for qrel %d", idx)
        return QrelResult(qrel_index=idx, qrel_query=qrel.query,
                          qrel_source=qrel.source.value,
                          retrieved_ids=[], retrieved_types=[],
                          rank_of_first_gold=None, n_gold_in_top_k=0,
                          n_gold_total=len(qrel.gold_ids), error=f"{type(exc).__name__}: {exc}")
```

#### B.7 NEW: `nous/eval/metrics.py` (~200 LOC)

Pure Python via `statistics.mean` + list comps — no numpy.

```python
from statistics import mean
from dataclasses import dataclass

@dataclass(frozen=True)
class MetricsResult:
    mrr: float
    p_at_1: float; p_at_5: float; p_at_10: float
    r_at_1: float; r_at_5: float; r_at_10: float
    ndcg_at_10: float
    n_qrels: int
    n_errored: int

def compute_metrics(per_qrel: list[QrelResult], top_k: int = 10) -> MetricsResult:
    valid = [q for q in per_qrel if q.error is None]
    if not valid:
        return MetricsResult(0,0,0,0,0,0,0,0, n_qrels=0, n_errored=len(per_qrel))
    mrr = mean((1 / q.rank_of_first_gold) if q.rank_of_first_gold else 0 for q in valid)
    # ... P@K, R@K, nDCG
    return MetricsResult(...)

@dataclass(frozen=True)
class Delta:
    metric: str
    baseline_mean: float
    experimental_mean: float
    absolute: float
    relative_pct: float
    n_pairs: int

def compute_delta(baseline: MetricsResult, experimental: MetricsResult, metric: str) -> Delta:
    """Point estimate = mean-of-means (identical to paired-average for all metrics;
    see spec §"Resolved decisions" for the math)."""
```

#### B.8 NEW: `nous/eval/report.py` (~280 LOC)

Same spec §8 layout. `decide_gate_f050`:

```python
def decide_gate_f050(
    run_results: list[RunResult],
    resolved_sources: list[ResolvedSource],
    threshold: float = 0.07,
    max_single_regression: float = 0.03,
    require_majority_positive: bool = True,
) -> GateDecision:
    base = next((r for r in run_results if r.config.name == "baseline"), None)
    exp  = next((r for r in run_results if r.config.name == "f050_on"), None)
    if base is None or exp is None:
        return GateDecision(feature="F050", passed=False, reason="missing baseline or f050_on config")

    gate_sources = [s for s in resolved_sources if s.gate_eligible_effective]
    if not gate_sources:
        return GateDecision(feature="F050", passed=False, reason="no gate-eligible sources")

    base_agg = compute_metrics(_filter_by_sources(base.per_qrel, gate_sources))
    exp_agg  = compute_metrics(_filter_by_sources(exp.per_qrel, gate_sources))
    agg_delta = compute_delta(base_agg, exp_agg, "mrr")
    if agg_delta.relative_pct < threshold * 100:
        return GateDecision(..., reason=f"aggregate MRR +{agg_delta.relative_pct:.1f}% < {threshold*100:.1f}%")

    # Per-source checks
    per_source_deltas = [
        compute_delta(
            compute_metrics(_filter_by_sources(base.per_qrel, [s])),
            compute_metrics(_filter_by_sources(exp.per_qrel, [s])),
            "mrr",
        )
        for s in gate_sources
    ]
    regressions = [(s.spec.name, d) for s, d in zip(gate_sources, per_source_deltas)
                   if d.relative_pct < -max_single_regression * 100]
    if regressions:
        return GateDecision(..., reason=f"single-source regression: {regressions[0][0]} {regressions[0][1].relative_pct:+.1f}%")

    if require_majority_positive:
        n_positive = sum(1 for d in per_source_deltas if d.relative_pct > 0)
        if n_positive * 2 <= len(per_source_deltas):
            return GateDecision(..., reason=f"only {n_positive}/{len(per_source_deltas)} sources positive (need majority)")

    return GateDecision(feature="F050", passed=True, ...)
```

#### B.9 NEW: `nous/eval/retrieval.py` (~280 LOC)

Renamed from v1's `cli.py` so `python -m nous.eval.retrieval` resolves. Contains the main eval entrypoint.

#### B.10 NEW: `nous/eval/rebuild.py` (~40 LOC)

`python -m nous.eval.rebuild` — drops the named volume + restarts the nous-eval-db service.

#### B.11 NEW: `nous/eval/ingest_entry.py` (~20 LOC)

`python -m nous.eval.ingest_entry` — thin dispatcher into `nous/eval/ingest.py::run()`. (`ingest.py` itself is under Infra's ownership.)

### C. Infra (`nous-eval-impl-infra`)

#### C.1 NEW: `Dockerfile.eval-db` (~60 LOC)

Base image: `pgvector/pgvector:pg17` (not `postgres:17`). Multi-stage.

```dockerfile
# syntax=docker/dockerfile:1.6
FROM pgvector/pgvector:pg17 AS ingest
ARG FIXTURE_VERSION
ENV POSTGRES_USER=nous POSTGRES_PASSWORD=nous_eval POSTGRES_DB=nous_eval
COPY sql/init.sql /docker-entrypoint-initdb.d/00_init.sql
COPY sql/migrations/*.sql /docker-entrypoint-initdb.d/
COPY nous-eval-fixtures-staging/ /fixtures/
COPY Dockerfile.eval-db.load.sh /load.sh
RUN chmod +x /load.sh && /load.sh

FROM pgvector/pgvector:pg17
ARG FIXTURE_VERSION
LABEL org.nous.fixture_version=$FIXTURE_VERSION
ENV POSTGRES_USER=nous POSTGRES_PASSWORD=nous_eval POSTGRES_DB=nous_eval
COPY --from=ingest /var/lib/postgresql/data /var/lib/postgresql/data
HEALTHCHECK --interval=5s --timeout=3s --retries=20 \
    CMD pg_isready -U nous -d nous_eval || exit 1
EXPOSE 5432
```

#### C.2 NEW: `Dockerfile.eval-db.load.sh` (~40 LOC) — LF line endings enforced

(See §Review finding #10 → `.gitattributes` fix.)

#### C.3 EDIT: `docker-compose.yml` (~18 LOC added)

```yaml
services:
  nous-eval-db:
    image: ghcr.io/tfatykhov/nous-eval-db:${NOUS_EVAL_FIXTURE_VERSION:-v2026-Q2}
    profiles: [eval]
    ports: ["127.0.0.1:5433:5432"]   # 127.0.0.1 bind — not 0.0.0.0 (security)
    volumes:
      - nous_eval_db_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "nous", "-d", "nous_eval"]
      interval: 5s
      timeout: 3s
      retries: 20
    restart: unless-stopped

volumes:
  nous_eval_db_data:
```

#### C.4 NEW: `.gitattributes` (3 lines)

```
*.sh text eol=lf
*.py text eol=lf
*.md text
```

#### C.5 NEW: `nous/eval/tasks.py` (~260 LOC)

Subcommands: `build-image`, `push-image`, `serve-eval-db`, `stop-eval-db`, `rebuild`, `ingest`, `probe-gen`, `hand-labels-draft`, `longmemeval-subset`. Each wraps a specific subprocess call with `capture_output=True, text=True, check=True`.

`rebuild` does (v2.1: drop `-v` from compose-down; use targeted volume removal only):
```python
def _rebuild(args) -> int:
    # Stop the nous-eval-db service specifically — no -v flag (would sweep other volumes on some Compose versions)
    subprocess.run(["docker", "compose", "--profile", "eval", "stop", "nous-eval-db"], check=False)
    subprocess.run(["docker", "compose", "--profile", "eval", "rm", "-f", "nous-eval-db"], check=False)
    # Targeted volume removal — does not touch postgres_data or other volumes
    subprocess.run(["docker", "volume", "rm", "-f", "nous_eval_db_data"], check=False)
    subprocess.run(["docker", "compose", "--profile", "eval", "up", "-d", "nous-eval-db"], check=True)
    return 0
```

#### C.6 NEW: `nous/eval/ingest.py` (~240 LOC)

Operator-run. Fails fast if `NOUS_PROD_DB_HOST/PORT/USER/PASSWORD/NAME` unset. Disables EventBus. Writes replayed state to a **scratch** eval DB (ephemeral), then dumps JSONL.

#### C.7 NEW: `nous/eval/ingest_longmemeval.py` (~200 LOC)

#### C.8 NEW: `nous/eval/probe_gen.py` (~130 LOC)

#### C.9 NEW: `nous/eval/hand_labels_draft.py` (~180 LOC)

#### C.10 NEW: `sql/migrations/037_eval_runs.sql` (~30 LOC)

Unchanged from v1.

Verify migration 037 does not conflict with existing — migrations 001-036 were checked via Glob.

#### C.11 EDIT: `nous/config.py` (~30 LOC added)

12 new env vars per spec §Config. All with pydantic Field + description docstring comments. All default to values that don't touch existing behavior.

### D. Tests (`nous-eval-impl-tests`)

#### D.1 NEW: `tests/eval/test_source_registry.py` (~180 LOC) — unchanged from v1
#### D.2 NEW: `tests/eval/test_qrels_loader.py` (~200 LOC) — unchanged
#### D.3 NEW: `tests/eval/test_metrics.py` (~250 LOC) — hand-computed golden vectors
#### D.4 NEW: `tests/eval/test_retrieval_runner.py` (~250 LOC) — uses a FakePipeline not a FakeHeart
#### D.5 NEW: `tests/eval/test_report.py` (~220 LOC) — includes the new gate-majority-positive rule
#### D.6 NEW: `tests/eval/test_config.py` (~130 LOC) — includes default-password-warning test
#### D.7 NEW: `tests/eval/test_retrieval_pipeline.py` (~180 LOC)

Covers the REFACTORED `run_recall_pipeline`:
- `test_pipeline_returns_structured_results`
- `test_pipeline_respects_graph_recall_enabled_flag`
- `test_pipeline_respects_spreading_activation_setting`
- `test_pipeline_contradiction_detection_flag`
- `test_pipeline_cross_type_linking_flag`
- `test_pipeline_n_heart_results_stats`

And regression:
- `test_recall_deep_text_format_unchanged` — snapshot test comparing pre/post refactor text output

#### D.8 NEW: `tests/integration/test_eval_harness.py` (~280 LOC)

No pytest-docker. Uses `socket.connect_ex(("localhost", 5433))` preflight. Skips cleanly if eval-db container not up.

#### D.9 NEW: `tests/fixtures/eval_smoke_corpus.jsonl` (~10 items, DETERMINISTIC non-random embeddings)

**10 synthetic items, DISTRIBUTED across all 5 memory types** so smoke exercises every pipeline branch:

| Type | Count | Example content |
|---|---|---|
| `fact` | 3 | "F049 shipped — session cleanup via try/finally"; "F050 draft spec — multi-query expansion"; "Nous uses pgvector for semantic search" |
| `decision` | 2 | "Chose LongMemEval_S over full LongMemEval for N=20 stratified"; "Use Docker image for eval DB distribution" |
| `episode` | 2 | "Discussed retrieval eval harness approach with Tim"; "Identified phantom API bug in F051 v1 plan" |
| `procedure` | 2 | "Review team: architect + devil + python-pro"; "Pipeline refactor protocol: snapshot before, snapshot after" |
| `censor` | 1 | "Don't mutate eval DB at runtime" (pattern: `.*eval.*insert.*`, reason: "eval DB is read-only") |

Embeddings **captured once from OpenAI text-embedding-3-small** and hardcoded. Per-item metadata header comment documents the capture protocol:

```
# _fixture_version: v1
# _embedding_model: text-embedding-3-small
# _captured_at: 2026-04-20T15:30:00Z
# _seed_text_prefix: nous-smoke
# _capture_script: scripts/capture_smoke_embeddings.py (one-shot, not committed)
```

Agent_id column for these 10 items = `nous-eval-smoke` (distinct from `nous-eval-corpus` used for the main ingested corpus — lets integration tests run without the full eval-db container).

#### D.10 NEW: `tests/fixtures/eval_smoke.jsonl` (~10 queries)

Queries cover specific-lookup (feature ID), concept (jargon drift), and basic fact retrieval. Gold IDs are the deterministic UUIDs from smoke_corpus.jsonl.

#### D.11 NEW: `tests/fixtures/eval_probes.jsonl` (~20 items)

Auto-generated once via `probe_gen.py` against the smoke corpus; committed as-is.

#### D.12 NEW: `tests/eval/__init__.py` + `tests/eval/conftest.py` (~40 LOC)

Registers `@pytest.mark.eval` marker. conftest has:
- `eval_db_fixture` — async fixture verifying the eval-db container is reachable; pytest.skip if not.
- `mock_fixtures_dir` — tmpdir with minimal JSONL files for smoke tests.

#### D.13 EDIT: `pyproject.toml` (~2 LOC added)

Add `eval` and `integration` to `[tool.pytest.ini_options].markers` if not already present.

---

## Acceptance criteria (Phase 1, v2)

1. **All files listed exist, are syntactically valid Python / YAML / SQL / Dockerfile.**
2. **`uv run pytest tests/ -v`** passes — ALL tests, including pre-existing recall_deep tests (refactor didn't regress).
3. **`uv run pytest tests/eval/ -v`** passes — new unit suite green.
4. **`uv run pytest tests/integration/test_eval_harness.py -v`** passes when eval-db container up; skips with clear reason when down.
5. **`uv run python -m nous.eval.retrieval --smoke`** runs without crashing even when `nous-eval-db` container is NOT up (fixtures dir unset → probes-only smoke).
6. **`docker compose --profile eval config`** validates.
7. **`uv run python -m nous.eval.tasks --help`** lists all 9 subcommands.
8. **`sql/migrations/037_eval_runs.sql`** applies cleanly on fresh `nous` DB.
9. **CLAUDE.md + INDEX.md updated.** F051 marked Shipped.
10. **Windows 11 compatibility:** `uv run` commands all succeed from Git Bash; `.sh` files check-in with LF (verified via `git ls-files --eol`).
11. **No new production behavior:** `tests/test_tools.py` + all recall_deep consumers pass unchanged; no new warnings in stdout on Nous boot.
12. **Pipeline refactor round-trip:** feeding identical inputs through old-recall_deep fixture (captured pre-refactor) and new run_recall_pipeline + formatter produces byte-identical text.

---

## Silent-failure test coverage (extended from v1)

| Silent failure path | Test |
|---|---|
| Docker daemon unreachable | `test_cli_docker_unreachable_shows_helpful_error` |
| Port 5433 refused | `test_cli_port_5433_refused_shows_docker_compose_hint` |
| Port 5433 held by non-Postgres | `test_cli_port_5433_wrong_service_shows_conflict_error` |
| Healthcheck still failing | integration test: `test_healthcheck_timeout_shows_container_logs` |
| Missing fixtures dir | `test_source_registry.test_load_smoke_mode` |
| Malformed JSONL | `test_qrels_loader.test_malformed_line_raises_with_line_number` |
| Pipeline raises on specific qrel | `test_retrieval_runner.test_qrel_exception_captured_not_zero_scored` |
| eval_runs INSERT timeout | `test_retrieval.test_run_history_insert_timeout_logs_warn_and_continues` |
| Unknown config name | `test_retrieval.test_unknown_config_name_fails_fast_with_list` |
| Gate aggregate met but single-source regressed | `test_report.test_gate_f050_fail_when_single_source_regresses` |
| Gate aggregate met but only minority positive | `test_report.test_gate_f050_fail_when_minority_sources_positive` |
| Fixture version mismatch | `test_retrieval.test_version_mismatch_warns_and_continues` |
| NOUS_PROD_DB_* unset during ingest | `test_ingest.test_missing_prod_db_env_fails_fast` |
| EventBus not disabled during ingest | `test_ingest.test_event_bus_disabled` |
| agent_id mismatch between corpus and harness | `test_retrieval.test_corpus_agent_id_mismatch_logs_warn` |
| Default password not changed | `test_config.test_warn_if_default_password_emitted` |
| RuntimeConfig not reset between configs | `test_retrieval_runner.test_runtime_config_reset_between_configs` |
| CRLF in .sh file | `test_repo_line_endings.py::test_shell_scripts_are_lf` (uses git ls-files --eol) |

Tests agent must include all 18.

---

## Documentation updates (orchestrator task, post-impl)

1. CLAUDE.md — add 12 new env vars + `nous/eval/` section in project layout + F051 Shipped row
2. INDEX.md — F051 row Shipped
3. F051 spec status flip to ✅ Shipped after PR merges
4. Memory: `project_f051_shipped.md`

---

## Risks & mitigations — v2

| Risk | Likelihood | Mitigation |
|---|---|---|
| Pipeline refactor breaks production recall_deep output | medium-low | Byte-identical snapshot test; 10 existing recall_deep tests run unchanged |
| Scope creep from refactor delays Phase 1 | medium | Refactor is vertical slice 0, well-defined contract; ~280 LOC net; one agent's full task |
| Windows path handling regresses | low | Named volumes + `.gitattributes` + integration test runs on Windows |
| `pgvector/pgvector:pg17` image has breaking change vs prod | low | Same image prod uses; if tag stops being maintained, F051.1 pins to a specific digest |
| Fixture version skew causing silent bad metrics | medium | Version-compare on startup; nous_eval_meta table records fixture_version; rebuild command purges volume |
| Ingest pipeline takes >30min | medium | Per-step `--skip-<step>` flags; operator can resume |
| nous_system.eval_runs table growth | low | Retention deferred to F051.3 (cron sweep) |
| LongMemEval download flakes during ingest | medium | Local cache at `~/.cache/nous-eval/` |
| RuntimeConfig reset between configs leaks state | low | Explicit test `test_runtime_config_reset_between_configs`; spec invariant stated |

---

## Re-review request

This is plan v2. Before dispatching implementation subagents, requesting a **lightweight re-review** on the deltas only:

- Does the `run_recall_pipeline` extraction match what the reviewers expected?
- Is the `_settings_for_eval_db` pattern safe? (clone production settings, swap DB fields, disable handlers)
- Does the revised gate-decision logic (majority-positive + single-source regression) survive N=20 noise analysis?
- Is the `.gitattributes` + LF-enforcement path complete?

Re-review should take <10 min per agent. If all three agents return APPROVE, implementation begins immediately.
