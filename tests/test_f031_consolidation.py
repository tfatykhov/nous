"""Tests for F031 Consolidation Orient & Resolve.

Covers:
- find_contradiction_candidates() query
- Orient context injection in sleep reflection
- Contradiction resolution phase
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.events import Event, EventBus
from nous.heart.schemas import FactInput, FactRejected


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    event_type: str = "sleep_started",
    agent_id: str = "test-agent",
    data: dict | None = None,
    session_id: str | None = "sess-1",
) -> Event:
    return Event(type=event_type, agent_id=agent_id, data=data or {}, session_id=session_id)


def _mock_settings(**overrides) -> MagicMock:
    s = MagicMock()
    s.background_model = "claude-sonnet-4-5-20250514"
    s.anthropic_api_key = "sk-ant-test-key"
    s.anthropic_auth_token = ""
    s.agent_id = "test-agent"
    s.sleep_enabled = True
    s.consolidation_audit_enabled = False  # F035.6: match real default
    # 282626c memory-fidelity settings: sleep_handler slices with these ints;
    # bare MagicMock attrs raise TypeError and kill the phase.
    s.sleep_reflection_summary_chars = 500
    s.sleep_contradiction_fact_chars = 1000
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _mock_llm_client(text: str = "", status_code: int = 200) -> AsyncMock:
    """Create mock LLM client.

    When text is valid JSON, returns tool_use format (for call_background_llm_structured).
    Otherwise returns text format (for call_background_llm).
    """
    client = AsyncMock()
    if status_code == 200:
        response = MagicMock()
        # Try to parse as JSON for structured responses (tool_use format)
        try:
            parsed = json.loads(text)
            response.content = [{"type": "tool_use", "id": "test", "name": "store_reflection", "input": parsed}]
        except (json.JSONDecodeError, TypeError):
            response.content = [{"type": "text", "text": text}]
        client.call = AsyncMock(return_value=response)
    else:
        client.call = AsyncMock(side_effect=RuntimeError(f"API error ({status_code})"))
    return client


def _make_sleep_handler(brain=None, heart=None, settings=None, bus=None, llm_client=None):
    from nous.handlers.sleep_handler import SleepHandler

    brain = brain or AsyncMock()
    heart = heart or AsyncMock()
    settings = settings or _mock_settings()
    bus = bus or MagicMock(spec=EventBus)
    bus.on = MagicMock()
    bus.emit = AsyncMock()
    llm_client = llm_client or _mock_llm_client()
    handler = SleepHandler(brain, heart, settings, bus, llm_client)
    return handler, brain, heart, bus, llm_client


# ===========================================================================
# Task 1: find_contradiction_candidates
# ===========================================================================

class TestFindContradictionCandidates:
    """FactManager.find_contradiction_candidates() returns same-subject, high-similarity pairs."""

    @pytest.mark.asyncio
    async def test_method_exists_and_callable(self):
        """find_contradiction_candidates should exist on Heart and be callable."""
        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[])
        result = await heart.find_contradiction_candidates(limit=10)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_dict_structure(self):
        """Results should have the expected dict keys."""
        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[{
            "fact1_id": uuid4(),
            "fact2_id": uuid4(),
            "content1": "Tim's timezone is EST",
            "content2": "Tim's timezone is PST",
            "date1": "2026-03-01",
            "date2": "2026-03-15",
            "similarity": 0.88,
        }])
        result = await heart.find_contradiction_candidates(limit=10)
        assert len(result) == 1
        pair = result[0]
        assert "fact1_id" in pair
        assert "fact2_id" in pair
        assert "content1" in pair
        assert "content2" in pair
        assert "date1" in pair
        assert "date2" in pair
        assert "similarity" in pair
        assert pair["similarity"] > 0.75

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        """Should return at most `limit` pairs."""
        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[
            {"fact1_id": uuid4(), "fact2_id": uuid4(), "content1": "A", "content2": "B",
             "date1": "2026-03-01", "date2": "2026-03-15", "similarity": 0.88},
        ])
        result = await heart.find_contradiction_candidates(limit=1)
        assert len(result) <= 1

    @pytest.mark.asyncio
    async def test_empty_when_no_candidates(self):
        """Returns empty list when no matching pairs exist."""
        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[])
        result = await heart.find_contradiction_candidates(limit=10)
        assert result == []

    @pytest.mark.asyncio
    async def test_recheck_cooldown_clause_gated(self):
        """F031 re-check cooldown (2026-06-14): the candidate SQL skips pairs
        with a recent GENUINE keep-both resolution when cooldown_days>0, and
        omits the clause at 0. raw_action='KEEP_BOTH' so truncation-downgraded
        merges still retry."""
        from types import SimpleNamespace

        from nous.heart.facts import FactManager

        captured: dict = {}

        class _EmptyResult:
            def all(self):
                return []

            def __iter__(self):
                return iter([])

        async def _exec(sql, params):
            captured["sql"] = str(sql)
            captured["params"] = dict(params)
            return _EmptyResult()

        session = SimpleNamespace(execute=_exec)
        mgr = FactManager.__new__(FactManager)
        mgr.agent_id = "a"

        mgr._settings = SimpleNamespace(contradiction_recheck_cooldown_days=30)
        await mgr._find_contradiction_candidates(10, session)
        assert "NOT EXISTS" in captured["sql"]
        assert "raw_action" in captured["sql"] and "KEEP_BOTH" in captured["sql"]
        assert captured["params"].get("cooldown_days") == 30

        mgr._settings = SimpleNamespace(contradiction_recheck_cooldown_days=0)
        await mgr._find_contradiction_candidates(10, session)
        assert "NOT EXISTS" not in captured["sql"]
        assert "cooldown_days" not in captured["params"]


# ===========================================================================
# Task 2: Orient context in sleep reflection
# ===========================================================================

class TestOrientContext:
    """Sleep reflection injects existing facts into the prompt."""

    @pytest.mark.asyncio
    async def test_search_facts_called_for_orient_context(self):
        """Reflection should search for existing facts based on episode content."""
        ep1 = MagicMock()
        ep1.summary = "Discussed Tim's preference for Celsius temperature display"
        ep2 = MagicMock()
        ep2.summary = "Worked on the database migration for PostgreSQL upgrade"

        heart = AsyncMock()
        heart.list_episodes = AsyncMock(return_value=[ep1, ep2])
        heart.search_facts = AsyncMock(return_value=[])
        heart.learn = AsyncMock(return_value=MagicMock())

        llm_response = json.dumps({
            "patterns": [], "lessons": [], "connections": [], "gaps": [],
            "summary": "Productive day",
            "facts": [{"subject": "test", "content": "test fact", "category": "concept"}],
        })
        handler, _, _, bus, _ = _make_sleep_handler(
            heart=heart, llm_client=_mock_llm_client(llm_response)
        )
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_reflect(sleep_stats)

        assert heart.search_facts.call_count >= 1

    @pytest.mark.asyncio
    async def test_existing_facts_injected_into_prompt(self):
        """The LLM prompt should contain existing facts as orient context."""
        ep1 = MagicMock()
        ep1.summary = "Discussed temperature preferences"

        existing_fact = MagicMock()
        existing_fact.id = uuid4()
        existing_fact.content = "Tim prefers Celsius for temperature"
        existing_fact.category = "preference"

        heart = AsyncMock()
        heart.list_episodes = AsyncMock(return_value=[ep1, ep1])
        heart.search_facts = AsyncMock(return_value=[existing_fact])
        heart.learn = AsyncMock(return_value=MagicMock())

        captured_payloads = []
        async def capture_call(payload):
            captured_payloads.append(payload)
            response = MagicMock()
            response.content = [{"type": "text", "text": json.dumps({
                "patterns": [], "lessons": [], "connections": [], "gaps": [],
                "summary": "Day summary", "facts": [],
            })}]
            return response

        llm_client = AsyncMock()
        llm_client.call = capture_call

        handler, _, _, bus, _ = _make_sleep_handler(heart=heart, llm_client=llm_client)
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_reflect(sleep_stats)

        assert len(captured_payloads) >= 1
        user_text = captured_payloads[0]["messages"][0]["content"][0]["text"]
        assert "Tim prefers Celsius" in user_text
        assert "EXISTING KNOWLEDGE" in user_text

    @pytest.mark.asyncio
    async def test_no_orient_context_when_no_existing_facts(self):
        """When search_facts returns nothing, prompt should not have EXISTING KNOWLEDGE."""
        ep1 = MagicMock()
        ep1.summary = "Discussed completely novel topic"

        heart = AsyncMock()
        heart.list_episodes = AsyncMock(return_value=[ep1, ep1])
        heart.search_facts = AsyncMock(return_value=[])
        heart.learn = AsyncMock(return_value=MagicMock())

        captured_payloads = []
        async def capture_call(payload):
            captured_payloads.append(payload)
            response = MagicMock()
            response.content = [{"type": "text", "text": json.dumps({
                "patterns": [], "lessons": [], "connections": [], "gaps": [],
                "summary": "Novel day", "facts": [],
            })}]
            return response

        llm_client = AsyncMock()
        llm_client.call = capture_call

        handler, _, _, bus, _ = _make_sleep_handler(heart=heart, llm_client=llm_client)
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_reflect(sleep_stats)

        assert len(captured_payloads) >= 1
        user_text = captured_payloads[0]["messages"][0]["content"][0]["text"]
        assert "EXISTING KNOWLEDGE" not in user_text

    @pytest.mark.asyncio
    async def test_updates_prefix_triggers_supersession(self):
        """When LLM returns 'UPDATES: ...' subject, supersede_fact is called."""
        ep1 = MagicMock()
        ep1.summary = "Tim changed timezone preference"

        existing_fact = MagicMock()
        existing_fact.id = uuid4()
        existing_fact.content = "Tim's timezone is EST"
        existing_fact.category = "person"
        existing_fact.subject = "Tim timezone"
        existing_fact.score = 0.95  # Above threshold

        heart = AsyncMock()
        heart.list_episodes = AsyncMock(return_value=[ep1, ep1])
        heart.search_facts = AsyncMock(return_value=[existing_fact])
        # HD-2: UPDATES supersession now probes find_similar_facts (raw cosine).
        heart.find_similar_facts = AsyncMock(return_value=[existing_fact])
        heart.supersede_fact = AsyncMock(return_value=MagicMock())
        heart.learn = AsyncMock(return_value=MagicMock())

        llm_response = json.dumps({
            "patterns": [], "lessons": [], "connections": [], "gaps": [],
            "summary": "Timezone update",
            "facts": [{
                "subject": "UPDATES: Tim's timezone is EST",
                "content": "Tim's timezone is PST",
                "category": "person",
            }],
        })
        handler, _, _, bus, _ = _make_sleep_handler(
            heart=heart, llm_client=_mock_llm_client(llm_response)
        )
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_reflect(sleep_stats)

        heart.supersede_fact.assert_called_once()
        call_args = heart.supersede_fact.call_args
        assert call_args[0][0] == existing_fact.id

    @pytest.mark.asyncio
    async def test_updates_prefix_case_insensitive(self):
        """UPDATES prefix should work regardless of case."""
        ep1 = MagicMock()
        ep1.summary = "Test episode"

        existing_fact = MagicMock()
        existing_fact.id = uuid4()
        existing_fact.content = "Old fact"
        existing_fact.category = "concept"
        existing_fact.subject = "test"
        existing_fact.score = 0.90

        heart = AsyncMock()
        heart.list_episodes = AsyncMock(return_value=[ep1, ep1])
        heart.search_facts = AsyncMock(return_value=[existing_fact])
        # HD-2: UPDATES supersession now probes find_similar_facts (raw cosine).
        heart.find_similar_facts = AsyncMock(return_value=[existing_fact])
        heart.supersede_fact = AsyncMock(return_value=MagicMock())
        heart.learn = AsyncMock(return_value=MagicMock())

        llm_response = json.dumps({
            "patterns": [], "lessons": [], "connections": [], "gaps": [],
            "summary": "Test",
            "facts": [{
                "subject": "updates: Old fact",
                "content": "New fact content",
                "category": "concept",
            }],
        })
        handler, _, _, bus, _ = _make_sleep_handler(
            heart=heart, llm_client=_mock_llm_client(llm_response)
        )
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_reflect(sleep_stats)

        heart.supersede_fact.assert_called_once()

    @pytest.mark.asyncio
    async def test_updates_below_threshold_learns_as_new(self):
        """When UPDATES match score is below 0.80, learn as new fact instead."""
        ep1 = MagicMock()
        ep1.summary = "Test episode"

        low_match = MagicMock()
        low_match.id = uuid4()
        low_match.content = "Barely related fact"
        low_match.category = "concept"
        low_match.subject = "something"
        low_match.score = 0.50  # Below threshold

        heart = AsyncMock()
        heart.list_episodes = AsyncMock(return_value=[ep1, ep1])
        heart.search_facts = AsyncMock(return_value=[low_match])
        # HD-2: UPDATES supersession now probes find_similar_facts (raw cosine).
        heart.find_similar_facts = AsyncMock(return_value=[low_match])
        heart.supersede_fact = AsyncMock()
        heart.learn = AsyncMock(return_value=MagicMock())

        llm_response = json.dumps({
            "patterns": [], "lessons": [], "connections": [], "gaps": [],
            "summary": "Test",
            "facts": [{
                "subject": "UPDATES: Barely related fact",
                "content": "New fact content",
                "category": "concept",
            }],
        })
        handler, _, _, bus, _ = _make_sleep_handler(
            heart=heart, llm_client=_mock_llm_client(llm_response)
        )
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_reflect(sleep_stats)

        heart.supersede_fact.assert_not_called()
        heart.learn.assert_called()  # Learned as new fact instead


# ===========================================================================
# Task 3: Contradiction resolution phase
# ===========================================================================

class TestContradictionResolution:
    """Phase 4.5: resolve accumulated contradictions during sleep."""

    @pytest.mark.asyncio
    @staticmethod
    def _heart_with_session(loser_id, winner_id, candidate):
        """2a (PR #520): SUPERSEDE now preserves the chain in-session, so mock a
        real session CM + link_facts. orm.superseded_by=None so the race guard
        proceeds."""
        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[candidate])
        heart.deactivate_fact = AsyncMock()
        heart.search_facts = AsyncMock(return_value=[])
        heart.link_facts = AsyncMock()
        orm = MagicMock()
        orm.superseded_by = None
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=orm)
        mock_session.commit = AsyncMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_session)
        cm.__aexit__ = AsyncMock(return_value=False)
        heart.db = MagicMock()
        heart.db.session = MagicMock(return_value=cm)
        return heart, orm

    async def test_supersede_a_preserves_chain(self):
        """SUPERSEDE_A: loser=fact1 superseded_by winner=fact2, + supersedes edge."""
        fact1_id, fact2_id = uuid4(), uuid4()
        heart, orm = self._heart_with_session(fact1_id, fact2_id, {
            "fact1_id": fact1_id, "fact2_id": fact2_id,
            "content1": "Tim's timezone is EST", "content2": "Tim's timezone is PST",
            "date1": "2026-03-01", "date2": "2026-03-15", "similarity": 0.88,
        })
        handler, *_ = _make_sleep_handler(
            heart=heart,
            llm_client=_mock_llm_client(json.dumps(
                {"action": "SUPERSEDE_A", "confidence": 0.9, "reason": "B newer"})),
        )
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_resolve_contradictions(sleep_stats)
        assert result is True
        assert orm.superseded_by == fact2_id and orm.active is False
        heart.link_facts.assert_awaited_once()
        args = heart.link_facts.await_args.args
        assert args[0] == fact2_id and args[1] == fact1_id and args[2] == "supersedes"
        heart.deactivate_fact.assert_not_called()
        assert sleep_stats["contradictions_resolved"] == 1

    @pytest.mark.asyncio
    async def test_supersede_b_preserves_chain(self):
        """SUPERSEDE_B inverts: loser=fact2, winner=fact1."""
        fact1_id, fact2_id = uuid4(), uuid4()
        heart, orm = self._heart_with_session(fact2_id, fact1_id, {
            "fact1_id": fact1_id, "fact2_id": fact2_id,
            "content1": "Correct info", "content2": "Outdated info",
            "date1": "2026-03-01", "date2": "2026-03-15", "similarity": 0.85,
        })
        handler, *_ = _make_sleep_handler(
            heart=heart,
            llm_client=_mock_llm_client(json.dumps(
                {"action": "SUPERSEDE_B", "confidence": 0.85, "reason": "A correct"})),
        )
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_resolve_contradictions(sleep_stats)
        assert orm.superseded_by == fact1_id
        args = heart.link_facts.await_args.args
        assert args[0] == fact1_id and args[1] == fact2_id and args[2] == "supersedes"
        heart.deactivate_fact.assert_not_called()

    @pytest.mark.asyncio
    async def test_merge_supersedes_both_originals(self):
        """MERGE should learn merged fact and link both originals via superseded_by.

        Codex P1 on PR #411 caught that the prior implementation called
        ``deactivate_fact()`` on both originals, leaving them ``active=False``
        with no ``superseded_by`` link — orphaning the chain. PR #412 rewrote
        the branch to capture ``merged_detail.id`` and set ``superseded_by`` +
        ``active=False`` on both originals in a single session. This test
        verifies that runtime contract.
        """
        fact1_id = uuid4()
        fact2_id = uuid4()
        merged_id = uuid4()

        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[{
            "fact1_id": fact1_id,
            "fact2_id": fact2_id,
            "content1": "Tim uses EST",
            "content2": "Tim's hours are 9-5",
            "date1": "2026-03-01",
            "date2": "2026-03-15",
            "similarity": 0.82,
        }])
        merged_detail = MagicMock()
        merged_detail.id = merged_id
        heart.learn = AsyncMock(return_value=merged_detail)
        heart.deactivate_fact = AsyncMock()
        heart.search_facts = AsyncMock(return_value=[])

        # heart.db.session() returns an async context manager. AsyncMock alone
        # would yield a coroutine, not an __aenter__/__aexit__ object, so build
        # the CM explicitly. ORM mocks must have superseded_by=None explicitly
        # set or MagicMock auto-attr would defeat the race-guard's `is None`
        # check in sleep_handler.py.
        orm1 = MagicMock()
        orm1.superseded_by = None
        orm2 = MagicMock()
        orm2.superseded_by = None
        orm_facts = {fact1_id: orm1, fact2_id: orm2}
        mock_session = MagicMock()
        mock_session.get = AsyncMock(side_effect=lambda _model, fid: orm_facts.get(fid))
        mock_session.commit = AsyncMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_session)
        cm.__aexit__ = AsyncMock(return_value=False)
        heart.db = MagicMock()
        heart.db.session = MagicMock(return_value=cm)

        llm_response = json.dumps({
            "action": "MERGE",
            "confidence": 0.85,
            "reason": "Complementary timezone info",
            "merged_content": "Tim works in EST timezone, hours 9-5",
        })
        handler, _, _, bus, _ = _make_sleep_handler(
            heart=heart, llm_client=_mock_llm_client(llm_response)
        )
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_resolve_contradictions(sleep_stats)

        heart.learn.assert_called_once()
        # New contract: NO deactivate_fact() calls — supersession handled in-session.
        heart.deactivate_fact.assert_not_called()
        # Session entered exactly once and committed.
        heart.db.session.assert_called_once()
        mock_session.commit.assert_awaited_once()
        # Both originals fetched and linked to merged fact.
        assert mock_session.get.await_count == 2
        for orig_id, orm in orm_facts.items():
            assert orm.superseded_by == merged_id, (
                f"orig {orig_id} should be linked to merged_id"
            )
            assert orm.active is False
        assert sleep_stats["facts_created"] == 1
        assert sleep_stats["contradictions_resolved"] == 1

    @pytest.mark.asyncio
    async def test_merge_preserves_existing_supersede_link(self):
        """Codex P1 on PR #412: MERGE must NOT clobber a pre-existing
        ``superseded_by`` link. If a concurrent path superseded one of the
        candidate facts between selection and the MERGE write, the original
        chain target must win.
        """
        fact1_id = uuid4()
        fact2_id = uuid4()
        merged_id = uuid4()
        prior_supersede_target = uuid4()

        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[{
            "fact1_id": fact1_id,
            "fact2_id": fact2_id,
            "content1": "Tim uses EST",
            "content2": "Tim's hours are 9-5",
            "date1": "2026-03-01",
            "date2": "2026-03-15",
            "similarity": 0.82,
        }])
        merged_detail = MagicMock()
        merged_detail.id = merged_id
        heart.learn = AsyncMock(return_value=merged_detail)
        heart.deactivate_fact = AsyncMock()
        heart.search_facts = AsyncMock(return_value=[])

        # fact1 was concurrently superseded; fact2 is fresh.
        orm1 = MagicMock()
        orm1.superseded_by = prior_supersede_target
        orm1.active = False  # already inactive (paired invariant)
        orm2 = MagicMock()
        orm2.superseded_by = None
        orm2.active = True
        orm_facts = {fact1_id: orm1, fact2_id: orm2}
        mock_session = MagicMock()
        mock_session.get = AsyncMock(side_effect=lambda _model, fid: orm_facts.get(fid))
        mock_session.commit = AsyncMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_session)
        cm.__aexit__ = AsyncMock(return_value=False)
        heart.db = MagicMock()
        heart.db.session = MagicMock(return_value=cm)

        llm_response = json.dumps({
            "action": "MERGE",
            "confidence": 0.85,
            "reason": "Complementary timezone info",
            "merged_content": "Tim works in EST timezone, hours 9-5",
        })
        handler, _, _, _, _ = _make_sleep_handler(
            heart=heart, llm_client=_mock_llm_client(llm_response)
        )
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_resolve_contradictions(sleep_stats)

        # fact1: original chain target preserved, NOT overwritten
        assert orm1.superseded_by == prior_supersede_target, (
            "Race-guard must not clobber an already-populated superseded_by"
        )
        assert orm1.active is False  # unchanged

        # fact2: normal path, gets linked to merged_id
        assert orm2.superseded_by == merged_id
        assert orm2.active is False

        # commit still happens once (only fact2 was modified)
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_keep_both_takes_no_action(self):
        """KEEP_BOTH should not modify any facts."""
        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[{
            "fact1_id": uuid4(),
            "fact2_id": uuid4(),
            "content1": "Python is a language",
            "content2": "Python is for data science",
            "date1": "2026-03-01",
            "date2": "2026-03-15",
            "similarity": 0.80,
        }])
        heart.deactivate_fact = AsyncMock()
        heart.learn = AsyncMock()
        heart.search_facts = AsyncMock(return_value=[])

        llm_response = json.dumps({
            "action": "KEEP_BOTH",
            "confidence": 0.95,
            "reason": "Different information",
        })
        handler, _, _, bus, _ = _make_sleep_handler(
            heart=heart, llm_client=_mock_llm_client(llm_response)
        )
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_resolve_contradictions(sleep_stats)

        heart.deactivate_fact.assert_not_called()
        heart.learn.assert_not_called()
        assert sleep_stats["contradictions_found"] == 1
        assert sleep_stats.get("contradictions_resolved", 0) == 0

    @pytest.mark.asyncio
    async def test_remove_a_deactivates_fact(self):
        """REMOVE_A should deactivate fact1."""
        fact1_id = uuid4()

        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[{
            "fact1_id": fact1_id,
            "fact2_id": uuid4(),
            "content1": "Wrong fact",
            "content2": "Correct fact",
            "date1": "2026-03-01",
            "date2": "2026-03-15",
            "similarity": 0.85,
        }])
        heart.deactivate_fact = AsyncMock()
        heart.search_facts = AsyncMock(return_value=[])

        llm_response = json.dumps({
            "action": "REMOVE_A",
            "confidence": 0.9,
            "reason": "Fact A is stale",
        })
        handler, _, _, bus, _ = _make_sleep_handler(
            heart=heart, llm_client=_mock_llm_client(llm_response)
        )
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_resolve_contradictions(sleep_stats)
        heart.deactivate_fact.assert_called_once_with(fact1_id)

    @pytest.mark.asyncio
    async def test_low_confidence_treated_as_keep_both(self):
        """Resolution with confidence < 0.7 should be treated as KEEP_BOTH."""
        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[{
            "fact1_id": uuid4(),
            "fact2_id": uuid4(),
            "content1": "A",
            "content2": "B",
            "date1": "2026-03-01",
            "date2": "2026-03-15",
            "similarity": 0.85,
        }])
        heart.deactivate_fact = AsyncMock()
        heart.search_facts = AsyncMock(return_value=[])

        llm_response = json.dumps({
            "action": "SUPERSEDE_A",
            "confidence": 0.5,
            "reason": "Not sure",
        })
        handler, _, _, bus, _ = _make_sleep_handler(
            heart=heart, llm_client=_mock_llm_client(llm_response)
        )
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_resolve_contradictions(sleep_stats)
        heart.deactivate_fact.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_true_no_candidates(self):
        """Phase returns True when no candidates found."""
        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[])
        heart.search_facts = AsyncMock(return_value=[])

        handler, _, _, bus, _ = _make_sleep_handler(heart=heart)
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_resolve_contradictions(sleep_stats)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_no_llm(self):
        """Phase returns True when no LLM client."""
        handler, _, _, _, _ = _make_sleep_handler(llm_client=None)
        handler._llm = None
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_resolve_contradictions(sleep_stats)
        assert result is True

    @pytest.mark.asyncio
    async def test_respects_interrupted(self):
        """Phase stops processing when interrupted."""
        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[
            {"fact1_id": uuid4(), "fact2_id": uuid4(), "content1": "A", "content2": "B",
             "date1": "2026-03-01", "date2": "2026-03-15", "similarity": 0.85},
        ])
        heart.search_facts = AsyncMock(return_value=[])

        handler, _, _, bus, _ = _make_sleep_handler(heart=heart)
        handler._interrupted = True
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_resolve_contradictions(sleep_stats)
        assert result is True
        assert sleep_stats.get("contradictions_resolved", 0) == 0

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        """Phase returns False on exception."""
        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(side_effect=RuntimeError("db error"))

        handler, _, _, bus, _ = _make_sleep_handler(heart=heart)
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_resolve_contradictions(sleep_stats)
        assert result is False

    @pytest.mark.asyncio
    async def test_action_normalization(self):
        """Action string should be normalized (uppercase, stripped)."""
        fact1_id, fact2_id = uuid4(), uuid4()
        # SUPERSEDE_A → loser=fact1, winner=fact2.
        heart, orm = self._heart_with_session(fact1_id, fact2_id, {
            "fact1_id": fact1_id, "fact2_id": fact2_id,
            "content1": "A", "content2": "B",
            "date1": "2026-03-01", "date2": "2026-03-15", "similarity": 0.85,
        })

        # Lowercase action with whitespace
        llm_response = json.dumps({
            "action": " supersede_a ",
            "confidence": 0.9,
            "reason": "test",
        })
        handler, _, _, bus, _ = _make_sleep_handler(
            heart=heart, llm_client=_mock_llm_client(llm_response)
        )
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_resolve_contradictions(sleep_stats)
        # Normalized to SUPERSEDE_A → chain preserved (not a bare deactivate).
        assert orm.superseded_by == fact2_id
        heart.link_facts.assert_awaited_once()


# ===========================================================================
# Task 3b: Sleep cycle integration
# ===========================================================================

class TestSleepCycleWithF031:
    """Full sleep cycle includes F031 phases."""

    @pytest.mark.asyncio
    async def test_resolve_contradictions_in_phase_ordering(self):
        """resolve_contradictions should appear in phases_completed."""
        handler, brain, heart, bus, _ = _make_sleep_handler()
        brain.list_decisions = AsyncMock(return_value=([], 0))
        heart.list_episodes = AsyncMock(return_value=[])
        heart.find_contradiction_candidates = AsyncMock(return_value=[])
        heart.search_facts = AsyncMock(return_value=[])

        event = _make_event("sleep_started")
        await handler._run_sleep(event)

        emit_calls = [c for c in bus.emit.call_args_list if c[0][0].type == "sleep_completed"]
        assert len(emit_calls) == 1
        phases = emit_calls[0][0][0].data["phases_completed"]
        assert "resolve_contradictions" in phases

    @pytest.mark.asyncio
    async def test_resolve_after_reflect_before_generalize(self):
        """Phase ordering: reflect -> resolve_contradictions -> generalize."""
        ep1 = MagicMock()
        ep1.summary = "Test episode for ordering"

        handler, brain, heart, bus, _ = _make_sleep_handler(
            llm_client=_mock_llm_client(json.dumps({
                "patterns": [], "lessons": [], "connections": [], "gaps": [],
                "summary": "Test", "facts": [],
            }))
        )
        brain.list_decisions = AsyncMock(return_value=([], 0))
        heart.list_episodes = AsyncMock(return_value=[ep1, ep1])
        heart.search_facts = AsyncMock(return_value=[])
        heart.find_contradiction_candidates = AsyncMock(return_value=[])
        heart.learn = AsyncMock(return_value=MagicMock())

        event = _make_event("sleep_started")
        await handler._run_sleep(event)

        emit_calls = [c for c in bus.emit.call_args_list if c[0][0].type == "sleep_completed"]
        phases = emit_calls[0][0][0].data["phases_completed"]
        if "reflect" in phases and "resolve_contradictions" in phases:
            assert phases.index("resolve_contradictions") > phases.index("reflect")
        if "resolve_contradictions" in phases and "generalize" in phases:
            assert phases.index("resolve_contradictions") < phases.index("generalize")
