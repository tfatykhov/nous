"""F099 Phase 2c-1: the fenced commit of an arrival (spec 4.5.6, contract 4.14) and quiet hours."""

from __future__ import annotations

import dataclasses
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from f099_support import (
    CHAN,
    CONT,
    RESULT,
    age,
    claim,
    env_factory,  # noqa: F401
    inbox_rows,
    intention_of,
    make_child,
    make_root,
    record,
    set_intention,
)
from sqlalchemy import select, text

from nous.brain import continuation
from nous.brain.continuation import Resolution
from nous.heartbeat.quiet_hours import in_quiet_hours, quiet_hours_end
from nous.storage.models import Decision, DecisionTag, IntentionArrival

DAY = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)  # outside the default quiet hours (23 to 8)
NIGHT = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)


def _settings(start: int, end: int):
    return SimpleNamespace(heartbeat_quiet_start=start, heartbeat_quiet_end=end)


@pytest.mark.parametrize(
    ("start", "end", "hour", "expected"),
    [
        (9, 17, 12, True),
        (9, 17, 20, False),
        (9, 17, 17, False),
        (23, 8, 2, True),
        (23, 8, 23, True),
        (23, 8, 15, False),
        (5, 5, 5, False),
    ],
)
def test_in_quiet_hours_follows_the_heartbeats_rule(start, end, hour, expected):
    assert in_quiet_hours(_settings(start, end), datetime(2026, 10, 6, hour, 30, tzinfo=UTC)) is expected


@pytest.mark.parametrize(
    ("start", "end", "now", "expected"),
    [
        (23, 8, datetime(2026, 10, 6, 2, 30, tzinfo=UTC), datetime(2026, 10, 6, 8, 0, tzinfo=UTC)),
        (23, 8, datetime(2026, 10, 6, 23, 30, tzinfo=UTC), datetime(2026, 10, 7, 8, 0, tzinfo=UTC)),
        (9, 17, datetime(2026, 10, 6, 12, 0, tzinfo=UTC), datetime(2026, 10, 6, 17, 0, tzinfo=UTC)),
        (23, 8, datetime(2026, 10, 6, 12, 0, tzinfo=UTC), datetime(2026, 10, 6, 12, 0, tzinfo=UTC)),  # not quiet: now
    ],
)
def test_quiet_hours_end_is_the_next_end_of_the_window(start, end, now, expected):
    assert quiet_hours_end(_settings(start, end), now) == expected


def test_push_after_is_now_by_day_and_the_end_of_the_quiet_hours_by_night():
    settings = _settings(23, 8)
    assert continuation.push_after_for(settings, DAY) == DAY
    assert continuation.push_after_for(settings, NIGHT) == datetime(2026, 10, 6, 8, 0, tzinfo=UTC)


@pytest.mark.parametrize("start", range(24))
def test_the_heartbeat_runner_keeps_its_quiet_hours_rule_for_every_hour(start):  # PIN
    """The runner's answer for every (start, end, hour), read through its own module's ``datetime`` (what
    ``tests/test_heartbeat.py`` patches), equals the rule its method had before the extraction."""
    from nous.heartbeat.runner import HeartbeatRunner

    runner = HeartbeatRunner.__new__(HeartbeatRunner)
    with patch("nous.heartbeat.runner.datetime") as mock_dt:
        for end in range(24):
            runner._settings = _settings(start, end)
            for hour in range(24):
                mock_dt.now.return_value = datetime(2026, 10, 6, hour, 30, tzinfo=UTC)
                # The expected value is a formula, not a literal (a deliberate exception to the pin rule): it
                # is the removed method body, transcribed, so it cannot drift with the code it pins.
                before = start <= hour < end if start <= end else hour >= start or hour < end
                assert runner._in_quiet_hours() is before, (start, end, hour)


def _r(decision: str, note: str = "Because the snow report changed the plan.", *, progress=True, confidence=0.8):
    return Resolution(decision, note, progress, confidence)


