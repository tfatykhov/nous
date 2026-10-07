"""F099 Phase 2e, the final fix wave: open work is work that will come back (final review I1), a cancelled lineage
sends no legacy push (final review m1), a cancel cut at its bound says so (final review m4), and a quiet close that
leaves a root waiting on nothing ends it (re-review N1)."""

from __future__ import annotations

import uuid

import pytest
from f099_support import (
    CONT,
    ON,
    RESULT,
    claim,
    env_factory,  # noqa: F401
    finish,
    inbox_rows,
    intention_of,
    make_child,
    make_root,
    make_subtask,
    record,
    set_intention,
)
from test_f099_phase2e_cancel import _cancel, _fire, _row, _schedule_container

from nous.brain import continuation
from nous.brain.continuation import RootLimits
from nous.brain.intentions import IntentionSpec
from nous.handlers.continuation_runner import ArrivalState, make_resolve_intention_executor
from nous.handlers.subtask_worker import SubtaskWorkerPool
from nous.heart.result_inbox import record_subtask_result
from nous.heart.result_reconciler import IntentionClosePass, repair_missing_results

pytestmark = pytest.mark.postgres_only

TG = {"telegram_bot_token": "test-token", "telegram_chat_id": "4242"}
FREE = RootLimits(depth=1, spawns=1, turns=1, tokens=0, stalls=0, spawn_blocked=False, escalate=None)
CONTINUE = {"decision": "continue", "note": "Wait for the next step.", "progress": True, "confidence": 0.8}


# ---- final review I1: a continue counts only continue work as open --------------------------------------------


async def _claimed_with_a_sibling(env, policy: str):
    """An owner root with a result to decide and one open child of ``policy``, spawned by the root's own turn (an
    owner turn may ask for any policy: only an internal lineage is forced to ``continue``)."""
    root = await make_root(env)
    await record(env, root)
    if policy == "continue":
        await make_child(env, root)
    else:
        await env.heart.subtasks.create(
            task="note the snow",
            intention=IntentionSpec(intent="note it", origin_kind="interactive", wake_policy=policy, parent_id=root.id),
        )
    got = await claim(env, root.id)
    assert [i.id for i in got.intentions] == [root.id]
    return got


def _executor(env, got):
    async def limits_of():
        return FREE

    async def open_work_of():  # the runner's `_open_work_of`, on this claim
        async with env.db.session() as s:
            return await continuation.has_open_work(s, env.agent, got)

    state = ArrivalState()
    return state, make_resolve_intention_executor(state, limits_of=limits_of, open_work_of=open_work_of)


@pytest.mark.parametrize("policy", ["remember", "report", "none"])
async def test_a_continue_whose_only_open_sibling_cannot_wake_the_root_is_refused(env_factory, policy):  # noqa: F811
    """Such a child is closed by the quiet writer close or the close pass, neither of which ends the root: accepted,
    the continue would leave a root that nothing wakes, nothing expires and the owner cannot cancel."""
    env = await env_factory(**CONT)
    got = await _claimed_with_a_sibling(env, policy)
    state, execute = _executor(env, got)
    text, is_error = await execute(**CONTINUE)
    assert is_error is True and state.resolution is None
    assert "you chose continue, but nothing is running under this work" in text


async def test_a_continue_whose_open_sibling_is_a_continue_child_is_accepted(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**CONT)
    got = await _claimed_with_a_sibling(env, "continue")
    state, execute = _executor(env, got)
    assert await execute(**CONTINUE) == ("Recorded.", False)
    assert state.resolution.decision == "continue"


# ---- final review m1: a cancelled lineage sends no legacy push ------------------------------------------------


class _CancelsMidRun:
    """A subtask's turn during which the owner cancels the root: the worker's `complete()` is then a no-op."""

    def __init__(self, env, root_id):
        self.env, self.root_id = env, root_id

    async def run_turn(self, **kwargs):
        await _cancel(self.env, self.root_id)
        return RESULT, None, {"input_tokens": 1, "output_tokens": 1}

    async def end_conversation(self, *a, **k):
        return None


@pytest.mark.parametrize("policy", ["remember", "report"])
async def test_a_subtask_whose_root_is_cancelled_mid_run_sends_no_push(env_factory, policy):  # noqa: F811
    env = await env_factory(**CONT, **TG)
    st = await make_subtask(env, policy=policy, notify=True)
    root = await intention_of(env, "subtask", st.id)
    pool = SubtaskWorkerPool(_CancelsMidRun(env, root.id), env.heart, env.settings, http_client=env.http)
    await pool._process_subtask(await env.heart.subtasks.dequeue("worker-0"))
    assert (await env.heart.subtasks.get(st.id)).status == "cancelled"
    env.http.post.assert_not_awaited()


async def test_a_child_of_a_cancelled_root_sends_no_push(env_factory):  # noqa: F811
    env = await env_factory(**CONT, **TG)
    root = await make_root(env, policy="report")
    st = await env.heart.subtasks.create(
        task="note the snow",
        notify=True,
        intention=IntentionSpec(intent="note it", origin_kind="interactive", wake_policy="remember", parent_id=root.id),
    )
    await _cancel(env, root.id)
    await env.pool._notify_telegram(st, result="done")
    env.http.post.assert_not_awaited()


async def test_the_push_of_a_live_lineage_still_goes_out(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**CONT, **TG)
    st = await make_subtask(env, policy="remember", notify=True)
    await env.pool._notify_telegram(st, result="done")
    env.http.post.assert_awaited_once()


