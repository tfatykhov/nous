"""Fact writes and merges fail closed: no supersession or merge on uncertain
evidence.

- Legacy same-subject supersession keeps both facts unless the classifier
  confidently says UPDATE. Bucket subjects never take part, and the candidate
  scan is a capped pgvector query gated on the key-classifier budget.
- A differently dated fact is never a contradiction candidate.
- A new fact that loses to an existing one keeps its lineage, and the caller
  sees it inactive.
- An in-band dedup hit the classifier did not call a duplicate does not hand
  its metadata to the existing fact.
- Supersession never points at a winner that is gone, inactive or
  superseded, and F031 re-reads each pair before judging it.
- A sleep merge is one transaction. It is aborted, and the pair or cluster
  cools down, when the merged content confirms a third fact or the merged
  fact loses its own write-time scan. An F027 cluster the LLM refused cools
  down for its own, shorter window.
- F031 never MERGEs a tier-1 (person/preference/rule) fact, and an UPDATES:
  line never supersedes one.
- fact_learned on a caller's session is published only after that commit.
- Migration 095 lets refines edges land.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update

from nous.heart.facts import FactManager
from nous.heart.schemas import FactInput
from nous.storage.migrator import _split_sql_statements
from nous.storage.models import Event, Fact, FactEntityKey, GraphEdge

DIM = 1536


def _vec(sim: float, axis: int = 1) -> list[float]:
    """Unit vector whose cosine to ``ANCHOR`` is ``sim``."""
    v = [0.0] * DIM
    v[0] = sim
    v[axis] = math.sqrt(max(0.0, 1.0 - sim * sim))
    return v


ANCHOR = _vec(1.0)


def _fm(heart, **overrides) -> FactManager:
    """A FactManager with its own agent id, so no other test's rows are visible."""
    settings = heart.settings.model_copy(
        update={
            "fact_native_cosine_threshold": 0.95,
            "fact_supersession_threshold": 0.80,
            "supersession_key_resolution_enabled": False,
            **overrides,
        }
    )
    return FactManager(heart.db, None, agent_id=f"test-pr-d-{uuid4()}", settings=settings)


def _classifier(fm: FactManager, verdict: dict | None) -> AsyncMock:
    fm._llm = MagicMock()
    fm._classify_fact_pair = AsyncMock(return_value=verdict)
    return fm._classify_fact_pair


async def _add(session, fm: FactManager, content: str, embedding: list[float], **kw) -> Fact:
    row = Fact(id=uuid4(), agent_id=fm.agent_id, content=content, embedding=embedding, active=True, **kw)
    session.add(row)
    await session.flush()
    return row


async def _edge(session, source_id, target_id, relation: str) -> bool:
    found = await session.execute(
        select(GraphEdge.id).where(
            GraphEdge.source_id == source_id,
            GraphEdge.target_id == target_id,
            GraphEdge.relation == relation,
        )
    )
    return found.first() is not None


UPDATE_NEW = {"relation": "UPDATE", "current_fact": "new", "confidence": 0.95}
UPDATE_OLD = {"relation": "UPDATE", "current_fact": "old", "confidence": 0.95}


# ---------------------------------------------------------------------------
# Legacy same-subject supersession
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    "verdict",
    [
        None,
        {"relation": "SAME_THING", "current_fact": "new", "confidence": 0.95},
        {"relation": "UPDATE", "current_fact": "new", "confidence": 0.6},
    ],
    ids=["classifier-failed", "unknown-relation", "low-confidence-update"],
)
async def test_an_uncertain_verdict_keeps_both_facts(heart, session, verdict):
    fm = _fm(heart)
    classify = _classifier(fm, verdict)
    old = await _add(session, fm, "The deploy window is Tuesday at noon UTC.", ANCHOR, subject="deploy window")
    new = await _add(session, fm, "The deploy window is Thursday at noon UTC.", _vec(0.88), subject="deploy window")

    lost = await fm._supersede_by_subject(new.id, "deploy window", _vec(0.88), session, new_content=new.content)

    classify.assert_awaited_once()
    assert lost is False
    assert old.active is True and old.superseded_by is None


@pytest.mark.postgres_only
async def test_a_confident_update_still_supersedes(heart, session):
    fm = _fm(heart)
    _classifier(fm, UPDATE_NEW)
    old = await _add(session, fm, "The deploy window is Tuesday at noon UTC.", ANCHOR, subject="deploy window")
    new = await _add(session, fm, "The deploy window is Thursday at noon UTC.", _vec(0.88), subject="deploy window")

    lost = await fm._supersede_by_subject(new.id, "deploy window", _vec(0.88), session, new_content=new.content)

    assert lost is False
    assert old.active is False and old.superseded_by == new.id
    assert old.deactivation_reason == "superseded"


