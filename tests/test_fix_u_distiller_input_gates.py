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
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from test_fix_e_strategy_card_distiller import LLM, _card, _cards, _Rig, _state

from nous.brain.brain import Brain
from nous.cognitive.deliberation import DeliberationEngine
from nous.cognitive.layer import CognitiveLayer
from nous.cognitive.schemas import FrameSelection, TurnResult
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


async def _captured(rig, cap: int, *, finalized: bool, reply: str = _REPLY) -> uuid.UUID:
    """A decision as the deliberation capture writes it during a turn: start() with
    the user's request, then finalize() with the reply, each cut at ``cap``
    characters as the cognitive layer cuts them (500 since 2026-03-29, 200 before)."""
    engine = DeliberationEngine(rig.brain, rig.settings)
    decision_id = await engine.start(rig.agent_id, _REQUEST[:cap], _FRAME)
    if finalized:
        await engine.finalize(decision_id, description=reply[:cap], confidence=0.8)
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
    the model fills in the rest: no model call, no card."""
    decision_id = await _captured(rig, cap, finalized=finalized)
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
    "case", ["reply-shorter-than-the-cap", "longer-than-any-cap", "the-agent-wrote-the-cap-length"]
)
async def test_a_description_the_capture_did_not_cut_still_gets_a_card(rig, case):
    """The control: a reply one character shorter than the cap is stored whole by
    the capture; a description one character longer than the cap is not one the
    capture wrote; and a description exactly as long as the cap that carries no
    capture reason is the agent's own text, not a cut reply."""
    if case == "reply-shorter-than-the-cap":
        decision_id = await _captured(rig, 500, finalized=True, reply=_REPLY[:499])
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
    assert description_was_cut_by_capture(planned.description, planned.reasons)
    assert description_was_cut_by_capture(finalized.description, finalized.reasons)
