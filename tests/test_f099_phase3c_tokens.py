"""F099 Phase 3c-4: token_spend (contract 4.9.8, S4, C2, C3)."""

from __future__ import annotations

import pytest
from f099_dashboard_support import put_arrivals, put_context_log, put_intention, put_model_call
from f099_support import CONT, env_factory, set_intention  # noqa: F401

from nous.api import intention_dashboard

pytestmark = pytest.mark.postgres_only


async def _spend(env) -> dict:
    async with env.db.session() as s:
        return (await intention_dashboard.get_intentions_data(s, env.agent, settings=env.settings))["metrics"][
            "token_spend"
        ]


async def test_the_two_columns_and_the_gap_are_shown_separately(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await put_intention(env)
    await set_intention(env, root.id, failed_tokens=25)
    await put_arrivals(env, root, [{"tokens": (100, 40)}, {"tokens": (50, 30)}])
    for output in (40, 30, 60):
        await put_context_log(env, f"intent-{root.id}", output=output, inp=90, cw=5, cr=200)
    await put_context_log(env, "chat-1", output=999)  # not a continuation turn
    await put_context_log(env, f"intent-{root.id}", output=777, agent="someone-else")  # another agent
    spend = await _spend(env)
    assert spend["basis"] == "lifetime_of_active_roots" and spend["roots_counted"] == 1
    assert spend["budget_counted"] == {
        "arrivals_in": 150,
        "arrivals_out": 70,
        "failed_tokens": 25,
        "subtask_tokens": 0,
        "dag_tokens": 0,
        "total": 245,
    }
    assert spend["context_log"] == {
        "input_uncached": 270,
        "output": 130,
        "cache_write": 15,
        "cache_read": 600,
        "calls": 3,
    }
    assert spend["unattributed_output"] == 130 - 70  # output only: failed_tokens is NOT subtracted (S4)
    assert spend["failed_tokens"] == 25
    [top] = spend["top_roots"]
    assert (top["root_id"], top["unattributed_output"], top["budget_counted"]["total"]) == (str(root.id), 60, 245)


async def test_the_window_picks_the_roots_but_both_columns_are_lifetime(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    active = await put_intention(env)
    quiet = await put_intention(env)
    await put_arrivals(env, active, [{"tokens": (10, 10), "decided_ago": 40 * 86400}, {"tokens": (5, 5)}])
    await put_context_log(env, f"intent-{active.id}", output=11, ago=40 * 86400)  # old call of an active root: counted
    await put_context_log(env, f"intent-{quiet.id}", output=500, ago=40 * 86400)  # quiet root: not in the cohort
    spend = await _spend(env)
    assert spend["roots_counted"] == 1
    assert spend["budget_counted"]["total"] == 30 and spend["context_log"]["output"] == 11


async def test_a_root_with_calls_but_no_arrival_is_counted_and_top_roots_is_capped_and_ranked(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    roots = [await put_intention(env) for _ in range(12)]
    for i, root in enumerate(roots):
        await put_arrivals(env, root, [{"tokens": (i, 0)}])
        await put_context_log(env, f"intent-{root.id}", output=1)
    only_calls = await put_intention(env)
    await put_context_log(env, f"intent-{only_calls.id}", output=42)
    spend = await _spend(env)
    assert spend["roots_counted"] == 13 and len(spend["top_roots"]) == 10
    totals = [r["budget_counted"]["total"] for r in spend["top_roots"]]
    assert totals == sorted(totals, reverse=True) and totals[0] == 11
    assert spend["unattributed_output"] == 12 * 1 + 42 - 0


async def test_a_session_id_that_is_not_a_root_is_skipped(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    await put_context_log(env, "intent-not-a-uuid", output=5)
    spend = await _spend(env)
    assert spend["roots_counted"] == 0 and spend["context_log"]["calls"] == 0


# F108 PR 4: with NOUS_MODEL_CALL_LOG_ENABLED the runner's calls go to model_call_log, not context_log.


async def test_calls_recorded_only_in_model_call_log_are_counted(env_factory):  # noqa: F811
    """After the cutover a continuation root's calls are model_call_log rows: they pick the root (no arrival needed)
    and fill the column, as context_log rows did. Only the calls context_log held count (the runner's own, its
    history summaries included); a background helper's call, another agent's and a non-root session's do not."""
    env = await env_factory(**CONT)
    root = await put_intention(env)
    await put_arrivals(env, root, [{"tokens": (100, 40)}])
    for purpose, output in (("continuation", 40), ("continuation", 30), ("action_gate", 9), ("compaction", 21)):
        await put_model_call(env, f"intent-{root.id}", output=output, inp=90, cw=5, cr=200, purpose=purpose)
    await put_model_call(env, f"intent-{root.id}", output=500, purpose="critic")  # a background helper's call
    await put_model_call(env, f"intent-{root.id}", output=777, agent="someone-else")  # another agent
    await put_model_call(env, "chat-1", output=999, purpose="chat")  # not a continuation turn
    only_calls = await put_intention(env)
    await put_model_call(env, f"intent-{only_calls.id}", output=42)  # picks its root, as a context_log row would
    spend = await _spend(env)
    assert spend["roots_counted"] == 2
    assert spend["context_log"] == {
        "input_uncached": 4 * 90 + 10,  # the root's four calls, and only_calls' one (the helper's default input)
        "output": 40 + 30 + 9 + 21 + 42,
        "cache_write": 4 * 5,
        "cache_read": 4 * 200,
        "calls": 5,
    }
    assert spend["unattributed_output"] == 142 - 40  # never negative for want of the old table's rows
    by_root = {r["root_id"]: r for r in spend["top_roots"]}
    assert by_root[str(root.id)]["context_log"]["calls"] == 4
    assert by_root[str(root.id)]["unattributed_output"] == 100 - 40


async def test_a_call_in_each_table_counts_once_each(env_factory):  # noqa: F811
    """Across the cutover a root has rows in both tables, one per call: each counts once, in its own table, even when
    two calls report the very same numbers (a UNION that dropped duplicates would merge them)."""
    env = await env_factory(**CONT)
    root = await put_intention(env)
    await put_context_log(env, f"intent-{root.id}", output=11, inp=7, cw=3, cr=100, ago=7200)  # before the cutover
    for _ in range(2):  # after it, two calls with the same numbers as the one before
        await put_model_call(env, f"intent-{root.id}", output=11, inp=7, cw=3, cr=100)
    spend = await _spend(env)
    assert spend["roots_counted"] == 1
    assert spend["context_log"] == {"input_uncached": 21, "output": 33, "cache_write": 9, "cache_read": 300, "calls": 3}
