# F052 Implementation Plan v1 — Multi-Embedding Seed for `_backfill_same_type`

**Spec:** `docs/features/F052-multi-embedding-backfill-seed.md` (v2 — approved by 3-agent review)
**Branch:** `feat/F052-multi-embedding-backfill`
**Total LOC:** ~660 (250 impl + 180 tests + 230 harness/judge)
**Estimated effort:** 2-3 day spike, parallel-implementable across 3 subagents

---

## Pre-emptive corrections to spec text

These spec wording details are corrected here so impl agents don't reproduce them:

| # | Spec says | Reality | Action |
|---|---|---|---|
| C1 | `Heart.expand_query_pairs(query, agent_id)` referenced once in §Eval-determinism | Spec §Implementation surface row 2 already drops `agent_id` per python-pro P1-1 fix. Method is `expand_query_pairs(self, query: str)`. | Plan uses correct signature throughout. |
| C2 | "F050 expansion temperature default 0.4" | Reading `query_expansion.py:280-292` — Anthropic SDK default actually applies (Haiku stock = 1.0, NOT 0.4 as v1 plan claimed). F050 doesn't currently set `temperature` at all. | Plan adds the field as `Field(default=1.0)` to match Anthropic's actual stock value (preserves current implicit behavior). density_eval still overrides to 0.0 for re-run determinism. |
| C3 | `nous_eval/_build_densifier_for_eval.py` — NEW file | Existing F051 module pattern uses helper functions inside `nous_eval/retrieval_runner.py`, not standalone files. Helper belongs adjacent to `_build_heart_for_eval`. | Plan adds `_build_densifier_for_eval` to `retrieval_runner.py`, not a new file. (Reduces file count, keeps the wiring-helpers cohesive.) |
| C4 | "GraphDensifier needs Heart reference at construction" | Verified via grep: GraphDensifier is constructed at `nous/main.py:300` and 14 test sites in `tests/test_graph_densifier.py`. Adding `heart: Heart | None = None` as 6th arg with default None keeps all existing constructions backward-compatible. | Plan uses `heart: Heart | None = None` keyword arg. Test sites unchanged unless they exercise F052. |
| C5 | Spec §Implementation surface row 4 says `query_expansion.py` change is "5 LOC" | Realistic LOC: ~3 lines (one `temperature` parameter to `messages.create`, value sourced from settings). | Cosmetic — note in plan. |

---

## File ownership map (parallel-safe partition)

Three subagents work on disjoint file sets. Sequence: A starts first; B + C dispatch in parallel after A's heart.py change is in.

### Subagent A: heart-config (sequential, ~80 LOC)
Owns:
- `nous/config.py` — 2 new `Field` declarations
- `nous/heart/query_expansion.py` — read `settings.query_expansion_temperature`, thread to `messages.create`
- `nous/heart/heart.py` — extract `expand_query_pairs` from `_recall:809-835`, refactor `_recall` to call it

**Why sequential first**: Subagent B (densifier) imports `Heart.expand_query_pairs` and Subagent C (tests) asserts on it. Method must exist before B/C can write final code.

### Subagent B: densifier-wedge (parallel after A, ~70 LOC + main.py wiring)
Owns:
- `nous/brain/graph_densifier.py` — `_backfill_same_type` wedge + new `_heart` member + constructor signature change
- `nous/main.py:300` — pass `heart=heart` to GraphDensifier construction
- (no other handlers touched — `_backfill_cross_type` explicitly Phase 1 OUT)

### Subagent C: harness + tests (parallel after A, ~510 LOC)
Owns:
- `nous_eval/density_eval.py` (NEW, ~110 LOC)
- `nous_eval/edge_judge.py` (NEW, ~70 LOC)
- `nous_eval/templates/edge_precision_prompt.md` (NEW, ~40 LOC)
- `nous_eval/retrieval_runner.py` — add `_build_densifier_for_eval(...)` helper (~30 LOC)
- `nous_eval/retrieval.py` — add `f052_on` `RetrievalConfig` (~10 LOC)
- `tests/test_f052_multi_embedding_seed.py` (NEW, ~180 LOC, 8 cases per spec §Test plan)

---

## Subagent A — detailed task list

### A.1: `nous/config.py`