@pytest.mark.postgres_only
@pytest.mark.parametrize("subject", ["lesson_learned", "Daily_Reflection", "reflection", " unknown "])
async def test_a_sentinel_subject_never_supersedes(heart, session, subject):
    fm = _fm(heart)
    old = await _add(session, fm, "Always rerun the flaky suite before blaming the change.", ANCHOR, subject=subject)
    new = await _add(session, fm, "Always rerun the flaky suite before blaming a change.", _vec(0.99), subject=subject)

    lost = await fm._supersede_by_subject(new.id, subject, _vec(0.99), session, new_content=new.content)

    assert lost is False
    assert old.active is True and old.superseded_by is None


@pytest.mark.postgres_only
async def test_a_spent_key_budget_keeps_both_without_a_classifier_call(heart, session):
    fm = _fm(heart, supersession_classifier_max_per_hour=1)
    assert fm._key_budget_ok() is True  # spend the only slot
    classify = _classifier(fm, UPDATE_NEW)
    old = await _add(session, fm, "The deploy window is Tuesday at noon UTC.", ANCHOR, subject="deploy window")
    new = await _add(session, fm, "The deploy window is Thursday at noon UTC.", _vec(0.88), subject="deploy window")

    lost = await fm._supersede_by_subject(new.id, "deploy window", _vec(0.88), session, new_content=new.content)

    classify.assert_not_awaited()
    assert lost is False
    assert old.active is True


@pytest.mark.postgres_only
async def test_the_candidate_scan_is_capped_at_the_five_nearest(heart, session):
    fm = _fm(heart)
    classify = _classifier(fm, {"relation": "UNRELATED", "current_fact": "new", "confidence": 0.9})
    sims = [0.93, 0.92, 0.91, 0.90, 0.89, 0.86, 0.85]
    olds = [
        await _add(session, fm, f"Same-subject fact number {i} about the cache.", _vec(s, axis=i + 1), subject="cache")
        for i, s in enumerate(sims)
    ]
    new = await _add(session, fm, "The cache is warmed from the replica on boot.", ANCHOR, subject="cache")

    await fm._supersede_by_subject(new.id, "cache", ANCHOR, session, new_content=new.content)

    assert classify.await_count == 5
    judged = {call.args[0] for call in classify.await_args_list}
    assert judged == {o.content for o in olds[:5]}


# ---------------------------------------------------------------------------
# A differently dated fact is never a contradiction candidate
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_a_dated_event_bypass_is_not_superseded_by_the_contradiction_scan(heart, session):
    # 0.90 clears the 0.85 dedup threshold, so this lands on the F075
    # distinct-date bypass, then sits inside the 0.85-0.95 contradiction band.
    fm = _fm(heart, fact_native_cosine_threshold=0.85)
    _classifier(fm, UPDATE_NEW)
    old = await _add(
        session, fm, "The API key was obtained from the vendor portal.", ANCHOR, event_date=date(2026, 3, 10)
    )

    detail = await fm.learn(
        FactInput(content="The API key was rotated through the vendor portal.", event_date=date(2026, 3, 12)),
        session=session,
        precomputed_embedding=_vec(0.90),
    )

    assert detail.id != old.id
    assert old.active is True and old.superseded_by is None


# ---------------------------------------------------------------------------
# A new fact that loses keeps its lineage, and the caller sees it inactive
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_losing_the_contradiction_scan_records_lineage_and_returns_inactive(heart, session):
    fm = _fm(heart)
    _classifier(fm, UPDATE_OLD)
    old = await _add(session, fm, "The billing export runs on the first of the month.", ANCHOR)

    detail = await fm.learn(
        FactInput(content="The billing export ran on the first of last month."),
        session=session,
        precomputed_embedding=_vec(0.90),
    )

    row = await session.get(Fact, detail.id)
    assert row.active is False
    assert detail.active is False
    assert row.superseded_by == old.id
    assert row.deactivation_reason == "superseded"
    assert await _edge(session, old.id, row.id, "supersedes")
    assert old.active is True


@pytest.mark.postgres_only
async def test_losing_the_subject_scan_records_lineage(heart, session):
    fm = _fm(heart)
    _classifier(fm, UPDATE_OLD)
    old = await _add(session, fm, "The deploy window is Tuesday at noon UTC.", ANCHOR, subject="deploy window")

    detail = await fm.learn(
        FactInput(content="The deploy window was Monday at noon UTC.", subject="deploy window"),
        session=session,
        precomputed_embedding=_vec(0.88),
    )

    row = await session.get(Fact, detail.id)
    assert detail.active is False and row.active is False
    assert row.superseded_by == old.id
    assert await _edge(session, old.id, row.id, "supersedes")
    assert old.active is True


# ---------------------------------------------------------------------------
# An in-band dedup hit not judged a duplicate keeps its own metadata
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_an_unjudged_band_confirm_only_counts_the_confirmation(heart, session):
    fm = _fm(heart, fact_native_cosine_threshold=0.80)
    _classifier(fm, UPDATE_OLD)  # in [0.85, 0.95): UPDATE/old confirms
    old = await _add(session, fm, "The release train leaves every Thursday at noon.", ANCHOR)

    detail = await fm.learn(
        FactInput(
            content="The release train left on Thursday the fifth at noon.",
            event_date=date(2026, 1, 5),
            subject_key="release train",
            attribute_key="schedule",
            source_ordinal=3,
        ),
        session=session,
        precomputed_embedding=_vec(0.88),
    )

    assert detail.id == old.id
    assert old.confirmation_count == 1
    assert old.event_date is None
    assert old.subject_key is None and old.attribute_key is None
    assert old.source_ordinal is None
    keys = await session.execute(select(func.count()).select_from(FactEntityKey).where(FactEntityKey.fact_id == old.id))
    assert keys.scalar_one() == 0