async def _claimed(env, *, root=None, body=RESULT):
    root = root or await make_root(env)
    await record(env, root, body=body)
    return root, await claim(env, root.id)


async def _commit(env, got, resolution, *, outcome="resolved", **kwargs):
    async with env.db.session() as s:
        done = await continuation.commit_arrival(
            s, env.agent, got, resolution=resolution, outcome=outcome, settings=env.settings, **kwargs
        )
        if done is not None:
            await s.commit()
    return done


async def _arrivals(env, root_id):
    async with env.db.session() as s:
        query = select(IntentionArrival).where(
            IntentionArrival.agent_id == env.agent, IntentionArrival.root_id == root_id
        )
        return list((await s.execute(query.order_by(IntentionArrival.n))).scalars().all())


async def _owner_rows(env):
    return [r for r in await inbox_rows(env) if r.source_kind == "intention_report"]


@pytest.mark.postgres_only
async def test_continue_after_spawning_closes_resolved_and_records_the_arrival(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    await make_child(env, got.deepest)  # the turn spawned under the claimed intention
    done = await _commit(env, got, _r("continue"), tokens=(1200, 340))
    (arrival,) = await _arrivals(env, root.id)
    assert (done.n, arrival.n) == (1, 1)
    assert (arrival.decision, arrival.outcome, arrival.gate_reason) == ("continue", "resolved", None)
    assert (arrival.progress_claimed, arrival.progress) == (True, True)
    assert arrival.confidence == pytest.approx(0.8)  # a REAL column
    assert (arrival.tokens_in, arrival.tokens_out) == (1200, 340)
    assert arrival.id == done.arrival_id and arrival.claim_token == got.claim_token and arrival.decided_at is not None
    assert list(arrival.intention_ids) == [root.id] and list(arrival.inbox_ids) == [r.id for r in got.inbox_rows]
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.close_reason, fresh.claim_token, fresh.claimed_at, fresh.attempts) == (
        "closed",
        "resolved",
        None,
        None,
        0,
    )
    assert done.next_states == {root.id: "closed"} and done.report_ids == ()


# ---- has_open_work: what a continue or revise needs running under the root (final review I1) ----------------


async def _open_work(env, got) -> bool:
    async with env.db.session() as s:
        return await continuation.has_open_work(s, env.agent, got)


@pytest.mark.postgres_only
async def test_the_claimed_intentions_are_not_open_work(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    _root, got = await _claimed(env)  # the root alone, deciding: an open state, but the claim's own
    assert await _open_work(env, got) is False


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("state", "expected"),
    [(s, True) for s in continuation.OPEN_STATES] + [(s, False) for s in ("closed", "cancelled", "expired")],
)
async def test_an_open_intention_of_the_root_besides_the_claimed_ones_is_open_work(env_factory, state, expected):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    first, second = await make_child(env, root), await make_child(env, root)
    await set_intention(env, root.id, state="closed", close_reason="resolved")  # only the sibling can count
    await record(env, first)
    got = await claim(env, root.id)
    assert [i.id for i in got.intentions] == [first.id]
    await set_intention(env, second.id, state=state)
    assert await _open_work(env, got) is expected


@pytest.mark.postgres_only
async def test_a_child_spawned_since_the_claim_is_open_work_only_while_open(env_factory):  # noqa: F811
    """Re-review N1: a child the turn spawned counts while it runs; once closed (an inline spawn closes within the
    turn) nothing is left running, whenever it was created."""
    env = await env_factory(**CONT)
    _root, got = await _claimed(env)
    child = await make_child(env, got.deepest)  # the turn spawned it
    assert await _open_work(env, got) is True
    await set_intention(env, child.id, state="closed", close_reason="delivered")  # it already finished
    assert await _open_work(env, got) is False


