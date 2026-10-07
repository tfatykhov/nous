"""F099 Phase 2e-2: the unified late-result rule (an expired root reports raw, a cancelled root stamps silently),
and the repair's two cancel cases (carry-over 5, 7 and 8)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from f099_support import (
    CHAN,
    CONT,
    add_arrival,
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
from sqlalchemy import select

from nous.brain import continuation
from nous.brain.continuation import Resolution
from nous.heart.result_reconciler import InboxSubtaskPass, repair_missing_results
from nous.storage.models import Intention

pytestmark = pytest.mark.postgres_only  # CAST(text AS uuid) joins, FOR NO KEY UPDATE

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


async def _reports(env):
    return [r for r in await inbox_rows(env) if r.source_kind == "intention_report"]


async def _gate(env, got):
    async with env.db.session() as s:

        async def plan_outcome_of(_decision_id):
            return None

        return await continuation.gate(s, env.agent, got, settings=env.settings, plan_outcome_of=plan_outcome_of)


async def _commit_gate(env, got, reason):
    resolution, report_text = continuation.gate_inputs(reason, got)
    async with env.db.session() as s:
        done = await continuation.commit_arrival(
            s,
            env.agent,
            got,
            resolution=resolution,
            outcome="resolved",
            gate_reason=reason,
            settings=env.settings,
            report_text=report_text,
        )
        await s.commit()
    return done


async def _sweep(env, **kwargs):
    async with env.db.session() as s:
        out = await continuation.expire_roots(
            s, env.agent, ttl_hours=env.settings.intention_root_ttl_hours, settings=env.settings, **kwargs
        )
        await s.commit()
    return out


# ---- the gate ------------------------------------------------------------------------------------------------


async def test_an_expired_roots_arrival_reports_what_came_back_and_a_cancelled_ones_does_not(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    expired = await make_root(env)
    await record(env, expired, body="Powder: 40 cm.")
    got_expired = await claim(env, expired.id)
    await set_intention(env, expired.id, root_expired_at=datetime.now(UTC))
    cancelled = await make_root(env)
    await record(env, cancelled, body="Powder: 10 cm.")
    got_cancelled = await claim(env, cancelled.id)
    await set_intention(env, cancelled.id, root_cancelled_at=datetime.now(UTC))
    assert await _gate(env, got_expired) == "expired" and await _gate(env, got_cancelled) == "cancelled"

    expired_resolution, expired_text = continuation.gate_inputs("expired", got_expired)
    cancelled_resolution, cancelled_text = continuation.gate_inputs("cancelled", got_cancelled)
    assert (expired_resolution.decision, "Powder: 40 cm." in expired_text) == ("report", True)
    assert (cancelled_resolution.decision, cancelled_text) == ("drop", None)

    await _commit_gate(env, got_expired, "expired")
    await _commit_gate(env, got_cancelled, "cancelled")

    (report,) = await _reports(env)
    assert report.intention_id == expired.id and "Powder: 40 cm." in report.body and "expired" in report.body
    assert (await intention_of(env, "subtask", expired.source_id)).state == "expired"
    assert (await intention_of(env, "subtask", cancelled.source_id)).state == "cancelled"
    # Both arrivals consumed their rows: nothing is left unread for a later sweep to report.
    assert [r.delivered_at is not None for r in await inbox_rows(env) if r.source_kind == "subtask"] == [True, True]


# ---- record_result -------------------------------------------------------------------------------------------


async def test_a_late_result_of_a_cancelled_root_is_stamped_and_never_reported(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="8080")
    root = await make_root(env)
    await set_intention(env, root.id, root_cancelled_at=NOW, state="cancelled", close_reason="cancelled")
    recorded = await record(env, root, body="late")
    assert (recorded.inserted, recorded.reported, recorded.state_after) == (False, False, "cancelled")
    assert await _reports(env) == []
    (twin,) = await inbox_rows(env, uuid.UUID(root.source_id))
    assert twin.delivered_at is not None and twin.delivered_session_id == continuation.SILENT_SESSION_ID
    assert twin.channel is None and twin.session_id is None  # no chat turn can claim it
    again = await record(env, root, body="late")
    assert (again.inserted, again.reported) == (False, False) and len(await inbox_rows(env)) == 1


async def test_a_late_result_under_a_cancelled_marker_is_silent_whatever_the_intention_state(env_factory):  # noqa: F811
    """By marker, not only by state: an intention that is still open (or closed) under a cancelled root."""
    env = await env_factory(**CONT, telegram_chat_id="8080")
    root = await make_root(env)
    child = await make_child(env, root)
    await set_intention(env, root.id, root_cancelled_at=NOW)  # the child is still `pending` under it
    await record(env, child, body="late")
    assert await _reports(env) == []
    (twin,) = await inbox_rows(env, uuid.UUID(child.source_id))
    assert twin.delivered_session_id == continuation.SILENT_SESSION_ID


async def test_a_late_result_of_an_expired_root_is_reported_raw(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await set_intention(env, root.id, root_expired_at=NOW, state="expired", close_reason="expired")
    recorded = await record(env, root, body="late snow")
    assert (recorded.inserted, recorded.reported) == (True, True)
    (report,) = await _reports(env)
    assert report.channel == CHAN and "late snow" in report.body


async def test_a_cancelled_marker_wins_over_an_expired_one(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await set_intention(env, root.id, root_cancelled_at=NOW, root_expired_at=NOW)
    await record(env, root, body="late")
    assert await _reports(env) == []


# ---- the stranded rows ---------------------------------------------------------------------------------------


async def _strand(env, root, *, state):
    """A row a gate arrival left behind on an intention it closed (``state``): unread, intention-keyed."""
    await record(env, root, body="stranded")
    await set_intention(
        env,
        root.id,
        state=state,
        close_reason=state,
        root_cancelled_at=NOW if state == "cancelled" else None,
        root_expired_at=NOW if state == "expired" else None,
    )


async def test_the_sweep_stamps_the_rows_stranded_on_a_cancelled_intention_without_a_report(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await _strand(env, root, state="cancelled")
    await _sweep(env)
    assert await _reports(env) == []
    (row,) = await inbox_rows(env, uuid.UUID(root.source_id))
    assert row.delivered_at is not None and row.delivered_session_id == f"intent-{root.id}"
    await _sweep(env)  # and it stays quiet
    assert await _reports(env) == []


async def test_the_sweep_judges_a_stranded_row_by_the_roots_marker_as_well_as_the_intentions_state(env_factory):  # noqa: F811
    """One rule in three places: by marker. An intention that expired before the owner cancelled its root is still
    under a cancelled root, so its stranded row is stamped and nothing is said."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    await _strand(env, root, state="expired")
    await set_intention(env, root.id, root_cancelled_at=NOW)
    await _sweep(env)
    assert await _reports(env) == []
    (row,) = await inbox_rows(env, uuid.UUID(root.source_id))
    assert row.delivered_at is not None