@pytest.mark.postgres_only
async def test_a_confident_unrelated_verdict_below_the_band_inserts(heart, session):
    fm = _fm(heart, fact_native_cosine_threshold=0.80)
    _classifier(fm, {"relation": "UNRELATED", "current_fact": "new", "confidence": 0.9})
    old = await _add(session, fm, "The staging database runs Postgres seventeen.", ANCHOR)

    detail = await fm.learn(
        FactInput(content="The staging cache runs Redis seven on port 6380."),
        session=session,
        precomputed_embedding=_vec(0.82),
    )

    assert detail.id != old.id
    assert (old.confirmation_count or 0) == 0


@pytest.mark.postgres_only
async def test_a_distinct_refinement_below_the_band_inserts(heart, session):
    fm = _fm(heart, fact_native_cosine_threshold=0.80)
    _classifier(fm, {"relation": "REFINEMENT", "current_fact": "new", "confidence": 0.9})
    fm.is_distinct_fact = AsyncMock(return_value=True)
    old = await _add(session, fm, "The nightly backup writes to the archive bucket.", ANCHOR)

    detail = await fm.learn(
        FactInput(content="The nightly backup writes to the archive bucket in eu-west-2."),
        session=session,
        precomputed_embedding=_vec(0.82),
    )

    assert detail.id != old.id
    fm.is_distinct_fact.assert_awaited_once()
    assert (old.confirmation_count or 0) == 0
    assert await _edge(session, detail.id, old.id, "refines")  # migration 095


@pytest.mark.postgres_only
async def test_a_refinement_judged_duplicate_confirms_and_copies_metadata(heart, session):
    fm = _fm(heart, fact_native_cosine_threshold=0.80)
    _classifier(fm, {"relation": "REFINEMENT", "current_fact": "new", "confidence": 0.9})
    fm.is_distinct_fact = AsyncMock(return_value=False)
    old = await _add(session, fm, "The nightly backup writes to the archive bucket.", ANCHOR)

    detail = await fm.learn(
        FactInput(content="Nightly backups are written to the archive bucket.", event_date=date(2026, 2, 1)),
        session=session,
        precomputed_embedding=_vec(0.82),
    )

    assert detail.id == old.id
    assert old.confirmation_count == 1
    assert old.event_date == date(2026, 2, 1)


# ---------------------------------------------------------------------------
# apply_supersession re-reads the winner
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize("winner_state", ["missing", "inactive", "superseded"])
async def test_supersession_never_points_at_a_gone_winner(heart, session, winner_state):
    fm = _fm(heart)
    loser = await _add(session, fm, "The on-call rotation hands over on Fridays.", ANCHOR)
    if winner_state == "missing":
        winner_id = uuid4()
    else:
        winner = await _add(session, fm, "The on-call rotation hands over on Mondays.", _vec(0.5))
        if winner_state == "inactive":
            winner.active = False
        else:
            winner.superseded_by = loser.id
        await session.flush()
        winner_id = winner.id

    assert await fm.apply_supersession(winner_id, loser.id, session) is False
    assert loser.active is True and loser.superseded_by is None


# ---------------------------------------------------------------------------
# Sleep-phase merges (F031 / F027): real Postgres rows under a private agent
# ---------------------------------------------------------------------------


class _Agent:
    """Point ``heart`` at a fresh agent id for one test; restore and clean up after."""

    def __init__(self, heart):
        self.heart = heart
        self.agent_id = f"test-pr-d-{uuid4()}"

    async def __aenter__(self):
        self._orig = (self.heart.agent_id, self.heart.facts.agent_id)
        self.heart.agent_id = self.agent_id
        self.heart.facts.agent_id = self.agent_id
        return self

    async def __aexit__(self, *exc):
        self.heart.agent_id, self.heart.facts.agent_id = self._orig
        async with self.heart.db.session() as s:
            for model in (FactEntityKey, GraphEdge, Event, Fact):
                await s.execute(delete(model).where(model.agent_id == self.agent_id))
            await s.commit()
        return False

    async def fact(
        self,
        content: str,
        *,
        subject: str = "pr-d subject",
        category: str | None = "technical",
        embedding: list[float] | None = None,
    ) -> Fact:
        """A committed active row, embedded like heart.learn would embed it."""
        row = Fact(
            id=uuid4(),
            agent_id=self.agent_id,
            content=content,
            subject=subject,
            category=category,
            embedding=embedding if embedding is not None else await self.heart.facts.embeddings.embed(content),
            active=True,
        )
        async with self.heart.db.session() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
        return row

    async def row(self, fact_id) -> Fact:
        async with self.heart.db.session() as s:
            return await s.get(Fact, fact_id)

    async def count(self, content: str) -> int:
        async with self.heart.db.session() as s:
            found = await s.execute(
                select(func.count()).select_from(Fact).where(Fact.agent_id == self.agent_id, Fact.content == content)
            )
            return found.scalar_one()


