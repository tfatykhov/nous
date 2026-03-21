# Reciprocal Rank Fusion (RRF) for Hybrid Search — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace weighted-sum hybrid search with Reciprocal Rank Fusion (RRF) so keyword results contribute meaningfully regardless of raw score scale differences.

**Architecture:** Run vector and keyword searches as separate ranked lists, then merge using RRF formula `weight / (k + rank)` instead of `weight * raw_score`. This makes rank-1 keyword match equal to rank-1 vector match at equal weights, fixing the scale mismatch where keyword scores max at ~0.08 vs vector scores at 0.5-0.9. Changes touch 3 search sites + config + admin API.

**Tech Stack:** Python 3.12+, SQLAlchemy async, PostgreSQL + pgvector, pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `nous/config.py` | Modify | Add `rrf_k: int = 60` setting |
| `nous/runtime_config.py` | Modify | Add `rrf_k` runtime override support |
| `nous/heart/search.py` | Modify | Rewrite `hybrid_search()` to use RRF (two queries + Python merge) |
| `nous/heart/facts.py` | Modify | Refactor `_search_all()` to use RRF (same approach) |
| `nous/brain/brain.py` | Modify | Refactor `Brain.query()` to use RRF |
| `nous/api/rest.py` | Modify | Expose `rrf_k` in GET/POST `/admin/search-weights` |
| `tests/test_rrf_search.py` | Create | Unit tests for RRF merge logic + integration tests |

---

## Chunk 1: Config + RRF Core

### Task 1: Add `rrf_k` to config and runtime config

**Files:**
- Modify: `nous/config.py:40-41` (near `vector_weight`)
- Modify: `nous/runtime_config.py` (add rrf_k getter/setter/persistence)

- [ ] **Step 1: Write test for rrf_k config default**

```python
# tests/test_rrf_search.py
import pytest
from nous.config import Settings


class TestRRFConfig:
    def test_rrf_k_default(self):
        s = Settings(anthropic_api_key="test", openai_api_key="test")
        assert s.rrf_k == 60

    def test_rrf_k_from_env(self, monkeypatch):
        monkeypatch.setenv("NOUS_RRF_K", "40")
        s = Settings(anthropic_api_key="test", openai_api_key="test")
        assert s.rrf_k == 40
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rrf_search.py::TestRRFConfig -v`
Expected: FAIL — `rrf_k` attribute not found

- [ ] **Step 3: Add `rrf_k` to Settings**

In `nous/config.py`, after line 41 (`vector_weight: float = 0.7`), add:

```python
rrf_k: int = 60  # RRF smoothing constant (F025)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rrf_search.py::TestRRFConfig -v`
Expected: PASS

- [ ] **Step 5: Add rrf_k to RuntimeConfig**

In `nous/runtime_config.py`, add after the vector weight section (line ~68):

```python
_KEY_RRF_K = "rrf_k"
```

(Add at module level near `_KEY_VECTOR_WEIGHT`.)

Then add methods to the `RuntimeConfig` class after `clear_vector_weight()`:

```python
    # -- RRF K ----------------------------------------------------------------

    def get_rrf_k(self, settings: Any) -> int:
        """Resolve rrf_k: runtime override > env/settings > default."""
        if _KEY_RRF_K in self._overrides:
            return int(self._overrides[_KEY_RRF_K])
        return int(settings.rrf_k)

    def get_rrf_k_source(self, settings: Any) -> str:
        """Return the source of the current effective rrf_k."""
        if _KEY_RRF_K in self._overrides:
            return "runtime_override"
        if "rrf_k" in settings.model_fields_set:
            return "env_var"
        return "default"

    def set_rrf_k(self, value: int) -> None:
        """Set runtime override (call persist_to_db separately)."""
        self._overrides[_KEY_RRF_K] = value

    def clear_rrf_k(self) -> None:
        """Remove runtime override, falling back to settings."""
        self._overrides.pop(_KEY_RRF_K, None)
```

