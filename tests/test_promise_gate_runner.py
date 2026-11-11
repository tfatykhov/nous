"""F111 PR A: the promise gate at both terminal points, the commit tool in every context, and /drop (spec D.7 items
6 shadow and warn, 7, 10, 11, 15 /drop, 16).

The runner runs with a fake model (``_call_api_stream`` for stream_chat, ``_tool_loop`` for run_turn), the real
dispatcher, authorization and claim verifier, and an in-memory ledger store with the real store's validation
(``test_promise_gate.FakeStore``). The /drop route runs against Postgres and skips until migration 107 is applied.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from starlette.applications import Starlette
from test_f099_phase2d_bot import _bot, _message, _sent
from test_promise_gate import REAL_RESULTS, FakeStore

from nous.api import runner as runner_module
from nous.api.anthropic_client import StreamEvent
from nous.api.commitment_routes import build_commitment_routes
from nous.api.execution_context import ExecutionContext
from nous.api.runner import AgentRunner
from nous.api.tools import ToolDispatcher, register_commit_tool
from nous.cognitive.promise_gate import PromiseGate
from nous.cognitive.schemas import Assessment, FrameSelection, ToolResult, TurnContext
from nous.config import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "promise_gate"
NOV10 = json.loads((FIXTURES / "nov10_hero.json").read_text())
OCT08 = json.loads((FIXTURES / "oct08_items_1_3.json").read_text())


def _turn_context() -> TurnContext:
    return TurnContext(
        system_prompt="You are Nous.",
        frame=FrameSelection(
            frame_id="conversation", frame_name="Conversation", confidence=0.9, match_method="default"
        ),
        decision_id=None,
        active_censors=[],
        context_token_estimate=100,
    )


class _Cognitive:
    """Records what post_turn was handed: the text the episode and the turn's memory are built from."""

    def __init__(self) -> None:
        self.post_turn_texts: list[str] = []

    async def pre_turn(self, agent_id, session_id, user_input, **kwargs):
        return _turn_context()

    async def post_turn(self, agent_id, session_id, turn_result, turn_context, **kwargs):
        self.post_turn_texts.append(turn_result.response_text)
        return Assessment(actual=turn_result.response_text[:200])

    async def end_session(self, *args, **kwargs):
        return None

    def get_active_episode_id(self, session_id):
        return None


class _Stub:
    async def close(self):
        pass


def _text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _dispatcher(store: FakeStore) -> ToolDispatcher:
    """read_file, recall_deep, record_decision and dag_create answer with their real success shapes; commit is the
    real tool over the fake store."""
    d = ToolDispatcher()
    schema = {"type": "object", "properties": {}}

    async def read_file(**_):
        return _text(NOV10["tool_results"][0]["result"])

    async def recall_deep(**_):
        return _text("1 memory found.")

    async def record_decision(**_):
        return _text("Decision recorded: 192d0e7d")

    async def dag_create(**_):
        return _text(OCT08["dag_create_result"]["result"])

    async def schedule_task(**_):
        return _text(REAL_RESULTS["schedule_task"][0])

    for name, fn in [
        ("read_file", read_file),
        ("recall_deep", recall_deep),
        ("record_decision", record_decision),
        ("dag_create", dag_create),
        ("schedule_task", schedule_task),
    ]:
        d.register(name, fn, schema)
    register_commit_tool(d, store)
    return d


def _settings(mode: str) -> Settings:
    return Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key",
        agent_id="test-agent",
        promise_gate_mode=mode,
        action_gating_enabled=False,
    )


def _runner(mode: str, store: FakeStore, segments: list[list[StreamEvent]] | None = None, *, gate=True):
    cognitive = _Cognitive()
    settings = _settings(mode)
    runner = AgentRunner(cognitive, _Stub(), _Stub(), settings)
    runner.set_dispatcher(_dispatcher(store))
    if gate:
        runner.set_promise_gate(PromiseGate(store, settings))
    calls = iter(segments or [])

    async def fake_stream(*args, **kwargs):
        for event in next(calls):
            yield event

    runner._call_api_stream = MagicMock(side_effect=fake_stream)
    return runner, cognitive


