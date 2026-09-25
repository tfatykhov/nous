"""Harness dashboard visibility (spec 2026-09-25 v2): the read-only data the
DAG, Ledger, Harness and Overview views render."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio

from nous.api.dashboard_queries import get_dag_dashboard_data
from nous.api.harness_dashboard import ATTENTION_CAP, get_attention_data, get_execution_data, get_harness_data
from nous.config import Settings
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore
from nous.storage.models import Event, ExecutionLedgerEntry

BASE = "https://nous.example"
OPTIONS = [
    {"id": "send", "label": "Send it", "outcome": "proceed"},
    {"id": "hold", "label": "Don't send", "outcome": "stop"},
]


def _settings() -> Settings:
    return Settings(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)


@pytest_asyncio.fixture
async def agent_id() -> str:
    return f"test-hdash-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def store(db, agent_id):
    return DAGStore(db, agent_id, _settings())


async def _approval_dag(store, name="mail"):
    dag = await store.create(
        DAGCreateRequest(
            name=name,
            nodes=[
                DAGNodeSpec(name="draft", type=DAGNodeType.subtask, instructions="draft"),
                DAGNodeSpec(
                    name="approve", type=DAGNodeType.approval, instructions="Send the report?",
                    options=OPTIONS, default_option="hold",
                ),
                DAGNodeSpec(name="send", type=DAGNodeType.subtask, instructions="send"),
            ],
            edges=[
                DAGEdgeSpec(from_node="draft", to_node="approve", edge_type="context_flow"),
                DAGEdgeSpec(from_node="approve", to_node="send", edge_type="context_flow"),
            ],
        )
    )
    await store.update_dag_status(dag.id, "running")
    return dag, {n.name: n for n in dag.nodes}


def _ask(name: str, question: str) -> DAGNodeSpec:
    return DAGNodeSpec(name=name, type=DAGNodeType.approval, instructions=question,
                       options=OPTIONS, default_option="hold")


async def _dash(db, agent_id, **kw):
    async with db.session() as session:
        return await get_dag_dashboard_data(session, agent_id, **kw)


def _node(data, dag_id, name):
    dag = next(d for d in data["active_dags"] if UUID(d["id"]) == dag_id)
    return next(n for n in dag["nodes"] if n["name"] == name)


async def test_an_open_question_is_waiting_on_you_with_what_its_card_shows(db, store, agent_id):
    dag, n = await _approval_dag(store)
    deadline = datetime.now(UTC) + timedelta(hours=3)
    await store.update_node(n["draft"].id, status="completed", result="DRAFT BODY")
    await store.update_node(
        n["approve"].id, status="awaiting_input", surface_id="card-1",
        started_at=datetime.now(UTC), answer_deadline=deadline,
        answer_history=[{"answer": "hold", "label": "Don't send", "outcome": "stop",
                         "answer_source": "companion", "answered_by": "unattributed",
                         "answered_at": "2026-09-24T09:12:00+00:00"}],
    )

    data = await _dash(db, agent_id, public_base_url=BASE)

    view = _node(data, dag.id, "approve")["approval"]
    assert view["question"] == "Send the report?"
    assert view["default_label"] == "Don't send"
    assert view["card_url"] == f"{BASE}/companion#/s/card-1"
    assert view["card_error"] is None
    assert view["card_summary"].startswith("Send the report?")
    assert "From 'draft':\nDRAFT BODY" in view["card_summary"]
    assert view["reviewing"] == ["draft"]
    assert view["attempts"][0]["answered_by"] is None  # never "unattributed"
    assert view["answer"] is None
    assert data["stats"]["waiting_count"] == 1
    assert [w["node_name"] for w in data["waiting_on_you"]] == ["approve"]
    assert data["waiting_on_you"][0]["card_url"] == f"{BASE}/companion#/s/card-1"
    active = next(d for d in data["active_dags"] if UUID(d["id"]) == dag.id)
    assert active["waiting"] == 1
    assert "approval" not in _node(data, dag.id, "send")


async def test_an_undelivered_card_says_why(db, store, agent_id):
    dag, n = await _approval_dag(store)
    await store.update_node(
        n["approve"].id, status="awaiting_input", surface_id=None,
        error="approval card not delivered yet: companion down",
        answer_deadline=datetime.now(UTC) + timedelta(hours=1),
    )

    data = await _dash(db, agent_id, public_base_url=BASE)

    waiting = data["waiting_on_you"][0]
    assert waiting["card_url"] is None
    assert waiting["card_error"] == "approval card not delivered yet: companion down"


async def test_waiting_on_you_is_ordered_by_deadline_and_skips_answered(db, store, agent_id):
    late, ln = await _approval_dag(store, "late")
    soon, sn = await _approval_dag(store, "soon")
    done, dn = await _approval_dag(store, "done")
    now = datetime.now(UTC)
    await store.update_node(ln["approve"].id, status="awaiting_input", answer_deadline=now + timedelta(hours=9))
    await store.update_node(sn["approve"].id, status="awaiting_input", answer_deadline=now + timedelta(hours=1))
    await store.update_node(dn["approve"].id, status="completed", answer="send",
                            answer_source="deadline", answered_by="system:deadline")

    data = await _dash(db, agent_id)

    assert [w["dag_name"] for w in data["waiting_on_you"]] == ["soon", "late"]
    answered = _node(data, done.id, "approve")["approval"]
    assert answered["answer_label"] == "Send it"
    assert answered["answered_by"] is None  # the deadline actor is not a person


async def test_the_held_reason_comes_from_the_orchestrator_by_uuid(db, store, agent_id):
    dag, _ = await _approval_dag(store)
    seen = []

    def held(dag_id):
        seen.append(dag_id)
        return "approved — waiting for a free slot (5/5 DAGs working)" if dag_id == dag.id else None

    data = await _dash(db, agent_id, held_reason=held)

    active = next(d for d in data["active_dags"] if UUID(d["id"]) == dag.id)
    assert active["held_reason"] == "approved — waiting for a free slot (5/5 DAGs working)"
    assert all(isinstance(x, UUID) for x in seen)


async def test_a_held_reason_that_raises_reads_as_none(db, store, agent_id):
    """main.py passes a lazy proxy that raises RuntimeError when DAGs are off."""
    await _approval_dag(store)

    def broken(_dag_id):
        raise RuntimeError("component not initialised")

    data = await _dash(db, agent_id, held_reason=broken)

    assert all(d["held_reason"] is None for d in data["active_dags"])


async def _stopped(store, name, *, dag_status="failed", answer_source="companion"):
    dag, n = await _approval_dag(store, name)
    await store.update_node(n["draft"].id, status="completed", result="x")
    await store.update_node(n["approve"].id, status="failed", answer="hold", answer_source=answer_source,
                            error="declined")
    await store.update_node(n["send"].id, status="blocked")
    await store.update_dag_status(dag.id, dag_status, result_summary="Stopped at approval")
    return dag


async def test_stopped_by_says_who_stopped_it_and_only_for_failed_dags(db, store, agent_id):
    by_companion = await _stopped(store, "c")
    by_deadline = await _stopped(store, "d", answer_source="deadline")
    cancelled = await _stopped(store, "x", dag_status="cancelled")
    crashed, cn = await _approval_dag(store, "crash")
    await store.update_node(cn["draft"].id, status="failed", error="boom")
    await store.update_dag_status(crashed.id, "failed", result_summary="Failed nodes: draft")

    data = await _dash(db, agent_id)

    stopped = {UUID(d["id"]): d["stopped_by"] for d in data["recent_dags"]}
    stops = {UUID(d["id"]): d["stops"] for d in data["recent_dags"]}
    assert stops[by_companion.id] == [
        {"node_name": "approve", "answer_source": "companion", "answer_label": "Don't send"}
    ]
    assert stops[cancelled.id] == []
    assert stopped[by_companion.id] == "companion"
    assert stopped[by_deadline.id] == "deadline"
    assert stopped[cancelled.id] is None
    assert stopped[crashed.id] is None


# ── §3.2 /dashboard/execution ──────────────────────────────────────────────


MODES = {"persist": True, "retention_days": 90, "offered_set": "warn", "context_policy": "warn",
         "claim_verification": "enforce", "action_gating": "off", "events_persisted": True}


async def _row(db, agent_id, *, tool="bash", effect="write", status="success", ago=timedelta(minutes=5),
               key=None, summary=None, key_args=None, context="interactive", at=None, **kw):
    row = ExecutionLedgerEntry(
        id=kw.pop("id", uuid.uuid4()), agent_id=agent_id, context_kind=context, tool_name=tool,
        side_effect_type=effect, status=status, result_summary=summary, idempotency_key=key,
        key_args=key_args if key_args is not None else {}, created_at=at or (datetime.now(UTC) - ago), **kw,
    )
    async with db.session() as s:
        s.add(row)
        await s.commit()
    return row


async def _exec(db, agent_id, **kw):
    kw.setdefault("modes", MODES)
    async with db.session() as session:
        return await get_execution_data(session, agent_id, **kw)


async def test_ledger_stats_follow_the_window_and_their_definitions(db, agent_id):
    await _row(db, agent_id, tool="send_email", effect="external", key="dag:a:send:1")
    await _row(db, agent_id, tool="send_file", effect="external")
    await _row(db, agent_id, tool="bash", effect="external")  # git push: external, not a send
    await _row(db, agent_id, tool="send_email", effect="external", status="blocked",
               summary="refused by duplicate", key="dag:a:send:2")
    await _row(db, agent_id, tool="bash", status="blocked", summary="refused by context_policy")
    await _row(db, agent_id, tool="send_email", effect="external", status="unknown", key="dag:a:send:3")
    await _row(db, agent_id, tool="run_python", status="unknown")  # unkeyed: nothing held
    await _row(db, agent_id, tool="write_file", status="error")
    await _row(db, agent_id, tool="bash", status="pending")
    await _row(db, agent_id, tool="bash", ago=timedelta(days=2))  # outside 24 h

    data = await _exec(db, agent_id)

    assert data["stats"] == {"calls": 9, "sends": 4, "external": 5, "repeat_sends_refused": 1,
                             "blocked": 1, "unknown": 2, "unknown_keyed": 1, "errors": 1, "pending": 1}
    assert data["modes"] == MODES
    assert (await _exec(db, agent_id, window="7d"))["stats"]["calls"] == 10


async def test_attention_is_only_keyed_unknown_sends_whatever_their_age(db, agent_id):
    old = await _row(db, agent_id, tool="send_email", effect="external", status="unknown",
                     key="dag:a:send:old", ago=timedelta(days=12), key_args={"to": ["anna@x.example"]})
    new = await _row(db, agent_id, tool="send_file", effect="external", status="unknown", key="subtask:s:f:new")
    await _row(db, agent_id, tool="run_python", status="unknown")  # timed out, holds nothing
    await _row(db, agent_id, tool="send_email", effect="external", status="unknown")  # interactive: unkeyed

    data = await _exec(db, agent_id)

    assert [r["id"] for r in data["attention"]] == [str(new.id), str(old.id)]
    assert data["attention"][1]["key_args"] == {"to": ["anna@x.example"]}


async def test_filters_and_search_narrow_the_rows_not_the_stats(db, agent_id):
    await _row(db, agent_id, tool="send_email", effect="external", context="dag_node",
               key_args={"to": ["Anna@Northwind.example"]})
    await _row(db, agent_id, tool="bash", context="subtask", key_args={"command_sha256": "ab", "command_len": 4})
    await _row(db, agent_id, tool="write_file", context="interactive", key_args={"path": "/w/100%_done.md"})

    by_ctx = await _exec(db, agent_id, context="dag_node")
    assert [r["tool_name"] for r in by_ctx["rows"]] == ["send_email"]
    assert by_ctx["stats"]["calls"] == 3
    assert [r["tool_name"] for r in (await _exec(db, agent_id, q="anna@northwind"))["rows"]] == ["send_email"]
    assert [r["tool_name"] for r in (await _exec(db, agent_id, q="100%_"))["rows"]] == ["write_file"]
    # _ and % are literals: as wildcards "10_%" would match "100%_done".
    assert (await _exec(db, agent_id, q="10_%"))["rows"] == []
    assert [r["tool_name"] for r in (await _exec(db, agent_id, effect="external"))["rows"]] == ["send_email"]
    assert [r["tool_name"] for r in (await _exec(db, agent_id, status="success"))["rows"]]


async def test_paging_walks_tied_timestamps_without_gaps_or_repeats(db, agent_id):
    tie = datetime.now(UTC) - timedelta(minutes=1)
    ids = {str((await _row(db, agent_id, at=tie)).id) for _ in range(5)}
    seen: list[str] = []
    before = None
    while True:
        page = await _exec(db, agent_id, limit=2, before=before)
        seen += [r["id"] for r in page["rows"]]
        before = page["next_before"]
        if before is None:
            break
    assert sorted(seen) == sorted(ids) and len(seen) == 5


async def test_a_refused_repeat_names_the_row_currently_holding_its_key(db, agent_id, store):
    dag, n = await _approval_dag(store, "mail")
    holder = await _row(db, agent_id, tool="send_email", effect="external", status="unknown",
                        key="dag:k:send:9b41", external_ref="<1@nous>", ago=timedelta(hours=2),
                        dag_id=dag.id, dag_node_id=n["send"].id)
    await _row(db, agent_id, tool="send_email", effect="external", status="blocked",
               summary="refused by duplicate", key="dag:k:send:9b41", dag_id=dag.id, dag_node_id=n["send"].id)

    rows = (await _exec(db, agent_id))["rows"]

    refused = next(r for r in rows if r["status"] == "blocked")
    assert refused["refusal_code"] == "duplicate"
    assert refused["held_by"]["id"] == str(holder.id)
    assert refused["held_by"]["status"] == "unknown"
    assert refused["dag_name"] == "mail" and refused["node_name"] == "send"
    assert next(r for r in rows if r["status"] == "unknown")["held_by"] is None  # not its own holder


async def test_tombstones_are_flagged_and_other_agents_never_appear(db, agent_id):
    await _row(db, agent_id, tool="send_email", effect="external", status="success", key="s:1",
               key_args={}, summary=None)
    await _row(db, f"{agent_id}-other", tool="send_email", effect="external")

    data = await _exec(db, agent_id)

    assert len(data["rows"]) == 1 and data["rows"][0]["tombstone"] is True


async def test_a_confirmed_tombstone_is_still_a_tombstone(db, agent_id):
    # The Ledger's "they got it" statement appends to result_summary; the
    # row's recipients are still gone, so it must still read as trimmed.
    await _row(db, agent_id, tool="send_email", effect="external", status="success", key="s:1",
               key_args={}, summary="confirmed delivered by operator")

    assert (await _exec(db, agent_id))["rows"][0]["tombstone"] is True


async def test_a_bad_window_or_filter_is_refused(db, agent_id):
    for bad in ({"window": "1y"}, {"context": "heartbeat"}, {"status": "done"}, {"effect": "none!"},
                {"limit": 0}, {"limit": 201}, {"before": "garbage"}, {"q": "x" * 101}):
        with pytest.raises(ValueError):
            await _exec(db, agent_id, **bad)


# ── §3.3 /dashboard/harness ────────────────────────────────────────────────


RULE_MODES = {"offered_set": "warn", "context_policy": "warn", "claim_verification": "enforce"}


async def _event(db, agent_id, event_type, data, *, ago=timedelta(hours=1), session_id="s-1"):
    async with db.session() as s:
        s.add(Event(id=uuid.uuid4(), agent_id=agent_id, event_type=event_type, data=data,
                    session_id=session_id, created_at=datetime.now(UTC) - ago))
        await s.commit()


def _unoffered(tool, ctx, mode="warn"):
    return {"tool_name": tool, "context_kind": ctx, "mode": mode, "offered_count": 12}


def _policy(tool, ctx, violation, mode="warn"):
    return {"tool_name": tool, "context_kind": ctx, "violation": violation, "mode": mode}


async def _harness(db, agent_id, **kw):
    kw.setdefault("modes", RULE_MODES)
    kw.setdefault("events_persisted", True)
    async with db.session() as session:
        return await get_harness_data(session, agent_id, **kw)


async def test_each_rule_counts_its_own_events_grouped_by_the_mode_they_ran_under(db, agent_id):
    # One call flagged by BOTH rules (warn): it counts once in each, never summed.
    await _event(db, agent_id, "harness_unoffered_tool_call", _unoffered("send_file", "heartbeat_callback"))
    await _event(db, agent_id, "harness_context_policy_violation",
                 _policy("send_file", "heartbeat_callback", "undeclared"))
    await _event(db, agent_id, "harness_context_policy_violation",
                 _policy("send_email", "background", "level:external", mode="enforce"))
    await _event(db, agent_id, "harness_unoffered_tool_call", _unoffered("bash", "subtask"),
                 ago=timedelta(days=9))  # before the 7 d window

    data = await _harness(db, agent_id)

    off, pol = data["rules"]["offered_set"], data["rules"]["context_policy"]
    assert off["mode"] == "warn" and off["by_mode"] == {"warn": 1}
    assert pol["by_mode"] == {"warn": 1, "enforce": 1}
    assert off["by_context"] == [{"key": "heartbeat_callback", "count": 1}]
    assert pol["by_violation"] == [{"key": "level:external", "count": 1}, {"key": "undeclared", "count": 1}]
    assert off["first_event_at"] < (datetime.now(UTC) - timedelta(days=8)).isoformat()  # looks past the window
    assert "total" not in data  # nothing is ever summed across rules


async def test_claim_evidence_comes_from_new_events_and_older_ones_are_kept_apart(db, agent_id):
    await _event(db, agent_id, "f026_claim_verification",
                 {"verified": False, "violation_count": 2, "mode": "enforce"}, ago=timedelta(days=3))
    await _event(db, agent_id, "f026_claim_verification", {
        "verified": False, "claim_count": 2, "violation_count": 1, "mode": "enforce",
        "claims": [{"kind": "push", "evidence": "none", "text": "I've pushed the fix"},
                   {"kind": "file", "evidence": "exact", "text": "saved to /w/a.md"}],
    }, ago=timedelta(hours=2), session_id="tg-1")
    await _event(db, agent_id, "f026_claim_verification",
                 {"verified": True, "claim_count": 0, "claims": [], "violation_count": 0, "mode": "enforce"})

    claims = (await _harness(db, agent_id))["rules"]["claims"]

    assert claims["by_evidence"] == {"exact": 1, "plausible": 0, "none": 1}
    assert claims["turns_with_claims"] == 1
    assert claims["legacy"] == {"events": 1, "violations": 2}
    assert claims["evidence_since"] is not None
    assert claims["none_by_mode"] == {"enforce": 1}


async def test_no_evidence_claims_are_counted_under_the_mode_they_ran_in(db, agent_id):
    # A switch warn -> enforce inside the window: only the enforce-mode claims
    # had a correction queued, so the two must never be described together.
    for mode in ("warn", "enforce", "enforce"):
        await _event(db, agent_id, "f026_claim_verification", {
            "claim_count": 1, "violation_count": 1, "mode": mode,
            "claims": [{"kind": "push", "evidence": "none", "text": "I pushed"}]})

    claims = (await _harness(db, agent_id))["rules"]["claims"]

    assert claims["none_by_mode"] == {"warn": 1, "enforce": 2}
    assert claims["by_evidence"]["none"] == 3


async def test_top_patterns_and_one_entry_per_day(db, agent_id):
    for _ in range(3):
        await _event(db, agent_id, "harness_context_policy_violation",
                     _policy("run_python", "heartbeat_check", "undeclared"), session_id="hb-9")
    await _event(db, agent_id, "harness_unoffered_tool_call", _unoffered("spawn_task", "subtask"))
    await _event(db, agent_id, "f026_claim_verification", {
        "claim_count": 1, "violation_count": 1, "mode": "enforce",
        "claims": [{"kind": "push", "evidence": "none", "text": "I've pushed the fix"}]})

    data = await _harness(db, agent_id, window="7d")

    top = data["patterns"][0]
    assert (top["rule"], top["context"], top["tool"], top["violation"], top["count"]) == (
        "context_policy", "heartbeat_check", "run_python", "undeclared", 3)
    assert top["latest_session"] == "hb-9"
    claim = next(p for p in data["patterns"] if p["rule"] == "claims")
    assert (claim["context"], claim["tool"], claim["violation"]) == (None, None, "no evidence")
    assert claim["snippet"] == "I've pushed the fix"
    days = [d["date"] for d in data["daily"]]
    assert len(days) == 8 and days == sorted(days)
    assert sum(d["context_policy"] or 0 for d in data["daily"]) == 3  # earlier days are gaps
    assert sum(d["claims_none"] or 0 for d in data["daily"]) == 1


async def test_harness_says_whether_anything_was_recorded(db, agent_id):
    await _event(db, f"{agent_id}-other", "harness_unoffered_tool_call", _unoffered("bash", "subtask"))

    data = await _harness(db, agent_id, events_persisted=False)

    assert data["events_persisted"] is False
    assert data["rules"]["offered_set"]["by_mode"] == {}
    assert data["rules"]["offered_set"]["first_event_at"] is None
    with pytest.raises(ValueError):
        await _harness(db, agent_id, window="90d")


# ── §3.4 /dashboard/attention ──────────────────────────────────────────────


async def _attention(db, agent_id, **kw):
    kw.setdefault("modes", RULE_MODES)
    kw.setdefault("events_persisted", True)
    kw.setdefault("ledger_persisted", True)
    async with db.session() as session:
        return await get_attention_data(session, agent_id, **kw)


async def test_questions_waiting_uses_the_dag_tabs_own_predicate(db, store, agent_id):
    soon, sn = await _approval_dag(store, "soon")
    late, ln = await _approval_dag(store, "late")
    ended, en = await _approval_dag(store, "ended")
    now = datetime.now(UTC)
    await store.update_node(sn["approve"].id, status="awaiting_input", answer_deadline=now + timedelta(hours=1))
    await store.update_node(ln["approve"].id, status="awaiting_input", answer_deadline=now + timedelta(hours=5))
    await store.update_node(en["approve"].id, status="awaiting_input", answer_deadline=now + timedelta(minutes=5))
    await store.update_dag_status(ended.id, "cancelled")  # a stranded node is not a question

    data = await _attention(db, agent_id)

    assert data["questions_waiting"] == 2
    assert (data["next"]["dag_name"], data["next"]["node_name"], data["next"]["default_label"]) == (
        "soon", "approve", "Don't send")
    assert data["questions_waiting"] == (await _dash(db, agent_id))["stats"]["waiting_count"]


async def test_sends_in_doubt_match_the_ledger_attention_list(db, store, agent_id):
    dag, n = await _approval_dag(store, "mail")
    await _row(db, agent_id, tool="send_email", effect="external", status="unknown", key="dag:k:s:1",
               key_args={"to": ["anna@x.example"], "cc": ["bo@x.example"]}, dag_id=dag.id,
               ago=timedelta(hours=2))
    await _row(db, agent_id, tool="run_python", status="unknown")

    data = await _attention(db, agent_id)

    assert data["sends_in_doubt"] == len((await _exec(db, agent_id))["attention"]) == 1
    latest = data["latest_in_doubt"]
    assert (latest["tool_name"], latest["recipients"], latest["dag_name"], latest["tombstone"]) == (
        "send_email", ["anna@x.example", "bo@x.example"], "mail", False)


async def test_attention_harness_counts_warn_events_per_rule_this_week(db, agent_id):
    await _event(db, agent_id, "harness_unoffered_tool_call", _unoffered("bash", "subtask"))
    await _event(db, agent_id, "harness_context_policy_violation", _policy("bash", "subtask", "spawn"))
    await _event(db, agent_id, "harness_context_policy_violation",
                 _policy("bash", "subtask", "spawn", mode="enforce"))
    await _event(db, agent_id, "harness_unoffered_tool_call", _unoffered("bash", "subtask"),
                 ago=timedelta(days=8))

    data = await _attention(db, agent_id, events_persisted=False, ledger_persisted=False)

    assert data["harness"] == {"events_persisted": False,
                               "offered_set": {"mode": "warn", "warn_7d": 1, "refused_7d": 0},
                               "context_policy": {"mode": "warn", "warn_7d": 1, "refused_7d": 1}}
    assert data["ledger_persisted"] is False
    assert (data["questions_waiting"], data["sends_in_doubt"], data["next"], data["latest_in_doubt"]) == (
        0, 0, None, None)


async def test_two_stops_from_different_sources_read_mixed_and_list_both(db, store, agent_id):
    """Parallel branches: one approval declined in the companion, the other
    defaulted at its deadline — neither source may hide the other."""
    dag = await store.create(DAGCreateRequest(
        name="two",
        nodes=[
            _ask("a1", "A?"),
            _ask("a2", "B?"),
            DAGNodeSpec(name="x", type=DAGNodeType.subtask, instructions="x"),
            DAGNodeSpec(name="y", type=DAGNodeType.subtask, instructions="y"),
        ],
        edges=[DAGEdgeSpec(from_node="a1", to_node="x"), DAGEdgeSpec(from_node="a2", to_node="y")],
    ))
    n = {x.name: x for x in dag.nodes}
    await store.update_node(n["a1"].id, status="failed", answer="hold", answer_source="companion")
    await store.update_node(n["a2"].id, status="failed", answer="hold", answer_source="deadline")
    await store.update_node(n["x"].id, status="blocked")
    await store.update_node(n["y"].id, status="blocked")
    await store.update_dag_status(dag.id, "failed", result_summary="Stopped at approval")

    recent = next(d for d in (await _dash(db, agent_id))["recent_dags"] if UUID(d["id"]) == dag.id)

    assert recent["stopped_by"] == "mixed"
    assert sorted(s["answer_source"] for s in recent["stops"]) == ["companion", "deadline"]


async def test_reviewing_names_the_outputs_under_review_not_an_earlier_approval(db, store, agent_id):
    dag = await store.create(DAGCreateRequest(
        name="chain",
        nodes=[
            DAGNodeSpec(name="draft", type=DAGNodeType.subtask, instructions="d"),
            _ask("a1", "A?"),
            _ask("a2", "B?"),
            DAGNodeSpec(name="send", type=DAGNodeType.subtask, instructions="s"),
        ],
        edges=[DAGEdgeSpec(from_node="draft", to_node="a1", edge_type="context_flow"),
               DAGEdgeSpec(from_node="a1", to_node="a2", edge_type="context_flow"),
               DAGEdgeSpec(from_node="a2", to_node="send", edge_type="context_flow")],
    ))
    await store.update_dag_status(dag.id, "running")
    n = {x.name: x for x in dag.nodes}
    await store.update_node(n["draft"].id, status="completed", result="DRAFT")
    await store.update_node(n["a1"].id, status="completed", answer="send", answer_source="companion",
                            result="Answered in the companion: 'Send it'")
    await store.update_node(n["a2"].id, status="awaiting_input", answer_deadline=datetime.now(UTC) + timedelta(hours=1))

    view = _node(await _dash(db, agent_id), dag.id, "a2")["approval"]

    assert view["reviewing"] == ["draft"]
    assert "From 'draft':\nDRAFT" in view["card_summary"]
    assert "From 'a1':" in view["card_summary"]  # the card still shows the earlier answer


async def test_the_holder_carries_its_session_and_turn(db, agent_id):
    holder = await _row(db, agent_id, tool="send_email", effect="external", status="success", key="subtask:s:k",
                        session_id="subtask-1", turn=2, ago=timedelta(minutes=30))
    await _row(db, agent_id, tool="send_email", effect="external", status="blocked",
               summary="refused by duplicate", key="subtask:s:k", session_id="subtask-1", turn=2)

    refused = next(r for r in (await _exec(db, agent_id))["rows"] if r["status"] == "blocked")

    assert refused["held_by"] == {"id": str(holder.id), "status": "success",
                                  "created_at": refused["held_by"]["created_at"], "external_ref": None,
                                  "session_id": "subtask-1", "turn": 2}


async def test_unmeasured_days_are_gaps_not_zeros(db, agent_id):
    await _event(db, agent_id, "harness_unoffered_tool_call", _unoffered("bash", "subtask"), ago=timedelta(days=2))
    await _event(db, agent_id, "f026_claim_verification", {"violation_count": 1, "mode": "enforce"},
                 ago=timedelta(days=5))  # legacy: no evidence levels
    await _event(db, agent_id, "f026_claim_verification",
                 {"claim_count": 0, "claims": [], "violation_count": 0, "mode": "enforce"}, ago=timedelta(days=1))

    data = await _harness(db, agent_id, window="7d")

    offered = [d["offered_set"] for d in data["daily"]]
    claims = [d["claims_none"] for d in data["daily"]]
    # The two rules write an event only when they flag a call, so a quiet
    # day before the first flag could be clean OR before the rule ran at all
    # (a fresh deploy) — the record cannot tell, so it is a gap, never a zero
    # that would read as a clean day. After the first flag, with the rule
    # still on, a quiet day is a measured zero.
    assert offered[:5] == [None] * 5 and offered[5] == 1 and offered[-2:] == [0, 0]
    assert claims[:6] == [None] * 6 and claims[-2:] == [0, 0]  # claims write every turn
    assert all(d["context_policy"] is None for d in data["daily"])  # never flagged: can't tell
    assert data["rules"]["claims"]["legacy"] == {"events": 1, "violations": 0 + 1}
    assert data["rules"]["claims"]["turns_with_claims"] == 0  # claims: [] is NOT legacy
    assert data["rules"]["claims"]["by_mode"] == {"enforce": 1}


async def test_claim_days_count_from_the_first_claim_event_ever_when_all_are_post_2c(db, agent_id):
    new = {"claim_count": 0, "claims": [], "violation_count": 0, "mode": "enforce"}
    await _event(db, agent_id, "f026_claim_verification", new, ago=timedelta(days=10))  # before the window
    await _event(db, agent_id, "f026_claim_verification", new, ago=timedelta(days=2))

    data = await _harness(db, agent_id, window="7d")

    # Every turn writes a claim event, and none in view is pre-2c: a quiet
    # day in the window had no turns, so zero unsupported claims is measured.
    assert [d["claims_none"] for d in data["daily"]] == [0] * 8


async def test_a_rule_that_is_off_draws_gaps_but_keeps_what_it_recorded(db, agent_id):
    await _event(db, agent_id, "harness_context_policy_violation", _policy("bash", "subtask", "spawn"),
                 ago=timedelta(days=3))
    await _event(db, agent_id, "f026_claim_verification",
                 {"claim_count": 0, "claims": [], "violation_count": 0, "mode": "enforce"}, ago=timedelta(days=6))

    data = await _harness(db, agent_id, window="7d",
                          modes={**RULE_MODES, "context_policy": "off", "claim_verification": "off"})

    policy = [d["context_policy"] for d in data["daily"]]
    assert policy[4] == 1  # recorded while it was on: a record, whatever the mode is now
    assert [v for i, v in enumerate(policy) if i != 4] == [None] * 7  # off now: a zero would claim "clean"
    assert [d["claims_none"] for d in data["daily"]] == [None] * 8


async def test_with_ledger_persistence_off_no_send_is_in_doubt(db, agent_id):
    # No LedgerStore is installed then (main.py), so no retry is refused: an
    # old keyed unknown row holds nothing and must not be reported as held.
    await _row(db, agent_id, tool="send_email", effect="external", status="unknown", key="dag:k:s:1",
               key_args={"to": ["anna@x.example"]})

    execution = await _exec(db, agent_id, modes={**MODES, "persist": False})
    attention = await _attention(db, agent_id, ledger_persisted=False)

    assert (execution["attention"], execution["attention_total"]) == ([], 0)
    assert (attention["sends_in_doubt"], attention["latest_in_doubt"]) == (0, None)
    assert [r["status"] for r in execution["rows"]] == ["unknown"]  # history stays in the table


async def test_the_in_doubt_count_is_every_held_send_not_the_page_shown(db, agent_id):
    for i in range(ATTENTION_CAP + 5):
        await _row(db, agent_id, tool="send_email", effect="external", status="unknown",
                   key=f"dag:k:s:{i}", ago=timedelta(minutes=i + 1))

    execution = await _exec(db, agent_id)
    attention = await _attention(db, agent_id)

    assert len(execution["attention"]) == ATTENTION_CAP
    assert execution["attention_total"] == attention["sends_in_doubt"] == ATTENTION_CAP + 5
    assert attention["latest_in_doubt"]["created_at"] == execution["attention"][0]["created_at"]


async def test_nothing_recorded_means_no_numbers_at_all(db, agent_id):
    # Events from before persistence was switched off are still in the table;
    # none of them may reach a page that says nothing below was measured.
    await _event(db, agent_id, "harness_unoffered_tool_call", _unoffered("bash", "subtask"))
    await _event(db, agent_id, "f026_claim_verification", {
        "claim_count": 1, "violation_count": 1, "mode": "enforce",
        "claims": [{"kind": "push", "evidence": "none", "text": "I pushed"}]})

    data = await _harness(db, agent_id, events_persisted=False)

    assert all(v is None for d in data["daily"] for k, v in d.items() if k != "date")
    assert data["patterns"] == []
    off = data["rules"]["offered_set"]
    assert (off["by_mode"], off["by_context"], off["by_tool"], off["first_event_at"]) == ({}, [], [], None)
    assert data["rules"]["claims"]["by_evidence"] == {"exact": 0, "plausible": 0, "none": 0}


async def test_attention_reports_refusals_for_a_rule_in_enforce(db, agent_id):
    await _event(db, agent_id, "harness_context_policy_violation", _policy("bash", "subtask", "spawn", mode="enforce"))
    await _event(db, agent_id, "harness_context_policy_violation", _policy("bash", "subtask", "spawn"))

    h = (await _attention(db, agent_id, modes={**RULE_MODES, "context_policy": "enforce"}))["harness"]

    assert h["context_policy"] == {"mode": "enforce", "warn_7d": 1, "refused_7d": 1}
    assert h["offered_set"] == {"mode": "warn", "warn_7d": 0, "refused_7d": 0}