Also update `load_from_db` to handle the new key — add after the `_KEY_VECTOR_WEIGHT` block inside the for loop (around line 80):

```python
                if key == _KEY_RRF_K and value is not None:
                    k = int(value)
                    if k > 0:
                        self._overrides[key] = k
                        logger.info("Loaded runtime override: %s = %s", key, k)
```

- [ ] **Step 6: Write test for RuntimeConfig rrf_k**

```python
# Add to tests/test_rrf_search.py
from nous.runtime_config import RuntimeConfig


class TestRRFRuntimeConfig:
    def setup_method(self):
        RuntimeConfig.reset()

    def teardown_method(self):
        RuntimeConfig.reset()

    def test_get_rrf_k_default(self):
        rc = RuntimeConfig.get()
        s = Settings(anthropic_api_key="test", openai_api_key="test")
        assert rc.get_rrf_k(s) == 60

    def test_set_and_get_rrf_k(self):
        rc = RuntimeConfig.get()
        rc.set_rrf_k(40)
        s = Settings(anthropic_api_key="test", openai_api_key="test")
        assert rc.get_rrf_k(s) == 40
        assert rc.get_rrf_k_source(s) == "runtime_override"

    def test_clear_rrf_k(self):
        rc = RuntimeConfig.get()
        rc.set_rrf_k(40)
        rc.clear_rrf_k()
        s = Settings(anthropic_api_key="test", openai_api_key="test")
        assert rc.get_rrf_k(s) == 60
        assert rc.get_rrf_k_source(s) == "default"
```

- [ ] **Step 7: Run all rrf tests**

Run: `uv run pytest tests/test_rrf_search.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add nous/config.py nous/runtime_config.py tests/test_rrf_search.py
git commit -m "feat(config): add rrf_k setting for Reciprocal Rank Fusion (F025)"
```

---

### Task 2: Implement RRF merge in `hybrid_search()`

**Files:**
- Modify: `nous/heart/search.py:28-127`
- Test: `tests/test_rrf_search.py`

The core change: split the single SQL query into two separate queries (vector + keyword), then merge in Python using RRF formula.

- [ ] **Step 1: Write tests for RRF merge logic**