Insert AFTER existing `query_expansion_cache_ttl_days` field (currently the last F050 field at ~line 565):

```python
# F050/F052 — Haiku temperature for query expansion. Default 1.0 matches
# Anthropic's stock value (preserves F050's implicit behavior — F050 didn't
# set temperature, so Haiku used its default). density_eval overrides to
# 0.0 for re-run determinism.
query_expansion_temperature: float = Field(
    default=1.0,
    description="F050/F052 — Haiku temperature for query expansion. "
                "Default 1.0 matches Anthropic stock; F052 density_eval "
                "overrides to 0.0 for re-run determinism.",
)

# F052 — Multi-embedding seed for _backfill_same_type. Default-off ship.
# When enabled, the densifier expands each orphan's content into N=K_50
# variants via Heart.expand_query_pairs and routes through hybrid_search_multi.
graph_backfill_multi_embedding_enabled: bool = Field(
    default=False,
    description="F052 master switch — multi-embedding seed for graph "
                "densification backfill (_backfill_same_type only in Phase 1).",
)
```

**Style match**: F050's existing fields use `Field(default=..., description=...)` (config.py:538-565). New fields follow that exact pattern. **Do NOT use `validation_alias`** — env_prefix="NOUS_" handles env mapping (decision 0f425a05).

### A.2: `nous/heart/query_expansion.py`

Find `_call_haiku` method (currently `:280-292`). Locate the `messages.create` call that lacks `temperature=`. Add:

```python
"temperature": self._settings.query_expansion_temperature,
```

(Pass via the `**kwargs` dict the call uses, or as direct kwarg — match existing call style.)

**Verification**: after change, the value flows from `Settings.query_expansion_temperature` → `QueryExpander._call_haiku` → Anthropic API. Test C.5 asserts.

### A.3: `nous/heart/heart.py`

Extract `expand_query_pairs` as a public method on `Heart`. Current inline block at `:809-835`:

```python
# F050: Optionally expand the query into variants via Haiku, then embed
# them in a single batch call. variant_pairs stays None on any failure
# so sub-managers fall back to the existing single-query path
# (byte-identical when variant_pairs is None — see plan v2 §invariant).
variant_pairs: list[tuple[str, list[float] | None]] | None = None
if (
    self.settings.query_expansion_enabled
    and self._query_expander is not None
    and self._embeddings is not None
):
    try:
        variants = await self._query_expander.expand(query, self.agent_id)
    except Exception as exc:
        logger.warning("F050: query_expander.expand raised, using [query]: %s", exc)
        variants = [query]

    if len(variants) > 1:
        try:
            embeddings = await self._embeddings.embed_batch(variants)
            variant_pairs = list(zip(variants, embeddings))
        except Exception as exc:
            logger.warning(...)
            variant_pairs = None
```

**Refactor in two steps:**

**Step A.3a — Add the new method** (after `_recall`, near other private helpers):

```python
async def expand_query_pairs(
    self, query: str
) -> list[tuple[str, list[float] | None]]:
    """F050+F052 — expand a query into (text, embedding) pairs for
    hybrid_search_multi.

    **Contract: NEVER raises, NEVER returns None or [].**

    On any failure path (expansion disabled, no expander, no embeddings,
    Haiku raises, embed_batch raises, gate trips), returns
    ``[(query, None)]`` — a single pair with no precomputed embedding.
    Callers route this through ``hybrid_search_multi(queries=...)``,
    which short-circuits at search.py:319-332 to byte-identical behavior
    vs single-query ``hybrid_search``.

    F052 callers (graph_densifier._backfill_same_type) MUST NOT
    catch ``asyncio.CancelledError`` from this call — task cancellation
    must propagate. The implementation only catches ``Exception``,
    which excludes ``CancelledError`` in Python 3.8+.

    Edge case: if cancelled mid-``embed_batch`` after Haiku already
    succeeded, the Haiku tokens are spent but the helper raises
    ``CancelledError`` upward — accepted cost per spec §Risks #6.
    """
    if not (
        self.settings.query_expansion_enabled
        and self._query_expander is not None
        and self._embeddings is not None
    ):
        return [(query, None)]

    try:
        variants = await self._query_expander.expand(query, self.agent_id)
    except Exception as exc:
        logger.warning(
            "F050/F052: query_expander.expand raised for %s, falling back to [query]: %s",
            self.agent_id, exc,
        )
        return [(query, None)]

    if len(variants) <= 1:
        return [(query, None)]

    try:
        embeddings = await self._embeddings.embed_batch(variants)
    except Exception as exc:
        logger.warning(
            "F050/F052: embed_batch failed for %d variants (%s), falling back to [query]: %s",
            len(variants), self.agent_id, exc,
        )
        return [(query, None)]

    return list(zip(variants, embeddings))
```

