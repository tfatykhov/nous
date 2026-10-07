"""Shared builders for the F099 Phase 2b tests.

The fixture is imported by name into a test module (``# noqa: F401``); the
builders are plain functions. Every environment gets its own agent id, so
tests never see each other's rows.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, text, update

from nous.brain import Brain, continuation
from nous.brain.continuation import Resolution
from nous.brain.intentions import IntentionSpec
from nous.cognitive.schemas import Assessment, FrameSelection, TurnContext
from nous.config import Settings
from nous.storage.models import Intention, IntentionArrival, IntentionProposal, ResultInbox

ON = {"result_inbox_enabled": True, "intentions_enabled": True}
CONT = {**ON, "continuation_enabled": True}
CHAN = "telegram:8080"
RESULT = "Powder: 40cm overnight on the upper mountain."


class RecordingBus:
    """A bus that keeps what it was asked to emit."""

    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


@pytest.fixture
async def env_factory(db, mock_embeddings):
    """``await env_factory(**settings_overrides)``: heart, a worker pool with a mock
    HTTP client, and a recording bus, all on one fresh agent."""
    from nous.handlers.subtask_worker import SubtaskWorkerPool
    from nous.heart import Heart

    hearts = []

    async def build(**over):
        agent = f"f099-2b-{uuid.uuid4().hex[:8]}"
        values = {"telegram_bot_token": "", "telegram_chat_id": "", **over}
        settings = Settings(_env_file=None, agent_id=agent, **values)
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        hearts.append(heart)
        http = MagicMock()
        http.post = AsyncMock(return_value=SimpleNamespace(status_code=200))
        pool = SubtaskWorkerPool(MagicMock(), heart, settings, http_client=http)
        return SimpleNamespace(
            agent=agent, settings=settings, heart=heart, pool=pool, http=http, db=db, bus=RecordingBus()
        )

    yield build
    for heart in hearts:
        await heart.close()


async def make_subtask(env, *, policy: str = "continue", routed: bool = True, notify: bool = False):
    """A pending subtask with its intention. ``routed`` gives it F098's routing keys."""
    return await env.heart.subtasks.create(
        task="Check the snow report",
        parent_session_id="S1" if routed else None,
        parent_channel=CHAN if routed else None,
        notify=notify,
        intention=IntentionSpec(
            intent="Tell the user about the snow",
            origin_kind="interactive",
            wake_policy=policy,
            origin_channel=CHAN if routed else None,
        ),
    )


async def finish(env, subtask, how: str = "complete"):
    """Finish a subtask ('complete', 'empty' or 'fail') and return the fresh row."""
    if how == "complete":
        await env.heart.subtasks.complete(subtask.id, RESULT, final_outcome="completed")
    elif how == "empty":
        await env.heart.subtasks.complete(subtask.id, "", final_outcome="completed")
    else:
        await env.heart.subtasks.fail(subtask.id, "boom")
    return await env.heart.subtasks.get(subtask.id)


async def make_dag(
    env,
    *,
    policy: str = "continue",
    origin_channel: str | None = None,
    status: str = "completed",
    parent: Intention | None = None,
):
    """A terminal DAG with its intention (a child of ``parent`` when given). Returns ``(dag, store)``."""
    from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
    from nous.dag.store import DAGStore

    store = DAGStore(env.db, env.agent, env.settings)
    dag = await store.create(
        DAGCreateRequest(
            name="snow-dag",
            origin_channel=origin_channel,
            nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions="x")],
        ),
        intention=IntentionSpec(
            intent="Summarise the alerts",
            origin_kind="continuation" if parent is not None else "interactive",
            wake_policy=policy,
            origin_channel=origin_channel,
            parent_id=parent.id if parent is not None else None,
            origin_authority="internal_only" if parent is not None else None,
        ),
    )
    await store.update_dag_status(dag.id, status, result_summary="ok")
    return await store.get_dag(dag.id), store


