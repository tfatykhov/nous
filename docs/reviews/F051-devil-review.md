# F051 Devil's Advocate Review — nous-eval-devil

**Verdict:** REWORK — multiple P1 phantom-API and silent-failure blockers prevent the plan from being implementable as-written.
**Decision ID:** `f5b5006e`
**Reviewer agent_id:** `nous-eval-devil`
**Date:** 2026-04-20
**Targets:**
- `E:\Projects\nous\docs\features\F051-retrieval-eval-harness.md`
- `E:\Projects\nous\docs\superpowers\plans\2026-04-20-f051-retrieval-eval-harness.md`

## Summary

The plan is well-structured at the module level, but the integration seams with real Nous internals are spec-as-wishful-thinking rather than spec-as-verified-by-code. Three findings would hard-crash on the very first real harness invocation. Five more would produce silent data-quality regressions that defeat the whole point of a regression harness. Two are Windows-specific traps that flip on the target machine. The Docker persistence design has a fundamental conflict between "immutable baked image" and "named volume for persistence" that must be resolved before building fixture v2026-Q2.

**P1 count:** 11 (must fix)
**P2 count:** 7 (should fix)
**P3 count:** 3 (nice to have)

---

## P1 (must-fix before implementation)

### P1-1: [PHANTOM-API] `Heart.recall` has no `session_id` parameter

**Problem.** Plan §5 snippet + §retrieval_runner.py contract explicitly call:

```python
retrieved = await heart.recall(query=qrel.query, limit=top_k, session_id=None)
```

Actual signature at `nous/heart/heart.py:736`:

```python
async def recall(
    self,
    query: str,
    limit: int = 10,
    types: list[str] | None = None,
    session: AsyncSession | None = None,
) -> list[RecallResult]:
```

There is **no** `session_id` kwarg. The `session` kwarg is a SQLAlchemy `AsyncSession`, not a session identifier. Calling with `session_id=None` raises `TypeError: recall() got an unexpected keyword argument 'session_id'` on the very first qrel.

**File/line:** `docs/superpowers/plans/2026-04-20-f051-retrieval-eval-harness.md` lines 246–248, 252. Self-contradicts its own claim "no modification to existing Heart signature".

**Proposed fix.** Change snippet to `await heart.recall(query=qrel.query, limit=top_k)` (omit the phantom kwarg). If conversational memory scoping is required, pass a real `AsyncSession` obtained via `self.db.session()` and drop the `session_id` concept entirely. Add a test that imports and introspects `inspect.signature(Heart.recall)` at test-collection time to prevent regression.

---

### P1-2: [PHANTOM-API] Heart.recall does not search `brain.decisions` — entire "decisions" metric silently zero

**Problem.** `Heart._recall()` at `nous/heart/heart.py:763` builds `search_types = types or ["episode", "fact", "procedure", "censor"]`. Notice: **no `"decision"`**. Brain decisions are stored in `brain.decisions` and are owned by `Brain`, not `Heart`. The harness wires the entire retrieval matrix through `Heart.recall` exclusively (plan §5, §retrieval_runner.py §6).

Yet the spec repeatedly references decisions as part of the retrieval corpus:
- §1 Problem: "hybrid search (F025)...MMR...CE...cross-type graph linking"
- §4 Qrel schema: `memory_types: list[Literal["fact", "decision", "episode", "procedure"]]`
- §Goals §1: "MRR / P@K / R@K / nDCG on a fixed corpus"
- §12 Ingest step 1: "SELECT * FROM heart.facts / brain.decisions / heart.episodes / heart.procedures"

Every qrel whose `gold_ids` points into `brain.decisions` will score **0.0** across all configs, producing a massive silent recall drop that looks like a baseline regression but is actually the harness wiring. Paired deltas between configs on decision-heavy qrels will be ~zero-noise, hiding real signal.

**File/line:** spec §4 line 194 (`Literal["fact", "decision", "episode", "procedure"]`) + §12 step 1; plan §5 retrieval_runner snippet.

**Proposed fix.** Either (a) extend `Heart.recall` (or the harness wrapper) to also query `brain.decisions` via `Brain.query()` and merge with RRF — but this is a non-trivial new integration, not "no modification to existing Heart signature"; OR (b) drop `"decision"` from `memory_types` and the ingest step, and state clearly in §Non-goals that decisions are out of scope for Phase 1 retrieval eval. Option (b) is the honest Phase 1 move. Whichever path is chosen, `Qrel.memory_types` Literal must match what `Heart.recall` actually searches.