@pytest.mark.postgres_only
async def test_a_child_closed_before_the_claim_is_not_open_work(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    earlier = await make_child(env, root)  # an earlier arrival's spawn, finished before this claim
    await set_intention(env, earlier.id, state="closed", close_reason="delivered")
    _root, got = await _claimed(env, root=root)
    assert await _open_work(env, got) is False


@pytest.mark.postgres_only
async def test_another_roots_open_intentions_are_not_open_work(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    other = await make_root(env)
    await make_child(env, other)  # another root, with open work of its own
    _root, got = await _claimed(env)
    assert await _open_work(env, got) is False


@pytest.mark.postgres_only
async def test_delivery_is_stamped_only_by_the_commit_on_the_rows_the_turn_was_shown(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    (before,) = await inbox_rows(env, UUID(root.source_id))
    assert before.delivered_at is None  # the claim read it and stamped nothing
    await _commit(env, got, _r("drop"))
    (after,) = await inbox_rows(env, UUID(root.source_id))
    assert after.delivered_at is not None and after.delivered_session_id == f"intent-{root.id}"


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("decision", "claimed", "spawned", "memory", "expected"),
    [
        ("continue", True, True, False, True),
        ("continue", True, False, False, False),  # claimed, nothing done: stored false
        ("revise", True, False, False, True),  # a plan change is progress
        ("continue", True, False, True, True),  # memory written
        ("continue", False, True, False, False),  # not claimed
    ],
)
async def test_progress_is_the_claim_checked(env_factory, decision, claimed, spawned, memory, expected):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    if spawned:
        await make_child(env, got.deepest)
    await _commit(env, got, _r(decision, progress=claimed), wrote_memory=memory)
    (arrival,) = await _arrivals(env, root.id)
    assert (arrival.progress_claimed, arrival.progress) == (claimed, expected)


@pytest.mark.postgres_only
@pytest.mark.parametrize("spawns_again", [False, True])
async def test_a_child_an_earlier_arrival_spawned_does_not_count_as_progress(env_factory, spawns_again):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    await make_child(env, got.deepest)  # arrival 1 spawns under the root
    await record(env, root, generation=1, body="a later result")  # and a late row sends the root back
    first = await _commit(env, got, _r("continue"))
    assert first.next_states == {root.id: "result_ready"}
    await age(env, root.id)
    again = await claim(env, root.id)  # the same intention, claimed a second time
    if spawns_again:
        await make_child(env, again.deepest)
    await _commit(env, again, _r("continue"))
    earlier, second = await _arrivals(env, root.id)
    assert earlier.progress is True
    assert (second.progress_claimed, second.progress) == (True, spawns_again)


@pytest.mark.postgres_only
async def test_ask_waits_for_the_owner_and_writes_a_question_row(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    done = await _commit(env, got, _r("ask", "Shall I book the Friday slot?"), now=DAY)
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.close_reason, fresh.claim_token) == ("awaiting_owner", None, None)
    (question,) = await _owner_rows(env)
    assert (question.msg_type, question.channel, question.intention_id, question.arrival_id) == (
        "QUESTION",
        CHAN,
        root.id,
        done.arrival_id,
    )
    assert "Friday slot" in question.body and question.push_after == DAY and question.pushed_at is None
    assert done.report_ids == (question.source_id,) and done.next_states == {root.id: "awaiting_owner"}


@pytest.mark.postgres_only
async def test_report_writes_the_note_as_a_report_and_drop_writes_nothing(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    other, other_claim = await _claimed(env)
    await _commit(env, got, _r("report", "The snow is deep; drive up tomorrow."), now=DAY)
    await _commit(env, other_claim, _r("drop", "No longer needed."))
    (report,) = await _owner_rows(env)
    assert (report.msg_type, report.channel, report.intention_id) == ("REPORT", CHAN, root.id)
    assert report.body == "The snow is deep; drive up tomorrow." and report.title.startswith("Update:")
    assert (await intention_of(env, "subtask", other.source_id)).close_reason == "resolved"


@pytest.mark.postgres_only
async def test_quiet_hours_defer_only_the_push(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    _, got = await _claimed(env)
    await _commit(env, got, _r("report", "News."), now=NIGHT)
    (report,) = await _owner_rows(env)  # written at once: chat sees it at night
    assert report.push_after == datetime(2026, 10, 6, 8, 0, tzinfo=UTC) and report.pushed_at is None


@pytest.mark.postgres_only
async def test_a_descendants_report_goes_to_the_roots_origin_channel(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="1111")
    root = await make_root(env)  # origin channel CHAN
    child = await make_child(env, root)  # its own origin channel is NULL, as every continuation spawn's is
    await record(env, child)
    got = await claim(env, root.id)
    await _commit(env, got, _r("report", "Done."))
    (report,) = await _owner_rows(env)
    assert report.channel == CHAN  # not the default chat telegram:1111


@pytest.mark.postgres_only
async def test_rows_that_arrived_after_the_claim_send_the_intention_back(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    await record(env, root, generation=1, body="a later result")  # inserted and held while deciding
    done = await _commit(env, got, _r("continue", progress=False))
    assert done.next_states == {root.id: "result_ready"}
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.close_reason, fresh.claim_token, fresh.result_at is not None) == (
        "result_ready",
        None,
        None,
        True,
    )
    rows = {r.body: r for r in await inbox_rows(env, UUID(root.source_id))}
    assert rows[RESULT].delivered_at is not None and rows["a later result"].delivered_at is None


@pytest.mark.postgres_only
async def test_an_ask_holds_late_rows(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    await record(env, root, generation=1, body="a later result")
    await _commit(env, got, _r("ask", "Which one?"))
    fresh = await intention_of(env, "subtask", root.source_id)
    assert fresh.state == "awaiting_owner"  # the chain must not move on while its question is open
    rows = {r.body: r for r in await inbox_rows(env, UUID(root.source_id))}
    assert rows["a later result"].delivered_at is None


@pytest.mark.postgres_only
async def test_a_batch_is_one_arrival(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    await record(env, root)
    await record(env, child)
    got = await claim(env, root.id)
    done = await _commit(env, got, _r("drop", progress=False))
    (arrival,) = await _arrivals(env, root.id)
    assert set(arrival.intention_ids) == {root.id, child.id} and len(arrival.inbox_ids) == 2
    assert done.next_states == {root.id: "closed", child.id: "closed"}


@pytest.mark.postgres_only
async def test_a_stale_token_cannot_commit_and_writes_nothing(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    stale = dataclasses.replace(got, claim_token=uuid.uuid4())
    assert await _commit(env, stale, _r("report", "Late news.")) is None
    assert await _arrivals(env, root.id) == [] and await _owner_rows(env) == []
    (row,) = await inbox_rows(env, UUID(root.source_id))
    assert row.delivered_at is None
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.claim_token) == ("deciding", got.claim_token)  # the live claim is untouched


@pytest.mark.postgres_only
async def test_a_cancelled_root_commits_a_drop_and_cancels_the_intention(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    await set_intention(env, root.id, root_cancelled_at=datetime.now(UTC))
    resolution, report = continuation.gate_inputs("cancelled", got)
    done = await _commit(env, got, resolution, gate_reason="cancelled", report_text=report)
    (arrival,) = await _arrivals(env, root.id)
    assert (arrival.decision, arrival.gate_reason) == ("drop", "cancelled")
    assert (arrival.outcome, arrival.progress) == ("resolved", None)
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.close_reason) == ("cancelled", "cancelled") and done.report_ids == ()
    assert await _owner_rows(env) == []


@pytest.mark.postgres_only
async def test_an_escalation_reports_the_raw_results_without_a_model(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env, body="40 cm overnight")
    resolution, report = continuation.gate_inputs("past_deadline", got)
    done = await _commit(env, got, resolution, gate_reason="past_deadline", report_text=report, now=DAY)
    (arrival,) = await _arrivals(env, root.id)
    assert (arrival.decision, arrival.gate_reason, arrival.outcome) == ("report", "past_deadline", "resolved")
    (row,) = await _owner_rows(env)
    assert "after its deadline" in row.body and "40 cm overnight" in row.body and done.report_ids == (row.source_id,)
    assert (await intention_of(env, "subtask", root.source_id)).close_reason == "resolved"


@pytest.mark.postgres_only
async def test_a_fallback_closes_as_fallback_report_with_the_turns_text(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    await _commit(
        env,
        got,
        _r("report", "turn text", progress=False, confidence=0.3),
        outcome="fallback_report",
        report_text="turn text",
    )
    (arrival,) = await _arrivals(env, root.id)
    assert (arrival.outcome, arrival.progress) == ("fallback_report", False)
    assert (await intention_of(env, "subtask", root.source_id)).close_reason == "fallback_report"
    (row,) = await _owner_rows(env)
    assert row.body == "turn text"


@pytest.mark.postgres_only
@pytest.mark.parametrize(("outcome", "decision"), [("fallback_report", "continue"), ("failed_report", "drop")])
async def test_a_fallback_reports_whatever_its_decision(env_factory, outcome, decision):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    resolution = _r(decision, "what the turn said", progress=False)
    done = await _commit(env, got, resolution, outcome=outcome, report_text="the raw results")
    (row,) = await _owner_rows(env)  # contract 4.14 item 5: both fallbacks write a REPORT, unconditionally
    assert (row.msg_type, row.body, row.channel, row.intention_id) == ("REPORT", "the raw results", CHAN, root.id)
    assert done.report_ids == (row.source_id,)
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.close_reason) == ("closed", outcome)


@pytest.mark.postgres_only
@pytest.mark.parametrize("note", ["done", "The feed encountered an error and the run was cut short.", "ok"])
async def test_a_terse_note_still_records_its_decision(env_factory, note):  # noqa: F811
    from nous.brain import Brain

    env = await env_factory(**CONT)
    brain = Brain(database=env.db, settings=env.settings)
    root, got = await _claimed(env)
    done = await _commit(env, got, _r("continue", note), brain=brain)
    assert done.decision_record_id is not None
    async with env.db.session() as s:
        decision = await s.get(Decision, done.decision_record_id)
        tags = (await s.execute(select(DecisionTag.tag).where(DecisionTag.decision_id == decision.id))).scalars().all()
    assert (decision.category, decision.stakes, decision.session_id) == ("process", "low", f"intent-{root.id}")
    assert set(tags) == {"f099", "continue"} and str(root.id) in decision.context
    (arrival,) = await _arrivals(env, root.id)
    assert arrival.decision_record_id == decision.id


@pytest.mark.postgres_only
async def test_a_failing_brain_does_not_abort_the_commit(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    brain = SimpleNamespace(record=AsyncMock(side_effect=RuntimeError("the decision store is down")))
    root, got = await _claimed(env)
    done = await _commit(env, got, _r("drop"), brain=brain)
    assert done is not None and done.decision_record_id is None
    assert (await intention_of(env, "subtask", root.source_id)).state == "closed"


@pytest.mark.postgres_only
async def test_a_slow_brain_statement_is_cut_off_and_the_commit_still_lands(env_factory, monkeypatch):  # noqa: F811
    env = await env_factory(**CONT)
    monkeypatch.setattr(continuation, "BRAIN_RECORD_STATEMENT_TIMEOUT_MS", 100)

    async def record_slowly(_input, session):
        await session.execute(text("SELECT pg_sleep(1)"))  # a Brain statement still running past the bound
        return SimpleNamespace(id=uuid.uuid4())

    root, got = await _claimed(env)
    started = time.monotonic()
    done = await _commit(env, got, _r("continue"), brain=SimpleNamespace(record=record_slowly))
    assert time.monotonic() - started < 1  # cut off at the bound, not after the sleep
    assert done is not None and done.decision_record_id is None
    assert (await intention_of(env, "subtask", root.source_id)).state == "closed"
    (arrival,) = await _arrivals(env, root.id)
    assert arrival.decision_record_id is None


@pytest.mark.postgres_only
async def test_the_brain_statement_bound_does_not_outlive_the_record(env_factory, monkeypatch):  # noqa: F811
    env = await env_factory(**CONT)
    monkeypatch.setattr(continuation, "BRAIN_RECORD_STATEMENT_TIMEOUT_MS", 250)
    seen: list[str] = []

    async def record(_input, session):
        seen.append((await session.execute(text("SHOW statement_timeout"))).scalar_one())
        return SimpleNamespace(id=uuid.uuid4())

    _, got = await _claimed(env)
    async with env.db.session() as s:
        before = (await s.execute(text("SHOW statement_timeout"))).scalar_one()
        done = await continuation.commit_arrival(
            s,
            env.agent,
            got,
            resolution=_r("drop"),
            outcome="resolved",
            settings=env.settings,
            brain=SimpleNamespace(record=record),
        )
        after = (await s.execute(text("SHOW statement_timeout"))).scalar_one()
        await s.commit()
    assert seen == ["250ms"] and done.decision_record_id is not None
    assert after == before  # the rest of the caller's transaction keeps its own bound


@pytest.mark.postgres_only
async def test_an_arrival_with_no_owner_channel_still_commits(env_factory, caplog):  # noqa: F811
    env = await env_factory(**CONT)  # no default chat either
    root = await make_root(env, routed=False)
    _, got = await _claimed(env, root=root)
    done = await _commit(env, got, _r("report", "News."))
    assert done is not None and done.report_ids == () and await _owner_rows(env) == []
    assert "no owner channel" in caplog.text


@pytest.mark.postgres_only
async def test_an_ask_with_nowhere_to_ask_is_refused_and_writes_nothing(env_factory):  # noqa: F811
    """No origin channel and no default chat: committed, the ask would wait in awaiting_owner on a QUESTION that
    was never written, which reads as answered, and the next sweep would wake it to a turn with no rows."""
    env = await env_factory(**CONT, telegram_chat_id="")
    root = await make_root(env, routed=False)
    _, got = await _claimed(env, root=root)
    with pytest.raises(ValueError, match="owner channel"):
        await _commit(env, got, _r("ask", "Shall I book the Friday slot?"))
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.claim_token) == ("deciding", got.claim_token)
    assert await _arrivals(env, root.id) == [] and await _owner_rows(env) == []
    shown = await inbox_rows(env, UUID(root.source_id))
    assert [r.delivered_at for r in shown] == [None]


@pytest.mark.postgres_only
async def test_an_unknown_decision_is_refused_before_anything_is_written(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    with pytest.raises(ValueError, match="decision"):
        await _commit(env, got, _r("approve"))
    assert (await intention_of(env, "subtask", root.source_id)).state == "deciding"


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("decision", "outcome", "match"),
    [
        ("continue", "resolve", "outcome"),  # a typo would otherwise close as 'resolved'
        ("report", "fallback", "outcome"),
        ("ask", "fallback_report", "fallback"),  # a fallback asks nothing: no QUESTION, nothing to wait for
    ],
)
async def test_an_unknown_outcome_or_a_fallback_that_asks_is_refused(env_factory, decision, outcome, match):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    with pytest.raises(ValueError, match=match):
        await _commit(env, got, _r(decision), outcome=outcome)
    assert (await intention_of(env, "subtask", root.source_id)).state == "deciding"
    assert await _arrivals(env, root.id) == [] and await _owner_rows(env) == []