**Step A.3b — Refactor `_recall` to call the new method.** Replace `:809-835` block with:

```python
# F050: Expand query via Haiku + embed variants. Helper guarantees a
# non-empty list with single-pair fallback on any failure (NEVER None).
pairs = await self.expand_query_pairs(query)
# Translate single-pair-with-None-embedding back to "no expansion"
# so sub-managers route through their existing hybrid_search fast path.
variant_pairs: list[tuple[str, list[float] | None]] | None = (
    pairs if len(pairs) > 1 else None
)
```

**Behavior preservation check**: when expansion disabled, helper returns `[(query, None)]`, len=1 → variant_pairs becomes `None` → sub-managers skip the multi path. Byte-identical to today.

When expansion succeeds with N variants, helper returns N pairs, len>1 → variant_pairs = pairs → sub-managers route to hybrid_search_multi. Byte-identical to today.

---

## Subagent B — detailed task list

### B.1: `nous/brain/graph_densifier.py` constructor

Modify `GraphDensifier.__init__` (currently `:103-115`):

```python
# BEFORE
def __init__(
    self,
    db: Database,
    graph_linker: GraphLinker,
    embedder: EmbeddingProvider | None,
    settings: Settings,
    agent_id: str,
) -> None:
    ...
    self._db = db
    self._linker = graph_linker
    self._embedder = embedder
    self._settings = settings
    self._agent_id = agent_id
    ...
```

```python
# AFTER
def __init__(
    self,
    db: Database,
    graph_linker: GraphLinker,
    embedder: EmbeddingProvider | None,
    settings: Settings,
    agent_id: str,
    heart: "Heart | None" = None,  # F052 — for expand_query_pairs in same-type backfill
) -> None:
    ...
    self._db = db
    self._linker = graph_linker
    self._embedder = embedder
    self._settings = settings
    self._agent_id = agent_id
    self._heart = heart  # F052
    ...
```

Add forward-only TYPE_CHECKING import at top:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from nous.heart.heart import Heart
```

(Avoid circular import — heart imports brain.embeddings; densifier importing heart would cycle. TYPE_CHECKING-only guards it.)

### B.2: `nous/brain/graph_densifier.py` import addition

Add at top with existing import block:

```python
from nous.heart.search import hybrid_search, hybrid_search_multi  # F052
```

### B.3: `nous/brain/graph_densifier.py::_backfill_same_type` wedge

Locate `_backfill_same_type` at `:163-274`. Replace the `hybrid_search(...)` call at `:195-206`:

```python
# BEFORE
candidates = await hybrid_search(
    session=session,
    table=table,
    embedding=orphan_embedding,
    query_text=orphan_content[:500] if orphan_content else "",
    agent_id=self._agent_id,
    extra_where=f"AND t.id != :orphan_id",
    extra_params={"orphan_id": orphan_id},
    limit=10,
    vector_weight=0.6,
    active_filter=has_active,
)
```

```python
# AFTER
# F052: When enabled + Heart wired, expand orphan content into N (text, embedding)
# variants and route through hybrid_search_multi. Single-pair fallback (from helper
# OR when feature disabled) short-circuits at search.py:319-332 to byte-identical
# single-query hybrid_search behavior.
if (
    self._settings.graph_backfill_multi_embedding_enabled
    and self._heart is not None
    and orphan_content
):
    queries_pairs = await self._heart.expand_query_pairs(orphan_content[:500])
    # Helper returns [(content, None)] when expansion is unavailable —
    # in that case substitute the orphan's stored embedding so the
    # short-circuit path uses it (matches today's hybrid_search call).
    # Note: orphan_embedding may itself be None if the row had no embedding
    # at write time (graph_densifier.py:186-190). That's fine — the
    # short-circuit then routes to hybrid_search(embedding=None, ...)
    # which is the existing keyword-only fallback path; weight assignment
    # at :259-260 already handles the None-embedding branch via RRF score.
    if len(queries_pairs) == 1 and queries_pairs[0][1] is None:
        queries_pairs = [(orphan_content[:500], orphan_embedding)]
