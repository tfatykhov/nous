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
# Extraction-prompt guards (regression — 2026-06-13 edge-precision audit)
# ---------------------------------------------------------------------------


def test_temporal_instruction_excludes_bibliographic_and_anchors_year():
    """The summarizer's date-extraction addendum must keep the three guards
    that fix the measured happened_before 0.27 precision: exclude bibliographic
    publication dates, omit month/year-only dates, and anchor the year."""
    from nous.handlers.episode_summarizer import _F075_TEMPORAL_INSTRUCTION as instr

    low = instr.lower()
    assert "arxiv" in low and "publication" in low  # bibliographic exclusion
    assert "omit event_date" in low  # month/year-granularity omission
    assert "never assume a prior year" in low  # year anchor


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


def test_event_date_validator_coerces_datetime_to_date():
    """datetime is a subclass of date in Python; the validator must explicitly
    coerce so the DATE DB column doesn't receive a datetime.
    """
    dt = datetime(2024, 3, 10, 14, 30, 0)
    f = FactInput(content="x", event_date=dt)
    # Result should be a pure date, not a datetime (datetime IS-A date so
    # an isinstance(date) check passes for both — verify the strict type).
    assert type(f.event_date) is date  # noqa: E721
    assert f.event_date == date(2024, 3, 10)


def test_merge_summaries_dated_partition_preserves_more_than_5_dated():
    """Pure-Python test of _merge_summaries split caps (no DB needed).

    Regression guard for spec v2.11 P1: stable [:5] cap was dropping dated
    events from chunks 6+. Fix splits into dated[:event_limit] + stable[:5].
    35 dated + 10 stable across chunks must survive as 30 + 5.
    """
    from nous.handlers.episode_summarizer import EpisodeSummarizer

    class FakeSettings:
        candidate_facts_event_limit = 30

    class FakeSelf:
        _settings = FakeSettings()

    # 35 dated + 10 stable in a single "chunk summary"
    summaries = [{
        "candidate_facts": (
            [{"subject": f"s{i}", "content": "x", "event_date": "2024-03-10"} for i in range(35)]
            + [{"subject": f"t{i}", "content": "y"} for i in range(10)]
        ),
        "summary": "x", "key_points": [], "topics": [],
    }]
    merged = EpisodeSummarizer._merge_summaries(FakeSelf(), summaries)
    dated = [c for c in merged["candidate_facts"] if c.get("event_date")]
    stable = [c for c in merged["candidate_facts"] if not c.get("event_date")]
    assert len(dated) == 30, f"expected 30 dated facts after merge, got {len(dated)}"
    assert len(stable) == 5, f"expected 5 stable facts after merge, got {len(stable)}"


