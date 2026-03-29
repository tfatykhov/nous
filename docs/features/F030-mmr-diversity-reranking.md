# F030: MMR Diversity Re-Ranking

**Status:** Draft  
**Proposed by:** Nous (via xMemory multi-agent debate)  
**Date:** 2026-03-28  
**Research basis:** xMemory (arXiv:2602.02007v2, ICML 2026) — submodular diverse retrieval  
**Depends on:** F025 (RRF hybrid search — deployed)  
**Blocks:** None (additive, feature-flagged)

---

## Problem Statement

Nous's `recall_deep` returns results ranked by score, but **does not penalize redundancy**. When Tim asks a broad question like "what memory decisions have we made?", the top-k results cluster around the most-discussed topic (e.g., F022 graph recall) because their embeddings are tightly packed in vector space. Less-discussed but equally relevant results (F023 admission, F027 forgetting, Membrain integration) get pushed below the limit cutoff.

This is the **redundancy collapse** problem identified by xMemory: naive top-k maximizes individual relevance but not information coverage.

### Concrete failure example

Query: "what are our outstanding architecture items?"

Current top-5 (simulated from F025 test data):
1. F022 graph recall spec (score: 0.866)
2. F022 Phase 4 spreading activation (score: 0.850)
3. F022 contradiction detection (score: 0.835)
4. F025 RRF implementation (score: 0.820)
5. F025 weight sweep results (score: 0.807)

Three F022 results and two F025 results. Missing entirely: F023 admission control, F027 forgetting, F028 context paging, F029 trajectory learning, Membrain integration. Tim gets depth on two features instead of breadth across eight.

### Why now

- RRF is deployed and working (12/13 top-1 accuracy) — the scoring foundation is solid
- MMR operates as a **post-processing layer** on already-scored results — zero risk to scoring quality
- Multi-agent debate consensus: highest impact/effort ratio of all xMemory ideas
- Estimated ~40-60 LOC, no schema changes, no new dependencies

---

## Solution: Maximal Marginal Relevance (MMR)

MMR (Carbonell & Goldstein, 1998) selects results greedily, at each step choosing the item that maximizes:

```
MMR(d) = λ · sim(d, query) − (1 − λ) · max(sim(d, d_selected) for d_selected in already_selected)
```

Where:
- `λ` controls the relevance vs. diversity tradeoff (1.0 = pure relevance, 0.0 = pure diversity)
- `sim` is cosine similarity between embeddings
- The penalty term grows as selected items cover more of the embedding space

This is a **greedy approximation to submodular maximization** — the same family of algorithms xMemory uses, but simpler and well-proven in IR.

---

## Architecture Context

### Where MMR hooks in

```
recall_deep (api/tools.py)
  → heart.recall() (heart/heart.py:672)
      → [episodes.search, facts.search, procedures.search, censors.search]
      → merge results by score (line 759)    ← CURRENT: naive sort
      → return merged[:limit]

      → mmr_rerank(merged, embeddings, λ)   ← NEW: diversity selection
      → return mmr_selected[:limit]
```

MMR replaces the `merged.sort() + [:limit]` at line 759 of `heart.py`. Everything upstream (hybrid_search, RRF, per-type searches) stays untouched.

### The embedding challenge

Current `RecallResult` schema does NOT carry embeddings — only `id`, `summary`, `score`, `metadata`. MMR needs pairwise cosine similarity between candidates, which requires their embeddings.

**Solution: batch-fetch embeddings for merged candidates.**

After the merge step produces ~`limit * 2` candidates (across all types), fetch their embeddings in a single query per source table. This is 2-4 small queries fetching only the `embedding` column for known IDs — not a full scan.

---

## Implementation Plan

### Phase 1: Core MMR function (pure Python, no DB changes)

**File:** `heart/search.py` (add to existing shared search utilities)