else:
    queries_pairs = [(orphan_content[:500] if orphan_content else "", orphan_embedding)]

candidates = await hybrid_search_multi(
    session=session,
    table=table,
    queries=queries_pairs,
    agent_id=self._agent_id,
    extra_where=f"AND t.id != :orphan_id",
    extra_params={"orphan_id": orphan_id},
    limit=10,
    vector_weight=0.6,
    active_filter=has_active,
)
```

**Invariants preserved** (from spec §Mechanism):
- CE rerank at `:212-232` unchanged — still keys on `orphan_content`.
- Cosine verification at `:244-256` unchanged — still uses `orphan_embedding`. Variants influence candidate-gen only.
- `weight = float(sim_row.similarity)` at `:257` is original-embedding cosine — preserved.
- When `multi_embedding_enabled=False`, queries_pairs has len 1 with the original embedding → search.py:319-332 fast-path → byte-identical.
- `_backfill_cross_type` at `:276+` UNTOUCHED.

### B.4: `nous/main.py` wiring

At `:300` — pass `heart` to GraphDensifier construction:

```python
# BEFORE
graph_densifier = GraphDensifier(
    db=database, graph_linker=graph_linker,
    embedder=embedding_provider, settings=settings,
    agent_id=settings.agent_id,
)
```

```python
# AFTER
graph_densifier = GraphDensifier(
    db=database, graph_linker=graph_linker,
    embedder=embedding_provider, settings=settings,
    agent_id=settings.agent_id,
    heart=heart,  # F052 — enables expand_query_pairs in _backfill_same_type
)
```

`heart` is already in scope at `main.py:300` (constructed earlier in the same function).

---

## Subagent C — detailed task list

### C.1: `nous_eval/retrieval_runner.py` — `_build_densifier_for_eval`

Add helper near existing `_build_heart_for_eval` (search the file for that name):

```python
async def _build_densifier_for_eval(
    settings: Settings,
    db: Database,
    agent_id: str,
    heart: Heart,
) -> "GraphDensifier":
    """F052 eval helper — construct GraphDensifier with Heart reference wired.

    Mirrors the wiring path at nous/main.py:300 so density_eval invokes the
    same densifier the production sleep handler does.
    """
    from nous.brain.graph_densifier import GraphDensifier
    from nous.brain.graph_linker import GraphLinker
    from nous.brain.embeddings import EmbeddingProvider

    embedder = EmbeddingProvider(settings) if settings.openai_api_key else None
    linker = GraphLinker(db=db, embedder=embedder, settings=settings, agent_id=agent_id)
    return GraphDensifier(
        db=db,
        graph_linker=linker,
        embedder=embedder,
        settings=settings,
        agent_id=agent_id,
        heart=heart,  # F052
    )