def _tool_segment(*calls: tuple[str, dict]) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    for i, (name, args) in enumerate(calls):
        events += [
            StreamEvent(type="tool_start", tool_id=f"t{i}", tool_name=name, block_index=i),
            StreamEvent(type="tool_input_delta", text=json.dumps(args), block_index=i),
            StreamEvent(type="block_stop", block_index=i),
        ]
    return [*events, StreamEvent(type="done", stop_reason="tool_use")]


def _reply_segment(text: str) -> list[StreamEvent]:
    return [StreamEvent(type="text_delta", text=text), StreamEvent(type="done", stop_reason="end_turn")]


async def _stream(runner: AgentRunner, session_id: str, message: str = "go") -> str:
    out = []
    try:
        async for event in runner.stream_chat(session_id, message):
            if event.type == "text_delta":
                out.append(event.text)
    finally:
        await runner.close()
    return "".join(out)


def _stored_reply(runner: AgentRunner, session_id: str) -> str:
    return runner._conversations[session_id].messages[-1].content


NOV10_READS = _tool_segment(("read_file", {"path": "site/index.html"}), ("recall_deep", {"query": "hero"}))


# ---- 6. the Nov 10 replay: shadow and warn, both paths -------------------------------------------------------------


async def test_nov10_stream_shadow_records_a_row_and_changes_nothing_visible():
    store = FakeStore()
    runner, cognitive = _runner("shadow", store, [NOV10_READS, _reply_segment(NOV10["reply"])])
    shown = await _stream(runner, "s-nov10-shadow", NOV10["user_message"])
    assert shown == NOV10["reply"]
    [row] = store.rows.values()
    assert row.consent == "silence" and row.status == "open" and row.detected_by == "gate"
    assert row.source_kind == "chat_stream" and row.kind == "action"
    assert "origin_decision_id" not in store.calls[0]  # the gate never links a decision
    [log] = store.logs
    assert (log["path"], log["mode"], log["outcome"]) == ("stream", "shadow", "clean")
    assert (log["detected"], log["backed"], log["unbacked"]) == (1, 0, 1)


async def test_nov10_stream_warn_shows_one_footer_and_stores_none_of_it():
    store = FakeStore()
    runner, cognitive = _runner("warn", store, [NOV10_READS, _reply_segment(NOV10["reply"])])
    shown = await _stream(runner, "s-nov10-warn", NOV10["user_message"])
    [row] = store.rows.values()
    assert shown.startswith(NOV10["reply"]) and shown != NOV10["reply"]
    footer = shown[len(NOV10["reply"]) :].strip()
    assert footer.count("\n") == 0 and row.label in footer and "due in 60 min" in footer
    assert store.logs[0]["outcome"] == "footer"
    # Item 16: neither the conversation, nor post_turn (the episode), nor the claim check saw the footer.
    assert _stored_reply(runner, "s-nov10-warn") == NOV10["reply"]
    assert cognitive.post_turn_texts == [NOV10["reply"]]


async def test_nov10_run_turn_warn_returns_the_footer_and_stores_none_of_it():
    store = FakeStore()
    runner, cognitive = _runner("warn", store)
    results = [ToolResult(**r) for r in NOV10["tool_results"]]
    runner._tool_loop = AsyncMock(return_value=(NOV10["reply"], results, {"input_tokens": 1, "output_tokens": 1}, []))
    try:
        text, _ctx, _usage = await runner.run_turn("s-nov10-rt", NOV10["user_message"])
    finally:
        await runner.close()
    [row] = store.rows.values()
    assert text.startswith(NOV10["reply"]) and row.label in text[len(NOV10["reply"]) :]
    assert row.source_kind == "chat_turn" and store.logs[0]["path"] == "run_turn"
    assert _stored_reply(runner, "s-nov10-rt") == NOV10["reply"]
    assert cognitive.post_turn_texts == [NOV10["reply"]]


async def test_enforce_is_handled_as_warn_in_pr_a(caplog):
    store = FakeStore()
    runner, _ = _runner("enforce", store, [_reply_segment(NOV10["reply"])])
    with caplog.at_level(logging.INFO, logger="nous.cognitive.promise_gate"):
        shown = await _stream(runner, "s-enforce")
    assert shown != NOV10["reply"] and len(store.rows) == 1 and store.logs[0]["outcome"] == "footer"
    assert runner._call_api_stream.call_count == 1  # no re-entry
    assert "handled as warn" in caplog.text


