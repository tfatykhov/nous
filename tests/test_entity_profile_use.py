"""Entity profiles in turns (F119 PR 3, ``NOUS_ENTITY_PROFILE_USE``): the three modes, which turns ask for them, how
a message names a profiled entity, which lines are shown and how they are dated, the section's place and size, the
record on the turn, and that a failed read leaves the context as it is with the setting off.

The context tests run on real Postgres, each with its own agent and committed rows (the vocabulary is read in a session
of its own). The names in these tests are made up.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text, update
from test_layer_critic_skills import _mock_layer
from test_layer_critic_skills import _settings as _layer_settings
from test_tool_call_recording import PATHS, _drive, _runner, _scripted_api
from test_turn_record import _agent, _context, _delete, _json, _rows, _turn, _TurnSpy

from nous.api.execution_context import CONTEXT_KINDS, ExecutionContext
from nous.cognitive.context import _stated_at
from nous.cognitive.intent import RetrievalPlan, RetrievalQuery
from nous.cognitive.schemas import BuildResult
from nous.heart import entity_profile as ep
from nous.heart.entity_profile import EntityProfileStore, match_profiled_keys, use_mode
from nous.heart.profile_card import ProfileCardStore
from nous.heart.schemas import FactSummary
from nous.heart.temporal import as_date
from nous.observability.model_calls import analyze_context, parse_system_sections
from nous.observability.recording import BoundedWriter
from nous.observability.tool_calls import ToolCallRecorder
from nous.storage.models import EntityProfile, Fact
from tests.test_tiered_context import _frame, _fresh_engine

ZERO_SKIPS = {"not_live": 0, "owner_card": 0, "already_shown": 0, "budget": 0}
# The review's own vocabulary, with made-up names: a project and its repo, a person, and ordinary words.
VOCAB = frozenset({"orrin", "orrin repo", "repo", "max", "link", "r-1", "quill", "sol"})


# ---- matching (no database) -------------------------------------------------------------------------------------


def test_a_key_inside_a_longer_matched_key_is_dropped():
    # extract_entity_candidates alone gives ["orrin repo", "orrin", "repo"]: three slots from one mention
    assert match_profiled_keys("what is the state of the Orrin repo?", VOCAB) == ["orrin repo"]


def test_ordinary_words_match_and_only_the_first_three_are_read():
    matched = match_profiled_keys("raise max_tokens and max out the budget, then link R-1 to Quill's PR", VOCAB)
    assert matched == ["max", "link", "r-1", "quill"]  # D3: no special rule; trace mode measures these
    assert matched[: ep.TURN_MAX_PROFILES] == ["max", "link", "r-1"]


def test_a_key_is_dropped_only_as_whole_words_inside_a_longer_key():
    # "rin" is letters inside "orrin", not a word of "orrin repo": it stays
    assert match_profiled_keys("the orrin repo and rin", frozenset({"rin", "orrin repo"})) == ["orrin repo", "rin"]
    assert match_profiled_keys("orrin repo", frozenset({"orrin", "repo", "orrin repo"})) == ["orrin repo"]


def test_only_the_first_1000_characters_of_a_message_are_read():
    assert match_profiled_keys("quill " + "x " * 600, VOCAB) == ["quill"]
    assert match_profiled_keys("x " * 600 + "quill", VOCAB) == []


@pytest.mark.parametrize(
    "raw, mode", [("off", "off"), ("trace", "trace"), ("on", "on"), (" ON ", "on"), ("Trace", "trace"), ("", "off")]
)
def test_the_three_modes(raw, mode):
    assert use_mode(SimpleNamespace(entity_profile_use=raw)) == mode


def test_an_unknown_value_is_off_with_one_warning(caplog):
    value = f"sometimes-{uuid.uuid4().hex[:6]}"
    s = SimpleNamespace(entity_profile_use=value)
    with caplog.at_level(logging.WARNING, logger="nous.heart.entity_profile"):
        assert [use_mode(s), use_mode(s), use_mode(s)] == ["off", "off", "off"]
    assert len([r for r in caplog.records if value in r.getMessage()]) == 1


# ---- which turns ask for it (the runner) ------------------------------------------------------------------------


def _ctx_of(kind: str, session_id: str = "s1") -> ExecutionContext:
    """A valid context of ``kind``: a continuation (a goal's turns among them) is internal and names its intention, an
    approved action its proposal and one tool."""
    extra: dict = {}
    if kind == "continuation":
        extra = {"authority": "internal_only", "intention_id": uuid.uuid4(), "root_intention_id": uuid.uuid4()}
    elif kind == "approved_action":
        extra = {"proposal_id": uuid.uuid4(), "declared_tools": ("learn_fact",)}
    return ExecutionContext(kind=kind, session_id=session_id, **extra)


@pytest.mark.parametrize("kind", CONTEXT_KINDS)
def test_only_interactive_and_mcp_turns_ask_for_entity_profiles(kind):
    r, _calls = _runner(entity_profile_use="on")
    expected = {"entity_profiles": True} if kind in ("interactive", "mcp") else {}
    assert r._entity_profile_kwargs(_ctx_of(kind), "s1") == expected


def test_a_background_session_a_goal_turn_and_off_ask_for_none():
    on, _ = _runner(entity_profile_use="on")
    # a job report over /chat runs as interactive in a background path's session
    assert on._entity_profile_kwargs(ExecutionContext(kind="interactive", session_id="claude-j1"), "claude-j1") == {}
    # a goal's turn: a continuation in its intention's session (nous/goals/chain.py)
    assert on._entity_profile_kwargs(_ctx_of("continuation", "intent-g1"), "intent-g1") == {}
    off, _ = _runner()
    assert off._entity_profile_kwargs(ExecutionContext(kind="interactive", session_id="s1"), "s1") == {}
    trace, _ = _runner(entity_profile_use="trace")
    assert trace._entity_profile_kwargs(ExecutionContext(kind="mcp", session_id="s1"), "s1") == {
        "entity_profiles": True
    }


def _recording_pre_turn(r) -> list[dict]:
    seen: list[dict] = []
    preset = r._cognitive.preset

    async def pre_turn(agent_id, session_id, user_input, session=None, **kwargs):
        seen.append(kwargs)
        return preset

    r._cognitive.pre_turn = pre_turn
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_both_chat_paths_hand_the_flag_to_pre_turn(path):
    r, _calls = _runner(entity_profile_use="on")
    seen = _recording_pre_turn(r)
    assert await _drive(r, path, []) == "done"
    assert seen[0]["entity_profiles"] is True


@pytest.mark.asyncio
async def test_off_and_a_background_turn_call_pre_turn_as_before():
    off, _ = _runner()
    seen_off = _recording_pre_turn(off)
    assert await _drive(off, "run_turn", []) == "done"
    assert "entity_profiles" not in seen_off[0]
    on, _ = _runner(entity_profile_use="on")
    seen_on = _recording_pre_turn(on)
    on._call_api, _sent = _scripted_api([])
    try:
        await on.run_turn(
            "schedule-1", "go", is_background=True, context=ExecutionContext(kind="scheduled", session_id="schedule-1")
        )
    finally:
        on._api_shared = True
        await on.close()
    assert "entity_profiles" not in seen_on[0]


# ---- the layer and the turn record ------------------------------------------------------------------------------

RECORD = {
    "mode": "trace",
    "matched": ["orrin"],
    "profiles": [{"key": "orrin", "lines": 2}],
    "lines": 2,
    "skipped": {**ZERO_SKIPS, "already_shown": 1},
    "tokens": 30,
}


@pytest.mark.asyncio
async def test_pre_turn_hands_the_flag_to_build_and_the_record_to_the_turn():
    layer = _mock_layer(_layer_settings())
    layer._context.build = AsyncMock(return_value=BuildResult(system_prompt="t", entity_profiles=RECORD))
    turn = await layer.pre_turn("a", "s1", "how is the orrin project doing?", session=None, entity_profiles=True)
    assert layer._context.build.call_args.kwargs["entity_profiles"] is True
    assert turn.entity_profiles == RECORD
    layer._context.build = AsyncMock(return_value=BuildResult(system_prompt="t"))
    turn = await layer.pre_turn("a", "s2", "how is the orrin project doing?", session=None)
    assert "entity_profiles" not in layer._context.build.call_args.kwargs  # called exactly as before
    assert turn.entity_profiles is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_the_turn_row_carries_the_record_only_when_there_is_one(path):
    spy = _TurnSpy()
    r, _calls = _runner(recorder=spy)
    r._cognitive.preset = _context(recalled_fact_ids=["f1"], entity_profiles=RECORD)
    assert await _drive(r, path, []) == "done"
    (turn,) = spy.turns
    assert turn.memory_context == {
        "decision": [],
        "fact": ["f1"],
        "procedure": [],
        "episode": [],
        "entity_profiles": RECORD,
    }


@pytest.mark.postgres_only
async def test_the_recorder_stores_the_record_whole_and_still_caps_the_ids(db):
    agent = _agent()
    writer = BoundedWriter("test")
    recorder = ToolCallRecorder(
        writer,
        db,
        agent,
        SimpleNamespace(tool_input_max_chars=2000, tool_result_preview_chars=500, tool_reason_max_chars=500),
    )
    facts = [f"f{i}" for i in range(60)]
    try:
        recorder.record_turn(_turn(memory_context={"fact": facts, "entity_profiles": RECORD}))
        await writer.drain(5.0)
        (row,) = await _rows(db, agent)
        assert _json(row["memory_context"]) == {"fact": facts[:50], "entity_profiles": RECORD}
    finally:
        await _delete(db, agent)


# ---- the section (real Postgres) --------------------------------------------------------------------------------

pg = pytest.mark.postgres_only


async def _engine(db, mock_embeddings, settings, mode: str = "on", **flags):
    engine, heart, s = await _fresh_engine(
        db,
        mock_embeddings,
        settings,
        identity_prompt="",
        settings_update={"entity_profile_use": mode, "budget_scale_enabled": False, **flags},
    )
    heart.search_facts = AsyncMock(return_value=[])  # Relevant Facts only where a test sets them
    return engine, heart, s


async def _facts(db, agent: str, *specs) -> list[Fact]:
    rows = []
    for i, spec in enumerate(specs):
        spec = {"content": spec} if isinstance(spec, str) else spec
        fields = {
            "category": "project",
            "subject": f"topic {i}",
            "source": "user_direct",
            "confidence": 0.9,
            "learned_at": datetime(2026, 3, 1, 12, tzinfo=UTC) + timedelta(days=i),
            **spec,
        }
        rows.append(Fact(id=uuid.uuid4(), agent_id=agent, **fields))
    async with db.session() as s:
        s.add_all(rows)
        await s.commit()
    return rows


def _line(text: str, *facts: Fact, said_on: str = "2000-01-01") -> dict:
    """A stored line. ``said_on`` is what the writer stored; the reader dates the line from its facts instead."""
    return {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "fact_ids": [str(f.id) for f in facts],
        "said_on": said_on,
        "date_from": "learned_at",
    }


async def _profile(db, agent: str, key: str, *lines: dict, status="profile", active=True, name=None, kind="project"):
    async with db.session() as s:
        s.add(
            EntityProfile(
                agent_id=agent,
                entity_key=key,
                status=status,
                display_name=name or key.title(),
                kind=kind,
                lines=list(lines),
                previous_lines=[],
                rejected=[],
                source_hash="h",
                active=active,
            )
        )
        await s.commit()


async def _build(engine, s, message: str, *, eligible: bool = True, session=None, plan=None, session_id=None):
    return await engine.build(
        agent_id=s.agent_id,
        session_id=session_id or f"s-{uuid.uuid4().hex[:6]}",
        input_text=message,
        frame=_frame(),
        session=session,
        retrieval_plan=plan,
        **({"entity_profiles": True} if eligible else {}),
    )


def _section(result):
    return next((sec for sec in result.sections if sec.label == "Entity Profiles"), None)


def _content(result) -> list[tuple[str, str]]:
    """Every section but the clock, which changes by the minute."""
    return [(sec.label, sec.content) for sec in result.sections if sec.label != "Current Date/Time"]


def _hit(f: Fact, *, category: str = "project") -> FactSummary:
    return FactSummary(
        id=f.id, content=f.content, category=category, subject=f.subject, confidence=0.9, active=True, score=0.9
    )


async def _orrin(db, agent: str) -> tuple[Fact, Fact]:
    f1, f2 = await _facts(
        db,
        agent,
        {"content": "orrin moved to the new build host", "mentioned_at": datetime(2026, 3, 5, 9, tzinfo=UTC)},
        {"content": "orrin ships a release every week", "mentioned_at": datetime(2026, 3, 9, 9, tzinfo=UTC)},
    )
    await _profile(
        db,
        agent,
        "orrin",
        _line("Orrin moved to the new build host.", f1),
        _line("Orrin ships weekly.", f2),
        name="Orrin",
    )
    return f1, f2


ORRIN = "### Orrin (project)\n- (2026-03-09) Orrin ships weekly.\n- (2026-03-05) Orrin moved to the new build host."


def _no_store(monkeypatch) -> None:
    async def _boom(*_a, **_k):
        raise AssertionError("the store was read")

    monkeypatch.setattr(EntityProfileStore, "profiled_vocabulary", _boom)
    monkeypatch.setattr(EntityProfileStore, "turn_profiles", _boom)


@pg
async def test_on_adds_the_section_between_relevant_facts_and_session_profile(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, profile_intent_leg_enabled=True)
    try:
        await _orrin(db, s.agent_id)
        (other,) = await _facts(db, s.agent_id, "the orrin dashboard reads the queue table")
        # the Session Profile leg's hit: not stored, so the User Profile section does not claim it first
        leg = FactSummary(
            id=uuid.uuid4(),
            content="prefers orrin builds at night",
            category="preference",
            subject="builds",
            confidence=0.9,
            active=True,
            score=0.9,
        )

        async def _search(*_a, **kwargs):
            return [leg] if kwargs.get("include_categories") else [_hit(other)]

        heart.search_facts = _search
        result = await _build(engine, s, "how is orrin doing?")
        section = _section(result)
        assert section.content == ORRIN
        assert (section.priority, section.tier) == (6, "dynamic")
        labels = [sec.label for sec in sorted(result.sections, key=lambda sec: sec.priority)]
        at = labels.index("Entity Profiles")
        assert labels[at - 1 : at + 2] == ["Relevant Facts", "Entity Profiles", "Session Profile"]
        assert f"## Entity Profiles\n\n{ORRIN}" in result.system_prompt
        assert f"## Entity Profiles\n\n{ORRIN}" in result.sections_by_tier["dynamic"]
        assert result.entity_profiles == {
            "mode": "on",
            "matched": ["orrin"],
            "profiles": [{"key": "orrin", "lines": 2}],
            "lines": 2,
            "skipped": ZERO_SKIPS,
            "tokens": len(ORRIN) // 4,
        }
    finally:
        await heart.close()


@pg
async def test_trace_leaves_the_prompt_as_off_and_records_what_on_shows(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "trace")
    try:
        await _orrin(db, s.agent_id)
        traced = await _build(engine, s, "how is orrin doing?")
        engine._settings = engine._settings.model_copy(update={"entity_profile_use": "off"})  # read on every turn
        off = await _build(engine, s, "how is orrin doing?")
        engine._settings = engine._settings.model_copy(update={"entity_profile_use": "on"})
        on = await _build(engine, s, "how is orrin doing?")
        assert _section(traced) is None and _content(traced) == _content(off)
        assert traced.system_prompt.split("\n", 4)[4] == off.system_prompt.split("\n", 4)[4]
        assert off.entity_profiles is None
        assert _section(on).content == ORRIN
        assert traced.entity_profiles == {**on.entity_profiles, "mode": "trace"}
        assert traced.entity_profiles["lines"] == 2
    finally:
        await heart.close()


@pg
async def test_off_never_reads_and_leaves_the_context_as_it_was(db, mock_embeddings, settings, monkeypatch):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "off")
    try:
        before = await _build(engine, s, "how is orrin doing?")
        await _orrin(db, s.agent_id)
        _no_store(monkeypatch)
        after = await _build(engine, s, "how is orrin doing?")
        assert _content(after) == _content(before) and after.entity_profiles is None
    finally:
        await heart.close()


@pg
async def test_a_turn_the_runner_did_not_mark_never_reads(db, mock_embeddings, settings, monkeypatch):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on")
    try:
        await _orrin(db, s.agent_id)
        _no_store(monkeypatch)
        result = await _build(engine, s, "how is orrin doing?", eligible=False)
        assert _section(result) is None and result.entity_profiles is None
    finally:
        await heart.close()


@pg
async def test_a_turn_that_recalls_no_facts_gets_nothing(db, mock_embeddings, settings, monkeypatch):
    engine, heart, s = await _engine(
        db,
        mock_embeddings,
        settings,
        "on",
        context_skip_unchanged_query=True,
        context_query_mode="conditional",
    )
    try:
        await _orrin(db, s.agent_id)
        # a greeting or a short ack: the plan skips facts
        greeting = await _build(engine, s, "hi orrin", plan=RetrievalPlan(skip_types={"fact"}))
        assert _section(greeting) is None and greeting.entity_profiles is None
        # the same query again in one session: the memory legs are skipped
        plan = RetrievalPlan(queries=[RetrievalQuery(memory_type="fact", query_text="orrin status")])
        first = await _build(engine, s, "orrin status?", plan=plan, session_id="s-same")
        assert _section(first).content == ORRIN
        _no_store(monkeypatch)
        again = await _build(engine, s, "orrin status?", plan=plan, session_id="s-same")
        assert _section(again) is None and again.entity_profiles is None
    finally:
        await heart.close()


@pg
async def test_only_an_active_profile_row_matches_and_never_an_owner_key(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on", entity_profile_owner_keys=["Quill"])
    try:
        (f,) = await _facts(db, s.agent_id, "a live fact")
        for key, status, active in (
            ("alder", "owner", True),
            ("birch", "not_entity", True),
            ("cedar", "asked", True),
            ("dogwood", "failed", True),
            ("elm", "profile", False),
        ):
            await _profile(db, s.agent_id, key, _line(f"{key} line", f), status=status, active=active)
        await _profile(db, s.agent_id, "quill", _line("Quill line", f), kind="person")
        result = await _build(engine, s, "alder birch cedar dogwood elm quill")
        assert _section(result) is None
        assert result.entity_profiles["matched"] == []
    finally:
        await heart.close()


@pg
async def test_the_turn_read_checks_status_and_active_itself(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on")
    try:
        await _orrin(db, s.agent_id)
        assert _section(await _build(engine, s, "orrin?")).content == ORRIN  # the vocabulary is cached now

        async def _set(**values):
            async with db.session() as session:
                await session.execute(
                    update(EntityProfile)
                    .where(EntityProfile.agent_id == s.agent_id, EntityProfile.entity_key == "orrin")
                    .values(**values)
                )
                await session.commit()

        await _set(status="not_entity")
        verdict = await _build(engine, s, "orrin?")
        assert verdict.entity_profiles["matched"] == ["orrin"] and _section(verdict) is None
        await _set(status="profile", active=False)
        removed = await _build(engine, s, "orrin?")
        assert removed.entity_profiles["matched"] == ["orrin"] and _section(removed) is None
    finally:
        await heart.close()


@pg
async def test_only_lines_whose_cited_facts_are_all_live_are_shown(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on")
    other_agent = f"test-upf-{uuid.uuid4().hex[:8]}"
    try:
        live, gone, old, new = await _facts(db, s.agent_id, "live one", "gone one", "old value", "new value")
        (foreign,) = await _facts(db, other_agent, "another agent's fact")
        await _profile(
            db,
            s.agent_id,
            "juniper",
            _line("Live line.", live),
            _line("Inactive line.", gone),
            _line("Superseded line.", old),
            _line("Half live line.", live, gone),
            _line("Foreign line.", foreign),
            {"id": "x", "text": "No citations.", "fact_ids": []},
            {"id": "y", "text": "Not an id.", "fact_ids": ["not-a-uuid"]},
            {"id": "z", "text": "Ids not a list.", "fact_ids": "nope"},
            {"id": "w", "text": "A number among the ids.", "fact_ids": [str(live.id), 7]},
            {"id": "v", "text": "A list among the ids.", "fact_ids": [str(live.id), ["x"]]},
            "not a line",
        )
        # a profile none of whose lines is live: not in the vocabulary at all
        await _profile(db, s.agent_id, "yew", _line("Inactive line.", gone), _line("Superseded line.", old))
        async with db.session() as session:
            await session.execute(update(Fact).where(Fact.id == gone.id).values(active=False))
            await session.execute(update(Fact).where(Fact.id == old.id).values(superseded_by=new.id))
            await session.commit()
        result = await _build(engine, s, "juniper and yew")
        assert result.entity_profiles["matched"] == ["juniper"]
        assert _section(result).content == "### Juniper (project)\n- (2026-03-01) Live line."
        assert result.entity_profiles["skipped"] == {**ZERO_SKIPS, "not_live": 9}
    finally:
        await heart.close()


@pg
async def test_another_agents_profile_is_never_shown(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on")
    other_agent = f"test-upf-{uuid.uuid4().hex[:8]}"
    try:
        (theirs,) = await _facts(db, other_agent, "kestrel is their project")
        await _profile(db, other_agent, "kestrel", _line("Kestrel is theirs.", theirs))
        result = await _build(engine, s, "what about kestrel?")
        assert _section(result) is None and result.entity_profiles["matched"] == []
    finally:
        await heart.close()


@pg
async def test_a_line_citing_a_fact_the_profile_card_cites_is_skipped(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on", profile_card_enabled=True)
    try:
        carded, plain = await _facts(
            db, s.agent_id, {"content": "the user sails on lake tern", "category": "preference"}, "tern has a club"
        )
        await ProfileCardStore(db, s.agent_id).write(
            [{"id": "c1", "text": "The user sails on Lake Tern.", "fact_ids": [str(carded.id)]}], "h"
        )
        await _profile(
            db,
            s.agent_id,
            "tern",
            _line("The user sails here.", carded, plain),
            _line("Tern has a club.", plain),
            kind="place",
        )
        result = await _build(engine, s, "anything new at tern?")
        assert "The user sails on Lake Tern." in result.system_prompt  # the card is in the prompt
        assert _section(result).content == "### Tern (place)\n- (2026-03-02) Tern has a club."
        assert result.entity_profiles["skipped"] == {**ZERO_SKIPS, "owner_card": 1}
    finally:
        await heart.close()


@pg
async def test_a_line_is_skipped_only_when_its_facts_reached_relevant_facts(db, mock_embeddings, settings):
    # a facts budget of 25 tokens (100 characters): the first fact's line (82 characters) fits, and only a few
    # characters of the second are left, so the second counts as cut (not even its first 40 characters reached it)
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on", context_budget_overrides={"facts": 25})
    try:
        shown, cut = await _facts(
            db,
            s.agent_id,
            {"content": "wren builds on tuesdays and ships the build on wednesdays", "subject": "a"},
            {"content": "wren keeps its release notes in the docs folder of the main tree", "subject": "b"},
        )
        await _profile(
            db,
            s.agent_id,
            "wren",
            _line("Wren builds on Tuesdays.", shown),
            _line("Wren keeps notes in the docs folder.", cut),
            _line("Wren builds and keeps notes.", shown, cut),
        )
        heart.search_facts = AsyncMock(return_value=[_hit(shown), _hit(cut)])
        result = await _build(engine, s, "wren")
        facts_section = next(sec for sec in result.sections if sec.label == "Relevant Facts")
        assert "wren builds on tuesdays" in facts_section.content
        assert "release notes in the docs folder" not in facts_section.content
        assert {str(shown.id), str(cut.id)} <= set(result.recalled_ids["fact"])  # both recalled, one cut
        assert _section(result).content == (
            "### Wren (project)\n- (2026-03-02) Wren keeps notes in the docs folder.\n"
            "- (2026-03-02) Wren builds and keeps notes."
        )
        assert result.entity_profiles["skipped"] == {**ZERO_SKIPS, "already_shown": 1}
    finally:
        await heart.close()


@pg
async def test_without_a_relevant_facts_section_nothing_counts_as_shown(db, mock_embeddings, settings, monkeypatch):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on")
    try:
        (shown,) = await _facts(db, s.agent_id, "wren builds on tuesdays")
        await _profile(db, s.agent_id, "wren", _line("Wren builds on Tuesdays.", shown))
        heart.search_facts = AsyncMock(return_value=[_hit(shown)])
        real = engine._format_facts

        def _fails_for_the_facts_leg(facts, **kwargs):
            if "budget_tokens" in kwargs:  # only the Relevant Facts call passes a budget
                raise RuntimeError("format failed")
            return real(facts, **kwargs)

        monkeypatch.setattr(engine, "_format_facts", _fails_for_the_facts_leg)
        result = await _build(engine, s, "wren")
        assert all(sec.label != "Relevant Facts" for sec in result.sections)
        assert str(shown.id) in result.recalled_ids["fact"]  # recalled before the format raised
        assert _section(result).content == "### Wren (project)\n- (2026-03-01) Wren builds on Tuesdays."
    finally:
        await heart.close()


@pg
async def test_the_section_is_cut_by_whole_lines_then_whole_profiles(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on")
    try:
        facts = await _facts(db, s.agent_id, *(f"fact {i}" for i in range(17)))
        lines = {
            key: [_line(f"{key.title()} line {i:02d} " + "x" * 130, facts[n + i]) for i in range(count)]
            for key, n, count in (("golf", 0, 8), ("hotel", 8, 8), ("india", 16, 1))
        }
        for key, key_lines in lines.items():
            await _profile(db, s.agent_id, key, *key_lines)
        result = await _build(engine, s, "golf hotel india")
        content = _section(result).content
        cap = engine._scaled_budget(ep.TURN_MAX_TOKENS) * 4
        assert len(content) <= cap
        rendered = content.split("\n")
        stored = {line["text"] for key_lines in lines.values() for line in key_lines}
        for line in rendered:  # never a cut line
            assert line == "" or line.startswith("### ") or line.split(") ", 1)[1] in stored, line
        headings = [line for line in rendered if line.startswith("### ")]
        assert headings == ["### Golf (project)", "### Hotel (project)"]  # india, last, went whole
        shown = sum(1 for line in rendered if line.startswith("- "))
        assert sum(1 for line in rendered if line.startswith("- (") and "Golf line" in line) == 8
        record = result.entity_profiles
        assert record["skipped"] == {**ZERO_SKIPS, "budget": 17 - shown}
        assert record["profiles"] == [{"key": "golf", "lines": 8}, {"key": "hotel", "lines": shown - 8}]
        # one more line of hotel would not have fit
        assert len(content) + len(lines["hotel"][0]["text"]) + len("\n- (2026-03-01) ") > cap
    finally:
        await heart.close()


@pg
async def test_a_line_is_dated_as_the_context_engine_reads_a_statement_time(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on")
    try:
        # said late in the evening five hours west of UTC: the next day in UTC; learned days later
        (said,) = await _facts(
            db,
            s.agent_id,
            {
                "content": "pike opened a second office",
                "mentioned_at": datetime(2026, 3, 10, 23, 30, tzinfo=timezone(timedelta(hours=-5))),
                "learned_at": datetime(2026, 3, 14, 8, tzinfo=UTC),
            },
        )
        await _profile(db, s.agent_id, "pike", _line("Pike opened a second office.", said, said_on="2026-03-01"))
        async with db.session() as session:
            stored = await session.get(Fact, said.id)
        day = as_date(_stated_at(stored, mentioned_first=True)).isoformat()
        assert day == "2026-03-11" == ep._stated_on(stored)  # the writer's date of the same fact
        assert (
            _section(await _build(engine, s, "pike")).content
            == f"### Pike (project)\n- ({day}) Pike opened a second office."
        )
    finally:
        await heart.close()


@pg
async def test_a_naive_statement_time_is_read_as_utc(db, mock_embeddings, settings, monkeypatch):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on")
    try:
        fact = SimpleNamespace(mentioned_at=datetime(2026, 3, 10, 23, 30), learned_at=datetime(2026, 3, 14, 8))
        row = {"display_name": "Pike", "kind": "place", "lines": [_line("Pike line.")], "facts": {}}
        row["lines"][0]["fact_ids"] = ["f-1"]
        row["facts"]["f-1"] = fact
        monkeypatch.setattr(EntityProfileStore, "profiled_vocabulary", AsyncMock(return_value=frozenset({"pike"})))
        monkeypatch.setattr(EntityProfileStore, "turn_profiles", AsyncMock(return_value={"pike": row}))
        content = _section(await _build(engine, s, "pike")).content
        assert content == f"### Pike (place)\n- ({as_date(_stated_at(fact, mentioned_first=True))}) Pike line."
        assert "(2026-03-10)" in content
    finally:
        await heart.close()


@pg
@pytest.mark.parametrize("failing", ["vocabulary", "profiles"])
@pytest.mark.parametrize("shared_session", [False, True])
async def test_a_failed_read_leaves_the_context_as_with_off(
    db, mock_embeddings, settings, monkeypatch, caplog, failing, shared_session
):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on")
    try:
        await _orrin(db, s.agent_id)
        engine._settings = engine._settings.model_copy(update={"entity_profile_use": "off"})
        off = await _build(engine, s, "how is orrin doing?")
        engine._settings = engine._settings.model_copy(update={"entity_profile_use": "on"})

        async def _fail(self, *_a, session=None, **_k):
            if session is None:
                raise RuntimeError("profile store down")
            await session.execute(text("SELECT 1/0"))  # without a savepoint this aborts the pre-turn transaction

        name = "profiled_vocabulary" if failing == "vocabulary" else "turn_profiles"
        monkeypatch.setattr(EntityProfileStore, name, _fail)
        with caplog.at_level(logging.WARNING, logger="nous.cognitive.context"):
            if shared_session:
                async with db.session() as session:
                    result = await _build(engine, s, "how is orrin doing?", session=session)
                    assert (await session.execute(text("SELECT 1"))).scalar_one() == 1  # still usable
            else:
                result = await _build(engine, s, "how is orrin doing?")
        assert _content(result) == _content(off)
        assert result.entity_profiles["error"] in ("RuntimeError", "DBAPIError")
        assert len([r for r in caplog.records if "Entity profiles" in r.getMessage()]) == 1
    finally:
        await heart.close()


@pg
async def test_the_writers_write_is_seen_on_the_next_turn(db, mock_embeddings, settings):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on")
    try:
        (f,) = await _facts(db, s.agent_id, "lima has a harbour")
        assert (await _build(engine, s, "lima")).entity_profiles["matched"] == []  # an empty vocabulary is cached
        await EntityProfileStore(db, s.agent_id).write(
            "lima",
            "profile",
            "h",
            fact_count=1,
            source_count=1,
            lines=[_line("Lima has a harbour.", f)],
            display_name="Lima",
            kind="place",
        )
        assert (
            _section(await _build(engine, s, "lima")).content == "### Lima (place)\n- (2026-03-01) Lima has a harbour."
        )
    finally:
        await heart.close()


@pg
async def test_a_row_written_by_another_process_is_seen_after_the_cache_ages(
    db, mock_embeddings, settings, monkeypatch
):
    engine, heart, s = await _engine(db, mock_embeddings, settings, "on")
    try:
        (f,) = await _facts(db, s.agent_id, "oslo has a fjord")
        clock = [1000.0]
        monkeypatch.setattr(ep.time, "monotonic", lambda: clock[0])
        assert (await _build(engine, s, "oslo")).entity_profiles["matched"] == []
        await _profile(db, s.agent_id, "oslo", _line("Oslo has a fjord.", f), kind="place")  # no invalidation
        clock[0] += ep.VOCAB_TTL_SECONDS - 1
        assert (await _build(engine, s, "oslo")).entity_profiles["matched"] == []
        clock[0] += 2
        assert (await _build(engine, s, "oslo")).entity_profiles["matched"] == ["oslo"]
    finally:
        await heart.close()


# ---- the section marker -----------------------------------------------------------------------------------------


def test_the_section_is_reported_under_entity_profiles():
    prompt = f"## Relevant Facts\n\n- [a] a fact\n\n## Entity Profiles\n\n{ORRIN}\n\n## Session Profile\n\n- [b] x"
    sections = parse_system_sections(prompt)
    # without the marker the heading would fold into Relevant Facts, whose stored text may carry its own headings
    assert sections["entity_profiles"] == ORRIN
    assert sections["relevant_facts"] == "- [a] a fact"
    stats = analyze_context(prompt, [], None, 200_000)
    assert stats.section_tokens_est["entity_profiles"] == len(ORRIN) // 4