---

### P1-3: [PHANTOM-API / SELF-CONTRADICTION] `Heart.close()` does not close the DB pool and "fresh Heart per config = no state bleed" is false

**Problem.** Plan §5 line 252 puts `await heart.close()` in a `finally` block and claims "Heart is instantiated fresh per config (no state bleed)." Actual `Heart.close()` at `nous/heart/heart.py:126`:

```python
async def close(self) -> None:
    """Close owned resources (embedding provider httpx client).
    Only closes the embedding provider if this Heart instance owns it...
```

It closes **only** the `EmbeddingProvider` httpx client, and only when `owns_embeddings=True`. It does **not** reset process-wide singletons that the retrieval pipeline actually reads from:

- **`RuntimeConfig` singleton** (`nous/runtime_config.py:30`) — heart.py:827 reads `RuntimeConfig.get().get_cross_encoder_enabled(self.settings)`. If config `ce_off` calls `RuntimeConfig.get().set_cross_encoder_enabled(False)`, the next config (`baseline`) will **inherit** that override unless `RuntimeConfig.reset()` is explicitly called between configs.
- **Cross-encoder model cache** — F042's cross-encoder is a module-level lazy singleton. First config loads ~80MB of torch weights; subsequent configs reuse. That's accidentally harmless for correctness but explains why "fresh Heart" is a half-truth.

So state DOES bleed across configs on exactly the thing under test (CE toggles, MMR toggles, graph toggles).

**File/line:** plan §5 lines 251–252; §retrieval_runner §6 "Heart is instantiated fresh per config (no state bleed)".

**Proposed fix.** Add explicit `RuntimeConfig.reset()` + re-apply `config.flags` at the start of every config iteration. Update the snippet and the §6 prose to say "RuntimeConfig reset per config; Heart DB pool reused". Add a unit test: build two configs differing only on `cross_encoder_enabled`, verify `RuntimeConfig.get_cross_encoder_enabled()` reflects each config's override during its iteration.

---

### P1-4: [SELF-CONTRADICTION] `Dockerfile.eval-db` uses stock `postgres:17` but `init.sql` requires pgvector extension

**Problem.** Spec §9 Dockerfile-eval-db stage 1:

```dockerfile
FROM postgres:17 AS ingest
...
COPY sql/init.sql /docker-entrypoint-initdb.d/00_init.sql
```

`sql/init.sql:10` starts with `CREATE EXTENSION IF NOT EXISTS vector;`. The stock `postgres:17` image does **not** have the pgvector extension binary installed. The `CREATE EXTENSION` call will fail with `ERROR: could not open extension control file ".../vector.control": No such file or directory`, crashing the build at stage 1's entrypoint before any fixtures load.

Stage 2 has the same `FROM postgres:17` — even if stage 1 somehow worked, runtime queries against `vector(1536)` columns would fail.

The spec §1 ASCII diagram explicitly says "Postgres 17 + pgvector + pre-loaded corpus". Self-contradiction with §9 `FROM postgres:17`.

The existing working `postgres` service in `docker-compose.yml:105` uses `image: pgvector/pgvector:pg17` — the same image must be used for the eval DB.

**File/line:** spec §9 lines 320, 331 (both `FROM postgres:17`); plan §10 Dockerfile.eval-db section; plan §24 integration test says "minimal `postgres:17` container with pgvector" — contradicts its own `FROM`.

**Proposed fix.** Replace both `FROM postgres:17` with `FROM pgvector/pgvector:pg17` in Dockerfile.eval-db. Replace `pytest-docker`-spun `postgres:17` in integration test with `pgvector/pgvector:pg17`. Add an acceptance-criteria check: `docker run --rm <built-image> pg_isready && psql -c 'CREATE EXTENSION IF NOT EXISTS vector'` exits 0.

---

### P1-5: [CONCURRENCY / SILENT-FAILURE] Named-volume + baked-image = stale fixtures on version bump

**Problem.** Spec §10 docker-compose:

