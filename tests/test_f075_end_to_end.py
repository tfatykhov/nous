"""F075 end-to-end tests — live-path verification.

Two test categories:

1. **Schema / validator unit tests** — Pydantic-only, run on the default
   SQLite test harness. Cover the FactInput validator, FactSummary/FactDetail
   passthrough, and the value contract documented in the spec.

2. **Roundtrip integration tests** — require the Postgres + pgvector test
   harness (`conftest_postgres.py`). Currently skipped under the default
   SQLite conftest because Heart.learn's `_find_duplicate` uses pgvector
   syntax (`embedding <=> CAST(... AS vector)`) that SQLite cannot parse.
   These tests verify the wire path through the ORM and recall surface.
   To enable: run with `pytest --conf=postgres tests/test_f075_end_to_end.py`
   (or whatever harness selector the F075.1 impl PR ships).

The backfill script (Phase 8) has its own test file under F075.1 — out of
scope here.
"""
from __future__ import annotations

import os
from datetime import date, datetime

import pytest

from nous.heart.schemas import FactInput, FactSummary

# A simple env-var gate so the postgres-only tests can be enabled by an
# operator running them against a real DB without changing imports.
_REQUIRES_PG = pytest.mark.skipif(
    not os.environ.get("NOUS_F075_POSTGRES_TESTS"),
    reason=(
        "Requires Postgres+pgvector test harness; Heart.learn dedup uses "
        "pgvector syntax incompatible with the default SQLite conftest. "
        "Enable with NOUS_F075_POSTGRES_TESTS=1 against eval-scratch DB."
    ),
)


# ---------------------------------------------------------------------------
# Schema / validator unit tests — run on any harness
# ---------------------------------------------------------------------------


def test_event_date_validator_accepts_iso_date_strings():
    """Strict YYYY-MM-DD strings parse to date objects."""
    assert FactInput(content="x", event_date="2024-03-10").event_date == date(2024, 3, 10)
    # date object passthrough
    assert FactInput(content="x", event_date=date(2024, 4, 15)).event_date == date(2024, 4, 15)
    # None passthrough
    assert FactInput(content="x", event_date=None).event_date is None


def test_event_date_validator_rejects_alternate_iso_forms():
    """Python 3.12 date.fromisoformat() accepts forms outside the F075
    contract (compact, ISO week). The regex gate must reject them.
    """
    # Compact form (no hyphens)
    assert FactInput(content="x", event_date="20240310").event_date is None
    # ISO week date
    assert FactInput(content="x", event_date="2024-W10-7").event_date is None
    # Empty string
    assert FactInput(content="x", event_date="").event_date is None
    # Garbage
    assert FactInput(content="x", event_date="banana").event_date is None


def test_event_date_validator_rejects_invalid_calendar_dates():
    """Surface shape passes regex but calendar logic rejects."""
    assert FactInput(content="x", event_date="2024-02-30").event_date is None
    assert FactInput(content="x", event_date="2024-13-01").event_date is None


def test_event_date_classified_at_passthrough():
    """event_date_classified_at is a plain datetime — no validator coercion."""
    now = datetime.now()
    f = FactInput(content="x", event_date_classified_at=now)
    assert f.event_date_classified_at == now
    # Default is None for non-F075 callers
    assert FactInput(content="x").event_date_classified_at is None


def test_factsummary_carries_event_date():
    """FactSummary accepts event_date — needed for pre-learn dedup bypass
    at fact_extractor.py:176-184 / 263-273.
    """
    import uuid
    s = FactSummary(
        id=uuid.uuid4(),
        content="x",
        category=None,
        subject="API key event",
        confidence=1.0,
        active=True,
        event_date=date(2024, 3, 10),
    )
    assert s.event_date == date(2024, 3, 10)
    # Default is None for stable facts
    s_stable = FactSummary(
        id=uuid.uuid4(),
        content="y",
        category=None,
        subject=None,
        confidence=1.0,
        active=True,
    )
    assert s_stable.event_date is None


# ---------------------------------------------------------------------------
# Roundtrip integration tests — Postgres required
# ---------------------------------------------------------------------------


@_REQUIRES_PG
@pytest.mark.asyncio
async def test_factinput_roundtrip_event_date_persists(heart, session):
    """Roundtrip: FactInput(event_date=...) → Heart.learn → ORM column populated."""
    from datetime import UTC
    from sqlalchemy import select
    from nous.storage.models import Fact

    fi = FactInput(
        content="Christina obtained the OpenWeather API key on March 10, 2024.",
        subject="OpenWeather API key acquisition",
        event_date=date(2024, 3, 10),
        event_date_classified_at=datetime.now(UTC),
    )
    detail = await heart.learn(fi)
    assert hasattr(detail, "id"), f"expected FactDetail, got {type(detail).__name__}"

    row = (await session.execute(
        select(Fact).where(Fact.id == detail.id),
    )).scalar_one()
    assert row.event_date == date(2024, 3, 10)
    assert row.event_date_classified_at is not None