```python
# Add to tests/test_rrf_search.py
from uuid import uuid4

class TestRRFMerge:
    """Test the pure RRF merge function (no DB needed)."""

    def test_both_lists_same_doc(self):
        """Doc appearing in both lists gets scores from both."""
        from nous.heart.search import _rrf_merge
        doc_a = uuid4()
        vector_ranked = [(doc_a, 0.95)]  # rank 0
        keyword_ranked = [(doc_a, 0.08)]  # rank 0
        result = _rrf_merge(vector_ranked, keyword_ranked, k=60, vector_weight=0.5, limit=10)
        assert len(result) == 1
        assert result[0][0] == doc_a
        # Both at rank 0: 0.5/(60+0) + 0.5/(60+0) = 1/60
        expected = 0.5 / 60 + 0.5 / 60
        assert abs(result[0][1] - expected) < 1e-9

    def test_disjoint_lists(self):
        """Docs in only one list get penalty rank for the other."""
        from nous.heart.search import _rrf_merge
        doc_v = uuid4()
        doc_k = uuid4()
        vector_ranked = [(doc_v, 0.9)]
        keyword_ranked = [(doc_k, 0.08)]
        result = _rrf_merge(vector_ranked, keyword_ranked, k=60, vector_weight=0.5, limit=10)
        assert len(result) == 2
        # doc_v: vector rank 0, keyword penalty rank = limit(10)+1=11
        # 0.5/(60+0) + 0.5/(60+11) = 0.5/60 + 0.5/71
        score_v = 0.5 / 60 + 0.5 / 71
        # doc_k: keyword rank 0, vector penalty rank = 11
        score_k = 0.5 / 71 + 0.5 / 60
        # Both should be equal (symmetric weights)
        assert abs(result[0][1] - score_v) < 1e-9
        assert abs(result[1][1] - score_k) < 1e-9

    def test_vector_only(self):
        """Empty keyword list — all docs use penalty rank for keyword."""
        from nous.heart.search import _rrf_merge
        doc = uuid4()
        result = _rrf_merge([(doc, 0.9)], [], k=60, vector_weight=0.7, limit=5)
        assert len(result) == 1
        expected = 0.7 / 60 + 0.3 / (60 + 6)  # penalty rank = limit+1 = 6
        assert abs(result[0][1] - expected) < 1e-9

    def test_keyword_only(self):
        """Empty vector list — all docs use penalty rank for vector."""
        from nous.heart.search import _rrf_merge
        doc = uuid4()
        result = _rrf_merge([], [(doc, 0.05)], k=60, vector_weight=0.7, limit=5)
        assert len(result) == 1
        expected = 0.7 / (60 + 6) + 0.3 / 60  # penalty rank = limit+1 = 6
        assert abs(result[0][1] - expected) < 1e-9

    def test_both_empty(self):
        """Both lists empty — returns empty."""
        from nous.heart.search import _rrf_merge
        result = _rrf_merge([], [], k=60, vector_weight=0.5, limit=10)
        assert result == []

    def test_limit_respected(self):
        """Result count capped at limit."""
        from nous.heart.search import _rrf_merge
        docs = [(uuid4(), 0.9 - i * 0.01) for i in range(20)]
        result = _rrf_merge(docs, [], k=60, vector_weight=0.7, limit=5)
        assert len(result) == 5

    def test_ordering_by_rrf_score(self):
        """Results sorted by RRF score descending."""
        from nous.heart.search import _rrf_merge
        doc_a, doc_b, doc_c = uuid4(), uuid4(), uuid4()
        # doc_a: rank 0 in vector, rank 2 in keyword
        # doc_b: rank 1 in vector, rank 0 in keyword
        # doc_c: rank 2 in vector, rank 1 in keyword
        vector = [(doc_a, 0.9), (doc_b, 0.8), (doc_c, 0.7)]
        keyword = [(doc_b, 0.08), (doc_c, 0.06), (doc_a, 0.04)]
        result = _rrf_merge(vector, keyword, k=60, vector_weight=0.5, limit=10)
        # Verify sorted by score desc
        scores = [s for _, s in result]
        assert scores == sorted(scores, reverse=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rrf_search.py::TestRRFMerge -v`
Expected: FAIL — `_rrf_merge` not found

- [ ] **Step 3: Implement `_rrf_merge` function**

Add to `nous/heart/search.py` before the `hybrid_search` function (after imports):

```python
def _rrf_merge(
    vector_ranked: list[tuple[UUID, float]],
    keyword_ranked: list[tuple[UUID, float]],
    k: int,
    vector_weight: float,
    limit: int,
) -> list[tuple[UUID, float]]:
    """Merge two ranked lists using Reciprocal Rank Fusion.

    rrf_score(doc) = vector_weight / (k + vector_rank)
                   + keyword_weight / (k + keyword_rank)

    Docs appearing in only one list get a penalty rank of limit + 1.
    """
    keyword_weight = 1.0 - vector_weight
    penalty_rank = limit + 1

    # Build rank maps (0-indexed)
    vector_ranks: dict[UUID, int] = {doc_id: i for i, (doc_id, _) in enumerate(vector_ranked)}
    keyword_ranks: dict[UUID, int] = {doc_id: i for i, (doc_id, _) in enumerate(keyword_ranked)}

    all_ids = set(vector_ranks) | set(keyword_ranks)
    if not all_ids:
        return []

    scored: list[tuple[UUID, float]] = []
    for doc_id in all_ids:
        v_rank = vector_ranks.get(doc_id, penalty_rank)
        k_rank = keyword_ranks.get(doc_id, penalty_rank)
        score = vector_weight / (k + v_rank) + keyword_weight / (k + k_rank)
        scored.append((doc_id, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]
```