```

### C.2: `nous_eval/retrieval.py` — new RetrievalConfig

Add to `_DEFAULT_CONFIGS` dict (next to the other f050_* entries):

```python
"f052_on": RetrievalConfig(
    name="f052_on",
    flags={
        "graph_backfill_multi_embedding_enabled": True,
        "query_expansion_enabled": True,  # F052 depends on F050 expander
        "query_expansion_temperature": 0.0,  # density determinism — see C.3
    },
    description=(
        "F052 multi-embedding seed for _backfill_same_type. "
        "Eval-only retrieval-side measurement; density-side is in density_eval."
    ),
),
```

### C.3: `nous_eval/density_eval.py` (NEW)

Skeleton (~110 LOC):

```python
"""F052 density-eval harness mode.

Measures graph_edges + orphan-rate deltas across baseline vs f052_on configs
on the F051 eval DB, using a snapshot-reset-run-snapshot loop with
transactional restore on per-config failure.

Determinism: forces query_expansion_temperature=0.0 for the duration of the run.
Reproducibility: heart.query_expansions cache is preserved across runs.

Per-config behavior:
  1. Restore baseline (DELETE graph_edges WHERE agent_id = $eval_agent_id)
  2. Snapshot pre-state (orphan counts per type)
  3. Apply RuntimeConfig overrides
  4. Run GraphDensifier.run_backfill_cycle() + discover_clusters(max_bridges=20)
  5. Snapshot post-state
  6. On exception: restore from eval_baseline_edges_snapshot, log config FAIL
  7. Cleanup: pop RuntimeConfig overrides
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from nous_eval.config import EvalSettings
from nous_eval.retrieval import _DEFAULT_CONFIGS, RetrievalConfig
from nous_eval.retrieval_runner import (
    RuntimeConfig,
    _apply_config_flags,        # F051 Settings-overlay helper
    _settings_for_eval_db,      # redirect Settings DB connection to the eval DB
    _build_heart_for_eval,
    _build_densifier_for_eval,  # NEW in this PR — see C.1
)
from nous.config import Settings
from nous.storage.database import Database

logger = logging.getLogger(__name__)


@dataclass
class DensitySnapshot:
    edge_count_total: int
    edge_count_per_relation: dict[str, int]
    orphan_count_per_type: dict[str, int]


@dataclass
class DensityRunResult:
    config_name: str
    pre: DensitySnapshot
    post: DensitySnapshot | None
    edges_created: int
    ce_pruned: int
    union_size_p95: float | None
    haiku_calls: int
    openai_embed_calls: int
    wall_seconds: float
    failure: str | None = None  # set on exception


async def _ensure_zero_edge_baseline(db: Database, agent_id: str) -> None:
    """Pre-condition: zero edges for agent_id; create persistent snapshot for restore.

    NOTE: Uses a real (non-TEMP) table named ``brain.eval_baseline_edges_snapshot``
    so the snapshot survives across the multiple connection-pool sessions that
    a per-config run uses (TEMP tables are session-scoped in Postgres and would
    silently disappear when SQLAlchemy returns a different pooled connection
    on the next session). Table is created idempotently; TRUNCATE on each run.

    The snapshot is intentionally empty — it anchors a restore-to-zero-edges
    operation if a config crashes mid-cycle, so the next config starts clean.
    """
    async with db.session() as session:
        await session.execute(text(
            "DELETE FROM brain.graph_edges WHERE agent_id = :aid"
        ), {"aid": agent_id})
        await session.execute(text(
            "CREATE TABLE IF NOT EXISTS brain.eval_baseline_edges_snapshot "
            "(LIKE brain.graph_edges INCLUDING ALL)"
        ))
        await session.execute(text(
            "TRUNCATE brain.eval_baseline_edges_snapshot"
        ))  # intentionally empty — anchor for restore-on-crash
        await session.commit()


async def _snapshot(db: Database, agent_id: str) -> DensitySnapshot:
    # SELECT count(*) FROM brain.graph_edges WHERE agent_id = ... GROUP BY relation
    # SELECT type, count(*) FROM (... orphan finder ...) GROUP BY type
    ...


async def _restore_baseline(db: Database, agent_id: str) -> None:
    async with db.session() as session:
        await session.execute(text(
            "DELETE FROM brain.graph_edges WHERE agent_id = :aid"
        ), {"aid": agent_id})
        # Empty snapshot → no-op INSERT, restoring zero-edge state.
        await session.execute(text(
            "INSERT INTO brain.graph_edges SELECT * FROM brain.eval_baseline_edges_snapshot"
        ))
        await session.commit()


async def _run_one_config(
    config: RetrievalConfig,
    main_settings_template: Settings,
    eval_settings: EvalSettings,
    db: Database,
) -> DensityRunResult:
    # Pattern mirrors nous_eval/retrieval_runner.run_matrix:160-220 EXACTLY:
    # 1. RuntimeConfig.reset()  ← clear any leakage from a prior config
    # 2. overridden = _apply_config_flags(main_settings_template, config)  ← Settings overlay
    # 3. eval_scoped = _settings_for_eval_db(eval_settings, overridden)  ← redirect to eval DB
    # 4. _ensure_zero_edge_baseline(db, agent_id)
    # 5. snapshot pre
    # 6. async with _build_heart_for_eval(db, eval_scoped) as heart:
    #        densifier = await _build_densifier_for_eval(eval_scoped, db, agent_id, heart)
    #        try:
    #            result = await densifier.run_backfill_cycle()
    #            _ = await densifier.discover_clusters(max_bridges=20)
    #        except Exception as exc:
    #            await _restore_baseline(db, agent_id)
    #            return DensityRunResult(..., failure=str(exc))
    # 7. snapshot post
    # NOTE: RuntimeConfig.reset() at top of NEXT config call clears state — same as F051.
    ...


async def run_density_eval(
    config_names: list[str],
    settings: EvalSettings,
    n_runs: int = 1,
) -> list[DensityRunResult]:
    db = Database(settings.eval_db_url)
    await db.connect()
    try:
        results = []
        for run_idx in range(n_runs):
            for name in config_names:
                config = _DEFAULT_CONFIGS[name]
                results.append(await _run_one_config(config, settings, db))
        return results
    finally:
        await db.close()


def _write_report(results: list[DensityRunResult], path: Path) -> None:
    # markdown table per spec §Eval methodology output
    ...


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m nous_eval.density_eval")
    p.add_argument("--configs", default="baseline,f052_on")
    p.add_argument("--n-runs", type=int, default=1)
    args = p.parse_args(argv)

    config_names = args.configs.split(",")
    settings = EvalSettings()
    results = asyncio.run(run_density_eval(config_names, settings, args.n_runs))

    out = Path(settings.eval_report_dir) / f"density-eval-{datetime.now(tz=UTC):%Y%m%d-%H%M%S}.md"
    _write_report(results, out)
    logger.info("density_eval report written: %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### C.4: `nous_eval/edge_judge.py` (NEW, ~70 LOC)

Sonnet-judge wrapper:

```python
"""F052 — LLM-judge for edge precision in density_eval reports.

