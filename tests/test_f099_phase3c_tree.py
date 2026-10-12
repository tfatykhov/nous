"""F099 Phase 3c-2: GET /dashboard/intentions, the tree (contract 4.9)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta

import pytest
from f099_dashboard_support import (
    put_arrivals,
    put_decision,
    put_inbox,
    put_intention,
    put_proposal,
    utcnow,
)
from f099_support import CONT, ON, env_factory  # noqa: F401

from nous.api import intention_dashboard
from nous.brain import continuation

pytestmark = pytest.mark.postgres_only

ROOT_KEYS = {
    "id",
    "short_id",
    "intent",
    "state",
    "wake_policy",
    "authority",
    "origin_kind",
    "origin_channel",
    "created_at",
    "deadline",
    "root_cancelled_at",
    "root_expired_at",
    "closed_at",
    "limits",
    "children",
    "arrivals",
    "proposals",
    "owner_rows",
}
CHILD_KEYS = {
    "id",
    "parent_id",
    "depth",
    "source_kind",
    "source_id",
    "state",
    "close_reason",
    "intent",
    "created_at",
    "result_at",
    "closed_at",
    "attempts",
    "failed_tokens",
    "children",
}
ARRIVAL_KEYS = {
    "id",
    "n",
    "intention_ids",
    "decision",
    "note",
    "progress_claimed",
    "progress",
    "confidence",
    "gate_reason",
    "outcome",
    "tokens_in",
    "tokens_out",
    "decided_at",
    "decision_record_id",
    "decision_outcome",
    "decision_reviewer",
    "result_to_decision_s",
}
OWNER_ROW_KEYS = {"id", "msg_type", "title", "created_at", "push_after", "pushed_at", "delivered_at"}


async def _data(env, **kw):
    async with env.db.session() as s:
        return await intention_dashboard.get_intentions_data(s, env.agent, settings=env.settings, **kw)


async def test_the_tree_nests_children_and_carries_every_contract_key(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await put_intention(env)
    child = await put_intention(env, parent=root, intent="check the forecast")
    grand = await put_intention(env, parent=child, intent="read the radar")
    first = await put_inbox(env, root, created_ago=120)
    decision_id = await put_decision(env, outcome="noise", reviewer="agent-background")
    await put_arrivals(
        env,
        root,
        [
            {
                "inbox_ids": [first.id],
                "decision_record_id": decision_id,
                "note": "looks fine",
                "progress": True,
                "progress_claimed": True,
                "confidence": 0.8,
                "tokens": (10, 5),
                "decided_at": first.created_at + timedelta(seconds=38),
            },
            {"decision": "report"},  # no decision record, no inbox
        ],
    )
    await put_proposal(env, root, state="executed")
    await put_proposal(env, root, state="staged")  # never shown to the owner, so not here either
    await put_inbox(env, root, channel="telegram:1", source_kind="intention_report", msg_type="REPORT", title="Done")
    await put_inbox(env, root, source_kind="intention_report", msg_type="INFORM", title="Owner's answer")  # keyed

    data = await _data(env)
    assert data["continuation"] is True and data["window"] == "7d"
    [view] = data["roots"]
    assert set(view) == ROOT_KEYS
    assert [c["id"] for c in view["children"]] == [str(child.id)]
    assert set(view["children"][0]) == CHILD_KEYS
    assert [g["id"] for g in view["children"][0]["children"]] == [str(grand.id)]
    first_arrival, second = view["arrivals"]
    assert set(first_arrival) == ARRIVAL_KEYS
    assert (first_arrival["n"], second["n"]) == (1, 2)
    assert (first_arrival["decision_outcome"], first_arrival["decision_reviewer"]) == ("noise", "agent-background")
    assert first_arrival["result_to_decision_s"] == 38.0
    assert (second["decision_record_id"], second["decision_outcome"], second["result_to_decision_s"]) == (
        None,
        None,
        None,
    )
    assert [p["state"] for p in view["proposals"]] == ["executed"]  # C13: staged is left out
    assert [set(r) for r in view["owner_rows"]] == [OWNER_ROW_KEYS]
    assert view["owner_rows"][0]["title"] == "Done"  # the keyed INFORM answer row is not an owner row


async def test_the_tree_has_no_caps_and_model_text_is_returned_raw(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    hostile = "<img src=x onerror=alert(1)> **bold** ```"
    root = await put_intention(env, intent=hostile)
    for i in range(55):  # more than continuation.LINEAGE_VIEW_MAX
        await put_intention(env, parent=root, intent=f"step {i}")
    await put_arrivals(env, root, [{"note": hostile}] * 7)  # more than ARRIVALS_VIEW_MAX
    [view] = (await _data(env))["roots"]
    assert view["intent"] == hostile and view["arrivals"][0]["note"] == hostile
    assert len(view["children"]) == 55 and [a["n"] for a in view["arrivals"]] == list(range(1, 8))


async def test_limits_are_the_roots_budgets_and_null_for_a_container(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    plain = await put_intention(env)
    container = await put_intention(env, wake_policy="container")
    await put_arrivals(env, plain, [{"tokens": (7, 3)}])
    views = {v["id"]: v for v in (await _data(env))["roots"]}
    assert views[str(container.id)]["limits"] is None  # nit: null for containers
    async with env.db.session() as s:
        expected = asdict(await continuation.root_limits(s, env.agent, plain.id, settings=env.settings))
    assert views[str(plain.id)]["limits"] == expected and expected["tokens"] == 10
    assert "spawn_blocked" in expected  # C1


async def test_state_open_lists_exactly_what_list_roots_lists(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    open_root = await put_intention(env)
    cancelled = await put_intention(env, state="cancelled", root_cancelled_at=utcnow())
    expired = await put_intention(env, state="expired", root_expired_at=utcnow())
    finished = await put_intention(env, state="closed", close_reason="resolved", closed_at=utcnow())
    async with env.db.session() as s:
        listed = await continuation.list_roots(s, env.agent, state="open", limit=50, settings=env.settings)
    dash_open = await _data(env, state="open")
    assert {v["id"] for v in dash_open["roots"]} == {v["id"] for v in listed} == {str(open_root.id)}
    everything = await _data(env, state="all")
    assert {v["id"] for v in everything["roots"]} == {str(r.id) for r in (open_root, cancelled, expired, finished)}


async def test_roots_are_newest_first_and_the_limit_is_honoured(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    older = await put_intention(env, created_at=utcnow() - timedelta(hours=2))
    newer = await put_intention(env, created_at=utcnow() - timedelta(hours=1))
    await put_intention(env, created_at=utcnow() - timedelta(hours=3))
    got = [v["id"] for v in (await _data(env, limit=2))["roots"]]
    assert got == [str(newer.id), str(older.id)]


@pytest.mark.parametrize("kw", [{"window": "90d"}, {"state": "recent"}, {"limit": 0}, {"limit": 201}])
async def test_bad_input_is_a_value_error_even_with_continuation_off(env_factory, kw):  # noqa: F811
    env = await env_factory(**ON)  # continuation off

    class Boom:
        async def execute(self, *a, **k):
            raise AssertionError("a row was read")

    with pytest.raises(ValueError):
        await intention_dashboard.get_intentions_data(Boom(), env.agent, settings=env.settings, **kw)


async def test_flag_off_answers_the_disabled_payload_and_reads_no_row(env_factory):  # noqa: F811
    env = await env_factory(**ON)

    class Boom:
        async def execute(self, *a, **k):
            raise AssertionError("a row was read")

    got = await intention_dashboard.get_intentions_data(Boom(), env.agent, settings=env.settings)
    assert got == {"continuation": False, "roots": [], "metrics": None}