```yaml
nous-eval-db:
  image: ghcr.io/tfatykhov/nous-eval-db:${NOUS_EVAL_FIXTURE_VERSION:-latest}
  volumes:
    - nous_eval_db_data:/var/lib/postgresql/data   # Named volume
```

Docker named-volume semantics: on first `up`, docker copies the image's baked `/var/lib/postgresql/data` INTO the empty named volume. On subsequent `up`s with a **different image tag** (e.g., bumping `NOUS_EVAL_FIXTURE_VERSION` from `v2026-Q2` to `v2026-Q3`), the named volume is **non-empty**, so docker does **NOT** overlay the new image's data. Container starts against the OLD fixture data. The fresh image's baked corpus is silently ignored.

The spec §9 entire Dockerfile design is predicated on the image being self-sufficient via COPY --from=ingest. The named volume defeats that design.

§Silent-failure item #12 claims "fixture version mismatch → WARN at startup", but this check (if implemented via the `nous_eval_meta` table) will see the OLD Q2 stamp — and if the image tag env matches, no warning fires. Silent drift guaranteed.

**File/line:** spec §10 lines 368–382; spec §Goals §4 "one-time ingestion, then every subsequent run reuses the baked Postgres state" — this is actually true only on v1, false across version bumps.

**Proposed fix.** Either (a) drop the named volume entirely and run the eval DB as ephemeral — container restart re-mounts baked image data every time; cost is ~3 sec start + Postgres init; acceptable for harness usage; OR (b) keep volume but make `NOUS_EVAL_FIXTURE_VERSION` part of the volume name: `nous_eval_db_data_${NOUS_EVAL_FIXTURE_VERSION}`. Also add explicit `tasks.py reset-eval-db` subcommand that does `docker volume rm` before pull of new tag, and put this in the fixture-refresh runbook.

---

### P1-6: [WINDOWS-BREAK] CRLF line endings on `Dockerfile.eval-db.load.sh` break the Docker build on Windows

**Problem.** Plan §10 writes `Dockerfile.eval-db.load.sh` with bash `set -euo pipefail`. When a fresh clone of the nous repo happens on Windows 11 with default `core.autocrlf=true`, Git converts `*.sh` files to CRLF on checkout. When `COPY Dockerfile.eval-db.load.sh /load.sh` runs inside BuildKit's Linux builder, `bash /load.sh` fails with `$'\r': command not found` on line 2 — a well-documented Windows→Linux shell script trap.

The repo does not have a `.gitattributes` that pins `*.sh` to LF. (Quick check via Grep on root `.gitattributes`: file is absent.)

**File/line:** plan §10 load.sh section; missing root `.gitattributes`.

**Proposed fix.** Add (or augment) `.gitattributes` at repo root:

```
*.sh text eol=lf
Dockerfile* text eol=lf
```

And optionally `RUN sed -i 's/\r$//' /load.sh` defensively inside the Dockerfile before `chmod +x`. Add an acceptance criterion: after fresh clone on Windows, `docker build -f Dockerfile.eval-db .` succeeds.

---

### P1-7: [SILENT-FAILURE] `nous_system.eval_runs` INSERT can block the harness for 30+s despite "never blocks" promise

**Problem.** Spec §11 last paragraph: "Writes are best-effort: if the main `nous` DB is unreachable at run end, the run is logged to stderr as WARN and the report still persists on disk. **Never blocks the harness invocation**."

Plan §cli.py step 11: "INSERT INTO nous_system.eval_runs (best-effort, never blocks)".

The existing main-Nous `Database` pool is asyncpg via SQLAlchemy. When the main nous Postgres is **unreachable** (firewall closed, service down, wrong host), asyncpg's connection attempt blocks for up to 30s (default) or the configured `command_timeout`. "Best-effort" wrapped as a try/except does NOT bound wall-clock time; it bounds exceptions.

A harness run completes metrics + writes report, then spends 30s hanging on DB connection before logging WARN. Worse: if the main DB host resolves but TCP SYN is blackholed (common with corporate firewalls), the timeout can be even longer.

**File/line:** spec §11 line 418; plan §cli.py step 11.

**Proposed fix.** Wrap the insert in `asyncio.wait_for(..., timeout=5.0)` so the harness cannot hang more than 5 seconds on persistence. On timeout, log WARN with duration. Add a unit test that mocks the engine's `.connect()` to sleep 10s and asserts the harness completes within 7s.

