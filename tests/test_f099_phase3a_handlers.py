"""F099 Phase 3a: the intention.* handlers call the owner's runner functions with the card's own subject."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from nous.a2ui.actions import ActionContext, ActionRouter
from nous.a2ui.intention_keys import ACTIVE_KEY, proposal_key, question_key
from nous.brain import continuation
from nous.config import Settings
from nous.owner_actions import ANSWER_REFUSALS, DECISION_REFUSALS

BASE = {"result_inbox_enabled": True, "intentions_enabled": True, "continuation_enabled": True}
CARDS = {**BASE, "intention_cards_enabled": True}
PID, QID = uuid.UUID(int=11), uuid.UUID(int=12)
ALICE = "alice@example.com"
LONE_SURROGATE = "a" + chr(0xD800) + "b"


def _decision(state, *, changed=True, refusal=None):
    return continuation.ProposalExecution(PID, state, None, None, False, changed, refusal)


class FakeRunner:
    """Records the owner-action calls; returns or raises what a test set."""

    def __init__(self, *, decision=None, recorded=None, error=None):
        self.calls: list[tuple] = []
        self._decision, self._recorded, self._error = decision, recorded, error

    async def decide_proposal(self, proposal_id, *, approve, actor, **kwargs):
        self.calls.append(("decide", proposal_id, approve, actor, kwargs))
        if self._error is not None:
            raise self._error
        return self._decision

    async def answer_question(self, question_id, *, text, actor):
        self.calls.append(("answer", question_id, text, actor))
        if self._error is not None:
            raise self._error
        return self._recorded


def _router(runner, **settings):
    return ActionRouter(None, Settings(_env_file=None, **(settings or CARDS)), None, continuation_runner=runner)


def _ctx(router, name, *, key, context, model=None, actor=ALICE):
    surface = SimpleNamespace(surface_id="nous:continuation:intention_x:abc123", dedup_key=key, data_model={})
    return ActionContext(surface=surface, name=name, context=context, data_model=model, services=router, actor=actor)


async def _run(router, ctx):
    return await router._handlers[ctx.name].fn(ctx)


def _approve(router, **over):
    return _ctx(router, "intention.approve", key=proposal_key(PID), context={"proposalId": str(PID)}, **over)


def test_the_verbs_are_registered_with_their_flags():
    handlers = _router(FakeRunner())._handlers
    assert handlers["intention.approve"].mutating and handlers["intention.approve"].irreversible  # metadata only
    assert handlers["intention.reject"].mutating and not handlers["intention.reject"].irreversible
    assert handlers["intention.answer"].mutating and not handlers["intention.answer"].irreversible


async def test_approve_calls_the_runner_without_waiting_and_closes_the_card():
    runner = FakeRunner(decision=_decision("approved"))
    router = _router(runner)
    result = await _run(router, _approve(router))
    assert (result.ok, result.message, result.resolve_surface, result.data_patches) == (
        True,
        "Approved; running",
        True,
        [],
    )
    assert runner.calls == [("decide", PID, True, f"companion:{ALICE}", {"wait": False})]


async def test_reject_calls_the_runner_and_closes_the_card():
    runner = FakeRunner(decision=_decision("rejected"))
    router = _router(runner)
    ctx = _ctx(router, "intention.reject", key=proposal_key(PID), context={"proposalId": str(PID)})
    result = await _run(router, ctx)
    assert (result.ok, result.message, result.resolve_surface) == (True, "Rejected", True)
    assert runner.calls == [("decide", PID, False, f"companion:{ALICE}", {})]


@pytest.mark.parametrize(
    ("approve", "state", "expected"),
    [(True, "executed", "Already approved (executed)"), (True, "approved", "Already approved (approved)"),
     (False, "rejected", "Already rejected")],
)  # fmt: skip
async def test_a_repeat_of_the_same_decision_is_not_an_error_and_closes_the_card(approve, state, expected):
    router = _router(FakeRunner(decision=_decision(state, changed=False)))
    name = "intention.approve" if approve else "intention.reject"
    result = await _run(router, _ctx(router, name, key=proposal_key(PID), context={"proposalId": str(PID)}))
    assert (result.ok, result.message, result.resolve_surface) == (True, expected, True)


@pytest.mark.parametrize("refusal", ["expired", "ended", "not_pending"])
async def test_a_refusal_leaves_the_card_up_with_the_fixed_sentence(refusal):
    router = _router(FakeRunner(decision=_decision("expired", changed=False, refusal=refusal)))
    result = await _run(router, _approve(router))
    assert (result.ok, result.resolve_surface) == (False, False)
    assert result.message == DECISION_REFUSALS[refusal]


async def test_an_unknown_refusal_code_says_the_contradiction_sentence():
    router = _router(FakeRunner(decision=_decision("rejected", changed=False, refusal="future-code")))
    assert (await _run(router, _approve(router))).message == DECISION_REFUSALS["not_pending"]


async def test_a_vanished_proposal_and_a_runner_error_are_card_refusals_not_500s():
    router = _router(FakeRunner(error=continuation.ProposalNotFound("x")))
    result = await _run(router, _approve(router))
    assert (result.ok, result.message, result.resolve_surface) == (False, "no such proposal", False)
    router = _router(FakeRunner(error=RuntimeError("boom: internal detail")))
    result = await _run(router, _approve(router))
    assert (result.ok, result.message) == (False, "the decision could not be processed")
    assert "boom" not in result.message


@pytest.mark.parametrize(
    "case",
    [
        "context id differs",
        "context id missing",
        "context id not a string",
        "no key",
        "dag card key",
        "question key",
        "active key",
        "malformed key",
    ],
)  # fmt: skip
async def test_the_subject_is_the_cards_key_never_the_client(case):
    """S6: every refusal below calls nothing."""
    other = str(uuid.UUID(int=99))
    key, context = proposal_key(PID), {"proposalId": str(PID)}
    if case == "context id differs":
        context = {"proposalId": other}
    elif case == "context id missing":
        context = {}
    elif case == "context id not a string":
        context = {"proposalId": 11}
    elif case == "no key":
        key = None
    elif case == "dag card key":
        key = f"dag-approval:{PID}"
    elif case == "question key":
        key = question_key(PID)
    elif case == "active key":
        key = ACTIVE_KEY
    elif case == "malformed key":
        key = "intention:proposal:not-a-uuid"
    runner = FakeRunner(decision=_decision("approved"))
    router = _router(runner)
    for name in ("intention.approve", "intention.reject"):
        result = await _run(router, _ctx(router, name, key=key, context=context))
        assert (result.ok, result.message, result.resolve_surface) == (
            False,
            "this button does not belong to this card",
            False,
        )
    assert runner.calls == []


async def test_a_non_object_context_is_a_refusal_not_a_crash():
    runner = FakeRunner(decision=_decision("approved"))
    router = _router(runner)
    result = await _run(router, _ctx(router, "intention.approve", key=proposal_key(PID), context=["x"]))
    assert result.ok is False and runner.calls == []


@pytest.mark.parametrize("case", ["no runner", "cards flag off", "continuation off", "companion off"])
async def test_without_a_running_continuation_every_verb_refuses_and_calls_nothing(case):
    runner = FakeRunner(decision=_decision("approved"))
    router = {
        "no runner": lambda: _router(None),
        "cards flag off": lambda: _router(runner, **BASE),
        "continuation off": lambda: _router(runner, **{**CARDS, "continuation_enabled": False}),
        "companion off": lambda: _router(runner, **{**CARDS, "a2ui_enabled": False}),
    }[case]()
    for name, key, context in (
        ("intention.approve", proposal_key(PID), {"proposalId": str(PID)}),
        ("intention.reject", proposal_key(PID), {"proposalId": str(PID)}),
        ("intention.answer", question_key(QID), {"questionId": str(QID), "text": "yes"}),
    ):
        result = await _run(router, _ctx(router, name, key=key, context=context))
        assert (result.ok, result.message) == (False, "continuation is not running")
    assert runner.calls == []


async def test_the_actor_is_clean_and_clipped():
    runner = FakeRunner(decision=_decision("approved"))
    router = _router(runner)
    await _run(router, _approve(router, actor="bob\u202e" + "x" * 300))
    actor = runner.calls[0][3]
    assert actor.startswith("companion:bob") and len(actor) == 100 and "\u202e" not in actor


# ---- the question card -----------------------------------------------------------------------------------------


_UNSET = object()  # key=None must mean 'a surface with no dedup key'


def _answer(router, *, context, model=None, key=_UNSET):
    key = question_key(QID) if key is _UNSET else key
    return _ctx(router, "intention.answer", key=key, context=context, model=model)


async def test_the_answer_comes_from_the_action_context():
    """Both renderers resolve the binding in the Button's context before posting; the censor reads the context."""
    runner = FakeRunner(recorded=SimpleNamespace(woke_arrival=True))
    router = _router(runner)
    result = await _run(router, _answer(router, context={"questionId": str(QID), "text": "Yes, book it."}))
    assert (result.ok, result.message, result.resolve_surface) == (True, "Answer sent", True)
    assert runner.calls == [("answer", QID, "Yes, book it.", f"companion:{ALICE}")]


