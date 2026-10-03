"""A strategy card is distilled only from a decision whose stored text can carry a
lesson, and a card stores nothing that is not its own text.

A decision row that cannot carry one: a failure or a partial grade without result
notes (the lesson's "because" would be the model's own), an empty description,
and a description the deliberation capture cut at its length cap (a fragment of
a request or a reply, not a decision). Such a row gets no model call, and the card
it has is retired at its next review.

Tool-call markup the model leaks into one field of the card (the lesson ending
in the tag of another argument, one the call does not have) is not stored; a
lesson that only mentions such a tag keeps it.

The card tests run the production chain of the earlier card tests: Brain.review ->
EventBus -> StrategyCardDistiller on real rows under a fresh agent_id. The one
double is the model.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import update
from test_fix_e_strategy_card_distiller import LLM, _card, _cards, _Rig, _state

from nous.brain.brain import Brain
from nous.cognitive.context import ContextEngine
from nous.cognitive.deliberation import DeliberationEngine, description_was_cut_by_capture
from nous.cognitive.layer import CognitiveLayer
from nous.cognitive.schemas import FrameSelection, TurnResult
from nous.config import Settings
from nous.storage.models import Decision

_LOG = "nous.handlers.strategy_card_distiller"


@pytest_asyncio.fixture
async def rig(db, mock_embeddings):
    r = _Rig(db, mock_embeddings)
    yield r
    await r.close()


# ---------------------------------------------------------------------------
# A failure or a partial grade needs result notes; a decision needs a description
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failure", "partial"])
@pytest.mark.parametrize("result", [None, "", " \n "], ids=["none", "empty", "blank"])
async def test_a_failure_or_partial_grade_without_result_notes_gets_no_card(rig, caplog, outcome, result):
    """The grade says that the decision went wrong, or half wrong, but not why: a
    lesson of the form "avoid Y because Z" would get its Z from the model. Without
    result notes on the row there is no model call and no card, and the log says
    why."""
    decision = await rig.record()
    with patch(LLM, new_callable=AsyncMock, return_value=_card()) as llm, caplog.at_level("DEBUG", logger=_LOG):
        await rig.brain.review(decision.id, outcome=outcome, result=result, reviewer="agent")
        await rig.settle()

    llm.assert_not_awaited()
    assert await _cards(rig) == []
    assert f"no card for decision {decision.id} (ungraded, auto-reviewed, gone, or its text" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "result"),
    [("failure", "rolled back twice"), ("partial", "half of the hosts switched"), ("success", None)],
    ids=["failure-with-notes", "partial-with-notes", "success-without-notes"],
)
async def test_a_grade_whose_text_carries_its_reason_still_gets_a_card(rig, outcome, result):
    """The control: a failure or a partial grade with result notes, and a success
    without them (the decision says what was done), are distilled as before."""
    decision = await rig.record()
    with patch(LLM, new_callable=AsyncMock, return_value=_card()) as llm:
        await rig.brain.review(decision.id, outcome=outcome, result=result, reviewer="agent")
        await rig.settle()

    llm.assert_awaited_once()
    assert _state(await _cards(rig)) == [(True, outcome)]


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_card_is_retired_when_its_failure_is_regraded_without_result_notes(rig, caplog):
    """A review replaces the result notes. A failure graded again without notes no
    longer holds the text its card was distilled from: the card is retired, no new
    one is distilled, and the log says why (the card lookups are JSONB queries:
    Postgres lane)."""
    decision = await rig.record()
    with patch(LLM, new_callable=AsyncMock, return_value=_card()) as llm, caplog.at_level("INFO", logger=_LOG):
        await rig.brain.review(decision.id, outcome="failure", result="rolled back twice", reviewer="agent")
        await rig.settle()
        await rig.brain.review(decision.id, outcome="failure", result=None, reviewer="agent")
        await rig.settle()

    assert _state(await _cards(rig)) == [(False, "failure")]
    assert llm.await_count == 1
    assert f"retired 1 card(s) of decision {decision.id} (its text cannot carry a lesson)" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["", " \n "], ids=["empty", "blank"])
async def test_a_decision_with_an_empty_description_gets_no_card(rig, description):
    """Nothing in the row says what was decided: the model would write the whole
    lesson from the outcome and the result notes."""
    async with rig.db.session() as s:
        s.add(
            Decision(
                id=(decision_id := uuid.uuid4()),
                agent_id=rig.agent_id,
                description=description,
                confidence=0.9,
                category="process",
                stakes="medium",
            )
        )
        await s.commit()

    with patch(LLM, new_callable=AsyncMock, return_value=_card()) as llm:
        await rig.brain.review(decision_id, outcome="success", result="worked", reviewer="agent")
        await rig.settle()

    llm.assert_not_awaited()
    assert await _cards(rig) == []


# ---------------------------------------------------------------------------
# A description the deliberation capture cut is a fragment, not a decision
# ---------------------------------------------------------------------------


_FRAME = FrameSelection(frame_id="decision", frame_name="Decision", confidence=0.9, match_method="test")
_REQUEST = "Should the nightly import move to a queue so that one failed batch can be retried alone? " * 12
_REPLY = "The import moves to a queue: the worker pool drains it and a failed batch is retried alone. " * 12
# A row the capture wrote while its cap was still 200 characters.
_BEFORE_THE_CAP_ROSE = datetime(2026, 3, 1, tzinfo=UTC)


async def _captured(
    rig,
    cap: int,
    *,
    finalized: bool,
    reply: str = _REPLY,
    request: str = _REQUEST,
    captured_at: datetime | None = None,
) -> uuid.UUID:
    """A decision as the deliberation capture writes it during a turn: start() with
    the user's request, then finalize() with the reply, each cut at ``cap``
    characters as the cognitive layer cuts them (500 since 2026-03-29, 200 before).
    ``captured_at`` dates the row back to when it would have been captured."""
    engine = DeliberationEngine(rig.brain, rig.settings)
    decision_id = await engine.start(rig.agent_id, request[:cap], _FRAME)
    if finalized:
        await engine.finalize(decision_id, description=reply[:cap], confidence=0.8)
    if captured_at is not None:
        async with rig.db.session() as s:
            row = update(Decision).where(Decision.id == uuid.UUID(decision_id)).values(created_at=captured_at)
            await s.execute(row)
            await s.commit()
    return uuid.UUID(decision_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cap", "finalized"),
    [(500, True), (200, True), (500, False), (200, False)],
    ids=["reply-cut-at-500", "reply-cut-at-200", "request-cut-at-500", "request-cut-at-200"],
)
async def test_a_description_cut_by_the_deliberation_capture_gets_no_card(rig, cap, finalized):
    """The capture keeps the reply (or, until the turn ends, "Plan: " and the
    request) cut at its cap. A row cut there holds a fragment, not a decision, and
    the model fills in the rest: no model call, no card. (A row cut at 200 was
    captured before the cap rose to 500.)"""
    decision_id = await _captured(
        rig, cap, finalized=finalized, captured_at=_BEFORE_THE_CAP_ROSE if cap == 200 else None
    )
    stored = await rig.brain.get(decision_id)
    if finalized:
        assert stored.description == _REPLY[:cap]
    else:
        assert stored.description == "Plan: " + _REQUEST[:cap]

    with patch(LLM, new_callable=AsyncMock, return_value=_card()) as llm:
        await rig.brain.review(decision_id, outcome="success", result="worked", reviewer="agent")
        await rig.settle()

    llm.assert_not_awaited()
    assert await _cards(rig) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "reply-shorter-than-the-cap",
        "longer-than-any-cap",
        "the-agent-wrote-the-cap-length",
        "a-whole-reply-of-200-since-the-cap-rose",
        "a-whole-request-of-200-since-the-cap-rose",
    ],
)
async def test_a_description_the_capture_did_not_cut_still_gets_a_card(rig, case):
    """The control: a reply one character shorter than the cap is stored whole by
    the capture; a description one character longer than the cap is not one the
    capture wrote; and a description exactly as long as the cap that carries no
    capture reason is the agent's own text, not a cut reply. A reply or a request
    of exactly 200 characters captured since the cap rose to 500 is whole: the
    earlier cap counts only for a row captured before."""
    if case == "reply-shorter-than-the-cap":
        decision_id = await _captured(rig, 500, finalized=True, reply=_REPLY[:499])
    elif case == "a-whole-reply-of-200-since-the-cap-rose":
        decision_id = await _captured(rig, 500, finalized=True, reply=_REPLY[:200])
    elif case == "a-whole-request-of-200-since-the-cap-rose":
        decision_id = await _captured(rig, 500, finalized=False, request=_REQUEST[:200])
    else:
        async with rig.db.session() as s:
            s.add(
                Decision(
                    id=(decision_id := uuid.uuid4()),
                    agent_id=rig.agent_id,
                    description=_REPLY[:501] if case == "longer-than-any-cap" else _REPLY[:500],
                    confidence=0.9,
                    category="process",
                    stakes="medium",
                )
            )
            await s.commit()

    with patch(LLM, new_callable=AsyncMock, return_value=_card()) as llm:
        await rig.brain.review(decision_id, outcome="success", result="worked", reviewer="agent")
        await rig.settle()

    llm.assert_awaited_once()
    assert _state(await _cards(rig)) == [(True, "success")]


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_the_cognitive_layer_cuts_a_decision_where_the_gate_looks_for_the_cut(db, heart, settings, session):
    """The turn's own capture, not a copy of it: pre_turn starts the decision with
    the request and post_turn finalizes it with the reply, both cut at the one cap
    the gate also reads (finalizing writes through Brain.update: Postgres lane)."""
    from nous.cognitive.deliberation import DESCRIPTION_CAPTURE_CHARS, description_was_cut_by_capture

    brain = Brain(database=db, settings=settings)
    layer = CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")
    sid = f"fix-u-capture-{uuid.uuid4().hex[:8]}"
    try:
        ctx = await layer.pre_turn("nous-default", sid, _REQUEST, session=session)
        planned = await brain.get(uuid.UUID(ctx.decision_id), session=session)
        await layer.post_turn("nous-default", sid, TurnResult(response_text=_REPLY), ctx, session=session)
        finalized = await brain.get(uuid.UUID(ctx.decision_id), session=session)
    finally:
        await brain.close()

    assert planned.description == "Plan: " + _REQUEST[:DESCRIPTION_CAPTURE_CHARS]
    assert finalized.description == _REPLY[:DESCRIPTION_CAPTURE_CHARS]
    assert description_was_cut_by_capture(planned.description, planned.reasons, planned.created_at)
    assert description_was_cut_by_capture(finalized.description, finalized.reasons, finalized.created_at)


# Written by DeliberationEngine.start() since 2026-02-22, so every captured row carries it.
_STORED = "Frame 'Decision' triggered deliberation for: Should the nightly import move"
# The change that raised the capture's cap from 200 to 500 characters was merged then.
_CAP_ROSE_AT = datetime(2026, 3, 29, 17, 47, 39, tzinfo=UTC)


@pytest.mark.parametrize(
    ("description", "reason", "cut"),
    [
        ("x" * 500, _STORED, True),
        ("Plan: " + "x" * 500, _STORED, True),
        ("x" * 500, "The log read: " + _STORED, False),
        ("x" * 500, "Frame 'Decision' was picked by the router", False),
        ("Plan: " + "x" * 300, _STORED, False),
        ("x" * 506, _STORED, False),
    ],
    ids=[
        "a-reply-in-the-words-rows-carry",
        "a-request-in-the-words-rows-carry",
        "an-agent-reason-quoting-the-capture",
        "a-reason-that-only-starts-like-it",
        "a-request-the-capture-did-not-cut",
        "a-request-length-without-the-prefix",
    ],
)
def test_a_captured_row_is_recognised_by_its_stored_reason_and_length(description, reason, cut):
    """The reason is matched in the words the capture has always written (rewording
    it would leave every row already stored unrecognised), from its start; and only a
    "Plan: " request cut at a cap counts, not every request."""
    assert description_was_cut_by_capture(description, [SimpleNamespace(text=reason)], _CAP_ROSE_AT) is cut


@pytest.mark.parametrize(
    ("description", "created_at", "cut"),
    [
        ("x" * 200, _CAP_ROSE_AT - timedelta(seconds=1), True),
        ("Plan: " + "x" * 200, _CAP_ROSE_AT - timedelta(seconds=1), True),
        ("x" * 200, _CAP_ROSE_AT.replace(tzinfo=None) - timedelta(seconds=1), True),
        ("x" * 200, _CAP_ROSE_AT, False),
        ("Plan: " + "x" * 200, _CAP_ROSE_AT, False),
        ("x" * 200, None, False),
        ("x" * 500, _CAP_ROSE_AT, True),
    ],
    ids=[
        "a-reply-captured-before-the-cap-rose",
        "a-request-captured-before-the-cap-rose",
        "a-time-without-a-zone-is-utc",
        "a-reply-captured-since",
        "a-request-captured-since",
        "no-time-counts-as-since",
        "the-current-cap-since",
    ],
)
def test_the_earlier_cap_counts_only_for_a_row_captured_before_the_cap_rose(description, created_at, cut):
    """The capture cut at 200 characters until the cap rose to 500 on 2026-03-29.
    A whole reply or request of 200 characters captured since then is not a cut
    one. A time without a zone is read as UTC (the SQLite lane hands one back),
    and a row without a time counts as captured since."""
    assert description_was_cut_by_capture(description, [SimpleNamespace(text=_STORED)], created_at) is cut


# ---------------------------------------------------------------------------
# Tool-call markup the model leaks into a field of the card is not stored
# ---------------------------------------------------------------------------


class _Model:
    """A client that answers every call with one tool call carrying ``tool_input``."""

    def __init__(self, tool_input: dict) -> None:
        self.tool_input = tool_input

    async def call(self, payload: dict) -> SimpleNamespace:
        name = payload["tool_choice"]["name"]
        return SimpleNamespace(content=[{"type": "tool_use", "name": name, "input": self.tool_input}])


_LESSON = "When a release can be switched back in one step, ship it that way: recovery is fast."


async def _distilled(rig, tool_input: dict) -> list:
    """The cards the distiller stores for one reviewed decision when the model answers with ``tool_input``."""
    # The rig wires a placeholder client; these tests need the model's own tool call.
    rig.distiller._llm = _Model(tool_input)
    decision = await rig.record()
    await rig.brain.review(decision.id, outcome="success", result="zero downtime", reviewer="agent")
    await rig.settle()
    return await _cards(rig)


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_markup_leaked_into_the_lesson_is_neither_stored_nor_rendered(rig, caplog):
    """The model ended the lesson with its own closing tag and then wrote the tags
    argument as tool-call markup inside the lesson string; the call has no tags.
    The card keeps the lesson's own text, its rendered block carries none of that
    markup, and the log says that markup was cut (the lesson is stored in a
    Postgres array, which the SQLite lane hands back as text: Postgres lane)."""
    with caplog.at_level("WARNING", logger=_LOG):
        (card,) = await _distilled(
            rig,
            {
                "name": "Prefer reversible rollouts",
                "description": "Reversible rollouts keep an outage short",
                "lesson": _LESSON + '</lesson> <parameter name="tags">["deploy", "rollback"]',
            },
        )

    assert card.implementation_notes == [_LESSON]
    assert "cut leaked tool-call markup" in caplog.text
    detail = await rig.heart.get_procedure(card.id)
    engine = ContextEngine(MagicMock(), MagicMock(), Settings(_env_file=None), identity_prompt="Test")
    (block,) = engine._format_procedure_bodies([detail], 8000)
    assert _LESSON in block
    assert "<parameter" not in block and "</lesson>" not in block


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lesson", "tags"),
    [
        (
            'When a tool takes a row limit, write it as <parameter name="limit"> with a number,'
            " never a word: the call then fails before it runs.",
            None,
        ),
        (
            'When a card needs labels, the model writes them as <parameter name="tags"> and a JSON'
            " list, never inside the lesson.",
            ["labels"],
        ),
        ('A leaked argument looks like <parameter name="tags">["a"]</parameter>', None),
        (
            'Write the labels as <parameter name="tags">["a"]</parameter>, and a row limit as'
            ' <parameter name="limit"> with a number.',
            None,
        ),
    ],
    ids=[
        "a-tag-the-card-does-not-have",
        "a-card-argument-the-call-has",
        "a-complete-tag-at-the-end",
        "a-card-tag-before-the-end",
    ],
)
async def test_a_lesson_that_mentions_the_markup_keeps_it(rig, caplog, lesson, tags):
    """Only a tag that names an argument of the card the call left out is a slip
    into the tool-call syntax, and only a tag in the run that ends the string
    counts. A lesson that mentions an unclosed tag in its last sentence, one that
    names something else or an argument the call has, or that quotes a complete
    tag, is stored as the model wrote it."""
    tool_input = {"name": "Write tool arguments as values", "description": "Arguments are values", "lesson": lesson}
    if tags is not None:
        tool_input["tags"] = tags

    with caplog.at_level("WARNING", logger=_LOG):
        (card,) = await _distilled(rig, tool_input)

    assert card.implementation_notes == [lesson]
    assert "leaked tool-call markup" not in caplog.text
