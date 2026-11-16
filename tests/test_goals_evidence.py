"""F114 goals (PR 1c): the evidence a goal session cites to close a plan step is checked against its own chain.

Each kind has its accepted case and its refusals: a ref of another session, another goal, another agent, a row older
than the session, work that has not completed, a file outside the session's own directory.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from types import SimpleNamespace

import pytest
from f099_support import env_factory, make_child, make_dag, runner_env  # noqa: F401
from goals_support import active, started, world

from nous.goals import evidence
from nous.storage.models import ExecutionLedgerEntry, GoalSession, Intention

pytestmark = pytest.mark.postgres_only


async def _session(runner_env, tmp_path, **settings) -> SimpleNamespace:  # noqa: F811
    w = await world(runner_env, bind=False, workspace_dir=str(tmp_path), **settings)
    goal = await active(w)
    root, row = await started(w, goal)
    w.goal, w.root, w.row = goal, root, row
    w.scope = evidence.Scope(agent_id=w.agent, root_id=root.id, started_at=row.started_at, workspace_dir=str(tmp_path))
    w.dir = tmp_path / "intentions" / str(root.id)
    return w


async def _check(w: SimpleNamespace, ref: str, scope: evidence.Scope | None = None) -> evidence.Checked:
    async with w.db.session() as s:
        return await evidence.check(s, scope or w.scope, ref)


async def _ledger(w: SimpleNamespace, *, agent: str | None = None, entry_id: uuid.UUID | None = None, **over):
    values = {
        "agent_id": agent or w.agent,
        "session_id": f"intent-{w.root.id}",
        "context_kind": "continuation",
        "tool_name": "write_file",
        "side_effect_type": "write",
        "key_args": {},
        "status": "success",
        **over,
    }
    async with w.db.session() as s:
        row = ExecutionLedgerEntry(id=entry_id or uuid.uuid4(), **values)
        s.add(row)
        await s.commit()
        return row.id


async def _other_session(w: SimpleNamespace) -> tuple[Intention, GoalSession]:
    """A session of a second goal of the same agent."""
    other = await active(w, title="Sort the photos")
    return await started(w, other)


# ---- ledger ------------------------------------------------------------------------------------------------------


async def test_a_successful_call_of_the_sessions_chain_is_evidence(runner_env, tmp_path):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    by_turn = await _ledger(w)
    by_subtask = await _ledger(w, session_id="subtask-x", subtask_id=uuid.UUID(w.root.source_id))
    assert (await _check(w, f"ledger:{by_turn}")).ref == f"ledger:{by_turn}"
    assert (await _check(w, f"LEDGER:{str(by_subtask).upper()}")).ref == f"ledger:{by_subtask}"


async def test_a_call_of_another_session_another_agent_or_before_the_start_is_refused(runner_env, tmp_path):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    other_root, _ = await _other_session(w)
    refused = [
        await _ledger(w, session_id=f"intent-{other_root.id}"),  # another goal's session
        await _ledger(w, session_id="subtask-x", subtask_id=uuid.UUID(other_root.source_id)),
        await _ledger(w, agent="someone-else"),  # another agent
        await _ledger(w, status="error"),  # it did not succeed
        await _ledger(w, created_at=w.row.started_at.replace(year=w.row.started_at.year - 1)),  # older than the start
    ]
    for entry_id in refused:
        checked = await _check(w, f"ledger:{entry_id}")
        assert checked.ref is None and "is not a successful recorded call of this work" in checked.refusal


async def test_a_dag_nodes_call_counts_when_the_dag_is_the_sessions(runner_env, tmp_path):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    dag, _store = await make_dag(w.env, parent=w.root)
    elsewhere, _store = await make_dag(w.env)  # a DAG of no goal
    mine = await _ledger(w, session_id="dag-node", dag_id=dag.id)
    theirs = await _ledger(w, session_id="dag-node", dag_id=elsewhere.id)
    assert (await _check(w, f"ledger:{mine}")).ref == f"ledger:{mine}"
    assert (await _check(w, f"ledger:{theirs}")).ref is None


# ---- subtask and dag --------------------------------------------------------------------------------------------


async def test_a_completed_subtask_of_the_lineage_is_evidence_and_an_unfinished_one_is_not(runner_env, tmp_path):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    first = uuid.UUID(w.root.source_id)
    assert (await _check(w, f"subtask:{first}")).ref is None  # still pending
    await w.env.heart.subtasks.complete(first, "done", final_outcome="completed")
    assert (await _check(w, f"subtask:{first}")).ref == f"subtask:{first}"
    child = await make_child(w.env, w.root)  # a step the chain spawned
    child_id = uuid.UUID(child.source_id)
    await w.env.heart.subtasks.fail(child_id, "boom")
    assert "is not a completed subtask of this work" in (await _check(w, f"subtask:{child_id}")).refusal


async def test_a_subtask_of_another_goal_is_refused(runner_env, tmp_path):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    other_root, _ = await _other_session(w)
    theirs = uuid.UUID(other_root.source_id)
    await w.env.heart.subtasks.complete(theirs, "done", final_outcome="completed")
    assert (await _check(w, f"subtask:{theirs}")).ref is None


async def test_a_completed_dag_of_the_lineage_is_evidence_and_another_lineages_is_not(runner_env, tmp_path):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    mine, _ = await make_dag(w.env, parent=w.root)
    running, _ = await make_dag(w.env, parent=w.root, status="running")
    other_root, _ = await _other_session(w)
    theirs, _ = await make_dag(w.env, parent=other_root)
    assert (await _check(w, f"dag:{mine.id}")).ref == f"dag:{mine.id}"
    assert "is not a completed DAG of this work" in (await _check(w, f"dag:{running.id}")).refusal
    assert (await _check(w, f"dag:{theirs.id}")).ref is None


# ---- prefixes and forms -----------------------------------------------------------------------------------------


async def test_an_eight_character_prefix_is_expanded_when_it_names_one_ref(runner_env, tmp_path):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    entry_id = await _ledger(w)
    assert (await _check(w, f"ledger:{str(entry_id)[:8]}")).ref == f"ledger:{entry_id}"


async def test_an_eight_character_prefix_that_names_two_refs_asks_for_the_full_id(runner_env, tmp_path):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    head = uuid.uuid4().hex[:8]
    for _ in range(2):
        await _ledger(w, entry_id=uuid.UUID(head + uuid.uuid4().hex[8:]))
    checked = await _check(w, f"ledger:{head}")
    assert checked.ref is None and "matches more than one ledger" in checked.refusal


@pytest.mark.parametrize(
    "ref", ["", "   ", "ledger", "pr:123", "job:job-1", "ledger:not-an-id", "subtask:1234", 42, None, ["ledger:x"]]
)
async def test_a_malformed_ref_is_refused_with_the_accepted_forms(runner_env, tmp_path, ref):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    checked = await _check(w, ref)
    assert checked.ref is None and checked.refusal


async def test_an_unknown_kind_names_the_forms_it_accepts(runner_env, tmp_path):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    refusal = (await _check(w, "pr:123")).refusal
    assert "ledger:<id>" in refusal and "subtask:<id>" in refusal and "file:intentions/" in refusal


# ---- files ------------------------------------------------------------------------------------------------------


async def test_a_file_in_the_sessions_own_directory_is_evidence(runner_env, tmp_path):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    w.dir.mkdir(parents=True)
    (w.dir / "notes.md").write_text("merged", encoding="utf-8")
    want = f"file:intentions/{w.root.id}/notes.md"
    assert (await _check(w, want)).ref == want
    assert (await _check(w, f"file:{w.dir / 'notes.md'}")).ref == want  # an absolute path is the same file
    assert (await _check(w, f"file:intentions/{w.root.id}/sub/../notes.md")).ref == want


async def test_a_file_outside_the_sessions_directory_or_missing_or_old_is_refused(runner_env, tmp_path):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    other_root, _ = await _other_session(w)
    w.dir.mkdir(parents=True)
    (tmp_path / "outside.md").write_text("x", encoding="utf-8")
    theirs = tmp_path / "intentions" / str(other_root.id)
    theirs.mkdir(parents=True)
    (theirs / "notes.md").write_text("x", encoding="utf-8")
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "notes.md").write_text("x", encoding="utf-8")
    old = w.dir / "old.md"
    old.write_text("x", encoding="utf-8")
    long_ago = time.time() - 3 * 86400
    os.utime(old, (long_ago, long_ago))
    for ref in (
        f"file:intentions/{w.root.id}/../../outside.md",
        f"file:{tmp_path / 'outside.md'}",
        f"file:intentions/{other_root.id}/notes.md",  # another goal's session
        "file:research/notes.md",  # an owner's shared directory
        f"file:intentions/{w.root.id}/missing.md",
        f"file:intentions/{w.root.id}",  # the directory itself
        f"file:intentions/{w.root.id}/old.md",  # older than the session
        f"file:intentions/{w.root.id}/notes\x00.md",
    ):
        checked = await _check(w, ref)
        assert checked.ref is None and "is not a file this work wrote" in checked.refusal, ref


@pytest.mark.skipif(sys.platform == "win32", reason="creating a symlink needs a privilege Windows rarely grants")
async def test_a_symlink_out_of_the_sessions_directory_is_refused(runner_env, tmp_path):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    w.dir.mkdir(parents=True)
    (tmp_path / "secret.md").write_text("x", encoding="utf-8")
    (w.dir / "link.md").symlink_to(tmp_path / "secret.md")
    assert (await _check(w, f"file:intentions/{w.root.id}/link.md")).ref is None


# ---- what the block lists ---------------------------------------------------------------------------------------


async def test_the_citeable_list_has_the_sessions_refs_with_full_ids_and_nothing_else(runner_env, tmp_path):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    first = uuid.UUID(w.root.source_id)
    await w.env.heart.subtasks.complete(first, "done", final_outcome="completed")
    dag, _ = await make_dag(w.env, parent=w.root)
    entry_id = await _ledger(w)
    other_root, _ = await _other_session(w)
    await _ledger(w, session_id=f"intent-{other_root.id}")
    w.dir.mkdir(parents=True)
    (w.dir / "notes.md").write_text("merged", encoding="utf-8")
    async with w.db.session() as s:
        listed = await evidence.citeable(s, w.scope, limit=10)
    assert [c.ref for c in listed] == [
        f"subtask:{first}",
        f"dag:{dag.id}",
        f"ledger:{entry_id}",
        f"file:intentions/{w.root.id}/notes.md",
    ]
    for item in listed:  # every listed ref passes the check
        assert (await _check(w, item.ref)).ref == item.ref


async def test_the_citeable_list_looks_at_a_bounded_number_of_files(runner_env, tmp_path, monkeypatch):  # noqa: F811
    w = await _session(runner_env, tmp_path)
    w.dir.mkdir(parents=True)
    for name in ("a.md", "b.md", "c.md"):
        (w.dir / name).write_text("x", encoding="utf-8")
    monkeypatch.setattr(evidence, "FILES_SCANNED", 2)
    async with w.db.session() as s:
        listed = await evidence.citeable(s, w.scope, limit=10)
    assert [c.ref for c in listed] == [f"file:intentions/{w.root.id}/a.md", f"file:intentions/{w.root.id}/b.md"]
    assert (await _check(w, f"file:intentions/{w.root.id}/c.md")).ref is not None  # still citeable by its path