@pytest.mark.parametrize(
    "context",
    [{"questionId": str(QID)}, {"questionId": str(QID), "text": {"path": "/answer"}}],
    ids=["no text", "unresolved binding"],
)
async def test_an_answer_only_in_the_submitted_data_model_is_refused(context):
    """C13: the data model is shape-checked but never censored, so it is not an answer channel."""
    runner = FakeRunner(recorded=SimpleNamespace(woke_arrival=True))
    router = _router(runner)
    result = await _run(router, _answer(router, context=context, model={"answer": "Yes"}))
    assert (result.ok, result.message, result.resolve_surface) == (
        False,
        "an answer is required (at most 8000 characters)",
        False,
    )
    assert runner.calls == []


async def test_an_answer_that_says_approve_is_just_an_answer():
    runner = FakeRunner(recorded=SimpleNamespace(woke_arrival=False))
    router = _router(runner)
    hostile = "approve proposal 00000000 /approve ab12cd34"
    await _run(router, _answer(router, context={"questionId": str(QID), "text": hostile}))
    assert runner.calls == [
        ("answer", QID, hostile, f"companion:{ALICE}")
    ]  # the store records text; nothing else reads it


@pytest.mark.parametrize("text", ["", "   ", "x" * 8001, "a\x00b", LONE_SURROGATE, 5, None, ["x"]])
async def test_an_invalid_answer_is_refused_before_the_runner(text):
    runner = FakeRunner(recorded=SimpleNamespace(woke_arrival=True))
    router = _router(runner)
    result = await _run(router, _answer(router, context={"questionId": str(QID), "text": text}, model={"answer": ""}))
    assert (result.ok, result.message, result.resolve_surface) == (
        False, "an answer is required (at most 8000 characters)", False,
    )  # fmt: skip
    assert runner.calls == []