---

### P1-8: [SILENT-FAILURE] Integration-test "smoke" uses RANDOM embeddings — proves nothing about retrieval

**Problem.** Plan §25+§26 `tests/fixtures/eval_smoke_corpus.jsonl`: "10 synthetic memory items... Embeddings are random 1536-dim vectors seeded to `0xF051`."

Plan §24 integration test "Runs `cli.main(["--configs", "baseline", "--smoke"])` against that container" and asserts report file exists + JSON schema valid + stdout summary printed.

With random embeddings, cosine similarity between a query embedding (a REAL embedding from the embedding provider at runtime) and the baked-in random vectors is essentially noise. Retrieved ranks are random. Reported MRR will be near zero. The test asserts plumbing, but any regression that breaks ranking logic (e.g., sort direction flipped, top-K truncation bug) will still produce "a report" and "a JSON schema". The assertions are satisfied even when the product is broken.

**File/line:** plan §26 lines 581–584; §24 assertions.

**Proposed fix.** Replace random embeddings with **deterministic content-derived** embeddings: generate embeddings by running the actual embedding provider on each item's text at fixture-creation time, cache the resulting vectors, commit the `.jsonl` with real values. Alternative: use a mock `EmbeddingProvider` that returns a seeded but content-correlated vector (e.g., hash-based with structured clusters so gold IDs legitimately score higher than distractors). Add a unit-level assertion inside the integration test: on the smoke corpus, MRR must be ≥ 0.5 for `baseline` on the `eval_smoke.jsonl` queries — if it's near 0, retrieval is genuinely broken.

---

### P1-9: [SECURITY / SILENT-FAILURE] Default `NOUS_EVAL_DB_PASSWORD=nous_eval` + port 5433 exposed on all interfaces is a LAN-reachable production-derived data leak

**Problem.** Spec §Config: `NOUS_EVAL_DB_PASSWORD` default is `nous_eval` (plaintext trivial).

Plan §10 docker-compose service:

```yaml
ports: ["5433:5432"]
```

Unqualified `5433:5432` in docker-compose publishes on **all interfaces** on Linux (binds `0.0.0.0`). On Windows Docker Desktop, it publishes on the Windows host 5433. Anyone on the same LAN (home wifi, office VPN, hotel network) can connect with `psql -h <windows-machine-ip> -p 5433 -U nous -d nous_eval` using the hardcoded trivial password.

The baked data contains:
- `heart.facts` derived from prod Nous memory — may include names, URLs, personal data
- `heart.episodes` full transcripts from real Tim↔Nous conversations
- Silver qrels extracted from those transcripts

This is production-derived personal data behind a publicly-known password on a LAN-reachable port. "Never touches production DB at runtime" (spec §Goals §7) is true for writes but irrelevant — the privacy surface is the baked image.

**File/line:** spec §Config table (line 480); spec §10 line 370.

**Proposed fix.** (a) Change port publishing to `127.0.0.1:5433:5432` — binds to loopback only, LAN cannot reach. (b) Generate the password at image-build time from a secret passed via `--build-arg` or require operator to override via `NOUS_EVAL_DB_PASSWORD` in a non-committed `.env.eval`. Default must NOT be a literal. (c) Document in fixture-refresh runbook that baked images should never be pushed to a public registry if they contain prod-derived data; GHCR visibility must be private. (d) Consider making the eval image read-only with `--read-only` at runtime to reduce blast radius.

---

### P1-10: [SILENT-FAILURE] Ingest pipeline `NOUS_PROD_DB_*` env vars not defined — falls back to libpq defaults, risk of reading wrong DB

**Problem.** Plan §12 line 462: "Reads from prod Nous DB (via env vars like `NOUS_PROD_DB_*` pointing at SSH tunnel)."

These env vars are not defined in `nous/config.py` `Settings`, not in the plan's EvalSettings, not in `.env.example` (doesn't exist). If the operator forgets to set them, psycopg/asyncpg falls back to libpq defaults: `PGHOST=localhost`, `PGPORT=5432`, `PGUSER=current-OS-user`. On a Windows dev box with a local Postgres running (e.g., from `docker compose up postgres` for dev work), the ingest pipeline silently reads from the LOCAL DEV DB instead of prod — produces fixture v<tag> from wrong source. The mined silver qrels + corpus dump would then contain dev data.

