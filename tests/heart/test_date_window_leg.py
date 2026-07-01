"""F075 L3 Task 4: integration test for FactManager._date_window_leg.

Postgres-only: uses the pgvector <=> operator which is not available in SQLite.
Run with: NOUS_TEST_DB=postgres uv run pytest tests/heart/test_date_window_leg.py -v
"""
from __future__ import annotations

import datetime
from uuid import uuid4

import pytest
import pytest_asyncio

from nous.heart.date_window import DateWindow
from nous.storage.models import Fact

pytestmark = [pytest.mark.postgres_only, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fact_store(heart):
    """FactManager from the shared heart fixture (uses MockEmbeddingProvider)."""
    return heart.facts


@pytest_asyncio.fixture
async def seed_dated_facts(session, heart):
    """Insert three facts into the savepoint session and return their UUIDs.

    F_in:      event_date=2026-04-25, embedding identical to query text
    F_out:     event_date=2026-06-01  (out of the April window)
    F_undated: event_date=NULL        (excluded by IS NOT NULL filter)
    """
    agent_id = heart.facts.agent_id
    embeddings = heart.facts.embeddings

    # Use the EXACT query text for F_in so MockEmbeddingProvider returns
    # an identical vector → cosine similarity = 1.0 vs the query.
    query_text = "calibration work in late April"
    emb_in = await embeddings.embed(query_text)
    emb_out = await embeddings.embed("June deployment notes")
    emb_undated = await embeddings.embed("system architecture uses PostgreSQL")

    f_in = Fact(
        id=uuid4(),
        agent_id=agent_id,
        content=query_text,
        event_date=datetime.date(2026, 4, 25),
        embedding=emb_in,
        active=True,
        category="technical",
    )
    f_out = Fact(
        id=uuid4(),
        agent_id=agent_id,
        content="June deployment notes",
        event_date=datetime.date(2026, 6, 1),
        embedding=emb_out,
        active=True,
        category="technical",
    )
    f_undated = Fact(
        id=uuid4(),
        agent_id=agent_id,
        content="system architecture uses PostgreSQL",
        event_date=None,
        embedding=emb_undated,
        active=True,
        category="technical",
    )

    session.add(f_in)
    session.add(f_out)
    session.add(f_undated)
    await session.flush()

    return {"F_in": f_in.id, "F_out": f_out.id, "F_undated": f_undated.id}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_date_window_leg_returns_in_window_ranked(
    fact_store, seed_dated_facts, session
):
    """_date_window_leg returns only in-window active dated facts, cosine-ranked."""
    window = DateWindow(start=datetime.date(2026, 4, 20), end=datetime.date(2026, 4, 30))
    emb = await fact_store.embeddings.embed("calibration work in late April")
    leg = await fact_store._date_window_leg(session, emb, window, limit=15)
    ids = [row[0] for row in leg]
    assert seed_dated_facts["F_in"] in ids           # in window → included
    assert seed_dated_facts["F_out"] not in ids      # June → excluded
    assert seed_dated_facts["F_undated"] not in ids  # NULL event_date → excluded


# ---------------------------------------------------------------------------
# Task 5 fixtures + tests — fusion via _rrf_merge_n
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seed_rescue_case(session, heart):
    """Seed a scenario where vanilla search misses the gold fact but fusion rescues it.

    Strategy:
    - 6 crowder facts: embedding == query embedding (cosine = 1.0), no event_date.
      This pushes them all into vanilla top-6, leaving gold at rank 6 (outside limit=5).
    - 1 gold fact: embedding from different text (cosine ≈ 0), event_date in the window.
      The date-window leg returns gold at rank 0; RRF fusion brings it into fused top-5.
    - Both embedding AND content avoid FTS matches on the query so keyword results
      are empty — pure vector ranking, fully deterministic.

    Verified analytically:
    - Vanilla _rrf_merge (empty keyword): crowder ranks 0-4 score 0.0162..0.0155,
      crowder rank 5 scores 0.0153, gold at rank 6 scores 0.0152 → gold excluded.
    - Fused _rrf_merge_n (vanilla list ++ date-leg): gold and c0 both score 0.01591
      (tied), so all of {c0, gold, c1, c2, c3} appear in the fused top-5. ✓
    """
    agent_id = heart.facts.agent_id
    embeddings = heart.facts.embeddings

    query = "april window rescue query"
    q_emb = await embeddings.embed(query)
    gold_emb = await embeddings.embed("database initialization sequence")

    from nous.storage.models import Fact

    crowders = []
    for i in range(6):
        f = Fact(
            id=uuid4(),
            agent_id=agent_id,
            content=f"background heartbeat check step {i}",
            event_date=None,
            embedding=q_emb,
            active=True,
            category="technical",
        )
        session.add(f)
        crowders.append(f)

    gold = Fact(
        id=uuid4(),
        agent_id=agent_id,
        content="database initialization sequence",
        event_date=datetime.date(2026, 4, 25),
        embedding=gold_emb,
        active=True,
        category="technical",
    )
    session.add(gold)
    await session.flush()

    window = DateWindow(start=datetime.date(2026, 4, 20), end=datetime.date(2026, 4, 30))
    return {"query": query, "window": window, "gold": gold.id}


async def test_date_window_fusion_rescues_missed_fact(
    fact_store, seed_rescue_case, session
):
    """Fusion of the date-window leg rescues a gold fact crowded out of vanilla top-5.

    Passes session explicitly so that both search calls see the uncommitted seeded data.
    Asserts the rescue mechanism is genuinely exercised (gold NOT in vanilla, IN fused).
    """
    q = seed_rescue_case["query"]
    window = seed_rescue_case["window"]
    gold = seed_rescue_case["gold"]

    vanilla = await fact_store.search(q, limit=5, session=session)
    fused = await fact_store.search(q, limit=5, date_window=window, session=session)

    assert gold not in [f.id for f in vanilla], (
        "gold should be crowded out of vanilla top-5 by the 6 crowders with cosine=1.0"
    )
    assert gold in [f.id for f in fused], (
        "fusion of the date-window leg must lift gold into the fused top-5"
    )


async def test_no_window_is_unchanged(fact_store, seed_rescue_case, session):
    """date_window=None leaves results byte-identical to no date_window argument."""
    q = seed_rescue_case["query"]
    a = await fact_store.search(q, limit=5, session=session)
    b = await fact_store.search(q, limit=5, date_window=None, session=session)
    assert [f.id for f in a] == [f.id for f in b]