```python
import numpy as np
from numpy.typing import NDArray

def mmr_rerank(
    candidates: list[RecallResult],
    embeddings: dict[UUID, NDArray],
    query_embedding: NDArray,
    lambda_: float = 0.7,
    limit: int = 10,
) -> list[RecallResult]:
    """Maximal Marginal Relevance re-ranking for diversity.
    
    Greedily selects items maximizing:
      MMR(d) = λ · cos_sim(d, query) − (1−λ) · max(cos_sim(d, selected))
    
    Items without embeddings fall back to score-only ranking (appended after
    MMR-selected items in original score order).
    
    Args:
        candidates: Pre-scored results from hybrid search merge.
        embeddings: Map of result ID → embedding vector.
        query_embedding: The query's embedding vector.
        lambda_: Relevance vs diversity weight (0.0–1.0). Default 0.7.
        limit: Number of results to return.
    
    Returns:
        Re-ranked list of RecallResult, length ≤ limit.
    """
    # Separate candidates with/without embeddings
    with_emb = [(c, embeddings[c.id]) for c in candidates if c.id in embeddings]
    without_emb = [c for c in candidates if c.id not in embeddings]
    
    if len(with_emb) <= 1:
        # Not enough candidates for diversity — fall back to score sort
        return (candidates[:limit])
    
    # Precompute query similarities
    query_sims = {}
    for c, emb in with_emb:
        query_sims[c.id] = float(np.dot(query_embedding, emb) / (
            np.linalg.norm(query_embedding) * np.linalg.norm(emb) + 1e-10
        ))
    
    selected: list[RecallResult] = []
    selected_embs: list[NDArray] = []
    remaining = list(with_emb)
    
    while len(selected) < limit and remaining:
        best_score = -float('inf')
        best_idx = 0
        
        for i, (c, emb) in enumerate(remaining):
            relevance = query_sims[c.id]
            
            if selected_embs:
                # Max similarity to any already-selected item
                max_sim = max(
                    float(np.dot(emb, s_emb) / (
                        np.linalg.norm(emb) * np.linalg.norm(s_emb) + 1e-10
                    ))
                    for s_emb in selected_embs
                )
            else:
                max_sim = 0.0
            
            mmr = lambda_ * relevance - (1 - lambda_) * max_sim
            
            if mmr > best_score:
                best_score = mmr
                best_idx = i
        
        winner, winner_emb = remaining.pop(best_idx)
        selected.append(winner)
        selected_embs.append(winner_emb)
    
    # Append non-embedded items if space remains
    for c in without_emb:
        if len(selected) >= limit:
            break
        selected.append(c)
    
    return selected
```

**Complexity:** O(k² · d) where k = number of candidates, d = embedding dimension. For k=20, d=1536, this is ~0.5ms — negligible.

### Phase 2: Embedding batch-fetch utility

**File:** `heart/heart.py` (add method)

```python
async def _fetch_embeddings(
    self,
    results: list[RecallResult],
    session: AsyncSession,
) -> tuple[dict[UUID, NDArray], NDArray | None]:
    """Batch-fetch embeddings for recall results and the query.
    
    Returns:
        (embeddings_by_id, query_embedding)
    """
    from nous.storage.models import Fact, Episode, Procedure, Censor
    
    # Group IDs by type
    type_ids: dict[str, list[UUID]] = {}
    for r in results:
        type_ids.setdefault(r.type, []).append(r.id)
    
    type_to_model = {
        "fact": Fact,
        "episode": Episode,
        "procedure": Procedure,
        "censor": Censor,
    }
    
    embeddings: dict[UUID, NDArray] = {}
    for mem_type, ids in type_ids.items():
        model = type_to_model.get(mem_type)
        if not model:
            continue
        result = await session.execute(
            select(model.id, model.embedding)
            .where(model.id.in_(ids))
            .where(model.embedding.isnot(None))
        )
        for row in result.all():
            embeddings[row.id] = np.array(row.embedding)
    
    return embeddings
```

### Phase 3: Wire into `_recall`

