"""A turn that starts with a greeting is judged by what follows the greeting,
as any turn is: if that carries a request, the turn is planned as that request
alone. Only a greeting with no request after it skips retrieval.

IntentClassifier.classify set is_greeting whenever the input started with a
greeting word, whatever followed, and plan_retrieval then planned no queries
and zero budgets, so "Hi, can you find what we decided ..." got no
decisions, facts, procedures or episodes. Every test string here is made up.
"""

from __future__ import annotations

import time
import uuid

import pytest
import pytest_asyncio
from conftest import MockEmbeddingProvider

from nous.brain.brain import Brain
from nous.cognitive.intent import IntentClassifier, RetrievalPlan
from nous.cognitive.layer import CognitiveLayer
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings

MEMORY_TYPES = {"decision", "fact", "procedure", "episode"}
# What a greeting with no request after it is planned as, field by field.
GREETING_PLAN = RetrievalPlan(
    queries=[],
    skip_types={"decision", "fact", "procedure", "episode"},
    budget_overrides={"decisions": 0, "facts": 0, "procedures": 0, "episodes": 0},
)


def _classify_and_plan(text: str, **settings):
    classifier = IntentClassifier(Settings(_env_file=None, **settings))
    frame = FrameSelection(frame_id="conversation", frame_name="Conversation", confidence=0.9, match_method="pattern")
    signals = classifier.classify(text, frame)
    return signals, classifier.plan_retrieval(signals, input_text=text)


@pytest.mark.parametrize(
    "text, what_follows",
    [
        ("hi can u check redis", "can u check redis"),
        ("hi, can u check redis", "can u check redis"),
        ("hey, what's new?", "what's new?"),
        ("Hi, the Postgres migration failed", "the Postgres migration failed"),
        ("hello, tell me about the plan", "tell me about the plan"),
        (
            "Hi, can you find what we decided about the deploy last week? I want to write it up before Friday.",
            "can you find what we decided about the deploy last week? I want to write it up before Friday.",
        ),
        # No exception for small talk or a name: each is judged as it would be on
        # its own, and "how are you?" or "Nous" on its own is planned for retrieval.
        ("Hello, how are you?", "how are you?"),
        ("Good morning, Nous", "Nous"),
    ],
)
def test_a_greeting_and_a_request_are_planned_as_the_request_alone(text, what_follows):
    signals, plan = _classify_and_plan(text)
    _, alone = _classify_and_plan(what_follows)

    assert signals.is_greeting is False
    assert plan == alone
    assert {q.memory_type for q in plan.queries} == MEMORY_TYPES


def test_a_long_run_of_greetings_is_passed_over_without_recursion():
    """Every leading greeting is stripped at once, so a message of thousands of
    greetings does not reach the interpreter's recursion limit."""
    signals, plan = _classify_and_plan("hi " * 2000 + "can u check redis")
    _, alone = _classify_and_plan("can u check redis")

    assert signals.is_greeting is False
    assert plan == alone


def test_a_long_run_of_greetings_is_stripped_in_linear_time():
    """One pass over the input: classifying a run of greetings costs no more
    than classifying the same text with the greeting rule off. Stripping one
    greeting at a time copied the rest of the input each time."""
    text = "hi " * 128_000 + "what did we decide?"
    frame = FrameSelection(frame_id="conversation", frame_name="Conversation", confidence=0.9, match_method="pattern")
    on = IntentClassifier(Settings(_env_file=None))
    off = IntentClassifier(Settings(_env_file=None, followup_greeting_request_detection_enabled=False))

    def fastest_of_three(classifier: IntentClassifier) -> float:
        times = []
        for _ in range(3):
            started = time.perf_counter()
            classifier.classify(text, frame)
            times.append(time.perf_counter() - started)
        return min(times)

    assert fastest_of_three(on) <= 2 * fastest_of_three(off) + 0.05


@pytest.mark.parametrize(
    "text", ["hi", "hey there", "good morning!", "hello :)", "what's up?", "Hello?", "Hi there", "hey 👋", "Hi! Hello!"]
)
def test_a_greeting_with_no_request_after_it_still_skips_retrieval(text):
    """Pin: nothing follows the greeting, or nothing that carries a request, so
    the plan is today's, field by field."""
    signals, plan = _classify_and_plan(text)

    assert signals.is_greeting is True
    assert plan == GREETING_PLAN