Loads prompt from nous_eval/templates/edge_precision_prompt.md, batches
edges, calls Sonnet via existing AnthropicClient (OAT-supporting per
F050 lessons — see decision ecf364d7 lessons), returns YES/WEAK/NO per edge.
"""

from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path

from nous.api.anthropic_client import create_client
from nous.config import Settings


@dataclass
class EdgeJudgment:
    source_id: str
    target_id: str
    relation: str
    verdict: str  # "YES" | "WEAK" | "NO"
    reasoning: str


async def judge_edges(
    edges: list[dict],  # [{source_content, target_content, relation, weight}, ...]
    settings: Settings,
    model: str = "claude-sonnet-4-6",
) -> list[EdgeJudgment]:
    client = create_client(settings)
    await client.start()
    try:
        prompt_template = (
            Path(__file__).parent / "templates" / "edge_precision_prompt.md"
        ).read_text(encoding="utf-8")
        # Batch ≤30 edges per Sonnet call
        ...
        # Parse JSON-array response
        ...
    finally:
        await client.close()
```

### C.5: `nous_eval/templates/edge_precision_prompt.md` (NEW, ~40 LOC)

Cached prompt template. Operator-editable. Schema:

```
You are evaluating graph edges for semantic correctness.
For each edge, decide: YES / WEAK / NO.

YES = the source and target are semantically related per the relation type.
WEAK = a tenuous or indirect link; would not be the first thing a reasoner reaches for.
NO = unrelated or wrong-direction.

Edges to evaluate (JSON array follows). Return JSON array of {source_id, target_id, verdict, reasoning} in same order.
```

### C.6: `tests/test_f052_multi_embedding_seed.py` (NEW, ~180 LOC, 8 cases)

Per spec §Test plan exactly:

```python
"""F052 tests — multi-embedding seed for _backfill_same_type.

