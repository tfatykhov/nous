# F051 Python-Pro Review — nous-eval-python-pro

**Verdict:** APPROVE WITH REVISIONS
**Decision ID:** 5675e0ed
**Date:** 2026-04-20
**Reviewer:** nous-eval-python-pro (Python idiomaticity / correctness / maintainability angle)

**Scope of this review:** Python 3.12+ idioms, pydantic v2 / pydantic-settings, asyncio lifecycle, typing, CLI / packaging, deps, test-harness conventions. Architecture and devil's-advocate concerns are for the sister reviewers.

**Summary:** The plan is broadly sound and well-matched to the existing Nous codebase style. I found **2 P1s** (one correctness bug in the CLI invocation, one unnecessary dep), **7 P2s**, and **5 P3s**. None are structural — all are localized edits to the plan §Files sketches.

---

## P1 (must-fix)

### P1-1: `python -m nous_eval.retrieval` will crash — no such module

**File/line:** plan §Acceptance criteria #4, §Files §8, spec §Goals #1 (`uv run python -m nous_eval.retrieval`)

**Problem:** The plan's CLI invocation is `uv run python -m nous_eval.retrieval`. Python's `-m` flag requires one of:
- `nous_eval/retrieval.py` containing `if __name__ == "__main__":`
- `nous_eval/retrieval/` package with `__main__.py`

Neither exists in the plan's file list. The actual entry-point module per §Files §8 is `nous_eval/cli.py`, and §Files §5 defines `nous_eval/retrieval_runner.py` (which is the runner, not a CLI). Running `python -m nous_eval.retrieval` today would raise `No module named nous_eval.retrieval`.

The plan also references `python -m nous_eval.rebuild` and `python -m nous_eval.ingest` (§Files §8 last line) — same issue; these modules don't exist in the file list.

**Proposed fix (cheapest):** Rename `nous_eval/cli.py` → `nous_eval/retrieval.py`, and make `nous_eval/tasks.py` / `nous_eval/ingest.py` the real modules for their `-m` invocations. Each exposes `main(argv)` + `if __name__ == "__main__": raise SystemExit(main())`. So:

```
nous_eval/retrieval.py       # was cli.py — entry for `python -m nous_eval.retrieval`
nous_eval/retrieval_runner.py  # unchanged — internal matrix runner
nous_eval/tasks.py             # entry for `python -m nous_eval.tasks`
nous_eval/ingest.py            # entry for `python -m nous_eval.ingest`
```

and every entry module ends with:

```python
if __name__ == "__main__":
    raise SystemExit(main())
```

Consider adding a `tests/eval/test_module_entrypoints.py` that does `import nous_eval.retrieval` and `runpy.run_module("nous_eval.retrieval", run_name="__main__")` with `["--help"]` args to catch this class of regression.

### P1-2: `numpy` is not a dep — drop it, use `statistics`

**File/line:** plan §Files §6 (`import numpy as np`), spec §7 ("vectorized via numpy")

**Problem:** `pyproject.toml` currently has **no** numpy dep. Adding it solely for `metrics.py` over 100 qrels × 5 configs = 500 data points is over-engineering: pure Python + `statistics.mean` / list comprehensions run in well under 1ms at that scale. numpy adds ~15 MB install cost and ~100 ms import latency to every harness invocation for a workload that is trivially tractable without it.

The real costs numpy pays for itself on are 10⁴+ element vector ops; we're at ~500.

**Proposed fix:** Drop the import. Rewrite `compute_metrics` as:

```python
from statistics import mean

def compute_metrics(per_qrel: list[QrelResult], top_k: int = 10) -> MetricsResult:
    ranks = [q.rank_of_first_gold for q in per_qrel]
    mrr = mean(1.0 / r if r else 0.0 for r in ranks) if ranks else 0.0
    p_at_1 = mean(1.0 if q.n_gold_in_top_k_at(1) else 0.0 for q in per_qrel)
    # ... etc
```

If you ever need numpy later (F051.2 answer-quality eval with embedding-space scoring), add it then under an `eval` optional extra, keeping the core harness dep-light.

---

## P2 (should-fix)

### P2-1: `model_config = {...}` dict — use `SettingsConfigDict` for parity with `nous/config.py`

**File/line:** plan §Files §2 (`model_config = {"env_prefix": "NOUS_EVAL_", ...}`)

**Problem:** `nous/config.py:15` uses:

```python
model_config = SettingsConfigDict(env_prefix="NOUS_", env_file=".env")
```