async def test_with_continuation_off_the_push_reads_no_intention_and_goes_out(env_factory, monkeypatch):  # noqa: F811  # PIN
    """Prod's flags: no root is ever cancelled, and the push is exactly as before (no intention is read)."""
    env = await env_factory(**ON, **TG)
    st = await make_subtask(env, policy="remember", notify=True)
    await set_intention(env, (await intention_of(env, "subtask", st.id)).id, state="cancelled")

    async def boom(*args, **kwargs):
        raise AssertionError("an intention was read with continuation off")

    monkeypatch.setattr(env.heart.intentions, "get_for_source", boom)
    await env.pool._notify_telegram(st, result="done")
    env.http.post.assert_awaited_once()


# ---- final review m4: a cancel cut at its bound says so -------------------------------------------------------


async def test_a_cancel_that_stops_at_its_root_bound_says_it_is_cut_and_a_repeat_finishes_it(env_factory, monkeypatch):  # noqa: F811
    env = await env_factory(**CONT)
    schedule, container = await _schedule_container(env)
    _st, fire = await _fire(env, schedule)
    monkeypatch.setattr(continuation, "CANCEL_ROOTS_MAX", 1)  # the container alone: its fire is left for later
    first = await _cancel(env, container.id)
    assert first.truncated is True and first.root_ids == (container.id,)
    assert (await _row(env, fire.id)).root_cancelled_at is None
    monkeypatch.setattr(continuation, "CANCEL_ROOTS_MAX", 2)
    again = await _cancel(env, container.id)
    assert (again.already_cancelled, again.truncated) == (True, False)
    assert (await _row(env, fire.id)).root_cancelled_at is not None


async def test_a_cancel_inside_its_bound_is_not_cut(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**CONT)
    schedule, container = await _schedule_container(env)
    await _fire(env, schedule)
    assert (await _cancel(env, container.id)).truncated is False


# ---- re-review N1: a quiet close that leaves a root waiting on nothing ends it ----------------------------------


async def _waiting_on_a_cancelled_child_and_a_remember_one(env):
    """The re-review's sequence: the root's turn spawned a `continue` child C and a `remember` child M and resolved
    `continue` (accepted: C counts). C is cancelled; its close (the repair) sees M open and ends nothing."""
    root = await make_root(env)
    await record(env, root)
    child = await make_child(env, root)
    remember = await env.heart.subtasks.create(
        task="note the snow",
        intention=IntentionSpec(intent="note it", origin_kind="interactive", wake_policy="remember", parent_id=root.id),
    )
    got = await claim(env, root.id)
    async with env.db.session() as s:
        assert await continuation.has_open_work(s, env.agent, got)
        resolution = continuation.Resolution("continue", "waiting on the follow-up", True, 0.6)
        assert await continuation.commit_arrival(
            s, env.agent, got, resolution=resolution, outcome="resolved", settings=env.settings
        )
        await s.commit()
    await env.heart.subtasks.cancel(uuid.UUID(child.source_id))
    await repair_missing_results(env.db, env.heart.result_inbox, env.settings, limit=50)
    assert (await intention_of(env, "subtask", child.source_id)).close_reason == "legacy"
    assert (await _row(env, root.id)).root_expired_at is None  # M is still open
    return root, remember


async def _hanging_reports(env, root):
    return [r for r in await inbox_rows(env) if r.source_kind == "intention_report" and r.intention_id == root.id]


async def _close_by_writer(env, subtask):
    await record_subtask_result(env.heart.result_inbox, await env.heart.subtasks.get(subtask.id), env.settings)


async def _close_by_pass(env, _subtask):
    await IntentionClosePass(env.db, env.settings).run(limit=50)  # the writer never ran: the backstop closes it


@pytest.mark.parametrize("close", [_close_by_writer, _close_by_pass], ids=["writer", "close-pass"])
async def test_the_quiet_close_of_the_last_open_child_ends_a_root_left_waiting_on_nothing(env_factory, close):  # noqa: F811
    env = await env_factory(**CONT)
    root, remember = await _waiting_on_a_cancelled_child_and_a_remember_one(env)
    await finish(env, remember)
    await close(env, remember)
    assert (await intention_of(env, "subtask", remember.id)).state == "closed"
    fresh = await _row(env, root.id)
    assert fresh.root_expired_at is not None  # ended, so it is reported, not left hanging
    (report,) = await _hanging_reports(env, root)
    assert report.msg_type == "REPORT" and report.source_id == uuid.uuid5(continuation._HANGING_NAMESPACE, str(root.id))
    await close(env, remember)  # a second close, or the next tick, reports nothing more
    assert len(await _hanging_reports(env, root)) == 1


async def test_a_quiet_close_with_a_continue_child_still_open_ends_nothing(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    await make_child(env, root)  # still running: it will come back
    remember = await env.heart.subtasks.create(
        task="note the snow",
        intention=IntentionSpec(intent="note it", origin_kind="interactive", wake_policy="remember", parent_id=root.id),
    )
    got = await claim(env, root.id)
    async with env.db.session() as s:
        resolution = continuation.Resolution("continue", "waiting on the follow-up", True, 0.6)
        await continuation.commit_arrival(
            s, env.agent, got, resolution=resolution, outcome="resolved", settings=env.settings
        )
        await s.commit()
    await finish(env, remember)
    await _close_by_writer(env, remember)
    assert (await _row(env, root.id)).root_expired_at is None and await _hanging_reports(env, root) == []