**File:** `heart/heart.py` — modify `_recall` method

```python
async def _recall(self, query, limit, types, session):
    # ... existing search code unchanged ...
    
    # Current: merged.sort(key=lambda r: r.score, reverse=True)
    # Current: return merged[:limit]
    
    # NEW: MMR diversity re-ranking (F030)
    if self._mmr_enabled and len(merged) > 1:
        embeddings = await self._fetch_embeddings(merged, session)
        query_embedding = await self._get_query_embedding(query)
        
        merged = mmr_rerank(
            candidates=merged,
            embeddings=embeddings,
            query_embedding=query_embedding,
            lambda_=self._mmr_lambda,
            limit=limit,
        )
    else:
        merged.sort(key=lambda r: r.score, reverse=True)
        merged = merged[:limit]
    
    return merged
```

**Note:** The query embedding is already computed upstream for each `hybrid_search()` call but isn't passed back. Two options:
- **Option A (simpler):** Re-embed the query in `_recall`. One extra embedding API call per recall (~$0.0001, ~50ms). Acceptable for now.
- **Option B (cleaner):** Thread the query embedding through from the first `hybrid_search` call and cache it. More refactoring but eliminates the redundant call.

**Recommendation:** Start with Option A, optimize to B if profiling shows latency concerns.

### Phase 4: Config + feature flag

**File:** `config.py`

```python
# F030: MMR Diversity Re-Ranking
mmr_enabled: bool = Field(default=False, description="Enable MMR diversity re-ranking in recall")
mmr_lambda: float = Field(default=0.7, ge=0.0, le=1.0, description="MMR relevance vs diversity weight")
```

**Environment variables:**
- `NOUS_MMR_ENABLED=true` — enable MMR (default: false)
- `NOUS_MMR_LAMBDA=0.7` — relevance/diversity tradeoff (default: 0.7)

---

## Lambda Tuning Guide

| λ value | Behavior | Use case |
|---------|----------|----------|
| 1.0 | Pure relevance (equivalent to current naive top-k) | Narrow, specific queries |
| 0.7 | **Recommended default** — mild diversity pressure | General recall |
| 0.5 | Balanced — significant diversity enforcement | Broad exploratory queries |
| 0.3 | Diversity-dominant — may sacrifice some relevance | "What do I know about X?" |

The λ parameter should be tuned against the F025 test query set. Specifically:
- **Category A (exact fact lookup):** λ should be high (relevance matters, diversity is noise)
- **Category E (broad conceptual):** λ should be lower (diversity is the point)

**Future:** λ could be query-adaptive. Short specific queries → λ=0.85. Long exploratory queries → λ=0.6. This maps to xMemory's uncertainty-gated expansion idea.

---

## Testing Plan

### Unit tests

1. **MMR with identical embeddings** — should still return items (no infinite loop)
2. **MMR with orthogonal embeddings** — should return in original score order (no penalty needed)
3. **MMR with clustered embeddings** — should select one from each cluster
4. **MMR with empty embeddings dict** — should fall back to score sort
5. **Lambda=1.0** — should produce identical ranking to naive top-k
6. **Lambda=0.0** — should maximize pairwise distance

### Integration tests (against F025 test set)

Run the F025 50-query benchmark with MMR on vs off:

**Metrics to capture:**
- **Top-1 accuracy** — must not regress from 12/13 baseline
- **Result set diversity** — mean pairwise cosine similarity of top-10 (target: < 0.65, current estimated ~0.78)
- **Coverage** — number of distinct memory types in top-10
- **Type spread** — Shannon entropy of type distribution in results
- **Latency** — p50/p95 per-query (target: < 100ms overhead)

### A/B shadow mode

Before enabling MMR by default:
1. Log both naive-sorted and MMR-reranked results for each query
2. Compare which set the LLM actually uses (by tracking fact IDs in generated responses)
3. Measure whether MMR results lead to more comprehensive answers

---