@_REQUIRES_PG
@pytest.mark.asyncio
async def test_non_f075_caller_leaves_classified_at_null(heart, session):
    """Heart.learn from non-F075 paths leaves event_date_classified_at NULL.

    Simulates tools.py:516 / rest.py:1722 / knowledge_extractor.py:127 style
    callers — the FactInput does not carry F075 producer-side fields. The
    row must remain backfill-eligible (classified_at IS NULL).
    """
    from sqlalchemy import select
    from nous.storage.models import Fact

    fi = FactInput(content="Plain stable fact, no temporal anchor.", subject="topic")
    detail = await heart.learn(fi)
    row = (await session.execute(
        select(Fact).where(Fact.id == detail.id),
    )).scalar_one()
    assert row.event_date is None
    assert row.event_date_classified_at is None


@_REQUIRES_PG
@pytest.mark.asyncio
async def test_recall_surfaces_event_date_in_metadata(heart):
    """Heart.recall returns event_date in RecallResult.metadata as ISO string."""
    fi = FactInput(
        content="User completed the Flask migration on April 15, 2024.",
        subject="Flask migration",
        event_date=date(2024, 4, 15),
    )
    detail = await heart.learn(fi)
    results = await heart.recall("Flask migration date", types=("fact",), limit=5)
    matched = [r for r in results if r.id == detail.id]
    assert matched, "newly learned fact should be recall-able"
    assert matched[0].metadata.get("event_date") == "2024-04-15"


@_REQUIRES_PG
@pytest.mark.asyncio
async def test_dedup_bypass_distinct_dates_same_subject(heart, session):
    """Same subject, similar embedding, DIFFERENT event_date → both persist.

    Both the cosine-dedup path AND the subject-supersession path must
    honor the date-difference bypass — otherwise temporal_reasoning loses
    the date pair.
    """
    from sqlalchemy import select
    from nous.storage.models import Fact

    fi1 = FactInput(
        content="Christina obtained the OpenWeather API key on March 10, 2024.",
        subject="OpenWeather API key event",
        event_date=date(2024, 3, 10),
    )
    fi2 = FactInput(
        content="Christina rotated the OpenWeather API key on March 12, 2024.",
        subject="OpenWeather API key event",
        event_date=date(2024, 3, 12),
    )
    d1 = await heart.learn(fi1)
    d2 = await heart.learn(fi2)
    assert d1.id != d2.id

    rows = (await session.execute(
        select(Fact).where(Fact.id.in_([d1.id, d2.id])),
    )).scalars().all()
    actives = [r for r in rows if r.active]
    assert len(actives) == 2, "Both date-distinct facts should remain active"


@_REQUIRES_PG
@pytest.mark.asyncio
async def test_happened_before_edges_built_for_dated_chain(
    heart, settings, mock_embeddings, db, session,
):
    """GraphDensifier._build_happened_before_edges chains dated facts in episode order."""
    import uuid
    from sqlalchemy import select, text
    from nous.brain.brain import Brain
    from nous.brain.graph_densifier import GraphDensifier
    from nous.brain.graph_linker import GraphLinker
    from nous.storage.models import GraphEdge

    episode_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO heart.episodes (id, agent_id, summary, started_at, ended_at) "
            "VALUES (:id, :agent, :summary, NOW(), NOW())"
        ),
        {"id": episode_id, "agent": heart.agent_id, "summary": "test episode"},
    )
    await session.commit()

    fids = []
    for d in [date(2024, 3, 10), date(2024, 3, 11), date(2024, 3, 12)]:
        fi = FactInput(
            content=f"Event on {d.isoformat()}",
            subject="Project milestone",
            source_episode_id=episode_id,
            event_date=d,
        )
        detail = await heart.learn(fi)
        fids.append(detail.id)

    brain = Brain(db, settings, embedding_provider=mock_embeddings)
    linker = GraphLinker(db, mock_embeddings, settings, heart.agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, heart.agent_id)
    count = await densifier._build_happened_before_edges()
    assert count >= 2

    # Re-running is idempotent
    count2 = await densifier._build_happened_before_edges()
    edges = (await session.execute(
        select(GraphEdge).where(
            GraphEdge.relation == "happened_before",
            GraphEdge.agent_id == heart.agent_id,
        ),
    )).scalars().all()
    assert len(edges) >= 2
    # ON CONFLICT DO NOTHING — second run adds no new rows
    assert count2 == 0