@pytest.mark.parametrize("text", ["ok", "yes", "sure"])
def test_a_turn_with_no_request_and_no_greeting_is_still_not_planned(text):
    """Pin: the short-input rule the greeting branch now shares still decides
    a turn that has no greeting."""
    signals, plan = _classify_and_plan(text)

    assert signals.is_greeting is False
    assert plan == GREETING_PLAN


@pytest.mark.parametrize(
    "text", ["hi can u check redis", "Hi, the Postgres migration failed", "hello, tell me about the plan"]
)
def test_with_the_switch_off_any_greeting_led_turn_is_classified_as_a_greeting(text):
    """Pin: with the switch off, classify and plan work as they did before the
    setting was added."""
    signals, plan = _classify_and_plan(text, followup_greeting_request_detection_enabled=False)

    assert signals.is_greeting is True
    assert plan == GREETING_PLAN


@pytest.mark.parametrize("text", ["hola", "bonjour", "привет", "こんにちは"])
def test_a_greeting_the_patterns_do_not_cover_is_not_a_greeting(text):
    """Pin: the greeting rule only ever looks at a turn its patterns match."""
    signals, _ = _classify_and_plan(text)

    assert signals.is_greeting is False


@pytest_asyncio.fixture
async def cognitive(db, heart, settings):
    brain = Brain(database=db, settings=settings, embedding_provider=MockEmbeddingProvider())
    yield CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")
    await brain.close()


async def _plan_handed_to_the_context_build(cognitive, session, text: str) -> RetrievalPlan:
    seen: list[RetrievalPlan] = []
    build = cognitive._context.build

    async def build_and_keep_the_plan(*args, **kwargs):
        seen.append(kwargs["retrieval_plan"])
        return await build(*args, **kwargs)

    cognitive._context.build = build_and_keep_the_plan
    await cognitive.pre_turn("nous-default", f"test-fixs-{uuid.uuid4().hex[:8]}", text, session=session)
    (plan,) = seen
    return plan


async def test_pre_turn_plans_a_greeting_led_request_on_what_follows_the_greeting(cognitive, session):
    plan = await _plan_handed_to_the_context_build(cognitive, session, "hi can u check redis")

    assert {q.memory_type for q in plan.queries} == MEMORY_TYPES
    assert {q.query_text for q in plan.queries} == {"can u check redis"}


async def test_the_deictic_rescue_plans_on_what_follows_the_greeting(cognitive, session):
    """A first-turn deictic follow-up after a greeting was always rescued; its
    query no longer carries the greeting."""
    plan = await _plan_handed_to_the_context_build(cognitive, session, "hi, pick up where we left off")

    assert {q.query_text for q in plan.queries} == {"pick up where we left off"}


async def test_the_recap_rebuild_plans_on_what_follows_the_greeting(cognitive, session):
    plan = await _plan_handed_to_the_context_build(cognitive, session, "hi, can u recap?")

    assert {q.query_text for q in plan.queries} == {"can u recap?"}


async def test_pre_turn_plans_a_recap_after_a_greeting_as_a_recap(cognitive, session):
    """The recap rebuild in pre_turn copied the greeting flag, so a recap asked
    for after a greeting ("hey, give me a recap") kept the empty greeting plan."""
    plan = await _plan_handed_to_the_context_build(cognitive, session, "hey, give me a recap")

    assert {q.query_text for q in plan.queries} == {"give me a recap"}
    assert next(q.limit for q in plan.queries if q.memory_type == "episode") >= 8


@pytest.mark.parametrize(
    "text, planned", [("hey, recap", False), ("hi, catch me up", False), ("give me a recap", True)]
)
async def test_with_the_switch_off_a_recap_is_planned_as_before(db, heart, settings, session, text, planned):
    """The switch restores the old recap rebuild too: a recap asked for after a
    greeting keeps the empty greeting plan, and one without a greeting is planned."""
    off = settings.model_copy(update={"followup_greeting_request_detection_enabled": False})
    brain = Brain(database=db, settings=off, embedding_provider=MockEmbeddingProvider())
    try:
        layer = CognitiveLayer(brain, heart, off, identity_prompt="You are Nous.")
        plan = await _plan_handed_to_the_context_build(layer, session, text)
    finally:
        await brain.close()

    assert (plan != GREETING_PLAN) is planned
