"""F118 parity: the outside judge changes nothing anyone reads (real Postgres, stub judges, no model).

For every branch of ``resolve_decisions`` (one item, a batch, a partial failure, a refused supersession, a background
refusal, a background noise) the reply equals the golden reply captured before the judge existed, and
``Brain.review_many`` receives the same arguments, with the judge off and with a live service in shadow whose judge
answers, raises or hangs past its timeout, and with a service whose submit raises. With the judge off nothing is
written to ``nous_system.judge_verdicts``. In shadow the decision row, the ``decision_reviewed`` events and the
calibration report are the same before the judge runs and after it has written its row.

The hook submits a decision only for an item that was resolved as success, partial or failure in an interactive turn,
with the outcome, the cleaned note and one time per call; it is synchronous and adds no await.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from nous import judge as judge_slot
from nous.api.tools import create_nous_tools
from nous.brain.brain import Brain
from nous.config import Settings
from nous.judge.service import OutsideJudge
from nous.judge.slot import JudgeInput, JudgeOutput

pytestmark = pytest.mark.postgres_only

BG_ERROR = (
    "Error: outcome='success' is not available in background/heartbeat turns — they may only resolve a decision as "
    "noise or superseded (non-prediction outcomes, excluded from calibration). Grading a prediction as "
    "success/partial/failure requires an interactive turn."
)
NO_SUCCESSOR = (
    "Error: outcome='superseded' requires superseded_by — the UUID of the decision that replaces this one. Record "
    "the replacement decision first, then resolve this one with its ID. If nothing replaced it, use a different "
    "outcome (e.g. 'noise' or 'failure')."
)

# Each branch: the resolutions (``{dN}`` is the N-th decision, ``{missing}`` an id that does not exist), whether the
# turn is a background one, and the golden reply. The text is the reply as it was before the judge existed.
BRANCHES: dict[str, tuple[list[dict[str, Any]], bool, str, bool]] = {
    "single": (
        [{"decision_id": "{d0}", "outcome": "success", "resolution_note": "the job ran at 03:00"}],
        False,
        "Resolved 1/1: {d0:8} → success",
        False,
    ),
    "batch": (
        [
            {"decision_id": "{d0}", "outcome": "success", "resolution_note": "shipped"},
            {"decision_id": "{d1}", "outcome": "noise"},
            {"decision_id": "{d2}", "outcome": "superseded", "superseded_by": "{d3}", "resolution_note": "replaced"},
            {"decision_id": "{d4}", "outcome": "partial", "resolution_note": "half of it"},
        ],
        False,
        "Resolved 4/4:\n- {d0:8} → success\n- {d1:8} → noise\n- {d2:8} → superseded (by {d3:8})\n- {d4:8} → partial",
        False,
    ),
    "partial_failure": (
        [
            {"decision_id": "{missing}", "outcome": "failure"},
            {"decision_id": "{d0}", "outcome": "failure", "resolution_note": "the job did not run"},
        ],
        False,
        "Resolved 1/2:\n- {d0:8} → failure\nFailures:\n- {missing}: Decision {missing} not found",
        False,
    ),
    "refused_supersession": (
        [{"decision_id": "{d0}", "outcome": "superseded"}],
        False,
        "Resolved 0/1.\nFailures:\n- {d0}: " + NO_SUCCESSOR,
        True,
    ),
    "background_refusal": (
        [{"decision_id": "{d0}", "outcome": "success"}],
        True,
        "Resolved 0/1.\nFailures:\n- {d0}: " + BG_ERROR,
        True,
    ),
    "background_noise": (
        [{"decision_id": "{d0}", "outcome": "noise", "resolution_note": "a tick artifact"}],
        True,
        "Resolved 1/1: {d0:8} → noise",
        False,
    ),
}


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event: Any) -> None:
        self.events.append((event.type, dict(event.data)))


class StubJudge:
    name = "stub"
    version = "stub-1"

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self.calls = 0

    async def grade(self, record: JudgeInput, *, timeout: float) -> JudgeOutput:
        self.calls += 1
        if self.behaviour == "raise":
            raise RuntimeError("broken judge")
        if self.behaviour == "hang":
            await asyncio.sleep(3600)
        return JudgeOutput(status="ok", verdict="failure", confidence=0.9, labels=("evidence_contradicts",))


class RaisingService:
    """A service whose submit raises (it never does; the hook must not care)."""

    def __init__(self) -> None:
        self.calls = 0

    def submit_decision(self, *args: Any, **kwargs: Any) -> bool:
        self.calls += 1
        raise RuntimeError("submit broke")


class SpyService:
    def __init__(self) -> None:
        self.submitted: list[tuple[uuid.UUID, dict[str, Any]]] = []

    def submit_decision(self, decision_id: Any, **kwargs: Any) -> bool:
        self.submitted.append((decision_id, kwargs))
        return True


@pytest_asyncio.fixture
async def env(db, mock_embeddings):
    agent = f"test-f118-par-{uuid.uuid4().hex[:8]}"
    settings = Settings(_env_file=None, agent_id=agent)
    brain = Brain(database=db, settings=settings, embedding_provider=mock_embeddings)
    bus = RecordingBus()
    brain._bus = bus
    tools = create_nous_tools(brain, None, settings)
    yield {"agent": agent, "settings": settings, "brain": brain, "bus": bus, "tools": tools}
    judge_slot.set_current(None)
    await brain.close()
    async with db.session() as session:
        for table in (
            "brain.decisions",
            "nous_system.tool_call_log",
            "nous_system.judge_verdicts",
            "nous_system.events",
        ):
            await session.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": agent})
        await session.commit()


async def _decisions(db, agent: str, n: int) -> list[uuid.UUID]:
    """``n`` decisions, each with a session and one call in it, so a judge is called for each."""
    ids = [uuid.uuid4() for _ in range(n)]
    now = datetime.now(UTC)
    async with db.session() as session:
        for i, decision_id in enumerate(ids):
            await session.execute(
                text(
                    "INSERT INTO brain.decisions (id, agent_id, description, confidence, category, stakes, "
                    "session_id, created_at) VALUES (:id, :a, :d, 0.7, 'tooling', 'low', :s, :t)"
                ),
                {"id": decision_id, "a": agent, "d": f"Decision {i}", "s": f"s-{decision_id}", "t": now - timedelta(1)},
            )
            await session.execute(
                text(
                    "INSERT INTO nous_system.tool_call_log (agent_id, session_id, call_index, work_kind, tool_name, "
                    "tool_name_called, outcome, result_preview, started_at) VALUES (:a, :s, 0, 'conversation', 'bash', "
                    "'bash', 'ok', 'ran', :t)"
                ),
                {"a": agent, "s": f"s-{decision_id}", "t": now - timedelta(hours=23)},
            )
        await session.commit()
    return ids


def _fill(value: Any, names: dict[str, str]) -> Any:
    if isinstance(value, str):
        out = value
        for key, real in names.items():
            out = out.replace("{" + key + ":8}", real[:8]).replace("{" + key + "}", real)
        return out
    if isinstance(value, dict):
        return {k: _fill(v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, names) for v in value]
    return value


def _unfill(value: Any, names: dict[str, str]) -> Any:
    """``value`` with every id written back as its placeholder, so two runs over different ids compare."""
    if isinstance(value, uuid.UUID):
        value = str(value)
    if isinstance(value, str):
        out = value
        for key, real in names.items():
            out = out.replace(real, "{" + key + "}")
        return out
    if isinstance(value, dict):
        return {k: _unfill(v, names) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_unfill(v, names) for v in value]
    return value


async def _run_branch(db, env: dict, branch: str) -> dict[str, Any]:
    """Run one branch on fresh decisions; return its reply, review_many's arguments and the ids, all unfilled."""
    resolutions, background, _golden, _is_error = BRANCHES[branch]
    ids = await _decisions(db, env["agent"], 5)
    names = {f"d{i}": str(d) for i, d in enumerate(ids)} | {"missing": str(uuid.uuid4())}
    brain = env["brain"]
    seen: list[Any] = []
    real = Brain.review_many

    async def spy(self, items, reviewer=None, session=None, preserve_graded=False, captures=None):
        seen.append(
            {
                "items": [dict(i) for i in items],
                "reviewer": reviewer,
                "preserve_graded": preserve_graded,
                "captures": captures,
            }
        )
        return await real(
            self, items, reviewer=reviewer, session=session, preserve_graded=preserve_graded, captures=captures
        )

    brain.review_many = spy.__get__(brain)
    try:
        reply = await env["tools"]["resolve_decisions"](
            resolutions=_fill(resolutions, names), _is_background=background
        )
    finally:
        del brain.review_many
    return {"reply": _unfill(reply, names), "review": _unfill(seen, names), "ids": ids, "names": names}