async def test_mode_off_and_no_gate_do_nothing():
    for runner, store in [
        _runner("off", s := FakeStore(), [_reply_segment(NOV10["reply"])])[:1] + (s,),
        _runner("warn", s2 := FakeStore(), [_reply_segment(NOV10["reply"])], gate=False)[:1] + (s2,),
    ]:
        assert await _stream(runner, f"s-off-{uuid.uuid4().hex[:6]}") == NOV10["reply"]
        assert store.rows == {} and store.logs == []


async def test_an_awaiting_user_promise_is_recorded_without_a_footer():
    store = FakeStore()
    runner, _ = _runner("warn", store, [_reply_segment("Done with the review. I'll merge it once you confirm.")])
    shown = await _stream(runner, "s-await")
    [row] = store.rows.values()
    assert row.kind == "awaiting_user" and "C-" not in shown and store.logs[0]["outcome"] == "clean"


async def test_a_mixed_reply_names_only_the_action_row():
    store = FakeStore()
    reply = "I'll merge it once you confirm. I'll check back tomorrow."
    runner, _ = _runner("warn", store, [_reply_segment(reply)])
    shown = await _stream(runner, "s-mixed")
    footer = shown[len(reply) :]
    assert len(store.rows) == 2 and "C-0002" in footer and "C-0001" not in footer


async def test_nov10_run_turn_shadow_returns_the_reply_unchanged():
    store = FakeStore()
    runner, _ = _runner("shadow", store)
    results = [ToolResult(**r) for r in NOV10["tool_results"]]
    runner._tool_loop = AsyncMock(return_value=(NOV10["reply"], results, {"input_tokens": 1, "output_tokens": 1}, []))
    try:
        text, _ctx, _usage = await runner.run_turn("s-nov10-rt-shadow", NOV10["user_message"])
    finally:
        await runner.close()
    [row] = store.rows.values()
    assert text == NOV10["reply"] and row.consent == "silence" and store.logs[0]["outcome"] == "clean"


async def test_a_far_promise_is_clamped_not_lost():
    store = FakeStore()
    runner, _ = _runner(
        "warn", store, [_reply_segment("I'll revisit the pricing in 20 weeks. I'll check back tomorrow.")]
    )
    await _stream(runner, "s-far")
    assert len(store.rows) == 2 and store.logs[0]["outcome"] == "footer"


async def test_a_clean_turn_still_logs_the_denominator():
    store = FakeStore()
    runner, _ = _runner("warn", store, [_reply_segment("Here is the summary you asked for.")])
    await _stream(runner, "s-clean")
    assert store.rows == {} and store.logs[0]["outcome"] == "clean" and store.logs[0]["detected"] == 0


# ---- 7. the Oct 8 replay ----------------------------------------------------------------------------------------


async def test_oct08_record_decision_does_not_back_the_promise():
    store = FakeStore()
    tools = _tool_segment(("record_decision", {"description": "NOW list"}), ("recall_deep", {"query": "NOW list"}))
    runner, _ = _runner("warn", store, [tools, _reply_segment(OCT08["reply"])])
    await _stream(runner, "s-oct08")
    assert (store.logs[0]["backed"], store.logs[0]["unbacked"]) == (0, 1) and len(store.rows) == 1


async def test_oct08_a_dag_create_in_the_same_turn_backs_it():
    store = FakeStore()
    tools = _tool_segment(("record_decision", {"description": "NOW list"}), ("dag_create", {"name": "items 1-3"}))
    runner, _ = _runner("warn", store, [tools, _reply_segment(OCT08["reply"])])
    shown = await _stream(runner, "s-oct08-dag")
    assert shown == OCT08["reply"] and store.rows == {}
    assert (store.logs[0]["backed"], store.logs[0]["unbacked"], store.logs[0]["outcome"]) == (1, 0, "clean")


