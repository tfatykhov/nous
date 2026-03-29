# F030: MMR Diversity Re-Ranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Maximal Marginal Relevance (MMR) re-ranking to Heart's `_recall` method so `recall_deep` returns diverse results instead of clustering around the most-discussed topic.

**Architecture:** MMR is a post-processing step inserted after the cross-type merge in `Heart._recall()`. It greedily selects results that maximize `λ · cos_sim(candidate, query) − (1−λ) · max(cos_sim(candidate, already_selected))`. Uses pure-Python cosine similarity (no numpy — consistent with project pattern). Embeddings are batch-fetched via a new shared utility in `search.py`. Feature-flagged with `NOUS_MMR_ENABLED` (default `false`).

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 async, pgvector, pydantic-settings, pytest + pytest-asyncio

**Key review findings addressed:**
- Pure Python cosine sim (no numpy dependency — project pattern; spec incorrectly claims numpy is available but it is NOT in pyproject.toml)
- Embedding fetch routed through `search.py` shared utility, not raw ORM in Heart
- Query embedding for MMR computed once in `_recall` (note: existing 4x per-manager embed redundancy is a pre-existing issue, not addressed in F030 scope)
- Fallback path sorts by score
- Scope: `recall_deep` tool path only (context engine already has `_enforce_diversity`)
- Shadow mode (spec Phase 2): deferred to follow-up — F030 is feature-flagged off by default, validation via unit tests + manual observation
- F025 50-query benchmark referenced in spec does NOT exist in codebase — validation uses unit/integration tests instead

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `nous/heart/search.py` | Modify | Add `cosine_similarity()`, `mmr_rerank()`, `batch_fetch_embeddings()` |
| `nous/heart/heart.py` | Modify | Wire MMR into `_recall()`, hoist query embedding |
| `nous/heart/schemas.py` | No change | `RecallResult` stays unchanged — embeddings are transient |
| `nous/config.py` | Modify | Add `mmr_enabled`, `mmr_diversity_weight` settings |
| `tests/test_mmr.py` | Create | Unit tests for `cosine_similarity`, `mmr_rerank` |
| `tests/test_mmr_integration.py` | Create | Integration tests for MMR in `_recall` pipeline |

---

### Task 1: Pure-Python Cosine Similarity in search.py

**Files:**
- Modify: `nous/heart/search.py` (add at top, after imports)
- Create: `tests/test_mmr.py`

- [ ] **Step 1: Write failing test for cosine_similarity**

```python
# tests/test_mmr.py
"""Tests for MMR diversity re-ranking (F030)."""

from __future__ import annotations

import math

import pytest

from nous.heart.search import cosine_similarity


class TestCosineSimilarity:
    def test_identical_vectors(self):
        a = [1.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_zero_vector_returns_zero(self):
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, b) == 0.0

    def test_both_zero_vectors(self):
        a = [0.0, 0.0]
        b = [0.0, 0.0]
        assert cosine_similarity(a, b) == 0.0

    def test_high_dimensional(self):
        """1536-dim vectors (text-embedding-3-small size)."""
        a = [1.0] * 1536
        b = [1.0] * 1536
        assert cosine_similarity(a, b) == pytest.approx(1.0, abs=1e-6)

    def test_similar_vectors(self):
        a = [1.0, 2.0, 3.0]
        b = [1.1, 2.1, 3.1]
        result = cosine_similarity(a, b)
        assert 0.99 < result < 1.0  # Very similar but not identical
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mmr.py::TestCosineSimilarity -v`
Expected: FAIL with ImportError (cosine_similarity not defined in search.py)

- [ ] **Step 3: Implement cosine_similarity in search.py**

Add at the top of `nous/heart/search.py`, after the existing imports:

```python
import math


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors (pure Python — no numpy dependency).

    Used by MMR re-ranking (F030). Returns 0.0 if either vector is zero.
    """
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mmr.py::TestCosineSimilarity -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add nous/heart/search.py tests/test_mmr.py
git commit -m "feat(f030): add pure-Python cosine_similarity to search.py"
```

---

### Task 2: Core mmr_rerank Function