The plan uses a raw dict literal. Both are behaviorally valid in pydantic-settings v2, but `SettingsConfigDict` is a `TypedDict` that gives IDE autocomplete, catches typos (e.g. `env_previx`) at type-check time, and matches the rest of the codebase.

**Proposed fix:**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class EvalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NOUS_EVAL_",
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )
```

Also consider: the plan explicitly sets `extra="ignore"` — `nous/config.py` relies on pydantic's default (`ignore`), so this is redundant. Harmless but noisy. Strip it.

### P2-2: Heart lifecycle — prefer `async with` over try/finally

**File/line:** plan §Files §5 (`run_matrix` sketch), spec §6

**Problem:** Spec §6 sketch uses:

```python
heart = await _build_heart_for_config(eval_db, config)
try:
    ...
finally:
    await heart.close()
```

`Heart` implements `__aenter__` / `__aexit__` (verified: `nous/heart/heart.py:136-140`). The try/finally works but `async with` is the idiomatic Python pattern — it composes correctly with CancelledError propagation during asyncio.run shutdown and makes the ownership explicit.

**Proposed fix:**

```python
async for config in configs:  # conceptual
    async with await _build_heart_for_config(eval_db, config) as heart:
        per_qrel: list[QrelResult] = []
        for qrel in qrels:
            ...
        results.append(RunResult(config=config, per_qrel=per_qrel))
```

(or equivalent `async with _build_heart_for_config(...) as heart:` if the builder is itself an async context manager — cleaner).

### P2-3: Disable event bus / fact extraction / sleep in eval Heart

**File/line:** plan §Files §5 (`_build_heart_for_config`)

**Problem:** Spec says "eval runtime is read-only on `nous_eval`", but the plan doesn't specify which Settings flags `_build_heart_for_config` overrides. Heart, when constructed with default Settings, will spin up:
- `event_bus_enabled=True`
- `fact_extraction_enabled=True`
- `episode_summary_enabled=True`
- `sleep_enabled=True`
- `actionability_backfill_on_startup=True` (F047)
- `decision_review_enabled=True`

Background tasks from these will start on `Heart.__init__` / initialization paths, then the harness's `asyncio.run` will tear them down ungracefully when `_run` returns. Result: warning spam, potentially orphaned connection-pool handles, and (worst case) stray writes to `heart.working_memory` or the event bus stream.

**Proposed fix:** In `_build_heart_for_config`, start from Settings with these explicitly disabled:

```python
def _eval_settings(base: Settings, overrides: dict[str, Any]) -> Settings:
    eval_flags = {
        "event_bus_enabled": False,
        "fact_extraction_enabled": False,
        "episode_summary_enabled": False,
        "sleep_enabled": False,
        "actionability_backfill_on_startup": False,
        "decision_review_enabled": False,
        "correction_extraction_enabled": False,
        "subtask_enabled": False,
        "schedule_enabled": False,
        "heartbeat_enabled": False,
    }
    return base.model_copy(update={**eval_flags, **overrides})
```

Add a test (`test_eval_settings_disables_background_systems`) asserting all ten flags are False on the Settings that gets passed to Heart.

### P2-4: Per-qrel exception handler must log `exc_info=True` and capture exception type

**File/line:** plan §Files §5 docstring (`Per-qrel exceptions are caught, logged, and yield a QrelResult with retrieved_ids=[]`)

**Problem:** This is a replay of prior F001 review finding 71f48548-P1-3 ("Judge.evaluate currently swallows all exceptions"). The silent-failure risk: any bug in Heart.recall silently produces zero-retrieval scores for that qrel, which still gets averaged into MRR / P@K as a 0.0, which looks like "bad retrieval" but is actually a code bug.

**Proposed fix:** The caught-exception path must:
1. `logger.warning("F051: qrel failure qrel_idx=%d query=%r", qrel.index, qrel.query, exc_info=True)` — with `exc_info=True` so traceback survives.
2. Attach the exception class name to the `QrelResult` (add an optional `error: str | None = None` field) so `compute_metrics` can *exclude* failed qrels from the denominator instead of scoring them 0.
3. Surface the per-source failure count in the report header:

   ```
   longmemeval (N=20)  MRR 0.58 → 0.64 (+10.3%)   [2 qrels excluded: RetryError, TimeoutError]
   ```

Crash-counted-as-zero is the F001 silent-failure pattern that review caught; don't re-ship it in F051.

### P2-5: `datetime.utcnow()` will break on 3.14 — use `datetime.now(timezone.utc)`

**File/line:** plan §Files §7 (`from datetime import datetime, timezone`)

**Problem:** Python 3.12 deprecated `datetime.utcnow()`; per prior review b60107eb-P1-2, Nous tests run on 3.14 in CI where this triggers `DeprecationWarning` (and will be removed in a future Python). The import shows awareness but the plan doesn't exhibit the call site.

**Proposed fix:** Everywhere a UTC "now" is needed in `report.py` / `cli.py` / elsewhere:

```python
from datetime import datetime, timezone
timestamp = datetime.now(tz=timezone.utc)
```

Never `datetime.utcnow()`. Consider a grep-linter in `tests/eval/test_no_utcnow.py`:

```python
def test_no_utcnow_in_eval():
    for path in Path("nous/eval").rglob("*.py"):
        assert "utcnow(" not in path.read_text(), f"utcnow found in {path}"
