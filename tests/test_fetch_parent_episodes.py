"""F067 Phase 2 follow-ups (codex review): unit tests for
``_fetch_parent_episodes_for_facts`` covering:

- ``max_parents=0`` returns ``[]`` (Codex P2-#3: cap disable should work)
- Cap is enforced BEFORE appending (Codex P2-#3: N items, not N+1)
- SQL extracts ``structured_summary->>'summary'`` not full JSON blob
  (Codex P2-#2: returned text should be the inner summary string)

The helper hits Postgres for fact→episode joins, so these tests use
fake heart + a stubbed DB session that captures the SQL string.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.tools import _fetch_parent_episodes_for_facts


@dataclass
class _FakeResult:
    id: uuid.UUID
    type: str = "fact"


class _FakeSession:
    """Captures the SQL strings + parameters executed against it."""

    def __init__(self):
        self.executed: list[tuple[str, dict]] = []
        # Map executed-call-index -> rows. Index 0 = fact lookup, 1 = episode lookup.
        self.return_rows: list[list[tuple]] = [[], []]

    async def execute(self, stmt, params):
        sql = str(stmt)
        self.executed.append((sql, dict(params)))
        # Return next-in-sequence rows
        idx = len(self.executed) - 1
        rows = self.return_rows[idx] if idx < len(self.return_rows) else []
        result = MagicMock()
        result.all = MagicMock(return_value=rows)
        return result


class _FakeDB:
    def __init__(self, fake_session):
        self._sess = fake_session

    def session(self):
        # async context manager
        class _Ctx:
            def __init__(s, sess): s._s = sess
            async def __aenter__(s): return s._s
            async def __aexit__(s, *a): return None
        return _Ctx(self._sess)


@dataclass
class _FakeHeart:
    agent_id: str = "test-agent"
    db: object = None


@pytest.mark.asyncio
async def test_max_parents_zero_returns_empty():
    """Codex P2-#3: max_parents=0 disables injection completely."""
    fake_session = _FakeSession()
    heart = _FakeHeart(db=_FakeDB(fake_session))
    fid = uuid.uuid4()
    results = [_FakeResult(id=fid, type="fact")]
    out = await _fetch_parent_episodes_for_facts(
        heart=heart, results=results, max_parents=0, truncate=500,
    )
    assert out == []
    # Codex point: should also skip the DB roundtrip
    assert fake_session.executed == []


@pytest.mark.asyncio
async def test_max_parents_cap_enforced_before_append():
    """Codex P2-#3: max_parents=2 yields exactly 2, not 3."""
    fake_session = _FakeSession()
    # 5 distinct facts each with a distinct source_episode_id
    fact_ids = [uuid.uuid4() for _ in range(5)]
    ep_ids = [str(uuid.uuid4()) for _ in range(5)]
    fake_session.return_rows = [
        # Fact -> source_episode_id rows
        [(str(fid), eid) for fid, eid in zip(fact_ids, ep_ids)],
        # Episode -> summary rows for whatever the helper queries
        [(eid, f"summary for {eid[:8]}") for eid in ep_ids[:2]],
    ]
    heart = _FakeHeart(db=_FakeDB(fake_session))
    results = [_FakeResult(id=fid, type="fact") for fid in fact_ids]
    out = await _fetch_parent_episodes_for_facts(
        heart=heart, results=results, max_parents=2, truncate=500,
    )
    assert len(out) == 2
    # And the episode lookup SQL only queries 2 IDs
    assert len(fake_session.executed) == 2
    ep_params = fake_session.executed[1][1]
    assert len(ep_params["ids"]) == 2


@pytest.mark.asyncio
async def test_sql_extracts_summary_subfield_not_full_json():
    """Codex P2-#2: episode SQL must use structured_summary->>'summary',
    not COALESCE(structured_summary::text, summary). The first form returns
    just the summary string; the second returns the whole JSON blob."""
    fake_session = _FakeSession()
    fid = uuid.uuid4()
    ep_id = str(uuid.uuid4())
    fake_session.return_rows = [
        [(str(fid), ep_id)],
        [(ep_id, "the summary text")],
    ]
    heart = _FakeHeart(db=_FakeDB(fake_session))
    out = await _fetch_parent_episodes_for_facts(
        heart=heart, results=[_FakeResult(id=fid, type="fact")],
        max_parents=2, truncate=500,
    )
    assert len(out) == 1
    # Inspect the second SQL — must use ->>'summary' extraction, not the
    # whole JSON blob via ::text cast
    ep_sql = fake_session.executed[1][0]
    assert "structured_summary->>'summary'" in ep_sql
    # The pre-fix `::text` cast must be gone
    assert "structured_summary::text" not in ep_sql


@pytest.mark.asyncio
async def test_no_fact_results_returns_empty_without_db_hit():
    """When there are no fact-typed results, no DB query happens."""
    fake_session = _FakeSession()
    heart = _FakeHeart(db=_FakeDB(fake_session))
    results = [_FakeResult(id=uuid.uuid4(), type="episode")]
    out = await _fetch_parent_episodes_for_facts(
        heart=heart, results=results, max_parents=5, truncate=500,
    )
    assert out == []
    assert fake_session.executed == []
