"""Typed waits for lineage DAGs: the in-process evaluators (job and file here; pr below). Read-only, no shell."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from nous.dag import typed_checks
from nous.dag.schemas import FileWait, JobWait
from nous.dag.waits import evaluate_file, evaluate_job, evaluate_wait, resolve_workspace_file

JOB = "job-20261027-101500-ab12cd34"


def _job(workspace: Path, status: str | None, *, pid: int | None = None, pid_file: str = "run_pid") -> Path:
    """A job dir as runner.sh leaves it: a status word and the pid of the process that will write the final one."""
    job_dir = workspace / "claude-jobs" / JOB
    job_dir.mkdir(parents=True, exist_ok=True)
    if status is not None:
        (job_dir / "status").write_text(status + "\n")
    if pid is not None:
        (job_dir / pid_file).write_text(f"{pid}\n")
    return job_dir


def _snapshot(job_dir: Path) -> dict[str, tuple[int, int]]:
    if not job_dir.exists():
        return {}
    return {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in job_dir.iterdir()}


def _evaluate(workspace: Path, until: str = "finished"):
    job_dir = workspace / "claude-jobs" / JOB
    before = _snapshot(job_dir)
    outcome = evaluate_job(JobWait(kind="job", job_id=JOB, until=until), str(workspace))
    assert _snapshot(job_dir) == before  # read-only: nothing written, touched or added
    assert not (job_dir / "finished_at").exists()
    return outcome


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()  # reaped: its pid names no process
    return proc.pid


def test_a_running_job_with_a_live_worker_is_pending(tmp_path):
    _job(tmp_path, "running", pid=os.getpid())
    for until in ("finished", "completed"):
        outcome = _evaluate(tmp_path, until)
        assert (outcome.state, outcome.text) == ("pending", f"job {JOB} is running")


def test_a_running_job_whose_worker_is_gone_finished_failed(tmp_path, monkeypatch):
    _job(tmp_path, "running", pid=12345)
    monkeypatch.setattr(typed_checks, "_gone", lambda job_dir: True)
    finished = _evaluate(tmp_path, "finished")
    assert (finished.state, finished.text) == ("met", f"job {JOB} finished: failed (process died)")
    assert _evaluate(tmp_path, "completed").state == "impossible"


@pytest.mark.skipif(os.name != "posix", reason="os.kill(pid, 0) is a probe only on POSIX")
@pytest.mark.parametrize("pid_file", ["run_pid", "pid"])
def test_a_dead_pid_is_read_as_a_dead_worker(tmp_path, pid_file):
    _job(tmp_path, "running", pid=_dead_pid(), pid_file=pid_file)
    outcome = _evaluate(tmp_path)
    assert (outcome.state, outcome.text) == ("met", f"job {JOB} finished: failed (process died)")


@pytest.mark.parametrize(
    ("status", "finished", "completed"),
    [
        ("completed", "met", "met"),
        ("completed 2026-10-27T10:20:00Z", "met", "met"),  # the first word counts
        ("failed", "met", "impossible"),
        ("cancelled", "met", "impossible"),
    ],
)
def test_a_final_status(tmp_path, status, finished, completed):
    _job(tmp_path, status)
    word = status.split()[0]
    for until, state in (("finished", finished), ("completed", completed)):
        outcome = _evaluate(tmp_path, until)
        assert (outcome.state, outcome.text) == (state, f"job {JOB} finished: {word}")


@pytest.mark.parametrize("status", [None, ""])
def test_a_job_with_no_readable_status_is_pending(tmp_path, status):
    if status is not None:
        _job(tmp_path, status)
    outcome = _evaluate(tmp_path)
    assert (outcome.state, outcome.text) == ("pending", f"job {JOB}: status not readable yet")


def test_a_status_word_is_one_short_line(tmp_path):
    _job(tmp_path, "\x1b[31m" + "x" * 500)
    outcome = _evaluate(tmp_path)
    assert outcome.state == "met"
    assert "\x1b" not in outcome.text and len(outcome.text) < 120


def test_job_state_for_answers_as_job_state(tmp_path, monkeypatch):
    _job(tmp_path, "failed")
    id_file = tmp_path / "dag" / "job.id"
    id_file.parent.mkdir()
    id_file.write_text(JOB + "\n")
    monkeypatch.setenv("NOUS_WORKSPACE_DIR", str(tmp_path))
    by_file = typed_checks.job_state(str(id_file), now=1e10)
    by_id = typed_checks.job_state_for(JOB, typed_checks._workspace(), now=1e10)
    assert by_file == by_id and by_id is not None and by_id[0] == "failed"
    assert typed_checks.job_state_for("job-20261027-101500-00000000", str(tmp_path)) is None


# ---------------------------------------------------------------------------
# file
# ---------------------------------------------------------------------------


def _file(path: str, nonempty: bool = False) -> FileWait:
    return FileWait(kind="file", path=path, nonempty=nonempty)


def test_a_missing_file_is_pending_and_an_existing_one_met(tmp_path):
    assert evaluate_file(_file("out/r.md"), str(tmp_path)).state == "pending"
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "r.md").write_text("x")
    outcome = evaluate_file(_file("out/r.md"), str(tmp_path))
    assert (outcome.state, outcome.text) == ("met", "file out/r.md exists")
    absolute = evaluate_file(_file(str(tmp_path / "out" / "r.md")), str(tmp_path))
    assert (absolute.state, absolute.text) == ("met", "file out/r.md exists")


def test_nonempty_waits_for_content(tmp_path):
    target = tmp_path / "r.md"
    target.write_text("")
    assert evaluate_file(_file("r.md", nonempty=True), str(tmp_path)).state == "pending"
    assert evaluate_file(_file("r.md"), str(tmp_path)).state == "met"
    target.write_text("x")
    outcome = evaluate_file(_file("r.md", nonempty=True), str(tmp_path))
    assert (outcome.state, outcome.text) == ("met", "file r.md exists and is not empty")


def test_nonempty_on_a_directory_waits_for_an_entry(tmp_path):
    (tmp_path / "d").mkdir()
    assert evaluate_file(_file("d", nonempty=True), str(tmp_path)).state == "pending"
    (tmp_path / "d" / "f").write_text("")
    assert evaluate_file(_file("d", nonempty=True), str(tmp_path)).state == "met"


@pytest.mark.parametrize("path", ["../x", "a/.b/c", "/etc/hosts", "."])
def test_a_path_outside_the_workspace_or_hidden_is_impossible(tmp_path, path):
    (tmp_path / "a" / ".b").mkdir(parents=True)
    (tmp_path / "a" / ".b" / "c").write_text("x")
    assert resolve_workspace_file(path, str(tmp_path)) is None
    outcome = evaluate_file(FileWait.model_construct(kind="file", path=path, nonempty=False), str(tmp_path))
    assert outcome.state == "impossible"
    assert outcome.text.endswith("not a path this wait may read (outside the workspace or hidden)")


def test_the_workspace_root_itself_is_no_file_to_wait_for(tmp_path):
    assert resolve_workspace_file(str(tmp_path), str(tmp_path)) is None


def test_a_sibling_directory_sharing_the_prefix_is_outside(tmp_path):
    workspace = tmp_path / "ws"
    sibling = tmp_path / "ws2"
    workspace.mkdir()
    sibling.mkdir()
    (sibling / "x").write_text("x")
    assert resolve_workspace_file(str(sibling / "x"), str(workspace)) is None


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


def test_a_symlink_out_of_the_workspace_is_impossible(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("x")
    _symlink(workspace / "link", outside)
    assert evaluate_file(_file("link"), str(workspace)).state == "impossible"


def test_a_symlink_into_a_hidden_directory_is_impossible(tmp_path):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id").write_text("x")
    _symlink(tmp_path / "link", tmp_path / ".ssh" / "id")
    assert evaluate_file(_file("link"), str(tmp_path)).state == "impossible"


def test_a_symlink_to_an_ordinary_workspace_file_is_followed(tmp_path):
    (tmp_path / "real.md").write_text("x")
    _symlink(tmp_path / "link", tmp_path / "real.md")
    outcome = evaluate_file(_file("link"), str(tmp_path))
    assert (outcome.state, outcome.text) == ("met", "file real.md exists")


# ---------------------------------------------------------------------------
# evaluate_wait
# ---------------------------------------------------------------------------


async def test_evaluate_wait_refuses_what_it_cannot_parse(tmp_path):
    outcome = await evaluate_wait(
        {"kind": "shell", "cmd": "x"}, workspace=str(tmp_path), github=None, default_repo=None
    )
    assert outcome.state == "impossible"
    assert outcome.text.startswith("wait_for is not valid")
    assert "\n" not in outcome.text


async def test_evaluate_wait_runs_job_and_file_waits(tmp_path):
    _job(tmp_path, "completed")
    job = await evaluate_wait(
        {"kind": "job", "job_id": JOB, "until": "completed"}, workspace=str(tmp_path), github=None, default_repo=None
    )
    assert (job.state, job.text) == ("met", f"job {JOB} finished: completed")
    file = await evaluate_wait(
        {"kind": "file", "path": "nope"}, workspace=str(tmp_path), github=None, default_repo=None
    )
    assert file.state == "pending"