```

### P2-6: Drop `pytest-docker` — use a `@pytest.mark.integration` fixture that shells out to `docker compose`

**File/line:** plan §Files §24 (`pytest-docker fixture starts a minimal postgres:17 container`)

**Problem:** `pytest-docker` is not in `pyproject.toml`. Adding it for one integration test is scope creep. Nous's existing integration pattern (`tests/test_mmr_integration.py`, `tests/test_admission_integration.py`) uses mocked Heart or real Postgres via the existing `conftest.py::USE_POSTGRES` switch.

**Proposed fix:** Write the integration test to **reuse the already-running** `nous-eval-db` container (started by `docker compose --profile eval up -d nous-eval-db`), just like the existing pattern assumes `postgres:5432` is up. Preflight in the fixture:

```python
@pytest.fixture(scope="session")
def eval_db_available():
    import socket
    with socket.socket() as s:
        try:
            s.settimeout(0.5)
            s.connect(("localhost", 5433))
        except OSError:
            pytest.skip("eval DB not running — `docker compose --profile eval up -d nous-eval-db` first")
```

Same UX, zero new deps, matches existing integration-test ergonomics.

If the operator experience of "developer must manually start the eval DB" is unacceptable, the fallback is `subprocess.run(["docker", "compose", "--profile", "eval", "up", "-d", "nous-eval-db"], check=True)` in a session-scoped fixture — still zero new deps.

### P2-7: `@pytest.mark.integration` semantics — clarify which DB

**File/line:** pyproject.toml:57-59 + plan §Files §24

**Problem:** The existing `integration` mark is documented as "requires live PostgreSQL (run with --integration)". Nous's integration tests today assume `postgres:5432` (main DB) is up. F051's integration test requires `nous-eval-db:5433`. Using the same mark is ambiguous — `pytest --integration` users will hit "connection refused on 5433" with no hint that they also need `--profile eval`.

**Proposed fix:** Option A (minimal): update pyproject.toml `markers` entry to document both requirements:

```toml
markers = [
    "integration: requires live PostgreSQL (run with --integration). Some tests additionally require the eval DB on :5433 (docker compose --profile eval up -d nous-eval-db).",
]
```

Option B (cleaner): add a second mark `eval_integration` for F051's tests, registered in pyproject.toml and skipped by default unless `--eval-integration` is passed (paralleling conftest.py's `--integration` flag handling).

---

## P3 (nice-to-have)

### P3-1: Mixing `Literal` and `Enum` in `Qrel` is inconsistent

**File/line:** plan §Files §4 (`source: QrelSource` + `confidence: Literal["high", ...]`)

**Observation:** `QrelSource(str, Enum)` and `Literal["high", "medium", "low"]` behave similarly under pydantic v2 JSON round-trip (both are serialized as strings, both support `==` comparison with plain strings), but using both in the same model is inconsistent style. Neither `QrelSource` nor the confidence values have methods, aliases, or inheritance that argue for Enum over Literal.

**Proposed fix (optional):** Unify to `Literal` everywhere:

```python
QrelSourceType = Literal[
    "longmemeval", "ai_hand_labeled", "probes",
    "silver_episodes", "synthetic_haiku",
]

class Qrel(BaseModel):
    source: QrelSourceType
    confidence: Literal["high", "medium", "low"] = "high"