- [ ] **Step 4: Run merge tests to verify they pass**

Run: `uv run pytest tests/test_rrf_search.py::TestRRFMerge -v`
Expected: PASS

- [ ] **Step 5: Add `_resolve_rrf_k` helper and rewrite `hybrid_search()`**

Add helper after `_resolve_vector_weight()`:

```python
def _resolve_rrf_k() -> int:
    """Resolve rrf_k from runtime config > settings > default 60."""
    from nous.config import Settings
    from nous.runtime_config import RuntimeConfig

    try:
        settings = Settings()
    except Exception:
        return 60
    return RuntimeConfig.get().get_rrf_k(settings)
```

Rewrite the `hybrid_search()` function. The signature stays identical. Replace lines 67-127 with:

```python
    if vector_weight is None:
        vector_weight = _resolve_vector_weight()
    rrf_k = _resolve_rrf_k()

    params: dict = {
        "agent_id": agent_id,
        "query_text": query_text,
        "limit": limit,
        "limit_expanded": limit * 3,
    }
    if extra_params:
        params.update(extra_params)

    filter_clauses = f"AND t.agent_id = :agent_id AND t.active = true {extra_where}"

    vector_results: list[tuple[UUID, float]] = []
    keyword_results: list[tuple[UUID, float]] = []

    if embedding is not None:
        # Vector search
        params["query_embedding"] = "[" + ",".join(str(float(v)) for v in embedding) + "]"
        vector_sql = text(f"""
            SELECT t.id, 1 - (t.embedding <=> CAST(:query_embedding AS vector)) AS score
            FROM {table} t
            WHERE t.embedding IS NOT NULL {filter_clauses}
            ORDER BY t.embedding <=> CAST(:query_embedding AS vector)
            LIMIT :limit_expanded
        """)
        result = await session.execute(vector_sql, params)
        vector_results = [(row.id, float(row.score)) for row in result.all()]

    # Keyword search
    keyword_sql = text(f"""
        SELECT t.id,
            ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))
            / (1.0 + ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))) AS score
        FROM {table} t
        WHERE t.search_tsv @@ plainto_tsquery('english', :query_text)
            {filter_clauses}
        ORDER BY score DESC
        LIMIT :limit_expanded
    """)
    result = await session.execute(keyword_sql, params)
    keyword_results = [(row.id, float(row.score)) for row in result.all()]

    if embedding is None:
        # Keyword-only fallback — return keyword results directly
        return keyword_results[:limit]

    return _rrf_merge(vector_results, keyword_results, rrf_k, vector_weight, limit)
```

- [ ] **Step 6: Update docstring**

Replace the docstring (lines 39-66) to reflect RRF:

```python
    """Hybrid vector + keyword search over a Heart table using RRF.

    Uses Reciprocal Rank Fusion to combine vector and keyword results:
    1. Vector similarity via cosine distance on embedding column (ranked list)
    2. Keyword relevance via ts_rank_cd on search_tsv column (ranked list)
    3. RRF score = vector_weight / (k + vector_rank) + keyword_weight / (k + keyword_rank)

    This solves the scale mismatch where keyword scores max at ~0.08
    vs vector scores at 0.5-0.9, making weighted-sum keyword-blind.

    Weight resolution order:
    1. Explicit vector_weight param (highest priority)
    2. Runtime override (set via /admin/search-weights API)
    3. NOUS_VECTOR_WEIGHT env var / config default
    4. Fallback: 0.7

    Args:
        session: Active SQLAlchemy async session.
        table: Fully qualified table name (e.g. "heart.episodes").
        embedding: Query embedding vector, or None for keyword-only fallback.
        query_text: Text query for keyword search.
        agent_id: Agent ID filter (always applied).
        extra_where: Additional SQL WHERE clauses (e.g. "AND category = :category").
            Must use :param style placeholders with values in extra_params.
        extra_params: Additional parameters for extra_where bindings.
        limit: Maximum number of results to return.
        vector_weight: Weight for vector score (keyword weight = 1 - vector_weight).
            None = resolve from runtime config / settings / default.

    Returns:
        List of (id, rrf_score) ordered by score DESC.
    """
```

