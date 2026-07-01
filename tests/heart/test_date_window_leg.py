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