```

If autocomplete via `QrelSource.PROBES` attribute access is wanted, keep the Enum but use `class QrelSource(StrEnum)` (PEP 663, py3.11+) instead of `(str, Enum)` — cleaner. Either way, commit to one mechanism, not both.

### P3-2: `RetrievalConfig.flags: dict[str, Any]` bypasses type checking

**File/line:** plan §Files §5 (`flags: dict[str, Any]`)

**Observation:** Per prior F001 review 71f48548-P1-2, dict-of-Any over a typed settings surface is where env-var-string-to-int bugs hide. For F051 the `flags` dict is eventually passed to `Settings.model_copy(update=flags)`, which DOES validate (pydantic enforces types on `model_copy`). So the bug class is caught — but the plan should call this out so future maintainers don't bypass `model_copy` and do `setattr(settings, k, v)` silently.

**Proposed fix (doc-only):** Add a docstring to `RetrievalConfig.flags`:

```python
flags: dict[str, Any] = field(default_factory=dict)
"""Settings overrides applied via Settings.model_copy(update=flags).

Types are validated at model_copy time (pydantic v2). Do NOT inject flags
via setattr — that silently skips validation and has bitten us before
(see decision 71f48548)."""
```

### P3-3: `subprocess.run` in tasks.py — specify kwargs

**File/line:** plan §Files §9

**Observation:** Plan sketches `subprocess.run` wrappers but doesn't specify kwargs. For consistency and to avoid Windows-specific foot-guns:

```python
subprocess.run(
    ["docker", "buildx", "build", ...],
    check=True,      # propagate non-zero
    text=True,       # Unicode stderr on Windows (avoids cp1252)
    # NO capture_output — user wants live Docker progress
    # NO shell=True — we pass an argv list; shell=True is a Windows injection vector
)
```

For push / ingest where stderr capture aids error messages:

```python
try:
    subprocess.run([...], check=True, text=True, capture_output=True)
except subprocess.CalledProcessError as e:
    logger.error("docker push failed:\n%s", e.stderr)
    raise
```

### P3-4: Specify `logger = logging.getLogger(__name__)` as the logging convention

**File/line:** plan §Observability

**Observation:** Every module in `nous/` uses the standard `logger = logging.getLogger(__name__)` pattern (101 files). Plan §Observability enumerates INFO / DEBUG messages but doesn't specify the logger. Make it explicit so subagents don't reach for `structlog` or a custom helper.

**Proposed fix:** Add one line to §Observability:

> All log lines use `logger = logging.getLogger(__name__)` matching the rest of `nous/`. No structured logging framework.

### P3-5: `[project.scripts]` — deferred, not needed in Phase 1

**Observation:** Nous's `main.py` is invoked via `python -m nous.main` with no `[project.scripts]` entry. Plan follows the same pattern. This is consistent and correct for Phase 1. If user experience later demands `nous-eval retrieval` as a first-class command, add `[project.scripts]` in F051.3 alongside the dashboard. No change needed.

---

## Things the plan got right (worth explicit credit)

- **PEP 604 union syntax (`Path | None`)** throughout — matches codebase (101 files use `from __future__ import annotations`). Reviewer flagged `Optional[...]` explicitly as a potential reversion risk; plan is clean.
- **`@dataclass(frozen=True)` for internal structs, `BaseModel` for JSONL-serialized data** — correct split. Frozen dataclass is the right choice for RetrievalConfig / QrelResult / RunResult (never deserialized from disk); BaseModel is correct for Qrel (disk → object with validation).
- **`asyncio.run(_run(args))` in CLI entrypoint** — standard pattern, matches `nous/main.py`. Works cleanly once P2-3 (disable background systems) is applied.
- **`field_validator(..., mode="before")` for empty-string-to-None coercion** — correct pydantic v2 usage; `mode="before"` runs pre-coercion so string literal `""` can be intercepted before Path() validation fails.
- **Tests under `tests/eval/` subdir** — precedented (`tests/heart/`, `tests/handlers/`). No conftest duplication needed; `tests/conftest.py` applies recursively.
- **`list[X]` not `Sequence[X]` in public APIs** — matches `nous/heart/heart.py` (95 uses of `list[...]`, zero `Sequence[...]`). Plan is consistent. If you want variance, use `list` in parameter types (concrete containers for input) and `list` in return types (concrete for output) — `Sequence` only when the function genuinely accepts `tuple` / arbitrary iterable, which none of F051's sketches do.

---

## Confirming the decision

**Decision ID:** 5675e0ed

**Recommendation:** APPROVE WITH REVISIONS. Land P1-1 and P1-2 before implementation kicks off (both are 10-minute edits to the plan document). The P2s are all plan-level sketch corrections that the implementing subagents should pick up in their first pass. P3s are polish and can be deferred to review feedback during implementation.