def _handler(heart):
    from nous.handlers.sleep_handler import SleepHandler

    handler = SleepHandler(AsyncMock(), heart, heart.settings, MagicMock(), llm_client=MagicMock())
    handler._emit_action_event = MagicMock()
    return handler


def _pair(a, b) -> dict:
    return {
        "fact1_id": a.id,
        "fact2_id": b.id,
        "content1": a.content,
        "content2": b.content,
        "date1": a.learned_at,
        "date2": b.learned_at,
        "subject": a.subject,
        "category": a.category,
        "similarity": 0.85,
    }


def _merge(content: str) -> dict:
    return {"action": "MERGE", "confidence": 0.9, "reason": "complementary", "merged_content": content}


def _outcomes(handler, event_type: str) -> list[str]:
    return [
        c.args[1].get("outcome")
        for c in handler._emit_action_event.call_args_list
        if c.args and c.args[0] == event_type
    ]


@pytest.mark.postgres_only
async def test_f031_skips_a_pair_an_earlier_verdict_already_resolved(heart, monkeypatch):
    async with _Agent(heart) as agent:
        a = await agent.fact("The build cache lives on the shared NFS volume.")
        b = await agent.fact("The build cache lives on the local SSD of each runner.")
        c = await agent.fact("The build cache lives in the object store bucket.")
        heart.find_contradiction_candidates = AsyncMock(return_value=[_pair(a, b), _pair(b, c)])
        llm = AsyncMock(
            side_effect=[
                {"action": "SUPERSEDE_B", "confidence": 0.9, "reason": "A is current"},
                {"action": "SUPERSEDE_A", "confidence": 0.9, "reason": "C is current"},
            ]
        )
        monkeypatch.setattr("nous.handlers.sleep_handler.call_background_llm_structured", llm)
        try:
            assert await _handler(heart)._phase_resolve_contradictions({"facts_created": 0}) is True
        finally:
            del heart.find_contradiction_candidates

        assert llm.await_count == 1
        assert (await agent.row(b.id)).superseded_by == a.id
        assert (await agent.row(c.id)).active is True


@pytest.mark.postgres_only
async def test_f031_never_merges_a_tier1_fact(heart, monkeypatch):
    async with _Agent(heart) as agent:
        person = await agent.fact("Tim prefers to be called by his first name only.", category="person")
        other = await agent.fact("Tim's team calls the weekly sync the Tuesday huddle.")
        heart.find_contradiction_candidates = AsyncMock(return_value=[_pair(person, other)])
        merged = "Tim goes by his first name and his team holds a Tuesday huddle."
        monkeypatch.setattr(
            "nous.handlers.sleep_handler.call_background_llm_structured", AsyncMock(return_value=_merge(merged))
        )
        try:
            await _handler(heart)._phase_resolve_contradictions({"facts_created": 0})
        finally:
            del heart.find_contradiction_candidates

        assert (await agent.row(person.id)).active is True
        assert (await agent.row(other.id)).active is True
        assert await agent.count(merged) == 0


@pytest.mark.postgres_only
async def test_f031_still_supersedes_a_tier1_fact(heart, monkeypatch):
    async with _Agent(heart) as agent:
        old = await agent.fact("Tim's timezone is US Eastern for all scheduling.", category="person")
        new = await agent.fact("Tim's timezone is US Pacific for all scheduling.", category="person")
        heart.find_contradiction_candidates = AsyncMock(return_value=[_pair(old, new)])
        monkeypatch.setattr(
            "nous.handlers.sleep_handler.call_background_llm_structured",
            AsyncMock(return_value={"action": "SUPERSEDE_A", "confidence": 0.9, "reason": "B is later"}),
        )
        try:
            await _handler(heart)._phase_resolve_contradictions({"facts_created": 0})
        finally:
            del heart.find_contradiction_candidates

        assert (await agent.row(old.id)).superseded_by == new.id


@pytest.mark.postgres_only
async def test_f031_merge_that_confirms_a_third_fact_is_aborted(heart, monkeypatch):
    async with _Agent(heart) as agent:
        a = await agent.fact("The metrics pipeline samples every ten seconds.")
        b = await agent.fact("The metrics pipeline keeps raw samples for a week.")
        third = await agent.fact("The metrics pipeline samples every 10s and keeps raw data 7 days.")
        heart.find_contradiction_candidates = AsyncMock(return_value=[_pair(a, b)])
        monkeypatch.setattr(
            "nous.handlers.sleep_handler.call_background_llm_structured",
            AsyncMock(return_value=_merge(third.content)),
        )
        handler = _handler(heart)
        stats: dict = {"facts_created": 0}
        try:
            await handler._phase_resolve_contradictions(stats)
        finally:
            del heart.find_contradiction_candidates

        for source in (a, b):
            row = await agent.row(source.id)
            assert row.active is True and row.superseded_by is None
        assert (await agent.row(third.id)).confirmation_count == 0
        assert stats["contradictions_resolved"] == 0
        assert _outcomes(handler, "f031_merge") == ["merge_confirmed_third_fact"]