8 mandatory cases per spec §Implementation surface row "tests".
"""

import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
# ... imports ...


# 1. Single-pair short-circuit byte-identity
async def test_disabled_path_byte_identical_to_hybrid_search(...):
    """When graph_backfill_multi_embedding_enabled=False, the wedge calls
    hybrid_search_multi(queries=[(orphan_content, orphan_embedding)]).
    The len==1 short-circuit at search.py:319-332 delegates to hybrid_search
    with byte-identical args. Test asserts:
      - hybrid_search_multi is called once with queries len==1
      - The candidate list returned matches a direct hybrid_search call with
        the same orphan_embedding/orphan_content (control comparison)."""
    ...

# 2. Multi-pair candidate-set strictly widens (or equals on full overlap)
async def test_multi_pair_widens_candidate_set(...):
    """With 3 distinct variant embeddings, union of candidates ≥ single-query result."""
    ...

# 3. Original-embedding still gates cosine
async def test_cosine_uses_original_orphan_embedding(...):
    """Even when candidate came from variant_2 search, the cosine check at
    graph_densifier.py:244-256 uses orphan_embedding, not variant embedding.
    Verify weight matches original-embedding cosine."""
    ...

# 4. Expander failure → helper single-pair fallback (helper itself NEVER raises)
async def test_expander_failure_helper_returns_single_pair(...):
    """Mock query_expander.expand (the INTERNAL Haiku call) to raise.
    Assert: Heart.expand_query_pairs returns [(query, None)] without raising
    (helper's contract). Then assert the densifier wedge sees this single-pair
    fallback and routes through the byte-identical short-circuit path —
    NEVER sees None, never has to check for it."""
    ...

# 5. All-variants-return-same-candidates: RRF doesn't double-count
async def test_rrf_no_double_count_on_identical_lists(...):
    """3 variants returning identical candidate lists → final ranks are
    correct (not inflated). Asserts via _rrf_merge_n contract."""
    ...

# 6. Empty union → return 0 cleanly
async def test_empty_union_returns_zero_edges(...):
    """All 3 variant searches return [] → candidates falsy →
    _backfill_same_type returns 0 without raising."""
    ...

# 7. CE truncation at 30-cap
async def test_union_above_ce_cap_truncated(...):
    """3 variants × 10 candidates = 30 unique → CE sees exactly 30 (cap).
    Verify behavior is well-defined (not a silent failure)."""
    ...

# 8. CancelledError propagates through wedge
async def test_cancelled_error_propagates_not_swallowed(...):
    """Mock query_expander.expand to raise asyncio.CancelledError.
    Helper's `except Exception` MUST NOT catch it (CancelledError is
    BaseException in 3.8+). Densifier wedge must let it propagate."""
    ...
```

---

## Migration plan: NONE

Verified: `heart.query_expansions` exists from F050 migration 038. F052 adds no schema. No new migration file needed.

---

## Test commands (post-impl)

```bash
# Subagent A scope
uv run pytest tests/test_f052_multi_embedding_seed.py::test_expander_failure -v
uv run python -c "from nous.config import Settings; s = Settings(); print(s.query_expansion_temperature, s.graph_backfill_multi_embedding_enabled)"

# Subagent B scope
uv run pytest tests/test_graph_densifier.py -v  # NO regressions on existing 14 tests
uv run pytest tests/test_f052_multi_embedding_seed.py -v  # All 8 new cases pass

# Subagent C scope (smoke — no real Haiku calls; uses scratch DB)
NOUS_GRAPH_BACKFILL_MULTI_EMBEDDING_ENABLED=true \
NOUS_QUERY_EXPANSION_ENABLED=true \
NOUS_QUERY_EXPANSION_TEMPERATURE=0.0 \
uv run python -m nous_eval.density_eval --configs baseline,f052_on --n-runs 1
```

---

## Definition of done (all must hold before PR)

1. All 8 new tests pass.
2. All 15 existing `test_graph_densifier.py` tests pass (no regression).
3. `uv run pytest tests/test_f050_*.py -v` passes (F050 callers unaffected by helper extraction).
4. `density_eval --configs baseline,f052_on` runs to completion on the F051 eval DB without crashing.
5. The eval report is human-readable and shows the gate-eligibility column correctly.
6. `edge_judge.py` produces YES/WEAK/NO output on a 30-edge sample (smoke test).
7. `git diff main...HEAD --stat` shows ONLY the files listed in this plan's §File ownership map. No collateral edits.
8. Manual scan of `_backfill_cross_type` confirms it is **completely unchanged** from main (Phase 1 invariant).

---

## Out of scope for this PR (deferred)

- Cross-type backfill multi-embedding seed → F052.4 (separate spec)
- Production shadow mode → F052.1
- Adaptive variant count per orphan → F052.2
- Variant-embedding caching → F052.3
- Shared budget bucket vs separate backfill bucket → F052.5 (if prod-flip phase shows starvation)