**Files:**
- Modify: `nous/heart/search.py` (add mmr_rerank after cosine_similarity)
- Modify: `tests/test_mmr.py` (add TestMMRRerank class)

The `mmr_rerank` function takes pre-scored candidates with their embeddings and a query embedding, then greedily selects items maximizing `λ · relevance − (1−λ) · max_similarity_to_selected`.

- [ ] **Step 1: Write failing tests for mmr_rerank**

Add to `tests/test_mmr.py`:

```python
from uuid import uuid4

from nous.heart.schemas import RecallResult
from nous.heart.search import mmr_rerank


def _make_result(score: float = 0.5, mem_type: str = "fact") -> RecallResult:
    """Helper to create a RecallResult with a random ID."""
    return RecallResult(
        type=mem_type,
        id=uuid4(),
        summary=f"test result score={score}",
        score=score,
    )


class TestMMRRerank:
    def test_empty_candidates(self):
        result = mmr_rerank([], {}, [1.0, 0.0, 0.0])
        assert result == []

    def test_single_candidate(self):
        c = _make_result(0.9)
        emb = [1.0, 0.0, 0.0]
        result = mmr_rerank([c], {c.id: emb}, [1.0, 0.0, 0.0])
        assert len(result) == 1
        assert result[0].id == c.id

    def test_lambda_1_preserves_score_order(self):
        """λ=1.0 means pure relevance — should match score-sorted order."""
        c1 = _make_result(0.9)
        c2 = _make_result(0.7)
        c3 = _make_result(0.5)
        # Embeddings: c1 closest to query, c2 next, c3 furthest
        embs = {
            c1.id: [1.0, 0.0, 0.0],
            c2.id: [0.9, 0.4, 0.0],
            c3.id: [0.5, 0.5, 0.5],
        }
        query_emb = [1.0, 0.0, 0.0]
        result = mmr_rerank([c1, c2, c3], embs, query_emb, lambda_=1.0, limit=3)
        # With λ=1.0, no diversity penalty — order is by cosine sim to query
        assert result[0].id == c1.id  # cos=1.0

    def test_clustered_embeddings_get_diversified(self):
        """Two items with identical embeddings — MMR should pick one, then diversify."""
        c1 = _make_result(0.9)
        c2 = _make_result(0.85)  # Same embedding as c1 (cluster)
        c3 = _make_result(0.6)   # Different embedding
        embs = {
            c1.id: [1.0, 0.0, 0.0],
            c2.id: [1.0, 0.0, 0.0],  # Identical to c1
            c3.id: [0.0, 1.0, 0.0],  # Orthogonal
        }
        query_emb = [0.7, 0.7, 0.0]  # Between c1 cluster and c3
        result = mmr_rerank([c1, c2, c3], embs, query_emb, lambda_=0.5, limit=3)
        # c1 selected first (highest relevance to query)
        assert result[0].id == c1.id
        # c3 should be selected second — c2 is penalized for being identical to c1
        assert result[1].id == c3.id
        assert result[2].id == c2.id

    def test_limit_respected(self):
        candidates = [_make_result(0.9 - i * 0.1) for i in range(5)]
        embs = {c.id: [float(i), 0.0, 0.0] for i, c in enumerate(candidates)}
        query_emb = [1.0, 0.0, 0.0]
        result = mmr_rerank(candidates, embs, query_emb, limit=3)
        assert len(result) == 3

    def test_missing_embeddings_appended_after(self):
        """Items without embeddings are appended after MMR-selected items, sorted by score."""
        c1 = _make_result(0.9)
        c2 = _make_result(0.8)  # No embedding
        c3 = _make_result(0.7)
        c4 = _make_result(0.6)  # No embedding
        embs = {
            c1.id: [1.0, 0.0, 0.0],
            c3.id: [0.0, 1.0, 0.0],
        }
        query_emb = [1.0, 0.0, 0.0]
        result = mmr_rerank([c1, c2, c3, c4], embs, query_emb, limit=4)
        # MMR selects c1, c3 first; then c2, c4 appended by score desc
        assert result[0].id == c1.id
        assert result[1].id == c3.id
        assert result[2].id == c2.id  # score 0.8 > 0.6
        assert result[3].id == c4.id

    def test_no_embeddings_at_all_falls_back_to_score_sort(self):
        """When no items have embeddings, fall back to score-sorted order."""
        c1 = _make_result(0.5)
        c2 = _make_result(0.9)
        c3 = _make_result(0.7)
        result = mmr_rerank([c1, c2, c3], {}, [1.0, 0.0, 0.0], limit=3)
        assert result[0].id == c2.id  # Highest score
        assert result[1].id == c3.id
        assert result[2].id == c1.id

    def test_identical_embeddings_no_infinite_loop(self):
        """All items have the same embedding — should not hang."""
        candidates = [_make_result(0.9 - i * 0.1) for i in range(5)]
        embs = {c.id: [1.0, 0.0, 0.0] for c in candidates}
        query_emb = [1.0, 0.0, 0.0]
        result = mmr_rerank(candidates, embs, query_emb, limit=5)
        assert len(result) == 5

    def test_default_lambda(self):
        """Default λ=0.7 should favor relevance but apply some diversity."""
        c1 = _make_result(0.9)
        c2 = _make_result(0.85)
        embs = {
            c1.id: [1.0, 0.0, 0.0],
            c2.id: [0.99, 0.1, 0.0],  # Very similar to c1
        }
        query_emb = [1.0, 0.0, 0.0]
        # With default lambda, should still select both (only 2 items)
        result = mmr_rerank([c1, c2], embs, query_emb)
        assert len(result) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mmr.py::TestMMRRerank -v`