@pytest.mark.postgres_only
async def test_f027_cluster_merge_that_confirms_a_third_fact_is_aborted(heart, monkeypatch):
    async with _Agent(heart) as agent:
        members = [
            await agent.fact(f"Cluster member {i}: the ingest worker retries {i} times.", subject="ingest worker")
            for i in range(3)
        ]
        third = await agent.fact("The ingest worker retries up to three times.", subject="ingest retries")
        monkeypatch.setattr(
            "nous.handlers.sleep_handler.call_background_llm_structured",
            AsyncMock(return_value={"should_merge": True, "merged_content": third.content, "confidence": 0.9}),
        )
        handler = _handler(heart)
        stats: dict = {}
        assert await handler._phase_cluster_consolidation(stats) is True

        for member in members:
            row = await agent.row(member.id)
            assert row.active is True and row.superseded_by is None
        assert (await agent.row(third.id)).confirmation_count == 0
        assert stats["clusters_merged"] == 0
        assert _outcomes(handler, "f027_cluster_merge") == ["merge_confirmed_third_fact"]


# ---------------------------------------------------------------------------
# A merge is one transaction
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_a_failed_f031_merge_leaves_no_merged_fact(heart, monkeypatch):
    async with _Agent(heart) as agent:
        a = await agent.fact("The search index rebuilds every Sunday night.")
        b = await agent.fact("The search index rebuild takes about forty minutes.")
        merged = "The search index rebuilds every Sunday night and takes about forty minutes."
        heart.find_contradiction_candidates = AsyncMock(return_value=[_pair(a, b)])
        monkeypatch.setattr(
            "nous.handlers.sleep_handler.call_background_llm_structured", AsyncMock(return_value=_merge(merged))
        )
        monkeypatch.setattr(heart.facts, "inherit_conflict_slot_keys", AsyncMock(side_effect=RuntimeError("boom")))
        try:
            await _handler(heart)._phase_resolve_contradictions({"facts_created": 0})
        finally:
            del heart.find_contradiction_candidates

        assert await agent.count(merged) == 0
        assert (await agent.row(a.id)).active is True
        assert (await agent.row(b.id)).active is True


@pytest.mark.postgres_only
async def test_a_failed_f027_merge_leaves_no_merged_fact(heart, monkeypatch):
    async with _Agent(heart) as agent:
        members = [
            await agent.fact(f"Cluster member {i}: the export job writes shard {i}.", subject="export job")
            for i in range(3)
        ]
        merged = "The export job writes shards 0, 1 and 2."
        monkeypatch.setattr(
            "nous.handlers.sleep_handler.call_background_llm_structured",
            AsyncMock(return_value={"should_merge": True, "merged_content": merged, "confidence": 0.9}),
        )
        monkeypatch.setattr(heart.facts, "inherit_conflict_slot_keys", AsyncMock(side_effect=RuntimeError("boom")))
        await _handler(heart)._phase_cluster_consolidation({})

        assert await agent.count(merged) == 0
        for member in members:
            assert (await agent.row(member.id)).active is True


# ---------------------------------------------------------------------------
# A merge never points its sources at a merged fact that is already inactive
# ---------------------------------------------------------------------------


def _merged_fact_loses_to(heart, winner_id):
    """Stand-in contradiction scan that makes the new (merged) fact lose."""

    async def lose(
        embedding, new_content, exclude_ids, session, new_fact_id=None, new_event_date=None, new_interval=None
    ):
        assert await heart.facts.apply_supersession(winner_id, new_fact_id, session)
        return None

    return lose


@pytest.mark.postgres_only
async def test_f031_merge_whose_merged_fact_lost_is_aborted(heart, monkeypatch):
    async with _Agent(heart) as agent:
        a = await agent.fact("The alert router pages the primary on-call first.")
        b = await agent.fact("The alert router escalates after fifteen minutes.")
        winner = await agent.fact("The alert router config lives in the ops repository.", subject="router config")
        merged = "The alert router pages the primary on-call and escalates after fifteen minutes."
        heart.find_contradiction_candidates = AsyncMock(return_value=[_pair(a, b)])
        monkeypatch.setattr(
            "nous.handlers.sleep_handler.call_background_llm_structured", AsyncMock(return_value=_merge(merged))
        )
        monkeypatch.setattr(heart.facts, "_find_contradiction", _merged_fact_loses_to(heart, winner.id))
        handler = _handler(heart)
        stats: dict = {"facts_created": 0}
        try:
            await handler._phase_resolve_contradictions(stats)
        finally:
            del heart.find_contradiction_candidates

        for source in (a, b):
            row = await agent.row(source.id)
            assert row.active is True and row.superseded_by is None
        assert await agent.count(merged) == 0
        assert stats["contradictions_resolved"] == 0
        assert _outcomes(handler, "f031_merge") == ["merge_target_inactive"]