async def test_the_sweep_reports_the_rows_stranded_on_an_expired_intention_once(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await _strand(env, root, state="expired")
    await _sweep(env)
    (report,) = await _reports(env)
    assert "stranded" in report.body
    await _sweep(env)
    assert len(await _reports(env)) == 1


async def test_a_cancelled_roots_unread_results_are_not_reported_by_the_next_sweep(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    await record(env, root)
    await record(env, child)
    async with env.db.session() as s:
        await continuation.cancel_root(s, env.agent, root.id, reason="t", actor="t")
        await s.commit()
    await _sweep(env, now=datetime.now(UTC) + timedelta(hours=100))
    assert await _reports(env) == []
    assert all(r.delivered_at is not None for r in await inbox_rows(env))


# ---- carry-over 7: the repair and a cancelled root ------------------------------------------------------------


async def test_the_repair_leaves_a_cancelled_roots_finished_work_alone_and_does_not_spin(env_factory):  # noqa: F811
    """No `cancelled` arm in the repair: the owner cancelled this work, so a finished subtask with no row has no
    result to deliver, and nothing selects it again."""
    env = await env_factory(**CONT)
    root = await make_root(env, routed=False)
    child = await make_child(env, root)
    done = await env.heart.subtasks.get(uuid.UUID(child.source_id))
    await finish(env, done)  # completed before the cancel; its writer never ran
    async with env.db.session() as s:
        await continuation.cancel_root(s, env.agent, root.id, reason="t", actor="t")
        await s.commit()
    for _ in range(2):
        assert await repair_missing_results(env.db, env.heart.result_inbox, env.settings, limit=50) == 0
    assert await inbox_rows(env) == []
    assert (await intention_of(env, "subtask", child.source_id)).state == "cancelled"


async def test_the_inbox_pass_settles_a_routable_result_of_a_cancelled_root_silently(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="8080")
    await env.heart.result_inbox.ensure_enabled_at()
    st = await make_subtask(env)  # routed: it has a parent channel and session
    await finish(env, st)  # the worker hook never ran
    async with env.db.session() as s:
        root = await intention_of(env, "subtask", st.id)
        await continuation.cancel_root(s, env.agent, root.id, reason="t", actor="t")
        await s.commit()
    assert await InboxSubtaskPass(env.db, env.heart.result_inbox, env.settings).run(limit=10) == 0
    assert await _reports(env) == []
    (twin,) = await inbox_rows(env, st.id)
    assert twin.delivered_at is not None and twin.delivered_session_id == continuation.SILENT_SESSION_ID
    assert await InboxSubtaskPass(env.db, env.heart.result_inbox, env.settings).run(limit=10) == 0  # not selected again


# ---- carry-over 8: a lineage left hanging --------------------------------------------------------------------


async def _hang(env, *, decision="continue", with_arrival=True):
    """A root a continuation resolved with `decision` (it waits on `child`), whose child then gets cancelled."""
    root = await make_root(env)
    child = await make_child(env, root)
    await set_intention(env, root.id, state="closed", close_reason="resolved", closed_at=NOW)
    if with_arrival:
        await add_arrival(env, root.id, 1, decision=decision, progress=True)
    return root, child


async def _cancel_source(env, child):
    await env.heart.subtasks.cancel(uuid.UUID(child.source_id))
    return await repair_missing_results(env.db, env.heart.result_inbox, env.settings, limit=50)


async def test_the_last_child_of_a_lineage_waiting_on_it_is_cancelled_and_the_root_is_closed_and_reported(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="8080")
    root, child = await _hang(env)
    await _cancel_source(env, child)
    fresh_child = await intention_of(env, "subtask", child.source_id)
    assert (fresh_child.state, fresh_child.close_reason) == ("closed", "legacy")
    async with env.db.session() as s:
        fresh_root = (await s.execute(select(Intention).where(Intention.id == root.id))).scalar_one()
    assert fresh_root.root_expired_at is not None
    (report,) = await _reports(env)
    assert report.msg_type == "REPORT" and root.intent in report.body and report.channel == CHAN
    await _cancel_source(env, child)  # idempotent
    assert len(await _reports(env)) == 1
    recorded = await record(env, root, body="late")  # a late result is now reported raw, never silently reopened
    assert recorded.reported is True


@pytest.mark.parametrize("case", ["drop", "report", "no_arrival", "other_child_open"])
async def test_a_cancelled_child_ends_nothing_the_lineage_is_not_waiting_on(env_factory, case):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="8080")
    root, child = await _hang(
        env,
        decision="drop" if case == "drop" else "report" if case == "report" else "continue",
        with_arrival=case != "no_arrival",
    )
    if case == "other_child_open":
        await make_child(env, root)  # something else is still running
    await _cancel_source(env, child)
    async with env.db.session() as s:
        fresh_root = (await s.execute(select(Intention).where(Intention.id == root.id))).scalar_one()
    assert fresh_root.root_expired_at is None and await _reports(env) == []


async def test_a_cancelled_child_under_a_cancelled_root_reports_nothing(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="8080")
    root, child = await _hang(env)
    await set_intention(env, root.id, root_cancelled_at=NOW)
    await _cancel_source(env, child)
    assert (await intention_of(env, "subtask", child.source_id)).close_reason == "cancelled"
    assert await _reports(env) == []


async def test_a_claim_gate_commit_chain_still_closes_a_resolved_arrival(env_factory):  # noqa: F811  # PIN
    """The commit path of an ordinary decision is untouched by the rule: a `drop` closes `resolved`, no report."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    got = await claim(env, root.id)
    async with env.db.session() as s:
        done = await continuation.commit_arrival(
            s,
            env.agent,
            got,
            resolution=Resolution("drop", "n/a", False, 0.5),
            outcome="resolved",
            settings=env.settings,
        )
        await s.commit()
    assert done is not None and await _reports(env) == []


# ---- prod parity (R14) ---------------------------------------------------------------------------------------


async def test_prods_flags_never_reach_the_late_result_rule(env_factory, monkeypatch):  # noqa: F811  # PIN
    """Prod runs the inbox, intentions and result memory ON with continuation OFF. A subtask that finishes under a
    cancelled root is written as F098 writes it today, and no path of the rule runs: the writer, the inbox pass and
    the repair never reach record_result, the stranded settle, close_cancelled_source or end_hanging_root."""
    from nous.heart.result_inbox import record_subtask_result

    env = await env_factory(result_inbox_enabled=True, intentions_enabled=True, result_memory_enabled=True)
    reached: list[str] = []
    for name in ("record_result", "_settle_stranded_rows", "close_cancelled_source", "end_hanging_root"):

        async def recorder(*_args, _name=name, **_kwargs):
            reached.append(_name)
            raise AssertionError(f"{_name} ran on prod's flags")

        monkeypatch.setattr(continuation, name, recorder)
    st = await make_subtask(env)
    root = await intention_of(env, "subtask", st.id)
    await set_intention(env, root.id, root_cancelled_at=NOW)
    done = await finish(env, st)
    assert await record_subtask_result(env.heart.result_inbox, done, env.settings) is True
    await InboxSubtaskPass(env.db, env.heart.result_inbox, env.settings).run(limit=10)
    assert await repair_missing_results(env.db, env.heart.result_inbox, env.settings, limit=50) == 0
    assert reached == []
    (row,) = await inbox_rows(env, st.id)
    assert (row.channel, row.session_id, row.delivered_at) == (CHAN, "S1", None)  # F098's keyed row, as today
    assert await _reports(env) == []