Expected: FAIL with ImportError (mmr_rerank not defined)

- [ ] **Step 3: Implement mmr_rerank in search.py**

Add to `nous/heart/search.py` after `cosine_similarity`:

```python
def mmr_rerank(
    candidates: list,
    embeddings: dict,
    query_embedding: list[float],
    lambda_: float = 0.7,
    limit: int = 10,
) -> list:
    """Maximal Marginal Relevance re-ranking for diversity (F030).

    Greedily selects items maximizing:
      MMR(d) = λ · cos_sim(d, query) − (1−λ) · max(cos_sim(d, selected))

    Items without embeddings are appended after MMR-selected items
    in descending score order.

    Args:
        candidates: Pre-scored results (must have .id and .score attrs).
        embeddings: Map of result ID → embedding vector (list[float]).
        query_embedding: The query's embedding vector.
        lambda_: Relevance vs diversity weight (0.0–1.0). Default 0.7.
        limit: Number of results to return.

    Returns:
        Re-ranked list, length ≤ limit.
    """
    if not candidates:
        return []

    # Separate candidates with/without embeddings
    with_emb = [(c, embeddings[c.id]) for c in candidates if c.id in embeddings]
    without_emb = sorted(
        [c for c in candidates if c.id not in embeddings],
        key=lambda c: c.score,
        reverse=True,
    )

    if len(with_emb) <= 1:
        # Not enough candidates for diversity — fall back to score sort
        all_sorted = sorted(candidates, key=lambda c: c.score, reverse=True)
        return all_sorted[:limit]

    # Precompute query similarities
    query_sims: dict = {}
    for c, emb in with_emb:
        query_sims[c.id] = cosine_similarity(query_embedding, emb)

    selected: list = []
    selected_embs: list[list[float]] = []
    remaining = list(with_emb)

    while len(selected) < limit and remaining:
        best_score = -float("inf")
        best_idx = 0

        for i, (c, emb) in enumerate(remaining):
            relevance = query_sims[c.id]

            if selected_embs:
                max_sim = max(
                    cosine_similarity(emb, s_emb) for s_emb in selected_embs
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

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mmr.py -v`
Expected: All tests PASS (both TestCosineSimilarity and TestMMRRerank)

- [ ] **Step 5: Commit**

```bash
git add nous/heart/search.py tests/test_mmr.py
git commit -m "feat(f030): add mmr_rerank function with greedy diversity selection"
```

---

### Task 3: Batch Embedding Fetch Utility

**Files:**
- Modify: `nous/heart/search.py` (add `batch_fetch_embeddings`)
- Modify: `tests/test_mmr.py` (add integration-ready test)

This adds a shared utility in `search.py` that batch-fetches embeddings for a set of IDs grouped by memory type. Uses raw SQL (consistent with `hybrid_search` pattern) rather than ORM models, so Heart doesn't need to import storage models directly.

