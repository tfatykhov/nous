"""F114 goals: what a goal's session is doing now (``nous.goals.activity``), read from rows.

Every row is written directly (``goal_activity_support``), each test on an agent of its own. Times are fixed
instants around ``T0``: the read compares stored times only, so nothing here depends on the clock of the machine
that runs it.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from goal_activity_support import JOB, RUNNER, SECRET, T0, at, bash_input, intent_sid, world_of
from sqlalchemy import text

from nous.api import work_view_dashboard
from nous.goals import activity, spend

pytestmark = pytest.mark.postgres_only

# ---- the session, the current work and the phase ------------------------------------------------------------------


async def test_a_goal_with_no_session_reads_none(db):
    w = await world_of(db)
    found = await w.read()
    assert found["session"] is None and found["phase"] == "none" and found["current"] is None
    assert found["running_count"] == 0
    assert (found["last_call_at"], found["calls"], found["jobs"]) == (None, None, None)
    assert (found["recent"], found["work_sessions"]) == ([], [])


async def test_a_running_task_of_the_chain_is_named_by_its_intent_redacted_and_cut(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    await w.task(s.root_id, intent=f"Fix the export {SECRET} " + "and check it " * 30, started=at(5))
    found = await w.read()
    assert found["phase"] == "task" and found["running_count"] == 1
    current = found["current"]
    assert (current["kind"], current["status"], current["first_task"]) == ("task", "running", False)
    assert current["started_at"] == at(5).isoformat()
    assert current["name"].startswith("Fix the export ") and SECRET not in current["name"]
    assert "REDACTED" in current["name"]
    assert len(current["name"]) <= activity.NAME_CHARS == 120
    assert list(current) == ["kind", "name", "first_task", "status", "started_at", "waiting_for"]
    assert current["waiting_for"] is None


async def test_the_sessions_own_first_task_has_no_name(db):
    w = await world_of(db)
    await w.session()
    found = await w.read()
    assert found["phase"] == "task"
    assert (found["current"]["name"], found["current"]["first_task"]) == (None, True)


async def test_a_running_workflow_is_named_and_the_later_started_item_is_current(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    await w.workflow(s.root_id, name="Build and check the export", started=at(3))
    found = await w.read()
    assert found["phase"] == "workflow" and found["running_count"] == 1
    assert (found["current"]["kind"], found["current"]["name"]) == ("workflow", "Build and check the export")
    await w.task(s.root_id, intent="Sort the photos", started=at(7))
    found = await w.read()
    assert found["running_count"] == 2
    assert (found["current"]["kind"], found["current"]["name"]) == ("task", "Sort the photos")


async def test_a_pending_task_alone_is_current_and_waiting_to_start(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    await w.task(s.root_id, status="pending", intent="Sort the photos")
    found = await w.read()
    assert found["phase"] == "task" and found["running_count"] == 0
    assert (found["current"]["status"], found["current"]["started_at"]) == ("pending", None)


WAIT = ("wait_for_job", "wait", "awaiting_check")


@pytest.mark.parametrize(
    ("wait_for", "expected"),
    [
        ({"kind": "job", "job_id": JOB}, {"kind": "job", "job_id": JOB}),
        ({"kind": "job", "job_id": f"wait for {JOB} then email the notes"}, {"kind": "other"}),
        ({"kind": "pr", "number": 951, "repo": "owner/private-repo", "until": "merged"}, {"kind": "pr", "number": 951}),
        ({"kind": "pr", "number": True, "until": "merged"}, {"kind": "other"}),
        ({"kind": "pr", "number": 0, "until": "merged"}, {"kind": "other"}),
        ({"kind": "file", "path": "notes/the-private-plan.md"}, {"kind": "file"}),
        ({"kind": "mail", "from": "someone"}, {"kind": "other"}),
        (["job", JOB], {"kind": "other"}),
    ],
    ids=["job", "job-sentence", "pr", "pr-bool", "pr-zero", "file", "unknown-kind", "not-an-object"],
)
async def test_a_workflow_waiting_on_a_wait_step_says_what_for_in_identifiers_only(db, wait_for, expected):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    await w.workflow(
        s.root_id,
        started=at(1),
        nodes=(("build", "subtask", "completed", at(1), None), (*WAIT, at(20), wait_for)),
    )
    found = await w.read()
    assert found["phase"] == "waiting"
    assert found["current"]["waiting_for"] == expected
    said = json.dumps(found)
    for words in ("then email the notes", "owner/private-repo", "the-private-plan", "someone", "wait_for_job"):
        assert words not in said


@pytest.mark.parametrize("step", ["running", "awaiting_input", "awaiting_check"])
async def test_the_answer_holds_no_step_name(db, step):
    """A workflow step's name is the agent's text: only the workflow's own name is sent, never a step's."""
    w = await world_of(db)
    s = await w.session(first_status="completed")
    kind = {"running": "subtask", "awaiting_input": "approval", "awaiting_check": "wait"}[step]
    wait = {"kind": "job", "job_id": JOB} if kind == "wait" else None
    await w.workflow(
        s.root_id, name="Build the export", started=at(1), nodes=(("STEP-NAME-MARK", kind, step, at(2), wait),)
    )
    found = await w.read()
    assert found["current"]["name"] == "Build the export"
    assert "node" not in found["current"]
    assert "STEP-NAME-MARK" not in json.dumps(found)


async def test_a_workflow_with_a_running_step_besides_a_wait_is_running(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    await w.workflow(
        s.root_id,
        started=at(1),
        nodes=((*WAIT, at(20), {"kind": "job", "job_id": JOB}), ("check", "subtask", "running", at(10), None)),
    )
    found = await w.read()
    assert found["phase"] == "workflow" and found["current"]["waiting_for"] is None


async def test_a_workflow_step_waiting_for_the_owners_approval_reads_approval(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    await w.workflow(s.root_id, started=at(1), nodes=(("ship", "approval", "awaiting_input", at(4), None),))
    found = await w.read()
    assert found["phase"] == "approval" and found["current"]["waiting_for"] is None


@pytest.mark.parametrize(
    ("root_state", "phase"),
    [("deciding", "deciding"), ("result_ready", "deciding"), ("awaiting_owner", "asking"), ("pending", "between")],
)
async def test_with_nothing_running_the_chains_own_state_gives_the_phase(db, root_state, phase):
    w = await world_of(db)
    await w.session(first_status="completed", root_state=root_state)
    found = await w.read()
    assert (found["phase"], found["current"], found["running_count"]) == (phase, None, 0)


async def test_a_pending_intention_whose_subtask_finished_is_not_running(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    await w.task(s.root_id, status="completed", started=at(2), state="pending")
    found = await w.read()
    assert (found["current"], found["running_count"], found["phase"]) == (None, 0, "between")


async def test_the_root_is_found_from_the_first_subtask_before_the_session_row_has_it(db):
    w = await world_of(db)
    await w.session(root_on_row=False)
    found = await w.read()
    assert found["phase"] == "task" and found["current"]["first_task"] is True


async def test_an_open_session_with_no_root_at_all_is_between_steps(db):
    w = await world_of(db)
    await w.session(with_root=False)
    found = await w.read()
    assert (found["phase"], found["current"], found["running_count"]) == ("between", None, 0)


async def test_an_ended_newest_session_reads_none_with_no_current_work(db):
    w = await world_of(db)
    s = await w.session(n=1, first_status="running", ended=at(30))
    await w.task(s.root_id, started=at(5))
    found = await w.read()
    assert found["session"] == {"n": 1, "open": False, "started_at": T0.isoformat(), "ended_at": at(30).isoformat()}
    assert (found["phase"], found["current"]) == ("none", None)
    assert found["jobs"] is None and found["recent"] == []


async def test_the_open_session_is_read_before_an_ended_one(db):
    w = await world_of(db)
    await w.session(n=1, ended=at(30), first_status="completed")
    await w.session(n=2, started=at(60))
    found = await w.read()
    assert (found["session"]["n"], found["session"]["open"], found["phase"]) == (2, True, "task")


@pytest.mark.parametrize(
    ("nodes", "phase"),
    [
        ([("running", "subtask")], "workflow"),
        ([("awaiting_input", "approval"), ("awaiting_check", "wait")], "approval"),
        ([("awaiting_check", "wait")], "waiting"),
        ([("awaiting_check", "subtask")], "workflow"),
        ([], "workflow"),
    ],
)
def test_phase_of_a_workflow_follows_its_active_steps(nodes, phase):
    assert activity.phase_of(session_open=True, current_kind="workflow", nodes=nodes, states=[]) == phase


def test_phase_of_an_ended_session_is_none_whatever_else_holds():
    assert activity.phase_of(session_open=False, current_kind="task", nodes=[], states=["deciding"]) == "none"


# ---- the calls, the jobs and the Work screen's sessions -----------------------------------------------------------


async def test_calls_of_the_chain_are_counted_on_each_of_the_three_branches_and_no_others(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    child = await w.task(s.root_id, status="completed", started=at(1))
    dag = await w.workflow(s.root_id, status="completed", started=at(2))
    await w.call(at(3), session_id=intent_sid(s.root_id))
    await w.call(at(4), subtask_id=child, session_id="subtask-0a1b2c3d", work_kind="subtask", is_error=True)
    await w.call(at(5), dag_id=dag, session_id="dag-step-session", work_kind="workflow_step")
    await w.call(at(6), session_id=intent_sid(uuid.uuid4()))  # another chain
    await w.call(at(6), subtask_id=uuid.uuid4())  # a task of no chain of this goal
    await w.call(at(-1), session_id=intent_sid(s.root_id))  # before the session started
    await w.call(at(6), session_id=intent_sid(s.root_id), agent=f"other-{uuid.uuid4().hex[:8]}")  # another agent
    found = await w.read()
    assert found["calls"] == {"total": 3, "errors": 1, "capped": False}
    assert found["last_call_at"] == at(5).isoformat()


async def test_an_ended_session_counts_calls_up_to_five_minutes_after_its_end(db):
    w = await world_of(db)
    s = await w.session(first_status="completed", ended=at(30))
    for minute in (29, 34, 36):
        await w.call(at(minute), session_id=intent_sid(s.root_id))
    found = await w.read()
    assert found["calls"] == {"total": 2, "errors": 0, "capped": False}
    assert found["last_call_at"] == at(34).isoformat()
    assert (found["recent"], found["jobs"]) == ([], None)


async def test_an_open_session_with_no_root_has_no_calls(db):
    w = await world_of(db)
    await w.session(with_root=False)
    found = await w.read()
    assert found["calls"] == {"total": 0, "errors": 0, "capped": False} and found["last_call_at"] is None
    assert found["jobs"] == {"launched": 0, "last_launch_at": None, "capped": False}


async def test_the_recent_calls_are_the_newest_eight_in_words_from_closed_sets(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    rows = [
        ("bash", "ran", "intention"),
        ("read_file", "error", "subtask"),
        ("send_email", "refused", "workflow_step"),
        ("browser_task", "gated", "intention"),
        ("bash", "suppressed", "heartbeat"),
        ("old_tool", "retired", "intention"),
        ("bash", "bad_input", "intention"),
        ("mcp__notes__search", "a-new-outcome", "intention"),
        ("bash", "ran", "intention"),
        ("bash", "ran", "intention"),
    ]
    sid = intent_sid(s.root_id)
    for minute, (tool, outcome, kind) in enumerate(rows, start=1):
        await w.call(at(minute), session_id=sid, tool=tool, outcome=outcome, work_kind=kind)
    recent = (await w.read())["recent"]
    assert len(recent) == activity.RECENT_SHOWN == 8
    assert [r["at"] for r in recent] == [at(m).isoformat() for m in range(10, 2, -1)]
    assert recent[0] == {"tool": "bash", "at": at(10).isoformat(), "outcome": "ran", "in": "decisions"}
    by_minute = {r["at"]: r for r in recent}
    third = by_minute[at(3).isoformat()]
    assert (third["tool"], third["outcome"], third["in"]) == ("send_email", "refused", "workflow")
    assert by_minute[at(4).isoformat()]["outcome"] == "gated"
    assert by_minute[at(5).isoformat()]["in"] == "other"
    assert by_minute[at(6).isoformat()]["outcome"] == "retired"
    assert by_minute[at(7).isoformat()]["outcome"] == "bad_input"
    eighth = by_minute[at(8).isoformat()]
    assert (eighth["tool"], eighth["outcome"]) == ("mcp__notes__search", "other")


@pytest.mark.parametrize(
    ("called", "sent"),
    [
        ("mcp__notes__search", "mcp__notes__search"),
        ("tool.v2-beta", "tool.v2-beta"),
        ("x" * 64, "x" * 64),
        ("x" * 65, "other"),
        ("send the notes to everyone", "other"),
        ("bash;rm", "other"),
        ("", "other"),
        ("café", "other"),
    ],
    ids=["mcp", "dots-and-dashes", "64", "65", "a-sentence", "punctuation", "empty", "non-ascii"],
)
async def test_a_tool_name_is_sent_only_in_the_shape_of_a_tool_name(db, called, sent):
    """The log keeps what the model called a tool it made up; only a name made of letters, digits, ``_``, ``.`` and
    ``-``, at most 64 long, is sent."""
    w = await world_of(db)
    s = await w.session()
    await w.call(at(1), session_id=intent_sid(s.root_id), tool=called)
    assert (await w.read())["recent"][0]["tool"] == sent


async def test_a_call_of_a_task_says_task(db):
    w = await world_of(db)
    s = await w.session()
    await w.call(at(1), subtask_id=s.first_subtask_id, work_kind="subtask", outcome="error", is_error=True)
    assert (await w.read())["recent"] == [{"tool": "bash", "at": at(1).isoformat(), "outcome": "error", "in": "task"}]


async def test_a_calls_time_is_when_it_ended_and_the_last_call_is_the_last_to_end(db):
    """A row is written when its call ends: a two-hour command that ended a minute ago is the last call, not one of
    two hours ago."""
    w = await world_of(db)
    s = await w.session()
    sid = intent_sid(s.root_id)
    await w.call(at(1), session_id=sid, tool="bash", duration_ms=120 * 60_000)
    await w.call(at(10), session_id=sid, tool="read_file", duration_ms=2_000)
    await w.call(at(20), session_id=sid, tool="write_file", duration_ms=None)
    found = await w.read()
    assert found["last_call_at"] == at(121).isoformat()
    assert [(r["tool"], r["at"]) for r in found["recent"]] == [
        ("bash", at(121).isoformat()),
        ("write_file", at(20).isoformat()),
        ("read_file", (at(10) + timedelta(seconds=2)).isoformat()),
    ]


@pytest.mark.parametrize(("rows", "total", "capped"), [(5, 3, True), (3, 3, False)])
async def test_the_calls_read_is_capped_and_says_so(db, monkeypatch, rows, total, capped):
    monkeypatch.setattr(activity, "CALLS_CAP", 3)
    w = await world_of(db)
    s = await w.session()
    for minute in range(rows):
        await w.call(at(minute + 1), session_id=intent_sid(s.root_id), is_error=True)
    found = await w.read()
    assert found["calls"] == {"total": total, "errors": total, "capped": capped}
    assert found["last_call_at"] == at(rows).isoformat()


async def test_a_long_call_that_ended_last_is_the_last_call_though_more_than_the_cap_started_after_it(db, monkeypatch):
    """The capped read keeps the calls that ended last: a two-hour command is the last call even when more short
    calls than the cap started after it and ended before it."""
    monkeypatch.setattr(activity, "CALLS_CAP", 3)
    w = await world_of(db)
    s = await w.session()
    sid = intent_sid(s.root_id)
    await w.call(at(1), session_id=sid, tool="bash", duration_ms=120 * 60_000)
    for minute in range(2, 7):
        await w.call(at(minute), session_id=sid, tool="read_file", duration_ms=1_000)
    found = await w.read()
    assert found["last_call_at"] == at(121).isoformat()
    assert found["recent"][0] == {"tool": "bash", "at": at(121).isoformat(), "outcome": "ran", "in": "decisions"}
    assert [r["at"] for r in found["recent"][1:]] == [(at(m) + timedelta(seconds=1)).isoformat() for m in (6, 5)]
    assert found["calls"] == {"total": 3, "errors": 0, "capped": True}


async def test_jobs_started_this_session_count_each_launch_and_agree_with_the_days_count(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    child = await w.task(s.root_id, status="completed", started=at(1))
    launch = f"{RUNNER} launch nous --prompt-file /workspace/notes/p.md"
    sid = intent_sid(s.root_id)
    await w.call(at(10), session_id=sid, input_text=bash_input(f"{launch} && {launch}"))
    await w.call(at(11), subtask_id=child, work_kind="subtask", input_text=bash_input(launch))
    await w.call(at(12), session_id=sid, input_text=bash_input(f"{RUNNER} status {JOB}"))
    await w.call(at(13), session_id=sid, outcome="refused", input_text=bash_input(launch))
    await w.call(at(14), session_id=sid, tool="read_file", input_text=bash_input(launch))
    # The shell reads these as runner.sh launch too, though no text of them says "runner.sh".
    for minute, command in enumerate(
        [
            '/workspace/claude-jobs/run"ner".sh launch nous',
            "/workspace/claude-jobs/runner'.sh' launch nous",
            "/workspace/claude-jobs/runner\\.sh launch nous",
        ],
        start=15,
    ):
        await w.call(at(minute), session_id=sid, input_text=bash_input(command))
    found = await w.read()
    assert found["jobs"] == {"launched": 6, "last_launch_at": at(17).isoformat(), "capped": False}
    async with db.session() as session:
        today = await spend.jobs_launched_today(
            session, w.agent, w.goal_id, SimpleNamespace(tool_call_log_enabled=True), now=at(60)
        )
    assert today == found["jobs"]["launched"]


@pytest.mark.parametrize(("rows", "launched", "capped"), [(3, 2, True), (2, 2, False)])
async def test_the_shell_calls_read_for_launches_are_capped_newest_first_and_say_so(
    db, monkeypatch, rows, launched, capped
):
    monkeypatch.setattr(activity, "LAUNCH_ROWS_CAP", 2)
    w = await world_of(db)
    s = await w.session()
    launch = bash_input(f"{RUNNER} launch nous --prompt-file /workspace/notes/p.md")
    for minute in range(1, rows + 1):
        await w.call(at(minute), session_id=intent_sid(s.root_id), input_text=launch)
    assert (await w.read())["jobs"] == {"launched": launched, "last_launch_at": at(rows).isoformat(), "capped": capped}


async def test_the_read_runs_under_its_statement_limit(db):
    w = await world_of(db)
    await w.session()
    async with db.session() as session:
        await activity.read_activity(session, w.agent, w.goal_id, log_enabled=True)
        limit = (await session.execute(text("SHOW statement_timeout"))).scalar_one()
    assert limit == activity.STATEMENT_TIMEOUT == "5s"


async def test_no_stored_text_of_a_call_leaves_the_read(db):
    w = await world_of(db)
    s = await w.session()
    await w.call(
        at(1),
        session_id=intent_sid(s.root_id),
        input_text=bash_input(f"{RUNNER} launch nous --prompt-file /workspace/notes/INPUT-MARK.md"),
        reason="REASON-MARK",
        result_preview="PREVIEW-MARK",
        result_text="RESULT-MARK",
        error_text="ERROR-MARK",
        is_error=True,
    )
    found = await w.read()
    assert found["jobs"]["launched"] == 1 and found["calls"]["total"] == 1
    said = json.dumps(found)
    for mark in ("INPUT-MARK", "REASON-MARK", "PREVIEW-MARK", "RESULT-MARK", "ERROR-MARK", "prompt-file"):
        assert mark not in said


async def test_with_the_call_log_off_only_the_current_work_is_read(db):
    w = await world_of(db)
    s = await w.session()
    await w.call(at(1), session_id=intent_sid(s.root_id), input_text=bash_input(f"{RUNNER} launch nous"))
    found = await w.read(log=False)
    assert found["log_enabled"] is False
    assert (found["calls"], found["jobs"], found["last_call_at"]) == (None, None, None)
    assert (found["recent"], found["work_sessions"]) == ([], [])
    assert found["phase"] == "task" and found["current"]["first_task"] is True


async def test_the_work_sessions_are_the_newest_five_distinct_ids_and_each_opens_on_the_work_screen(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    child = await w.task(s.root_id, status="completed", started=at(1))
    dag = await w.workflow(s.root_id, status="completed", started=at(1))
    await w.call(at(2), session_id=intent_sid(s.root_id))
    for minute, sid in enumerate(["subtask-1", "subtask-2", "subtask-3", "subtask-4"], start=3):
        await w.call(at(minute), session_id=sid, subtask_id=child, work_kind="subtask")
    await w.call(at(8), session_id="step-a", dag_id=dag, work_kind="workflow_step")
    await w.call(at(9), session_id=intent_sid(s.root_id), dag_id=dag, work_kind="intention")
    await w.call(at(10), session_id=None, dag_id=dag, work_kind="workflow_step")
    found = await w.read()
    assert found["work_sessions"] == [
        {"id": intent_sid(s.root_id), "kind": "decisions"},
        {"id": "step-a", "kind": "workflow"},
        {"id": "subtask-4", "kind": "task"},
        {"id": "subtask-3", "kind": "task"},
        {"id": "subtask-2", "kind": "task"},
    ]
    async with db.session() as session:
        trace = await work_view_dashboard.get_trace(
            session, w.agent, "step-a", limit=5, after=None, model_calls_enabled=False
        )
    assert trace is not None
