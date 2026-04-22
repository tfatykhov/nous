# F051 Architecture Review — nous-eval-arch

**Verdict:** REWORK
**Decision ID:** fd69ffe1
**Reviewer:** nous-eval-arch
**Date:** 2026-04-20
**Artifacts reviewed:**
- `E:\Projects\nous\docs\features\F051-retrieval-eval-harness.md`
- `E:\Projects\nous\docs\superpowers\plans\2026-04-20-f051-retrieval-eval-harness.md`

The overall architecture is defensible, the silent-failure surface is unusually well-enumerated, and the separation of concerns between core / infra / tests is clean. However the plan contains several wiring-level errors that will cause the harness to measure the wrong thing, silently return zero results, or raise TypeErrors on the first call. Every P1 below is a concrete break traceable to a specific file:line. Fix the P1s and this becomes APPROVE WITH REVISIONS.

---

## P1 (must-fix before implementation)

### P1-1: `heart.recall()` is NOT the production retrieval path — graph/spreading/contradiction flags are dead letters

**Problem.** The plan calls `await heart.recall(query=..., limit=top_k, session_id=None)` from `retrieval_runner.py` and assumes retrieval is gated by `graph_recall_enabled`, `spreading_activation_enabled`, `cross_type_linking_enabled`, and `contradiction_detection`. It isn't. Those four settings are consumed inside the `recall_deep` **tool dispatcher** at `nous/api/tools.py:365, 398, 403, 482` — NOT inside `Heart.recall` (`nous/heart/heart.py:736-854`). `Heart.recall` does RRF + CE rerank + MMR and returns, full stop. Graph expansion, spreading activation, and contradiction surfacing happen one layer up.

This means:
- The `graph_off` config in the RetrievalConfig matrix is a no-op.
- `f050_on` (query expansion, once F050 lands) will only A/B whatever layer actually owns expansion — if that lives in `tools.py` too, same problem.
- The harness silently measures a different pipeline than production uses when the user asks via `recall_deep`.

**Proposed fix.** Extract the `recall_deep` retrieval pipeline from `nous/api/tools.py:340-510` into a reusable `async def recall_expanded(heart, brain, query, ..., settings) -> list[RecallResult]` in a new module (e.g. `nous/retrieval/pipeline.py`). Both the tool dispatcher AND F051's `retrieval_runner` call this shared function. Plan §6's `run_matrix` changes one line: `retrieved = await recall_expanded(heart, brain, qrel.query, limit=top_k, settings=per_config_settings)`. Without this refactor F051 is measuring an incomplete pipeline and the whole feature's value proposition collapses.

### P1-2: `heart.recall` signature does NOT accept `session_id`

**Problem.** Plan §6 code block:
```python
retrieved = await heart.recall(
    query=qrel.query, limit=top_k, session_id=None,
)
```
Actual signature at `nous/heart/heart.py:736`:
```python
async def recall(self, query: str, limit: int = 10,
                 types: list[str] | None = None,
                 session: AsyncSession | None = None) -> list[RecallResult]:
```
There is no `session_id` kwarg. The call will raise `TypeError` immediately. The parameter is `session` (an `AsyncSession`), not `session_id`.

**Proposed fix.** Drop `session_id=None` entirely — `session=None` is the default and the plan wants exactly that.

### P1-3: `RuntimeConfig` is a process-wide singleton; per-config Settings overrides are a partial lie

**Problem.** Plan §6 claims each config applies its `flags` as per-instance Settings overrides, and explicitly rejects `RuntimeConfig` because it's "too global". But three retrieval knobs that the plan wants to A/B are resolved through `RuntimeConfig.get()`:
- `cross_encoder_enabled` → `nous/heart/heart.py:827` calls `RuntimeConfig.get().get_cross_encoder_enabled(self.settings)`
- `vector_weight` → `nous/heart/search.py:43` calls `RuntimeConfig.get().get_vector_weight(settings)`
- `rrf_k` → `nous/heart/search.py:55`

Worse, `_resolve_vector_weight` / `_resolve_rrf_k` in `search.py:34-55` construct a **fresh `Settings()` from os.environ** internally (they do not receive `self.settings`), so per-instance Settings overrides don't flow through at all for those two. For `cross_encoder_enabled` the resolver does receive `self.settings`, so per-config overrides flow through — UNLESS a previous harness run (or a concurrent prod process) has called `set_cross_encoder_enabled(...)` on the singleton, in which case the runtime override silently wins.

Net effect: `ce_off` partially works (flag flows through only when no runtime override exists), and any vector_weight A/B is impossible without code changes.

