"""F108 Phase 3: the tool-note table and its store, on real Postgres."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError

from nous.handlers.tool_notes import (
    NOTE_MAX_CHARS,
    NOTES_PER_TOOL,
    NOTES_TOTAL,
    REREVIEW_DAYS,
    NoteCapError,
    StoredNote,
    ToolNoteStore,
)
from nous.observability.tool_patterns import Pattern

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def pat(tool: str = "bash", kind: str = "error_repeat", key: str = "boom", hits: int = 6, calls: int = 30) -> Pattern:
    return Pattern(kind=kind, tool_name=tool, key=key, hits=hits, calls=calls, guard=None, example="boom")


@pytest.fixture
def agent_id() -> str:
    return f"test-p3-tn-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture
async def store(db, agent_id):
    yield ToolNoteStore(db, agent_id)
    async with db.session() as s:
        await s.execute(sa_text("DELETE FROM nous_system.tool_note WHERE agent_id LIKE 'test-p3-tn-%'"))
        await s.commit()


async def _row(db, agent_id: str, note_id: uuid.UUID) -> dict:
    async with db.session() as s:
        r = await s.execute(
            sa_text("SELECT * FROM nous_system.tool_note WHERE agent_id = :a AND id = :i"),
            {"a": agent_id, "i": note_id},
        )
        return dict(r.mappings().one())


def test_constants():
    assert (NOTES_PER_TOOL, NOTES_TOTAL, NOTE_MAX_CHARS, REREVIEW_DAYS) == (3, 40, 300, 14)
    assert issubclass(NoteCapError, ValueError)


@pytest.mark.asyncio
async def test_add_stores_baseline_and_text(store, db, agent_id):
    p = pat(hits=7, calls=28)
    nid = await store.add(p, "check the path first", now=NOW)
    row = await _row(db, agent_id, nid)
    assert row["baseline_rate"] == p.rate and row["baseline_calls"] == 28 and row["baseline_hits"] == 7
    assert row["active"] is True and row["retired"] is None
    assert row["created_at"] == NOW
    notes = await store.active()
    assert [n.id for n in notes] == [nid]
    n = notes[0]
    assert isinstance(n, StoredNote)
    assert (n.tool_name, n.pattern_kind, n.pattern_key, n.note_text) == ("bash", "error_repeat", "boom", "check the path first")
    assert n.pattern_id == p.id
    assert (n.baseline_rate, n.baseline_calls, n.baseline_hits) == (p.rate, 28, 7)
    assert n.judged is None and n.created_at == NOW


@pytest.mark.asyncio
async def test_active_newest_first(store):
    a = await store.add(pat(key="a"), "one", now=NOW - timedelta(days=2))
    b = await store.add(pat(key="b"), "two", now=NOW)
    assert [n.id for n in await store.active()] == [b, a]


@pytest.mark.asyncio
async def test_per_tool_cap(store, db, agent_id):
    ids = [await store.add(pat(key=f"k{i}"), f"n{i}", now=NOW) for i in range(3)]
    with pytest.raises(NoteCapError):
        await store.add(pat(key="k3"), "n3", now=NOW)
    assert len(await store.active()) == 3
    new = await store.add(pat(key="k3"), "n3", now=NOW, replaces=ids[0])
    old = await _row(db, agent_id, ids[0])
    assert old["active"] is False and old["retired"]["reason"] == "rewritten" and old["retired_at"] == NOW
    active = await store.active()
    assert len(active) == 3 and new in {n.id for n in active} and ids[0] not in {n.id for n in active}


@pytest.mark.asyncio
async def test_total_cap(store):
    ids = []
    for t in range(14):
        for i in range(3 if t < 13 else 1):
            ids.append(await store.add(pat(tool=f"tool{t}", key=f"k{i}"), "n", now=NOW))
    assert len(ids) == NOTES_TOTAL
    with pytest.raises(NoteCapError):
        await store.add(pat(tool="tool13", key="extra"), "n", now=NOW)
    new = await store.add(pat(tool="tool13", key="extra"), "n", now=NOW, replaces=ids[0])
    assert new and len(await store.active()) == NOTES_TOTAL


@pytest.mark.asyncio
async def test_failed_add_leaves_replaced_note_alone(store):
    ids = [await store.add(pat(key=f"k{i}"), f"n{i}", now=NOW) for i in range(3)]
    with pytest.raises(ValueError):
        await store.add(pat(key="k9"), "x" * 301, now=NOW, replaces=ids[0])
    assert {n.id for n in await store.active()} == set(ids)


@pytest.mark.asyncio
async def test_text_limits(store, db, agent_id):
    for bad in ("x" * 301, "", "   \n "):
        with pytest.raises(ValueError):
            await store.add(pat(), bad, now=NOW)
    assert await store.add(pat(), "y" * 300, now=NOW)
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            await s.execute(
                sa_text(
                    "INSERT INTO nous_system.tool_note (agent_id, tool_name, pattern_kind, pattern_key, note_text, "
                    "baseline_rate, baseline_calls, baseline_hits) VALUES (:a, 't', 'error_repeat', 'raw', :t, 0, 0, 0)"
                ),
                {"a": agent_id, "t": "z" * 301},
            )
            await s.commit()


@pytest.mark.asyncio
async def test_one_active_note_per_pattern(store):
    first = await store.add(pat(), "one", now=NOW)
    with pytest.raises(ValueError) as exc:
        await store.add(pat(), "two", now=NOW)
    assert not isinstance(exc.value, IntegrityError)
    await store.retire(first, {"reason": "no_improvement"}, now=NOW)
    assert await store.add(pat(), "two", now=NOW)


@pytest.mark.asyncio
async def test_close_rows_block_rereview_but_not_the_caps(store):
    p = pat()
    await store.close(p, "declined", now=NOW)
    assert p.id in await store.recently_reviewed(NOW)
    assert await store.active() == []
    for i in range(3):
        await store.add(pat(key=f"k{i}"), "n", now=NOW)
    assert len(await store.active()) == 3
    assert p.id in await store.recently_reviewed(NOW + timedelta(days=13))
    assert p.id not in await store.recently_reviewed(NOW + timedelta(days=15))


@pytest.mark.asyncio
async def test_close_records_reason_and_time(store, db, agent_id):
    p = pat(kind="call_repeat", key="")
    await store.close(p, "guard", now=NOW)
    async with db.session() as s:
        r = await s.execute(sa_text("SELECT * FROM nous_system.tool_note WHERE agent_id = :a"), {"a": agent_id})
        row = dict(r.mappings().one())
    assert row["active"] is False and row["note_text"] == ""
    assert row["retired"]["reason"] == "guard" and row["retired"]["at"]
    assert row["retired_at"] == NOW
    assert (row["baseline_calls"], row["baseline_hits"]) == (p.calls, p.hits)


@pytest.mark.asyncio
async def test_close_rejects_unknown_reason(store):
    with pytest.raises(ValueError):
        await store.close(pat(), "bogus", now=NOW)


@pytest.mark.asyncio
async def test_retired_note_blocks_for_rereview_days(store):
    p = pat()
    nid = await store.add(p, "old note", now=NOW - timedelta(days=40))
    assert p.id in await store.recently_reviewed(NOW - timedelta(days=39))
    assert p.id not in await store.recently_reviewed(NOW)
    await store.retire(nid, {"reason": "no_improvement"}, now=NOW)
    assert p.id in await store.recently_reviewed(NOW)
    assert p.id not in await store.recently_reviewed(NOW + timedelta(days=15))


@pytest.mark.asyncio
async def test_agent_scoped(db, store, agent_id):
    other = ToolNoteStore(db, agent_id + "-other")
    p = pat()
    await other.add(p, "theirs", now=NOW)
    await other.close(pat(key="c"), "declined", now=NOW)
    assert await store.active() == []
    assert await store.recently_reviewed(NOW) == set()
    # another agent's note does not count against this agent's caps or unique index
    assert await store.add(p, "mine", now=NOW)


@pytest.mark.asyncio
async def test_retire_is_soft(store, db, agent_id):
    nid = await store.add(pat(), "n", now=NOW)
    await store.retire(nid, {"reason": "unused", "after": {"calls": 3}}, now=NOW + timedelta(days=1))
    row = await _row(db, agent_id, nid)
    assert row["active"] is False and row["note_text"] == "n"
    assert row["retired_at"] == NOW + timedelta(days=1)
    assert row["retired"]["reason"] == "unused" and row["retired"]["after"] == {"calls": 3} and row["retired"]["at"]
    assert await store.active() == []


@pytest.mark.asyncio
async def test_retire_is_agent_scoped(db, store, agent_id):
    other = ToolNoteStore(db, agent_id + "-other")
    nid = await other.add(pat(), "theirs", now=NOW)
    await store.retire(nid, {"reason": "unused"}, now=NOW)
    assert len(await other.active()) == 1


@pytest.mark.asyncio
async def test_mark_judged(store):
    nid = await store.add(pat(), "n", now=NOW)
    judged = {"at": NOW.isoformat(), "calls": 22, "hits": 1, "rate": 0.045, "days": 8, "verdict": "kept"}
    await store.mark_judged(nid, judged)
    (n,) = await store.active()
    assert n.judged == judged