- [ ] **Step 1: Write failing test for batch_fetch_embeddings**

Add to `tests/test_mmr.py`:

```python
from unittest.mock import AsyncMock, MagicMock

from nous.heart.search import batch_fetch_embeddings


class TestBatchFetchEmbeddings:
    @pytest.mark.asyncio
    async def test_empty_type_ids(self):
        session = AsyncMock()
        result = await batch_fetch_embeddings(session, {}, "test-agent")
        assert result == {}
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_groups_queries_by_type(self):
        """Should issue one query per memory type."""
        session = AsyncMock()
        # Mock empty results for each query
        mock_result = MagicMock()
        mock_result.all.return_value = []
        session.execute.return_value = mock_result

        id1, id2 = uuid4(), uuid4()
        type_ids = {"fact": [id1], "episode": [id2]}
        await batch_fetch_embeddings(session, type_ids, "test-agent")
        assert session.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_embedding_dict(self):
        """Should return {id: embedding} dict from DB results."""
        session = AsyncMock()
        id1 = uuid4()
        emb = [0.1] * 10

        mock_row = MagicMock()
        mock_row.id = id1
        mock_row.embedding = str(emb)  # pgvector returns string

        mock_result = MagicMock()
        mock_result.all.return_value = [mock_row]
        session.execute.return_value = mock_result

        result = await batch_fetch_embeddings(
            session, {"fact": [id1]}, "test-agent"
        )
        assert id1 in result
        assert result[id1] == emb

    @pytest.mark.asyncio
    async def test_skips_unknown_types(self):
        session = AsyncMock()
        result = await batch_fetch_embeddings(
            session, {"unknown_type": [uuid4()]}, "test-agent"
        )
        assert result == {}
        session.execute.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mmr.py::TestBatchFetchEmbeddings -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement batch_fetch_embeddings in search.py**

Add to `nous/heart/search.py`:

```python
import json


# Table mapping for batch embedding fetch (F030)
_TYPE_TO_TABLE = {
    "fact": "heart.facts",
    "episode": "heart.episodes",
    "procedure": "heart.procedures",
    "censor": "heart.censors",
}


async def batch_fetch_embeddings(
    session: AsyncSession,
    type_ids: dict[str, list[UUID]],
    agent_id: str,
) -> dict[UUID, list[float]]:
    """Batch-fetch embeddings for recall results grouped by memory type (F030).

    Issues one query per memory type (2-4 small index scans on primary key).
    Returns a flat dict mapping result ID → embedding vector.

    Args:
        session: Active SQLAlchemy async session.
        type_ids: Map of memory type → list of IDs to fetch embeddings for.
        agent_id: Agent ID for scoping (defensive filter).

    Returns:
        Dict of {UUID: list[float]} for all IDs that have embeddings.
    """
    embeddings: dict[UUID, list[float]] = {}

    for mem_type, ids in type_ids.items():
        table = _TYPE_TO_TABLE.get(mem_type)
        if not table or not ids:
            continue

        # Build parameterized IN clause
        placeholders = ", ".join(f":id_{i}" for i in range(len(ids)))
        sql = text(f"""
            SELECT id, embedding::text
            FROM {table}
            WHERE id IN ({placeholders})
              AND agent_id = :agent_id
              AND embedding IS NOT NULL
        """)
        params = {f"id_{i}": uid for i, uid in enumerate(ids)}
        params["agent_id"] = agent_id

        result = await session.execute(sql, params)
        for row in result.all():
            # pgvector returns embedding as string "[0.1,0.2,...]"
            emb_str = row.embedding
            if isinstance(emb_str, str):
                emb = json.loads(emb_str)
            elif isinstance(emb_str, list):
                emb = emb_str
            else:
                continue
            embeddings[row.id] = emb

    return embeddings
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mmr.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add nous/heart/search.py tests/test_mmr.py
git commit -m "feat(f030): add batch_fetch_embeddings utility in search.py"
```

---

### Task 4: Config Settings for MMR

**Files:**
- Modify: `nous/config.py` (add 2 settings after F026 section)
- Modify: `tests/test_mmr.py` (add TestMMRConfig class)

- [ ] **Step 1: Write failing test for config**

Add to `tests/test_mmr.py`:

```python
from nous.config import Settings


