# F021 Memory Dashboard Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only web dashboard served at `/dashboard` that visualizes Nous memory, graph, decisions, and system activity.

**Architecture:** Vanilla HTML/CSS/JS SPA with hash routing, served as static files via Starlette `StaticFiles`. Backend adds 4 new dashboard-specific endpoints + extends 5 existing endpoints with filters/pagination. All queries go through a new `dashboard_queries.py` module.

**Tech Stack:** Python 3.12+ (Starlette, SQLAlchemy async), vanilla JS, Chart.js (vendored), D3.js (vendored), PostgreSQL.

**Spec:** `docs/features/F021-memory-dashboard.md`

---

## File Structure

```
nous/api/dashboard_queries.py   (NEW — all dashboard SQL aggregations)
nous/api/rest.py                (MODIFY — 4 new endpoints, 5 extended, static mount)
nous/sql/migrations/017-dashboard-index.sql  (NEW — graph_edges created_at index)
nous/sql/init.sql                   (MODIFY — add idx_graph_edges_created for fresh installs)
static/dashboard/index.html     (NEW — SPA shell)
static/dashboard/css/dashboard.css (NEW — dark theme)
static/dashboard/js/app.js      (NEW — routing, API client, state management)
static/dashboard/js/overview.js  (NEW — stat cards + mini charts)
static/dashboard/js/graph.js     (NEW — D3 force-directed graph)
static/dashboard/js/browser.js   (NEW — tabbed memory browser)
static/dashboard/js/decisions.js (NEW — calibration + decision analytics)
static/dashboard/js/activity.js  (NEW — system activity timeline)
static/dashboard/js/health.js    (NEW — graph health trends)
static/dashboard/lib/chart.min.js (NEW — vendored Chart.js)
static/dashboard/lib/d3.min.js   (NEW — vendored D3.js)
tests/test_dashboard_queries.py  (NEW — unit tests for query functions)
tests/test_rest_dashboard.py     (NEW — integration tests for dashboard endpoints)
```

---

## Chunk 1: Backend — Extend Existing Endpoints

### Task 1: Make `/facts` `q` parameter optional (browse mode)

**Files:**
- Modify: `nous/api/rest.py:301-321`
- Modify: `nous/heart/heart.py` (add `list_facts` method)
- Modify: `nous/heart/facts.py` (add `list_all` method)
- Test: `tests/test_rest_dashboard.py`

- [ ] **Step 1: Write failing test for facts browse mode**

```python
# tests/test_rest_dashboard.py
"""Integration tests for F021 dashboard endpoint extensions."""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.cognitive.layer import CognitiveLayer
from nous.cognitive.schemas import FrameSelection, TurnContext
from nous.heart import CensorInput, FactInput


class MockAgentRunner:
    def __init__(self):
        self._conversations = {}

    async def start(self):
        pass

    async def close(self):
        pass


@pytest_asyncio.fixture
async def brain(db, settings):
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def cognitive(brain, heart, settings):
    return CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")


@pytest.fixture
def app(brain, heart, cognitive, db, settings):
    from nous.api.rest import create_app
    return create_app(MockAgentRunner(), brain, heart, cognitive, db, settings)


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_facts_browse_no_query(client, heart, db):
    """GET /facts without q param returns paginated list."""
    async with db.session() as session:
        await heart.learn(
            FactInput(content="Dashboard browse fact", category="technical", confidence=0.9),
            session=session,
        )
        await session.commit()

    resp = await client.get("/facts?limit=10&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert "facts" in data
    assert "total" in data
    assert data["total"] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rest_dashboard.py::test_facts_browse_no_query -v`
Expected: FAIL — `/facts` returns 400 when `q` is missing.

- [ ] **Step 3: Add `list_all` to FactManager**

In `nous/heart/facts.py`, add a method that returns paginated facts without search:

```python
async def list_all(
    self,
    limit: int = 50,
    offset: int = 0,
    category: str | None = None,
    active_only: bool = True,
    confidence_min: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "created_at",
    order: str = "desc",
    session: AsyncSession | None = None,
) -> tuple[list[FactSummary], int]:
    """Return paginated facts without search. Used by dashboard browse mode."""
    if session is None:
        async with self.db.session() as session:
            return await self._list_all(
                limit, offset, category, active_only,
                confidence_min, date_from, date_to, sort, order, session,
            )
    return await self._list_all(
        limit, offset, category, active_only,
        confidence_min, date_from, date_to, sort, order, session,
    )

async def _list_all(
    self,
    limit: int,
    offset: int,
    category: str | None,
    active_only: bool,
    confidence_min: float | None,
    date_from: str | None,
    date_to: str | None,
    sort: str,
    order: str,
    session: AsyncSession,
) -> tuple[list[FactSummary], int]:
    from sqlalchemy import func as sa_func, text

    conditions = [Fact.agent_id == self.agent_id]
    if active_only:
        conditions.append(Fact.active == True)
    if category:
        conditions.append(Fact.category == category)
    if confidence_min is not None:
        conditions.append(Fact.confidence >= confidence_min)
    if date_from:
        conditions.append(Fact.created_at >= date_from)
    if date_to:
        conditions.append(Fact.created_at <= date_to)

    # Count
    count_q = select(sa_func.count()).select_from(Fact).where(*conditions)
    total = (await session.execute(count_q)).scalar() or 0

    # Sort — VALIDATE against allowlist to prevent attribute injection
    ALLOWED_SORTS = {"created_at", "confidence", "category", "subject"}
    if sort not in ALLOWED_SORTS:
        sort = "created_at"
    if order not in ("asc", "desc"):
        order = "desc"
    sort_col = getattr(Fact, sort)
    order_clause = sort_col.desc() if order == "desc" else sort_col.asc()

    # Fetch
    q = select(Fact).where(*conditions).order_by(order_clause).limit(limit).offset(offset)
    result = await session.execute(q)
    facts = list(result.scalars().all())

    # NOTE: FactSummary has fields: id, content, category, subject, confidence, active, score.
    # It does NOT have source, tags, or learned_at. Use only existing fields.
    summaries = [
        FactSummary(
            id=f.id,
            content=f.content,
            category=f.category,
            subject=f.subject,
            confidence=f.confidence or 1.0,
            active=f.active if f.active is not None else True,
        )
        for f in facts
    ]
    return summaries, total
```

- [ ] **Step 4: Add `list_facts` delegation to Heart**

In `nous/heart/heart.py`, add:

```python
async def list_facts(
    self,
    limit: int = 50,
    offset: int = 0,
    category: str | None = None,
    active_only: bool = True,
    confidence_min: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "created_at",
    order: str = "desc",
    session: AsyncSession | None = None,
) -> tuple[list[FactSummary], int]:
    """List facts with pagination and filters (F021 browse mode)."""
    return await self.facts.list_all(
        limit, offset, category, active_only,
        confidence_min, date_from, date_to, sort, order, session,
    )
```

- [ ] **Step 5: Update `/facts` endpoint in rest.py**

Replace the `search_facts` function in `nous/api/rest.py:301-321`:

```python
async def search_facts(request: Request) -> JSONResponse:
    """GET /facts?q=query&limit=20 - Search or browse facts."""
    q = request.query_params.get("q")

    try:
        limit = int(request.query_params.get("limit", "20"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)

    try:
        if q:
            # Existing search behavior
            category = request.query_params.get("category")
            facts = await heart.search_facts(q, limit=limit, category=category)
            return JSONResponse({
                "facts": [f.model_dump(mode="json") for f in facts],
                "total": len(facts),
            })
        else:
            # Browse mode (F021)
            category = request.query_params.get("category")
            active_param = request.query_params.get("active")
            active_only = active_param != "false" if active_param else True
            confidence_min_str = request.query_params.get("confidence_min")
            confidence_min = float(confidence_min_str) if confidence_min_str else None
            date_from = request.query_params.get("date_from")
            date_to = request.query_params.get("date_to")
            sort = request.query_params.get("sort", "created_at")
            order = request.query_params.get("order", "desc")

            facts, total = await heart.list_facts(
                limit=limit, offset=offset, category=category,
                active_only=active_only, confidence_min=confidence_min,
                date_from=date_from, date_to=date_to, sort=sort, order=order,
            )
            return JSONResponse({
                "facts": [f.model_dump(mode="json") for f in facts],
                "total": total,
                "limit": limit,
                "offset": offset,
            })
    except Exception as e:
        logger.error("Search/browse facts error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
```

- [ ] **Step 6: Update existing test that asserts 400 on missing `q`**

The test at `tests/test_rest.py:303-307` (`test_search_facts_no_query`) asserts 400 when `q` is missing. This must be updated to expect 200 (browse mode):

```python
# tests/test_rest.py — update test_search_facts_no_query
async def test_search_facts_no_query(client):
    """GET /facts without q -> 200 (browse mode, F021)."""
    resp = await client.get("/facts")
    assert resp.status_code == 200
    data = resp.json()
    assert "facts" in data
    assert "total" in data
```

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest tests/test_rest_dashboard.py::test_facts_browse_no_query tests/test_rest.py::test_search_facts_no_query -v`
Expected: Both PASS

- [ ] **Step 8: Write additional filter tests**

```python
async def test_facts_browse_with_category_filter(client, heart, db):
    """GET /facts?category=technical returns only matching facts."""
    async with db.session() as session:
        await heart.learn(
            FactInput(content="Tech fact for filter test", category="technical", confidence=0.9),
            session=session,
        )
        await heart.learn(
            FactInput(content="Person fact for filter test", category="person", confidence=0.8),
            session=session,
        )
        await session.commit()

    resp = await client.get("/facts?category=technical&limit=50")
    assert resp.status_code == 200
    data = resp.json()
    for f in data["facts"]:
        assert f["category"] == "technical"