def dag_kwargs(dag, *, origin_channel=None, origin_session_id=None, status="completed") -> dict:
    """The keyword arguments ``record_dag_result`` takes, for ``dag``."""
    return dict(
        dag_id=dag.id,
        name=dag.name,
        status=status,
        summary="ok",
        blocked=False,
        origin_channel=origin_channel,
        origin_session_id=origin_session_id,
        generation=dag.delivery_generation,
    )


async def inbox_rows(env, source_id=None) -> list[ResultInbox]:
    async with env.db.session() as s:
        query = select(ResultInbox).where(ResultInbox.agent_id == env.agent)
        if source_id is not None:
            query = query.where(ResultInbox.source_id == source_id)
        return list((await s.execute(query.order_by(ResultInbox.created_at))).scalars().all())


async def intention_of(env, source_kind: str, source_id) -> Intention | None:
    return await env.heart.intentions.get_for_source(source_kind, source_id)


async def set_intention(env, intention_id, **values) -> None:
    """Move an intention by hand, as a later PR's runner would."""
    async with env.db.session() as s:
        await s.execute(update(Intention).where(Intention.id == intention_id).values(**values))
        await s.commit()


async def make_root(env, *, policy: str = "continue", routed: bool = True) -> Intention:
    """The root intention of a pending subtask (``policy``, origin channel ``CHAN`` when ``routed``)."""
    st = await make_subtask(env, policy=policy, routed=routed)
    return await intention_of(env, "subtask", st.id)


async def make_child(env, parent: Intention, *, authority: str = "internal_only") -> Intention:
    """A child intention under ``parent``, as a continuation turn's spawn writes it (a pending subtask)."""
    st = await env.heart.subtasks.create(
        task="follow-up work",
        intention=IntentionSpec(
            intent="next step", origin_kind="continuation", parent_id=parent.id, origin_authority=authority
        ),
    )
    return await intention_of(env, "subtask", st.id)


async def record(env, intention: Intention, *, body: str = RESULT, generation: int = 0):
    """A continue result for ``intention``'s source, written as the worker hook writes it (T4/T6)."""
    async with env.db.session() as s:
        recorded = await continuation.record_result(
            s,
            env.agent,
            intention_id=intention.id,
            source_kind=intention.source_kind,
            source_id=uuid.UUID(intention.source_id),
            msg_type="INFORM",
            title="Snow report",
            body=body,
            source_generation=generation,
            settings=env.settings,
        )
        await s.commit()
    return recorded


async def age(env, *intention_ids, seconds: float = 60) -> None:
    """Make the results of these intentions ``seconds`` old (the debounce reads ``result_at``)."""
    when = datetime.now(UTC) - timedelta(seconds=seconds)
    for intention_id in intention_ids:
        await set_intention(env, intention_id, result_at=when)


async def claim(env, root_id, *, debounce: float = 0, max_wait: float = 0):
    """``continuation.claim_root`` in its own session, committed when it claimed."""
    async with env.db.session() as s:
        got = await continuation.claim_root(
            s, env.agent, root_id, token=uuid.uuid4(), debounce_s=debounce, max_wait_s=max_wait
        )
        if got is not None:
            await s.commit()
    return got


async def eligible(env, *, debounce: float = 20, max_wait: float = 120, limit: int = 50):
    async with env.db.session() as s:
        return await continuation.eligible_roots(s, env.agent, debounce_s=debounce, max_wait_s=max_wait, limit=limit)


async def until_a_backend_waits_on_a_lock(env, *, at_least: int = 1) -> None:
    """Return once ``at_least`` backends of this database wait on a lock. Callers bound it with
    ``asyncio.wait_for``: a lock that is never waited on fails the test, it does not hang it."""
    while True:
        async with env.db.session() as s:  # a fresh transaction each time: the stats view is snapshotted
            waiting = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                        "AND query ILIKE '%brain.intentions%'"
                    )
                )
            ).scalar_one()
        if waiting >= at_least:
            return
        await asyncio.sleep(0.05)