async def test_oct08_three_commit_calls_back_it():
    store = FakeStore()
    tools = _tool_segment(
        *(("commit", {"what": r["arguments"]["what"], "due": "in 6 hours"}) for r in OCT08["commit_results"])
    )
    runner, _ = _runner("warn", store, [tools, _reply_segment(OCT08["reply"])])
    shown = await _stream(runner, "s-oct08-commit")
    assert shown == OCT08["reply"]
    assert {r.detected_by for r in store.rows.values()} == {"tool"} and len(store.rows) == 3  # no gate row
    assert (store.logs[0]["backed"], store.logs[0]["unbacked"]) == (1, 0)


async def test_oct08_retrospective_reply_is_clean():
    store = FakeStore()
    runner, _ = _runner("warn", store, [_reply_segment(OCT08["retrospective_reply"])])
    await _stream(runner, "s-oct08-retro")
    assert store.rows == {} and store.logs[0]["detected"] == 0


# ---- 10. fail-open ----------------------------------------------------------------------------------------------


class _BrokenStore(FakeStore):
    async def record_gate_turn(self, rows, log):
        raise RuntimeError("database is down")


class _SlowStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.finished = asyncio.Event()

    async def record_gate_turn(self, rows, log):
        await asyncio.sleep(0.3)
        written = await super().record_gate_turn(rows, log)
        self.finished.set()
        return written


async def test_a_gate_that_raises_leaves_the_stream_reply_unchanged(caplog):
    store = _BrokenStore()
    runner, cognitive = _runner("warn", store, [_reply_segment(NOV10["reply"])])
    with caplog.at_level(logging.WARNING, logger="nous.api.runner"):
        shown = await _stream(runner, "s-broken")
        await asyncio.sleep(0)  # the error row is written in the background
    assert shown == NOV10["reply"] and cognitive.post_turn_texts == [NOV10["reply"]]
    assert "promise gate failed" in caplog.text
    assert [log["outcome"] for log in store.logs] == ["error"]


async def test_a_gate_that_raises_leaves_the_run_turn_reply_unchanged(caplog):
    store = _BrokenStore()
    runner, _ = _runner("warn", store)
    runner._tool_loop = AsyncMock(return_value=(NOV10["reply"], [], {"input_tokens": 1, "output_tokens": 1}, []))
    with caplog.at_level(logging.WARNING, logger="nous.api.runner"):
        try:
            text, _ctx, _usage = await runner.run_turn("s-broken-rt", "go")
            await asyncio.sleep(0)  # the error row is written in the background
        finally:
            await runner.close()
    assert text == NOV10["reply"] and "promise gate failed" in caplog.text
    assert store.logs[0]["outcome"] == "error"


async def test_a_slow_gate_times_out_the_reply_goes_out_and_the_gate_is_not_cancelled(monkeypatch, caplog):
    monkeypatch.setattr(runner_module, "PROMISE_GATE_TIMEOUT_SECONDS", 0.05)
    store = _SlowStore()
    runner, _ = _runner("warn", store, [_reply_segment(NOV10["reply"])])
    with caplog.at_level(logging.WARNING, logger="nous.api.runner"):
        shown = await asyncio.wait_for(_stream(runner, "s-slow"), timeout=3)
    assert shown == NOV10["reply"] and "took longer than" in caplog.text
    await asyncio.wait_for(store.finished.wait(), timeout=3)  # the gate finished its transaction in the background
    await asyncio.sleep(0)  # the done callback runs one loop tick after the task ends
    assert len(store.rows) == 1 and store.logs[0]["outcome"] == "footer" and not runner._promise_gate_tasks


async def test_a_cancelled_turn_is_still_cancelled_while_the_gate_runs():
    store = _SlowStore()
    runner, _ = _runner("shadow", store)
    runner._tool_loop = AsyncMock(return_value=(NOV10["reply"], [], {"input_tokens": 1, "output_tokens": 1}, []))
    task = asyncio.ensure_future(runner.run_turn("s-cancel", "go"))
    await asyncio.sleep(0.1)  # inside the gate's 0.3 s write
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(store.finished.wait(), timeout=3)  # the shielded write still completed
    await runner.close()