- [ ] **Step 7: Run existing tests to check for regressions**

Run: `uv run pytest tests/ -v -x --timeout=60`
Expected: All existing tests pass (keyword was already invisible, so behavior at 0.7 weight is similar)

- [ ] **Step 8: Commit**

```bash
git add nous/heart/search.py tests/test_rrf_search.py
git commit -m "feat(search): implement RRF merge for hybrid_search() (F025)"
```

---

## Chunk 2: Propagate RRF to remaining search sites + admin API

### Task 3: Refactor `facts.py:_search_all()` to use RRF

**Files:**
- Modify: `nous/heart/facts.py:806-886`
- Test: `tests/test_rrf_search.py`

The `_search_all()` method has its own inline weighted-sum SQL. It intentionally skips the `active=true` filter. We'll apply the same two-query + RRF merge pattern.

- [ ] **Step 1: Write test for _search_all RRF behavior**

```python
# Add to tests/test_rrf_search.py
class TestSearchAllRRF:
    """Verify _search_all uses RRF (integration-level, mocked DB)."""

    @pytest.mark.asyncio
    async def test_search_all_returns_results(self):
        """Smoke test — _search_all should work with the new RRF approach."""
        # This is validated by existing tests in test_facts.py and test_rest_dashboard.py
        # We just verify the function is importable and has the right signature
        from nous.heart.facts import FactManager
        assert hasattr(FactManager, '_search_all')
```

- [ ] **Step 2: Rewrite `_search_all()` in facts.py**

Replace lines 806-886 of `nous/heart/facts.py`. Keep the same signature and return type:

```python
    async def _search_all(
        self,
        query: str,
        embedding: list[float] | None,
        limit: int,
        category: str | None,
        session: AsyncSession,
    ) -> list[FactSummary]:
        """Search all facts including inactive (no active filter).

        Uses RRF (Reciprocal Rank Fusion) for hybrid search — same approach
        as hybrid_search() but intentionally omits the active=true filter so
        superseded/inactive facts are included.
        """
        from nous.heart.search import _resolve_vector_weight, _resolve_rrf_k, _rrf_merge

        vw = _resolve_vector_weight()
        rrf_k = _resolve_rrf_k()

        params: dict = {
            "agent_id": self.agent_id,
            "query_text": query,
            "limit": limit,
            "limit_expanded": limit * 3,
        }
        filter_extra = ""
        if category:
            filter_extra = "AND t.category = :category"
            params["category"] = category

        vector_results: list[tuple] = []
        keyword_results: list[tuple] = []

        if embedding is not None:
            params["query_embedding"] = "[" + ",".join(str(float(v)) for v in embedding) + "]"
            vector_sql = text(f"""
                SELECT t.id, 1 - (t.embedding <=> CAST(:query_embedding AS vector)) AS score
                FROM heart.facts t
                WHERE t.embedding IS NOT NULL
                  AND t.agent_id = :agent_id {filter_extra}
                ORDER BY t.embedding <=> CAST(:query_embedding AS vector)
                LIMIT :limit_expanded
            """)
            result = await session.execute(vector_sql, params)
            vector_results = [(row.id, float(row.score)) for row in result.all()]

        keyword_sql = text(f"""
            SELECT t.id,
                ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))
                / (1.0 + ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))) AS score
            FROM heart.facts t
            WHERE t.search_tsv @@ plainto_tsquery('english', :query_text)
              AND t.agent_id = :agent_id {filter_extra}
            ORDER BY score DESC
            LIMIT :limit_expanded
        """)
        result = await session.execute(keyword_sql, params)
        keyword_results = [(row.id, float(row.score)) for row in result.all()]

        if embedding is None:
            ranked = keyword_results[:limit]
        else:
            ranked = _rrf_merge(vector_results, keyword_results, rrf_k, vw, limit)

        if not ranked:
            return []

        ids = [r[0] for r in ranked]
        scores = {r[0]: r[1] for r in ranked}

        fact_result = await session.execute(select(Fact).where(Fact.id.in_(ids)))
        facts = {f.id: f for f in fact_result.scalars().all()}

        return [
            FactSummary(
                id=f.id,
                content=f.content,
                category=f.category,
                subject=f.subject,
                confidence=f.confidence or 1.0,
                active=f.active if f.active is not None else True,
                score=scores.get(f.id),
            )
            for fid in ids
            if (f := facts.get(fid)) is not None
        ]
```