async def add_arrival(
    env,
    root_id,
    n: int,
    *,
    progress: bool | None = False,
    gate_reason: str | None = None,
    tokens: tuple[int, int] = (0, 0),
    decision: str = "continue",
    outcome: str = "resolved",
) -> None:
    """An arrival row as a committed arrival leaves it (the budgets read these)."""
    async with env.db.session() as s:
        s.add(
            IntentionArrival(
                agent_id=env.agent,
                root_id=root_id,
                n=n,
                intention_ids=[root_id],
                claim_token=uuid.uuid4(),
                decision=decision,
                progress_claimed=bool(progress),
                progress=progress,
                gate_reason=gate_reason,
                tokens_in=tokens[0],
                tokens_out=tokens[1],
                outcome=outcome,
            )
        )
        await s.commit()


class StubCognitive:
    """The cognitive layer as the runner calls it, recording what ``pre_turn`` was given."""

    def __init__(self) -> None:
        self.pre_turn_calls: list[dict] = []
        self.end_sessions: list[str] = []

    async def pre_turn(self, agent_id, session_id, user_message, **kwargs):
        self.pre_turn_calls.append({"session_id": session_id, "user_message": user_message, **kwargs})
        return TurnContext(
            system_prompt="You are Nous.",
            frame=FrameSelection(frame_id="task", frame_name="Task", confidence=0.9, match_method="default"),
            decision_id=None,
            active_censors=[],
            context_token_estimate=100,
        )

    async def post_turn(self, agent_id, session_id, turn_result, turn_context, **kwargs):
        return Assessment(actual=turn_result.response_text[:200])

    async def end_session(self, agent_id, session_id, **kwargs):
        self.end_sessions.append(session_id)

    def get_active_episode_id(self, session_id):
        return None

    async def pre_compaction(self, *args, **kwargs):
        return None

    async def list_frames(self, *args, **kwargs):
        return []


def use(name: str, **tool_input) -> dict:
    """A ``tool_use`` content block."""
    return {"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:12]}", "name": name, "input": tool_input}


def say(text: str) -> dict:
    """A ``text`` content block."""
    return {"type": "text", "text": text}


class ScriptedModel:
    """A stand-in for ``AgentRunner._call_api``: each call plays the next scripted step.

    A step is a list of content blocks (``use(...)``, ``say(...)``), an exception to raise, or an async
    callable taking the call's keyword arguments and returning blocks (a test blocks the model on an
    event this way). Every call's keyword arguments (``tools``, ``messages``, ``model_override`` ...) are
    kept in ``calls`` as DEEP COPIES: the tool loop keeps appending to the very ``messages`` list it passed,
    so a reference would show a later state, not what the request carried. A call beyond the script sets
    ``overrun`` (``runner_env`` fails the test at teardown, because the runner under test catches the
    AssertionError this raises and books it as a failed attempt).
    """

    def __init__(self, *steps) -> None:
        self._steps = list(steps)
        self.calls: list[dict] = []
        self.overrun = False

    async def __call__(self, *args, **kwargs):
        from nous.api.models import ApiResponse

        self.calls.append(copy.deepcopy(kwargs))
        if not self._steps:
            self.overrun = True
            raise AssertionError("the model was called more often than the test scripted")
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        blocks = await step(kwargs) if callable(step) else step
        return ApiResponse(
            content=list(blocks),
            stop_reason="tool_use" if any(b.get("type") == "tool_use" for b in blocks) else "end_turn",
            usage={"input_tokens": 100, "output_tokens": 10},
        )


def build_runner(env, model, *, brain=None):
    """A real AgentRunner on the environment's heart (stub cognitive layer, the scripted model) with the
    real subtask tools registered. Returns ``(runner, cognitive, dispatcher)``."""
    from nous.api.runner import AgentRunner
    from nous.api.tools import ToolDispatcher, register_subtask_tools

    cognitive = StubCognitive()
    runner = AgentRunner(cognitive, brain, env.heart, env.settings)
    dispatcher = ToolDispatcher()
    register_subtask_tools(dispatcher, env.heart, env.settings, runner=runner)
    runner.set_dispatcher(dispatcher)
    runner._call_api = model
    return runner, cognitive, dispatcher


