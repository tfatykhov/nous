# F051 — Retrieval Evaluation Harness

**Status:** ✅ Shipped (2026-04-20)
**Proposed by:** Tim
**Date:** 2026-04-20
**Depends on:** F002 (Heart Module — shipped), F025 (RRF hybrid search — shipped), F004 (Runtime / Docker — shipped)
**Blocks:** F050 Phase 3 enable decision; retroactive F042 / F045 / F030 scoring
**Related:** F042 (CE reranking), F045 (CE-aware thresholds), F050 (multi-query expansion — draft PR #342)

---

## Problem

Nous ships with a thick retrieval stack: hybrid search (F025), MMR diversity re-ranking (F030), cross-encoder re-ranking (F042), CE-aware cosine thresholds (F045), cross-type graph linking + spreading activation (F022), and proposed multi-query expansion (F050). None of these features landed with a reproducible before/after measurement. The only way to know whether any of them helps in aggregate is to eyeball `recall_deep` output at the chat line, which is slow and subjective.

Three concrete symptoms:

1. **F050 PR #342 cannot ship** — its enable gate is "MRR +5% on a retrieval eval harness" (§Rollout Phase 3). The harness does not exist. The spec is parked.
2. **F042 / F045 shipped without numeric justification.** Defaults landed because qualitative spot-checks looked good. We cannot answer "did CE re-ranking actually help?" because we have no delta to cite.
3. **Any future PR touching `nous/heart/search.py`, `nous/heart/heart.py`, or `nous/cognitive/context.py` has a blind regression risk.** There is no automated check that ranking quality didn't drop.

### What this is not

This is **not** an external benchmark system. It is a **Nous-internal regression + A/B tool**. Claims like "Nous beats MemGPT at retrieval" are out of scope. The decisions F051 needs to make possible are:
- Should we ship F050? (paired A/B gate)
- Did PR #X regress retrieval? (regression gate)
- Is CE re-ranking actually worth the latency? (retroactive A/B)

If Nous later needs external benchmark numbers for a paper or public comparison, F051.1 adds LongMemEval as a primary benchmark source via the same harness plumbing. F051 itself treats LongMemEval as **one fixture source among four**, with a deliberately small N=20 stratified subset for baseline signal.

---

## Goals

1. **Reproducible retrieval measurement** — any PR author can run `uv run python -m nous_eval.retrieval` and get MRR / P@K / R@K / nDCG on a fixed corpus + qrels set in under 5 minutes.
2. **Paired A/B between retrieval configs** — `--configs baseline,f050_on,ce_off` produces a side-by-side delta table, gate decisions use paired deltas not absolute numbers.
3. **Graceful degradation** — missing fixtures, unavailable LongMemEval, unreviewed hand-labels should all still yield *some* usable output with the limitation clearly flagged. Harness must never crash when a source is missing.
4. **Persistent, local eval DB** — one-time ingestion, then every subsequent run reuses the baked Postgres state. Zero re-ingestion cost on repeat runs.
5. **Windows 11 desktop compatible** — Docker Engine (WSL2 backend), Git Bash / PowerShell, named volumes not bind mounts, `uv run` not `make`.
6. **Intellectually honest label provenance** — every metric row carries its source tag; AI-generated-hand labels are explicitly distinct from reviewed-by-human labels and do not gate merge-decisions until `reviewed_by` is populated.
7. **Never touches production DB at runtime** — ingest pipeline (quarterly) is the only step that reads prod state; harness runs are fully offline from the prod VM.

## Non-goals

- **No CI integration** (no GitHub Actions). Solo developer; manual local invocation before PR creation is the gate.
- **No answer generation eval** in Phase 1 — retrieval-only metrics (MRR, P@K, R@K, nDCG). End-to-end QA quality with Sonnet generation is a F051.2 add-on.
- **No per-reasoning-type gate metrics** at N=20 — per-type numbers are noisy (3-4 Qs/type). Per-type deltas print to the report as *directional signal*, not gate-eligible.
- **No mutation of the eval DB at harness run time** — retrieval is read-only on `nous_eval`. Any test that learns facts at runtime belongs in existing pytest, not F051.
- **No schema changes on the main `nous` DB** beyond a single new `nous_system.eval_runs` table for run history.
- **No new REST endpoints.** F051 is CLI-only in Phase 1. If dashboard integration is wanted later, that's F051.3.
- **No changes to production retrieval code paths.** Every edit lives under `nous_eval/`, `sql/migrations/NNN_eval_runs.sql`, `Dockerfile.eval-db`, `docker-compose.yml` (one new service under a profile), and docs.

## Deferred (with rationale)

- **F051.1 — LongMemEval as primary benchmark source.** Expand from N=20 subset to full 500-Q benchmark when external numbers are wanted. Same harness, larger `longmemeval` source file.
- **F051.2 — Answer quality eval.** Add Sonnet generation step + RAG-evaluator-style correctness scoring. Requires care: LLM-as-judge has its own calibration problems.
- **F051.3 — Dashboard.** Expose `nous_system.eval_runs` history as a dashboard tab with a per-commit regression chart.

---

## Design

### 0. Pipeline refactor (prerequisite to the harness)

`Heart.recall()` alone does not exercise the full retrieval stack — it searches only Heart memory types (`episode, fact, procedure, censor`). The full user-facing pipeline (graph expansion, spreading activation, cross-type linking, contradiction detection, Brain decisions) lives in `nous/api/tools.py::recall_deep` as a 200-line orchestration tangled with LLM-tool formatting.

F051 extracts the pipeline into `nous/api/retrieval_pipeline.py::run_recall_pipeline(query, heart, brain, settings, limit, memory_types) -> (list[PipelineResult], PipelineStats)`. `recall_deep` becomes a thin wrapper that calls the pipeline, then formats results into its existing text output. Byte-identical text output is a hard invariant — verified by snapshot test. See implementation plan for refactor details.

This refactor must land BEFORE the eval harness reads from the pipeline. Blast radius is ~280 LOC net.

### 1. Architecture overview

```
┌──────────────────────────────────────────────────┐
│  Developer desktop (Windows 11)                  │
│                                                  │
│  ┌──────────────────────────────────────┐        │
│  │  git checkout of nous repo           │        │
│  │  + uv-managed venv                   │        │
│  └──────────────────┬───────────────────┘        │
│                     │                            │
│                     │ import nous.heart, .brain  │
│                     │                            │
│  ┌──────────────────┴───────────────────┐        │
│  │  `uv run python -m nous_eval.retrieval`        │
│  │     - loads RetrievalConfig matrix             │
│  │     - connects Heart(db=eval_db)               │
│  │     - loops qrels, calls recall_deep           │
│  │     - computes MRR / P@K / R@K / nDCG          │
│  │     - writes reports/<ts>.md + .json           │
│  └──────────────────┬────────────────────┘       │
│                     │ psycopg/asyncpg             │
│                     │ localhost:5433              │
│  ┌──────────────────┴────────────────────┐       │
│  │  Docker: ghcr.io/tfatykhov/            │       │
│  │          nous-eval-db:v2026-Q2         │       │
│  │  Postgres 17 + pgvector + pre-loaded   │       │
│  │  corpus + embeddings + qrels tables    │       │
│  └────────────────────────────────────────┘       │
│                                                   │
│  ┌────────────────────────────────────────┐       │
│  │  Docker: existing `postgres` service   │       │
│  │  localhost:5432 — UNTOUCHED            │       │
│  └────────────────────────────────────────┘       │
└───────────────────────────────────────────────────┘
         │
         │ only during quarterly fixture refresh
         │ SSH tunnel -L 15432:localhost:5432
         ▼
┌──────────────────────────────────────┐
│  Prod VM (read-only for ingest)      │
│  heart.episodes, heart.facts, ...    │
└──────────────────────────────────────┘
```

### 2. Module layout — `nous_eval/`

```
nous_eval/
├── __init__.py
├── config.py               # EvalSettings (Pydantic) — env vars, defaults
├── source_registry.py      # Load sources.yaml, resolve paths, per-source toggles
├── corpus_loader.py        # Bulk-COPY JSONL → Postgres on ingest + rebuild
├── qrels_loader.py         # JSONL → list[Qrel], row-level review gate filter
├── retrieval_runner.py     # Matrix runner: for config in configs, for qrel in qrels, call Heart.recall
├── metrics.py              # MRR, P@K, R@K, nDCG (vectorized over qrels)
├── report.py               # Markdown + JSON report formatters
├── ingest.py               # Corpus + silver qrels mining from prod DB (quarterly)
├── ingest_longmemeval.py   # LongMemEval_S subset ingestion (downloader + adapter)
├── probe_gen.py            # Auto-generate probe qrels from INDEX.md + heart.facts
├── hand_labels_draft.py    # AI-draft hand-label qrels against seeded corpus
├── tasks.py                # Python-based task runner (replaces Makefile on Windows)
│                           #   - build-image, push-image, rebuild, ingest, serve-eval-db
└── cli.py                  # argparse entry points for retrieval / ingest / rebuild / tasks
```

Under 1500 LOC total, ~800 LOC of tests alongside in `tests/eval/`.

### 3. Source registry — `nous_eval/config/sources.yaml`

```yaml
# Per-source metadata. Harness reads this at startup to build the fixture matrix.
sources:
  longmemeval:
    path: "${NOUS_EVAL_FIXTURES_DIR}/qrels_longmemeval.jsonl"
    enabled_by_default: true
    gate_eligible: true
    requires_fixtures_dir: true
    description: "Stratified 20-Q LongMemEval_S subset"

  ai_hand_labeled:
    path: "${NOUS_EVAL_FIXTURES_DIR}/qrels_ai_hand.jsonl"
    enabled_by_default: true
    gate_eligible: false   # Flips to true when all rows have reviewed_by populated (checked at load time)
    requires_fixtures_dir: true
    review_filter: "reviewed_by != null"   # Row-level filter for gate metrics
    description: "AI-drafted hand-labeled qrels against seeded corpus"

  probes:
    path: "tests/fixtures/eval_probes.jsonl"   # Checked into public repo
    enabled_by_default: true
    gate_eligible: true
    requires_fixtures_dir: false
    description: "Auto-generated deterministic probes (feature IDs, PR titles)"

  silver_episodes:
    path: "${NOUS_EVAL_FIXTURES_DIR}/qrels_silver.jsonl"
    enabled_by_default: true
    gate_eligible: false
    requires_fixtures_dir: true
    description: "Click-model-style silver labels mined from heart.episodes"

  synthetic_haiku:
    path: "${NOUS_EVAL_FIXTURES_DIR}/qrels_synthetic.jsonl"
    enabled_by_default: false
    gate_eligible: false
    requires_fixtures_dir: true
    description: "Haiku-reverse-generated (informational only, circular risk)"
```

**Resolution rules (at startup):**

1. If `NOUS_EVAL_FIXTURES_DIR` is unset → only `requires_fixtures_dir: false` sources load. Harness prints `[smoke mode]` banner. Gate decisions are flagged `N/A — insufficient sources`.
2. If a source file is missing → source is silently skipped with `WARN` log. Remaining sources proceed.
3. CLI `--sources <whitelist>` overrides `enabled_by_default` (explicit wins).
4. CLI `--exclude <list>` subtracts from whatever the resolution chose.
5. CLI `--gate-only` filters to `gate_eligible: true` + applies `review_filter` where present.
6. CLI `--include-unreviewed` bypasses `review_filter` row gates (promotes unreviewed rows to gate-eligible — use only when you've verified labels yourself).

### 4. Qrels row schema

```python
class Qrel(BaseModel):
    query: str
    gold_ids: list[UUID]                    # Any overlap with retrieval top-K counts as positive
    source: Literal[
        "longmemeval", "ai_hand_labeled", "probes",
        "silver_episodes", "synthetic_haiku",
    ]
    confidence: Literal["high", "medium", "low"] = "high"
    reasoning_type: str | None = None        # "temporal", "multi_session", "concept", etc. Optional.
    memory_types: list[Literal["fact", "decision", "episode", "procedure"]] | None = None
    notes: str | None = None                 # Freeform why-these-IDs for auditing
    reviewed_by: str | None = None           # null = informational, non-null = gate-eligible
```

JSONL on disk (one Qrel per line). Example:

```json
{"query":"what is F049?","gold_ids":["a1b2...","c3d4..."],"source":"probes",
 "confidence":"high","reasoning_type":"specific_lookup","notes":"F049 facts"}
{"query":"how does session cleanup work","gold_ids":["a1b2..."],"source":"ai_hand_labeled",
 "confidence":"medium","reasoning_type":"concept","notes":"jargon drift: session→subtask teardown",
 "reviewed_by":null}
```

### 5. RetrievalConfig matrix

```python
@dataclass(frozen=True)
class RetrievalConfig:
    name: str                               # "baseline", "f050_on", "ce_off", ...
    flags: dict[str, Any]                   # Maps to Settings overrides
    description: str
```

Example overrides used in Phase 1:

| Config name | Flags applied |
|---|---|
| `baseline` | Defaults from `Settings()` — nothing overridden |
| `f050_on` | `query_expansion_enabled=True` (after F050 Phase 1 lands) |
| `ce_off` | `cross_encoder_enabled=False` |
| `mmr_off` | `mmr_enabled=False` |
| `graph_off` | `graph_recall_enabled=False` |

Config matrix lives in `nous_eval/config/configs.yaml`. CLI `--configs a,b,c` selects by name; unknown name errors at startup with a list of available configs.

### 6. Retrieval runner — `retrieval_runner.py`

```python
async def run_matrix(
    configs: list[RetrievalConfig],
    qrels: list[Qrel],
    eval_db: Database,
    top_k: int = 10,
) -> list[RunResult]:
    results: list[RunResult] = []
    for config in configs:
        heart = await _build_heart_for_config(eval_db, config)
        try:
            per_qrel: list[QrelResult] = []
            for qrel in qrels:
                retrieved = await heart.recall(
                    query=qrel.query, limit=top_k, session_id=None,
                )
                per_qrel.append(_score_one(qrel, retrieved, top_k))
            results.append(RunResult(config=config, per_qrel=per_qrel))
        finally:
            await heart.close()
    return results
```

Heart is instantiated fresh per config (no state bleed). The eval DB connection pool is reused across configs.

### 7. Metrics — `metrics.py`

Pure-Python, vectorized via numpy for speed on 100+ qrels × 5 configs:

- **MRR** — mean of `1 / rank_of_first_gold_in_top_K`; 0 if no gold in top-K
- **P@K** — fraction of top-K that are gold (for K ∈ {1, 5, 10})
- **R@K** — fraction of gold IDs that appear in top-K (for K ∈ {1, 5, 10})
- **nDCG@10** — standard DCG / IDCG with binary gains (gold = 1, non-gold = 0)

All metrics computed per-config × per-source × aggregate. For point estimates, paired-average of per-qrel deltas equals (mean experimental − mean baseline) by linearity of expectation — true for MRR, P@K, R@K, and nDCG@10 when aggregated by per-qrel mean. The harness computes deltas as `mean_experimental − mean_baseline` (simpler, equivalent point estimate). What differs between paired and unpaired analysis is confidence interval width: paired bootstrap CIs are tighter when qrels behave correlatedly across configs. CI computation is Phase 1.5, not Phase 1.

### 8. Report — `report.py`

Writes two files to `reports/<utc_timestamp>_<configs_joined>.{md,json}`:

**Markdown (primary, human-readable):**

```
F051 retrieval eval — git_sha=a1b2c3d — 2026-04-20T14:32:07Z
corpus: 527 items (facts:300 decisions:120 episodes:80 procedures:27)
qrels:  148 queries (longmemeval:20 ai_hand:30 probes:20 silver:30 synthetic:48)
sources in gate aggregate: longmemeval, probes (2)
sources informational only: ai_hand_labeled (reviewed_by=null), silver_episodes, synthetic_haiku

                          baseline    f050_on    Δ           gate?
─── GATE AGGREGATE (longmemeval + probes, N=40) ───
MRR                       0.612       0.683     +11.6%       ✓ (threshold +7%)
P@1                       0.480       0.500     +4.2%
P@5                       0.341       0.362     +6.2%
P@10                      0.214       0.229     +7.0%
R@10                      0.682       0.741     +8.7%
nDCG@10                   0.597       0.625     +4.7%

─── PER-SOURCE (gate-eligible) ───
longmemeval (N=20)        MRR 0.58 → 0.64 (+10.3%)   ✓ single-source regression >3%? NO
probes (N=20)             MRR 0.85 → 0.91 (+7.1%)    ✓ single-source regression >3%? NO

─── PER-SOURCE (informational only, not in gate) ───
ai_hand_labeled unrev.    MRR 0.62 → 0.66 (+6.5%)   [reviewed_by=null, excluded]
silver_episodes (N=30)    MRR 0.54 → 0.59 (+9.3%)
synthetic_haiku (N=48)    MRR 0.77 → 0.84 (+9.1%)   [circular-risk flagged]

─── PER-REASONING-TYPE (directional only, N too small for gate) ───
temporal          (N=4)   0.50 → 0.58 (+16%)
multi_session     (N=3)   0.44 → 0.51 (+15.9%)
concept           (N=8)   0.63 → 0.68 (+7.9%)
specific_lookup   (N=12)  0.88 → 0.89 (+1.1%)
...

F050 gate: PASS — aggregate MRR +11.6% (threshold +7%), no single-source regression >3%
Full details: reports/2026-04-20T14-32-07_baseline-f050_on.json
```

**JSON (machine-readable, committed to `nous_system.eval_runs`):** full per-qrel per-config results for later statistical analysis.

### 9. Eval DB Docker image — `Dockerfile.eval-db`

Multi-stage build; stage 1 does ingestion against a fresh Postgres, stage 2 takes the data dir and commits it into a minimal image.

```dockerfile
# syntax=docker/dockerfile:1.6
# Stage 1: ingest
FROM postgres:17 AS ingest
ARG FIXTURE_VERSION
ENV POSTGRES_USER=nous POSTGRES_PASSWORD=nous_eval POSTGRES_DB=nous_eval
COPY sql/init.sql /docker-entrypoint-initdb.d/00_init.sql
COPY sql/migrations/*.sql /docker-entrypoint-initdb.d/
COPY nous-eval-fixtures-staging/ /fixtures/
# Custom entrypoint starts Postgres, runs bulk COPY from JSONL, shuts down cleanly
COPY Dockerfile.eval-db.load.sh /load.sh
RUN chmod +x /load.sh && /load.sh

# Stage 2: the consumable image
FROM postgres:17
ARG FIXTURE_VERSION
LABEL org.nous.fixture_version=$FIXTURE_VERSION
ENV POSTGRES_USER=nous POSTGRES_PASSWORD=nous_eval POSTGRES_DB=nous_eval
COPY --from=ingest /var/lib/postgresql/data /var/lib/postgresql/data
HEALTHCHECK --interval=5s --timeout=3s --retries=20 \
    CMD pg_isready -U nous -d nous_eval || exit 1
EXPOSE 5432
```

Built via `uv run python -m nous_eval.tasks build-image --version v2026-Q2` which wraps:

```bash
docker buildx build \
    --platform linux/amd64 \
    -f Dockerfile.eval-db \
    --build-arg FIXTURE_VERSION=v2026-Q2 \
    -t ghcr.io/tfatykhov/nous-eval-db:v2026-Q2 \
    -t ghcr.io/tfatykhov/nous-eval-db:latest \
    .
```

Image size budget: ≤500 MB compressed. If exceeded, prune ingest-time logs and vacuum full before stage 2 copies.

### 10. docker-compose integration

Added to existing `docker-compose.yml` under `profiles: [eval]` so it stays dormant during normal dev:

```yaml
services:
  postgres:
    # existing — UNCHANGED
    image: pgvector/pgvector:pg17
    ports: ["5432:5432"]
    # ...

  nous-eval-db:
    image: ghcr.io/tfatykhov/nous-eval-db:${NOUS_EVAL_FIXTURE_VERSION:-v2026-Q2}
    profiles: [eval]
    ports: ["127.0.0.1:5433:5432"]   # loopback bind — eval corpus contains personal memory data
    volumes:
      - nous_eval_db_data:/var/lib/postgresql/data   # Named volume, not bind mount (Windows-safe)
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "nous", "-d", "nous_eval"]
      interval: 5s
      timeout: 3s
      retries: 20
    restart: unless-stopped

volumes:
  nous_eval_db_data:   # Added alongside existing volumes
```

Starts on demand only:

```bash
docker compose --profile eval up -d nous-eval-db
uv run python -m nous_eval.retrieval --configs baseline,f050_on
docker compose --profile eval stop nous-eval-db   # Optional
```

### 11. `nous_system.eval_runs` — run history table

Lives on the **main** `nous` DB, not the eval DB. Written at end of every harness run so you can query "did retrieval regress between commit X and Y?" later.

```sql
-- sql/migrations/037_eval_runs.sql
CREATE TABLE IF NOT EXISTS nous_system.eval_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        TEXT NOT NULL,                    -- agent_id at time of run
    git_sha         TEXT NOT NULL,                    -- nous repo git HEAD
    fixture_version TEXT NOT NULL,                    -- e.g., v2026-Q2
    configs         JSONB NOT NULL,                   -- list of RetrievalConfig names + flags
    metrics         JSONB NOT NULL,                   -- full per-config per-source metrics
    qrel_counts     JSONB NOT NULL,                   -- per-source N values
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    report_path     TEXT,                             -- reports/<ts>_<configs>.md relative path
    notes           TEXT                              -- CLI --notes flag content
);

CREATE INDEX idx_eval_runs_created_at ON nous_system.eval_runs(created_at);
CREATE INDEX idx_eval_runs_agent_id   ON nous_system.eval_runs(agent_id);

COMMENT ON TABLE nous_system.eval_runs IS
    'F051: retrieval evaluation run history — one row per harness invocation.';
```

Writes are best-effort: if the main `nous` DB is unreachable at run end, the run is logged to stderr as WARN and the report still persists on disk. Never blocks the harness invocation.

### 12. Ingest pipeline — `nous_eval/ingest.py`

**When:** quarterly, or when adding new fixture sources. Runs against **prod Nous DB** via SSH tunnel (`ssh -L 15432:localhost:5432 vm` beforehand).

**Steps:**

1. **Corpus dump** (read-only): `SELECT * FROM heart.facts / brain.decisions / heart.episodes / heart.procedures` → JSONL files under `nous-eval-fixtures-staging/v<tag>/`. Includes embeddings.
2. **Silver qrels mining**: for each recent episode where `recall_deep` was called, extract `(user_query, retrieved_memory_ids)` from episode transcript + tool-call log, feed final assistant response to Haiku with a prompt asking "which memory IDs from this list were actually cited or used?" Haiku returns a subset → silver `gold_ids`. Rate limit: 1 Haiku call / episode, ~$0.001 each.
3. **LongMemEval_S ingestion**: download benchmark archive from GitHub, pick 20 questions stratified across 6 reasoning types (3-4 per type), replay each question's chat_history through Nous's fact extractor + episode summarizer pipeline (using the **same** configuration as prod), track `session_id → episode_id` mapping, emit qrels.
4. **Probe generation**: parse `docs/features/INDEX.md` for feature IDs, grep `heart.facts` for facts containing each, emit deterministic `(feature_id_query, matching_fact_ids)` qrels.
5. **AI-hand draft**: call an inline agent with access to the corpus sample, produce 30 queries across 3 categories (specific-lookup / concept / jargon-drift), emit qrels with `reviewed_by: null`.
6. **Synthetic Haiku**: optionally, 1-2 queries reverse-generated per fact. Off by default.
7. **Commit JSONL dump to `nous-eval-fixtures` git repo** at `v<tag>/` directory. Tag the repo.
8. **Build eval DB image**: `uv run python -m nous_eval.tasks build-image --version v<tag>`.
9. **Push image to GHCR**: `uv run python -m nous_eval.tasks push-image --version v<tag>`.
10. **Bump `NOUS_EVAL_FIXTURE_VERSION` default** in docker-compose.yml, commit to nous repo.

### 13. Hand-label draft — `hand_labels_draft.py`

AI-generated draft that you later review. Takes ~5 min to run:

```python
async def draft_hand_labels(
    corpus_sample: list[MemoryItem],
    n_queries: int = 30,
    model: str = "claude-sonnet-4-6",
) -> list[Qrel]:
    """Generate N hand-label draft queries + gold IDs against the corpus.

    Categories (equal split):
      - specific_lookup:  "what is F049?"
      - concept:           "how does session cleanup work?"
      - jargon_drift:      query phrased in user-vocab while memory uses jargon

    Each draft Qrel has reviewed_by=null. Tim reviews, flips reviewed_by when approved.
    """
```

Output committed to `nous-eval-fixtures/v<tag>/qrels_ai_hand_draft.jsonl`. After Tim review, approved rows move to `qrels_ai_hand.jsonl` with `reviewed_by: "tfatykhov"` populated.

### 14. Windows-specific considerations

1. **Named volumes only** — no `./data:/var/lib/postgresql/data` bind mount, because Windows path translation via WSL2 backend has Docker permission issues and encoding traps. `nous_eval_db_data` named volume solves both.
2. **No Makefile** — all build / push / rebuild tasks are `uv run python -m nous_eval.tasks <subcommand>`. Argparse subcommands; cross-platform.
3. **Forward slashes in config YAML** — Python's `pathlib.PurePosixPath` normalizes. Absolute Windows paths (`E:\...`) only appear in the CLI `--out reports/...` arg, which is shell-quoted by the user.
4. **Port 5433 conflict detection** — at startup, harness does `socket.socket().connect_ex(("localhost", 5433))`. If the eval DB container is down (refused connection) → error message tells user to run `docker compose --profile eval up -d nous-eval-db`. If *something else* is squatting 5433 (successful connect to non-Postgres process) → separate error instructs user to stop the conflicting service.
5. **`docker buildx` uses `--platform linux/amd64` explicitly** so the Windows WSL2 Docker Engine produces images identical to what an amd64 Linux host produces. Prevents surprise arch mismatches.
6. **SSH tunnel command provided as-is in docs** — Windows Git Bash supports `ssh -L` the same way Linux does. No additional tooling.

---

## Config

New env vars in `nous.config.Settings`:

| Env var | Default | Purpose |
|---|---|---|
| `NOUS_EVAL_DB_HOST` | `localhost` | Eval DB host |
| `NOUS_EVAL_DB_PORT` | `5433` | Eval DB port |
| `NOUS_EVAL_DB_USER` | `nous` | Eval DB user |
| `NOUS_EVAL_DB_PASSWORD` | `nous_eval` | Eval DB password |
| `NOUS_EVAL_DB_NAME` | `nous_eval` | Eval DB database name |
| `NOUS_EVAL_FIXTURES_DIR` | *(unset)* | Path to clone of `nous-eval-fixtures` repo; unset → smoke mode |
| `NOUS_EVAL_FIXTURE_VERSION` | `latest` | Image tag pinned in docker-compose |
| `NOUS_EVAL_TOP_K` | `10` | Retrieval top-K for all metrics |
| `NOUS_EVAL_REPORT_DIR` | `reports/` | Where markdown + JSON reports land |
| `NOUS_EVAL_RUN_HISTORY_ENABLED` | `true` | Persist run to `nous_system.eval_runs` |
| `NOUS_EVAL_F050_GATE_THRESHOLD` | `0.07` | F050 paired-A/B MRR delta threshold (7%) |
| `NOUS_EVAL_F050_GATE_MAX_SINGLE_REGRESSION` | `0.03` | Max allowed per-source regression to still pass gate |

---

## Testing

### Unit (`tests/eval/`)

- `test_source_registry.py` — env-var resolution, CLI `--sources` whitelist, `--exclude` blacklist, `--gate-only` filter, `--include-unreviewed` bypass, missing-file silent-skip
- `test_qrels_loader.py` — JSONL parse, row-level `reviewed_by` filter, schema validation
- `test_metrics.py` — golden vectors for MRR / P@K / R@K / nDCG (hand-computed reference values); paired-delta averaging correctness
- `test_corpus_loader.py` — bulk COPY idempotency, embedding dimension validation, agent_id injection
- `test_report.py` — markdown table rendering, JSON schema stability, gate-decision logic
- `test_retrieval_runner.py` — config matrix produces config × qrel result grid, Heart close on every iteration
- `test_config.py` — env var parsing, Pydantic validation

### Integration (`tests/integration/test_eval_harness.py`)

- Requires Docker. Runs against an ephemeral **test** nous-eval-db container (not the production image — a minimal 10-item fixture baked in a test-specific image via `docker run postgres:17 + test init.sql`).
- End-to-end: start container → run `retrieval --configs baseline` on a 5-qrel smoke fixture → verify report files written + `eval_runs` row persisted.
- Graceful-degradation: `NOUS_EVAL_FIXTURES_DIR=/nonexistent` → smoke banner, no crash, probes-only result.

### Smoke (`tests/fixtures/eval_smoke.jsonl`)

10 deterministic queries (feature IDs from INDEX.md) checked into public repo. Used by the smoke-mode fallback when no fixtures dir is available. Also used by integration test as the base qrels set.

### Non-tests (one-off manual verification on first real run)

- Run ingest pipeline end-to-end against prod VM via SSH tunnel, verify JSONL dumps look sane.
- Build + push + pull v2026-Q2 image, verify `pg_isready` passes within 30s of container start.
- Run full harness against v2026-Q2 image with all 5 configs, confirm gate-decision math matches hand calculation.

---

## Observability

New log lines (INFO / DEBUG):

- `F051: run_started git_sha=%s configs=%s qrels=%d fixture_version=%s`
- `F051: config=%s qrel=%d/%d rank_of_first_gold=%s`
- `F051: config_complete config=%s mrr=%.3f p@1=%.3f p@10=%.3f`
- `F051: gate_decision feature=F050 result=%s delta_mrr=%+.3f`
- `F051: run_history_persisted id=%s` (or `WARN run_history_persist_failed reason=%s`)

No structured metrics (Prometheus etc.) — CLI tool, human-invoked, stdout is the primary observability surface. Historical analysis goes through `nous_system.eval_runs` SQL queries.

---

## Silent-failure surface (explicit call-out for silent-failure-hunter review)

Every path below must fail **loudly or gracefully** — never silently:

1. **Docker daemon unreachable** at harness startup → clear error: "Docker Engine not running. Start Docker Desktop / dockerd and retry."
2. **Port 5433 refused connection** → "nous-eval-db not running. Run: `docker compose --profile eval up -d nous-eval-db`"
3. **Port 5433 reached but NOT Postgres** → "Port 5433 held by non-Postgres process. Stop conflicting service and retry."
4. **nous-eval-db healthcheck still failing after 30s** → abort with last 20 lines of container logs in the error.
5. **Missing fixtures dir** + source `requires_fixtures_dir: true` → source silently skipped, `WARN` log, banner in report confirming which sources dropped.
6. **Fixture JSONL malformed** → pydantic validation error surfaced line-by-line, harness exits non-zero.
7. **Embedding provider timeout** during runtime query-embed → per-qrel warning, that qrel drops from metrics with a note in the report, never silently zeroed.
8. **`recall_deep` raises on a specific qrel** → exception caught per-qrel, logged with qrel index + full traceback, qrel drops from metrics.
9. **`nous_system.eval_runs` INSERT fails** → WARN log, run report still written to disk (`run_history_persist_failed`). Never aborts the run.
10. **Config name not in configs.yaml** → fast-fail at startup with list of valid names.
11. **Gate threshold met but single-source regression exceeds max** → gate decision = FAIL with explicit "single-source regression: <source> -<pct>%".
12. **Fixture version mismatch** (image tag vs `meta.json` in fixtures dir) → WARN at startup, run continues, version mismatch noted in report header.
13. **Named volume stale after image tag bump.** Docker copies image data INTO an empty named volume; a populated `nous_eval_db_data` silently keeps old fixtures even when the image tag bumps. Mitigation: (a) at startup, harness queries `nous_eval_meta.fixture_version` and compares to `NOUS_EVAL_FIXTURE_VERSION`; mismatch raises a HARD error instructing operator to run `python -m nous_eval.tasks rebuild` which purges the volume; (b) rebuild command always runs `docker volume rm -f nous_eval_db_data` before re-up.
14. **Default eval DB password `nous_eval` left unchanged.** Startup emits `UserWarning` if `NOUS_EVAL_DB_PASSWORD == "nous_eval"`. Port 5433 binds `127.0.0.1` only, so risk is contained to local machine, but operator is reminded.
15. **`agent_id` mismatch between ingested corpus and harness.** Corpus is ingested with `agent_id=nous-eval-corpus`; `EvalSettings.agent_id` defaults to the same; startup queries `SELECT DISTINCT agent_id FROM heart.facts` and warns if the corpus contains a different value.
16. **RuntimeConfig singleton state bleed between configs.** `RuntimeConfig.reset()` is called (a) at harness startup and (b) between each config in the matrix. If a config flag lives in RuntimeConfig-layer (e.g. `cross_encoder_enabled`, `vector_weight`, `rrf_k`), the per-config Settings override takes effect via the `_overrides`-empty fallback path. Verified by `test_runtime_config_reset_between_configs`.
17. **EventBus cascading writes during ingest.** Ingest pipeline runs Nous's fact_extractor + episode_summarizer against real data from prod — these would normally emit events. Ingest explicitly disables the EventBus via `settings.event_bus_enabled=False` and all background handler flags, so handlers run inline without bus fan-out. Prevents accidental writes to any shared state.
18. **`NOUS_PROD_DB_*` unset.** Ingest fails fast before any query if `NOUS_PROD_DB_HOST / PORT / USER / PASSWORD / NAME` are unset, preventing accidental reads from a local dev DB as "prod".
19. **CRLF line endings on shell scripts on Windows clone.** `.gitattributes` declares `*.sh text eol=lf`, ensuring `Dockerfile.eval-db.load.sh` checks in with LF regardless of clone platform.

---

## Rollout

1. **Phase 1 — Land the harness dark** *(this PR)*
   - Module + Dockerfile + docker-compose profile + configs + probes fixture + unit tests + smoke integration test
   - No fixture dir set in dev by default → harness is smoke-only locally until operator sets `NOUS_EVAL_FIXTURES_DIR`
   - **Shipped** = code merged, all tests green, manual smoke run passes

2. **Phase 2 — Fixture ingest + image push** *(follow-up, not gated on PR)*
   - Operator creates `nous-eval-fixtures` private GitHub repo
   - Operator runs ingest pipeline once: produces v2026-Q2 fixtures
   - Operator builds + pushes image to GHCR
   - Operator reviews + approves AI-hand-label draft rows (`reviewed_by: "tfatykhov"`)
   - After this phase, gate metrics are "full-fidelity"

3. **Phase 3 — Use F051 to gate F050** *(follow-up)*
   - Run harness with `--configs baseline,f050_on` after F050 Phase 1 lands
   - Gate decision: MRR delta ≥ +7%, no single-source regression > 3% → flip `NOUS_QUERY_EXPANSION_ENABLED=true`
   - Retroactively score F042 / F045 / F030 for calibration data

4. **Phase 4 (deferred) — F051.1 expansion**
   - Scale LongMemEval from 20 → 500 if external benchmark numbers wanted
   - Add F051.2 answer-quality eval if RAG correctness matters
   - Add F051.3 dashboard if per-commit trend chart is useful

---

## Success criteria

- **Phase 1 ship gate:**
  - All unit + integration tests green
  - `uv run python -m nous_eval.retrieval` in smoke mode produces a valid report referencing ≥1 source
  - Manual docker-compose spin-up with `nous-eval-db:latest` (placeholder image on first PR; replaced in Phase 2) reaches healthcheck within 30s
  - Windows 11 compatibility verified: all CLI tasks run from Git Bash without modification
- **Phase 2 completion gate:** fixture ingest produces v2026-Q2 JSONL dumps + image + reviewed hand-labels; full-fidelity run produces gate-eligible metrics
- **Phase 3 completion gate:** F050 gate decision recorded in `eval_runs` with delta metrics — ship or park decision documented
- **Post-ship health:** zero regressions in unrelated retrieval code (F022, F030, F042) detected via retroactive runs

---

## Resolved decisions

1. **Scope = Nous-internal regression + A/B tool, not external benchmark.** N=20 LongMemEval is one source of four. Publishable/cross-system claims deferred to F051.1.
2. **Eval DB = persistent Docker image, not ephemeral-per-run.** Cost and wall-time of ingestion forbid per-run rebuild. Image versioned by fixture tag.
3. **No CI integration in Phase 1.** Solo developer + LLM non-determinism + privacy surface + redundant manual invocation all argue against GHA. Heartbeat dynamic check is a future option.
4. **Fixtures in separate private repo (`nous-eval-fixtures`).** Public `nous` repo stays free of personal memory data; 10-query smoke subset committed publicly with attribution for LongMemEval MIT terms.
5. **Windows-first CLI ergonomics.** Named volumes, no Makefile, `docker buildx --platform linux/amd64` explicit, forward-slash YAML paths.
6. **Row-level review gate on `ai_hand_labeled`.** Unreviewed rows are informational, reviewed rows are gate-eligible. Intellectual honesty enforced in data.
7. **F050 gate tightened to +7% MRR + no single-source regression > 3%.** Protects against N=20 noise flip-flops more than the original +5% spec value.
8. **`nous_system.eval_runs` on main DB, not eval DB.** Run history must survive eval DB rebuilds. Persist best-effort; never block harness on write failure.
9. **LongMemEval MIT attribution file committed publicly.** The 10-query smoke subset in the public repo must carry the attribution per MIT license; full 500-Q set stays in private fixtures repo.
10. **No schema changes on production Nous DB** beyond `eval_runs`. F051 must be fully removable via `DROP TABLE nous_system.eval_runs; rm -rf nous_eval/` with zero other impact.

---

## Out of scope

- External benchmark comparison claims (deferred to F051.1)
- Answer-quality / end-to-end QA eval (deferred to F051.2)
- Dashboard UI for eval history (deferred to F051.3)
- CI integration (deferred indefinitely; heartbeat dynamic check is the alternative)
- Multi-agent eval (single `agent_id` per run; multi-agent is out of scope)
- Non-English retrieval eval (LongMemEval is English-only, corpus is English-only)
- Adversarial prompt-injection eval for retrieval (separate from F050's injection testing, which lives inside F050 not F051)

---

## Estimated effort

| Component | Est. LOC | Est. time |
|---|---|---|
| `nous_eval/` module (config + registry + loaders + runner + metrics + report) | ~1200 | 1.5 days |
| `Dockerfile.eval-db` + `tasks.py` + docker-compose profile | ~200 | 0.5 days |
| Ingest pipeline (`ingest.py`, `ingest_longmemeval.py`, `probe_gen.py`, `hand_labels_draft.py`) | ~600 | 1 day |
| Tests (unit + integration + smoke fixtures) | ~800 | 1 day |
| Migration (`037_eval_runs.sql`) + Settings additions | ~80 | 0.25 days |
| Documentation (this spec + implementation plan + INDEX + CLAUDE.md) | — | 0.5 days |
| **Phase 1 total** | **~2880 LOC** | **~4.75 days** |
| Phase 2 (ingest run + image push + hand-label review) | 0 (just operator work) | ~0.5 days + ~20 min review |

Fixture ingest cost (one-time, Phase 2): ~$8 LongMemEval + ~$1 silver + ~$0.05 embeddings = **~$10 total**.