@pytest.mark.postgres_only
async def test_f027_merge_whose_merged_fact_lost_is_aborted(heart, monkeypatch):
    async with _Agent(heart) as agent:
        members = [
            await agent.fact(f"Cluster member {i}: the backup job keeps {i + 7} daily copies.", subject="backup job")
            for i in range(3)
        ]
        winner = await agent.fact("The backup job config lives in the ops repository.", subject="backup config")
        merged = "The backup job keeps between seven and nine daily copies."
        monkeypatch.setattr(
            "nous.handlers.sleep_handler.call_background_llm_structured",
            AsyncMock(return_value={"should_merge": True, "merged_content": merged, "confidence": 0.9}),
        )
        monkeypatch.setattr(heart.facts, "_find_contradiction", _merged_fact_loses_to(heart, winner.id))
        handler = _handler(heart)
        stats: dict = {}
        assert await handler._phase_cluster_consolidation(stats) is True

        for member in members:
            row = await agent.row(member.id)
            assert row.active is True and row.superseded_by is None
        assert await agent.count(merged) == 0
        assert stats["clusters_merged"] == 0
        assert _outcomes(handler, "f027_cluster_merge") == ["merge_target_inactive"]


# ---------------------------------------------------------------------------
# A tier-1 MERGE downgraded to KEEP_BOTH cools the pair down like a KEEP_BOTH
# ---------------------------------------------------------------------------


async def _wait_for_event(heart, agent_id: str, event_type: str) -> None:
    """The F031 resolution event is written fire-and-forget."""
    for _ in range(200):
        async with heart.db.session() as s:
            found = await s.execute(
                select(func.count())
                .select_from(Event)
                .where(Event.agent_id == agent_id, Event.event_type == event_type)
            )
            if found.scalar_one():
                return
        await asyncio.sleep(0.02)
    raise AssertionError(f"no {event_type} event was written")


def _persisting_handler(heart, agent):
    """A handler whose events land in nous_system.events, as Brain.emit_event writes them."""
    from nous.handlers.sleep_handler import SleepHandler

    async def emit_event(event_type, data):
        async with heart.db.session() as s:
            s.add(Event(agent_id=agent.agent_id, event_type=event_type, data=data))
            await s.commit()

    brain = SimpleNamespace(emit_event=emit_event)
    return SleepHandler(brain, heart, heart.settings, MagicMock(), llm_client=MagicMock())


@pytest.mark.postgres_only
async def test_a_tier1_merge_downgrade_is_not_rejudged_next_cycle(heart, monkeypatch):
    monkeypatch.setattr(heart.facts._settings, "contradiction_recheck_cooldown_days", 30)
    async with _Agent(heart) as agent:
        await agent.fact("Tim prefers short written updates over calls.", category="person", embedding=ANCHOR)
        await agent.fact("Tim's team sends its written updates every Friday.", embedding=_vec(0.85))
        llm = AsyncMock(return_value=_merge("Tim prefers short written updates, which his team sends on Fridays."))
        monkeypatch.setattr("nous.handlers.sleep_handler.call_background_llm_structured", llm)
        handler = _persisting_handler(heart, agent)

        assert len(await heart.find_contradiction_candidates(limit=10)) == 1
        await handler._phase_resolve_contradictions({"facts_created": 0})
        assert llm.await_count == 1
        await _wait_for_event(heart, agent.agent_id, "f031_contradiction_resolution")

        assert await heart.find_contradiction_candidates(limit=10) == []
        await handler._phase_resolve_contradictions({"facts_created": 0})
        assert llm.await_count == 1  # skipped on the next cycle


# ---------------------------------------------------------------------------
# An aborted sleep merge cools down: the same content would abort again
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_an_aborted_f031_merge_is_not_rejudged_next_cycle(heart, monkeypatch):
    monkeypatch.setattr(heart.facts._settings, "contradiction_recheck_cooldown_days", 30)
    async with _Agent(heart) as agent:
        await agent.fact("The metrics pipeline samples every ten seconds.", embedding=ANCHOR)
        await agent.fact("The metrics pipeline keeps raw samples for a week.", embedding=_vec(0.85))
        third = await agent.fact("The metrics pipeline samples every 10s, keeps raw data 7 days.", subject="metrics")
        llm = AsyncMock(return_value=_merge(third.content))
        monkeypatch.setattr("nous.handlers.sleep_handler.call_background_llm_structured", llm)
        handler = _persisting_handler(heart, agent)

        assert len(await heart.find_contradiction_candidates(limit=10)) == 1
        await handler._phase_resolve_contradictions({"facts_created": 0})
        await asyncio.gather(*list(handler._pending_emits))
        await _wait_for_event(heart, agent.agent_id, "f031_contradiction_resolution")
        assert llm.await_count == 1

        assert await heart.find_contradiction_candidates(limit=10) == []
        await handler._phase_resolve_contradictions({"facts_created": 0})
        assert llm.await_count == 1  # skipped on the next cycle