async def test_facts_search_still_works(client, heart, db):
    """GET /facts?q=something still uses semantic search."""
    async with db.session() as session:
        await heart.learn(
            FactInput(content="Searchable fact about Python", category="technical", confidence=0.9),
            session=session,
        )
        await session.commit()

    resp = await client.get("/facts?q=Python&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert "facts" in data
    assert "total" in data
```

- [ ] **Step 8: Run all facts tests**

Run: `uv run pytest tests/test_rest_dashboard.py -k facts -v`
Expected: All PASS

- [ ] **Step 9: Commit**

```bash
git add nous/heart/facts.py nous/heart/heart.py nous/api/rest.py tests/test_rest_dashboard.py
git commit -m "feat(f021): make /facts q param optional — browse mode with filters"
```

---

### Task 2: Extend `/episodes` with filters and pagination

**Files:**
- Modify: `nous/api/rest.py:283-299`
- Modify: `nous/heart/heart.py` (update `list_episodes` signature)
- Modify: `nous/heart/episodes.py` (add `list_all` with filters)
- Test: `tests/test_rest_dashboard.py`

- [ ] **Step 1: Write failing test**

```python
async def test_episodes_with_offset_and_total(client, heart, db):
    """GET /episodes returns total count and supports offset."""
    from nous.heart.schemas import EpisodeInput

    async with db.session() as session:
        await heart.start_episode(
            EpisodeInput(title="Episode A", summary="First episode", trigger="test"),
            session=session,
        )
        await session.commit()

    resp = await client.get("/episodes?limit=10&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert "episodes" in data
    assert "total" in data
    assert isinstance(data["total"], int)


async def test_episodes_filter_by_outcome(client, heart, db):
    """GET /episodes?outcome=success filters by outcome."""
    resp = await client.get("/episodes?outcome=success&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    for ep in data["episodes"]:
        assert ep.get("outcome") == "success" or len(data["episodes"]) == 0
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_rest_dashboard.py -k episodes -v`
Expected: FAIL — no `total` in response, no `offset` support.

- [ ] **Step 3: Add `list_all` to EpisodeManager**

In `nous/heart/episodes.py`, add a method similar to facts:

```python
async def list_all(
    self,
    limit: int = 50,
    offset: int = 0,
    outcome: str | None = None,
    frame: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "started_at",
    order: str = "desc",
    session: AsyncSession | None = None,
) -> tuple[list[EpisodeSummary], int]:
    """Paginated episode list with filters (F021)."""
    if session is None:
        async with self.db.session() as session:
            return await self._list_all(limit, offset, outcome, frame, date_from, date_to, sort, order, session)
    return await self._list_all(limit, offset, outcome, frame, date_from, date_to, sort, order, session)

async def _list_all(
    self, limit, offset, outcome, frame, date_from, date_to, sort, order, session,
) -> tuple[list[EpisodeSummary], int]:
    from sqlalchemy import func as sa_func

    conditions = [Episode.agent_id == self.agent_id]
    if outcome:
        conditions.append(Episode.outcome == outcome)
    if frame:
        conditions.append(Episode.frame_used == frame)
    if date_from:
        conditions.append(Episode.started_at >= date_from)
    if date_to:
        conditions.append(Episode.started_at <= date_to)

    count_q = select(sa_func.count()).select_from(Episode).where(*conditions)
    total = (await session.execute(count_q)).scalar() or 0

    # Sort — VALIDATE against allowlist to prevent attribute injection
    ALLOWED_SORTS = {"started_at", "ended_at", "outcome", "title"}
    if sort not in ALLOWED_SORTS:
        sort = "started_at"
    if order not in ("asc", "desc"):
        order = "desc"
    sort_col = getattr(Episode, sort)
    order_clause = sort_col.desc() if order == "desc" else sort_col.asc()

    q = select(Episode).where(*conditions).order_by(order_clause).limit(limit).offset(offset)
    result = await session.execute(q)
    episodes = list(result.scalars().all())

    # NOTE: There is NO `_to_summary` method in EpisodeManager.
    # list_recent() constructs EpisodeSummary inline (episodes.py:349-358).
    # Replicate the same inline construction here, adding structured_summary
    # for browser expand view:
    summaries = [
        EpisodeSummary(
            id=e.id,
            title=e.title,
            summary=e.summary,
            outcome=e.outcome,
            started_at=e.started_at,
            tags=e.tags or [],
            structured_summary=e.structured_summary,
        )
        for e in episodes
    ]
    return summaries, total
```

- [ ] **Step 4: Update Heart delegation**

In `nous/heart/heart.py`, add:

```python
async def list_episodes_paginated(
    self,
    limit: int = 50,
    offset: int = 0,
    outcome: str | None = None,
    frame: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "started_at",
    order: str = "desc",
    session: AsyncSession | None = None,
) -> tuple[list[EpisodeSummary], int]:
    """List episodes with pagination and filters (F021)."""
    return await self.episodes.list_all(
        limit, offset, outcome, frame, date_from, date_to, sort, order, session,
    )
```

- [ ] **Step 5: Update `/episodes` endpoint**

Replace `list_episodes` in `nous/api/rest.py:283-299`:

```python
async def list_episodes(request: Request) -> JSONResponse:
    """GET /episodes?limit=20&offset=0 - List episodes with filters."""
    try:
        limit = int(request.query_params.get("limit", "20"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)

    outcome = request.query_params.get("outcome")
    frame = request.query_params.get("frame")
    date_from = request.query_params.get("date_from")
    date_to = request.query_params.get("date_to")
    sort = request.query_params.get("sort", "started_at")
    order = request.query_params.get("order", "desc")

    try:
        episodes, total = await heart.list_episodes_paginated(
            limit=limit, offset=offset, outcome=outcome, frame=frame,
            date_from=date_from, date_to=date_to, sort=sort, order=order,
        )
        return JSONResponse({
            "episodes": [e.model_dump(mode="json") for e in episodes],
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    except Exception as e:
        logger.error("List episodes error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_rest_dashboard.py -k episodes -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add nous/heart/episodes.py nous/heart/heart.py nous/api/rest.py tests/test_rest_dashboard.py
git commit -m "feat(f021): extend /episodes with offset, filters, and total count"
```

---

### Task 3: Extend `/decisions` with filters

**Files:**
- Modify: `nous/api/rest.py:246-264`
- Modify: `nous/brain/brain.py:96-159` (add filter params to `_list_decisions`)
- Test: `tests/test_rest_dashboard.py`

- [ ] **Step 1: Write failing test**

```python
async def test_decisions_filter_by_category(client, brain, db):
    """GET /decisions?category=architecture filters by category."""
    async with db.session() as session:
        await brain.record(
            RecordInput(
                description="Architecture decision for dashboard test",
                confidence=0.85, category="architecture", stakes="medium",
                context="Testing", reasons=[ReasonInput(type="analysis", text="Test")],
            ),
            session=session,
        )
        await session.commit()

    resp = await client.get("/decisions?category=architecture&limit=50")
    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data
    for d in data["decisions"]:
        assert d["category"] == "architecture"


async def test_decisions_filter_by_stakes(client, brain, db):
    """GET /decisions?stakes=high filters by stakes level."""
    resp = await client.get("/decisions?stakes=high")
    assert resp.status_code == 200


async def test_decisions_filter_by_outcome(client, brain, db):
    """GET /decisions?outcome=success filters by outcome."""
    resp = await client.get("/decisions?outcome=success")
    assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_rest_dashboard.py -k "decisions_filter" -v`
Expected: FAIL — no filter params supported.

- [ ] **Step 3: Extend `_list_decisions` in Brain**

Modify `nous/brain/brain.py` — update `list_decisions` and `_list_decisions` to accept filter params.

**IMPORTANT:** The existing signature is `(self, limit, offset, agent_id, session)`. New filter params must be added AFTER `agent_id` but BEFORE `session` to preserve backward compatibility. Verify all callers use keyword args (rest.py does: `brain.list_decisions(limit=limit, offset=offset)`). The internal `_list_decisions` can have any signature since it's private.

```python
async def list_decisions(
    self,
    limit: int = 20,
    offset: int = 0,
    agent_id: str | None = None,
    category: str | None = None,
    stakes: str | None = None,
    outcome: str | None = None,
    confidence_min: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    reviewed: bool | None = None,
    sort: str = "created_at",
    order: str = "desc",
    session: AsyncSession | None = None,
) -> tuple[list[DecisionSummary], int]:
    """List decisions with optional filters. Returns (decisions, total_count)."""
    if session is None:
        async with self.db.session() as session:
            return await self._list_decisions(
                limit, offset, agent_id, category, stakes, outcome,
                confidence_min, date_from, date_to, reviewed, sort, order, session,
            )
    return await self._list_decisions(
        limit, offset, agent_id, category, stakes, outcome,
        confidence_min, date_from, date_to, reviewed, sort, order, session,
    )

async def _list_decisions(
    self,
    limit: int,
    offset: int,
    agent_id: str | None,
    category: str | None,
    stakes: str | None,
    outcome: str | None,
    confidence_min: float | None,
    date_from: str | None,
    date_to: str | None,
    reviewed: bool | None,
    sort: str,
    order: str,
    session: AsyncSession,
) -> tuple[list[DecisionSummary], int]:
    from sqlalchemy import func as sa_func

    _agent_id = agent_id or self.agent_id

    conditions = [Decision.agent_id == _agent_id]
    if category:
        conditions.append(Decision.category == category)
    if stakes:
        conditions.append(Decision.stakes == stakes)
    if outcome:
        conditions.append(Decision.outcome == outcome)
    if confidence_min is not None:
        conditions.append(Decision.confidence >= confidence_min)
    if date_from:
        conditions.append(Decision.created_at >= date_from)
    if date_to:
        conditions.append(Decision.created_at <= date_to)
    if reviewed is True:
        conditions.append(Decision.reviewed_at.isnot(None))
    elif reviewed is False:
        conditions.append(Decision.reviewed_at.is_(None))

    # Count
    count_q = select(sa_func.count()).select_from(Decision).where(*conditions)
    total = (await session.execute(count_q)).scalar() or 0

    # Sort — VALIDATE against allowlist to prevent attribute injection
    ALLOWED_SORTS = {"created_at", "confidence", "category", "stakes"}
    if sort not in ALLOWED_SORTS:
        sort = "created_at"
    if order not in ("asc", "desc"):
        order = "desc"
    sort_col = getattr(Decision, sort)
    order_clause = sort_col.desc() if order == "desc" else sort_col.asc()

    # Fetch
    result = await session.execute(
        select(Decision).where(*conditions).order_by(order_clause).limit(limit).offset(offset)
    )
    decisions = list(result.scalars().all())

    if not decisions:
        return [], total

    # Fetch tags (P2-17: separate query)
    decision_ids = [d.id for d in decisions]
    tag_result = await session.execute(select(DecisionTag).where(DecisionTag.decision_id.in_(decision_ids)))
    tags_by_id: dict[UUID, list[str]] = defaultdict(list)
    for t in tag_result.scalars().all():
        tags_by_id[t.decision_id].append(t.tag)

    summaries = [
        DecisionSummary(
            id=d.id,
            description=d.description,
            confidence=d.confidence,
            category=d.category,
            stakes=d.stakes,
            outcome=d.outcome or "pending",
            pattern=d.pattern,
            tags=tags_by_id.get(d.id, []),
            created_at=d.created_at,
        )
        for d in decisions
    ]
    return summaries, total
```

- [ ] **Step 4: Update `/decisions` endpoint in rest.py**

```python
async def list_decisions(request: Request) -> JSONResponse:
    """GET /decisions?limit=20&offset=0 - List decisions with filters."""
    try:
        limit = int(request.query_params.get("limit", "20"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)

    category = request.query_params.get("category")
    stakes = request.query_params.get("stakes")
    outcome = request.query_params.get("outcome")
    confidence_min_str = request.query_params.get("confidence_min")
    confidence_min = float(confidence_min_str) if confidence_min_str else None
    date_from = request.query_params.get("date_from")
    date_to = request.query_params.get("date_to")
    reviewed_param = request.query_params.get("reviewed")
    reviewed = {"true": True, "false": False}.get(reviewed_param) if reviewed_param else None
    sort = request.query_params.get("sort", "created_at")
    order = request.query_params.get("order", "desc")

    try:
        decisions, total = await brain.list_decisions(
            limit=limit, offset=offset, category=category, stakes=stakes,
            outcome=outcome, confidence_min=confidence_min,
            date_from=date_from, date_to=date_to, reviewed=reviewed,
            sort=sort, order=order,
        )
        return JSONResponse({
            "decisions": [d.model_dump(mode="json") for d in decisions],
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    except Exception as e:
        logger.error("List decisions error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_rest_dashboard.py -k "decisions_filter" -v`
Expected: PASS

- [ ] **Step 6: Run existing decision tests to verify backward compat**

Run: `uv run pytest tests/test_rest.py -k decisions -v`
Expected: PASS (no regressions)

- [ ] **Step 7: Commit**

```bash
git add nous/brain/brain.py nous/api/rest.py tests/test_rest_dashboard.py
git commit -m "feat(f021): extend /decisions with category, stakes, outcome, confidence filters"
```

---

### Task 4: Extend `/censors` with filters and pagination

**Files:**
- Modify: `nous/api/rest.py:323-334`
- Modify: `nous/heart/heart.py`
- Modify: `nous/heart/censors.py` (add `list_all` with pagination)
- Test: `tests/test_rest_dashboard.py`

- [ ] **Step 1: Write failing test**

```python
async def test_censors_with_pagination(client, heart, db):
    """GET /censors returns total and supports limit/offset."""
    async with db.session() as session:
        await heart.add_censor(
            CensorInput(trigger_pattern="test pattern", reason="Test", action="warn"),
            session=session,
        )
        await session.commit()

    resp = await client.get("/censors?limit=10&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert "censors" in data
    assert "total" in data


async def test_censors_filter_by_action(client):
    """GET /censors?action=block filters by action type."""
    resp = await client.get("/censors?action=block")
    assert resp.status_code == 200
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_rest_dashboard.py -k censors -v`
Expected: FAIL

- [ ] **Step 3: Add `list_all` to CensorManager**

In `nous/heart/censors.py`, add paginated list with filters:

```python
async def list_all(
    self,
    limit: int = 50,
    offset: int = 0,
    action: str | None = None,
    active_only: bool = True,
    domain: str | None = None,
    session: AsyncSession | None = None,
) -> tuple[list[CensorDetail], int]:
    """Paginated censor list with filters (F021)."""
    if session is None:
        async with self.db.session() as session:
            return await self._list_all(limit, offset, action, active_only, domain, session)
    return await self._list_all(limit, offset, action, active_only, domain, session)

async def _list_all(self, limit, offset, action, active_only, domain, session):
    from sqlalchemy import func as sa_func

    conditions = [Censor.agent_id == self.agent_id]
    if active_only:
        conditions.append(Censor.active == True)
    if action:
        conditions.append(Censor.action == action)
    if domain:
        conditions.append(Censor.domain == domain)

    count_q = select(sa_func.count()).select_from(Censor).where(*conditions)
    total = (await session.execute(count_q)).scalar() or 0

    q = (select(Censor).where(*conditions)
         .order_by(Censor.created_at.desc()).limit(limit).offset(offset))
    result = await session.execute(q)
    censors = list(result.scalars().all())

    details = [self._to_detail(c) for c in censors]
    return details, total
```

Note: Check `censors.py` for existing `_to_detail` method — reuse it.

- [ ] **Step 4: Update Heart and rest.py**

Heart delegation:
```python
async def list_censors_paginated(
    self, limit: int = 50, offset: int = 0,
    action: str | None = None, active_only: bool = True,
    domain: str | None = None, session: AsyncSession | None = None,
) -> tuple[list[CensorDetail], int]:
    """List censors with pagination and filters (F021)."""
    return await self.censors.list_all(limit, offset, action, active_only, domain, session)
```

Update endpoint:
```python
async def list_censors(request: Request) -> JSONResponse:
    """GET /censors - List censors with filters."""
    try:
        limit = int(request.query_params.get("limit", "50"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)

    action = request.query_params.get("action")
    active_param = request.query_params.get("active")
    active_only = active_param != "false" if active_param else True
    domain = request.query_params.get("domain")

    try:
        censors, total = await heart.list_censors_paginated(
            limit=limit, offset=offset, action=action,
            active_only=active_only, domain=domain,
        )
        return JSONResponse({
            "censors": [c.model_dump(mode="json") for c in censors],
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    except Exception as e:
        logger.error("List censors error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_rest_dashboard.py -k censors -v`
Expected: PASS

- [ ] **Step 6: Run existing censor tests**

Run: `uv run pytest tests/test_rest.py -k censors -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add nous/heart/censors.py nous/heart/heart.py nous/api/rest.py tests/test_rest_dashboard.py
git commit -m "feat(f021): extend /censors with pagination, action filter"
```

---

### Task 4b: Add `/procedures` endpoint (new)

**Files:**
- Modify: `nous/api/rest.py` (add new route)
- Modify: `nous/heart/heart.py` (add `list_procedures` delegation)
- Modify: `nous/heart/procedures.py` (add `list_all` method)
- Test: `tests/test_rest_dashboard.py`

The spec's Memory Browser has a Procedures tab but no REST endpoint exists.

- [ ] **Step 1: Write failing test**

```python
async def test_procedures_list(client, db):
    """GET /procedures returns paginated procedures."""
    resp = await client.get("/procedures?limit=10&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert "procedures" in data
    assert "total" in data
```

- [ ] **Step 2: Add `list_all` to ProcedureManager**

In `nous/heart/procedures.py`:

```python
async def list_all(
    self,
    limit: int = 50,
    offset: int = 0,
    domain: str | None = None,
    active_only: bool = True,
    min_activations: int | None = None,
    session: AsyncSession | None = None,
) -> tuple[list[ProcedureSummary], int]:
    """Paginated procedure list with filters (F021)."""
    if session is None:
        async with self.db.session() as session:
            return await self._list_all(limit, offset, domain, active_only, min_activations, session)
    return await self._list_all(limit, offset, domain, active_only, min_activations, session)

async def _list_all(self, limit, offset, domain, active_only, min_activations, session):
    from sqlalchemy import func as sa_func
    conditions = [Procedure.agent_id == self.agent_id]
    if active_only:
        conditions.append(Procedure.active == True)
    if domain:
        conditions.append(Procedure.domain == domain)
    if min_activations is not None:
        conditions.append(Procedure.activation_count >= min_activations)

    count_q = select(sa_func.count()).select_from(Procedure).where(*conditions)
    total = (await session.execute(count_q)).scalar() or 0

    q = (select(Procedure).where(*conditions)
         .order_by(Procedure.created_at.desc()).limit(limit).offset(offset))
    result = await session.execute(q)
    procs = list(result.scalars().all())

    # ProcedureSummary fields (schemas.py:198-207):
    #   id, name, domain, description, activation_count, effectiveness, score
    # NOTE: No success_count/failure_count fields. Compute effectiveness from counts.
    summaries = [
        ProcedureSummary(
            id=p.id, name=p.name, domain=p.domain,
            description=p.description,
            activation_count=p.activation_count or 0,
            effectiveness=(
                (p.success_count or 0) / max(p.activation_count or 1, 1)
                if p.activation_count else None
            ),
        )
        for p in procs
    ]
    return summaries, total
```

- [ ] **Step 3: Add Heart delegation + REST endpoint + route**

Heart: `list_procedures(...)` → `self.procedures.list_all(...)`

REST:
```python
async def list_procedures(request: Request) -> JSONResponse:
    """GET /procedures - List procedures with filters."""
    try:
        limit = int(request.query_params.get("limit", "50"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)
    domain = request.query_params.get("domain")
    active_param = request.query_params.get("active")
    active_only = active_param != "false" if active_param else True
    try:
        procs, total = await heart.list_procedures(limit=limit, offset=offset, domain=domain, active_only=active_only)
        return JSONResponse({"procedures": [p.model_dump(mode="json") for p in procs], "total": total, "limit": limit, "offset": offset})
    except Exception as e:
        logger.error("List procedures error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
```

Route: `Route("/procedures", list_procedures),`

- [ ] **Step 4: Run test, commit**

```bash
git add nous/heart/procedures.py nous/heart/heart.py nous/api/rest.py tests/test_rest_dashboard.py
git commit -m "feat(f021): add /procedures endpoint for browser tab"
```

---

### Task 5: Extend `/status` with `?dashboard=true`

**Files:**
- Modify: `nous/api/rest.py:164-244`
- Create: `nous/api/dashboard_queries.py` (start with `get_dashboard_stats`)
- Test: `tests/test_rest_dashboard.py`

- [ ] **Step 1: Write failing test**

```python
async def test_status_dashboard_mode(client, heart, brain, db):
    """GET /status?dashboard=true returns extended data."""
    async with db.session() as session:
        await heart.learn(
            FactInput(content="Fact for dashboard stats", category="technical", confidence=0.9),
            session=session,
        )
        await brain.record(
            RecordInput(
                description="Decision for dashboard stats",
                confidence=0.85, category="architecture", stakes="medium",
                context="Testing", reasons=[ReasonInput(type="analysis", text="Test")],
            ),
            session=session,
        )
        await session.commit()

    resp = await client.get("/status?dashboard=true")
    assert resp.status_code == 200
    data = resp.json()
    # Standard fields still present
    assert "agent_id" in data
    assert "memory" in data
    # Dashboard-specific fields
    assert "dashboard" in data
    dash = data["dashboard"]
    assert "deltas_7d" in dash
    assert "distributions" in dash
    assert "timeseries" in dash
    assert "graph_density" in dash


async def test_status_without_dashboard_flag(client):
    """GET /status without dashboard=true returns normal response."""
    resp = await client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "dashboard" not in data
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_rest_dashboard.py -k status -v`
Expected: FAIL

- [ ] **Step 3: Create `dashboard_queries.py`**

```python
# nous/api/dashboard_queries.py
"""SQL query functions for F021 Memory Dashboard.

All functions take an AsyncSession and agent_id, returning dicts
ready for JSON serialization. No ORM session management here —
callers handle session lifecycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.spreading_activation import compute_graph_density

logger = logging.getLogger(__name__)


async def get_dashboard_stats(
    session: AsyncSession, agent_id: str, days: int = 30,
) -> dict:
    """Aggregate stats for /status?dashboard=true."""
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    start_date = now - timedelta(days=days)

    # 7-day deltas
    deltas = {}
    for table, key, date_col in [
        ("heart.facts", "facts", "created_at"),
        ("heart.episodes", "episodes", "created_at"),
        ("brain.decisions", "decisions", "created_at"),
    ]:
        result = await session.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE agent_id = :agent_id AND {date_col} >= :since"),
            {"agent_id": agent_id, "since": seven_days_ago},
        )
        deltas[key] = result.scalar() or 0

    # Distributions
    # Fact categories
    fact_cats = await session.execute(
        text("""
            SELECT COALESCE(category, 'uncategorized') AS cat, COUNT(*) AS cnt
            FROM heart.facts WHERE agent_id = :agent_id AND active = true
            GROUP BY category
        """),
        {"agent_id": agent_id},
    )
    fact_categories = {row.cat: row.cnt for row in fact_cats}

    # Decision outcomes
    dec_outcomes = await session.execute(
        text("""
            SELECT COALESCE(outcome, 'pending') AS out, COUNT(*) AS cnt
            FROM brain.decisions WHERE agent_id = :agent_id
            GROUP BY outcome
        """),
        {"agent_id": agent_id},
    )
    decision_outcomes = {row.out: row.cnt for row in dec_outcomes}

    # Decision categories
    dec_cats = await session.execute(
        text("""
            SELECT category, COUNT(*) AS cnt
            FROM brain.decisions WHERE agent_id = :agent_id
            GROUP BY category
        """),
        {"agent_id": agent_id},
    )
    decision_categories = {row.category: row.cnt for row in dec_cats}

    # Edge relations
    edge_rels = await session.execute(
        text("""
            SELECT relation, COUNT(*) AS cnt
            FROM brain.graph_edges WHERE agent_id = :agent_id
            GROUP BY relation ORDER BY cnt DESC
        """),
        {"agent_id": agent_id},
    )
    edge_relations = {row.relation: row.cnt for row in edge_rels}

    # Timeseries (last N days)
    timeseries = await _get_timeseries(session, agent_id, start_date, now)

    # Graph density
    density = await compute_graph_density(session, agent_id)

    return {
        "deltas_7d": deltas,
        "distributions": {
            "fact_categories": fact_categories,
            "decision_outcomes": decision_outcomes,
            "decision_categories": decision_categories,
            "edge_relations": edge_relations,
        },
        "timeseries": timeseries,
        "graph_density": round(density, 2),
    }


async def _get_timeseries(
    session: AsyncSession, agent_id: str, start: datetime, end: datetime,
) -> dict:
    """Daily counts of facts, episodes, decisions for chart."""
    labels = []
    facts_data = []
    episodes_data = []
    decisions_data = []

    # Generate date labels
    current = start.date()
    end_date = end.date()
    while current <= end_date:
        labels.append(current.isoformat())
        current += timedelta(days=1)

    for table, data_list, date_col in [
        ("heart.facts", facts_data, "created_at"),
        ("heart.episodes", episodes_data, "created_at"),
        ("brain.decisions", decisions_data, "created_at"),
    ]:
        result = await session.execute(
            text(f"""
                SELECT ({date_col} AT TIME ZONE 'UTC')::date AS day, COUNT(*) AS cnt
                FROM {table}
                WHERE agent_id = :agent_id AND {date_col} >= :start AND {date_col} <= :end
                GROUP BY day ORDER BY day
            """),
            {"agent_id": agent_id, "start": start, "end": end},
        )
        day_counts = {row.day.isoformat(): row.cnt for row in result}
        data_list.extend(day_counts.get(label, 0) for label in labels)

    return {
        "labels": labels,
        "facts": facts_data,
        "episodes": episodes_data,
        "decisions": decisions_data,
    }
```

- [ ] **Step 4: Update `/status` endpoint to support `?dashboard=true`**

In `nous/api/rest.py`, modify the `status` function. After the existing response dict is built, check for dashboard flag:

```python
# At the end of the status function, before return:
response_data = {
    "agent_id": settings.agent_id,
    # ... existing fields ...
}

# F021: Dashboard extension
if request.query_params.get("dashboard") == "true":
    from nous.api.dashboard_queries import get_dashboard_stats
    async with database.session() as dash_session:
        response_data["dashboard"] = await get_dashboard_stats(
            dash_session, settings.agent_id,
        )

return JSONResponse(response_data)
```

Note: Use a separate session for dashboard queries since the existing session block is already closed by this point.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_rest_dashboard.py -k status -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nous/api/dashboard_queries.py nous/api/rest.py tests/test_rest_dashboard.py
git commit -m "feat(f021): extend /status with ?dashboard=true for trends and distributions"
```

---

## Chunk 2: Backend — New Dashboard Endpoints

### Task 6: Graph endpoint (`/dashboard/graph`)

**Files:**
- Modify: `nous/api/dashboard_queries.py` (add `get_graph_data`)
- Modify: `nous/api/rest.py` (add route)
- Test: `tests/test_dashboard_queries.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_dashboard_queries.py
"""Unit tests for dashboard query functions."""

import pytest
import pytest_asyncio

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.heart import FactInput


@pytest_asyncio.fixture
async def brain(db, settings):
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


async def test_get_graph_data_empty(db, settings):
    """Graph data returns empty nodes/edges with zero stats."""
    from nous.api.dashboard_queries import get_graph_data

    async with db.session() as session:
        data = await get_graph_data(session, settings.agent_id)

    assert "nodes" in data
    assert "edges" in data
    assert "stats" in data
    assert data["stats"]["total_nodes"] == 0
    assert data["stats"]["total_edges"] == 0


async def test_get_graph_data_with_edges(db, settings, brain, heart):
    """Graph data returns nodes and edges when data exists."""
    from nous.api.dashboard_queries import get_graph_data

    async with db.session() as session:
        # Create a fact and decision to have nodes
        fact = await heart.learn(
            FactInput(content="Graph test fact", category="technical", confidence=0.9),
            session=session,
        )
        decision = await brain.record(
            RecordInput(
                description="Graph test decision",
                confidence=0.85, category="architecture", stakes="medium",
                context="Testing", reasons=[ReasonInput(type="analysis", text="Test")],
            ),
            session=session,
        )
        # Manually insert an edge
        from sqlalchemy import text
        await session.execute(
            text("""
                INSERT INTO brain.graph_edges (source_id, target_id, source_type, target_type, agent_id, relation, weight, auto_linked)
                VALUES (:src, :tgt, 'fact', 'decision', :agent_id, 'extracted_from', 1.0, true)
            """),
            {"src": str(fact.id), "tgt": str(decision.id), "agent_id": settings.agent_id},
        )
        await session.commit()

        data = await get_graph_data(session, settings.agent_id)

    assert data["stats"]["total_edges"] >= 1
    assert len(data["edges"]) >= 1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_dashboard_queries.py::test_get_graph_data_empty -v`
Expected: FAIL — function doesn't exist yet.

- [ ] **Step 3: Implement `get_graph_data`**

Add to `nous/api/dashboard_queries.py`:

```python
async def get_graph_data(
    session: AsyncSession,
    agent_id: str,
    limit: int = 500,
    types: list[str] | None = None,
    min_edges: int = 0,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Build nodes + edges for D3 graph visualization."""
    type_filter = types or ["fact", "episode", "decision", "procedure"]

    # Get all edges for this agent
    edge_conditions = "agent_id = :agent_id"
    params: dict = {"agent_id": agent_id}

    if date_from:
        edge_conditions += " AND created_at >= :date_from"
        params["date_from"] = date_from
    if date_to:
        edge_conditions += " AND created_at <= :date_to"
        params["date_to"] = date_to

    # Limit edge fetching to prevent loading entire table into memory.
    # Fetch edges involving the most-connected nodes first.
    max_edges = limit * 4  # heuristic: ~4 edges per displayed node
    edges_result = await session.execute(
        text(f"""
            SELECT id, source_id, target_id, source_type, target_type,
                   relation, weight, auto_linked, created_at
            FROM brain.graph_edges
            WHERE {edge_conditions}
            ORDER BY created_at DESC
            LIMIT :max_edges
        """),
        {**params, "max_edges": max_edges},
    )
    edges_raw = list(edges_result)

    # Collect unique node IDs with their types and edge counts
    node_edge_counts: dict[str, int] = {}
    node_types: dict[str, str] = {}
    for e in edges_raw:
        src = str(e.source_id)
        tgt = str(e.target_id)
        node_types[src] = e.source_type
        node_types[tgt] = e.target_type
        node_edge_counts[src] = node_edge_counts.get(src, 0) + 1
        node_edge_counts[tgt] = node_edge_counts.get(tgt, 0) + 1

    # Filter by type and min_edges
    valid_nodes = {
        nid for nid, ntype in node_types.items()
        if ntype in type_filter and node_edge_counts.get(nid, 0) >= min_edges
    }

    # Fetch labels for nodes (truncated to 120 chars)
    nodes = []
    for ntype in type_filter:
        nids = [nid for nid in valid_nodes if node_types.get(nid) == ntype]
        if not nids:
            continue

        # NOTE: Not all tables have a `category` column:
        # - facts and decisions have `category`
        # - procedures have `domain` (not category)
        # - episodes have NO category column at all
        # The query must be type-aware.
        if ntype == "fact":
            table, label_col, cat_expr = "heart.facts", "content", "COALESCE(category, '')"
        elif ntype == "episode":
            table, label_col, cat_expr = "heart.episodes", "title", "COALESCE(frame_used, '')"
        elif ntype == "decision":
            table, label_col, cat_expr = "brain.decisions", "description", "COALESCE(category, '')"
        elif ntype == "procedure":
            table, label_col, cat_expr = "heart.procedures", "name", "COALESCE(domain, '')"
        else:
            continue

        # Fetch in batches to avoid parameter limit issues
        for i in range(0, len(nids), 100):
            batch = nids[i:i+100]
            placeholders = ", ".join(f":id_{j}" for j in range(len(batch)))
            batch_params = {f"id_{j}": uid for j, uid in enumerate(batch)}
            result = await session.execute(
                text(f"""
                    SELECT id, LEFT({label_col}, 120) AS label,
                           {cat_expr} AS category, created_at
                    FROM {table}
                    WHERE id IN ({placeholders})
                """),
                batch_params,
            )
            for row in result:
                nodes.append({
                    "id": str(row.id),
                    "type": ntype,
                    "label": row.label or "(untitled)",
                    "category": row.category,
                    "edge_count": node_edge_counts.get(str(row.id), 0),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                })

    # Sort by edge count desc, apply limit
    nodes.sort(key=lambda n: n["edge_count"], reverse=True)
    if len(nodes) > limit:
        kept_ids = {n["id"] for n in nodes[:limit]}
        nodes = nodes[:limit]
    else:
        kept_ids = {n["id"] for n in nodes}

    # Filter edges to only include kept nodes
    edges = [
        {
            "source": str(e.source_id),
            "target": str(e.target_id),
            "relation": e.relation,
            "weight": e.weight or 1.0,
            "auto_linked": e.auto_linked or False,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in edges_raw
        if str(e.source_id) in kept_ids and str(e.target_id) in kept_ids
    ]

    # Stats
    all_node_ids = set(node_types.keys())
    density = await compute_graph_density(session, agent_id)

    # Orphan count: nodes in source tables that have NO edges at all.
    # all_node_ids only contains nodes FROM edges, so we need a separate query.
    orphan_result = await session.execute(
        text("""
            WITH all_items AS (
                SELECT id FROM heart.facts WHERE agent_id = :agent_id AND active = true
                UNION ALL
                SELECT id FROM heart.episodes WHERE agent_id = :agent_id
                UNION ALL
                SELECT id FROM brain.decisions WHERE agent_id = :agent_id
                UNION ALL
                SELECT id FROM heart.procedures WHERE agent_id = :agent_id AND active = true
            ),
            connected AS (
                SELECT DISTINCT node_id FROM (
                    SELECT source_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                    UNION
                    SELECT target_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                ) c
            )
            SELECT COUNT(*) FROM all_items WHERE id NOT IN (SELECT node_id FROM connected)
        """),
        {"agent_id": agent_id},
    )
    orphan_count = orphan_result.scalar() or 0
    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "total_nodes": len(all_node_ids),
            "total_edges": len(edges_raw),
            "displayed_nodes": len(nodes),
            "density": round(density, 2),
            "largest_cluster": 0,  # TODO: connected components if needed
            "orphan_count": orphan_count,
        },
    }
```

- [ ] **Step 4: Add route to rest.py**

```python
async def dashboard_graph(request: Request) -> JSONResponse:
    """GET /dashboard/graph - Graph data for D3 visualization."""
    try:
        limit = int(request.query_params.get("limit", "500"))
    except ValueError:
        return JSONResponse({"error": "limit must be integer"}, status_code=400)

    types_param = request.query_params.get("types")
    types = types_param.split(",") if types_param else None
    min_edges = int(request.query_params.get("min_edges", "0"))
    date_from = request.query_params.get("date_from")
    date_to = request.query_params.get("date_to")

    try:
        from nous.api.dashboard_queries import get_graph_data
        async with database.session() as session:
            data = await get_graph_data(
                session, settings.agent_id, limit=limit,
                types=types, min_edges=min_edges,
                date_from=date_from, date_to=date_to,
            )
        return JSONResponse(data)
    except Exception as e:
        logger.error("Dashboard graph error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
```

Add to routes list (before the static mount that will come later):
```python
Route("/dashboard/graph", dashboard_graph),
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_dashboard_queries.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nous/api/dashboard_queries.py nous/api/rest.py tests/test_dashboard_queries.py
git commit -m "feat(f021): add /dashboard/graph endpoint for D3 visualization"
```

---

### Task 7: Calibration endpoint (`/dashboard/calibration`)

**Files:**
- Modify: `nous/api/dashboard_queries.py`
- Modify: `nous/api/rest.py`
- Test: `tests/test_dashboard_queries.py`

- [ ] **Step 1: Write failing test**

```python
async def test_get_calibration_data_empty(db, settings):
    """Calibration data returns empty arrays when no decisions."""
    from nous.api.dashboard_queries import get_calibration_data

    async with db.session() as session:
        data = await get_calibration_data(session, settings.agent_id)

    assert "calibration_curve" in data
    assert "confidence_histogram" in data
    assert "outcome_by_category" in data
    assert "outcome_by_stakes" in data
    assert "reason_type_stats" in data
    assert "brier_history" in data
    assert "daily_decisions" in data


async def test_get_calibration_data_with_decisions(db, settings, brain):
    """Calibration data reflects actual decision data."""
    from nous.api.dashboard_queries import get_calibration_data

    async with db.session() as session:
        await brain.record(
            RecordInput(
                description="Calibration test decision",
                confidence=0.85, category="architecture", stakes="medium",
                context="Testing", reasons=[ReasonInput(type="analysis", text="Test")],
            ),
            session=session,
        )
        await session.commit()

        data = await get_calibration_data(session, settings.agent_id)

    assert data["daily_decisions"]  # at least one entry
    assert data["outcome_by_category"]  # at least architecture
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_dashboard_queries.py -k calibration -v`
Expected: FAIL

- [ ] **Step 3: Implement `get_calibration_data`**

Add to `nous/api/dashboard_queries.py`:

```python
async def get_calibration_data(
    session: AsyncSession, agent_id: str, days: int = 30,
) -> dict:
    """Decision intelligence analytics for /dashboard/calibration."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    # Calibration curve (bucketed by confidence)
    cal_result = await session.execute(
        text("""
            SELECT
                FLOOR(confidence * 5) / 5 AS bucket_start,
                AVG(confidence) AS predicted_avg,
                AVG(CASE WHEN outcome = 'success' THEN 1.0 ELSE 0.0 END) AS actual_success_rate,
                COUNT(*) AS count
            FROM brain.decisions
            WHERE agent_id = :agent_id AND outcome IS NOT NULL AND outcome != 'pending'
            GROUP BY bucket_start ORDER BY bucket_start
        """),
        {"agent_id": agent_id},
    )
    calibration_curve = [
        {
            "bucket": f"{row.bucket_start:.1f}-{row.bucket_start + 0.2:.1f}",
            "predicted_avg": round(float(row.predicted_avg), 3),
            "actual_success_rate": round(float(row.actual_success_rate), 3),
            "count": row.count,
        }
        for row in cal_result
    ]

    # Confidence histogram
    hist_result = await session.execute(
        text("""
            SELECT FLOOR(confidence * 10) / 10 AS range_start, COUNT(*) AS count
            FROM brain.decisions WHERE agent_id = :agent_id
            GROUP BY range_start ORDER BY range_start
        """),
        {"agent_id": agent_id},
    )
    confidence_histogram = [
        {"range": f"{row.range_start:.1f}-{row.range_start + 0.1:.1f}", "count": row.count}
        for row in hist_result
    ]

    # Outcome by category
    obc_result = await session.execute(
        text("""
            SELECT category, COALESCE(outcome, 'pending') AS outcome, COUNT(*) AS cnt
            FROM brain.decisions WHERE agent_id = :agent_id
            GROUP BY category, outcome
        """),
        {"agent_id": agent_id},
    )
    outcome_by_category: dict[str, dict[str, int]] = {}
    for row in obc_result:
        outcome_by_category.setdefault(row.category, {})[row.outcome] = row.cnt

    # Outcome by stakes
    obs_result = await session.execute(
        text("""
            SELECT stakes, COALESCE(outcome, 'pending') AS outcome, COUNT(*) AS cnt
            FROM brain.decisions WHERE agent_id = :agent_id
            GROUP BY stakes, outcome
        """),
        {"agent_id": agent_id},
    )
    outcome_by_stakes: dict[str, dict[str, int]] = {}
    for row in obs_result:
        outcome_by_stakes.setdefault(row.stakes, {})[row.outcome] = row.cnt

    # Reason type stats
    reason_result = await session.execute(
        text("""
            SELECT r.type,
                   COUNT(*) AS count,
                   AVG(CASE WHEN d.outcome = 'success' THEN 1.0 ELSE 0.0 END) AS success_rate
            FROM brain.decision_reasons r
            JOIN brain.decisions d ON r.decision_id = d.id
            WHERE d.agent_id = :agent_id AND d.outcome IS NOT NULL AND d.outcome != 'pending'
            GROUP BY r.type
        """),
        {"agent_id": agent_id},
    )
    reason_type_stats = {
        row.type: {"count": row.count, "success_rate": round(float(row.success_rate), 3)}
        for row in reason_result
    }

    # Brier history from calibration_snapshots
    brier_result = await session.execute(
        text("""
            SELECT snapshot_at, brier_score, accuracy
            FROM brain.calibration_snapshots
            WHERE agent_id = :agent_id
            ORDER BY snapshot_at
        """),
        {"agent_id": agent_id},
    )
    brier_history = [
        {
            "date": row.snapshot_at.isoformat() if row.snapshot_at else None,
            "brier_score": row.brier_score,
            "accuracy": row.accuracy,
        }
        for row in brier_result
    ]

    # Daily decisions (last N days)
    daily_result = await session.execute(
        text("""
            SELECT (created_at AT TIME ZONE 'UTC')::date AS day, COUNT(*) AS count
            FROM brain.decisions
            WHERE agent_id = :agent_id AND created_at >= :start
            GROUP BY day ORDER BY day
        """),
        {"agent_id": agent_id, "start": start},
    )
    daily_decisions = [
        {"date": row.day.isoformat(), "count": row.count}
        for row in daily_result
    ]

    return {
        "calibration_curve": calibration_curve,
        "confidence_histogram": confidence_histogram,
        "outcome_by_category": outcome_by_category,
        "outcome_by_stakes": outcome_by_stakes,
        "reason_type_stats": reason_type_stats,
        "brier_history": brier_history,
        "daily_decisions": daily_decisions,
    }
```

- [ ] **Step 4: Add route**

```python
async def dashboard_calibration(request: Request) -> JSONResponse:
    """GET /dashboard/calibration - Decision intelligence analytics."""
    days = int(request.query_params.get("days", "30"))
    try:
        from nous.api.dashboard_queries import get_calibration_data
        async with database.session() as session:
            data = await get_calibration_data(session, settings.agent_id, days=days)
        return JSONResponse(data)
    except Exception as e:
        logger.error("Dashboard calibration error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
```

Add route: `Route("/dashboard/calibration", dashboard_calibration),`

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_dashboard_queries.py -k calibration -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nous/api/dashboard_queries.py nous/api/rest.py tests/test_dashboard_queries.py
git commit -m "feat(f021): add /dashboard/calibration endpoint for decision analytics"
```

---

### Task 8: Activity endpoint (`/dashboard/activity`)

**Files:**
- Modify: `nous/api/dashboard_queries.py`
- Modify: `nous/api/rest.py`
- Test: `tests/test_dashboard_queries.py`

- [ ] **Step 1: Write failing test**

```python
async def test_get_activity_data(db, settings):
    """Activity data returns events and stats."""
    from nous.api.dashboard_queries import get_activity_data

    async with db.session() as session:
        data = await get_activity_data(session, settings.agent_id)

    assert "censor_stats" in data
    assert "schedule_stats" in data
    assert "sleep_stats" in data
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_dashboard_queries.py -k activity -v`

- [ ] **Step 3: Implement `get_activity_data`**

Add to `nous/api/dashboard_queries.py`:

```python
async def get_activity_data(
    session: AsyncSession, agent_id: str, hours: int = 168,
) -> dict:
    """System activity data for /dashboard/activity."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Events timeline from nous_system.events table
    events_result = await session.execute(
        text("""
            SELECT id, event_type, data, created_at
            FROM nous_system.events
            WHERE agent_id = :agent_id AND created_at >= :since
            ORDER BY created_at DESC LIMIT 100
        """),
        {"agent_id": agent_id, "since": since},
    )
    events = [
        {
            "type": row.event_type,
            "data": row.data,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in events_result
    ]

    # Censor stats (7-day window via events, not all-time from censor table)
    censor_activations = await session.execute(
        text("""
            SELECT id, trigger_pattern, activation_count, false_positive_count, created_by
            FROM heart.censors WHERE agent_id = :agent_id AND active = true
            ORDER BY activation_count DESC LIMIT 10
        """),
        {"agent_id": agent_id},
    )
    censors = list(censor_activations)

    auto_count = sum(1 for c in censors if c.created_by and c.created_by.startswith("auto"))
    manual_count = sum(1 for c in censors if not c.created_by or c.created_by == "manual")
    total_activations = sum(c.activation_count or 0 for c in censors)
    total_fp = sum(c.false_positive_count or 0 for c in censors)

    # Schedule stats
    schedules = await session.execute(
        text("""
            SELECT id, task, next_fire_at, fire_count, active
            FROM heart.schedules WHERE agent_id = :agent_id AND active = true
            ORDER BY next_fire_at NULLS LAST
        """),
        {"agent_id": agent_id},
    )
    schedule_list = list(schedules)

    # Sleep stats — check episodes with frame_used = 'sleep' or trigger containing 'sleep'
    sleep_result = await session.execute(
        text("""
            SELECT started_at FROM heart.episodes
            WHERE agent_id = :agent_id
              AND (frame_used = 'sleep' OR trigger ILIKE '%sleep%')
            ORDER BY started_at DESC LIMIT 1
        """),
        {"agent_id": agent_id},
    )
    last_sleep_row = sleep_result.first()

    return {
        "events": events,
        "censor_stats": {
            "total_activations": total_activations,
            "top_censors": [
                {
                    "id": str(c.id),
                    "trigger_pattern": c.trigger_pattern[:100],
                    "activations": c.activation_count or 0,
                }
                for c in censors[:5]
            ],
            "auto_created": auto_count,
            "manual_created": manual_count,
            "false_positives": total_fp,
        },
        "schedule_stats": {
            "active": len(schedule_list),
            "next_fires": [
                {
                    "id": str(s.id),
                    "task": s.task[:100] if s.task else "",
                    "next_fire_at": s.next_fire_at.isoformat() if s.next_fire_at else None,
                }
                for s in schedule_list[:5]
            ],
        },
        "sleep_stats": {
            "last_sleep": last_sleep_row.started_at.isoformat() if last_sleep_row else None,
        },
    }
```

- [ ] **Step 4: Add route**

```python
async def dashboard_activity(request: Request) -> JSONResponse:
    """GET /dashboard/activity - System activity timeline."""
    hours = int(request.query_params.get("hours", "168"))
    try:
        from nous.api.dashboard_queries import get_activity_data
        async with database.session() as session:
            data = await get_activity_data(session, settings.agent_id, hours=hours)
        return JSONResponse(data)
    except Exception as e:
        logger.error("Dashboard activity error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
```

Add route: `Route("/dashboard/activity", dashboard_activity),`

- [ ] **Step 5: Run tests, commit**

Run: `uv run pytest tests/test_dashboard_queries.py -k activity -v`

```bash
git add nous/api/dashboard_queries.py nous/api/rest.py tests/test_dashboard_queries.py
git commit -m "feat(f021): add /dashboard/activity endpoint for system events"
```

---

### Task 9: Health endpoint (`/dashboard/health`) + DB migration

**Files:**
- Modify: `nous/api/dashboard_queries.py`
- Modify: `nous/api/rest.py`
- Create: `nous/sql/migrations/017-dashboard-index.sql`
- Test: `tests/test_dashboard_queries.py`

- [ ] **Step 1: Write failing test**

```python
async def test_get_health_data(db, settings):
    """Health data returns graph trend arrays."""
    from nous.api.dashboard_queries import get_health_data

    async with db.session() as session:
        data = await get_health_data(session, settings.agent_id)

    assert "density_history" in data
    assert "daily_edges" in data
    assert "degree_distribution" in data
    assert "orphan_trend" in data
```

- [ ] **Step 2: Run to verify failure**

- [ ] **Step 3: Create migration file AND update init.sql**

Migration for existing deployments:
```sql
-- nous/sql/migrations/017-dashboard-index.sql
-- F021: Index for dashboard time-series queries on graph edges
CREATE INDEX IF NOT EXISTS idx_graph_edges_created
ON brain.graph_edges(created_at);
```

Also add the same index to `nous/sql/init.sql` (in the brain indexes section) so fresh installs get it:
```sql
CREATE INDEX IF NOT EXISTS idx_graph_edges_created ON brain.graph_edges(created_at);
```

- [ ] **Step 4: Implement `get_health_data`**

Add to `nous/api/dashboard_queries.py`:

```python
async def get_health_data(
    session: AsyncSession, agent_id: str, days: int = 30,
) -> dict:
    """Graph health metrics for /dashboard/health."""
    start = datetime.now(timezone.utc) - timedelta(days=days)

    # Daily edge creation
    daily = await session.execute(
        text("""
            SELECT (created_at AT TIME ZONE 'UTC')::date AS day,
                   COUNT(*) AS total,
                   SUM(CASE WHEN auto_linked = true THEN 1 ELSE 0 END) AS auto,
                   SUM(CASE WHEN auto_linked = false OR auto_linked IS NULL THEN 1 ELSE 0 END) AS manual
            FROM brain.graph_edges
            WHERE agent_id = :agent_id AND created_at >= :start
            GROUP BY day ORDER BY day
        """),
        {"agent_id": agent_id, "start": start},
    )
    daily_edges = [
        {"date": row.day.isoformat(), "count": row.total, "auto": row.auto, "manual": row.manual}
        for row in daily
    ]

    # Degree distribution
    degree = await session.execute(
        text("""
            WITH node_degrees AS (
                SELECT node_id, COUNT(*) AS degree FROM (
                    SELECT source_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                    UNION ALL
                    SELECT target_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                ) edges GROUP BY node_id
            )
            SELECT degree, COUNT(*) AS count FROM node_degrees GROUP BY degree ORDER BY degree
        """),
        {"agent_id": agent_id},
    )
    degree_distribution = [{"degree": row.degree, "count": row.count} for row in degree]

    # Current density
    density = await compute_graph_density(session, agent_id)

    # Density history — approximate from daily edge counts (compute cumulative)
    density_history = [{"date": datetime.now(timezone.utc).date().isoformat(), "density": round(density, 2)}]

    # Orphan trend — nodes with 0 edges (approximate: total facts+episodes+decisions minus connected nodes)
    orphan_result = await session.execute(
        text("""
            WITH all_items AS (
                SELECT id FROM heart.facts WHERE agent_id = :agent_id AND active = true
                UNION ALL
                SELECT id FROM heart.episodes WHERE agent_id = :agent_id
                UNION ALL
                SELECT id FROM brain.decisions WHERE agent_id = :agent_id
                UNION ALL
                SELECT id FROM heart.procedures WHERE agent_id = :agent_id AND active = true
            ),
            connected AS (
                SELECT DISTINCT node_id FROM (
                    SELECT source_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                    UNION
                    SELECT target_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                ) c
            )
            SELECT COUNT(*) AS orphan_count FROM all_items
            WHERE id NOT IN (SELECT node_id FROM connected)
        """),
        {"agent_id": agent_id},
    )
    orphan_count = orphan_result.scalar() or 0
    orphan_trend = [{"date": datetime.now(timezone.utc).date().isoformat(), "count": orphan_count}]

    return {
        "density_history": density_history,
        "daily_edges": daily_edges,
        "degree_distribution": degree_distribution,
        "orphan_trend": orphan_trend,
    }
```

- [ ] **Step 5: Add route**

```python
async def dashboard_health(request: Request) -> JSONResponse:
    """GET /dashboard/health - Graph health trends."""
    days = int(request.query_params.get("days", "30"))
    try:
        from nous.api.dashboard_queries import get_health_data
        async with database.session() as session:
            data = await get_health_data(session, settings.agent_id, days=days)
        return JSONResponse(data)
    except Exception as e:
        logger.error("Dashboard health error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
```

Add route: `Route("/dashboard/health", dashboard_health),`

- [ ] **Step 6: Run tests, commit**

Run: `uv run pytest tests/test_dashboard_queries.py -v`

```bash
git add nous/api/dashboard_queries.py nous/api/rest.py nous/sql/migrations/017-dashboard-index.sql tests/test_dashboard_queries.py
git commit -m "feat(f021): add /dashboard/health endpoint + graph_edges created_at index"
```

---

### Task 10: Static file serving + dashboard route registration

**Files:**
- Modify: `nous/api/rest.py` (add StaticFiles mount, register all dashboard routes)
- Test: `tests/test_rest_dashboard.py`

- [ ] **Step 1: Write test**

```python
async def test_dashboard_graph_endpoint(client, db):
    """GET /dashboard/graph returns graph data."""
    resp = await client.get("/dashboard/graph?limit=100")
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data
    assert "edges" in data
    assert "stats" in data


async def test_dashboard_calibration_endpoint(client, db):
    """GET /dashboard/calibration returns calibration data."""
    resp = await client.get("/dashboard/calibration")
    assert resp.status_code == 200
    data = resp.json()
    assert "calibration_curve" in data


async def test_dashboard_activity_endpoint(client, db):
    """GET /dashboard/activity returns activity data."""
    resp = await client.get("/dashboard/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert "censor_stats" in data


async def test_dashboard_health_endpoint(client, db):
    """GET /dashboard/health returns health data."""
    resp = await client.get("/dashboard/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "density_history" in data
```

- [ ] **Step 2: Update rest.py route list**

Add imports and static mount to `nous/api/rest.py`:

```python
# At top of rest.py, update the import line to add Mount:
from starlette.routing import Mount, Route
# Add new imports:
from starlette.staticfiles import StaticFiles
import os
```

**CRITICAL: Route ordering.** Dashboard API routes (`/dashboard/graph`, etc.) MUST appear BEFORE the `Mount("/dashboard", StaticFiles(...))` in the routes list. `Mount` is a catch-all prefix match — if it comes first, it will swallow `/dashboard/graph` and try to serve it as a static file.

```python
# In the routes list:
routes = [
    # ... existing routes ...
    Route("/health", health),
    Route("/procedures", list_procedures),  # F021 new
    # Dashboard API endpoints (F021) — MUST be before static Mount
    Route("/dashboard/graph", dashboard_graph),
    Route("/dashboard/calibration", dashboard_calibration),
    Route("/dashboard/activity", dashboard_activity),
    Route("/dashboard/health", dashboard_health),
]

# Static dashboard mount — only add if directory exists (avoids crash during tests)
# MUST be LAST in routes list (catch-all for /dashboard/*)
dashboard_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static", "dashboard")
if os.path.isdir(dashboard_dir):
    routes.append(
        Mount("/dashboard", app=StaticFiles(directory=dashboard_dir, html=True)),
    )
```

Note: `html=True` serves `index.html` for `/dashboard`. Hash routing (`#/overview`, etc.) is client-side so all routes resolve to the same file.

- [ ] **Step 3: Run all dashboard tests**

Run: `uv run pytest tests/test_rest_dashboard.py tests/test_dashboard_queries.py -v`
Expected: All PASS

- [ ] **Step 4: Run existing tests to verify no regressions**

Run: `uv run pytest tests/test_rest.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add nous/api/rest.py tests/test_rest_dashboard.py
git commit -m "feat(f021): register dashboard API routes + static file mount"
```

---

## Chunk 3: Frontend — SPA Shell + All Views

### Task 11: Vendor libraries + SPA shell

**Files:**
- Create: `static/dashboard/lib/chart.min.js` (vendored)
- Create: `static/dashboard/lib/d3.min.js` (vendored)
- Create: `static/dashboard/index.html`
- Create: `static/dashboard/css/dashboard.css`
- Create: `static/dashboard/js/app.js`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p static/dashboard/{css,js,lib}
```

- [ ] **Step 2: Download vendored libraries**

```bash
# Chart.js v4 (latest stable)
curl -L "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js" -o static/dashboard/lib/chart.min.js

# D3.js v7 (latest stable)
curl -L "https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js" -o static/dashboard/lib/d3.min.js
```

- [ ] **Step 3: Create `index.html`**

Create `static/dashboard/index.html` — SPA shell with sidebar navigation and view containers. Reference the spec's navigation section (lines 599-619). Include:
- Dark theme meta tags
- Link to `css/dashboard.css`
- Script tags for vendored libs + all view JS files
- Sidebar with hash-based nav links
- `<main>` with view container divs (one per view, hidden by default)
- Each view div has id matching route: `view-overview`, `view-graph`, `view-browser`, `view-decisions`, `view-activity`, `view-health`

Key structure:
```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Nous Dashboard</title>
    <link rel="stylesheet" href="css/dashboard.css">
</head>
<body>
    <nav class="sidebar">
        <div class="sidebar-header">Nous Dashboard</div>
        <a href="#/overview" class="nav-link active" data-view="overview">Overview</a>
        <a href="#/graph" class="nav-link" data-view="graph">Knowledge Graph</a>
        <a href="#/browser" class="nav-link" data-view="browser">Memory Browser</a>
        <a href="#/decisions" class="nav-link" data-view="decisions">Decisions</a>
        <a href="#/activity" class="nav-link" data-view="activity">Activity</a>
        <a href="#/health" class="nav-link" data-view="health">Graph Health</a>
    </nav>
    <main class="content">
        <div id="view-overview" class="view"></div>
        <div id="view-graph" class="view"></div>
        <div id="view-browser" class="view"></div>
        <div id="view-decisions" class="view"></div>
        <div id="view-activity" class="view"></div>
        <div id="view-health" class="view"></div>
    </main>
    <script src="lib/chart.min.js"></script>
    <script src="lib/d3.min.js"></script>
    <script src="js/app.js"></script>
    <script src="js/overview.js"></script>
    <script src="js/graph.js"></script>
    <script src="js/browser.js"></script>
    <script src="js/decisions.js"></script>
    <script src="js/activity.js"></script>
    <script src="js/health.js"></script>
</body>
</html>
```

- [ ] **Step 4: Create `dashboard.css`**

Create `static/dashboard/css/dashboard.css` with the design system from the spec (lines 649-693). Include:
- CSS variables (`:root` block from spec)
- Sidebar styles (fixed left, collapsible)
- Content area (main with left margin)
- Stat card component
- Table styles
- Chart container styles
- Loading/error/empty states
- Responsive breakpoints
- Typography (Inter font via Google Fonts or system fonts)

- [ ] **Step 5: Create `app.js`**

Create `static/dashboard/js/app.js` with:
- `apiGet(path, retries)` — fetch wrapper with retry logic (from spec lines 633-645)
- Hash router — `hashchange` listener, `loadView(viewName)` function
- `showLoading(container)`, `showError(container, msg)`, `showEmpty(container, msg)` helpers
- View registry — each view module registers via `Dashboard.registerView(name, loadFn)`
- Init: load default view (`overview`) on DOMContentLoaded

Key structure:
```javascript
const Dashboard = {
    views: {},
    currentView: null,

    registerView(name, loadFn) {
        this.views[name] = { load: loadFn, loaded: false };
    },

    async loadView(name) {
        // Hide all views, show target, call load function
    },

    async apiGet(path, retries = 3) {
        // Fetch with retry from spec
    },

    showLoading(container) { /* skeleton placeholders */ },
    showError(container, msg) { /* red banner + retry */ },
    showEmpty(container, msg) { /* friendly empty state */ },
};
```

- [ ] **Step 6: Commit**

```bash
git add static/dashboard/
git commit -m "feat(f021): dashboard SPA shell — index.html, CSS, app.js, vendored libs"
```

---

### Task 12: Overview view

**Files:**
- Create: `static/dashboard/js/overview.js`

- [ ] **Step 1: Implement overview.js**

Create `static/dashboard/js/overview.js`:
- Register view with `Dashboard.registerView('overview', loadOverview)`
- `loadOverview()`:
  1. Call `Dashboard.apiGet('/status?dashboard=true')`
  2. Render stat cards grid (facts, episodes, decisions, censors, density, brier, procedures, schedules)
  3. Render mini charts using Chart.js:
     - Memory Growth (line chart from `timeseries`)
     - Fact Categories (doughnut from `distributions.fact_categories`)
     - Decision Outcomes (doughnut from `distributions.decision_outcomes`)
     - Edge Types (bar from `distributions.edge_relations`)
  4. Handle empty/error states

Each stat card shows: value, label, trend arrow (delta from `deltas_7d`).

- [ ] **Step 2: Test manually**

Run: `uv run python -m nous.main` and visit `http://localhost:8000/dashboard`
Verify: Overview loads with stat cards and charts (or empty state if no data).

- [ ] **Step 3: Commit**

```bash
git add static/dashboard/js/overview.js
git commit -m "feat(f021): overview view — stat cards + mini charts"
```

---

### Task 13: Knowledge Graph view

**Files:**
- Create: `static/dashboard/js/graph.js`

- [ ] **Step 1: Implement graph.js**

Create `static/dashboard/js/graph.js`:
- Register view with `Dashboard.registerView('graph', loadGraph)`
- `loadGraph()`:
  1. Call `Dashboard.apiGet('/dashboard/graph?limit=500')`
  2. Create D3 force-directed graph:
     - Nodes colored by type (use CSS vars: `--fact-color`, `--episode-color`, etc.)
     - Node size proportional to `edge_count`
     - Edges colored by relation type, solid (weight>0.7) vs dashed (weight<0.3)
     - Force simulation with collision, charge, center forces
  3. Interactive features:
     - Click node → show detail in sidebar panel (fetch from existing detail endpoints)
     - Drag to rearrange
     - Zoom/pan (D3 zoom behavior)
     - Search box → highlights matching nodes
     - Filter checkboxes by node type
     - Minimum edge count slider
  4. Stats overlay: total nodes, edges, density, orphan count
  5. Handle empty/error states

- [ ] **Step 2: Test manually**

Visit `http://localhost:8000/dashboard#/graph`
Verify: Graph renders with nodes and edges (or empty state).

- [ ] **Step 3: Commit**

```bash
git add static/dashboard/js/graph.js
git commit -m "feat(f021): knowledge graph view — D3 force-directed with filters"
```

---

### Task 14: Memory Browser view

**Files:**
- Create: `static/dashboard/js/browser.js`

- [ ] **Step 1: Implement browser.js**

Create `static/dashboard/js/browser.js`:
- Register view with `Dashboard.registerView('browser', loadBrowser)`
- Tabbed interface: Facts | Episodes | Decisions | Procedures | Censors
- Each tab:
  1. Search bar (optional, triggers `q` param for facts)
  2. Filter dropdowns/inputs matching spec (category, outcome, stakes, etc.)
  3. Paginated table with columns from spec
  4. Click row → expand to show full detail
  5. Pagination controls (Previous / Next / page indicator)
- Use `Dashboard.apiGet` to fetch from existing endpoints with filters
- Facts tab uses `/facts?limit=50&offset=0` (browse mode)
- Episodes tab uses `/episodes?limit=50&offset=0`
- Decisions tab uses `/decisions?limit=50&offset=0`
- Procedures: direct query via `/dashboard/activity` or new simple endpoint (check if procedures have a list endpoint, otherwise add one)
- Censors tab uses `/censors?limit=50&offset=0`

Note: Check if `/procedures` endpoint exists. If not, the implementer should add a simple one similar to `/censors`.

- [ ] **Step 2: Test manually**

Visit `http://localhost:8000/dashboard#/browser`
Verify: Tabbed interface works, pagination works, filters apply.

- [ ] **Step 3: Commit**

```bash
git add static/dashboard/js/browser.js
git commit -m "feat(f021): memory browser view — tabbed search/browse with pagination"
```

---

### Task 15: Decision Intelligence view

**Files:**
- Create: `static/dashboard/js/decisions.js`

- [ ] **Step 1: Implement decisions.js**

Create `static/dashboard/js/decisions.js`:
- Register view with `Dashboard.registerView('decisions', loadDecisions)`
- `loadDecisions()`:
  1. Call `Dashboard.apiGet('/dashboard/calibration')`
  2. Render charts using Chart.js:
     - Calibration Curve (line chart: predicted vs actual, diagonal reference line)
     - Confidence Distribution (bar chart / histogram)
     - Outcome by Category (stacked bar)
     - Outcome by Stakes (stacked bar)
     - Reason Type Usage (horizontal bar)
     - Brier Score Over Time (line chart from `brier_history`)
     - Decisions Per Day (bar chart from `daily_decisions`)
  3. Handle empty/error states

- [ ] **Step 2: Test manually, commit**

```bash
git add static/dashboard/js/decisions.js
git commit -m "feat(f021): decision intelligence view — calibration curves + analytics"
```

---

### Task 16: Activity + Health views

**Files:**
- Create: `static/dashboard/js/activity.js`
- Create: `static/dashboard/js/health.js`

- [ ] **Step 1: Implement activity.js**

Create `static/dashboard/js/activity.js`:
- Register view with `Dashboard.registerView('activity', loadActivity)`
- Fetch from `/dashboard/activity`
- Render:
  - Censor stat cards (total activations, top censors, auto vs manual, false positives)
  - Schedule status (active count, next fires list)
  - Sleep stats (last sleep timestamp)
- Handle empty/error states

- [ ] **Step 2: Implement health.js**

Create `static/dashboard/js/health.js`:
- Register view with `Dashboard.registerView('health', loadHealth)`
- Fetch from `/dashboard/health`
- Render Chart.js charts:
  - Graph Density Over Time (line, target line at 3.0)
  - Edge Creation Rate (bar, daily)
  - Node Degree Distribution (histogram)
  - Orphan Nodes Over Time (line)
  - Auto-linked vs Manual Edges (stacked bar from `daily_edges`)
- Handle empty/error states

- [ ] **Step 3: Test manually all views**

Visit each view and verify rendering:
- `http://localhost:8000/dashboard` (overview)
- `http://localhost:8000/dashboard#/graph`
- `http://localhost:8000/dashboard#/browser`
- `http://localhost:8000/dashboard#/decisions`
- `http://localhost:8000/dashboard#/activity`
- `http://localhost:8000/dashboard#/health`

- [ ] **Step 4: Commit**

```bash
git add static/dashboard/js/activity.js static/dashboard/js/health.js
git commit -m "feat(f021): activity timeline + graph health views"
```

---

### Task 17: Final integration + full test pass

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/ -v --timeout=60`
Expected: All tests pass, no regressions.

- [ ] **Step 2: Run dashboard-specific tests**

Run: `uv run pytest tests/test_rest_dashboard.py tests/test_dashboard_queries.py -v`
Expected: All PASS

- [ ] **Step 3: Final commit with any polish**

```bash
git add -A
git commit -m "feat(f021): F021 Memory Dashboard — complete implementation"
```

---

## Dependencies Between Tasks

```
Task 1 (facts browse) ──────┐
Task 2 (episodes filters) ───┤
Task 3 (decisions filters) ──┼──→ Task 5 (/status?dashboard) ──→ Task 10 (route registration)
Task 4 (censors pagination) ─┤                                         │
Task 4b (procedures list) ───┘                                         ▼
                                                                 Task 11 (SPA shell)
Task 6 (graph endpoint) ───┐                                          │
Task 7 (calibration) ──────┤                                          ▼
Task 8 (activity) ──────────┼──→ Task 10 (route registration)  Tasks 12-16 (views)
Task 9 (health + migration) ┘                                         │
                                                                       ▼
                                                              Task 17 (integration)
```

**Parallelization opportunities:**
- Tasks 1-4b are independent (different endpoints)
- Tasks 6-9 are independent (different query functions)
- Tasks 12-16 are independent (different JS files) but all depend on Task 11
- Task 5 depends on Tasks 1-4 conceptually but not code-wise
- Task 10 depends on Tasks 6-9 (routes to register)

**Suggested agent assignment for subagent-driven execution:**
- Agent A: Tasks 1-5 (existing endpoint extensions)
- Agent B: Tasks 6-9 (new dashboard query functions + endpoints)
- Agent C: Tasks 11-16 (frontend, sequential)
- Task 10 and 17: coordinator after A+B complete

## Deployment Notes

**Dockerfile must be updated** to copy static files. Add before `HEALTHCHECK`:
```dockerfile
COPY static/ static/
```
Without this, the dashboard will silently not load in Docker (the `os.path.isdir` guard prevents a crash but the mount won't exist).

**Static file path resolution:** The `os.path.dirname(__file__)` approach works for local dev but may not resolve correctly if the package is pip-installed to site-packages. For Docker deployments, use the `COPY static/ static/` approach above with `WORKDIR /app`.

**Vendored libraries** (Chart.js, D3.js) are one-time downloads committed to git. The `curl` commands in Task 11 only need to run once.

## Frontend Implementation Notes

**Chart.js dark theme:** Set global defaults in `app.js` before any chart creation:
```javascript
Chart.defaults.color = '#6b6b8a';       // muted text for labels/legends
Chart.defaults.borderColor = '#1e1e2e'; // border color for gridlines
```

**Chart.js instance lifecycle:** When navigating between views, existing Chart instances MUST be destroyed before recreating. Either:
- Track instances per view and call `chart.destroy()` on nav-away, or
- Use the `loaded` flag in `Dashboard.views` to skip re-init on revisit

**D3 force simulation performance:** For 500 nodes:
- Set `simulation.alphaDecay(0.02)` to limit iterations
- Call `simulation.stop()` when navigating away from graph view
- If >500 nodes needed in future, switch from SVG to Canvas rendering

**Calibration curve diagonal:** Chart.js has no built-in reference line. Use either:
- `chartjs-plugin-annotation` (vendor alongside chart.min.js), or
- Custom `afterDraw` plugin hook to draw the diagonal manually

**Script loading:** Do NOT use `type="module"` on script tags. All scripts share global scope so `Dashboard` is accessible from view files.

**Font strategy:** Use system font stack (no Google Fonts dependency):
```css
font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
```

**Heatmap (Reason Type x Outcome):** Chart.js has no native heatmap. Implement as a colored HTML table/grid using CSS background colors based on values, not as a Chart.js chart.

**Activity timeline rendering:** The `events` array from `/dashboard/activity` should be rendered as a styled reverse-chronological list with event-type icons, timestamps, and summary text. This is the primary component of the Activity view.

## Known Limitations (deferred to follow-up)

- `density_history` and `orphan_trend` in `/dashboard/health` return single-point snapshots, not true historical trends. Historical density tracking would require periodic snapshots (like `calibration_snapshots`). Add snapshot mechanism in follow-up.
- `largest_cluster` in graph stats is stubbed as 0. Full connected-component analysis is complex; add if users request it.
- No server-side caching on dashboard endpoints. Spec suggests 60s cache on overview, 5m on graph. Add if performance requires it.
- `sleep_stats` only returns `last_sleep` timestamp. Full sleep stats (`facts_created`, `procedures_created`, `censors_retired`) require additional event queries — add when sleep handler emits structured events.