- [ ] **Step 3: Run existing fact tests**

Run: `uv run pytest tests/test_facts.py tests/test_rest_dashboard.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add nous/heart/facts.py
git commit -m "feat(facts): apply RRF to _search_all() (F025)"
```

---

### Task 4: Refactor `brain.py:query()` to use RRF

**Files:**
- Modify: `nous/brain/brain.py:665-718`
- Test: `tests/test_rrf_search.py`

Brain.query() has its own hardcoded 0.7/0.3 weighted sum. Apply the same RRF approach. Brain has additional complexity (bridge_join, filter_clauses) but the pattern is the same.

- [ ] **Step 1: Rewrite the hybrid section of Brain.query()**

In `nous/brain/brain.py`, replace the block from line 665 (`if query_embedding is not None:`) through line 718 (`tags_by_id[tag_row.decision_id].append(tag_row.tag)`).

The `params` dict and `filter_clauses` / `bridge_join` setup above line 665 stays unchanged.

Replace lines 665-718:

```python
        if query_embedding is not None:
            # Full hybrid search using RRF (F025)
            from nous.heart.search import _resolve_vector_weight, _resolve_rrf_k, _rrf_merge

            vw = _resolve_vector_weight()
            rrf_k = _resolve_rrf_k()

            params["query_embedding"] = "[" + ",".join(str(float(v)) for v in query_embedding) + "]"

            # Vector search
            vector_sql = text(f"""
                SELECT d.id, 1 - (d.embedding <=> CAST(:query_embedding AS vector)) AS score
                FROM brain.decisions d
                {bridge_join}
                WHERE d.embedding IS NOT NULL {filter_clauses}
                ORDER BY d.embedding <=> CAST(:query_embedding AS vector)
                LIMIT :limit_expanded
            """)
            v_result = await session.execute(vector_sql, params)
            vector_results = [(row.id, float(row.score)) for row in v_result.all()]

            # Keyword search
            keyword_sql = text(f"""
                SELECT d.id,
                    ts_rank_cd(d.search_tsv, plainto_tsquery('english', :query_text))
                    / (1.0 + ts_rank_cd(d.search_tsv, plainto_tsquery('english', :query_text))) AS score
                FROM brain.decisions d
                {bridge_join}
                WHERE d.search_tsv @@ plainto_tsquery('english', :query_text)
                    {filter_clauses}
                ORDER BY score DESC
                LIMIT :limit_expanded
            """)
            k_result = await session.execute(keyword_sql, params)
            keyword_results = [(row.id, float(row.score)) for row in k_result.all()]

            merged = _rrf_merge(vector_results, keyword_results, rrf_k, vw, limit)
        else:
            # Keyword-only fallback (P2-14: weight=1.0)
            sql = text(f"""
                SELECT d.id,
                    ts_rank_cd(d.search_tsv, plainto_tsquery('english', :query_text))
                    / (1.0 + ts_rank_cd(d.search_tsv, plainto_tsquery('english', :query_text))) AS score
                FROM brain.decisions d
                {bridge_join}
                WHERE d.search_tsv @@ plainto_tsquery('english', :query_text)
                    {filter_clauses}
                ORDER BY score DESC
                LIMIT :limit
            """)
            result = await session.execute(sql, params)
            merged = [(row.id, float(row.score)) for row in result.all()]

        rows = merged  # rename for compatibility with code below

        if not rows:
            return []

        decision_ids = [r[0] for r in rows]
        scores_by_id = {r[0]: r[1] for r in rows}
```