@pytest.mark.asyncio
async def test_pre_learn_dedup_bypass_polarity_extracted_facts():
    """Mock-based polarity verification for _store_extracted_facts dedup.

    A polarity inversion would silently break the feature. Covers the
    invariant: distinct event_dates with high embedding similarity must
    NOT dedup. Same-date or one-side-NULL must still dedup.

    Uses ``@pytest.mark.asyncio`` (not ``asyncio.run`` directly) — calling
    asyncio.run() multiple times in a sync test leaves the event-loop
    policy in a state where ``asyncio.get_event_loop()`` raises in later
    sync tests collected after this one (notably test_subtasks.py).
    """
    # Defer import to avoid heavy module-level loading in pure-validator tests.
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from nous.handlers.fact_extractor import FactExtractor
    from nous.heart.schemas import FactSummary

    async def run_case(candidate_event_date, existing_event_date, dates_should_differ):
        heart = MagicMock()
        existing_id = uuid4()
        # search_facts returns a near-duplicate above threshold
        heart.find_similar_facts = AsyncMock(return_value=[FactSummary(
            id=existing_id,
            content="existing",
            category=None,
            subject="API key event",
            confidence=1.0,
            active=True,
            score=0.99,  # above default dedup threshold
            event_date=existing_event_date,
        )])
        heart.learn = AsyncMock()  # only invoked if dedup bypassed

        settings = MagicMock()
        settings.fact_dedup_threshold = 0.92
        settings.candidate_facts_event_limit = 30
        settings.temporal_extraction_enabled = False  # don't stamp classified_at

        fx = FactExtractor.__new__(FactExtractor)
        fx._heart = heart
        fx._settings = settings
        fx._dedup_via_search = True

        candidates = [{
            "subject": "API key event",
            "content": "API key obtained",
            "confidence": 0.9,
            "event_date": candidate_event_date,
        }]
        stored_ids = await fx._store_extracted_facts(candidates, episode_id="?", transcript=None)

        if dates_should_differ:
            # Bypass: should have called learn (new fact), NOT just appended existing
            assert heart.learn.await_count == 1, (
                f"distinct dates {candidate_event_date} vs {existing_event_date} must bypass dedup"
            )
        else:
            # Dedup engaged: appended existing canonical id, no learn call
            assert heart.learn.await_count == 0, (
                f"same-or-null dates ({candidate_event_date}, {existing_event_date}) must dedup"
            )
            assert existing_id in stored_ids

    await run_case("2024-03-10", date(2024, 3, 12), dates_should_differ=True)
    await run_case("2024-03-10", date(2024, 3, 10), dates_should_differ=False)
    await run_case(None, date(2024, 3, 10), dates_should_differ=False)
    await run_case("2024-03-10", None, dates_should_differ=False)


@pytest.mark.asyncio
async def test_malformed_date_stays_backfill_eligible():
    """SFH final-review Medium: when the LLM emits a date the validator drops
    as malformed, _store_candidate_facts must leave event_date_classified_at
    NULL so the row stays backfill-eligible — NOT stamp it as terminal
    "classified, no date found" (which would permanently lock it out of
    F075.1 backfill on exactly the rows F075 exists to capture).

    Contrast: a genuinely-undated candidate SHOULD stamp (flag on) because
    there was no date to recover.
    """
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from nous.handlers.fact_extractor import FactExtractor

    async def captured_learn_input(raw_event_date):
        heart = MagicMock()
        heart.find_similar_facts = AsyncMock(return_value=[])  # no dedup hit
        captured = {}

        async def _learn(fact_input):
            captured["fi"] = fact_input
            r = MagicMock()
            r.id = uuid4()
            return r

        heart.learn = AsyncMock(side_effect=_learn)

        settings = MagicMock()
        settings.fact_dedup_threshold = 0.92
        settings.candidate_facts_event_limit = 30
        settings.temporal_extraction_enabled = True  # flag ON

        fx = FactExtractor.__new__(FactExtractor)
        fx._heart = heart
        fx._settings = settings
        fx._dedup_via_search = True

        candidates = [{
            "subject": "API key event",
            "content": "User obtained the API key.",
            "event_date": raw_event_date,
        }]
        await fx._store_candidate_facts(candidates, episode_id="?", transcript=None)
        return captured["fi"]

    # Malformed date (regex-rejected) → event_date None AND classified_at None
    fi_bad = await captured_learn_input("2024-3-10")  # not zero-padded
    assert fi_bad.event_date is None
    assert fi_bad.event_date_classified_at is None, (
        "malformed date must leave classified_at NULL (backfill-eligible)"
    )

    # Genuinely undated → event_date None but classified_at STAMPED (flag on)
    fi_none = await captured_learn_input(None)
    assert fi_none.event_date is None
    assert fi_none.event_date_classified_at is not None, (
        "genuinely-undated candidate should stamp classified_at when flag on"
    )

    # Valid date → both set
    fi_ok = await captured_learn_input("2024-03-10")
    assert fi_ok.event_date == date(2024, 3, 10)
    assert fi_ok.event_date_classified_at is not None


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