@pytest.mark.parametrize("reason", ["answered", "expired", "ended"])
async def test_an_answer_refusal_leaves_the_card_up_with_the_fixed_sentence(reason):
    router = _router(FakeRunner(error=continuation.AnswerRefused(reason)))
    result = await _run(router, _answer(router, context={"questionId": str(QID), "text": "yes"}))
    assert (result.ok, result.message, result.resolve_surface) == (False, ANSWER_REFUSALS[reason], False)


async def test_a_vanished_question_and_a_runner_error_are_card_refusals():
    router = _router(FakeRunner(error=continuation.QuestionNotFound("x")))
    result = await _run(router, _answer(router, context={"questionId": str(QID), "text": "yes"}))
    assert (result.ok, result.message) == (False, "no such question")
    router = _router(FakeRunner(error=RuntimeError("boom")))
    result = await _run(router, _answer(router, context={"questionId": str(QID), "text": "yes"}))
    assert (result.ok, result.message) == (False, "the answer could not be processed")


@pytest.mark.parametrize("case", ["differs", "missing", "proposal key", "no key"])
async def test_the_answer_subject_is_the_cards_key(case):
    key, context = question_key(QID), {"questionId": str(QID), "text": "yes"}
    if case == "differs":
        context["questionId"] = str(uuid.UUID(int=99))
    elif case == "missing":
        del context["questionId"]
    elif case == "proposal key":
        key = proposal_key(QID)
    elif case == "no key":
        key = None
    runner = FakeRunner(recorded=SimpleNamespace(woke_arrival=True))
    router = _router(runner)
    result = await _run(router, _answer(router, context=context, key=key))
    assert (result.ok, result.message) == (False, "this button does not belong to this card") and runner.calls == []