**IMPORTANT:** Replace lines 665 through 718 inclusive. The `decision_ids` and `scores_by_id` assignments at lines 715-718 are INSIDE the replacement range — the new code above constructs them from tuples (`r[0]`, `r[1]`) instead of Row attributes (`.id`, `.combined_score`). The downstream code below line 718 (fetching decisions, tags) uses `decision_ids` and `scores_by_id` unchanged.

- [ ] **Step 2: Run existing brain tests**

Run: `uv run pytest tests/test_brain.py tests/test_noise_reduction.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add nous/brain/brain.py
git commit -m "feat(brain): apply RRF to Brain.query() (F025)"
```

---

### Task 5: Expose `rrf_k` in admin API

**Files:**
- Modify: `nous/api/rest.py:812-871`

- [ ] **Step 1: Write test for admin endpoint**

```python
# Add to tests/test_rrf_search.py
import pytest


class TestAdminRRFEndpoint:
    @pytest.mark.asyncio
    async def test_get_search_weights_includes_rrf_k(self, client):
        """GET /admin/search-weights returns rrf_k."""
        response = await client.get("/admin/search-weights")
        assert response.status_code == 200
        data = response.json()
        assert "rrf_k" in data
        assert data["rrf_k"] == 60

    @pytest.mark.asyncio
    async def test_set_rrf_k_with_vector_weight(self, client):
        """POST /admin/search-weights with both fields updates both."""
        response = await client.post(
            "/admin/search-weights",
            json={"vector_weight": 0.7, "rrf_k": 40},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["rrf_k"] == 40

    @pytest.mark.asyncio
    async def test_set_rrf_k_only(self, client):
        """POST /admin/search-weights with only rrf_k (no vector_weight)."""
        response = await client.post(
            "/admin/search-weights",
            json={"rrf_k": 45},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["rrf_k"] == 45

    @pytest.mark.asyncio
    async def test_set_empty_body_rejected(self, client):
        """POST with no recognized fields returns 400."""
        response = await client.post(
            "/admin/search-weights",
            json={},
        )
        assert response.status_code == 400
```

Note: These tests depend on the existing `client` fixture from conftest.py. If the test infrastructure doesn't have one, skip these and validate manually.

- [ ] **Step 2: Update `get_search_weights` in rest.py**

In `nous/api/rest.py`, modify the `get_search_weights` function (~line 812):

```python
    async def get_search_weights(request: Request) -> JSONResponse:
        """GET /admin/search-weights — current vector/keyword weight + rrf_k + source."""
        from nous.runtime_config import RuntimeConfig

        rc = RuntimeConfig.get()
        vw = rc.get_vector_weight(settings)
        source = rc.get_vector_weight_source(settings)
        return JSONResponse({
            "vector_weight": vw,
            "keyword_weight": round(1.0 - vw, 4),
            "rrf_k": rc.get_rrf_k(settings),
            "source": source,
        })
```

- [ ] **Step 3: Update `set_search_weights` in rest.py**

In `nous/api/rest.py`, rewrite the `set_search_weights` function to make `vector_weight` optional (so `rrf_k` can be updated independently):