async def test_a_consumer_that_closes_at_the_footer_loses_nothing():
    """The footer is the last text delta; a consumer that stops there (aclose) finds the reply stored and claimed."""
    store = FakeStore()
    runner, cognitive = _runner("warn", store, [_reply_segment(NOV10["reply"])])
    gen = runner.stream_chat("s-close", "go")
    deltas = []
    async for event in gen:
        if event.type == "text_delta":
            deltas.append(event.text)
            if len(deltas) == 2:  # the reply, then the footer
                break
    await gen.aclose()
    await runner.close()
    assert "C-0001" in deltas[1]
    assert _stored_reply(runner, "s-close") == NOV10["reply"] and cognitive.post_turn_texts == [NOV10["reply"]]


# ---- background turns: rows, never a footer ---------------------------------------------------------------------


async def test_a_background_run_turn_records_but_never_shows_a_footer():
    store = FakeStore()
    runner, _ = _runner("warn", store)
    runner._tool_loop = AsyncMock(return_value=(NOV10["reply"], [], {"input_tokens": 1, "output_tokens": 1}, []))
    try:
        text, _ctx, _usage = await runner.run_turn(
            "subtask-abc", "check the hero", context=ExecutionContext(kind="subtask", session_id="subtask-abc")
        )
    finally:
        await runner.close()
    [row] = store.rows.values()
    assert text == NOV10["reply"] and row.source_kind == "subtask"
    assert store.logs[0]["context_kind"] == "subtask"


# ---- 11. commit in every context --------------------------------------------------------------------------------


def _offered(runner: AgentRunner, ctx: ExecutionContext, *, is_subtask: bool) -> set[str]:
    tools = runner._offered_tools(ctx, "conversation", is_subtask=is_subtask, tool_filter=None, refuse_active=False)
    return {t["name"] for t in tools}


def test_a_subtask_turn_is_offered_commit_but_not_schedule_task():
    runner, _ = _runner("shadow", FakeStore())
    names = _offered(runner, ExecutionContext(kind="subtask", session_id="subtask-1"), is_subtask=True)
    assert "commit" in names and "schedule_task" not in names


@pytest.mark.parametrize("kind", ["heartbeat_triage", "dag_node", "scheduled", "background"])
def test_background_kinds_are_offered_commit(kind):
    runner, _ = _runner("shadow", FakeStore())
    names = _offered(runner, ExecutionContext(kind=kind, session_id="bg-1"), is_subtask=False)
    assert "commit" in names
    if kind == "heartbeat_triage":
        assert "dag_create" not in names  # triage stays local-only: commit is a write, not a spawn


def test_commit_is_not_excluded_from_subtasks():
    assert "commit" not in runner_module._SUBTASK_EXCLUDED_TOOLS


async def test_commit_dispatched_from_a_subtask_turn_is_authorized_and_stamped_subtask():
    store = FakeStore()
    runner, _ = _runner("shadow", store)
    ctx = ExecutionContext(kind="subtask", session_id="subtask-9")
    assert runner._authorize_tool_call(ctx, "commit", frozenset({"commit"}), "subtask-9", {}) is None
    text, is_error = await runner._dispatcher.dispatch(
        "commit",
        {"what": "propose the Nov 13 review", "due": "in 1 day", "_context_kind": "interactive"},
        session_id="subtask-9",
        turn_number=2,
        context=ctx,
    )
    assert not is_error and text.startswith("Commitment C-0001 recorded")
    assert store.calls[0]["source_kind"] == "subtask"  # the model's own _context_kind was dropped


# ---- 15. /drop --------------------------------------------------------------------------------------------------


async def test_drop_from_the_owner_chat_posts_and_answers_in_fixed_words():
    bot = _bot({"/commitments/42/drop": (200, {"short_id": "C-0042", "status": "cancelled", "changed": True})})
    await bot._handle_update(_message("/drop C-0042"))
    assert bot._http.calls == [("/commitments/42/drop", {"actor": "telegram:42"})]
    assert _sent(bot) == ["Dropped C-0042; it will not be followed up."]
    bot._chat_streaming.assert_not_called()


