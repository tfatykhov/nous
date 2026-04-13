# F042: Cross-Encoder Reranking Stage

**Status:** Draft  
**Proposed by:** Tim + Nous  
**Date:** 2026-04-13  
**Research basis:** [Advanced RAG Retrieval: Cross-Encoders & Reranking](https://towardsdatascience.com/advanced-rag-retrieval-cross-encoders-reranking/) — practical guide on two-stage retrieval with cross-encoder reranking, fine-tuning, and knowledge distillation  
**Depends on:** F025 (RRF hybrid search — deployed), F030 (MMR diversity — deployed)  
**Blocks:** None (additive, feature-flagged)  
**Supplements:** F037 (Utility-Boosted Retrieval), F030 (MMR Diversity)

---

## Problem Statement

Nous's current retrieval pipeline uses bi-encoder embeddings (Voyage) for vector search and BM25 for keyword search, merged via Reciprocal Rank Fusion (F025). This is a **single-stage retrieval** architecture.

Bi-encoders encode query and document **independently** — they never see each other during encoding. This is fast (precompute all doc embeddings once) but fundamentally limited: the model cannot capture fine-grained interactions between query tokens and document tokens. Two memories that use different vocabulary to express the same concept may score poorly despite being highly relevant.

Cross-encoders solve this by encoding the **query-document pair together** through all transformer layers. The model sees full token-level interaction, producing significantly more accurate relevance judgments. The trade-off is speed: cross-encoders can't precompute, so they must run at query time. But because we only rerank a small candidate set (10-30 items from Stage 1), this is fast enough.

### Current Pipeline

```
query → embed(query) → vector search (top 30) ─┐
                                                 ├─ RRF merge → MMR diversity → return top-k
query → BM25 keyword search (top 30) ───────────┘
```

### Proposed Pipeline

```
query → embed(query) → vector search (top 30) ─┐
                                                 ├─ RRF merge → cross-encoder rerank → MMR diversity → return top-k
query → BM25 keyword search (top 30) ───────────┘
```

### Why This Matters for Nous

Nous stores heterogeneous memory types (episodes, facts, procedures, decisions, censors) with very different text structures. A fact might say "Tim lives in Silver Spring" while an episode might say "user mentioned being in Maryland near DC." A bi-encoder may not connect these well. A cross-encoder, seeing query + document together, can capture this semantic bridge.

### Key Property: No LLM Call Required

A cross-encoder is a **small BERT-based model** (~22M parameters for MiniLM-L-6), not a generative LLM. It:
- Runs locally via `sentence-transformers` — no API call, no tokens consumed, no billing
- Infers on 20 query-document pairs in **~30-50ms on CPU**
- Adds ~100MB RAM footprint (model weights loaded once at startup)
- Outputs a single relevance score per (query, doc) pair — no generation

This is fundamentally different from "LLM-as-judge" reranking which would require an API call per candidate.

---

## Solution

### Part 1: Cross-Encoder Reranking in recall_deep (MVP)

Add a cross-encoder reranking stage to `recall_deep` in `heart.py`, positioned **after** RRF merge and **before** MMR diversity reranking.

**Model:** `cross-encoder/ms-marco-MiniLM-L-6-v2` (default, configurable)
- 6 transformer layers, 22M parameters
- Trained on MS MARCO passage ranking
- Best speed/accuracy trade-off for general-purpose reranking

**Implementation:**

```python
# nous/heart/reranker.py

from __future__ import annotations
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

@lru_cache(maxsize=1)
def _load_cross_encoder(model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
    """Load cross-encoder model once, cache in memory."""
    from sentence_transformers import CrossEncoder
    logger.info("Loading cross-encoder model: %s", model_name)
    model = CrossEncoder(model_name)
    logger.info("Cross-encoder loaded successfully")
    return model

def cross_encoder_rerank(
    query: str,
    candidates: list,
    text_extractor: callable,
    model_name: str | None = None,
    limit: int | None = None,
) -> list:
    """Rerank candidates using a cross-encoder model.

    Args:
        query: The search query string.
        candidates: List of result objects (must have searchable text).
        text_extractor: Function that extracts reranking text from a candidate.
            E.g., lambda c: c.content or lambda c: f"{c.summary} {c.content}"
        model_name: Cross-encoder model name. None = default MiniLM-L-6.
        limit: Max results to return. None = return all reranked.

    Returns:
        Reranked list of candidates, ordered by cross-encoder score DESC.
    """
    if not candidates or len(candidates) <= 1:
        return candidates

    model = _load_cross_encoder(model_name or "cross-encoder/ms-marco-MiniLM-L-6-v2")

    # Build (query, document) pairs
    pairs = []
    valid_candidates = []
    for c in candidates:
        doc_text = text_extractor(c)
        if doc_text:
            pairs.append((query, doc_text))
            valid_candidates.append(c)

    if not pairs:
        return candidates

    # Score all pairs in one batch
    scores = model.predict(pairs)

    # Attach scores and sort
    scored = list(zip(valid_candidates, scores))
    scored.sort(key=lambda x: x[1], reverse=True)

    result = [c for c, _ in scored]
    if limit:
        result = result[:limit]

    return result
```

**Integration point in `heart.py` `recall_deep()`:**

After per-type searches are merged and before MMR, insert:

```python
if settings.cross_encoder_enabled:
    from nous.heart.reranker import cross_encoder_rerank

    def _extract_text(item) -> str:
        """Extract reranking text based on memory type."""
        if hasattr(item, 'content'):
            return item.content[:512]  # facts, censors
        if hasattr(item, 'summary'):
            return (item.summary or '')[:512]  # episodes
        if hasattr(item, 'description'):
            return f"{item.name}: {item.description}"[:512]  # procedures
        return str(item)[:512]

    merged = cross_encoder_rerank(
        query=query,
        candidates=merged,
        text_extractor=_extract_text,
        limit=limit * 2,  # Keep extra for MMR to diversify
    )
```

**Text truncation:** Cross-encoder input is capped at 512 chars per document to keep inference fast. MS MARCO MiniLM has a 512-token context window — truncating text ensures we don't exceed it and keeps batch inference under 50ms.

### Part 2: Configuration & Feature Flag

```python
# In nous/config.py (Settings)
NOUS_CROSS_ENCODER_ENABLED: bool = True          # Feature flag
NOUS_CROSS_ENCODER_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
NOUS_CROSS_ENCODER_MAX_CANDIDATES: int = 30      # Max candidates to rerank
NOUS_CROSS_ENCODER_TEXT_LIMIT: int = 512          # Max chars per doc for reranking
```

**Runtime toggle** via RuntimeConfig (same pattern as vector_weight/rrf_k):
```sql
INSERT INTO nous_system.config (key, value, agent_id)
VALUES ('cross_encoder_enabled', 'false', 'emerson');
```

### Part 3: Graceful Degradation

If `sentence-transformers` is not installed, the reranker is silently skipped:

```python
try:
    from sentence_transformers import CrossEncoder
    CROSS_ENCODER_AVAILABLE = True
except ImportError:
    CROSS_ENCODER_AVAILABLE = False
    logger.info("sentence-transformers not installed — cross-encoder reranking disabled")
```

This ensures zero breaking changes for existing deployments.

### Part 4: Text Extraction Strategy

Different memory types need different text representations for reranking:

| Memory Type | Text for Reranking |
|---|---|
| **Fact** | `content` (the fact text) |
| **Episode** | `summary` (compressed episode summary) |
| **Procedure** | `name: description` (skill name + what it does) |
| **Decision** | `description` (what was decided) |
| **Censor** | `trigger_pattern: reason` (what it blocks + why) |

The text extractor function handles this polymorphically based on available attributes.

---

## Pipeline Order Rationale

The final pipeline order is:

1. **Vector search + Keyword search** — cast a wide net (top 30 each)
2. **RRF merge** — combine into single ranked list (~30-50 unique candidates)
3. **Cross-encoder rerank** ← NEW — precision reranking of top candidates
4. **Utility boost (F037)** — blend empirical effectiveness signals
5. **Frame boost** — boost same-frame memories
6. **MMR diversity (F030)** — reduce redundancy in final set

Cross-encoder goes **before** utility/frame boost because:
- Cross-encoder provides **semantic relevance** — the ground truth of "is this relevant?"
- Utility and frame boosts are **contextual adjustments** on top of relevance
- MMR is last because it trades relevance for diversity — should operate on the most relevant set

---

## Future Phases (Not in MVP)

### Phase 2: Domain Fine-Tuning

Fine-tune the cross-encoder on Nous-specific query-memory pairs. Training data sources:
- **F025 test set** — 50 queries with ground truth relevance labels (ready-made)
- **Implicit feedback** — memories that were loaded into context AND the task succeeded (positive), vs loaded but task failed (negative)
- **Sleep consolidation signals** — facts that get reinforced vs decayed

The article showed domain fine-tuning improved accuracy from 30% → 95% on 72 training pairs. Even a small Nous-specific dataset could dramatically improve recall quality.

### Phase 3: Knowledge Distillation

Use the fine-tuned cross-encoder as a teacher to improve our bi-encoder embeddings. This would improve Stage 1 retrieval quality with zero runtime cost — better candidates enter the pipeline from the start.

### Phase 4: Adaptive Reranking

Only invoke the cross-encoder when the RRF scores show ambiguity (e.g., top-10 scores are tightly clustered). If there's a clear winner with large score gaps, skip the reranker to save compute.

---

## Dependencies

**New Python dependency:**
```
sentence-transformers>=3.0.0
```

This pulls in `torch` (~2GB) and `transformers`. For production, consider:
- `onnxruntime` + exported ONNX model for lighter inference
- CPU-only torch (`torch-cpu`) to avoid GPU dependency bloat

**Alternative: API-based reranking** (no local dependency)
- Cohere Rerank API — ~$1/1000 queries
- Jina Rerank API — similar pricing
- Voyage Rerank — already using Voyage for embeddings

Decision: Start with local `sentence-transformers` for zero-cost, zero-latency reranking. Evaluate API option if deployment size is a concern.

---

## Metrics & Evaluation

Use the F025 test query set to measure:
- **Precision@5** and **Recall@10** before/after cross-encoder
- **Latency impact** — p50/p95 of reranking stage
- **Score distribution** — do cross-encoder scores show better separation between relevant/irrelevant?

Log cross-encoder scores alongside RRF scores in observability (F035) for comparison.

---

## Implementation Estimate

- **Part 1 (reranker module):** ~60 LOC, 1 new file
- **Part 2 (integration in heart.py):** ~30 LOC modification
- **Part 3 (config/feature flag):** ~15 LOC
- **Part 4 (text extraction):** ~20 LOC
- **Tests:** ~80 LOC
- **Total MVP:** ~200 LOC + dependency addition

---

## Risk Assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| `sentence-transformers` bloats Docker image | HIGH | Use ONNX runtime or API fallback |
| Model cold-start on first query (~2-3s) | MEDIUM | Eager-load model during Heart initialization |
| Cross-encoder disagrees with RRF on obvious matches | LOW | Log both scores, monitor disagreement rate |
| torch CPU inference too slow on constrained hardware | LOW | 20 pairs × MiniLM = ~30ms — well within budget |