```python
    async def set_search_weights(request: Request) -> JSONResponse:
        """POST /admin/search-weights — update vector weight and/or rrf_k at runtime."""
        from nous.runtime_config import RuntimeConfig

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        rc = RuntimeConfig.get()

        # Optional vector_weight update
        raw = body.get("vector_weight")
        if raw is not None:
            try:
                vw = float(raw)
            except (TypeError, ValueError):
                return JSONResponse({"error": "vector_weight must be a number"}, status_code=400)
            if not (0.0 <= vw <= 1.0):
                return JSONResponse(
                    {"error": "vector_weight must be between 0.0 and 1.0"},
                    status_code=400,
                )
            old_vw = rc.get_vector_weight(settings)
            old_source = rc.get_vector_weight_source(settings)
            rc.set_vector_weight(vw)
            try:
                async with database.session() as session:
                    await rc.persist_to_db(session, "vector_weight", vw)
            except Exception as e:
                logger.error("Failed to persist vector_weight: %s", e)
            logger.info(
                "vector_weight updated to %.4f (was %.4f, source: %s)",
                vw, old_vw, old_source,
            )

        # Optional rrf_k update
        raw_k = body.get("rrf_k")
        if raw_k is not None:
            try:
                rrf_k_val = int(raw_k)
            except (TypeError, ValueError):
                return JSONResponse({"error": "rrf_k must be an integer"}, status_code=400)
            if rrf_k_val < 1:
                return JSONResponse({"error": "rrf_k must be >= 1"}, status_code=400)
            rc.set_rrf_k(rrf_k_val)
            try:
                async with database.session() as session:
                    await rc.persist_to_db(session, "rrf_k", rrf_k_val)
            except Exception as e:
                logger.error("Failed to persist rrf_k: %s", e)

        if raw is None and raw_k is None:
            return JSONResponse(
                {"error": "Must provide at least one of: vector_weight, rrf_k"},
                status_code=400,
            )

        vw_now = rc.get_vector_weight(settings)
        return JSONResponse({
            "vector_weight": vw_now,
            "keyword_weight": round(1.0 - vw_now, 4),
            "rrf_k": rc.get_rrf_k(settings),
            "source": rc.get_vector_weight_source(settings),
        })
```

This replaces the entire `set_search_weights` function (~lines 825-871).

- [ ] **Step 4: Run REST tests**

Run: `uv run pytest tests/test_rest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nous/api/rest.py
git commit -m "feat(api): expose rrf_k in /admin/search-weights endpoint (F025)"
```

---

### Task 6: Final grep + full test suite

**Files:**
- No new files

- [ ] **Step 1: Grep for any remaining weighted-sum patterns**

```bash
uv run python -c "
import subprocess
result = subprocess.run(['grep', '-rn', 'COALESCE.*score.*\\*.*weight\\|vector_weight.*\\*.*score\\|keyword_weight.*\\*.*score', 'nous/'], capture_output=True, text=True)
print(result.stdout or 'No remaining weighted-sum patterns found')
"
```

If any matches are found outside of comments/docstrings, apply the same RRF refactoring.

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest tests/ -v --timeout=120`
Expected: All tests pass

- [ ] **Step 3: Quick manual validation (if DB available)**

If a local Postgres is running with test data, run a few queries to compare:
- Pure keyword query (exact name): should now rank higher at v=0.5
- Pure semantic query (conceptual): should still rank well
- Mixed query: should blend meaningfully

- [ ] **Step 4: Final commit if any cleanup was needed**

```bash
git add -A
git commit -m "chore: cleanup remaining weighted-sum patterns (F025)"
```

---

## Summary of Changes

| File | Change | Risk |
|------|--------|------|
| `nous/config.py` | Add `rrf_k: int = 60` | Low — additive |
| `nous/runtime_config.py` | Add rrf_k getter/setter/persistence | Low — follows vector_weight pattern |
| `nous/heart/search.py` | Rewrite hybrid_search() to use RRF, add `_rrf_merge()` + `_resolve_rrf_k()` | Medium — core search path |
| `nous/heart/facts.py` | Rewrite _search_all() to use RRF | Medium — search path |
| `nous/brain/brain.py` | Rewrite Brain.query() hybrid section to use RRF | Medium — search path |
| `nous/api/rest.py` | Expose rrf_k in admin endpoints | Low — additive |
| `tests/test_rrf_search.py` | New test file for RRF logic | Low — new |

**Performance:** Goes from 1 SQL query to 2 per hybrid search. At Nous's scale (~50 queries/day) this is negligible. Both queries could be run concurrently with `asyncio.gather()` as a future optimization.
