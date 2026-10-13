"""F099 Phase 3c-3: the metrics (contract 4.9 and 5.11)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from f099_dashboard_support import put_arrivals, put_decision, put_inbox, put_intention, put_proposal, utcnow
from f099_support import CONT, env_factory  # noqa: F401

from nous.api import intention_dashboard
from nous.brain import continuation

pytestmark = pytest.mark.postgres_only


async def _metrics(env, **kw) -> dict:
    async with env.db.session() as s:
        data = await intention_dashboard.get_intentions_data(s, env.agent, settings=env.settings, **kw)
    return data["metrics"]


def _grace(env) -> int:
    return env.settings.continuation_lease_seconds + env.settings.continuation_max_wait_seconds + 60


async def test_latency_is_nearest_rank_over_the_window(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await put_intention(env)
    base = utcnow() - timedelta(minutes=10)
    latencies = [20, 25, 30, 35, 38, 40, 50, 60, 66, 123]  # the Phase 2 shape on prod
    specs = []
    for seconds in latencies:
        row = await put_inbox(env, root, created_at=base - timedelta(seconds=seconds))
        specs.append({"inbox_ids": [row.id], "decided_at": base})
    specs.append({"decided_at": base})  # no inbox rows: not in the sample
    specs.append(
        {
            "inbox_ids": [(await put_inbox(env, root, created_at=base - timedelta(seconds=5000))).id],
            "decided_at": utcnow() - timedelta(days=40),
        }
    )  # outside the window
    await put_arrivals(env, root, specs)
    got = (await _metrics(env))["result_to_decision_latency_s"]
    # nearest rank: ceil(0.5 * 10) = 5th value, ceil(0.9 * 10) = 9th. Interpolation would say 39.0 and 71.7.
    assert got == {"p50": 38.0, "p90": 66.0, "max": 123.0, "n": 10}


async def test_latency_of_nothing_is_null_not_zero(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    assert (await _metrics(env))["result_to_decision_latency_s"] == {"p50": None, "p90": None, "max": None, "n": 0}


async def test_results_lost_four_cases_and_results_waiting(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    old, young = _grace(env) + 60, _grace(env) - 60

    async def root_with_row(*, cancelled=False, state="pending", **row):
        root = await put_intention(env, state=state, **({"root_cancelled_at": utcnow()} if cancelled else {}))
        await put_inbox(env, root, **row)
        return root

    await root_with_row(
        cancelled=True, state="cancelled", created_ago=old, delivered=True, delivered_session_id="intent-x"
    )  # delivered: not lost
    assert (await _metrics(env))["results_lost"] == 0
    await root_with_row(cancelled=True, state="cancelled", created_ago=young)  # inside the grace
    assert (await _metrics(env))["results_lost"] == 0
    await root_with_row(
        cancelled=True,
        state="cancelled",
        created_ago=old,
        delivered=True,
        delivered_session_id=continuation.SILENT_SESSION_ID,
    )  # closed, not lost
    assert (await _metrics(env))["results_lost"] == 0
    await root_with_row(cancelled=True, state="cancelled", created_ago=old)  # nobody will ever read it
    m = await _metrics(env)
    assert m["results_lost"] == 1 and m["results_waiting"] == 0
    # an old undelivered row on an open root still waiting for its turn is a stall indicator, not a loss
    await root_with_row(state="result_ready", created_ago=old)
    m = await _metrics(env)
    assert (m["results_lost"], m["results_waiting"]) == (1, 1)
    # a pending intention's row will be claimed: neither number
    await root_with_row(state="pending", created_ago=old)
    m = await _metrics(env)
    assert (m["results_lost"], m["results_waiting"]) == (1, 1)
    # a waiting state under a cancelled root is lost, never waiting: the two numbers are disjoint (C4)
    await root_with_row(cancelled=True, state="result_ready", created_ago=old)
    m = await _metrics(env)
    assert (m["results_lost"], m["results_waiting"]) == (2, 1)
    assert m["results_lost_grace_s"] == _grace(env)


async def test_owner_rows_unpushed(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await put_intention(env)
    hour = timedelta(hours=1)
    kw = dict(channel="telegram:1", source_kind="intention_report", msg_type="REPORT")
    await put_inbox(env, root, push_after=utcnow() - 2 * hour, **kw)  # never shown: counted
    await put_inbox(env, root, push_after=utcnow() - 2 * hour, pushed_at=utcnow(), **kw)  # pushed
    await put_inbox(env, root, push_after=utcnow() - timedelta(minutes=10), **kw)  # still inside the hour
    await put_inbox(env, root, push_after=utcnow() - 2 * hour, delivered=True, **kw)  # delivered
    await put_inbox(env, root, push_after=utcnow() - 2 * hour, created_at=utcnow() - timedelta(days=40), **kw)  # C7
    assert (await _metrics(env))["owner_rows_unpushed"] == 1


async def test_arrivals_by_decision_outcome_separates_gate_arrivals(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await put_intention(env)
    await put_arrivals(
        env,
        root,
        [
            {"decision": "continue"},
            {"decision": "continue"},
            {"decision": "report"},
            {"decision": "drop", "outcome": "fallback_report"},
            {"decision": "drop", "gate_reason": "budget_turns"},  # a gate arrival's synthetic drop
            {"decision": "drop", "decided_at": utcnow() - timedelta(days=40)},  # outside the window
        ],
    )
    rows = (await _metrics(env))["arrivals_by_decision_outcome"]
    assert {(r["decision"], r["outcome"], r["gated"]): r["count"] for r in rows} == {
        ("continue", "resolved", False): 2,
        ("report", "resolved", False): 1,
        ("drop", "fallback_report", False): 1,
        ("drop", "resolved", True): 1,
    }


async def test_proposals_by_state_leaves_staged_out(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await put_intention(env)
    for state in ("staged", "pending", "approved", "executed", "executed", "failed", "rejected"):
        await put_proposal(env, root, state=state)
    got = (await _metrics(env))["proposals_by_state"]
    assert got["counts"] == {"pending": 1, "approved": 1, "executed": 2, "failed": 1, "rejected": 1}
    assert got["approved_total"] == 4  # approved + executing + executed + failed


async def test_escalations_by_cause_zero_fills_every_gate_reason(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await put_intention(env)
    await put_intention(env, root_expired_at=utcnow() - timedelta(hours=1), state="expired")
    await put_intention(env, root_expired_at=utcnow() - timedelta(days=40), state="expired")
    await put_arrivals(
        env, root, [{"gate_reason": "budget_stall"}, {"gate_reason": "budget_stall"}, {"gate_reason": "past_deadline"}]
    )
    got = (await _metrics(env))["escalations_by_cause"]
    assert set(got["by_reason"]) == set(continuation.GATE_REASONS)
    assert got["by_reason"]["budget_stall"] == 2 and got["by_reason"]["past_deadline"] == 1
    assert got["by_reason"]["cancelled"] == 0 and got["expired_roots"] == 1


async def test_stalls_reproduce_the_phase_2_claimed_versus_credited_shape(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await put_intention(env)
    specs = (
        [{"progress_claimed": True, "progress": True}] * 28  # claimed and credited
        + [{"progress_claimed": True, "progress": False}] * 12  # claimed, not credited: 40 claimed in all
        + [{"progress_claimed": False, "progress": False}] * 3  # verified no progress, no claim
        + [{"gate_reason": "budget_stall", "progress": None}] * 2  # stall escalations
        + [{"progress_claimed": None, "progress": None}]  # no verdict: counted nowhere
    )
    await put_arrivals(env, root, specs)
    assert (await _metrics(env))["stalls"] == {
        "verified_no_progress": 15,
        "claimed_not_credited": 12,
        "stall_escalations": 2,
        "progress_claimed": 40,
        "progress_credited": 28,
    }


async def test_arrivals_ungradable_counts_arrivals_without_a_decision_record(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await put_intention(env)
    record = await put_decision(env)
    await put_arrivals(
        env,
        root,
        [
            {},
            {"gate_reason": "budget_turns"},
            {"decision_record_id": record},
            {"decided_at": utcnow() - timedelta(days=40)},
        ],
    )
    assert (await _metrics(env))["arrivals_ungradable"] == 2