@pytest.fixture
async def runner_env(env_factory):
    """``await runner_env(*steps, **settings)``: an environment (continuation on, no debounce, an API key
    so an AgentRunner can be built) with ``.model`` (a ScriptedModel of ``steps``), ``.brain``,
    ``.runner``, ``.cognitive`` and ``.dispatcher``."""
    built = []

    async def build(*steps, **settings):
        values = {
            **CONT,
            "ANTHROPIC_API_KEY": "test-key",
            "continuation_debounce_seconds": 0,
            "continuation_max_wait_seconds": 0,
            **settings,
        }
        env = await env_factory(**values)
        env.model = ScriptedModel(*steps)
        env.brain = Brain(database=env.db, settings=env.settings)
        env.runner, env.cognitive, env.dispatcher = build_runner(env, env.model, brain=env.brain)
        built.append((env.runner, env.model))
        return env

    yield build
    for runner, model in built:
        runner._api_shared = True
        await runner.close()
    assert not any(model.overrun for _runner, model in built), "a scripted model was called more often than scripted"


# ---- Phase 2d ------------------------------------------------------------------------------------------------

SEND_EMAIL_SCHEMA = {
    "type": "object",
    "description": "Send an email.",
    "properties": {
        "to": {"type": "string"},
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["to", "subject", "body"],
}
SEND_EMAIL_ARGS = {"to": "friend@example.com", "subject": "Snow", "body": "40 cm overnight."}


def register_send_email(env, *, text: str = "sent") -> list[dict]:
    """A recording ``send_email`` on the environment's dispatcher (the real one lives in the server's tool set).
    Returns the list of keyword arguments each call received: a test asserts on it, so nothing is ever sent."""
    calls: list[dict] = []

    async def send_email(**kwargs):
        calls.append(kwargs)
        return {"content": [{"type": "text", "text": text}]}

    env.dispatcher.register("send_email", send_email, SEND_EMAIL_SCHEMA)
    return calls


async def claimed(env, *, routed: bool = True):
    """A root with one recorded result, claimed (its intention is ``deciding``): ``(root, claim)``."""
    root = await make_root(env, routed=routed)
    await record(env, root)
    return root, await claim(env, root.id)


async def stage(
    env, got, *, tool="send_email", arguments=None, rationale="The owner asked me to share the snow report."
):
    """Stage one proposal under ``got``'s claim, as ``propose_action`` does. Returns its id."""
    async with env.db.session() as s:
        proposal_id = await continuation.stage_proposal(
            s,
            env.agent,
            intention_id=got.deepest.id,
            root_id=got.root_id,
            claim_token=got.claim_token,
            tool=tool,
            arguments=dict(SEND_EMAIL_ARGS if arguments is None else arguments),
            rationale=rationale,
        )
        await s.commit()
    return proposal_id


async def proposal_row(env, proposal_id) -> IntentionProposal:
    async with env.db.session() as s:
        return await s.get(IntentionProposal, proposal_id)


async def commit_ask(env, got, note: str = "Shall I go ahead?"):
    """``commit_arrival`` of an ``ask`` under ``got``'s claim, committed. None when the fence rejected it."""
    async with env.db.session() as s:
        done = await continuation.commit_arrival(
            s,
            env.agent,
            got,
            resolution=Resolution("ask", note, True, 0.8),
            outcome="resolved",
            settings=env.settings,
        )
        await s.commit()
    return done


async def ask_with_proposals(env, *, count: int = 1, note: str = "May I email this?", routed: bool = True):
    """A root whose turn staged ``count`` distinct proposals and then asked: ``SimpleNamespace(root, got, done,
    ids)`` (``ids`` in creation order, every proposal ``pending``)."""
    root, got = await claimed(env, routed=routed)
    ids = [await stage(env, got, arguments={**SEND_EMAIL_ARGS, "subject": f"Snow {n}"}) for n in range(count)]
    done = await commit_ask(env, got, note)
    return SimpleNamespace(root=root, got=got, done=done, ids=ids)