async def test_done_and_a_repeat_drop_answer_in_fixed_words():
    bot = _bot(
        {
            "/commitments/7/done": (200, {"short_id": "C-0007", "status": "done", "changed": True}),
            "/commitments/8/drop": (200, {"short_id": "C-0008", "status": "cancelled", "changed": False}),
        }
    )
    await bot._handle_update(_message("/done C-7"))
    await bot._handle_update(_message("/drop c-0008"))
    assert _sent(bot) == ["Marked C-0007 as done.", "C-0008 was already closed; nothing changed."]


@pytest.mark.parametrize("text", ["/drop", "/drop C-1 C-2", "/drop everything", "/drop ../x"])
async def test_drop_without_one_id_is_not_ours(text):
    bot = _bot()
    await bot._handle_update(_message(text))
    assert _sent(bot) == []
    bot._chat_streaming.assert_called_once()


async def test_drop_from_another_chat_goes_on_to_chat():
    bot = _bot()
    await bot._handle_update(_message("/drop C-0042", chat=99, user=42))
    assert _sent(bot) == [] and bot._chat_streaming.call_count == 1


async def test_drop_of_an_unknown_id_is_answered_not_passed_to_chat():
    bot = _bot()
    await bot._handle_update(_message("/drop C-0042"))  # 404: no such commitment
    assert _sent(bot) == ["No commitment C-0042."]
    bot._chat_streaming.assert_not_called()


async def test_the_route_answers_400_404_and_500_without_a_database(monkeypatch):
    from nous.api import commitment_routes

    async def boom(self, short_id, status, *, close_reason=None):
        raise RuntimeError("database is down")

    monkeypatch.setattr(commitment_routes.CommitmentStore, "mark", boom)
    app = Starlette(routes=build_commitment_routes(database=None, settings=MagicMock(agent_id="a")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        bad = await client.post("/commitments/nope/drop")
        verb = await client.post("/commitments/C-1/explode")
        down = await client.post("/commitments/C-1/done")
    assert (bad.status_code, verb.status_code, down.status_code) == (400, 404, 500)


async def _ledger_tables_exist(db) -> bool:
    from sqlalchemy import text

    async with db.session() as s:
        return bool((await s.execute(text("SELECT to_regclass('brain.commitments') IS NOT NULL"))).scalar())


@pytest.mark.postgres_only
async def test_the_drop_route_cancels_the_row_and_the_watcher_never_sees_it(db):
    from test_promise_gate import MIGRATION_107, NO_MIGRATION_YET

    if not MIGRATION_107.exists():
        pytest.skip(NO_MIGRATION_YET)
    assert await _ledger_tables_exist(db)
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    from nous.brain.commitments import CommitmentStore

    agent = f"pg-drop-{uuid.uuid4().hex[:8]}"
    store = CommitmentStore(db, agent)
    settings = MagicMock(agent_id=agent)
    app = Starlette(routes=build_commitment_routes(database=db, settings=settings))
    try:
        row = await store.create_idempotent(
            source_kind="chat_stream",
            detected_by="gate",
            promise_text=NOV10["reply"][-46:],
            what="I'll remove it (option A) unless you object.",
            due_at=datetime.now(UTC) + timedelta(minutes=1),
            turn_id="t1",
            consent="silence",
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            first = await client.post(f"/commitments/{row.label}/drop", json={"actor": "telegram:42"})
            again = await client.post(f"/commitments/{row.short_id}/drop", json={})
            missing = await client.post("/commitments/C-999999999/drop", json={})
            bad = await client.post("/commitments/nope/drop", json={})
            verb = await client.post(f"/commitments/{row.short_id}/explode", json={})
        assert first.status_code == 200 and first.json() == {
            "short_id": row.label,
            "status": "cancelled",
            "changed": True,
        }
        assert again.status_code == 200 and again.json()["changed"] is False
        assert (missing.status_code, bad.status_code, verb.status_code) == (404, 400, 404)
        later = datetime.now(UTC) + timedelta(hours=2)
        assert await store.list_watchable(now=later) == []  # a dropped row is never pinged
    finally:
        async with db.session() as s:
            await s.execute(text("DELETE FROM brain.commitments WHERE agent_id = :a"), {"a": agent})
            await s.commit()