**Proposed fix.** Two parts.
1. At matrix start, call `RuntimeConfig.reset()` and assert no persisted overrides exist in `nous_system.config`. Document that harness invocation is mutually exclusive with prod `/admin/search-weights` edits.
2. Either (a) pass `vector_weight` explicitly into `hybrid_search` calls from the shared pipeline per P1-1, OR (b) refactor `_resolve_vector_weight` / `_resolve_rrf_k` to accept a `Settings` parameter and have Heart pass `self.settings`.

Plan must acknowledge that `mmr_enabled` reads directly from `self.settings` (heart.py:857) while the three above go through the singleton — the override mechanism is asymmetric today.

### P1-4: `agent_id` mismatch between eval DB corpus and harness Heart → zero results

**Problem.** Every Heart search filters `WHERE agent_id = self.agent_id`. The eval DB is populated by cloning prod `heart.facts` / `brain.decisions` / etc., whose rows carry the prod `agent_id` (`nous-default`). The plan's `run_matrix` defaults `agent_id="nous-eval"` (Core §Files#5 line 242). If the ingest pipeline does NOT rewrite `agent_id` to `"nous-eval"` during JSONL export or corpus load, every `heart.recall` call returns `[]` silently. MRR / P@K / R@K all compute as `0.0`. No error, no warning — just every metric is 0.

The plan mentions "agent_id injection" in `tests/eval/test_corpus_loader.py` (Core §Files#4 line 499) but does not specify the policy for production ingestion.

**Proposed fix.** Make it explicit in `corpus_loader.py` and `ingest.py`: all rows written to the eval DB carry `agent_id = <FIXTURE_AGENT_ID>` (a constant like `"nous-eval-fixture"`). The harness default becomes `agent_id="nous-eval-fixture"` matching. Add a test `test_corpus_loader.test_rewrites_agent_id_on_load` to the Tests agent's list. Add a startup assertion that the eval DB has at least one row where `agent_id = EvalSettings.agent_id` — fail loudly if zero.

### P1-5: Per-qrel paired-delta math is NOT different from mean-of-means (claim is wrong)

**Problem.** Spec §7 and plan §Core#6 (line 299) both claim paired-averaging of per-qrel deltas "reduces variance from per-qrel noise" and that it equals mean-of-means for MRR/P@K but NOT for nDCG. The point-estimate claim is mathematically false for all four metrics.

Each of MRR, P@K, R@K, nDCG@K is a per-qrel **scalar**. For any per-qrel scalar metric `m_i`:
```
mean_of_means   = (1/N) Σ m_b_i − (1/N) Σ m_e_i
paired_average  = (1/N) Σ (m_b_i − m_e_i)
                = (1/N) Σ m_b_i − (1/N) Σ m_e_i    # linearity
                = mean_of_means
```
They're identical point estimates. What differs is **variance** of the estimator: paired Δ has smaller variance when per-qrel scores are correlated across configs (standard paired-test logic). The plan conflates variance with point estimate.

This isn't just cosmetic — `test_metrics.test_paired_delta_averages_per_qrel_deltas` (plan Tests §Files#20) is designed to distinguish paired-avg from mean-of-means. That test will either pass trivially (they're equal) or fail (if the implementation diverges due to floating-point ordering). Either way the stated justification is wrong.

**Proposed fix.** Either (a) replace the "reduces variance" claim with a correct one: "paired analysis lets us report per-qrel standard error and p-value, not just a point estimate" — and implement a bootstrap CI for each paired Δ; OR (b) drop the distinction entirely and compute means of per-qrel metrics. Test `test_paired_delta_averages_per_qrel_deltas` should be rewritten to verify the standard error / CI computation, not a point-estimate difference.

### P1-6: `EvalSettings.dsn()` is not compatible with `Database(settings)` — they expect `.db_url`

**Problem.** Plan §Core#2 gives `EvalSettings` a `dsn() -> str` method. But `nous/storage/database.py:19` reads `settings.db_url` (a property, not a method). Passing `EvalSettings` to `Database(settings=eval_settings)` will raise `AttributeError: 'EvalSettings' object has no attribute 'db_url'`.

Also: `Database.connect()` at `database.py:28-40` requires schemas `{brain, heart, nous_system}` to exist — the eval DB has them (via init.sql) so that's fine. But Database also relies on `settings.db_pool_size`, `settings.db_max_overflow`, `settings.log_level`. EvalSettings has none of these.

**Proposed fix.** Either (a) rename `EvalSettings.dsn()` to a `db_url` property AND add the pool/log fields (duplicates three Settings fields but decouples cleanly), OR (b) have the eval CLI construct a `Settings` override: `main_settings = Settings(db_host=eval.db_host, db_port=eval.db_port, db_user=eval.db_user, db_password=eval.db_password, db_name=eval.db_name, agent_id=eval.agent_id)` and pass that to `Database`. Option (b) is simpler and avoids drift.