## Latency Budget

| Step | Estimated time | Notes |
|------|---------------|-------|
| Batch embed fetch (2-4 queries) | ~15ms | Index scan on primary key, embedding column only |
| Query re-embedding (Option A) | ~50ms | OpenAI API call; eliminable with Option B |
| MMR computation (k=20, d=1536) | ~0.5ms | Pure NumPy, no I/O |
| **Total overhead** | **~65ms** | **Acceptable for conversational latency** |

Current `recall_deep` p50 is ~200-400ms. Adding ~65ms (16-33% increase) is within budget.

---

## Rollout Plan

| Phase | Scope | Timeline | Risk |
|-------|-------|----------|------|
| 0 | Add `mmr_rerank()` to search.py with unit tests | Day 1 | None — dead code until wired |
| 1 | Wire into `_recall` behind `mmr_enabled=false` flag | Day 2 | None — flag off by default |
| 2 | Shadow mode: log both rankings, compare | Days 3-5 | Read-only logging |
| 3 | Enable for Tim, tune λ with F025 benchmark | Day 6-7 | Feature-flagged, instant rollback |
| 4 | Default on, remove flag | Week 3+ | Only after benchmark validation |

---

## What This Does NOT Do (Scope Boundaries)

- **Does NOT change hybrid_search()** — MMR operates on merged cross-type results, not per-type search
- **Does NOT change RRF** — RRF scores are inputs to MMR, not replaced by it
- **Does NOT add a theme layer** — that's F027 sleep consolidation territory
- **Does NOT change Brain (decision) retrieval** — Brain has its own query pipeline; MMR applies only to Heart's merged results. Brain integration is a future enhancement.
- **Does NOT implement adaptive λ** — fixed λ first, query-adaptive λ is a follow-up
- **Does NOT require numpy as a new dependency** — numpy is already in requirements (pgvector/embedding operations)

---

## Success Criteria

1. **Top-1 accuracy ≥ 12/13** on F025 test set (no regression)
2. **Mean pairwise similarity of top-10 drops by ≥ 15%** (from ~0.78 to ≤ 0.66)
3. **≥ 3 distinct memory types** in top-10 for broad queries (currently often 1-2)
4. **Latency overhead ≤ 100ms** at p95
5. **Tim subjectively reports better recall breadth** in normal usage

---

## Relationship to Other Features

- **F022 (Graph Recall):** MMR and graph expansion are complementary. Graph expansion adds new candidates; MMR ensures the expanded set doesn't just add more of the same cluster. Future: graph-expanded results could get a diversity bonus.
- **F023 (A-MAC Admission):** Better admission means higher-quality candidates for MMR to select from. No interaction.
- **F025 (Retrieval Optimization):** F025's benchmark set is the validation tool for F030. RRF scores are MMR's input signal.
- **F027 (Sleep Consolidation / Forgetting):** Theme clustering in F027 could eventually inform MMR — items from different themes get a diversity bonus. Future enhancement.
- **Membrain:** Membrain's neuromorphic associative recall is inherently diversity-aware (attractor dynamics). When Membrain ships, it may subsume MMR for its recall path. MMR remains valuable for the PostgreSQL path.

---

## Open Questions

1. **Should Brain decisions also get MMR?** Currently `brain.query()` has its own pipeline. Unifying under a single MMR pass would require merging Heart + Brain results before re-ranking. Deferred — test Heart-only first.
2. **Should λ vary by query length?** Short queries ("Tim's timezone") are precise — high λ. Long queries ("what memory architecture decisions have we made and what's still outstanding?") benefit from low λ. This is the xMemory uncertainty-gating idea in simpler form.
3. **Embedding cache?** If the same facts appear in repeated recalls, caching their embeddings in-process could skip the batch fetch. Worth considering if latency overhead exceeds budget.
4. **NumPy dependency scope** — confirm numpy is available in the production image. If not, implement cosine similarity with pure Python (trivial for k=20).
