# F043: Cross-Encoder Reranking for Sleep-Cycle Graph Backfill

**Status:** Shipped
**Proposed by:** Tim + Nous
**Date:** 2026-04-14
**Depends on:** F040 (Graph Densification — deployed), F042 (Cross-Encoder Reranking — deployed PR #312/#313)
**Blocks:** None (additive, feature-flagged)
**Supplements:** F040 (precision improvement on auto-linked edges)

---

## Problem Statement

F040 Graph Densification runs during sleep and backfills graph edges for orphan memories (facts, decisions, episodes, procedures) using a two-stage candidate pipeline:

1. **Candidate gathering** — hybrid search (RRF vector + keyword) for same-type linking, or vector + keyword merged for cross-type linking
2. **Threshold gate** — per-relation cosine similarity threshold verification before `create_edge()`

Stage 1 uses a bi-encoder (the stored embedding) — it returns many candidates that are topically near the orphan but may not be the *best* matches. Stage 2 is a pointwise cosine check that's cheap but coarse-grained: if the stored embedding happens to be cosine-close, the edge is created regardless of whether the two texts actually share semantic content the way a cross-encoder would see.

The result: graph edges get polluted with "close but not quite" links that erode spreading activation precision and make the graph noisier than it should be.

### Current Flow (F040)

```
orphan fact "Tim moved to Silver Spring in March"
  │
  ▼
hybrid_search(vector+keyword)  →  10 candidates by RRF rank
  │
  ▼
for each candidate:
    cosine(orphan_emb, cand_emb) ≥ threshold (0.82) ?
      yes → create_edge()
      no  → skip
```

Problem: a candidate fact "Maryland is a state near DC" has high cosine with "Tim moved to Silver Spring" because they share DC/Maryland context, so the edge gets created — but the two facts aren't really about the same thing.

### Proposed Flow (F043)

```
orphan fact "Tim moved to Silver Spring in March"
  │
  ▼
hybrid_search(vector+keyword)  →  10 candidates by RRF rank
  │
  ▼
cross_encoder_rerank(orphan, candidates, text_fn)  [NEW]
  │
  ▼
keep top-K with CE score ≥ min_score floor  [NEW]
  │
  ▼
for each surviving candidate:
    cosine(orphan_emb, cand_emb) ≥ threshold (0.82) ?
      yes → create_edge()
      no  → skip
```

The cosine gate stays — it's a correctness floor. CE is inserted *before* the cosine gate so it acts as a **precision pre-filter**, pruning candidates that look topically close but are semantically unrelated. Candidates the cross-encoder would score `<0.3` are dropped without spending a cosine query.

### Why Sleep Is a Better Fit Than Recall

F042 placed CE reranking in `recall_deep` where latency is visible to the user (~30-50ms per query). That's acceptable but not ideal. Sleep is genuinely async — it runs on idle timers, has no user waiting, and processes candidates in batches. The CE cost is **free** relative to the value it adds.

A secondary win: sleep is where we could eventually **fine-tune** the cross-encoder on actual link outcomes (which edges got traversed, which got contradicted later), feeding back into F042's Phase 2 roadmap. F043 is the prerequisite for that because it's where we accumulate the training signal.

---

## Solution

### Scope: MVP — Backfill Only

F043 MVP inserts CE reranking into both backfill code paths in `nous/brain/graph_densifier.py`:

- `_backfill_same_type()` — between `hybrid_search()` and the cosine-verification loop
- `_backfill_cross_type()` — after the content fetch and before the re-embed/cosine loop

**Out of MVP scope:**
- Contradiction-detection candidate filtering (see Phase 2 below — different semantics, different risks)
- Fact-at-ingest dedup (no current rerank point; separate effort)
- Fine-tuning / knowledge distillation (F042 Phase 2-3 territory)

### Part 1: Candidate Adapter

The existing F042 reranker `nous/heart/reranker.cross_encoder_rerank()` expects candidates with a mutable `.score` attribute and an optional `text_fn` to extract the reranking text. F040's `hybrid_search()` returns `list[tuple[UUID, float]]` — no `.score` attribute, no text.

**New module:** `nous/brain/backfill_rerank.py`

```python
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.heart.reranker import CROSS_ENCODER_AVAILABLE, cross_encoder_rerank

logger = logging.getLogger(__name__)


@dataclass
class RerankCandidate:
    id: UUID
    content: str
    score: float  # mutable; reranker writes sigmoid CE score here


async def fetch_candidate_content(
    session: AsyncSession,
    table: str,
    content_col: str,
    candidate_ids: Sequence[UUID],
) -> dict[UUID, str]:
    """Batch-fetch text for reranking.

    Returns dict[id → content]. Missing / empty rows are omitted.
    """
    if not candidate_ids:
        return {}
    placeholders = ", ".join(f":id_{i}" for i in range(len(candidate_ids)))
    params = {f"id_{i}": cid for i, cid in enumerate(candidate_ids)}
    sql = text(
        f"SELECT t.id, {content_col} AS content "
        f"FROM {table} t WHERE t.id IN ({placeholders})"
    )
    rows = await session.execute(sql, params)
    return {row.id: row.content for row in rows if row.content}


async def ce_rerank_backfill_candidates(
    query_text: str,
    candidate_rows: Sequence[tuple[UUID, float]],
    content_map: dict[UUID, str],
    *,
    settings,
    log_context: str = "",
) -> list[tuple[UUID, float]]:
    """Rerank F040 backfill candidates via cross-encoder.

    Returns the surviving candidates in CE-ranked order. Each survivor's
    RRF score is REPLACED with the sigmoid CE score. If CE is unavailable
    or disabled, returns ``candidate_rows`` unchanged.

    A candidate is dropped if:
      - its content is missing/empty
      - its sigmoid CE score < ``settings.ce_backfill_min_score``
      - it falls outside the top ``settings.ce_backfill_top_k``
    """
    if (
        not settings.ce_backfill_enabled
        or not CROSS_ENCODER_AVAILABLE
        or not candidate_rows
        or not query_text
    ):
        return list(candidate_rows)

    # Build RerankCandidate wrappers; drop rows with no content up front.
    wrapped: list[RerankCandidate] = []
    for cand_id, rrf in candidate_rows:
        content = content_map.get(cand_id, "")
        if not content:
            continue
        wrapped.append(RerankCandidate(id=cand_id, content=content, score=float(rrf)))

    if not wrapped:
        return []

    reranked = await cross_encoder_rerank(
        query=query_text,
        candidates=wrapped,
        text_fn=lambda c: c.content,
        model_name=settings.cross_encoder_model,
        max_candidates=settings.ce_backfill_top_k * 2,  # give CE headroom
        text_limit=settings.cross_encoder_text_limit,
    )

    kept: list[tuple[UUID, float]] = []
    for c in reranked[: settings.ce_backfill_top_k]:
        if c.score < settings.ce_backfill_min_score:
            break  # list is already sigmoid-DESC; nothing below will pass
        kept.append((c.id, c.score))

    pruned = len(wrapped) - len(kept)
    if pruned > 0:
        logger.debug(
            "CE backfill rerank: kept=%d pruned=%d ctx=%s",
            len(kept), pruned, log_context,
        )
    return kept
```

### Part 2: Integration in `_backfill_same_type`

After `hybrid_search()` returns candidates (graph_densifier.py:185):

```python
candidates = await hybrid_search(...)  # existing
if not candidates:
    return 0

# F043: CE rerank between hybrid search and cosine verification
if self._settings.ce_backfill_enabled:
    content_map = await fetch_candidate_content(
        session, table, content_col,
        [c[0] for c in candidates],
    )
    candidates = await ce_rerank_backfill_candidates(
        query_text=orphan_content,
        candidate_rows=candidates,
        content_map=content_map,
        settings=self._settings,
        log_context=f"{entity_type}-same:{orphan_id}",
    )
    if not candidates:
        return 0
```

The downstream cosine-verification loop is unchanged. The cosine gate still has the final word — CE is purely a pruning step.

### Part 3: Integration in `_backfill_cross_type`

The cross-type path already fetches candidate content in `candidate_content: dict[UUID, str]` at line 309-311. CE rerank is inserted there:

```python
candidate_content = {
    row.id: row.content for row in result if row.content
}

# F043: CE rerank cross-type candidates before re-embed loop
if self._settings.ce_backfill_enabled and candidate_content:
    ranked = await ce_rerank_backfill_candidates(
        query_text=orphan_content,
        candidate_rows=[(cid, 0.0) for cid in candidate_content.keys()],
        content_map=candidate_content,
        settings=self._settings,
        log_context=f"{source_type}->{target_type}:{orphan_id}",
    )
    surviving = {cid for cid, _ in ranked}
    candidate_content = {
        cid: txt for cid, txt in candidate_content.items() if cid in surviving
    }
```

### Part 4: Configuration

New fields in `nous/config.py`:

```python
# F043: CE reranking for sleep-cycle backfill
ce_backfill_enabled: bool = False
ce_backfill_top_k: int = 10
ce_backfill_min_score: float = 0.30  # sigmoid-normalized CE score floor
```

Env vars: `NOUS_CE_BACKFILL_ENABLED`, `NOUS_CE_BACKFILL_TOP_K`, `NOUS_CE_BACKFILL_MIN_SCORE`.

Reuses F042's `cross_encoder_model` and `cross_encoder_text_limit` — no new model config.

### Part 5: Telemetry

Track CE pruning effectiveness in `sleep_stats` and the sleep handler log line:

- `run_backfill_cycle()` accumulates `ce_kept` / `ce_pruned` counters via a `GraphDensifier._ce_stats` dict, returned alongside the existing per-type counts.
- `_phase_graph_densification()` in `sleep_handler.py` adds these to `sleep_stats` and logs:

  ```
  F040 graph densification: 12 backfill edges (CE kept=45 pruned=18), 3 bridge edges
  ```

No new dashboard tab in MVP — the existing `/dashboard/density` already shows edge counts; CE stats flow through sleep_stats for log-based analysis first.

### Part 6: Graceful Degradation

- `CROSS_ENCODER_AVAILABLE=False` (sentence-transformers absent) → adapter returns input unchanged
- `ce_backfill_enabled=False` (default) → adapter short-circuits before any DB work
- `content_map` empty (e.g. all candidates had NULL content) → return empty list, log at DEBUG
- CE load/predict exception → handled by the F042 reranker (returns candidates unchanged)
- Downstream cosine gate unchanged → even if CE misbehaves, no false-positive edges leak through

---

## Pipeline Position Rationale

CE goes **before** cosine verification because:
1. CE on 10 candidates is faster than 10 serial cosine SQL queries — pruning first saves DB round-trips
2. The cosine gate is a *correctness floor*, not a ranker. It should operate on a high-precision candidate set, not a high-recall one.
3. CE can catch cases where stored embeddings happen to be close for the wrong reason (shared context words, topical drift) — cosine can't see through that because it's pointwise on the bi-encoder.

CE does **not** replace cosine because:
1. Cross-encoders are trained for query-passage relevance, not exact-text identity. The cosine threshold is the hard guarantee that two nodes are "about the same thing" at the embedding level.
2. If CE is ever wrong in a systemic way, the cosine floor is the backstop.

---

## Future Phases (Not in MVP)

### Phase 2: Contradiction-Detection Candidate Filtering

`FactManager._find_contradiction_candidates()` currently does a SQL self-join with same `LOWER(subject)` + cosine-distance in [0.75, 0.95). Every candidate pair then goes through the expensive LLM resolver.

CE could pre-filter the candidate pairs by scoring `(fact1.content, fact2.content)` and keeping only the top-N — saving LLM tokens on obvious non-matches.

**Why not MVP:** the semantics are different (CE scores query-doc relevance, not contradiction entailment). If CE ranks a true-contradiction pair low, we lose a true positive and never fire the LLM. Requires careful validation against a labeled pair set we don't have yet.

### Phase 3: Contradiction Subject Expansion

Beyond re-ranking, CE could let us *drop* the same-subject constraint in `_find_contradiction_candidates` and use hybrid search + CE to find contradictory pairs that use different subject wording ("Tim lives in Maryland" vs "user is in Silver Spring"). Bigger recall win, bigger risk.

### Phase 4: Link-Outcome Fine-Tuning

Use which edges got traversed (spreading activation) and which got contradicted as positive/negative training pairs. Fine-tune the cross-encoder on Nous-specific link relevance. This is the F042 Phase 2 roadmap, now with a data source.

---

## Risk Assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| CE prunes too aggressively → fewer edges than F040 alone | MEDIUM | Feature flag default OFF; `ce_backfill_min_score` tunable; log kept/pruned counters for monitoring |
| CE disagrees with cosine on obvious matches | LOW | Cosine gate is the floor — CE only reorders/prunes, can't bypass threshold |
| First-query model load during sleep phase (~2-3s) | LOW | Sleep already tolerates multi-second phases; `huggingface_cache` volume persists model across restarts (F042-docker) |
| Model-score scale coupling between F042 recall and F043 backfill | LOW | Both use the same sigmoid-normalized [0,1] range; `ce_backfill_min_score` is independent of F042's consumers |
| DB round-trip cost for content fetch (`fetch_candidate_content`) | LOW | Single query per orphan, batched via `IN (...)` placeholders; N=10 candidates |
| Sleep phase test count regression | MEDIUM | No new phase added — F043 is inside existing `graph_densification` phase; phase count is unchanged |

---

## Metrics & Evaluation

Log during the backfill phase:

- **`ce_kept`** — candidates that survived CE pruning, per sleep cycle
- **`ce_pruned`** — candidates dropped by CE, per sleep cycle
- **`edges_created`** — unchanged existing metric, now expected to drop slightly when CE is enabled (that's the precision improvement)
- **`ce_prune_rate`** — `ce_pruned / (ce_kept + ce_pruned)` — target 20-50% based on F042 observation that cross-encoders typically prune ~30% of bi-encoder candidates

Watch for regressions:
- If `edges_created` drops to zero, CE min_score is too high
- If `ce_prune_rate` is zero, CE isn't disagreeing with hybrid search (consider lowering top_k or raising min_score)

---

## Implementation Estimate

- `nous/brain/backfill_rerank.py` (new): ~120 LOC
- `nous/brain/graph_densifier.py` (modified): ~40 LOC
- `nous/config.py`: 3 fields
- `nous/handlers/sleep_handler.py`: ~5 LOC (stats propagation)
- `tests/test_backfill_rerank.py` (new): ~180 LOC unit
- `tests/test_graph_densifier.py` (extended): ~60 LOC integration (monkeypatched reranker)
- Docs: CLAUDE.md env vars + shipped row, INDEX.md, spec status

**Total: ~405 LOC + dependency reuse from F042.**

---

## Dependencies

**No new deps.** Reuses:
- `nous/heart/reranker.py` (F042, shipped)
- `sentence-transformers>=3.0.0` (F042 optional dep, already in `[rerank]` extra and Dockerfile as of PR #313)
- `huggingface_cache` Docker volume (F042 docker PR #313)

The `[rerank]` extra is already installed in the Nous container. F043 requires zero deploy changes beyond flipping `NOUS_CE_BACKFILL_ENABLED=true`.
