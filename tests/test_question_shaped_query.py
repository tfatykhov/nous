"""Pre-turn retrieval can search on the user's words or the prompt's task (E-2/E-3/E-4).

``context_query_mode="bag"`` (the default) keeps the keyword bag of
``IntentClassifier.classify`` as the query of the decision, fact, episode and
Session Profile legs. ``"conditional"`` searches with the input itself when it
is short, and with the task part of a long templated prompt otherwise
(``extract_query``). ``context_skip_unchanged_query`` skips the memory legs when
a session's query has not changed since its previous build.
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
    settings_kw.setdefault("proc_selection_graph_primary", False)
    settings_kw.setdefault("temporal_context_enabled", False)
    return ContextEngine(brain, heart, _settings(**settings_kw), identity_prompt="")


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
# E-4: substantive imperatives are not short inputs in conditional mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["fix the cron job", "deploy nous to prod", "hey, fix the cron job"])
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


async def test_an_unchanged_query_skips_the_memory_legs():
    engine = _engine(context_skip_unchanged_query=True)
    engine._heart.search_facts.return_value = [_Fact("f1", "the auth flow uses OAT bearer tokens")]
    first = await _build(engine, SHORT_QUESTION, context_query_mode="conditional")
    assert any(s.label == "Relevant Facts" for s in first.sections)
    assert _memory_calls(engine) == (1, 1, 1)

    second = await _build(engine, "  " + SHORT_QUESTION + "\n", context_query_mode="conditional")
    assert _memory_calls(engine) == (1, 1, 1)  # nothing searched again
    assert not any(s.label == "Relevant Facts" for s in second.sections)
    for leg in ("context_decisions", "context_facts", "context_procedures", "context_episodes"):
        rec = _leg(second, leg)
        assert rec["attempted"] is False
        assert "unchanged" in rec["skip_reason"]


async def test_another_session_with_the_same_query_is_not_skipped():
    engine = _engine(context_skip_unchanged_query=True)
    await _build(engine, SHORT_QUESTION, session_id="s1", context_query_mode="conditional")
    await _build(engine, SHORT_QUESTION, session_id="s2", context_query_mode="conditional")
    assert _memory_calls(engine) == (2, 2, 2)


async def test_a_changed_query_is_not_skipped():
    engine = _engine(context_skip_unchanged_query=True)
    await _build(engine, SHORT_QUESTION, context_query_mode="conditional")
    await _build(engine, "what did we decide about the cron schedule?", context_query_mode="conditional")
    await _build(engine, SHORT_QUESTION, context_query_mode="conditional")
    assert _memory_calls(engine) == (3, 3, 3)


async def test_skip_unchanged_off_searches_every_time():
    engine = _engine()
    await _build(engine, SHORT_QUESTION, context_query_mode="conditional")
    await _build(engine, SHORT_QUESTION, context_query_mode="conditional")
    assert _memory_calls(engine) == (2, 2, 2)


def test_skip_unchanged_remembers_a_bounded_number_of_sessions():
    engine = _engine(context_skip_unchanged_query=True)
    for i in range(1001):
        assert engine._query_unchanged(f"s{i}", ("q",)) is False
    assert len(engine._last_query_by_session) == 1000
    assert engine._query_unchanged("s0", ("q",)) is False  # the oldest was evicted
    assert engine._query_unchanged("s1000", ("q",)) is True


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