---

## P2 (should-fix)

### P2-1: Smoke mode has no corpus to retrieve against → probes qrels score 0 silently

**Problem.** Spec's "graceful degradation" says when `NOUS_EVAL_FIXTURES_DIR` is unset, only `probes` (`requires_fixtures_dir: false`) loads from `tests/fixtures/eval_probes.jsonl`. But the probes qrels reference UUIDs that have to exist in a database somewhere. In pure smoke mode the eval DB container isn't running (acceptance criterion #4: "runs without crashing — even without `nous-eval-db` container up"). If the harness proceeds past the port-5433 preflight check (it won't with the proposed preflight), every query returns 0 results. The plan hides the contradiction between "smoke mode runs without nous-eval-db" and "probes reference UUIDs in heart.facts".

**Proposed fix.** Require smoke mode to spin up a minimal in-memory or tempfile Postgres (testcontainers / pytest-docker style) loaded from `tests/fixtures/eval_smoke_corpus.jsonl`. OR re-define smoke mode as "runs offline, reports 'insufficient infrastructure', exits 0" — no retrieval at all, just a plan validation. Pick one and make it explicit.

### P2-2: Single-source regression check is noise-sensitive at N=20

**Problem.** Gate = "+7% aggregate MRR AND no single-source regression > 3%". With gate-eligible sources `longmemeval` (N=20) and `probes` (N=20), one wrong rank on a single qrel shifts per-source MRR by ~5%, easily tripping the 3% threshold even when the aggregate improves. The gate will flap.

**Proposed fix.** Replace the point-estimate check with a bootstrap CI: "gate fails if any gate-eligible source's paired-Δ 90% CI upper bound is below -3%". Or raise minimum N per gate-eligible source to 50 (would require expanding LongMemEval subset). Document which choice.

### P2-3: Fixture version drift — `NOUS_EVAL_FIXTURE_VERSION=latest` is a reproducibility hazard

**Problem.** `docker-compose.yml` pins `${NOUS_EVAL_FIXTURE_VERSION:-latest}`. Default `latest` tag floats. The `nous_system.eval_runs` row records `git_sha` of the Nous repo, but the corpus may be from a newer ingest cycle. Two runs on the same `git_sha` may produce different numbers depending on when the eval DB image was last pulled.

**Proposed fix.** At harness startup, read the image's `org.nous.fixture_version` label (plan §9 already sets this) and the EvalSettings.fixture_version. If they disagree OR if image tag == `latest`, fail loudly unless `--yes-version-mismatch` is passed. Plan's current silent-failure §12 proposes WARN-only; that's insufficient for a gate-metric correctness invariant.

### P2-4: Ingest pipeline step 3 (LongMemEval replay) has an unspecified write target

**Problem.** Spec §12 step 3 "replay each question's chat_history through Nous's fact extractor + episode summarizer pipeline" — but where does the extractor write? If pointed at prod `heart.facts`, it pollutes prod. If pointed at eval DB, eval DB isn't built yet at ingest time. Plan doesn't resolve this.

**Proposed fix.** Ingest spins up a throwaway Postgres container, runs the replay against it, exports resulting rows to JSONL, tears the container down. Document in `ingest_longmemeval.py` docstring.

### P2-5: Heart.close does NOT dispose the Database pool; plan must clarify ownership

**Problem.** Plan §6 says "Heart is instantiated fresh per config. The eval DB connection pool is reused across configs." But `Heart.close` at `nous/heart/heart.py:126-134` only closes the embedding provider's httpx client (and only if `_owns_embeddings=True`). It does not touch the Database pool. That behavior is actually correct for the plan's intent, but the plan phrasing suggests `heart.close()` is doing cleanup it isn't.

Additional subtlety: if `owns_embeddings=True` is used per-config, each Heart spins a fresh httpx client and tears it down. If `owns_embeddings=False`, the CLI must explicitly close the shared provider AFTER `run_matrix` returns.

**Proposed fix.** Document in `retrieval_runner.py`: "Heart is constructed with `owns_embeddings=False`; a single `EmbeddingProvider` is built at CLI startup and closed in a `finally` after `run_matrix`. Heart.close per config is a no-op for embeddings but kept for future-proofing."

### P2-6: Plan vs spec disagreement on where EvalSettings lives

**Problem.** Spec §Config says "New env vars in `nous.config.Settings`" and lists 12 `NOUS_EVAL_*` env vars. Plan §Core#2 explicitly says `EvalSettings` must NOT inherit from Settings and must be its own pydantic-settings class with `env_prefix="NOUS_EVAL_"`. These contradict each other.

The plan's decision is correct (separate class, clean env namespace), but the spec needs updating to match, otherwise subagents will diverge based on which doc they read first.

**Proposed fix.** Update spec §Config to say "New env vars in a separate `nous_eval.config.EvalSettings` class (pydantic-settings, env_prefix `NOUS_EVAL_`)" and move the table there.

### P2-7: `nous_system.eval_runs` migration gets applied to both DBs — clarify

**Problem.** The eval DB image build (stage 1 load.sh) applies all `sql/migrations/*.sql`, so migration 037 creates `nous_system.eval_runs` in the eval DB too. Spec §11 says the table "lives on the main nous DB, not the eval DB". That's a runtime-write semantic, not a schema-existence semantic. The spec phrasing implies 037 should be excluded from the eval DB image, which would complicate the image build.

**Proposed fix.** Clarify: the table exists in both DBs (shared migration set) but the harness writes only to the main DB's copy. Harmless duplication; no image-build changes needed. Update spec §11 to say this explicitly.

---

## P3 (nice-to-have)

### P3-1: Migration 037 uses `TEXT` where rest of schema uses `VARCHAR(100)` for agent_id

The spec's 037 defines `agent_id TEXT NOT NULL`. Existing tables consistently use `VARCHAR(100)`. Functionally identical in Postgres but stylistically inconsistent. Use `VARCHAR(100)`.

### P3-2: `pg_ctl -m fast -w stop` at end of load.sh is correct but deserves a comment

The plan's `load.sh` ends with `pg_ctl -D "$PGDATA" -m fast -w stop` — this IS a clean checkpoint-complete shutdown and stage-2 can consume the data dir. However, if someone later changes to `-m immediate` thinking it's "faster", WAL recovery fires on stage-2 start and slows container startup. Add a comment in `load.sh`: `# Must be -m fast (not immediate); immediate leaves WAL dirty.`

### P3-3: Wire `RuntimeConfig.reset()` into smoke-mode fixtures

Add `RuntimeConfig.reset()` to `tests/eval/conftest.py` autouse fixture so unit tests don't pick up stray singleton state from other test modules.

### P3-4: Docker RUN-phase `pg_ctl start` in load.sh requires initialized PGDATA

Stage 1's `pg_ctl start` won't work unless `/var/lib/postgresql/data` is initialized. The postgres:17 base image's entrypoint does `initdb` automatically — but `load.sh` is invoked via `RUN`, not `ENTRYPOINT`, so the init is skipped. The load script needs to either run `docker-entrypoint.sh postgres &` and wait for ready, OR explicitly `initdb -D $PGDATA` before `pg_ctl start`. Verify during Phase 1 manual smoke.

---

## Strengths

1. **Silent-failure enumeration (spec §"Silent-failure surface")** is thorough — 12 concrete paths with explicit behavior. Most features ship without this; F051 is better.
2. **Row-level `reviewed_by` gate** on AI-hand-labeled qrels is intellectually honest. Informational vs. gate-eligible separation prevents unreviewed labels from polluting merge-decisions.
3. **Named volumes + Python `tasks.py` runner + `docker buildx --platform linux/amd64`** — Windows-compat thinking is baked in from day 1 rather than discovered during first run.
4. **Purely additive** — no changes to production retrieval code paths means blast radius is exactly `nous_eval/` + one migration + one docker-compose service block. Revocable via `rm -rf` + `DROP TABLE`.
5. **Separate private fixtures repo** keeps personal memory data out of the public repo while the 10-query smoke subset remains publicly reproducible.
6. **Persistent Docker image pattern** (stage-1 ingest → stage-2 consumable) avoids the naive "rebuild Postgres state every run" trap.
7. **Test coverage map per silent-failure item** in plan §"Silent-failure coverage map" is a genuinely good verification artifact.
8. **3-config starting matrix (baseline / f050_on / ce_off / mmr_off / graph_off)** is the right scope for Phase 1 — doesn't overreach.

---

## Recommendation

**REWORK.** P1-1 is architectural (the harness measures the wrong pipeline) and requires a pre-implementation refactor to extract the `recall_deep` pipeline into a shared function. P1-3 is a wiring trap that will cause the flag A/B to lie half the time. P1-4 (agent_id mismatch) causes silent all-zero metrics. P1-5 (paired-delta math justification) undermines a core claim of the spec. None of these are resolvable by code review during implementation; they need to be addressed in the plan before the three subagents start. P1-2 and P1-6 are one-line fixes.

Once the P1s are resolved, this becomes APPROVE WITH REVISIONS (P2s can be folded in during implementation without replanning).