class TestMMRConfig:
    def test_mmr_disabled_by_default(self):
        s = Settings()
        assert s.mmr_enabled is False

    def test_mmr_enabled_from_env(self, monkeypatch):
        monkeypatch.setenv("NOUS_MMR_ENABLED", "true")
        s = Settings()
        assert s.mmr_enabled is True

    def test_mmr_diversity_weight_default(self):
        s = Settings()
        assert s.mmr_diversity_weight == 0.7

    def test_mmr_diversity_weight_from_env(self, monkeypatch):
        monkeypatch.setenv("NOUS_MMR_DIVERSITY_WEIGHT", "0.5")
        s = Settings()
        assert s.mmr_diversity_weight == 0.5

    def test_mmr_diversity_weight_bounds(self):
        """Weight must be between 0.0 and 1.0."""
        s = Settings()
        assert 0.0 <= s.mmr_diversity_weight <= 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mmr.py::TestMMRConfig -v`
Expected: FAIL with AttributeError (mmr_enabled not defined)

- [ ] **Step 3: Add config fields**

Add to `nous/config.py` after the F026 Execution Integrity section (after `action_gating_external_only`), before the F024 Critic Agent section:

```python
    # F030: MMR Diversity Re-Ranking
    mmr_enabled: bool = False
    mmr_diversity_weight: float = Field(
        default=0.7, ge=0.0, le=1.0,
        description="MMR relevance vs diversity weight (1.0=pure relevance, 0.0=pure diversity)",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mmr.py::TestMMRConfig -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add nous/config.py tests/test_mmr.py
git commit -m "feat(f030): add mmr_enabled and mmr_diversity_weight config settings"
```

---

### Task 5: Wire MMR into Heart._recall

**Files:**
- Modify: `nous/heart/heart.py` (modify `_recall` method, lines 692-761)
- Create: `tests/test_mmr_integration.py`

This is the core wiring task. Changes to `_recall`:
1. After the merge loop, if MMR is enabled and there are 2+ candidates:
   - Group candidate IDs by type
   - Batch-fetch their embeddings via `batch_fetch_embeddings`
   - Generate query embedding (reuse `self._embeddings`)
   - Call `mmr_rerank` on the merged candidates
2. If MMR is disabled or not enough candidates, use existing sort-by-score

- [ ] **Step 1: Write failing integration test**

```python
# tests/test_mmr_integration.py
"""Integration tests for MMR in Heart._recall pipeline (F030)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.config import Settings
from nous.heart.heart import Heart
from nous.heart.schemas import RecallResult


def _make_heart(mmr_enabled: bool = True, mmr_weight: float = 0.7) -> Heart:
    """Create a Heart instance with mocked dependencies."""
    db = MagicMock()
    db.session = MagicMock()
    settings = Settings()
    # Override MMR settings
    object.__setattr__(settings, "mmr_enabled", mmr_enabled)
    object.__setattr__(settings, "mmr_diversity_weight", mmr_weight)
    embeddings = AsyncMock()
    embeddings.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
    heart = Heart(db, settings, embeddings)
    return heart


class TestRecallWithMMR:
    @pytest.mark.asyncio
    async def test_mmr_disabled_uses_score_sort(self):
        """When MMR is disabled, _recall returns score-sorted results."""
        heart = _make_heart(mmr_enabled=False)
        session = AsyncMock()

        # Mock manager searches to return results
        r1 = MagicMock(id=uuid4(), summary="fact1", score=0.9, tags=["a"],
                       subject="s", category="c", confidence=0.8)
        r1.__class__.__name__ = "FactSummary"
        r2 = MagicMock(id=uuid4(), summary="fact2", score=0.5, tags=["b"],
                       subject="s2", category="c2", confidence=0.7)
        r2.__class__.__name__ = "FactSummary"

        # Patch manager searches
        heart.facts.search = AsyncMock(return_value=[r2, r1])
        heart.episodes.search = AsyncMock(return_value=[])
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors.search = AsyncMock(return_value=[])

        results = await heart._recall("test query", 10, None, session)
        # Should be sorted by score DESC
        assert results[0].score >= results[-1].score if len(results) > 1 else True

    @pytest.mark.asyncio
    async def test_mmr_enabled_calls_rerank(self):
        """When MMR is enabled, _recall calls mmr_rerank."""
        heart = _make_heart(mmr_enabled=True)
        session = AsyncMock()

        # Create mock results that Heart._to_recall_result can handle
        from nous.heart.schemas import FactSummary
        r1 = FactSummary(id=uuid4(), content="fact1", subject="s", category="c",
                         confidence=0.8, active=True, score=0.9)
        r2 = FactSummary(id=uuid4(), content="fact2", subject="s2", category="c2",
                         confidence=0.7, active=True, score=0.5)

        heart.facts.search = AsyncMock(return_value=[r1, r2])
        heart.episodes.search = AsyncMock(return_value=[])
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors.search = AsyncMock(return_value=[])

        with patch("nous.heart.heart.batch_fetch_embeddings") as mock_fetch, \
             patch("nous.heart.heart.mmr_rerank") as mock_mmr:
            mock_fetch.return_value = {
                r1.id: [1.0, 0.0, 0.0],
                r2.id: [0.0, 1.0, 0.0],
            }
            mock_mmr.return_value = [
                RecallResult(type="fact", id=r2.id, summary="fact2", score=0.5),
                RecallResult(type="fact", id=r1.id, summary="fact1", score=0.9),
            ]
            results = await heart._recall("test query", 10, None, session)

            mock_fetch.assert_called_once()
            mock_mmr.assert_called_once()
            assert len(results) == 2

    @pytest.mark.asyncio
    async def test_mmr_skipped_with_single_result(self):
        """MMR should not run with 0 or 1 candidates."""
        heart = _make_heart(mmr_enabled=True)
        session = AsyncMock()

        from nous.heart.schemas import FactSummary
        r1 = FactSummary(id=uuid4(), content="only fact", subject="s", category="c",
                         confidence=0.8, active=True, score=0.9)

        heart.facts.search = AsyncMock(return_value=[r1])
        heart.episodes.search = AsyncMock(return_value=[])
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors.search = AsyncMock(return_value=[])

        with patch("nous.heart.heart.batch_fetch_embeddings") as mock_fetch:
            results = await heart._recall("test query", 10, None, session)
            mock_fetch.assert_not_called()
            assert len(results) == 1

    @pytest.mark.asyncio
    async def test_mmr_graceful_on_embedding_failure(self):
        """If embedding provider fails, fall back to score sort."""
        heart = _make_heart(mmr_enabled=True)
        heart._embeddings.embed = AsyncMock(side_effect=Exception("API down"))
        session = AsyncMock()

        from nous.heart.schemas import FactSummary
        r1 = FactSummary(id=uuid4(), content="f1", subject="s", category="c",
                         confidence=0.8, active=True, score=0.9)
        r2 = FactSummary(id=uuid4(), content="f2", subject="s", category="c",
                         confidence=0.8, active=True, score=0.7)

        heart.facts.search = AsyncMock(return_value=[r1, r2])
        heart.episodes.search = AsyncMock(return_value=[])
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors.search = AsyncMock(return_value=[])

        with patch("nous.heart.heart.batch_fetch_embeddings") as mock_fetch:
            mock_fetch.return_value = {}
            results = await heart._recall("test query", 10, None, session)
            # Should still return results (fallback to score sort)
            assert len(results) == 2
            assert results[0].score >= results[1].score
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mmr_integration.py -v`
Expected: FAIL (heart._recall doesn't have MMR logic yet)

- [ ] **Step 3: Wire MMR into Heart._recall**

Modify `nous/heart/heart.py`:

**Add imports** at top (after existing imports):

```python
from nous.heart.search import batch_fetch_embeddings, mmr_rerank
```

**Replace lines 758-761** (the sort + return) with:

```python
        # F030: MMR diversity re-ranking
        if (
            self.settings.mmr_enabled
            and len(merged) > 1
            and self._embeddings is not None
        ):
            try:
                # Group IDs by type for batch fetch
                type_ids: dict[str, list[UUID]] = {}
                for r in merged:
                    type_ids.setdefault(r.type, []).append(r.id)

                # Batch-fetch embeddings for candidates
                embeddings = await batch_fetch_embeddings(
                    session, type_ids, self.agent_id
                )

                # Generate query embedding for MMR relevance term
                query_embedding = await self._embeddings.embed(query)

                merged = mmr_rerank(
                    candidates=merged,
                    embeddings=embeddings,
                    query_embedding=query_embedding,
                    lambda_=self.settings.mmr_diversity_weight,
                    limit=limit,
                )
            except Exception as exc:
                logger.warning("MMR reranking failed, falling back to score sort: %s", exc)
                merged.sort(key=lambda r: r.score, reverse=True)
                merged = merged[:limit]
        else:
            # Sort by original hybrid score DESC
            merged.sort(key=lambda r: r.score, reverse=True)
            merged = merged[:limit]

        return merged
```

**Important:** Remove the old lines 758-761:
```python
        # Sort by original hybrid score DESC
        merged.sort(key=lambda r: r.score, reverse=True)

        return merged[:limit]
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `uv run pytest tests/test_mmr.py tests/test_mmr_integration.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run existing heart tests to verify no regression**

Run: `uv run pytest tests/test_heart.py -v`
Expected: Existing tests still PASS (MMR is off by default)

- [ ] **Step 6: Commit**

```bash
git add nous/heart/heart.py tests/test_mmr_integration.py
git commit -m "feat(f030): wire MMR reranking into Heart._recall with feature flag"
```

---

### Task 6: Update CLAUDE.md Documentation

**Files:**
- Modify: `CLAUDE.md` (add env vars to table)

- [ ] **Step 1: Add MMR env vars to the Environment Variables table**

Add after the `NOUS_ACTION_GATING_EXTERNAL_ONLY` row:

```markdown
| `NOUS_MMR_ENABLED` | `false` | Enable MMR diversity re-ranking in recall_deep |
| `NOUS_MMR_DIVERSITY_WEIGHT` | `0.7` | MMR relevance vs diversity weight (1.0=pure relevance, 0.0=pure diversity) |
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add F030 MMR config vars to CLAUDE.md"
```

---

### Task 7: Final Verification

- [ ] **Step 1: Run all MMR tests**

Run: `uv run pytest tests/test_mmr.py tests/test_mmr_integration.py -v`
Expected: All tests PASS

- [ ] **Step 2: Run full test suite to check for regressions**

Run: `uv run pytest tests/ -v --timeout=120`
Expected: No regressions from F030 changes

- [ ] **Step 3: Verify feature flag works**

Confirm that with `NOUS_MMR_ENABLED=false` (default), the `_recall` method takes the else branch and sorts by score as before. This ensures zero behavioral change for existing deployments.

**Note:** The F030 spec references an "F025 50-query benchmark" for validation and lambda tuning — this benchmark does not exist in the codebase. Validation is covered by the unit and integration tests above. Shadow mode logging (spec Phase 2) and lambda tuning are deferred to a follow-up task once F030 is deployed and manually observed.

---

## Summary

| Task | Description | Est. LOC |
|------|-------------|----------|
| 1 | `cosine_similarity` function + tests | ~15 |
| 2 | `mmr_rerank` function + tests | ~55 |
| 3 | `batch_fetch_embeddings` utility + tests | ~40 |
| 4 | Config settings + tests | ~10 |
| 5 | Wire into `Heart._recall` + integration tests | ~30 |
| 6 | CLAUDE.md docs | ~5 |
| 7 | Final verification | 0 |
| **Total** | | **~155 LOC** |

**Design decisions incorporated from review:**
- Pure Python cosine sim (no numpy — consistent with existing pattern in procedure_learner.py, correlation.py)
- `batch_fetch_embeddings` in `search.py` using raw SQL (follows `hybrid_search` pattern, avoids ORM import in Heart)
- Agent ID scoping in embedding fetch (defensive correctness)
- Fallback sort on all error paths (embedding failure, empty embeddings dict)
- Feature-flagged off by default — zero behavioral change until explicitly enabled
- Scope: `recall_deep` path only — context engine already has `_enforce_diversity`
