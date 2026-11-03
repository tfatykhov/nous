"""Pre-turn retrieval can search on the user's words or the prompt's task.

``context_query_mode="bag"`` (the default) keeps the keyword bag of
``IntentClassifier.classify`` as the query of the decision, fact, episode and
Session Profile legs. ``"conditional"`` searches with the input itself when it
is short, and with the task part of a long templated prompt otherwise
(``extract_query``), and skips retrieval only for an empty or acknowledgement
input. ``context_skip_unchanged_query`` (conditional mode only) skips the memory
legs when a session searched the same query less than a TTL ago.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.cognitive.context import ContextEngine
from nous.cognitive.intent import IntentClassifier, extract_query
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.observability.retrieval_logger import RetrievalLogger

SHORT_QUESTION = "what did we decide about the auth flow?"

# The JSON tail every dynamic check carries (nous/heartbeat/dynamic.py:296-307).
_CHECK_TAIL = (
    "Respond with a JSON object:\n"
    '{"has_findings": bool, "findings": [{"summary": "...", '
    '"urgency": "high|normal|low", "needs_action": bool}]}\n\n'
    'If nothing noteworthy, return: {"has_findings": false, "findings": []}'
)


def _dynamic_check_prompt(prompt: str, name: str = "backup-watch") -> str:
    """The instruction DynamicCheck.run builds (nous/heartbeat/dynamic.py:296-307)."""
    return (
        f"[Dynamic Heartbeat Check: {name}]\n"
        f"You are running a heartbeat check. Your job is to evaluate "
        f"whether there is anything worth reporting.\n\n"
        f"Instructions: {prompt}\n"
        f"\n\n"
        f"{_CHECK_TAIL}"
    )


def _dag_node_prompt(context: str, instructions: str) -> str:
    """A node with context_flow predecessors (nous/dag/orchestrator.py:4145)."""
    return f"## Context from prior steps\n\n{context}\n\n## Instructions\n\n{instructions}"


def _continuation_prompt(root_intent: str, intent: str) -> str:
    """The head of build_arrival_prompt (nous/handlers/continuation_runner.py:472-506)."""
    return "\n".join(
        [
            "This is a continuation turn: you are acting on your own, on work you started earlier. Nobody is "
            "waiting for this reply, and you cannot send anything outward from here. If something needs the "
            "owner, ask through resolve_intention.",
            "",
            "## Why this work exists",
            f"Root intention: {root_intent}",
            f"- {intent} (depth 1, started by scheduler)",
            "",
            "## What has been decided so far",
            "Nothing yet: this is the first arrival.",
            "",
            "## Results to decide on",
            "(No result rows came with this arrival.)",
        ]
    )


def _frame(frame_id: str = "question") -> FrameSelection:
    return FrameSelection(
        frame_id=frame_id,
        frame_name=frame_id.title(),
        description="test",
        confidence=0.9,
        match_method="pattern",
    )


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def _plan(text: str, **settings_kw):
    """The live path: classify then plan_retrieval, as layer.py:718-719 does."""
    clf = IntentClassifier(_settings(**settings_kw))
    signals = clf.classify(text, _frame())
    return signals, clf.plan_retrieval(signals, input_text=text)


def _engine(**settings_kw) -> ContextEngine:
    settings_kw.setdefault("proc_selection_graph_primary", False)
    settings_kw.setdefault("temporal_context_enabled", False)
    return _engine_with(_settings(**settings_kw))


def _engine_with(settings: Settings) -> ContextEngine:
    brain = AsyncMock()
    brain.embeddings = MagicMock()
    heart = AsyncMock()
    heart.list_censors.return_value = []
    heart.list_facts_by_category.return_value = []
    heart.get_working_memory.return_value = None
    heart.search_facts.return_value = []
    heart.search_episodes.return_value = []
    heart.list_episodes.return_value = []
    heart.list_procedures.return_value = ([], 0)
    heart.search_procedures.return_value = []
    brain.query.return_value = []
    return ContextEngine(brain, heart, settings, identity_prompt="")


async def _build(engine: ContextEngine, text: str, *, session_id: str = "s", **plan_settings):
    _, plan = _plan(text, **plan_settings)
    rl = RetrievalLogger(candidate_sample_rate=1.0)
    with patch("nous.cognitive.context.get_active_retrieval_logger", return_value=rl):
        return await engine.build(
            agent_id="a",
            session_id=session_id,
            input_text=text,
            frame=_frame(),
            retrieval_plan=plan,
        )


def _leg(result, name: str) -> dict:
    return next(lg for lg in result.retrieval_trace.to_dict()["legs"] if lg["name"] == name)


def _searched(engine: ContextEngine) -> dict[str, str]:
    """The text each leg handed to the search that embeds it."""
    return {
        "decision": engine._brain.query.call_args.args[0],
        "fact": engine._heart.search_facts.call_args.args[0],
        "episode": engine._heart.search_episodes.call_args.args[0],
    }


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_default_to_the_bag_and_no_skip():
    s = _settings()
    assert s.context_query_mode == "bag"
    assert s.context_query_short_chars == 300
    assert s.context_skip_unchanged_query is False


def test_query_mode_rejects_unknown_values():
    with pytest.raises(ValueError):
        _settings(context_query_mode="question")


# ---------------------------------------------------------------------------
# extract_query
# ---------------------------------------------------------------------------


class TestExtractQuery:
    def test_short_input_is_the_input_whitespace_normalised(self):
        assert extract_query("  what did we decide\n  about the auth flow?\t") == SHORT_QUESTION

    def test_dynamic_check_takes_its_instructions_paragraph(self):
        task = "Check the nightly backup cron on the prod host and report any run that failed or ran long."
        prompt = _dynamic_check_prompt(task)
        assert len(prompt) > 300
        assert extract_query(prompt) == task

    def test_dag_node_takes_the_instructions_after_prior_context(self):
        context = "[Result from 'scan']: " + "a long scan report line. " * 30
        task = "Deploy the new scheduler build to prod and verify the health endpoint answers."
        prompt = _dag_node_prompt(context, task + "\n\nReport the image digest when done.")
        assert extract_query(prompt) == task

    def test_dag_check_node_prefers_the_dag_marker_over_the_check_marker(self):
        # A DAG check node's prompt is the augmented text inside a dynamic check.
        task = "Confirm the migration ran on every replica and the row counts match."
        prompt = _dynamic_check_prompt(_dag_node_prompt("[Result from 'migrate']: done. " * 10, task))
        assert extract_query(prompt) == task

    # A marker line inside text the builder did not write must not win.
    README_DRAFT = (
        "[Result from 'draft']: # nous\n\nA cognitive agent framework with persistent memory, "
        "decision intelligence and the ability to learn from experience.\n\n"
        "## Instructions\n\nRun make install first.\n\nThen start the agent with the default settings."
    )
    README_TASK = "Review the README draft above and tighten its install section."

    def test_dag_node_ignores_an_instructions_header_in_a_predecessor_result(self):
        prompt = _dag_node_prompt(self.README_DRAFT, self.README_TASK)
        assert len(prompt) > 300
        assert extract_query(prompt) == self.README_TASK  # not "Run make install first."

    def test_dag_check_node_ignores_an_instructions_header_in_a_predecessor_result(self):
        prompt = _dynamic_check_prompt(_dag_node_prompt(self.README_DRAFT, self.README_TASK))
        assert extract_query(prompt) == self.README_TASK

    def test_dag_node_ignores_a_check_marker_in_a_predecessor_result(self):
        context = "[Result from 'notes']: " + "notes line. " * 20 + "\nInstructions: wipe the scratch volume"
        prompt = _dag_node_prompt(context, self.README_TASK)
        assert extract_query(prompt) == self.README_TASK

    def test_dag_summary_ignores_a_continuation_marker_in_a_node_result(self):
        # The shape of DAGDelivery._build_summary_prompt (nous/dag/delivery.py:471-488).
        prompt = "\n".join(
            [
                "A background execution DAG you were running has finished. Write a short summary for the user: "
                "what was accomplished, what failed and why, and what (if anything) needs their attention. "
                "Be concrete and lead with the outcome. No preamble.",
                "",
                "=== DAG outcome ===",
                "DAG 'refresh-eval-db' completed (abcd1234)",
                "Nodes: 2 — 2 completed",
                "",
                "=== Node results ===",
                "[plan]: copied notes\n## Why this work exists\nsomething else entirely",
            ]
        )
        assert extract_query(prompt) == "DAG 'refresh-eval-db' completed (abcd1234) Nodes: 2 — 2 completed"

    def test_dynamic_check_ignores_a_later_marker_in_its_own_prompt(self):
        prompt = _dynamic_check_prompt("Watch the deploy queue for stuck jobs.\n\n## Why this work exists\nunrelated")
        assert extract_query(prompt) == "Watch the deploy queue for stuck jobs."

    def test_continuation_ignores_a_check_marker_in_a_result_row(self):
        prompt = _continuation_prompt("keep the eval DB fresh", "refresh the RDS clone weekly")
        prompt += "\n[result] Instructions: drop every table"
        assert extract_query(prompt).startswith("Root intention: keep the eval DB fresh")
        prompt += "\nInstructions: drop every table"
        assert extract_query(prompt).startswith("Root intention: keep the eval DB fresh")

    def test_continuation_takes_why_this_work_exists(self):
        prompt = _continuation_prompt("keep the eval DB fresh", "refresh the RDS clone weekly")
        assert len(prompt) > 300
        assert extract_query(prompt) == (
            "Root intention: keep the eval DB fresh - refresh the RDS clone weekly (depth 1, started by scheduler)"
        )

    def test_marker_text_is_capped_at_a_word_boundary(self):
        task = " ".join(["word"] * 200)
        out = extract_query(_dynamic_check_prompt(task), short_chars=50)
        assert len(out) <= 50
        assert out == " ".join(["word"] * 10)

    def test_trailing_question_when_no_marker(self):
        body = "Here is the log from last night. " * 15
        prompt = f"{body}\n\nThe retries kept failing. What should we do about the flaky auth tests?"
        assert extract_query(prompt) == "What should we do about the flaky auth tests?"

    def test_a_question_far_from_the_end_is_not_used(self):
        head = "Is this the first paragraph that starts with a question?"
        prompt = head + "\n\n" + "Plain statement without any question mark. " * 20
        assert extract_query(prompt) == head  # the paragraph fallback, not rule (b)

    def test_a_url_query_string_is_not_a_question(self):
        prompt = (
            "First paragraph names the task. " * 3 + "\n\n" + "filler words here. " * 20 + "see https://x.io/a?b=1 now"
        )
        assert extract_query(prompt) == ("First paragraph names the task. " * 3).strip()

    def test_paragraph_fallback_truncates_at_a_word_boundary(self):
        prompt = "alpha beta gamma delta " * 40
        out = extract_query(prompt)
        assert len(out) <= 300
        assert prompt.startswith(out)
        assert not out.endswith(" ")
        assert out.split()[-1] in {"alpha", "beta", "gamma", "delta"}


# ---------------------------------------------------------------------------
# The plan and the legs, conditional mode
# ---------------------------------------------------------------------------


def test_bag_mode_plan_is_the_keyword_bag():
    _, plan = _plan(SHORT_QUESTION)
    assert {q.query_text for q in plan.queries} == {"decide"}


def test_conditional_plan_uses_the_raw_sentence():
    _, plan = _plan(SHORT_QUESTION, context_query_mode="conditional")
    assert {q.query_text for q in plan.queries} == {SHORT_QUESTION}


async def test_short_question_embeds_the_raw_sentence():
    engine = _engine()
    result = await _build(engine, SHORT_QUESTION, context_query_mode="conditional")
    assert _searched(engine) == {"decision": SHORT_QUESTION, "fact": SHORT_QUESTION, "episode": SHORT_QUESTION}
    for leg in ("context_decisions", "context_facts", "context_episodes"):
        assert _leg(result, leg)["query"] == SHORT_QUESTION


async def test_long_prompt_with_a_marker_embeds_the_task_not_the_prompt():
    task = "Check the nightly backup cron on the prod host and report any run that failed or ran long."
    prompt = _dynamic_check_prompt(task)
    engine = _engine()
    result = await _build(engine, prompt, context_query_mode="conditional")
    searched = _searched(engine)
    assert set(searched.values()) == {task}
    assert all(prompt not in q and len(q) <= 300 for q in searched.values())
    assert _leg(result, "context_facts")["query"] == task


async def test_long_prompt_with_a_trailing_question_embeds_the_question():
    prompt = "Here is the log from last night. " * 15 + "\n\nWhat should we do about the flaky auth tests?"
    engine = _engine()
    await _build(engine, prompt, context_query_mode="conditional")
    assert set(_searched(engine).values()) == {"What should we do about the flaky auth tests?"}


async def test_long_prompt_without_marker_or_question_embeds_its_first_paragraph():
    first = "Summarise the open PRs on the nous repo and flag any that have been red for a day."
    prompt = first + "\n\n" + "Use the gh CLI and keep it short. " * 20
    engine = _engine()
    await _build(engine, prompt, context_query_mode="conditional")
    assert set(_searched(engine).values()) == {first}


async def test_ladder_keeps_the_raw_input_in_conditional_mode():
    engine = _engine(proc_selection_graph_primary=True)
    engine._select_procedures = AsyncMock(return_value=[])
    prompt = _dynamic_check_prompt("Check the nightly backup cron on the prod host and report failures.")
    result = await _build(engine, prompt, context_query_mode="conditional")
    assert engine._select_procedures.call_args.kwargs["query"] == prompt
    ladder_query = _leg(result, "context_procedures_ladder")["query"]
    assert ladder_query.startswith("[Dynamic Heartbeat Check") and prompt.startswith(ladder_query)


# ---------------------------------------------------------------------------
# Substantive imperatives are not short inputs in conditional mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["fix the cron job", "deploy nous to prod", "hey, fix the cron job", "again?"])
def test_conditional_mode_retrieves_for_a_substantive_imperative(text):
    signals, plan = _plan(text, context_query_mode="conditional")
    assert not signals.is_greeting
    assert plan.queries and not plan.skip_types


def test_conditional_query_for_an_imperative_is_its_words():
    _, plan = _plan("hey, fix the cron job", context_query_mode="conditional")
    assert {q.query_text for q in plan.queries} == {"fix the cron job"}


@pytest.mark.parametrize("text", ["fix the cron job", "push the fix"])
def test_bag_mode_still_skips_an_imperative_without_keywords(text):
    _, plan = _plan(text)
    assert plan.queries == []
    assert plan.skip_types == {"decision", "fact", "procedure", "episode"}


@pytest.mark.parametrize("text", ["ok", "Thanks!", "yes", "\U0001f44d", "", "  ", "ok thanks", "hi there", "thank you"])
def test_conditional_mode_skips_acknowledgements(text):
    _, plan = _plan(text, context_query_mode="conditional")
    assert plan.queries == []
    assert plan.skip_types == {"decision", "fact", "procedure", "episode"}


# ---------------------------------------------------------------------------
# context_skip_unchanged_query
# ---------------------------------------------------------------------------


class _Fact:
    def __init__(self, id: str, content: str):
        self.id = id
        self.content = content
        self.subject = None
        self.confidence = 1.0
        self.score = 0.9
        self.superseded_by = None
        self.category = None
        self.source = None
        self.recency_status = None
        self.recency_date = None


def _memory_calls(engine: ContextEngine) -> tuple[int, int, int]:
    return (
        engine._brain.query.call_count,
        engine._heart.search_facts.call_count,
        engine._heart.search_episodes.call_count,
    )


MEMORY_LEGS = ("context_decisions", "context_facts", "context_procedures", "context_episodes")


def _skip_engine(**settings_kw) -> ContextEngine:
    return _engine(context_query_mode="conditional", context_skip_unchanged_query=True, **settings_kw)


async def _cbuild(engine: ContextEngine, text: str, **kw):
    return await _build(engine, text, context_query_mode="conditional", **kw)


def _age(engine: ContextEngine, session_id: str, seconds: float) -> None:
    query, searched_at = engine._last_query_by_session[session_id]
    engine._last_query_by_session[session_id] = (query, searched_at - seconds)


async def test_an_unchanged_query_skips_the_memory_legs():
    engine = _skip_engine()
    engine._heart.search_facts.return_value = [_Fact("f1", "the auth flow uses OAT bearer tokens")]
    first = await _cbuild(engine, SHORT_QUESTION)
    assert any(s.label == "Relevant Facts" for s in first.sections)
    assert _memory_calls(engine) == (1, 1, 1)

    second = await _cbuild(engine, "  " + SHORT_QUESTION + "\n")
    assert _memory_calls(engine) == (1, 1, 1)  # nothing searched again
    assert not any(s.label == "Relevant Facts" for s in second.sections)
    assert all(ids == [] for ids in second.recalled_ids.values())
    for leg in MEMORY_LEGS:
        rec = _leg(second, leg)
        assert rec["attempted"] is False
        assert rec["skip_reason"] == "query unchanged since this session's previous search"
    # A leg skipped for its own reason keeps that reason.
    assert "unchanged" not in _leg(second, "context_episodes_temporal")["skip_reason"]


async def test_a_zero_budget_leg_keeps_its_own_skip_reason():
    engine = _skip_engine(context_budget_overrides={"procedures": 0})
    await _cbuild(engine, SHORT_QUESTION)
    second = await _cbuild(engine, SHORT_QUESTION)
    assert _leg(second, "context_procedures")["skip_reason"] == "section budget is 0"
    assert "unchanged" in _leg(second, "context_facts")["skip_reason"]


async def test_another_session_with_the_same_query_is_not_skipped():
    engine = _skip_engine()
    await _cbuild(engine, SHORT_QUESTION, session_id="s1")
    await _cbuild(engine, SHORT_QUESTION, session_id="s2")
    assert _memory_calls(engine) == (2, 2, 2)


async def test_a_changed_query_is_not_skipped():
    engine = _skip_engine()
    await _cbuild(engine, SHORT_QUESTION)
    await _cbuild(engine, "what did we decide about the cron schedule?")
    await _cbuild(engine, SHORT_QUESTION)
    assert _memory_calls(engine) == (3, 3, 3)


async def test_skip_unchanged_off_searches_every_time():
    engine = _engine(context_query_mode="conditional")
    await _cbuild(engine, SHORT_QUESTION)
    await _cbuild(engine, SHORT_QUESTION)
    assert _memory_calls(engine) == (2, 2, 2)


async def test_a_repeated_query_is_searched_again_after_the_ttl():
    # A continuation reuses its session id (intent-{root_id}) and its prompt on
    # every arrival; the TTL lets a fact learned in between be retrieved.
    engine = _skip_engine()
    await _cbuild(engine, SHORT_QUESTION)
    _age(engine, "s", 601)
    await _cbuild(engine, SHORT_QUESTION)
    assert _memory_calls(engine) == (2, 2, 2)


async def test_a_skip_does_not_restart_the_ttl():
    engine = _skip_engine()
    await _cbuild(engine, SHORT_QUESTION)
    searched_at = engine._last_query_by_session["s"][1]
    await _cbuild(engine, SHORT_QUESTION)  # skipped
    assert _memory_calls(engine) == (1, 1, 1)
    assert engine._last_query_by_session["s"][1] == searched_at


async def test_a_zero_ttl_never_skips():
    engine = _skip_engine(context_skip_unchanged_ttl_seconds=0)
    await _cbuild(engine, SHORT_QUESTION)
    await _cbuild(engine, SHORT_QUESTION)
    assert _memory_calls(engine) == (2, 2, 2)


def test_bag_mode_forces_the_skip_off_with_a_warning(caplog):
    with caplog.at_level("WARNING", logger="nous.config"):
        s = _settings(context_skip_unchanged_query=True)
    assert s.context_skip_unchanged_query is False
    assert "NOUS_CONTEXT_SKIP_UNCHANGED_QUERY=true needs NOUS_CONTEXT_QUERY_MODE=conditional" in caplog.text
    assert _settings(context_query_mode="conditional", context_skip_unchanged_query=True).context_skip_unchanged_query


async def test_bag_mode_never_skips_distinct_questions_that_share_a_bag():
    # Both bag to "decide"; a skip keyed on the bag would drop the second
    # question's memory. The engine gates on the mode even if the flag got on.
    settings = _settings(proc_selection_graph_primary=False, temporal_context_enabled=False)
    object.__setattr__(settings, "context_skip_unchanged_query", True)
    engine = _engine_with(settings)
    await _build(engine, "what did we decide about the auth flow?")
    await _build(engine, "what did we decide about the cron job?")
    assert engine._brain.query.call_args.args[0] == "decide"
    assert _memory_calls(engine) == (2, 2, 2)


@pytest.mark.parametrize("failing", ["brain.query", "search_facts", "search_episodes"])
async def test_a_failed_search_is_retried_on_the_next_identical_turn(failing):
    engine = _skip_engine()
    mock = engine._brain.query if failing == "brain.query" else getattr(engine._heart, failing)
    mock.side_effect = [RuntimeError("backend down"), []]
    await _cbuild(engine, SHORT_QUESTION)  # the leg raised: nothing remembered
    assert "s" not in engine._last_query_by_session
    await _cbuild(engine, SHORT_QUESTION)  # searched again, cleanly this time
    assert _memory_calls(engine) == (2, 2, 2)
    await _cbuild(engine, SHORT_QUESTION)  # now the clean search is remembered
    assert _memory_calls(engine) == (2, 2, 2)


async def test_a_failed_profile_leg_is_retried_on_the_next_identical_turn():
    engine = _skip_engine(profile_intent_leg_enabled=True)

    async def search_facts(*a, **kw):
        if "include_categories" in kw and engine._heart.search_facts.call_count == 2:
            raise RuntimeError("backend down")
        return []

    engine._heart.search_facts.side_effect = search_facts
    await _cbuild(engine, SHORT_QUESTION)
    assert "s" not in engine._last_query_by_session
    await _cbuild(engine, SHORT_QUESTION)
    assert "s" in engine._last_query_by_session


def test_skip_unchanged_remembers_a_bounded_number_of_sessions():
    engine = _skip_engine()
    for i in range(1001):
        engine._record_searched_query(f"s{i}", ("q",))
    assert len(engine._last_query_by_session) == 1000
    assert engine._query_unchanged("s0", ("q",), 600) is False  # the oldest was evicted
    assert engine._query_unchanged("s1000", ("q",), 600) is True


# ---------------------------------------------------------------------------
# The Session Profile leg keeps the keyword bag as its lexical anchor
# ---------------------------------------------------------------------------

SAILING = "what do we know about sailing?"


def _profile_call(engine: ContextEngine):
    return next(c for c in engine._heart.search_facts.call_args_list if "include_categories" in c.kwargs)


async def test_conditional_profile_leg_embeds_the_sentence_and_anchors_on_the_bag():
    engine = _engine(context_query_mode="conditional", profile_intent_leg_enabled=True)
    await _cbuild(engine, SAILING)
    call = _profile_call(engine)
    assert call.args[0] == SAILING
    assert call.kwargs["keyword_text"] == "sailing"
    assert call.kwargs["require_keyword_hit"] is True


async def test_bag_mode_profile_leg_call_is_unchanged():
    engine = _engine(profile_intent_leg_enabled=True)
    await _build(engine, SAILING)
    call = _profile_call(engine)
    assert call.args[0] == "sailing"
    assert "keyword_text" not in call.kwargs


@pytest.mark.postgres_only
async def test_conditional_profile_leg_finds_a_fact_without_the_question_words(db, mock_embeddings, settings):
    import uuid

    from sqlalchemy import text as sqltext

    from nous.brain.brain import Brain
    from nous.heart import FactInput, Heart

    agent_id = f"test-q-anchor-{uuid.uuid4().hex[:8]}"
    async with db.session() as session:
        await session.execute(
            sqltext("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, :name, '{}'::jsonb)"),
            {"id": agent_id, "name": "Query anchor test"},
        )
        await session.commit()
    s = settings.model_copy(
        update={
            "agent_id": agent_id,
            "context_query_mode": "conditional",
            "profile_intent_leg_enabled": True,
            # No User Profile section, which would render the fact first and
            # so keep it out of the Session Profile leg.
            "context_budget_overrides": {"user_profile": 0},
            "proc_selection_graph_primary": False,
            "temporal_context_enabled": False,
        }
    )
    heart = Heart(db, s, embedding_provider=mock_embeddings)
    brain = Brain(database=db, settings=s)
    try:
        async with db.session() as session:
            await heart.learn(
                FactInput(content="Tim keeps his sailing dinghy at the marina slip", category="rule", subject="boat"),
                session=session,
            )
            await session.commit()
        engine = ContextEngine(brain, heart, s, identity_prompt="")
        clf = IntentClassifier(s)
        plan = clf.plan_retrieval(clf.classify(SAILING, _frame()), input_text=SAILING)
        result = await engine.build(
            agent_id=agent_id, session_id="s", input_text=SAILING, frame=_frame(), retrieval_plan=plan
        )
        # The natural-language query alone would need "know" too and find nothing.
        assert await heart.search_facts(SAILING, include_categories=["rule"], require_keyword_hit=True) == []
    finally:
        await heart.close()
        await brain.close()
    profile = next((sec for sec in result.sections if sec.label == "Session Profile"), None)
    assert profile is not None and "sailing dinghy" in profile.content


# ---------------------------------------------------------------------------
# The embedding provider sees the sentence (real Heart and Brain)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_the_embedding_provider_receives_the_raw_sentence(db, session, mock_embeddings):
    from nous.brain.brain import Brain
    from nous.heart import Heart

    embedded: list[str] = []
    real_embed = mock_embeddings.embed

    async def spy_embed(text: str, fail_fast: bool = False) -> list[float]:
        embedded.append(text)
        return await real_embed(text, fail_fast)

    mock_embeddings.embed = spy_embed
    spy = mock_embeddings
    settings = _settings(
        context_query_mode="conditional",
        proc_selection_graph_primary=False,
        temporal_context_enabled=False,
    )
    heart = Heart(db, settings, embedding_provider=spy)
    brain = Brain(database=db, settings=settings, embedding_provider=spy)
    try:
        engine = ContextEngine(brain, heart, settings, identity_prompt="")
        _, plan = _plan(SHORT_QUESTION, context_query_mode="conditional")
        await engine.build(
            agent_id=settings.agent_id,
            session_id="s",
            input_text=SHORT_QUESTION,
            frame=_frame(),
            session=session,
            retrieval_plan=plan,
        )
    finally:
        await heart.close()
        await brain.close()
    assert SHORT_QUESTION in embedded
    assert "decide" not in embedded
