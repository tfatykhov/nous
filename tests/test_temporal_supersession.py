"""Bi-temporal facts, PR2: supersession on time, flag S (``temporal_supersession_enabled``).

A judged conflict (classifier UPDATE or CONTRADICTION at confidence >= 0.8) is
still required. The intervals then decide only coexistence and direction:

- disjoint intervals coexist (for points this is the F075 rule), before any
  classifier call;
- the later-starting fact wins, and the loser's open interval is closed the day
  before the winner starts;
- a late-arriving fact that started earlier is closed the day before the stored
  one starts, and both stay active;
- a NULL date is never compared: today's logic runs, ordered by statement time.

With S off every path is unchanged.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from nous.config import Settings
from nous.heart.facts import FactManager
from nous.heart.schemas import FactInput
from nous.storage.models import Fact

UPDATE_NEW = {"relation": "UPDATE", "current_fact": "new", "confidence": 0.9}
CONTRADICTION = {"relation": "CONTRADICTION", "current_fact": "new", "confidence": 0.9}
UNRELATED = {"relation": "UNRELATED", "current_fact": "", "confidence": 0.9}
WEAK_UPDATE = {"relation": "UPDATE", "current_fact": "new", "confidence": 0.7}

BERLIN = "Tim has lived in Berlin, in the Mitte district, since June 2024."
PARIS = "Tim has lived in Paris, near the Canal Saint-Martin, since January 2024."
JAN, MAY_31, JUN = date(2024, 1, 1), date(2024, 5, 31), date(2024, 6, 1)


def _flags(heart, *, s: bool, r2: bool = True, **extra) -> None:
    heart.facts._settings = heart.facts._settings.model_copy(
        update={"supersession_key_resolution_enabled": r2, "temporal_supersession_enabled": s, **extra}
    )


def _classifier(monkeypatch, heart, verdict: dict) -> AsyncMock:
    mock = AsyncMock(return_value=verdict)
    monkeypatch.setattr(heart.facts, "_classify_fact_pair", mock)
    monkeypatch.setattr(heart.facts, "_key_budget_ok", lambda: True)
    return mock


async def _row(session, fact_id) -> Fact:
    row = await session.get(Fact, fact_id)
    await session.refresh(row)
    return row


async def _keyed(heart, session, content: str, slot: str = "residence", **kw):
    detail = await heart.learn(
        FactInput(content=content, subject_key="tim", attribute_key=slot, source="user_stated", **kw),
        session=session,
    )
    return detail.id


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_s_defaults_off():
    assert Settings.model_fields["temporal_supersession_enabled"].default is False


# ---------------------------------------------------------------------------
# Statement-time tie-break (pure)
# ---------------------------------------------------------------------------


def _manager(s: bool) -> FactManager:
    fm = FactManager.__new__(FactManager)
    fm._settings = SimpleNamespace(temporal_supersession_enabled=s)
    return fm


def _f(learned: datetime, mentioned: datetime | None = None) -> SimpleNamespace:
    return SimpleNamespace(source_ordinal=None, source_episode_id=None, learned_at=learned, mentioned_at=mentioned)


T = datetime(2026, 11, 1, tzinfo=UTC)


def test_contradiction_order_is_statement_time_under_s():
    # Ingested in the opposite order to when they were said (a backfill).
    old = _f(T, mentioned=datetime(2024, 6, 1, tzinfo=UTC))
    new = _f(datetime(2026, 10, 1, tzinfo=UTC), mentioned=datetime(2024, 7, 1, tzinfo=UTC))
    assert _manager(False)._pick_contradiction_winner(old, new) is old  # later learned_at
    assert _manager(True)._pick_contradiction_winner(old, new) is new  # later statement


def test_equal_statement_times_fall_back_to_learned_at():
    """Facts from one episode share its start: statement time ties, learned_at decides."""
    said = datetime(2024, 6, 1, tzinfo=UTC)
    old, new = _f(datetime(2026, 10, 1, tzinfo=UTC), said), _f(T, said)
    assert _manager(True)._pick_contradiction_winner(old, new) is new
    assert _manager(True)._pick_contradiction_winner(_f(T, said), _f(T, said)) is None


def test_a_missing_statement_time_reads_as_learned_at():
    old = _f(T, mentioned=None)  # learn_fact: said when learned
    new = _f(datetime(2026, 12, 1, tzinfo=UTC), mentioned=datetime(2024, 1, 1, tzinfo=UTC))
    assert _manager(True)._pick_contradiction_winner(old, new) is old
    assert _manager(False)._pick_contradiction_winner(old, new) is new


def test_update_recency_is_statement_time_under_s():
    old = _f(T, mentioned=datetime(2024, 6, 1, tzinfo=UTC))
    new = _f(datetime(2026, 10, 1, tzinfo=UTC), mentioned=datetime(2024, 7, 1, tzinfo=UTC))
    assert _manager(False)._pick_winner(new, old, {}) == (old, new)
    assert _manager(True)._pick_winner(new, old, {}) == (new, old)


# ---------------------------------------------------------------------------
# apply_supersession closes the loser's open interval
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("s", "loser_to", "winner_from", "expected_to"),
    [
        (True, None, JUN, MAY_31),
        (False, None, JUN, None),
        (True, date(2024, 3, 31), JUN, date(2024, 3, 31)),  # already closed: unchanged
        (True, None, date(2023, 12, 1), None),  # the winner started first: nothing to close
        (True, None, None, None),  # an unknown start is never compared
    ],
)
async def test_apply_supersession_closes_the_losers_open_interval(
    heart, session, s, loser_to, winner_from, expected_to
):
    _flags(heart, s=s, r2=False)
    loser = await heart.learn(
        FactInput(content=f"{PARIS} ({uuid4()})", valid_from=JAN, valid_to=loser_to, source="user_stated"),
        session=session,
    )
    winner = await heart.learn(
        FactInput(content=f"{BERLIN} ({uuid4()})", valid_from=winner_from, source="user_stated"),
        session=session,
    )
    assert await heart.facts.apply_supersession(winner.id, loser.id, session) is True
    row = await _row(session, loser.id)
    assert row.active is False and row.superseded_by == winner.id
    assert row.valid_to == expected_to


# ---------------------------------------------------------------------------
# Keyed write path (R2)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize("s", [True, False])
async def test_keyed_points_on_different_days_coexist_without_a_classifier_call(heart, session, monkeypatch, s):
    _flags(heart, s=s)
    cls = _classifier(monkeypatch, heart, CONTRADICTION)
    a = await _keyed(
        heart,
        session,
        "The quarterly review with the board took place on 10 March 2024.",
        "review",
        event_date="2024-03-10",
    )
    b = await _keyed(
        heart,
        session,
        "The quarterly review with the board took place on 12 March 2024.",
        "review",
        event_date="2024-03-12",
    )
    assert cls.await_count == 0
    assert (await _row(session, a)).active and (await _row(session, b)).active


@pytest.mark.postgres_only
async def test_keyed_late_arrival_closes_the_new_fact_and_keeps_both(heart, session, monkeypatch):
    _flags(heart, s=True)
    _classifier(monkeypatch, heart, UPDATE_NEW)
    berlin = await _keyed(heart, session, BERLIN, valid_from=JUN)
    paris = await _keyed(heart, session, PARIS, valid_from=JAN)  # arrives later, started earlier
    b, p = await _row(session, berlin), await _row(session, paris)
    assert b.active and b.superseded_by is None and b.valid_to is None
    assert p.active and p.superseded_by is None
    assert (p.valid_from, p.valid_to) == (JAN, MAY_31)


@pytest.mark.postgres_only
async def test_keyed_late_arrival_with_s_off_supersedes_as_before(heart, session, monkeypatch):
    _flags(heart, s=False)
    _classifier(monkeypatch, heart, UPDATE_NEW)
    berlin = await _keyed(heart, session, BERLIN, valid_from=JUN)
    paris = await _keyed(heart, session, PARIS, valid_from=JAN)
    b, p = await _row(session, berlin), await _row(session, paris)
    assert b.active is False and b.superseded_by == paris
    assert p.active and p.valid_to is None


@pytest.mark.postgres_only
@pytest.mark.parametrize("verdict", [UPDATE_NEW, CONTRADICTION], ids=["update", "contradiction"])
async def test_keyed_forward_update_supersedes_and_closes_the_old_state(heart, session, monkeypatch, verdict):
    _flags(heart, s=True)
    _classifier(monkeypatch, heart, verdict)
    paris = await _keyed(heart, session, PARIS, valid_from=JAN)
    berlin = await _keyed(heart, session, BERLIN, valid_from=JUN)
    p, b = await _row(session, paris), await _row(session, berlin)
    assert p.active is False and p.superseded_by == berlin
    assert (p.valid_from, p.valid_to) == (JAN, MAY_31)
    assert b.active and b.valid_to is None


@pytest.mark.postgres_only
async def test_keyed_forward_update_with_s_off_leaves_the_interval_open(heart, session, monkeypatch):
    _flags(heart, s=False)
    _classifier(monkeypatch, heart, UPDATE_NEW)
    paris = await _keyed(heart, session, PARIS, valid_from=JAN)
    berlin = await _keyed(heart, session, BERLIN, valid_from=JUN)
    p = await _row(session, paris)
    assert p.active is False and p.superseded_by == berlin and p.valid_to is None


@pytest.mark.postgres_only
@pytest.mark.parametrize("verdict", [UNRELATED, WEAK_UPDATE], ids=["unrelated", "low_confidence"])
async def test_keyed_intervals_never_act_without_a_judged_conflict(heart, session, monkeypatch, verdict):
    _flags(heart, s=True)
    _classifier(monkeypatch, heart, verdict)
    berlin = await _keyed(heart, session, BERLIN, valid_from=JUN)
    paris = await _keyed(heart, session, PARIS, valid_from=JAN)
    b, p = await _row(session, berlin), await _row(session, paris)
    assert b.active and p.active
    assert b.valid_to is None and p.valid_to is None


@pytest.mark.postgres_only
@pytest.mark.parametrize("s", [True, False])
async def test_keyed_undated_facts_resolve_as_with_s_off(heart, session, monkeypatch, s):
    """NULL intervals and NULL statement times: S changes nothing."""
    _flags(heart, s=s)
    _classifier(monkeypatch, heart, UPDATE_NEW)
    old = await _keyed(heart, session, "The production database runs on Postgres sixteen point four.", "db")
    new = await _keyed(heart, session, "The production database runs on Postgres seventeen point one.", "db")
    o, n = await _row(session, old), await _row(session, new)
    assert o.active is False and o.superseded_by == new and o.valid_to is None
    assert n.active


@pytest.mark.postgres_only
@pytest.mark.parametrize(("verdict", "superseded"), [(UNRELATED, False), (CONTRADICTION, True)])
async def test_keyed_a_trip_inside_a_residence_is_protected_only_by_the_classifier(
    heart, session, monkeypatch, verdict, superseded
):
    _flags(heart, s=True)
    _classifier(monkeypatch, heart, verdict)
    home = await _keyed(heart, session, PARIS, "whereabouts", valid_from=JAN)
    trip = await _keyed(
        heart,
        session,
        "Tim was on a work trip to Rome from 3 to 10 March 2024.",
        "whereabouts",
        valid_from="2024-03-03",
        valid_to="2024-03-10",
    )
    h = await _row(session, home)
    assert (h.active is False) is superseded
    assert h.valid_to == (date(2024, 3, 2) if superseded else None)
    assert (await _row(session, trip)).active


# ---------------------------------------------------------------------------
# Legacy same-subject write path (R2 off)
# ---------------------------------------------------------------------------


def _vec(sim_to_base: float) -> list[float]:
    v = [0.0] * 1536
    v[1500] = sim_to_base
    v[1501] = math.sqrt(max(0.0, 1.0 - sim_to_base**2))
    return v


async def _subject(heart, session, content: str, subject: str, sim: float, **kw):
    detail = await heart.learn(
        FactInput(content=content, subject=subject, source="user_stated", **kw),
        session=session,
        precomputed_embedding=_vec(sim),
    )
    return detail.id


@pytest.mark.postgres_only
@pytest.mark.parametrize("s", [True, False])
async def test_legacy_late_arrival_in_the_judged_band(heart, session, monkeypatch, s):
    _flags(heart, s=s, r2=False)
    monkeypatch.setattr(heart.facts, "_llm", object())
    _classifier(monkeypatch, heart, UPDATE_NEW)
    subject = f"Tim residence {uuid4().hex[:8]}"
    berlin = await _subject(heart, session, BERLIN, subject, 1.0, valid_from=JUN)
    paris = await _subject(heart, session, PARIS, subject, 0.83, valid_from=JAN)
    b, p = await _row(session, berlin), await _row(session, paris)
    if s:
        assert b.active and p.active and b.superseded_by is None
        assert p.valid_to == MAY_31
    else:
        assert b.active is False and b.superseded_by == paris and p.valid_to is None


@pytest.mark.postgres_only
async def test_legacy_forward_update_in_the_judged_band(heart, session, monkeypatch):
    _flags(heart, s=True, r2=False)
    monkeypatch.setattr(heart.facts, "_llm", object())
    _classifier(monkeypatch, heart, CONTRADICTION)
    subject = f"Tim residence {uuid4().hex[:8]}"
    paris = await _subject(heart, session, PARIS, subject, 1.0, valid_from=JAN)
    berlin = await _subject(heart, session, BERLIN, subject, 0.83, valid_from=JUN)
    p = await _row(session, paris)
    assert p.active is False and p.superseded_by == berlin and p.valid_to == MAY_31


@pytest.mark.postgres_only
@pytest.mark.parametrize("s", [True, False])
async def test_legacy_unjudged_near_duplicate_keeps_its_direction(heart, session, monkeypatch, s):
    """Above 0.95 no classifier runs: the new fact supersedes as before. Under S the
    deactivation goes through apply_supersession, so the old state is closed."""
    _flags(heart, s=s, r2=False, fact_native_cosine_threshold=0.99)
    cls = _classifier(monkeypatch, heart, CONTRADICTION)
    subject = f"Tim residence {uuid4().hex[:8]}"
    old = await _subject(heart, session, "Tim lives in Paris, since January 2024.", subject, 1.0, valid_from=JAN)
    new = await _subject(heart, session, "Tim lives in Paris, since June 2024.", subject, 0.97, valid_from=JUN)
    assert cls.await_count == 0
    o = await _row(session, old)
    assert o.active is False and o.superseded_by == new
    assert o.valid_to == (MAY_31 if s else None)


@pytest.mark.postgres_only
@pytest.mark.parametrize("s", [True, False])
async def test_legacy_disjoint_states_coexist_before_the_classifier(heart, session, monkeypatch, s):
    _flags(heart, s=s, r2=False)
    monkeypatch.setattr(heart.facts, "_llm", object())
    cls = _classifier(monkeypatch, heart, UPDATE_NEW)
    subject = f"Tim residence {uuid4().hex[:8]}"
    old = await _subject(
        heart,
        session,
        "Tim lived in Lyon from January to March 2023.",
        subject,
        1.0,
        valid_from="2023-01-01",
        valid_to="2023-03-31",
    )
    await _subject(heart, session, BERLIN, subject, 0.83, valid_from=JUN)
    # With S on the pair is disjoint and never judged; with S off the classifier decides.
    assert (cls.await_count == 0) is s
    assert (await _row(session, old)).active is s


# ---------------------------------------------------------------------------
# Sleep sweep (find_key_conflict_pairs + resolve_key_conflict_pair)
# ---------------------------------------------------------------------------


@pytest.fixture
async def own_agent(heart):
    agent = f"test-t2-sweep-{uuid4()}"
    saved = heart.facts.agent_id
    heart.facts.agent_id = agent
    try:
        yield agent
    finally:
        heart.facts.agent_id = saved
        async with heart.db.session() as s:
            await s.execute(text("DELETE FROM heart.facts WHERE agent_id = :a"), {"a": agent})
            await s.execute(text("DELETE FROM brain.graph_edges WHERE agent_id = :a"), {"a": agent})
            await s.commit()


async def _stored(heart, agent: str, content: str, learned: datetime, **cols) -> Fact:
    async with heart.db.session() as s:
        row = Fact(
            id=uuid4(),
            agent_id=agent,
            content=content,
            subject_key="tim",
            attribute_key="residence",
            learned_at=learned,
            active=True,
            **cols,
        )
        s.add(row)
        await s.commit()
        return row


@pytest.mark.postgres_only
@pytest.mark.parametrize("s", [True, False])
async def test_the_sweep_skips_coexisting_pairs_only_under_s(heart, own_agent, s):
    _flags(heart, s=s)
    lyon = await _stored(
        heart, own_agent, "Tim lived in Lyon in 2023.", T, valid_from=date(2023, 1, 1), valid_to=date(2023, 12, 31)
    )
    berlin = await _stored(heart, own_agent, BERLIN, datetime(2026, 11, 2, tzinfo=UTC), valid_from=JUN)
    pairs = {(p["id1"], p["id2"]) for p in await heart.facts.find_key_conflict_pairs(limit=10)}
    assert ((lyon.id, berlin.id) in pairs) is not s
    # The paged branch carries the same rule.
    after = (datetime(2000, 1, 1, tzinfo=UTC), uuid4(), datetime(2000, 1, 1, tzinfo=UTC), uuid4())
    paged = {(p["id1"], p["id2"]) for p in await heart.facts.find_key_conflict_pairs(limit=10, after=after)}
    assert ((lyon.id, berlin.id) in paged) is not s


@pytest.mark.postgres_only
async def test_the_sweep_closes_a_late_arrival_and_then_leaves_the_pair_alone(heart, own_agent, monkeypatch):
    _flags(heart, s=True)
    _classifier(monkeypatch, heart, UPDATE_NEW)
    berlin = await _stored(heart, own_agent, BERLIN, T, valid_from=JUN)
    paris = await _stored(heart, own_agent, PARIS, datetime(2026, 11, 2, tzinfo=UTC), valid_from=JAN)
    (pair,) = await heart.facts.find_key_conflict_pairs(limit=10)
    assert (pair["id1"], pair["id2"]) == (berlin.id, paris.id)
    assert await heart.facts.resolve_key_conflict_pair(pair["id1"], pair["id2"], pair["c1"], pair["c2"]) is False
    async with heart.db.session() as s:
        b, p = await s.get(Fact, berlin.id), await s.get(Fact, paris.id)
        assert b.active and p.active and b.superseded_by is None and p.superseded_by is None
        assert (p.valid_from, p.valid_to) == (JAN, MAY_31) and b.valid_to is None
    # Now disjoint: the next sweep does not pair them again.
    assert await heart.facts.find_key_conflict_pairs(limit=10) == []


@pytest.mark.postgres_only
async def test_the_sweep_forward_update_closes_the_loser(heart, own_agent, monkeypatch):
    _flags(heart, s=True)
    _classifier(monkeypatch, heart, CONTRADICTION)
    paris = await _stored(heart, own_agent, PARIS, T, valid_from=JAN)
    berlin = await _stored(heart, own_agent, BERLIN, datetime(2026, 11, 2, tzinfo=UTC), valid_from=JUN)
    assert await heart.facts.resolve_key_conflict_pair(paris.id, berlin.id, PARIS, BERLIN) is True
    async with heart.db.session() as s:
        p = await s.get(Fact, paris.id)
        assert p.active is False and p.superseded_by == berlin.id and p.valid_to == MAY_31


@pytest.mark.postgres_only
async def test_the_sweep_never_judges_a_pair_that_became_disjoint(heart, own_agent, monkeypatch):
    """A page can go stale: an earlier pair on it closed one side."""
    _flags(heart, s=True)
    cls = _classifier(monkeypatch, heart, UPDATE_NEW)
    lyon = await _stored(
        heart, own_agent, "Tim lived in Lyon in 2023.", T, valid_from=date(2023, 1, 1), valid_to=date(2023, 12, 31)
    )
    berlin = await _stored(heart, own_agent, BERLIN, datetime(2026, 11, 2, tzinfo=UTC), valid_from=JUN)
    assert await heart.facts.resolve_key_conflict_pair(lyon.id, berlin.id, lyon.content, BERLIN) is False
    assert cls.await_count == 0
    async with heart.db.session() as s:
        assert (await s.execute(select(Fact.active).where(Fact.id == lyon.id))).scalar_one() is True