Secondary risk: `SELECT * FROM heart.facts` is not streamed. On a production DB with N facts × 1536-float embeddings, this loads all rows into asyncpg buffers at once. Memory pressure on both ends.

**File/line:** plan §12 line 462, plan §13 line 466.

**Proposed fix.** (a) Declare `NOUS_PROD_DB_HOST`, `NOUS_PROD_DB_PORT`, `NOUS_PROD_DB_USER`, `NOUS_PROD_DB_PASSWORD`, `NOUS_PROD_DB_NAME` in EvalSettings as `Field(..., alias="NOUS_PROD_DB_HOST")` with NO DEFAULT — missing value raises pydantic validation error. (b) Preflight the ingest pipeline with a "sanity query": `SELECT COUNT(*) FROM nous_system.agents` and if count < 10 (or fingerprint matches dev) abort with "Refusing to ingest from dev-looking DB". (c) Use server-side cursor (asyncpg `cursor()` or `stream()`) for the corpus dump.

---

### P1-11: [SILENT-FAILURE] Ingest pipeline re-uses shared event-bus singleton — emits downstream events that can hit prod DB

**Problem.** Plan §13 line 470: "Uses Nous's existing `fact_extractor` + `episode_summarizer` handlers — no re-implementation. Session-id mapping tracked in-memory during the replay."

The real `fact_extractor` (`nous/handlers/fact_extractor.py`) is an event-bus subscriber. When invoked, it (a) reads episode content, (b) emits `FactLearned` events, (c) writes facts to `heart.facts`. In the running Nous process the event bus is a process-wide singleton; handlers registered during normal startup will also subscribe to any bus-emitted events from the ingest process.

If ingest_longmemeval.py imports `nous.main` or any module that registers handlers on the global event bus, the replay triggers handlers that may attempt to write to the bus and cascade → an on-bus handler might call `heart.learn_fact` pointed at whatever DB the Heart singleton was wired to at startup. The plan's claim "using the **same** configuration as prod" (spec §12 step 3) is alarming: same config = pointing at prod DB.

**File/line:** spec §12 line 428; plan §13 line 470.

**Proposed fix.** Ingest pipeline must construct a fully isolated event bus + Heart + handlers wired exclusively to the eval staging target. No import of `nous.main`. Add an assertion at pipeline start that `Heart.db.dsn` contains "eval" or matches the staging target — refuse to run otherwise. Document the isolation invariant in the code comment.

---

## P2 (should-fix)

### P2-1: [WINDOWS-BREAK] `ssh -L` tunneling silently fails if OpenSSH client not installed

Plan §Ingest assumes `ssh -L 15432:localhost:5432 vm` just works. Windows 11 includes OpenSSH client as an optional feature, often disabled on non-developer machines. Running `ssh -L` when the binary is absent fails with "not recognized as an internal or external command" (cmd), `command not found` (Git Bash), or a PS `CommandNotFoundException`. ingest.py should run `shutil.which("ssh")` preflight and produce a helpful Windows-specific error linking to the "Add Optional Feature: OpenSSH Client" docs.

### P2-2: [CONCURRENCY] Report filename collision on same-UTC-second run

Plan §8 "`reports/<utc_timestamp>_<configs_joined>.md`". Timestamp resolution unspecified; if seconds-only, two back-to-back runs with same `--configs` clobber. Add microsecond precision or PID suffix: `<utc_ts>_<pid>_<configs>.md`.

### P2-3: [SELF-CONTRADICTION] WSL2 port-forward can succeed on TCP handshake but fail Postgres handshake

Spec §14 "Port 5433 conflict detection: `socket.socket().connect_ex(...)`". On Windows with WSL2 backend, port-forwarded TCP sometimes connects but the stream is reset before Postgres auth completes. The preflight check says "OK", the real pool init says "connection reset". Add a second-level check: use asyncpg `connect(..., timeout=3)` in a try/except during preflight — if that fails, surface a specific WSL2-hint error.

### P2-4: [SILENT-FAILURE] `eval_probes.jsonl` goes stale as new features ship

