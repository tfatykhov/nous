"""Per-entity profiles, the owner's corrections (F119 PR 2): removing and restoring a line, removing and restoring a
profile, the owner's "not an entity" and its undo, the list's filters, what the writer does with each, and the shapes
the dashboard reads.

Store and writer tests run on real Postgres, each with its own agent inside the rolled-back ``session``; the route tests
commit their rows under their own agent and delete them after. The LLM is the fake client of test_entity_profile.py.
The names in these tests are made up.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session
from test_entity_profile import (
    FakeLLM,
    _a_day_later,
    _agent,
    _ago,
    _app,
    _call,
    _commit_keyed,
    _days_later,
    _entity,
    _keyed,
    _line,
    _purge,
    _row,
    _settings,
    _stored_line,
    _writer,
)
from test_entity_profile_use import _build as _turn
from test_entity_profile_use import _engine, _section
from test_entity_profile_use import _facts as _turn_facts
from test_entity_profile_use import _line as _turn_line
from test_entity_profile_use import _profile as _turn_profile

from nous.heart import entity_profile as ep
from nous.heart.entity_profile import (
    LIST_OFFSET_MAX,
    PREVIOUS_CAP,
    EntityProfileStore,
    EntityProfileWriter,
    _bounded_previous,
    detail_body,
    list_row,
    settle_lines,
)
from nous.storage.models import EntityProfile, Fact

ROOT = Path(__file__).resolve().parents[1]
TS = ROOT / "dashboard-app" / "src" / "lib" / "entity_profiles.ts"


async def _profile(db, session, agent: str, key: str = "zephyrine", n: int = 3):
    """``n`` facts of ``key`` and a profile written from them by the writer: one line per fact."""
    facts = await _keyed(session, agent, key, *({} for _ in range(n)))
    lines = [_line(f"{key.title()} point {i}.", f) for i, f in enumerate(facts)]
    llm = FakeLLM({key: _entity(*lines)})
    writer = _writer(db, agent, llm)
    result = await writer.rebuild(key, session=session)
    assert result["outcome"] == "written"
    return facts, writer, llm


async def _lines(session, agent: str, key: str = "zephyrine") -> list[dict]:
    return list((await _row(session, agent, key)).lines)


# ---------------------------------------------------------------------------
# Removing and restoring a line
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_removing_a_line_records_who_and_when_and_moves_neither_clock(db, session):
    agent = _agent()
    await _profile(db, session, agent)
    store = EntityProfileStore(db, agent)
    before = await _row(session, agent, "zephyrine")
    written, updated, tokens = before.written_at, before.updated_at, before.tokens
    first, second, third = before.lines

    assert await store.remove_line("zephyrine", first["id"], "proxy", session=session) is True
    row = await _row(session, agent, "zephyrine")
    assert [ln["id"] for ln in row.lines] == [second["id"], third["id"]]
    removed = row.previous_lines[-1]
    assert {k: removed[k] for k in first} == first
    assert removed["removed_by"] == "proxy"
    assert abs(datetime.fromisoformat(removed["removed_at"]) - datetime.now(UTC)) < timedelta(minutes=1)
    assert row.rejected == [first["text"]]
    # Neither the day's call count (written_at) nor the rests' clock (updated_at) moves.
    assert (row.written_at, row.updated_at) == (written, updated)
    assert 0 < row.tokens < tokens

    assert await store.remove_line("zephyrine", first["id"], "proxy", session=session) is False
    assert await store.remove_line("zephyrine", "no-such-line", "proxy", session=session) is False
    assert await store.remove_line("nobody", second["id"], "proxy", session=session) is False


@pytest.mark.postgres_only
async def test_a_removed_line_is_not_written_again_by_its_text_or_by_its_exact_cited_facts(db, session):
    agent = _agent()
    f1, f2, f3 = await _keyed(session, agent, "zephyrine", {}, {}, {})
    llm = FakeLLM({"zephyrine": _entity(_line("Zephyrine keeps bees.", f1), _line("Zephyrine sails.", f2))})
    writer = _writer(db, agent, llm)
    await writer.rebuild("zephyrine", session=session)
    bees = next(ln for ln in await _lines(session, agent) if ln["text"] == "Zephyrine keeps bees.")
    await writer.store.remove_line("zephyrine", bees["id"], "owner", session=session)

    llm.replies["zephyrine"] = _entity(
        _line("Zephyrine keeps bees.", f1),  # the same text
        _line("Zephyrine tends hives.", f1),  # other words, exactly the same facts
        _line("Zephyrine tends hives and a garden.", f1, f3),  # its cited facts changed
        _line("Zephyrine sails.", f2),
    )
    # The next sleep after a change to its facts, once its 24 hours have passed.
    await _keyed(session, agent, "zephyrine", {"content": "a change"})
    await _a_day_later(session, agent)
    result = await writer.run_sleep(session=session)

    assert (result["written"], result["rejected_dropped"]) == (1, 2)
    assert [ln["text"] for ln in await _lines(session, agent)] == [
        "Zephyrine tends hives and a garden.",
        "Zephyrine sails.",
    ]
    # The model is shown the removed text among the rejected lines.
    assert "REJECTED LINES" in llm.prompts[-1] and "- Zephyrine keeps bees." in llm.prompts[-1]


@pytest.mark.postgres_only
async def test_a_line_removed_while_the_model_writes_is_not_written(db, session):
    agent = _agent()
    f1, f2 = await _keyed(session, agent, "zephyrine", {}, {})
    llm = FakeLLM({"zephyrine": _entity(_line("Zephyrine keeps bees.", f1), _line("Zephyrine sails.", f2))})
    writer = _writer(db, agent, llm)
    await writer.rebuild("zephyrine", session=session)
    bees = next(ln for ln in await _lines(session, agent) if ln["text"] == "Zephyrine keeps bees.")

    async def remove_now(_prompt):
        await writer.store.remove_line("zephyrine", bees["id"], "owner", session=session)

    llm.on_call = remove_now
    llm.replies["zephyrine"] = _entity(
        _line("Zephyrine keeps bees.", f1), _line("Zephyrine tends hives.", f1), _line("Zephyrine sails.", f2)
    )
    await _a_day_later(session, agent)
    assert (await writer.rebuild("zephyrine", session=session))["outcome"] == "written"
    assert [ln["text"] for ln in await _lines(session, agent)] == ["Zephyrine sails."]


@pytest.mark.postgres_only
async def test_the_skip_outlives_fifty_newer_previous_lines(db, session):
    agent = _agent()
    facts = await _keyed(session, agent, "zephyrine", *({} for _ in range(9)))
    store = EntityProfileStore(db, agent)
    first = [_stored_line(f"a{i}", f"Zephyrine first {i}.", str(f.id)) for i, f in enumerate(facts[:2])]
    await store.write("zephyrine", "profile", "h", fact_count=9, source_count=9, lines=first, session=session)
    await store.remove_line("zephyrine", "a0", "owner", session=session)
    for round_ in range(10):  # 10 rewrites of 8 lines: 79 lines leave the profile after the removal
        lines = [_stored_line(f"r{round_}-{i}", f"Zephyrine {round_} {i}.", str(f.id)) for i, f in enumerate(facts[1:])]
        await store.write("zephyrine", "profile", "h", fact_count=9, source_count=9, lines=lines, session=session)

    row = await _row(session, agent, "zephyrine")
    assert len(row.previous_lines) == PREVIOUS_CAP
    assert [ln["id"] for ln in row.previous_lines if ln.get("removed_at")] == ["a0"]
    # The newest lines that left are kept, the oldest dropped first.
    assert row.previous_lines[-1]["id"] == "r8-7"
    again = _stored_line("new", "Zephyrine says it again.", str(facts[0].id))
    await store.write("zephyrine", "profile", "h", fact_count=9, source_count=9, lines=[again], session=session)
    assert await _lines(session, agent) == []


@pytest.mark.postgres_only
async def test_previous_lines_accumulate_without_repeats(db, session):
    agent = _agent()
    f1, f2 = await _keyed(session, agent, "zephyrine", {}, {})
    store = EntityProfileStore(db, agent)

    async def write(*lines):
        await store.write("zephyrine", "profile", "h", fact_count=2, source_count=2, lines=list(lines), session=session)

    a1, a2 = _stored_line("a1", "Zephyrine A1.", str(f1.id)), _stored_line("a2", "Zephyrine A2.", str(f2.id))
    b1, b2 = _stored_line("b1", "Zephyrine B1.", str(f1.id)), _stored_line("b2", "Zephyrine B2.", str(f2.id))
    await write(a1, a2)
    await write(b1, b2)
    await write(b1)  # a prune: only the line that left is added
    await write()  # emptied
    row = await _row(session, agent, "zephyrine")
    assert [ln["id"] for ln in row.previous_lines] == ["a1", "a2", "b2", "b1"]


def test_bounded_previous_drops_the_oldest_replaced_line_first():
    removed = {"id": "x", "text": "t", "fact_ids": ["f"], "removed_at": "2026-11-24T10:00:00+00:00", "removed_by": None}
    entries = [removed] + [{"id": str(i), "text": "t", "fact_ids": ["f"]} for i in range(PREVIOUS_CAP + 5)]
    kept = _bounded_previous(entries)
    assert len(kept) == PREVIOUS_CAP and kept[0] is removed and kept[-1]["id"] == str(PREVIOUS_CAP + 4)
    assert _bounded_previous(entries[:10]) == entries[:10]
    only_removed = [{**removed, "id": str(i)} for i in range(PREVIOUS_CAP + 2)]
    assert [e["id"] for e in _bounded_previous(only_removed)] == [str(i) for i in range(2, PREVIOUS_CAP + 2)]


def test_settle_lines_drops_a_line_citing_exactly_the_facts_of_a_removed_line():
    facts = [Fact(id=uuid.uuid4(), agent_id="a", content=f"fact {i}", learned_at=datetime.now(UTC)) for i in range(2)]
    a, b = (str(f.id) for f in facts)
    raw = [{"text": "One.", "fact_ids": [a[:8]]}, {"text": "Both.", "fact_ids": [a[:8], b[:8]]}]
    lines, counts = settle_lines(raw, facts, [], "Z", None, _settings("a"), removed={frozenset({a})})
    assert [ln["text"] for ln in lines] == ["Both."] and counts["rejected_dropped"] == 1
    assert len(settle_lines(raw, facts, [], "Z", None, _settings("a"))[0]) == 2


@pytest.mark.postgres_only
async def test_restoring_a_line_puts_it_back_and_lifts_both_skips(db, session):
    agent = _agent()
    (f1, f2, _f3), writer, llm = await _profile(db, session, agent)
    first = (await _lines(session, agent))[0]
    store = writer.store
    await store.remove_line("zephyrine", first["id"], "owner", session=session)

    assert await store.restore_line("zephyrine", first["id"], session=session) == "restored"
    row = await _row(session, agent, "zephyrine")
    assert row.lines[-1] == first  # back at the end, without who and when
    assert row.rejected == [] and not any(ln.get("removed_at") for ln in row.previous_lines)
    assert await store.restore_line("zephyrine", first["id"], session=session) == "not_found"

    # A rewrite may write it again, with another id.
    llm.replies["zephyrine"] = _entity(_line(first["text"], f1), _line("Zephyrine point 1.", f2))
    await _a_day_later(session, agent)
    assert (await writer.rebuild("zephyrine", session=session))["outcome"] == "written"
    rewritten = await _lines(session, agent)
    assert first["text"] in [ln["text"] for ln in rewritten] and first["id"] not in [ln["id"] for ln in rewritten]


@pytest.mark.postgres_only
async def test_a_line_is_not_restored_on_a_dead_fact_or_into_a_row_that_is_no_profile(db, session):
    agent = _agent()
    (f1, _f2, _f3), writer, _llm = await _profile(db, session, agent)
    store = writer.store
    first, second, third = await _lines(session, agent)
    for line in (first, second, third):
        await store.remove_line("zephyrine", line["id"], "owner", session=session)

    await session.execute(update(Fact).where(Fact.id == f1.id).values(active=False))
    assert await store.restore_line("zephyrine", first["id"], session=session) == "not_live"
    # Under the owner's "not an entity" a line can be removed and put back, as the writer's lines are kept.
    await store.set_owner_verdict("zephyrine", True, "owner", session=session)
    assert await store.restore_line("zephyrine", third["id"], session=session) == "restored"
    await store.set_owner_verdict("zephyrine", False, None, session=session)
    await session.execute(
        update(EntityProfile).where(EntityProfile.agent_id == agent).values(status="not_entity", lines=[])
    )
    assert await store.restore_line("zephyrine", second["id"], session=session) == "no_profile"
    await store.set_active("zephyrine", False, session=session)
    assert await store.restore_line("zephyrine", second["id"], session=session) == "not_found"


# ---------------------------------------------------------------------------
# Removing and restoring a profile
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_a_removed_profile_is_not_asked_about_or_written_and_comes_back_as_it_was(db, session):
    agent = _agent()
    _facts, writer, llm = await _profile(db, session, agent)
    before = await _row(session, agent, "zephyrine")
    snapshot = (list(before.lines), before.status, before.source_hash, before.written_at, before.updated_at)
    assert await writer.store.set_active("zephyrine", False, session=session) is True
    await _keyed(session, agent, "zephyrine", {"content": "a change"})
    await _a_day_later(session, agent)
    calls = len(llm.prompts)

    stats = await writer.run_sleep(session=session)
    assert (stats["candidates"], len(llm.prompts)) == (0, calls)  # not a candidate: no place, no call
    assert (await writer.rebuild("zephyrine", session=session))["outcome"] == "inactive"
    assert "zephyrine" not in await writer.store.profiled_keys(session=session)

    assert await writer.store.set_active("zephyrine", True, session=session) is True
    row = await _row(session, agent, "zephyrine")
    assert (list(row.lines), row.status, row.source_hash) == snapshot[:3]
    assert (await writer.run_sleep(session=session))["written"] == 1
    assert await writer.store.set_active("nobody", False, session=session) is False


# ---------------------------------------------------------------------------
# The owner's "not an entity"
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_an_owner_not_entity_survives_sleep_cycles_doubled_facts_and_thirty_days(db, session):
    agent = _agent()
    await _keyed(session, agent, "spreadsheet", {}, {})
    llm = FakeLLM({"spreadsheet": {"verdict": "not_entity"}})
    writer = _writer(db, agent, llm)
    await writer.run_sleep(session=session)
    assert (await _row(session, agent, "spreadsheet")).status == "not_entity"

    assert await writer.store.set_owner_verdict("spreadsheet", True, "proxy", session=session) is True
    verdict = (await _row(session, agent, "spreadsheet")).owner_verdict
    assert (verdict["verdict"], verdict["by"]) == ("not_entity", "proxy")
    # The model's verdict would be asked again now: the facts more than doubled and 31 days passed.
    await _keyed(session, agent, "spreadsheet", *({} for _ in range(4)))
    await _days_later(session, agent, 31)
    for _cycle in range(3):
        stats = await writer.run_sleep(session=session)
        # Not a candidate at all: not asked, not listed for a rewrite.
        assert (stats["candidates"], stats["asked"]) == (0, 0)
    assert (await writer.rebuild("spreadsheet", session=session))["outcome"] == "owner_not_entity"
    assert len(llm.prompts) == 1
    # Marking it again keeps the first who and when.
    await writer.store.set_owner_verdict("spreadsheet", True, "owner", session=session)
    assert (await _row(session, agent, "spreadsheet")).owner_verdict == verdict

    assert await writer.store.set_owner_verdict("spreadsheet", False, None, session=session) is True
    row = await _row(session, agent, "spreadsheet")
    assert (row.owner_verdict, row.status) == (None, "not_entity")
    assert (await writer.run_sleep(session=session))["not_entity"] == 1
    assert len(llm.prompts) == 2


@pytest.mark.postgres_only
async def test_taking_back_not_an_entity_returns_the_profile_as_the_writer_left_it(db, session):
    agent = _agent()
    _facts, writer, _llm = await _profile(db, session, agent)
    before = await _row(session, agent, "zephyrine")
    kept = (list(before.lines), before.status, before.source_hash, before.written_at, before.updated_at)

    await writer.store.set_owner_verdict("zephyrine", True, "owner", session=session)
    assert "zephyrine" not in await writer.store.profiled_keys(session=session)
    listed = await writer.store.list(session=session, status="not_entity")
    assert [r["key"] for r in listed] == ["zephyrine"]
    assert await writer.store.list(session=session, status="profile") == []

    await writer.store.set_owner_verdict("zephyrine", False, None, session=session)
    row = await _row(session, agent, "zephyrine")
    assert (list(row.lines), row.status, row.source_hash, row.written_at, row.updated_at) == kept
    assert "zephyrine" in await writer.store.profiled_keys(session=session)


@pytest.mark.postgres_only
async def test_an_owner_change_during_a_run_is_seen_before_the_next_call_and_the_next_write(db, session):
    agent = _agent()
    await _keyed(session, agent, "alder", {}, {}, {})
    await _keyed(session, agent, "birchwood", {}, {})
    a1 = (
        await session.execute(select(Fact).where(Fact.agent_id == agent, Fact.content == "alder fact number 0"))
    ).scalar_one()
    b1 = (
        await session.execute(select(Fact).where(Fact.agent_id == agent, Fact.content == "birchwood fact number 0"))
    ).scalar_one()
    llm = FakeLLM({"alder": _entity(_line("Alder one.", a1)), "birchwood": _entity(_line("Birchwood one.", b1))})
    writer = _writer(db, agent, llm)
    await writer.run_sleep(session=session)
    assert llm.keys == ["alder", "birchwood"]
    alder_lines = await _lines(session, agent, "alder")

    await _keyed(session, agent, "alder", {"content": "alder changed"})
    await _keyed(session, agent, "birchwood", {"content": "birchwood changed"})
    await _a_day_later(session, agent)

    async def owner_acts(prompt):
        if '"alder"' in prompt:
            await writer.store.set_owner_verdict("birchwood", True, "owner", session=session)
            await writer.store.set_active("alder", False, session=session)

    llm.on_call = owner_acts
    llm.replies["alder"] = _entity(_line("Alder rewritten.", a1))
    stats = await writer.run_sleep(session=session)

    # alder was asked (its call counts); its reply is not written into the removed row. birchwood is never asked.
    assert llm.keys == ["alder", "birchwood", "alder"]
    assert (stats["asked"], stats["owner_not_entity"]) == (1, 1)
    alder = await _row(session, agent, "alder")
    assert (alder.active, list(alder.lines)) == (False, alder_lines)


@pytest.mark.postgres_only
async def test_a_key_the_owner_called_not_an_entity_takes_no_place_among_the_candidates(db, session):
    agent = _agent()
    await _keyed(session, agent, "spreadsheet", {}, {}, {}, {})
    await _keyed(session, agent, "quillon", {}, {})
    store = EntityProfileStore(db, agent)
    await store.write("spreadsheet", "failed", "", fact_count=4, source_count=4, lines=[], session=session)
    await store.set_owner_verdict("spreadsheet", True, "owner", session=session)
    await _days_later(session, agent, 8)  # the failed row's rest is over: only the verdict holds it back
    llm = FakeLLM({"quillon": {"verdict": "not_entity"}})

    # One place among the candidates: the key with the most facts is the owner's "not an entity"; the next one gets it.
    stats = await _writer(db, agent, llm, entity_profile_max_profiles=1).run_sleep(session=session)
    assert (stats["candidates"], llm.keys) == (1, ["quillon"])


@pytest.mark.postgres_only
async def test_a_verdict_needs_an_active_row(db, session):
    agent = _agent()
    store = EntityProfileStore(db, agent)
    assert await store.set_owner_verdict("nobody", True, "owner", session=session) is False
    await store.write("quillon", "failed", "", fact_count=3, source_count=3, lines=[], session=session)
    await store.set_active("quillon", False, session=session)
    assert await store.set_owner_verdict("quillon", True, "owner", session=session) is False


@pytest.mark.postgres_only
async def test_owner_changes_never_read_or_write_another_agents_row(db, session):
    mine, theirs = _agent(), _agent()
    await _profile(db, session, theirs)
    their_line = (await _lines(session, theirs))[0]
    before = await _row(session, theirs, "zephyrine")
    snapshot = (list(before.lines), list(before.previous_lines), before.active, before.owner_verdict)
    store = EntityProfileStore(db, mine)

    assert await store.remove_line("zephyrine", their_line["id"], "owner", session=session) is False
    assert await store.set_active("zephyrine", False, session=session) is False
    assert await store.set_owner_verdict("zephyrine", True, "owner", session=session) is False
    assert await store.restore_line("zephyrine", their_line["id"], session=session) == "not_found"
    assert await store.list(session=session) == []
    assert all(n == 0 for n in (await store.counts(session=session)).values())
    after = await _row(session, theirs, "zephyrine")
    assert (list(after.lines), list(after.previous_lines), after.active, after.owner_verdict) == snapshot


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


async def _commit_profile(db, agent: str, key: str = "zephyrine") -> list[str]:
    """Two committed facts of ``key`` and a profile with one line on each; the fact ids."""
    await _commit_keyed(db, agent, key, 2)
    async with db.session() as s:
        ids = [str(i) for i in (await s.execute(select(Fact.id).where(Fact.agent_id == agent))).scalars().all()]
    lines = [_stored_line("l1", "Zephyrine keeps bees.", ids[0]), _stored_line("l2", "Zephyrine sails.", ids[1])]
    await EntityProfileStore(db, agent).write(
        key, "profile", "h", fact_count=2, source_count=2, lines=lines, display_name="Zephyrine", kind="person"
    )
    return ids


@pytest.mark.postgres_only
async def test_each_owner_route_and_its_undo(db):
    agent = _agent()
    try:
        await _commit_profile(db, agent)
        app = _app(db, agent)

        async def detail():
            return (await _call(app, "GET", "/entity-profiles/zephyrine")).json()

        removed = await _call(app, "DELETE", "/entity-profiles/Zephyrine's/lines/l1")
        assert (removed.status_code, removed.json()) == (200, {"status": "done", "key": "zephyrine"})
        body = await detail()
        assert [ln["id"] for ln in body["lines"]] == ["l2"] and body["rejected"] == ["Zephyrine keeps bees."]
        assert [(ln["id"], ln["removed_by"]) for ln in body["removed_lines"]] == [("l1", "owner")]
        assert (await _call(app, "DELETE", "/entity-profiles/zephyrine/lines/l1")).status_code == 404

        assert (await _call(app, "POST", "/entity-profiles/zephyrine/lines/l1/restore")).status_code == 200
        body = await detail()
        assert [ln["id"] for ln in body["lines"]] == ["l2", "l1"] and body["removed_lines"] == []
        assert (await _call(app, "POST", "/entity-profiles/zephyrine/lines/l1/restore")).status_code == 404

        assert (await _call(app, "POST", "/entity-profiles/zephyrine/not-entity")).status_code == 200
        verdict = (await detail())["owner_verdict"]
        assert (verdict["verdict"], verdict["by"]) == ("not_entity", "owner")
        listed = (await _call(app, "GET", "/entity-profiles?status=not_entity")).json()["profiles"]
        assert [p["key"] for p in listed] == ["zephyrine"]
        assert (await _call(app, "DELETE", "/entity-profiles/zephyrine/not-entity")).status_code == 200
        assert (await detail())["owner_verdict"] is None

        assert (await _call(app, "DELETE", "/entity-profiles/zephyrine")).status_code == 200
        assert (await _call(app, "GET", "/entity-profiles/zephyrine")).status_code == 404
        assert (await _call(app, "GET", "/entity-profiles")).json()["profiles"] == []
        gone = (await _call(app, "GET", "/entity-profiles?status=removed")).json()["profiles"]
        assert [(p["key"], p["active"]) for p in gone] == [("zephyrine", False)]
        assert (await _call(app, "POST", "/entity-profiles/zephyrine/not-entity")).status_code == 404
        assert (await _call(app, "POST", "/entity-profiles/zephyrine/restore")).status_code == 200
        assert [ln["id"] for ln in (await detail())["lines"]] == ["l2", "l1"]

        for method, path in (
            ("DELETE", "/entity-profiles/nobody"),
            ("POST", "/entity-profiles/nobody/restore"),
            ("POST", "/entity-profiles/nobody/not-entity"),
            ("DELETE", "/entity-profiles/nobody/not-entity"),
            ("DELETE", "/entity-profiles/nobody/lines/l1"),
            ("POST", "/entity-profiles/nobody/lines/l1/restore"),
            ("DELETE", "/entity-profiles/zephyrine/lines/no-such-line"),
        ):
            assert (await _call(app, method, path)).status_code == 404, (method, path)
    finally:
        await _purge(db, agent)


@pytest.mark.postgres_only
async def test_the_routes_never_read_or_change_another_agents_profile(db):
    mine, theirs = _agent(), _agent()
    try:
        await _commit_profile(db, theirs)
        before = await EntityProfileStore(db, theirs).get("zephyrine")
        app = _app(db, mine)
        for method, path in (
            ("DELETE", "/entity-profiles/zephyrine/lines/l1"),
            ("DELETE", "/entity-profiles/zephyrine"),
            ("POST", "/entity-profiles/zephyrine/restore"),
            ("POST", "/entity-profiles/zephyrine/not-entity"),
            ("DELETE", "/entity-profiles/zephyrine/not-entity"),
            ("GET", "/entity-profiles/zephyrine"),
        ):
            assert (await _call(app, method, path)).status_code == 404, (method, path)
        listed = (await _call(app, "GET", "/entity-profiles?status=removed")).json()
        assert listed["profiles"] == [] and set(listed["counts"].values()) == {0}
        assert await EntityProfileStore(db, theirs).get("zephyrine") == before
    finally:
        await _purge(db, theirs)


@pytest.mark.postgres_only
async def test_restoring_a_line_on_a_dead_fact_or_into_no_profile_is_409(db):
    agent = _agent()
    try:
        ids = await _commit_profile(db, agent)
        app = _app(db, agent)
        await _call(app, "DELETE", "/entity-profiles/zephyrine/lines/l1")
        await _call(app, "DELETE", "/entity-profiles/zephyrine/lines/l2")
        async with db.session() as s:
            await s.execute(update(Fact).where(Fact.id == uuid.UUID(ids[0])).values(active=False))
            await s.commit()
        dead = await _call(app, "POST", "/entity-profiles/zephyrine/lines/l1/restore")
        assert (dead.status_code, dead.json()) == (409, {"error": "a fact this line cites is no longer live"})
        async with db.session() as s:
            await s.execute(update(EntityProfile).where(EntityProfile.agent_id == agent).values(status="not_entity"))
            await s.commit()
        verdict = await _call(app, "POST", "/entity-profiles/zephyrine/lines/l2/restore")
        assert (verdict.status_code, verdict.json()) == (409, {"error": "this key has no profile now"})
    finally:
        await _purge(db, agent)


@pytest.mark.postgres_only
async def test_the_list_filters_by_status_and_name_counts_each_status_and_pages(db):
    agent = _agent()
    store = EntityProfileStore(db, agent)
    rows = [
        ("alder", "profile", 30, "Alder Grove"),
        ("birchwood", "not_entity", 20, None),
        ("cedarfall", "profile", 10, "100% Cedar"),
        ("dunmore", "asked", 5, None),
        ("elmstead", "failed", 4, None),
        ("fernhill", "profile", 3, "Fern Hill"),
    ]
    try:
        for key, status, count, name in rows:
            await store.write(key, status, "h", fact_count=count, source_count=count, lines=[], display_name=name)
        await store.set_owner_verdict("cedarfall", True, "owner")
        await store.set_active("fernhill", False)
        app = _app(db, agent)

        async def keys(query: str) -> list[str]:
            return [p["key"] for p in (await _call(app, "GET", f"/entity-profiles{query}")).json()["profiles"]]

        everything = (await _call(app, "GET", "/entity-profiles")).json()
        assert set(everything) == _ts_fields("ProfileList")
        assert [p["key"] for p in everything["profiles"]] == ["alder", "birchwood", "cedarfall", "dunmore", "elmstead"]
        assert everything["counts"] == {
            "profile": 1,
            "not_entity": 2,
            "owner": 0,
            "asked": 1,
            "failed": 1,
            "removed": 1,
        }
        assert (everything["total"], everything["limit"], everything["offset"]) == (5, 200, 0)
        assert await keys("?status=not_entity") == ["birchwood", "cedarfall"]
        assert await keys("?status=profile") == ["alder"]
        assert await keys("?status=removed") == ["fernhill"]
        assert await keys("?q=GROVE") == ["alder"]
        assert await keys("?q=%25") == ["cedarfall"]  # a % is a character, not a wildcard
        assert await keys("?q=fern") == []  # removed: listed only under its status
        assert await keys("?q=fern&status=removed") == ["fernhill"]
        filtered = (await _call(app, "GET", "/entity-profiles?q=e&status=failed")).json()
        assert [p["key"] for p in filtered["profiles"]] == ["elmstead"] and filtered["total"] == 1
        assert filtered["counts"]["removed"] == 1 and filtered["counts"]["profile"] == 1
        assert await keys("?limit=2&offset=2") == ["cedarfall", "dunmore"]
        assert await keys("?offset=-4&limit=1") == ["alder"]
        for bad in ("?status=deleted", "?offset=x", "?limit=x"):
            assert (await _call(app, "GET", f"/entity-profiles{bad}")).status_code == 400, bad
    finally:
        await _purge(db, agent)


@pytest.mark.postgres_only
async def test_a_rebuild_of_a_key_the_owner_called_not_an_entity_makes_no_call(db, session):
    agent = _agent()
    _facts, writer, llm = await _profile(db, session, agent)
    await writer.store.set_owner_verdict("zephyrine", True, "owner", session=session)
    result = await writer.rebuild("zephyrine", session=session)
    assert (result["outcome"], len(llm.prompts)) == ("owner_not_entity", 1)


# ---------------------------------------------------------------------------
# The daily limit, kept lines, an all-removed reply, the bounds, and corrections made while the model writes
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_ten_rebuilds_of_a_profile_written_an_hour_ago_make_no_call_and_the_count_does_not_move(db, session):
    agent = _agent()
    _facts, writer, llm = await _profile(db, session, agent)
    await _ago(session, agent, timedelta(hours=1))
    written_at = (await _row(session, agent, "zephyrine")).written_at
    assert await writer.store.written_last_day(session=session) == 1

    for _click in range(10):
        result = await writer.rebuild("zephyrine", session=session)
        assert result["outcome"] == "held"
        assert datetime.fromisoformat(result["next_at"]) == written_at + timedelta(hours=24)
    assert (len(llm.prompts), await writer.store.written_last_day(session=session)) == (1, 1)

    await _a_day_later(session, agent)
    assert await writer.store.written_last_day(session=session) == 0
    assert (await writer.rebuild("zephyrine", session=session))["outcome"] == "written"
    assert (len(llm.prompts), await writer.store.written_last_day(session=session)) == (2, 1)


@pytest.mark.postgres_only
async def test_the_rebuild_route_answers_held_with_when_it_can_be_written_again(db):
    agent = _agent()
    try:
        await _commit_profile(db, agent)
        llm = FakeLLM()
        writer = EntityProfileWriter(db, _settings(agent), llm)
        app = _app(db, agent, MagicMock(is_sleeping=False, _entity_profile_writer=writer))
        answer = await _call(app, "POST", "/entity-profiles/rebuild", json={"key": "zephyrine"})
        assert (answer.status_code, answer.json()["outcome"]) == (200, "held")
        assert datetime.fromisoformat(answer.json()["next_at"]) > datetime.now(UTC) + timedelta(hours=23)
        assert llm.prompts == []
    finally:
        await _purge(db, agent)


@pytest.mark.postgres_only
async def test_a_line_the_owner_kept_survives_a_rewrite_on_the_fact_of_a_removed_line(db, session):
    agent = _agent()
    (f1,) = await _keyed(session, agent, "zephyrine", {})
    llm = FakeLLM(
        {"zephyrine": _entity(_line("Zephyrine keeps bees.", f1), _line("Zephyrine sells honey at the market.", f1))}
    )
    writer = _writer(db, agent, llm)
    await writer.rebuild("zephyrine", session=session)
    bees = next(ln for ln in await _lines(session, agent) if ln["text"] == "Zephyrine keeps bees.")
    await writer.store.remove_line("zephyrine", bees["id"], "owner", session=session)

    # A new fact arrives and the profile is rewritten: the line the owner kept comes back on the same fact; a new
    # wording on that fact alone does not.
    (f2,) = await _keyed(session, agent, "zephyrine", {"content": "a new fact"})
    llm.replies["zephyrine"] = _entity(
        _line("Zephyrine sells honey at the market.", f1),
        _line("Zephyrine tends hives.", f1),
        _line("Zephyrine opened a stall.", f2),
    )
    await _a_day_later(session, agent)
    stats = await writer.run_sleep(session=session)

    assert (stats["written"], stats["rejected_dropped"]) == (1, 1)
    assert [ln["text"] for ln in await _lines(session, agent)] == [
        "Zephyrine sells honey at the market.",
        "Zephyrine opened a stall.",
    ]


@pytest.mark.postgres_only
async def test_a_reply_made_only_of_removed_lines_empties_the_profile_once_and_is_not_asked_again(db, session):
    agent = _agent()
    facts, writer, llm = await _profile(db, session, agent)
    for line in await _lines(session, agent):
        await writer.store.remove_line("zephyrine", line["id"], "owner", session=session)
    # A change to the facts the model is shown (a tag), and the 24 hours: the model writes the same three lines.
    await session.execute(update(Fact).where(Fact.id == facts[0].id).values(tags=["changed"]))
    await _a_day_later(session, agent)

    first = await writer.run_sleep(session=session)
    assert (first["all_rejected"], first["failed_closed"], first["rejected_dropped"]) == (1, 0, 3)
    row = await _row(session, agent, "zephyrine")
    assert (row.status, list(row.lines)) == ("profile", [])
    for _cycle in range(2):
        await _a_day_later(session, agent)
        stats = await writer.run_sleep(session=session)
        assert (stats["unchanged"], stats["asked"]) == (1, 0)
    assert len(llm.prompts) == 2


@pytest.mark.postgres_only
async def test_a_reply_of_removed_lines_only_keeps_the_lines_the_owner_kept(db, session):
    agent = _agent()
    f1, f2, f3 = await _keyed(session, agent, "zephyrine", {}, {}, {})
    llm = FakeLLM(
        {
            "zephyrine": _entity(
                _line("Zephyrine keeps bees.", f1), _line("Zephyrine sails.", f2), _line("Zephyrine rows.", f3)
            )
        }
    )
    writer = _writer(db, agent, llm)
    await writer.rebuild("zephyrine", session=session)
    bees, sails, rows = await _lines(session, agent)
    await writer.store.remove_line("zephyrine", bees["id"], "owner", session=session)
    # Its facts change (a tag; the fact of "rows" dies), the 24 hours pass, and the model writes only the removed
    # line's text.
    await session.execute(update(Fact).where(Fact.id == f2.id).values(tags=["changed"]))
    await session.execute(update(Fact).where(Fact.id == f3.id).values(active=False))
    llm.replies["zephyrine"] = _entity(_line("Zephyrine keeps bees.", f1))
    await _a_day_later(session, agent)

    first = await writer.run_sleep(session=session)
    assert (first["all_rejected"], first["failed_closed"], first["asked"]) == (1, 0, 1)
    row = await _row(session, agent, "zephyrine")
    # The line the owner kept is still there, the same line; the one on a dead fact is not.
    assert (row.status, list(row.lines)) == ("profile", [sails])
    assert [ln["id"] for ln in row.previous_lines] == [bees["id"], rows["id"]]
    for _cycle in range(2):
        await _a_day_later(session, agent)
        stats = await writer.run_sleep(session=session)
        assert (stats["unchanged"], stats["asked"]) == (1, 0)
    assert len(llm.prompts) == 2


@pytest.mark.postgres_only
async def test_an_owner_row_or_an_emptied_profile_the_owner_changed_meanwhile_is_counted_as_left_alone(db, session):
    agent = _agent()
    await _keyed(session, agent, "alder", {}, {}, {})
    gone = await _keyed(session, agent, "birchwood", {}, {})
    await _keyed(session, agent, "pat", {}, {})
    store = EntityProfileStore(db, agent)
    line = _stored_line("b1", "Birchwood one.", str(gone[0].id))
    await store.write("birchwood", "profile", "h", fact_count=2, source_count=2, lines=[line], session=session)
    await store.write("pat", "profile", "h", fact_count=2, source_count=2, lines=[], session=session)
    await _a_day_later(session, agent)
    # Every fact of birchwood is gone (it would be emptied without a call), and pat is now named an owner key (it
    # would become an owner row without a call).
    await session.execute(update(Fact).where(Fact.id.in_([f.id for f in gone])).values(active=False))

    async def owner_acts(prompt):
        if '"alder"' in prompt:
            await store.set_owner_verdict("birchwood", True, "owner", session=session)
            await store.set_active("pat", False, session=session)

    llm = FakeLLM({"alder": {"verdict": "not_entity"}}, on_call=owner_acts)
    stats = await _writer(db, agent, llm, entity_profile_owner_keys=["Pat"]).run_sleep(session=session)

    assert (stats["cleared"], stats["owner"]) == (0, 0)
    assert (stats["owner_not_entity"], stats["inactive"]) == (1, 1)
    assert (await _row(session, agent, "birchwood")).lines == [line]
    assert (await _row(session, agent, "pat")).status == "profile"


@pytest.mark.postgres_only
async def test_a_line_is_not_put_back_past_the_profiles_bounds(db, session):
    agent = _agent()
    _facts, writer, _llm = await _profile(db, session, agent)
    store = writer.store
    first = (await _lines(session, agent))[0]
    await store.remove_line("zephyrine", first["id"], "owner", session=session)

    assert await store.restore_line("zephyrine", first["id"], session=session, max_lines=2) == "too_big"
    assert await store.restore_line("zephyrine", first["id"], session=session, max_lines=3, max_tokens=5) == "too_big"
    row = await _row(session, agent, "zephyrine")
    assert (len(row.lines), row.rejected) == (2, [first["text"]])
    restored = await store.restore_line("zephyrine", first["id"], session=session, max_lines=3, max_tokens=300)
    assert restored == "restored"


@pytest.mark.postgres_only
async def test_putting_a_line_back_past_the_bound_is_409_in_words(db):
    agent = _agent()
    try:
        await _commit_profile(db, agent)
        app = _app(db, agent, entity_profile_max_lines=1)
        await _call(app, "DELETE", "/entity-profiles/zephyrine/lines/l1")
        answer = await _call(app, "POST", "/entity-profiles/zephyrine/lines/l1/restore")
        assert answer.status_code == 409
        assert answer.json()["error"].startswith("putting it back would make the profile longer than its bound")
    finally:
        await _purge(db, agent)


@pytest.mark.postgres_only
@pytest.mark.parametrize("change", ["verdict", "removed"])
async def test_a_correction_made_while_the_model_writes_is_reported_and_nothing_is_written(db, session, change):
    agent = _agent()
    facts, writer, llm = await _profile(db, session, agent)
    before = list((await _row(session, agent, "zephyrine")).lines)

    async def owner_acts(_prompt):
        if change == "verdict":
            await writer.store.set_owner_verdict("zephyrine", True, "owner", session=session)
        else:
            await writer.store.set_active("zephyrine", False, session=session)

    llm.on_call = owner_acts
    llm.replies["zephyrine"] = _entity(_line("Zephyrine was rewritten.", facts[0]))
    await _a_day_later(session, agent)
    result = await writer.rebuild("zephyrine", session=session)

    assert (result["outcome"], result["written"]) == ("skipped", 0)
    assert result["owner_not_entity" if change == "verdict" else "inactive"] == 1
    assert list((await _row(session, agent, "zephyrine")).lines) == before


@pytest.mark.postgres_only
async def test_an_unusable_reply_leaves_a_row_the_owner_called_not_an_entity_meanwhile_as_it_was(db, session):
    agent = _agent()
    await _keyed(session, agent, "quillon", {}, {})
    seen: dict = {}

    async def owner_acts(_prompt):
        await writer.store.set_owner_verdict("quillon", True, "owner", session=session)
        row = await _row(session, agent, "quillon")
        seen["status"], seen["updated_at"] = row.status, row.updated_at

    writer = _writer(db, agent, FakeLLM({"quillon": None}, on_call=owner_acts))  # no tool call: unusable
    assert (await writer.rebuild("quillon", session=session))["outcome"] == "failed_closed"
    # count_call left it asked; the unusable reply neither makes it failed nor moves its clock under the verdict.
    await writer.store.set_owner_verdict("quillon", False, None, session=session)
    row = await _row(session, agent, "quillon")
    assert (row.status, row.updated_at, row.owner_verdict) == ("asked", seen["updated_at"], None)
    assert seen["status"] == "asked"


@pytest.mark.postgres_only
async def test_a_removed_profile_takes_no_place_among_the_candidates(db, session):
    agent = _agent()
    await _keyed(session, agent, "spreadsheet", {}, {}, {}, {})
    await _keyed(session, agent, "quillon", {}, {})
    store = EntityProfileStore(db, agent)
    await store.write("spreadsheet", "not_entity", "", fact_count=4, source_count=4, lines=[], session=session)
    await store.set_active("spreadsheet", False, session=session)
    await _days_later(session, agent, 31)
    llm = FakeLLM({"quillon": {"verdict": "not_entity"}})

    stats = await _writer(db, agent, llm, entity_profile_max_profiles=1).run_sleep(session=session)
    assert (stats["candidates"], llm.keys) == (1, ["quillon"])


@pytest.mark.postgres_only
async def test_a_line_put_back_during_a_sleep_survives_a_prune_later_in_it(db, session):
    agent = _agent()
    await _keyed(session, agent, "alder", {}, {}, {})
    b1, b2, b3 = await _keyed(session, agent, "birchwood", {}, {}, {})
    store = EntityProfileStore(db, agent)
    lines = [_stored_line(f"l{i}", f"Birchwood {i}.", str(f.id)) for i, f in enumerate((b1, b2, b3), start=1)]
    await store.write("birchwood", "profile", "h", fact_count=3, source_count=3, lines=lines, session=session)
    await store.remove_line("birchwood", "l1", "owner", session=session)
    # birchwood was written now (held for 24 hours) and a fact it cites died: the sleep prunes it without a call.
    await session.execute(update(Fact).where(Fact.id == b2.id).values(active=False))

    async def owner_acts(prompt):
        if '"alder"' in prompt:
            assert await store.restore_line("birchwood", "l1", session=session) == "restored"

    llm = FakeLLM({"alder": {"verdict": "not_entity"}}, on_call=owner_acts)
    stats = await _writer(db, agent, llm).run_sleep(session=session)

    assert (llm.keys, stats["held"], stats["stale_dropped"]) == (["alder"], 1, 1)
    assert [ln["id"] for ln in await _lines(session, agent, "birchwood")] == ["l3", "l1"]


@pytest.mark.postgres_only
async def test_an_offset_past_any_table_is_clamped(db):
    agent = _agent()
    app = _app(db, agent)
    answer = await _call(app, "GET", "/entity-profiles?offset=99999999999999999999")
    assert answer.status_code == 200
    assert (answer.json()["profiles"], answer.json()["offset"]) == ([], LIST_OFFSET_MAX)


# ---------------------------------------------------------------------------
# The owner's corrections reach turns at once (with F119 PR 3's turn-time reads and its cached vocabulary)
# ---------------------------------------------------------------------------


async def _quillon(db, agent: str) -> tuple[dict, dict]:
    """Two facts of "quillon" and its committed profile, one line on each; the two lines."""
    f1, f2 = await _turn_facts(db, agent, "quillon keeps bees", "quillon sails")
    l1, l2 = _turn_line("Quillon keeps bees.", f1), _turn_line("Quillon sails.", f2)
    await _turn_profile(db, agent, "quillon", l1, l2, name="Quillon", kind="person")
    return l1, l2


def _cached(agent: str) -> frozenset[str] | None:
    entry = ep._vocab_cache.get(agent)
    return entry[0] if entry is not None else None


@pytest.mark.postgres_only
@pytest.mark.parametrize("change", ["removed", "not_entity"])
async def test_a_profile_removed_or_called_not_an_entity_is_not_shown_on_the_next_turn(
    db, mock_embeddings, settings, change
):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on")
    try:
        await _quillon(db, s.agent_id)
        assert _section(await _turn(engine, s, "how is quillon?")) is not None
        store = EntityProfileStore(db, s.agent_id)
        if change == "removed":
            assert await store.set_active("quillon", False) is True
        else:
            assert await store.set_owner_verdict("quillon", True, "owner") is True
        # The cached keys still hold it (nothing cleared them): the turn's own read leaves it out.
        assert "quillon" in _cached(s.agent_id)
        result = await _turn(engine, s, "how is quillon?")
        assert _section(result) is None and result.entity_profiles["matched"] == ["quillon"]
    finally:
        await heart.close()


@pytest.mark.postgres_only
@pytest.mark.parametrize("change", ["restored", "taken_back"])
async def test_a_profile_restored_or_taken_back_is_shown_on_the_next_turn(db, mock_embeddings, settings, change):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on")
    try:
        await _quillon(db, s.agent_id)
        store = EntityProfileStore(db, s.agent_id)
        if change == "restored":
            await store.set_active("quillon", False)
        else:
            await store.set_owner_verdict("quillon", True, "owner")
        ep.invalidate_profiled_vocabulary(s.agent_id)
        assert _section(await _turn(engine, s, "how is quillon?")) is None
        assert "quillon" not in _cached(s.agent_id)  # cached without it

        if change == "restored":
            assert await store.set_active("quillon", True) is True
        else:
            assert await store.set_owner_verdict("quillon", False, None) is True
        content = _section(await _turn(engine, s, "how is quillon?")).content
        assert "Quillon keeps bees." in content and "Quillon sails." in content
    finally:
        await heart.close()


@pytest.mark.postgres_only
async def test_a_line_removed_is_not_shown_on_the_next_turn_and_a_line_put_back_is(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on")
    try:
        l1, l2 = await _quillon(db, s.agent_id)
        store = EntityProfileStore(db, s.agent_id)
        assert "Quillon keeps bees." in _section(await _turn(engine, s, "quillon")).content

        assert await store.remove_line("quillon", l1["id"], "owner") is True
        content = _section(await _turn(engine, s, "quillon")).content
        assert "Quillon keeps bees." not in content and "Quillon sails." in content

        # Its last line: the profile has no live line left, and the key leaves the vocabulary.
        assert await store.remove_line("quillon", l2["id"], "owner") is True
        assert _section(await _turn(engine, s, "quillon")) is None
        assert "quillon" not in _cached(s.agent_id)

        # A line put back: the key is back in the vocabulary, and the line is shown on the very next turn.
        assert await store.restore_line("quillon", l2["id"]) == "restored"
        content = _section(await _turn(engine, s, "quillon")).content
        assert "Quillon sails." in content and "Quillon keeps bees." not in content
    finally:
        await heart.close()


@pytest.mark.postgres_only
@pytest.mark.parametrize("action", ["restore", "take_back", "remove_line", "restore_line", "write", "left_alone"])
async def test_the_cached_keys_are_cleared_after_the_commit_never_before(db, monkeypatch, action):
    agent = _agent()
    try:
        l1, _l2 = await _quillon(db, agent)
        store = EntityProfileStore(db, agent)
        if action == "restore":
            await store.set_active("quillon", False)
        elif action in ("take_back", "left_alone"):
            await store.set_owner_verdict("quillon", True, "owner")
        elif action == "restore_line":
            await store.remove_line("quillon", l1["id"], "owner")
        order: list[str] = []
        clear = ep.invalidate_profiled_vocabulary

        def _clear(agent_id: str) -> None:
            order.append("clear")
            clear(agent_id)

        def _commit(_session) -> None:
            order.append("commit")

        monkeypatch.setattr(ep, "invalidate_profiled_vocabulary", _clear)
        event.listen(Session, "before_commit", _commit)
        try:
            if action == "restore":
                await store.set_active("quillon", True)
            elif action == "take_back":
                await store.set_owner_verdict("quillon", False, None)
            elif action == "remove_line":
                await store.remove_line("quillon", l1["id"], "owner")
            elif action == "restore_line":
                await store.restore_line("quillon", l1["id"])
            else:
                written = await store.write(
                    "quillon", "profile", "h2", fact_count=2, source_count=2, lines=[], display_name="Quillon"
                )
                assert (written is None) is (action == "left_alone")
        finally:
            event.remove(Session, "before_commit", _commit)
        # Cleared once the change is committed; a write that left the row alone clears nothing.
        assert order == (["commit"] if action == "left_alone" else ["commit", "clear"])
    finally:
        await _purge(db, agent)


@pytest.mark.postgres_only
async def test_a_savepoint_or_a_rollback_clears_nothing(db, monkeypatch):
    agent = _agent()
    try:
        await _quillon(db, agent)
        store = EntityProfileStore(db, agent)
        await store.set_active("quillon", False)
        cleared: list[str] = []
        monkeypatch.setattr(ep, "invalidate_profiled_vocabulary", cleared.append)
        async with db.session() as s:
            async with s.begin_nested():
                await store.set_active("quillon", True, session=s)
            assert cleared == []  # a savepoint's release is not the commit
            await s.rollback()
        assert cleared == []
        async with db.session() as s:
            await store.set_active("quillon", True, session=s)
            await s.commit()
        assert cleared == [agent]
    finally:
        await _purge(db, agent)


# ---------------------------------------------------------------------------
# The shapes the dashboard reads (dashboard-app/src/lib/entity_profiles.ts)
# ---------------------------------------------------------------------------


def _ts_fields(name: str) -> set[str]:
    """The field names of the TypeScript interface ``name`` (with those of the interface it extends)."""
    source = TS.read_text(encoding="utf-8")
    match = re.search(rf"export interface {name}(?: extends (\w+))? \{{(.*?)\}}", source, re.S)
    assert match, name
    body = re.sub(r"/\*.*?\*/", "", match.group(2), flags=re.S)
    fields = set(re.findall(r"(\w+)\??:", body))
    return fields | (_ts_fields(match.group(1)) if match.group(1) else set())


def _store_row() -> dict:
    line = {"id": "l1", "text": "t", "fact_ids": ["f"], "said_on": "2026-11-02", "date_from": "learned_at"}
    gone = {**line, "id": "l0", "removed_by": "owner", "removed_at": "2026-11-24T10:00:00+00:00"}
    return {
        "key": "k", "display_name": None, "kind": None, "status": "profile", "lines": [line], "previous_lines": [gone],
        "rejected": ["t"], "source_hash": "h", "fact_count": 1, "source_count": 1, "tokens": 1, "active": True,
        "owner_verdict": {"verdict": "not_entity", "by": "owner", "at": "2026-11-24T10:00:00+00:00"},
        "written_at": None, "created_at": None, "updated_at": None,
    }  # fmt: skip


def test_a_list_row_has_exactly_the_fields_the_tab_reads():
    assert set(list_row(_store_row())) == _ts_fields("ProfileRow")


def test_the_detail_has_exactly_the_fields_the_profile_reads():
    row = _store_row()
    body = detail_body(row, 0)
    assert set(body) == _ts_fields("ProfileDetail")
    assert set(body["lines"][0]) == _ts_fields("ProfileLine")
    assert set(body["removed_lines"][0]) == _ts_fields("RemovedLine")
    assert set(body["owner_verdict"]) == _ts_fields("OwnerVerdict")


@pytest.mark.postgres_only
async def test_a_stored_line_and_a_removed_line_have_the_fields_the_profile_reads(db, session):
    agent = _agent()
    _facts, writer, _llm = await _profile(db, session, agent)
    first = (await _lines(session, agent))[0]
    assert set(first) == _ts_fields("ProfileLine")
    await writer.store.remove_line("zephyrine", first["id"], "owner", session=session)
    removed = (await _row(session, agent, "zephyrine")).previous_lines[-1]
    assert set(removed) == _ts_fields("RemovedLine")
    verdict_row = await writer.store.set_owner_verdict("zephyrine", True, "owner", session=session)
    assert verdict_row is True
    assert set((await _row(session, agent, "zephyrine")).owner_verdict) == _ts_fields("OwnerVerdict")
