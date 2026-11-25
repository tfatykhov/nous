"""Every fact writer records when its fact was said (``mentioned_at``), and never invents it.

The rule (``FactInput.mentioned_at``): a writer that runs inside the turn passes the turn time; a writer that runs
after the episode passes the episode's start; a derived fact takes the latest statement time of its sources. A writer
with no such time leaves it None, and a said-time never goes into ``event_date``/``valid_from``/``valid_to``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from nous.config import Settings
from nous.events import Event
from nous.heart.schemas import EpisodeInput, FactInput
from nous.storage.models import Fact

EPISODE_START = datetime(2024, 4, 11, 9, 30, tzinfo=UTC)
SAID = datetime(2024, 5, 1, 14, 0, tzinfo=UTC)


def _no_event_time(fi: FactInput) -> bool:
    return (fi.event_date, fi.valid_from, fi.valid_to) == (None, None, None)


def _learned(heart) -> list[FactInput]:
    return [c.args[0] for c in heart.learn.call_args_list]


def _own_settings(agent: str) -> Settings:
    return Settings().model_copy(update={"agent_id": agent, "admission_control_enabled": False})


# ---------------------------------------------------------------------------
# The rule for a derived fact
# ---------------------------------------------------------------------------


def test_latest_statement_time_is_the_latest_known_time_or_none():
    from nous.heart.facts import latest_statement_time

    assert latest_statement_time([]) is None
    assert latest_statement_time([None, None]) is None
    assert latest_statement_time([EPISODE_START, None, SAID]) == SAID
    assert latest_statement_time(t for t in (SAID, EPISODE_START)) == SAID
    assert latest_statement_time([MagicMock(), EPISODE_START]) == EPISODE_START  # not a time: unknown


# ---------------------------------------------------------------------------
# Writers inside the turn: the turn time
# ---------------------------------------------------------------------------


def _within(before: datetime, fi: FactInput, after: datetime) -> bool:
    return fi.mentioned_at is not None and before <= fi.mentioned_at <= after


async def test_the_attachment_fact_is_stamped_with_the_turn_time():
    from nous.api.attachment_store import record_attachment_fact

    heart = MagicMock()
    heart.learn = AsyncMock(return_value=MagicMock())
    att = SimpleNamespace(content_type="application/pdf", filename="plan.pdf", workspace_path="", source="telegram")
    episode = uuid4()
    before = datetime.now(UTC)
    await record_attachment_fact(heart, att, agent_id="a", source_episode_id=str(episode), analysis="A plan.")
    after = datetime.now(UTC)
    (fi,) = _learned(heart)
    assert _within(before, fi, after)
    assert fi.source_episode_id == episode and _no_event_time(fi)


async def test_the_learn_fact_tool_is_stamped_with_the_turn_time():
    from nous.api.tools import create_nous_tools

    heart = MagicMock()
    heart.learn = AsyncMock(
        return_value=SimpleNamespace(id=uuid4(), category="technical", subject="billing", contradiction_warning=None)
    )
    learn_fact = create_nous_tools(MagicMock(), heart, Settings(_env_file=None))["learn_fact"]
    episode = uuid4()
    before = datetime.now(UTC)
    out = await learn_fact(
        content="The billing service deploys every Tuesday after the review.",
        category="technical",
        subject="billing",
        source_episode_id=str(episode),
    )
    after = datetime.now(UTC)
    assert "Fact learned successfully" in out["content"][0]["text"]
    (fi,) = _learned(heart)
    assert _within(before, fi, after)
    assert fi.source == "user_direct" and fi.source_episode_id == episode and _no_event_time(fi)


async def test_a_fact_learned_by_a_script_is_stamped_with_the_turn_time():
    from nous.api.tools import create_programmatic_tools

    heart = AsyncMock()
    heart.learn = AsyncMock(return_value=MagicMock())
    episode = uuid4()
    run_python = create_programmatic_tools(
        AsyncMock(), heart, Settings(_env_file=None), episode_id_resolver=lambda _sid: str(episode)
    )["run_python"]
    before = datetime.now(UTC)
    out = await run_python(
        code='result = learn_fact("The billing service deploys every Tuesday.", category="technical")',
        _session_id="s-script",
    )
    after = datetime.now(UTC)
    assert "stored" in out["content"][0]["text"]
    (fi,) = _learned(heart)
    assert _within(before, fi, after)
    assert fi.source_episode_id == episode and _no_event_time(fi)


async def test_an_inline_correction_is_stamped_with_the_turn_time():
    from nous.cognitive.monitor import MonitorEngine

    heart = MagicMock()
    heart.learn = AsyncMock()
    settings = MagicMock()
    settings.correction_extraction_enabled = True
    settings.background_model = "test-model"
    settings.correction_input_max_chars = 2000
    settings.correction_max_tokens = 1024
    settings.correction_min_principle_chars = 20
    monitor = MonitorEngine(MagicMock(), heart, settings)
    monitor._llm_client = AsyncMock()
    episode = uuid4()
    reply = {
        "principle": "Always confirm the file path before changing a file",
        "subject": "file operations",
        "is_censor": False,
        "censor_pattern": None,
        "confidence": 0.8,
    }
    with patch("nous.handlers.call_background_llm", new_callable=AsyncMock, return_value=json.dumps(reply)):
        before = datetime.now(UTC)
        await monitor.detect_and_extract_correction(
            user_message="No, actually I meant the other file",
            ai_response="I'll update the other file instead",
            session_id="s-correct",
            episode_id=str(episode),
        )
        after = datetime.now(UTC)
    (fi,) = _learned(heart)
    assert _within(before, fi, after)
    assert fi.source == "inline_correction" and fi.source_episode_id == episode and _no_event_time(fi)


def _pre_prune_inputs(heart) -> list[FactInput]:
    return [fi for fi in _learned(heart) if fi.source == "pre_prune_extraction"]


async def test_a_fact_pulled_from_a_tool_result_in_the_tool_loop_is_stamped_with_the_turn_time():
    from test_status_line_loop import _loop, _reply, _runner, _tool_round

    runner = _runner(on=False, pruning=True)
    runner._heart.learn = AsyncMock()
    runner._compactor.prune_tool_results = MagicMock(return_value=["Port 8080 serves the billing API."])
    before = datetime.now(UTC)
    await _loop(runner, [_tool_round(1), _reply()])
    after = datetime.now(UTC)
    (fi,) = _pre_prune_inputs(runner._heart)
    assert fi.content == "Port 8080 serves the billing API."
    assert _within(before, fi, after) and _no_event_time(fi)


async def test_a_fact_pulled_from_a_tool_result_in_the_streaming_loop_is_stamped_with_the_turn_time():
    from test_streaming import _make_mock_cognitive, _make_mock_settings, _make_runner

    from nous.api.anthropic_client import StreamEvent

    cognitive, _ = _make_mock_cognitive()
    runner = _make_runner(cognitive, _make_mock_settings())
    runner._heart.learn = AsyncMock()
    runner._compactor = MagicMock()
    runner._compactor.prune_tool_results.return_value = ["Port 8080 serves the billing API."]
    calls = {"n": 0}

    async def fake_stream(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield StreamEvent(type="tool_start", tool_name="web_search", tool_id="t1", block_index=1)
            yield StreamEvent(type="tool_input_delta", text='{"query": "billing port"}', block_index=1)
            yield StreamEvent(type="block_stop", block_index=1)
            yield StreamEvent(type="done", stop_reason="tool_use")
        else:
            yield StreamEvent(type="text_delta", text="It is 8080.")
            yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=fake_stream)
    before = datetime.now(UTC)
    [e async for e in runner.stream_chat("s-stream", "Which port serves billing?")]
    after = datetime.now(UTC)
    (fi,) = _pre_prune_inputs(runner._heart)
    assert _within(before, fi, after) and _no_event_time(fi)


# ---------------------------------------------------------------------------
# Writers after the episode: the episode's start
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_a_session_reflection_lesson_gets_its_episode_start(db, session, mock_embeddings):
    from nous.brain.brain import Brain
    from nous.cognitive.layer import CognitiveLayer
    from nous.cognitive.schemas import SessionMetadata
    from nous.heart.heart import Heart

    agent = f"test-said-refl-{uuid4().hex[:8]}"
    settings = _own_settings(agent)
    heart = Heart(db, settings, embedding_provider=mock_embeddings)
    brain = Brain(database=db, settings=settings)
    try:
        cognitive = CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")
        ep = await heart.start_episode(EpisodeInput(summary="Billing release planning."), session=session)
        await session.execute(
            text("UPDATE heart.episodes SET started_at = :t WHERE id = :i"), {"t": EPISODE_START, "i": ep.id}
        )
        session.expire_all()
        sid = f"s-{agent}"
        cognitive._active_episodes[sid] = str(ep.id)
        cognitive._session_metadata[sid] = SessionMetadata(
            turn_count=4, tools_used={"bash"}, total_user_chars=600, total_assistant_chars=900
        )
        lesson = "The billing service ships on Tuesdays after the weekly review."
        await cognitive.end_session(agent, sid, reflection=f"Summary.\n- learned: {lesson}\n", session=session)
        rows = (
            (await session.execute(select(Fact).where(Fact.agent_id == agent, Fact.source == "reflection")))
            .scalars()
            .all()
        )
        assert [(r.content, r.source_episode_id, r.mentioned_at) for r in rows] == [(lesson, ep.id, EPISODE_START)]
        assert (rows[0].event_date, rows[0].valid_from, rows[0].valid_to) == (None, None, None)
    finally:
        await heart.close()
        await brain.close()


def _correction_extractor(episode_row):
    from test_correction_extractor import _AsyncCtx

    from nous.handlers.correction_extractor import CorrectionExtractor

    settings = MagicMock()
    settings.correction_extraction_enabled = True
    settings.background_model = "test-model"
    result = MagicMock()
    result.scalar_one_or_none.return_value = episode_row
    db_session = AsyncMock()
    if isinstance(episode_row, Exception):
        db_session.execute = AsyncMock(side_effect=episode_row)
    else:
        db_session.execute = AsyncMock(return_value=result)
    db = MagicMock()
    db.session = MagicMock(return_value=_AsyncCtx(db_session))
    heart = MagicMock()
    heart.learn = AsyncMock()
    heart.add_censor = AsyncMock()
    bus = MagicMock()
    extractor = CorrectionExtractor(
        db=db, settings=settings, bus=bus, llm_client=AsyncMock(), heart=heart, agent_id="test"
    )
    return extractor, heart


_CORRECTION = json.dumps(
    {
        "principle": "Always ask which environment is meant before running a deploy",
        "subject": "deploys",
        "is_censor": False,
        "censor_pattern": None,
        "confidence": 0.85,
    }
)


def _corrected_event() -> Event:
    return Event(
        type="outcome_signals_detected",
        agent_id="test",
        data={"episode_id": str(uuid4()), "signals": [{"type": "corrected", "confidence": 0.9, "evidence": "no"}]},
    )


async def test_a_correction_from_the_outcome_signals_gets_its_episode_start():
    episode = MagicMock(transcript="User: no, staging", structured_summary={}, summary="", started_at=EPISODE_START)
    extractor, heart = _correction_extractor(episode)
    with patch("nous.handlers.correction_extractor.call_background_llm", new_callable=AsyncMock) as llm:
        llm.return_value = _CORRECTION
        await extractor.handle(_corrected_event())
    (fi,) = _learned(heart)
    assert fi.source == "correction_extraction"
    assert fi.mentioned_at == EPISODE_START and _no_event_time(fi)


async def test_a_correction_whose_episode_cannot_be_read_is_stored_without_a_time():
    extractor, heart = _correction_extractor(RuntimeError("connection reset"))
    with patch("nous.handlers.correction_extractor.call_background_llm", new_callable=AsyncMock) as llm:
        llm.return_value = _CORRECTION
        await extractor.handle(_corrected_event())
    (fi,) = _learned(heart)
    assert fi.mentioned_at is None


async def test_exemplar_facts_get_their_episode_start():
    from test_exemplar_mode import PURE_STREAM, _StubEmbeddings

    from nous.handlers.fact_extractor import FactExtractor

    captured: list[FactInput] = []

    class _Heart:
        agent_id = "test-agent"
        _embeddings = _StubEmbeddings()

        async def get_episode(self, episode_id, session=None):
            return SimpleNamespace(started_at=EPISODE_START)

        async def learn(self, fact_input, **kw):
            captured.append(fact_input)
            return SimpleNamespace(id=uuid4())

    ext = FactExtractor.__new__(FactExtractor)
    ext._heart = _Heart()
    ext._settings = SimpleNamespace(
        exemplar_extraction_enabled=True,
        exemplar_density_threshold=0.8,
        exemplar_max_per_episode=5000,
        extraction_enumerative_enabled=False,
    )
    ext._dedup_via_search = False
    ext._llm = object()
    await ext.extract_and_store(summary={"candidate_facts": []}, episode_id=str(uuid4()), transcript=PURE_STREAM)
    assert len(captured) == 3
    assert all(fi.source == "exemplar_extractor" for fi in captured)
    assert all(fi.mentioned_at == EPISODE_START and _no_event_time(fi) for fi in captured)


async def test_the_compaction_event_names_the_open_episode():
    from test_outside_content_shadow import _Bus, _layer

    bus = _Bus()
    layer = _layer("off", bus)
    layer._heart.bump_episode_compaction_count = AsyncMock()
    episode = str(uuid4())
    layer._active_episodes["s1"] = episode
    snapshot = [{"role": "user", "content": "hello"}]
    await layer.pre_compaction("agent", "s1", snapshot)
    (event,) = bus.events
    assert event.data == {"message_snapshot": snapshot, "episode_id": episode}


_KNOWLEDGE_SNAPSHOT = [
    {"role": "user", "content": "The billing service deploys every Tuesday after the review. " + "x" * 120},
    {"role": "assistant", "content": "Noted: billing deploys on Tuesdays. " + "y" * 120},
]
_KNOWLEDGE_FACT = "The billing service deploys every Tuesday after the weekly review."


def _knowledge_llm() -> AsyncMock:
    client = AsyncMock()
    reply = [{"subject": "billing", "content": _KNOWLEDGE_FACT, "category": "technical", "confidence": 0.9}]
    client.call = AsyncMock(return_value=MagicMock(content=[{"type": "text", "text": json.dumps(reply)}]))
    return client


@pytest.mark.postgres_only
async def test_a_fact_extracted_at_compaction_gets_its_episode_start(db, mock_embeddings):
    from nous.handlers.knowledge_extractor import KnowledgeExtractor
    from nous.heart.heart import Heart

    agent = f"test-said-know-{uuid4().hex[:8]}"
    settings = _own_settings(agent)
    heart = Heart(db, settings, embedding_provider=mock_embeddings)
    try:
        ep = await heart.start_episode(EpisodeInput(summary="Billing deploy schedule."))
        async with db.session() as s:
            await s.execute(
                text("UPDATE heart.episodes SET started_at = :t WHERE id = :i"), {"t": EPISODE_START, "i": ep.id}
            )
            await s.commit()
        extractor = KnowledgeExtractor(heart, settings, MagicMock(), llm_client=_knowledge_llm())
        event = Event(
            type="conversation_compacting",
            agent_id=agent,
            session_id="s-know",
            data={"message_snapshot": _KNOWLEDGE_SNAPSHOT, "episode_id": str(ep.id)},
        )
        await extractor.handle(event)
        async with db.session() as s:
            rows = (await s.execute(select(Fact).where(Fact.agent_id == agent))).scalars().all()
        assert [(r.content, r.source, r.mentioned_at) for r in rows] == [
            (_KNOWLEDGE_FACT, "knowledge_extractor", EPISODE_START)
        ]
        assert (rows[0].event_date, rows[0].valid_from, rows[0].valid_to) == (None, None, None)
    finally:
        async with db.session() as s:
            await s.execute(text("DELETE FROM heart.facts WHERE agent_id = :a"), {"a": agent})
            await s.execute(text("DELETE FROM heart.episodes WHERE agent_id = :a"), {"a": agent})
            await s.commit()
        await heart.close()


async def test_a_fact_extracted_at_compaction_with_no_episode_has_no_time():
    from nous.handlers.knowledge_extractor import KnowledgeExtractor

    heart = MagicMock()
    heart.learn = AsyncMock()
    heart.find_similar_facts = AsyncMock(return_value=[])
    heart.get_episode = AsyncMock(side_effect=AssertionError("no episode to read"))
    settings = Settings(_env_file=None)
    extractor = KnowledgeExtractor(heart, settings, MagicMock(), llm_client=_knowledge_llm())
    event = Event(
        type="conversation_compacting", agent_id="a", session_id="s", data={"message_snapshot": _KNOWLEDGE_SNAPSHOT}
    )
    await extractor.handle(event)
    (fi,) = _learned(heart)
    assert fi.mentioned_at is None


# ---------------------------------------------------------------------------
# The sleep reflection: the latest start of the episodes it reflected on
# ---------------------------------------------------------------------------

T_FIRST = datetime(2024, 4, 10, 8, 0, tzinfo=UTC)
T_LATEST = datetime(2024, 4, 11, 9, 0, tzinfo=UTC)
T_NO_SUMMARY = datetime(2024, 4, 11, 20, 0, tzinfo=UTC)


def _reflecting_handler(reflection: dict):
    from test_sleep_handler import _make_sleep_handler

    handler, _brain, heart, _bus, llm_client = _make_sleep_handler()
    heart.list_episodes = AsyncMock(
        return_value=[
            # Newest first, as list_recent orders them. The episode with no summary is not reflected on.
            SimpleNamespace(summary="", started_at=T_NO_SUMMARY),
            SimpleNamespace(summary="Planned the billing release.", started_at=T_LATEST),
            SimpleNamespace(summary="Debugged the deploy pipeline.", started_at=T_FIRST),
        ]
    )
    heart.search_facts = AsyncMock(return_value=[])
    heart.learn = AsyncMock(return_value=MagicMock())
    heart.supersede_fact = AsyncMock()
    llm_client.call = AsyncMock(
        return_value=MagicMock(
            content=[{"type": "tool_use", "id": "toolu_1", "name": "store_reflection", "input": reflection}]
        )
    )
    return handler, heart


def _stats() -> dict:
    return {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}


async def test_sleep_reflection_facts_get_the_latest_reflected_episode_start():
    reflection = {
        "patterns": [],
        "lessons": [],
        "connections": [],
        "gaps": [],
        "summary": "",
        "facts": [
            {"subject": "billing", "content": "Billing releases follow the Tuesday review.", "category": "technical"},
            {
                "subject": "UPDATES: deploy freeze rules",
                "content": "Deploys freeze on Fridays.",
                "category": "technical",
            },
            {"subject": "UPDATES: staging owner", "content": "Dana owns staging now.", "category": "technical"},
            {"subject": "UPDATES: release cadence", "content": "Releases are weekly now.", "category": "technical"},
        ],
    }
    handler, heart = _reflecting_handler(reflection)
    matches = {
        "deploy freeze rules": [],  # no match: learned as new
        "staging owner": [SimpleNamespace(id=uuid4(), score=0.5, category="technical", subject="staging")],
        "release cadence": [
            SimpleNamespace(id=uuid4(), score=0.95, category="technical", subject="releases", content="Biweekly.")
        ],
    }
    heart.find_similar_facts = AsyncMock(side_effect=lambda content, limit=3: matches[content])
    assert await handler._phase_reflect(_stats()) is True

    learned = _learned(heart)
    assert [fi.content for fi in learned] == [
        "Billing releases follow the Tuesday review.",
        "Deploys freeze on Fridays.",
        "Dana owns staging now.",
    ]
    assert all(fi.mentioned_at == T_LATEST and _no_event_time(fi) for fi in learned)
    # The UPDATES replacement leaves the time unset, as supersede does.
    (replacement,) = [c.args[1] for c in heart.supersede_fact.call_args_list]
    assert replacement.content == "Releases are weekly now." and replacement.mentioned_at is None


async def test_sleep_reflection_summary_and_lessons_get_the_latest_reflected_episode_start():
    reflection = {
        "patterns": [],
        "lessons": ["Small releases are easier to roll back."],
        "connections": [],
        "gaps": [],
        "summary": "A day of release planning and pipeline fixes.",
        "facts": [],
    }
    handler, heart = _reflecting_handler(reflection)
    assert await handler._phase_reflect(_stats()) is True
    learned = _learned(heart)
    assert [fi.subject for fi in learned] == ["daily_reflection", "lesson_learned"]
    assert all(fi.mentioned_at == T_LATEST and _no_event_time(fi) for fi in learned)


# ---------------------------------------------------------------------------
# What _learn stores (guards: these pass before and after)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize("event_date", ["2024-04-10", None], ids=["dated", "undated"])
async def test_an_explicit_said_time_is_stored_unchanged_beside_a_source_episode(
    db, session, mock_embeddings, event_date
):
    from nous.heart.heart import Heart

    agent = f"test-said-expl-{uuid4().hex[:8]}"
    heart = Heart(db, _own_settings(agent), embedding_provider=mock_embeddings)
    try:
        ep = await heart.start_episode(EpisodeInput(summary="Canary rollout."), session=session)
        await session.execute(
            text("UPDATE heart.episodes SET started_at = :t WHERE id = :i"), {"t": EPISODE_START, "i": ep.id}
        )
        detail = await heart.learn(
            FactInput(
                content=f"The canary rollout of build {uuid4().hex[:6]} finished cleanly.",
                source_episode_id=ep.id,
                mentioned_at=SAID,
                event_date=event_date,
            ),
            session=session,
        )
        row = await session.get(Fact, detail.id)
        await session.refresh(row)
        assert row.mentioned_at == SAID
        expected_day = date.fromisoformat(event_date) if event_date else None
        assert (row.event_date, row.valid_from, row.valid_to) == (expected_day,) * 3
    finally:
        await heart.close()


@pytest.mark.postgres_only
@pytest.mark.parametrize("first", [SAID, None], ids=["timed", "untimed"])
async def test_a_duplicate_learn_leaves_the_stored_said_time_alone(db, session, mock_embeddings, first):
    from nous.heart.heart import Heart

    agent = f"test-said-dup-{uuid4().hex[:8]}"
    heart = Heart(db, _own_settings(agent), embedding_provider=mock_embeddings)
    try:
        content = f"The staging cluster {uuid4().hex[:6]} is rebuilt every Sunday night."
        stored = await heart.learn(FactInput(content=content, mentioned_at=first), session=session)
        again = await heart.learn(
            FactInput(content=content, mentioned_at=datetime(2026, 1, 2, tzinfo=UTC)), session=session
        )
        assert again.id == stored.id
        row = await session.get(Fact, stored.id)
        await session.refresh(row)
        assert row.mentioned_at == first
        count = (await session.execute(select(Fact.id).where(Fact.agent_id == agent))).all()
        assert len(count) == 1
    finally:
        await heart.close()


# ---------------------------------------------------------------------------
# Resolver pins (guards): a correction said later stays current, in both resolver copies, whether the in-turn
# writer left the said-time empty (before this change) or stored the turn time (after it).
# ---------------------------------------------------------------------------


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 4, 2, hour, minute, tzinfo=UTC)


def _case_b(stamped: bool) -> list[dict]:
    """One episode open from 09:00: May 10 said at 09:05, then moved to May 3 at 11:30, both by learn_fact."""
    first, later = _at(9, 5), _at(11, 30)
    return [
        {
            "text": "Alice's dentist appointment is on May 10",
            "day": date(2026, 5, 10),
            "learned_at": first,
            "mentioned_at": first if stamped else None,
        },
        {
            "text": "Alice's dentist appointment is on May 3",
            "day": date(2026, 5, 3),
            "learned_at": later,
            "mentioned_at": later if stamped else None,
        },
    ]


def _case_a(stamped: bool) -> list[dict]:
    """Session 2 (12:00-12:30) said June 3, stored by the extractor after it ended. At 15:00, in session 1 (open
    since 09:00), learn_fact stores the correction June 5."""
    return [
        {
            "text": "Alice's flight to Lisbon is on June 3",
            "day": date(2026, 6, 3),
            "learned_at": _at(12, 31),
            "mentioned_at": _at(12),
        },
        {
            "text": "Alice's flight to Lisbon is on June 5",
            "day": date(2026, 6, 5),
            "learned_at": _at(15),
            "mentioned_at": _at(15) if stamped else None,
        },
    ]


_CASES = {"case_a": _case_a, "case_b": _case_b}


@pytest.mark.parametrize("case", sorted(_CASES))
@pytest.mark.parametrize("stamped", [False, True], ids=["said_time_empty", "said_time_turn"])
@pytest.mark.parametrize("intervals", [False, True], ids=["intervals_off", "intervals_on"])
def test_the_recall_deep_resolver_keeps_the_later_correction_current(case, stamped, intervals):
    from test_temporal_read_render import _pfact, _rsettings, _tags

    from nous.api.retrieval_pipeline import _resolve_recency_conflicts

    older, newer = _CASES[case](stamped)
    results = [
        _pfact(
            f["text"],
            event_date=f["day"],
            valid_from=f["day"],
            valid_to=f["day"],
            learned_at=f["learned_at"],
            mentioned_at=f["mentioned_at"],
        )
        for f in (older, newer)
    ]
    tags = _tags(_resolve_recency_conflicts(results, _rsettings(intervals)))
    assert (tags[newer["text"]][0], tags[older["text"]][0]) == ("current", "superseded")


@pytest.mark.parametrize("case", sorted(_CASES))
@pytest.mark.parametrize("stamped", [False, True], ids=["said_time_empty", "said_time_turn"])
@pytest.mark.parametrize("intervals", [False, True], ids=["intervals_off", "intervals_on"])
def test_the_preturn_resolver_keeps_the_later_correction_current(case, stamped, intervals):
    from test_temporal_read_render import _engine, _sfact

    older, newer = _CASES[case](stamped)
    facts = [
        _sfact(
            f["text"],
            event_date=f["day"],
            valid_from=f["day"],
            valid_to=f["day"],
            learned_at=f["learned_at"],
            mentioned_at=f["mentioned_at"],
        )
        for f in (older, newer)
    ]
    out_older, out_newer = _engine(temporal_resolver_intervals_enabled=intervals)._resolve_recency(facts)
    assert (out_newer.recency_status, out_older.recency_status) == ("current", "superseded")