Plan §14 + §25 commit a deterministic probe fixture. No mechanism regenerates it when INDEX.md grows (F052, F053, ...). Add a test `test_probes_cover_all_current_features` that parses INDEX.md and asserts at least one probe per shipped F-number. Test fails → `tasks.py probe-gen --regenerate-public` must be re-run.

### P2-5: [SILENT-FAILURE] nDCG paired-delta math does not preserve variance reduction

Plan §6 line 299: "nDCG falls back to mean-of-means with a comment." Correct numerically but loses the paired-A/B variance benefit. With N=20 qrels per source, nDCG paired tests have high variance. Either (a) document that nDCG is informational-only for gate decisions, not a gate metric; OR (b) implement nDCG as per-qrel avg correctly (it IS a per-query metric — IDCG is per-query, so per-qrel averaging is the right definition). Plan's fallback math is wrong — per-qrel nDCG averaging is standard IR practice.

### P2-6: [PHANTOM-API / WINDOWS-BREAK] pytest-docker + Windows Docker socket discovery

Plan §24 uses pytest-docker. On Windows with WSL2 Docker Engine (no Desktop), socket discovery requires specific DOCKER_HOST. Plan says "If Docker unavailable on the test runner, the integration test skips with a clear reason" — but "Docker available" and "pytest-docker can find socket on Windows" are distinct. Add explicit check: `docker.from_env().ping()` before delegating to pytest-docker, skip with Windows-specific instructions if ping fails.

### P2-7: [SILENT-FAILURE] Image size 500MB budget + VACUUM FULL contention

Spec §9 budget ≤500MB. Corpus with ~10K memory rows × 1536-float embeddings + HNSW indexes (5-10× embedding size) + WAL + stage-2 Postgres base (~150MB) makes 500MB aggressive. No clean error path in `tasks.py push-image` when push fails due to size. Add: after build, `docker image inspect <tag> --format '{{.Size}}'` — fail the task if > 500MB with "Reduce N of mined episodes or move to layered image".

---

## P3 (nice-to-have)

### P3-1: [SILENT-FAILURE] `git_sha` in report requires clean working tree

Report header §8 includes `git_sha=a1b2c3d`. If dev has uncommitted changes, the sha is misleading (points at HEAD but code differs). Append `-dirty` when `git diff --quiet` fails.

### P3-2: [PHANTOM-API] Plan §15 `hand_labels_draft.py` uses `claude-sonnet-4-6` default

Plan line 446 `model: str = "claude-sonnet-4-6"`. CLAUDE.md env-var table points at `claude-sonnet-4-6` too. Verify exactly which Sonnet revision is in prod; a mismatch creates drift between hand-label draft quality and operational models.

### P3-3: [SILENT-FAILURE] eval_runs row retention unbounded

Spec §Risks mentions partitioning by created_at "deferred to F051.3". For a solo-dev cadence the table grows slowly — fine for Phase 1. But add a `LIMIT 1000` to the dashboard/history query site so a table of 100K rows doesn't brick the dashboard once F051.3 lands.

---

## Pattern observed

**Spec-without-code-verification phantom API.** Three of the four P1 phantom-APIs (P1-1, P1-2, P1-3) share a root cause: the spec describes an idealized `Heart.recall` that searches all four memory types with a session-scoped invocation, and the plan propagates that ideal without grepping heart.py to check. This pattern matches decision `fcf9dcdb` (F042 devil review — tuple-vs-list contradiction) and decision `22113f66` (#216 — `get_by_name` missing active filter). **Recommended mitigation:** before any spec claims an external API signature, require the author to paste the actual `async def` line from the source as a comment. Cheaper than three rounds of revision.

---

## Must-fix before proceeding to implementation

The following P1s block implementation entirely — a subagent writing code against the current plan would produce broken output on the first run:

- **P1-1** phantom `session_id` kwarg — breaks first qrel call
- **P1-2** decisions not searched — breaks half the corpus silently
- **P1-3** state bleed across configs — breaks the entire A/B comparison premise
- **P1-4** pgvector not in eval image — breaks Docker build at init.sql
- **P1-5** named-volume + baked-image conflict — breaks fixture version bumps

P1-6..P1-11 are fixable in parallel but must be addressed before merge.

**Verdict stands:** REWORK. The spec + plan need another iteration before implementation dispatch.