def _golden(branch: str) -> dict[str, Any]:
    _resolutions, _background, text_reply, is_error = BRANCHES[branch]
    return {"content": [{"type": "text", "text": text_reply.replace(":8}", "}")}], "is_error": is_error}


def _short(reply: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """The reply with each echoed short id (eight characters) written back as the full id's placeholder."""
    text_reply = reply["content"][0]["text"]
    for key, real in run["names"].items():
        text_reply = text_reply.replace(real[:8], "{" + key + "}")
    return {**reply, "content": [{**reply["content"][0], "text": text_reply}]}


async def _verdict_rows(db, agent: str) -> int:
    async with db.session() as session:
        return await session.scalar(
            text("SELECT count(*) FROM nous_system.judge_verdicts WHERE agent_id = :a"), {"a": agent}
        )


# ---- off ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("branch", list(BRANCHES))
async def test_with_the_judge_off_every_branch_gives_the_golden_reply(db, env, branch):
    assert judge_slot.current() is None
    run = await _run_branch(db, env, branch)
    assert _short(run["reply"], run) == _golden(branch)
    assert await _verdict_rows(db, env["agent"]) == 0


# ---- shadow ---------------------------------------------------------------------------------------------------------


# The whole decision row, every column, as one JSON value.
_WHOLE_ROWS = "SELECT to_jsonb(d) FROM brain.decisions d WHERE d.id = ANY(:ids) ORDER BY d.id"


async def _decision_rows(db, ids: list[uuid.UUID]) -> list[Any]:
    async with db.session() as session:
        return list((await session.execute(text(_WHOLE_ROWS), {"ids": ids})).scalars())


# A different value for a column of each type a decision has (a constrained column gets another allowed value).
_CHANGED = {
    "category": "'security'",
    "stakes": "'critical'",
    "outcome": "'noise'",
    "superseded_by": "id",
    "embedding": "array_fill(0.5::real, ARRAY[1536])::vector",
}
_BY_TYPE = {
    "text": "coalesce({c}, '') || 'x'",
    "character varying": "coalesce({c}, '') || 'x'",
    "double precision": "coalesce({c}, 0.5) / 2",
    "real": "coalesce({c}, 0.5) / 2",
    "timestamp with time zone": "coalesce({c}, now()) + interval '1 second'",
    "integer": "coalesce({c}, 0) + 1",
    "boolean": "NOT coalesce({c}, false)",
    "jsonb": "'{{\"changed\": true}}'::jsonb",
}


async def test_the_whole_row_snapshot_catches_a_write_to_any_column(db, env):
    """The snapshot the shadow parity test compares is every column of the row: a write to any one changes it."""
    [decision_id] = await _decisions(db, env["agent"], 1)
    async with db.engine.connect() as conn:
        trans = await conn.begin()
        try:
            columns = (
                await conn.execute(
                    text(
                        "SELECT column_name, data_type, is_generated FROM information_schema.columns "
                        "WHERE table_schema = 'brain' AND table_name = 'decisions' ORDER BY ordinal_position"
                    )
                )
            ).all()
            before = await conn.scalar(text(_WHOLE_ROWS), {"ids": [decision_id]})
            changed = []
            for column, data_type, generated in columns:
                if column == "id" or generated == "ALWAYS":
                    continue
                value = _CHANGED.get(column) or _BY_TYPE.get(data_type, "").format(c=column)
                assert value, f"no test value for {column} ({data_type})"
                savepoint = await conn.begin_nested()
                await conn.execute(
                    text(f"UPDATE brain.decisions SET {column} = {value} WHERE id = :id"), {"id": decision_id}
                )
                after = await conn.scalar(text(_WHOLE_ROWS), {"ids": [decision_id]})
                await savepoint.rollback()
                assert after != before, column
                changed.append(column)
            assert {"outcome", "outcome_result", "reviewer", "reviewed_at", "quality_score", "updated_at"} <= set(
                changed
            )
        finally:
            await trans.rollback()


@pytest.mark.parametrize("behaviour", ["ok", "raise", "hang", "submit_raises"])
@pytest.mark.parametrize("branch", list(BRANCHES))
async def test_in_shadow_every_branch_gives_the_golden_reply_and_the_same_review(db, env, branch, behaviour):
    off = await _run_branch(db, env, branch)
    if behaviour == "submit_raises":
        service: Any = RaisingService()
    else:
        service = OutsideJudge(db, env["settings"], StubJudge(behaviour), settle_seconds=0.0, timeout_seconds=0.3)
        service.start()
    judge_slot.set_current(service)
    try:
        shadow = await _run_branch(db, env, branch)
        assert _short(shadow["reply"], shadow) == _golden(branch) == _short(off["reply"], off)
        assert shadow["review"] == off["review"]
        if isinstance(service, OutsideJudge):
            await asyncio.wait_for(_until_idle(service), 10)
    finally:
        if isinstance(service, OutsideJudge):
            await service.close()


async def _until_idle(service: OutsideJudge) -> None:
    while not service.idle:
        await asyncio.sleep(0.02)


@pytest.mark.parametrize("behaviour", ["ok", "raise", "hang"])
async def test_in_shadow_the_decision_row_the_events_and_the_calibration_do_not_move(db, env, behaviour):
    brain, bus = env["brain"], env["bus"]
    stub = StubJudge(behaviour)
    service = OutsideJudge(db, env["settings"], stub, settle_seconds=1.0, timeout_seconds=0.3)
    service.start()
    judge_slot.set_current(service)
    try:
        run = await _run_branch(db, env, "batch")
        ids = run["ids"]
        rows, events = await _decision_rows(db, ids), list(bus.events)
        calibration = (await brain.get_calibration()).model_dump()
        assert await _verdict_rows(db, env["agent"]) == 0  # the judge has not run yet
        await asyncio.wait_for(_until_idle(service), 10)
        assert stub.calls == 2  # the success and the partial item
        assert await _verdict_rows(db, env["agent"]) == 2
        assert await _decision_rows(db, ids) == rows
        assert bus.events == events
        assert len([e for e in events if e[0] == "decision_reviewed"]) == 4
        assert (await brain.get_calibration()).model_dump() == calibration
    finally:
        await service.close()


# ---- the hook -------------------------------------------------------------------------------------------------------


async def test_the_hook_submits_only_graded_items_of_an_interactive_turn(db, env):
    spy = SpyService()
    judge_slot.set_current(spy)
    before = datetime.now(UTC)
    run = await _run_branch(db, env, "batch")
    after = datetime.now(UTC)
    names = run["names"]
    submitted = [(str(decision_id), kwargs) for decision_id, kwargs in spy.submitted]
    assert [(_unfill(d, names), k["grade"], k["grader"]) for d, k in submitted] == [
        ("{d0}", "success", "agent"),
        ("{d4}", "partial", "agent"),
    ]
    times = {k["resolved_at"] for _d, k in submitted}
    assert len(times) == 1 and before <= times.pop() <= after
    async with db.session() as session:
        stored = dict(
            (
                await session.execute(
                    text("SELECT id::text, outcome_result FROM brain.decisions WHERE id = ANY(:ids)"),
                    {"ids": run["ids"]},
                )
            ).all()
        )
    assert [k["note"] for _d, k in submitted] == [stored[d] for d, _k in submitted] == ["shipped", "half of it"]


@pytest.mark.parametrize("branch", ["partial_failure", "single"])
async def test_the_hook_submits_a_failure_and_a_success(db, env, branch):
    spy = SpyService()
    judge_slot.set_current(spy)
    run = await _run_branch(db, env, branch)
    expected = {"partial_failure": ("failure", "the job did not run"), "single": ("success", "the job ran at 03:00")}
    assert [(_unfill(str(d), run["names"]), k["grade"], k["note"]) for d, k in spy.submitted] == [
        ("{d0}", *expected[branch])
    ]


@pytest.mark.parametrize("branch", ["refused_supersession", "background_refusal", "background_noise"])
async def test_the_hook_submits_nothing_for_a_refused_item_or_a_background_turn(db, env, branch):
    spy = SpyService()
    judge_slot.set_current(spy)
    await _run_branch(db, env, branch)
    assert spy.submitted == []


async def test_a_background_turn_that_resolves_noise_submits_nothing_and_a_missing_id_neither(db, env):
    spy = SpyService()
    judge_slot.set_current(spy)
    missing = str(uuid.uuid4())
    await env["tools"]["resolve_decisions"](resolutions=[{"decision_id": missing, "outcome": "success"}])
    assert spy.submitted == []


def test_the_hook_is_synchronous_and_resolve_decisions_does_not_await_it():
    import inspect

    from nous.api import tools

    assert not inspect.iscoroutinefunction(tools._submit_to_outside_judge)
    source = inspect.getsource(tools.create_nous_tools)
    body = source.split("async def resolve_decisions(", 1)[1].split("async def list_decisions(", 1)[0]
    assert body.count("_submit_to_outside_judge(") == 1
    assert "await _submit_to_outside_judge" not in body
    # It runs once the reply's text is final, right before the reply is returned.
    assert body.index("_submit_to_outside_judge(") > body.index('text += "\\nFailures:\\n"')