@pytest.mark.postgres_only
async def test_an_aborted_f027_merge_is_skipped_until_the_cluster_changes(heart, monkeypatch):
    monkeypatch.setattr(heart.settings, "contradiction_recheck_cooldown_days", 30)
    async with _Agent(heart) as agent:
        for i in range(3):
            await agent.fact(f"Cluster member {i}: the ingest worker retries {i} times.", subject="ingest worker")
        third = await agent.fact("The ingest worker retries up to three times.", subject="ingest retries")
        llm = AsyncMock(return_value={"should_merge": True, "merged_content": third.content, "confidence": 0.9})
        monkeypatch.setattr("nous.handlers.sleep_handler.call_background_llm_structured", llm)
        handler = _persisting_handler(heart, agent)

        assert await handler._phase_cluster_consolidation({}) is True
        await asyncio.gather(*list(handler._pending_emits))
        assert llm.await_count == 1

        assert await handler._phase_cluster_consolidation({}) is True
        assert llm.await_count == 1  # skipped on the next cycle

        await agent.fact("Cluster member 3: the ingest worker retries 3 times.", subject="ingest worker")
        assert await handler._phase_cluster_consolidation({}) is True
        await asyncio.gather(*list(handler._pending_emits))
        assert llm.await_count == 2  # a cluster that changed size is judged again


# ---------------------------------------------------------------------------
# A refused F027 cluster gives up its slot until it changes or its TTL ends
# ---------------------------------------------------------------------------


REFUSE = {"should_merge": False, "refuse_reason": "entity collision", "confidence": 0.9}


def _judged(llm: AsyncMock) -> list[str]:
    """The subject of each cluster sent to the merge LLM, in call order."""
    return [re.search(r"^Subject: (.+)$", c.kwargs["user_message"], re.M).group(1) for c in llm.call_args_list]


async def _run_cycle(handler) -> None:
    assert await handler._phase_cluster_consolidation({}) is True
    await asyncio.gather(*list(handler._pending_emits))


@pytest.mark.postgres_only
async def test_a_refused_f027_cluster_gives_its_slot_to_the_next_until_it_changes(heart, monkeypatch):
    async with _Agent(heart) as agent:
        subjects = {size: f"refused cluster of {size}" for size in (8, 7, 6, 5, 4, 3)}
        for size, subject in subjects.items():
            for i in range(size):
                await agent.fact(f"Member {i} of the {subject}: detail {i}.", subject=subject)
        llm = AsyncMock(return_value=REFUSE)
        monkeypatch.setattr("nous.handlers.sleep_handler.call_background_llm_structured", llm)
        handler = _persisting_handler(heart, agent)

        await _run_cycle(handler)
        assert _judged(llm) == [subjects[s] for s in (8, 7, 6, 5, 4)]

        await _run_cycle(handler)
        assert _judged(llm)[5:] == [subjects[3]]  # the five refused clusters gave up their slots

        await agent.fact(f"Member 8 of the {subjects[8]}: detail 8.", subject=subjects[8])
        await _run_cycle(handler)
        assert _judged(llm)[6:] == [subjects[8]]  # a refused cluster that changed size is judged again


@pytest.mark.postgres_only
async def test_a_refused_f027_cluster_is_judged_again_after_its_ttl(heart, monkeypatch):
    # The abort cooldown stays at 30 days: a refusal has its own, shorter window.
    monkeypatch.setattr(heart.settings, "contradiction_recheck_cooldown_days", 30)
    async with _Agent(heart) as agent:
        for i in range(3):
            await agent.fact(f"Cluster member {i}: the deploy hook pings channel {i}.", subject="deploy hook")
        llm = AsyncMock(return_value=REFUSE)
        monkeypatch.setattr("nous.handlers.sleep_handler.call_background_llm_structured", llm)
        handler = _persisting_handler(heart, agent)

        await _run_cycle(handler)
        await _run_cycle(handler)
        assert llm.await_count == 1  # skipped inside the TTL

        async with heart.db.session() as s:
            await s.execute(
                update(Event)
                .where(Event.agent_id == agent.agent_id)
                .values(created_at=datetime.now(UTC) - timedelta(days=heart.settings.cluster_refusal_ttl_days + 1))
            )
            await s.commit()
        await _run_cycle(handler)
        assert llm.await_count == 2  # judged again once the refusal is older than the TTL


