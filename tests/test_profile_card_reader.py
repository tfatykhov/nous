"""The profile card reader (PR2): the pre-turn User Profile section renders the card's lines when
``profile_card_enabled`` is on and a line still cites an active fact; otherwise the profile facts render as before.

Real Postgres, one fresh agent per test (``_fresh_engine`` from the tiered-context tests).
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text, update

from nous.heart.profile_card import ProfileCardStore
from nous.heart.schemas import FactSummary
from nous.storage.models import Fact, ProfileCard
from tests.test_tiered_context import _frame, _fresh_engine, _profile_section

pytestmark = pytest.mark.postgres_only


async def _facts(db, agent: str, *contents: str, category: str = "preference") -> list[Fact]:
    rows = [
        Fact(id=uuid.uuid4(), agent_id=agent, content=c, category=category, subject="user", source="user_direct")
        for c in contents
    ]
    async with db.session() as s:
        s.add_all(rows)
        await s.commit()
    return rows


def _entry(text: str, *facts: Fact) -> dict:
    return {"id": uuid.uuid4().hex[:12], "text": text, "fact_ids": [str(f.id) for f in facts]}


async def _card(db, agent: str, *entries: dict) -> None:
    await ProfileCardStore(db, agent).write(list(entries), "h")


async def _build(engine, s, text: str = "hello"):
    return await engine.build(agent_id=s.agent_id, session_id="s-card", input_text=text, frame=_frame())


async def _engine(db, mock_embeddings, settings, **flags):
    return await _fresh_engine(db, mock_embeddings, settings, identity_prompt="", settings_update=flags)


async def test_the_card_renders_its_lines_without_citations(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, profile_card_enabled=True)
    try:
        f1, f2 = await _facts(db, s.agent_id, "prefers Celsius", "lives in Lisbon")
        await _card(db, s.agent_id, _entry("The user prefers Celsius.", f1), _entry("The user lives in Lisbon.", f2))
        profile = _profile_section(await _build(engine, s))
        assert profile.content == "- The user prefers Celsius.\n- The user lives in Lisbon."
        assert profile.tier == "semi_stable"
    finally:
        await heart.close()


async def test_with_the_flag_off_a_card_changes_nothing(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings)
    try:
        f1, _f2 = await _facts(db, s.agent_id, "prefers Celsius", "lives in Lisbon")
        before = await _build(engine, s)
        await _card(db, s.agent_id, _entry("The user prefers Celsius.", f1))
        after = await _build(engine, s)

        def _content(result):  # the clock section changes by the minute
            return [(sec.label, sec.content) for sec in result.sections if sec.label != "Current Date/Time"]

        assert _content(after) == _content(before)
        assert "prefers Celsius" in _profile_section(after).content
        assert "The user prefers Celsius." not in after.system_prompt
    finally:
        await heart.close()


async def test_a_line_whose_facts_are_inactive_or_superseded_is_hidden(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, profile_card_enabled=True)
    try:
        live, gone, old, new = await _facts(
            db, s.agent_id, "prefers tea", "owns a boat", "lives in Lisbon", "lives in Porto"
        )
        await _card(
            db,
            s.agent_id,
            _entry("The user prefers tea.", live),
            _entry("The user owns a boat.", gone),
            _entry("The user lives in Lisbon.", old),
            _entry("The user likes tea and Porto.", live, old),
        )
        async with db.session() as session:
            await session.execute(update(Fact).where(Fact.id == gone.id).values(active=False))
            await session.execute(update(Fact).where(Fact.id == old.id).values(superseded_by=new.id))
            await session.commit()
        profile = _profile_section(await _build(engine, s))
        assert profile.content == "- The user prefers tea.\n- The user likes tea and Porto."
    finally:
        await heart.close()


@pytest.mark.parametrize("card", ["none", "empty", "all_inactive"])
async def test_without_a_live_line_the_profile_facts_render_as_before(db, mock_embeddings, settings, card):
    engine, heart, s = await _engine(db, mock_embeddings, settings, profile_card_enabled=True)
    try:
        f1, f2 = await _facts(db, s.agent_id, "prefers Celsius", "lives in Lisbon")
        if card == "empty":
            await _card(db, s.agent_id)
        elif card == "all_inactive":
            await _card(db, s.agent_id, _entry("The user prefers Celsius.", f1))
            async with db.session() as session:
                await session.execute(update(Fact).where(Fact.id == f1.id).values(active=False))
                await session.commit()
        profile = _profile_section(await _build(engine, s))
        assert "lives in Lisbon" in profile.content and not profile.content.startswith("- The user")
    finally:
        await heart.close()


async def test_without_a_card_core_mode_still_renders_its_tagged_facts(db, mock_embeddings, settings):
    engine, heart, s = await _engine(
        db,
        mock_embeddings,
        settings,
        profile_card_enabled=True,
        profile_core_enabled=True,
        profile_core_probation_days=0,
    )
    try:
        tagged, _plain = await _facts(db, s.agent_id, "prefers Celsius always", "lives in Lisbon")
        async with db.session() as session:
            await session.execute(update(Fact).where(Fact.id == tagged.id).values(tags=["profile_core"]))
            await session.commit()
        profile = _profile_section(await _build(engine, s))
        assert "prefers Celsius always" in profile.content and "Lisbon" not in profile.content
    finally:
        await heart.close()


async def test_a_rejected_line_never_renders(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, profile_card_enabled=True)
    try:
        f1, f2 = await _facts(db, s.agent_id, "prefers Celsius", "lives in Lisbon")
        await _card(db, s.agent_id, _entry("The user prefers Celsius.", f1), _entry("The user lives in Lisbon.", f2))
        # A line deleted while a rewrite was in flight: still in entries, also in rejected.
        async with db.session() as session:
            await session.execute(
                update(ProfileCard)
                .where(ProfileCard.agent_id == s.agent_id)
                .values(rejected=["the user lives in  Lisbon."])
            )
            await session.commit()
        profile = _profile_section(await _build(engine, s))
        assert profile.content == "- The user prefers Celsius."
    finally:
        await heart.close()


async def test_a_full_size_card_is_not_cut_by_the_smaller_profile_budget(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, profile_card_enabled=True)
    try:
        facts = await _facts(db, s.agent_id, *(f"fact {i}" for i in range(25)))
        # 25 lines of 62 characters: 1,599 characters rendered, 399 tokens, past the 300-token scaled budget.
        lines = [f"Line {i:02d} " + "x" * 52 for i in range(25)]
        await _card(db, s.agent_id, *(_entry(t, f) for t, f in zip(lines, facts, strict=True)))
        profile = _profile_section(await _build(engine, s))
        assert profile.content == "\n".join(f"- {t}" for t in lines)
    finally:
        await heart.close()


async def test_the_session_profile_leg_skips_every_fact_the_card_cites(db, mock_embeddings, settings):
    engine, heart, s = await _engine(
        db, mock_embeddings, settings, profile_card_enabled=True, profile_intent_leg_enabled=True
    )
    try:
        cited, gone, other = await _facts(
            db, s.agent_id, "sails a Contessa 32", "races dinghies", "moors the boat in Cascais"
        )
        await _card(db, s.agent_id, _entry("The user sails a Contessa 32.", cited), _entry("Races.", gone))
        async with db.session() as session:
            await session.execute(update(Fact).where(Fact.id == gone.id).values(active=False))
            await session.commit()

        def _hit(f: Fact) -> FactSummary:
            return FactSummary(
                id=f.id,
                content=f.content,
                category="preference",
                subject="user",
                confidence=1.0,
                active=True,
                score=0.95,
            )

        async def _search(*args, **kwargs):
            return [_hit(cited), _hit(gone), _hit(other)] if kwargs.get("include_categories") else []

        heart.search_facts = _search
        result = await _build(engine, s, "tell me about sailing")
        leg = next(sec for sec in result.sections if sec.label == "Session Profile")
        assert "Cascais" in leg.content
        assert "Contessa" not in leg.content and "dinghies" not in leg.content
    finally:
        await heart.close()


class _Trace:
    def __init__(self):
        self.legs: dict[str, dict] = {}

    def leg(self, name, **kwargs):
        self.legs[name] = kwargs

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.mark.parametrize("flag", [True, False])
async def test_the_trace_records_which_source_the_profile_used(db, mock_embeddings, settings, monkeypatch, flag):
    engine, heart, s = await _engine(db, mock_embeddings, settings, profile_card_enabled=flag)
    try:
        f1, _f2 = await _facts(db, s.agent_id, "prefers Celsius", "lives in Lisbon")
        traces: list[_Trace] = []
        logger = MagicMock()
        logger.start = lambda **_: traces.append(_Trace()) or traces[-1]
        monkeypatch.setattr("nous.cognitive.context.get_active_retrieval_logger", lambda: logger)

        await _build(engine, s)
        await _card(db, s.agent_id, _entry("The user prefers Celsius.", f1))
        await _build(engine, s)

        if not flag:
            assert all("context_profile_card" not in t.legs for t in traces)
            return
        fallback, card = (t.legs["context_profile_card"] for t in traces)
        assert fallback["n_returned"] == 0 and fallback["skip_reason"] == "no card"
        assert card["n_returned"] == 1 and card.get("skip_reason") is None
    finally:
        await heart.close()


def _record_traces(monkeypatch) -> list[_Trace]:
    traces: list[_Trace] = []
    logger = MagicMock()
    logger.start = lambda **_: traces.append(_Trace()) or traces[-1]
    monkeypatch.setattr("nous.cognitive.context.get_active_retrieval_logger", lambda: logger)
    return traces


@pytest.mark.parametrize("shared_session", [False, True])
async def test_a_card_read_error_renders_the_facts_and_is_traced(
    db, mock_embeddings, settings, monkeypatch, shared_session
):
    engine, heart, s = await _engine(db, mock_embeddings, settings, profile_card_enabled=True)
    try:
        f1, _f2 = await _facts(db, s.agent_id, "prefers Celsius", "lives in Lisbon")
        await _card(db, s.agent_id, _entry("The user prefers Celsius.", f1))

        async def _fail(self, session=None):
            if session is None:
                raise RuntimeError("card store down")
            # A database error on the shared session: without a savepoint it aborts the pre-turn transaction.
            await session.execute(text("SELECT 1/0"))

        monkeypatch.setattr(ProfileCardStore, "live_entries", _fail)
        traces = _record_traces(monkeypatch)
        if shared_session:
            async with db.session() as session:
                result = await engine.build(
                    agent_id=s.agent_id, session_id="s-card", input_text="hello", frame=_frame(), session=session
                )
        else:
            result = await _build(engine, s)

        profile = _profile_section(result)
        assert profile is not None and "lives in Lisbon" in profile.content
        leg = traces[-1].legs["context_profile_card"]
        assert leg["error"] == ("DBAPIError" if shared_session else "RuntimeError") and leg["n_returned"] == 0
    finally:
        await heart.close()


async def test_malformed_card_entries_are_skipped(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, profile_card_enabled=True)
    try:
        (f1,) = await _facts(db, s.agent_id, "prefers Celsius")
        fid = str(f1.id)
        entries = [
            {"id": "a"},
            "junk",
            {"id": "b", "text": 5, "fact_ids": [fid]},
            {"id": "c", "text": "No ids.", "fact_ids": "nope"},
            {"id": "d", "text": "The user prefers Celsius.", "fact_ids": [fid, 7, ["x"]]},
        ]
        await _card(db, s.agent_id)
        async with db.session() as session:
            await session.execute(
                update(ProfileCard).where(ProfileCard.agent_id == s.agent_id).values(entries=entries, rejected=[3])
            )
            await session.commit()
        profile = _profile_section(await _build(engine, s))
        assert profile.content == "- The user prefers Celsius."
    finally:
        await heart.close()