# ---------------------------------------------------------------------------
# A sub-band REFINEMENT's tiebreaker call spends its own band-budget slot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cap, tiebreaker_called", [(1, False), (2, True)])
async def test_a_sub_band_refinement_spends_a_second_band_budget_slot(cap, tiebreaker_called):
    fm = FactManager(
        db=MagicMock(),
        embeddings=None,
        agent_id="test",
        settings=SimpleNamespace(fact_band_classification_max_per_hour=cap),
    )
    _classifier(fm, {"relation": "REFINEMENT", "current_fact": "new", "confidence": 0.9})
    fm.is_distinct_fact = AsyncMock(return_value=True)
    dupe = SimpleNamespace(content="The nightly backup writes to the archive bucket.")

    action = await fm._classify_dupe_in_band(
        dupe, 0.82, FactInput(content="The nightly backup writes to the archive bucket in eu-west-2."), True
    )

    assert fm.is_distinct_fact.await_count == int(tiebreaker_called)
    assert action == ("refines" if tiebreaker_called else None)


# ---------------------------------------------------------------------------
# fact_learned on a caller's session is published only after that commit
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_fact_learned_on_an_injected_session_is_published_after_commit(heart, monkeypatch):
    monkeypatch.setattr(heart.facts, "_admission_controller", None)
    seen: list[tuple[str, bool]] = []

    class _Bus:
        async def emit(self, event):
            async with heart.db.session() as other:  # another connection
                visible = await other.get(Fact, UUID(event.data["fact_id"])) is not None
            seen.append((event.type, visible))

    monkeypatch.setattr(heart, "_bus", _Bus())
    async with _Agent(heart):
        async with heart.db.session() as s:
            await heart.learn(FactInput(content="The release checklist now lives in the ops wiki."), session=s)
            await asyncio.sleep(0)
            assert seen == []
            await s.commit()
        await asyncio.gather(*list(heart._pending_emits))
        assert seen == [("fact_learned", True)]

        async with heart.db.session() as s:
            await heart.learn(FactInput(content="The rollback drill runs on the first Monday."), session=s)
            await s.rollback()
        await asyncio.sleep(0)
        assert not heart._pending_emits
        assert seen == [("fact_learned", True)]


# ---------------------------------------------------------------------------
# Migration 095: the refines edge relation
# ---------------------------------------------------------------------------

_MIGRATIONS = Path(__file__).resolve().parents[1] / "sql" / "migrations"
_REFINES_SQL = (_MIGRATIONS / "095_edges_refines_relation.sql").read_text(encoding="utf-8")


def _relations(sql_text: str) -> set[str]:
    inside = re.search(r"relation IN \(([^)]*)\)", sql_text)
    assert inside is not None, sql_text
    return set(re.findall(r"'([a-z_]+)'", inside.group(1)))


def test_095_adds_refines_to_the_full_relation_list_and_the_orm_matches():
    statements = _split_sql_statements(_REFINES_SQL)
    assert len(statements) == 2
    assert statements[0].startswith("ALTER TABLE brain.graph_edges")
    assert "DROP CONSTRAINT IF EXISTS ck_edges_relation" in statements[0]
    assert statements[1].startswith("ALTER TABLE brain.graph_edges")
    assert "ADD CONSTRAINT ck_edges_relation" in statements[1]
    assert all(";" not in ln for ln in _REFINES_SQL.splitlines() if ln.lstrip().startswith("--"))

    previous = _relations((_MIGRATIONS / "055_cooccurrence_edges.sql").read_text(encoding="utf-8"))
    (constraint,) = [c for c in GraphEdge.__table__.constraints if c.name == "ck_edges_relation"]
    assert _relations(statements[1]) == _relations(str(constraint.sqltext)) == previous | {"refines"}


@pytest.mark.postgres_only
async def test_095_applies_twice_and_a_refines_edge_lands(session):
    # DDL is transactional in Postgres: the session fixture rolls all of it back.
    for _ in range(2):
        for statement in _split_sql_statements(_REFINES_SQL):
            await session.execute(text(statement))
    async with session.begin_nested():
        session.add(
            GraphEdge(
                source_id=uuid4(),
                target_id=uuid4(),
                source_type="fact",
                target_type="fact",
                agent_id=f"test-pr-d-{uuid4()}",
                relation="refines",
                weight=0.8,
            )
        )


# ---------------------------------------------------------------------------
# An UPDATES: prefix never supersedes a tier-1 fact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", ["person", "preference", "rule"])
async def test_updates_prefix_learns_new_instead_of_superseding_tier1(category):
    from nous.handlers.sleep_handler import SleepHandler

    match = SimpleNamespace(
        id=uuid4(), content="Tim's timezone is EST", category=category, subject="Tim timezone", score=0.95
    )
    heart = AsyncMock()
    heart.find_similar_facts = AsyncMock(return_value=[match])
    heart.supersede_fact = AsyncMock()
    heart.learn = AsyncMock(return_value=MagicMock())
    handler = SleepHandler(AsyncMock(), heart, MagicMock(), MagicMock(), llm_client=None)
    stats = {"facts_created": 0}

    ok = await handler._handle_updates_prefix(
        "UPDATES: Tim's timezone is EST",
        {"content": "The deploy runbook now lives in the ops wiki.", "category": "technical"},
        stats,
    )

    assert ok is True
    heart.supersede_fact.assert_not_called()
    heart.learn.assert_awaited_once()
    assert stats["facts_created"] == 1
