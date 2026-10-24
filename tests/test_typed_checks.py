"""The built-in PR completion check: exit 0 once the PR exists, 1 when it is final, 2 while it may still come."""

from __future__ import annotations

import os
import sys
import time

import httpx
import pytest

from nous.dag import typed_checks


def transport(handler):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(handle), seen


def pull(state="open", merged_at=None, number=5):
    return {"number": number, "state": state, "merged_at": merged_at}


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(200, json=pull()), 0),  # open: the PR exists
        (httpx.Response(200, json=pull("closed", "2026-10-19T00:00:00Z")), 0),  # merged
        (httpx.Response(200, json=pull("closed")), 1),  # closed without a merge: final
        (httpx.Response(404, json={}), 2),  # not there yet
        (httpx.Response(500, json={}), 2),  # GitHub failing never fails the node
        (httpx.Response(403, json={"message": "rate limited"}), 2),
    ],
)
def test_pr_by_number(response, code, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    mock, seen = transport(lambda request: response)
    assert typed_checks.main(["pr", "tfatykhov/nous", "--pr", "5"], transport=mock) == code
    assert seen[0].url.path == "/repos/tfatykhov/nous/pulls/5"
    assert "authorization" not in seen[0].headers


def test_a_transport_error_is_pending():
    def boom(request):
        raise httpx.ConnectError("down", request=request)

    mock, _ = transport(boom)
    assert typed_checks.main(["pr", "tfatykhov/nous", "--pr", "5"], transport=mock) == 2


def _repo_then(listing, full_name="tfatykhov/nous"):
    """GitHub for a --head check: the repository lookup, then the branch's PR listing."""

    def handle(request):
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"full_name": full_name})
        return listing(request)

    return handle


def test_pr_by_head_branch_takes_the_newest_pr_on_it(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    mock, seen = transport(_repo_then(lambda request: httpx.Response(200, json=[pull(number=9)])))
    assert typed_checks.main(["pr", "tfatykhov/nous", "--head", "fix/thing"], transport=mock) == 0
    request = seen[1]
    assert request.url.path == "/repos/tfatykhov/nous/pulls"
    assert dict(request.url.params) == {
        "state": "all",
        "head": "tfatykhov:fix/thing",
        "sort": "created",
        "direction": "desc",
        "per_page": "1",
    }
    assert request.headers["authorization"] == "Bearer t0ken"


def test_no_pr_on_the_branch_yet_is_pending():
    mock, _ = transport(_repo_then(lambda request: httpx.Response(200, json=[])))
    assert typed_checks.main(["pr", "tfatykhov/nous", "--head", "fix/thing"], transport=mock) == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["pr", "not-a-repo", "--pr", "5"],
        ["pr", "tfatykhov/nous", "--pr", "0"],
        ["pr", "tfatykhov/nous", "--pr", "five"],
        ["pr", "tfatykhov/nous", "--head", "has space"],
        ["pr", "tfatykhov/nous"],  # neither --pr nor --head
        ["pr", "tfatykhov/nous", "--pr", "5", "--head", "b"],  # both
        ["merged", "tfatykhov/nous", "--pr", "5"],  # no such check
        ["--help"],  # would exit 0 and read as a pass
        ["pr", "--help"],
    ],
)
def test_wrong_arguments_fail_the_check_and_never_call_github(argv):
    def unexpected(request):
        raise AssertionError("GitHub must not be called")

    mock, _ = transport(unexpected)
    assert typed_checks.main(argv, transport=mock) == 1


def test_a_path_segment_is_not_a_repository():
    def unexpected(request):
        raise AssertionError("GitHub must not be called")

    mock, _ = transport(unexpected)
    assert typed_checks.main(["pr", "../..", "--pr", "5"], transport=mock) == 1


@pytest.mark.parametrize(
    ("opened", "state", "merged_at", "code"),
    [
        ("2026-10-18T00:00:00Z", "open", None, 2),  # an older PR on a reused branch name: not this DAG's PR yet
        ("2026-10-18T00:00:00Z", "closed", None, 2),  # an older PR closed unmerged must not fail this node either
        ("2026-10-19T13:00:00Z", "open", None, 0),  # opened after the DAG: this DAG's PR
    ],
)
def test_head_ignores_prs_opened_before_the_dag(opened, state, merged_at, code, monkeypatch):
    monkeypatch.setenv("NOUS_DAG_CREATED_AT", "2026-10-19T12:00:00+00:00")
    body = [{"number": 9, "state": state, "merged_at": merged_at, "created_at": opened}]
    mock, _ = transport(_repo_then(lambda request: httpx.Response(200, json=body)))
    assert typed_checks.main(["pr", "tfatykhov/nous", "--head", "fix/thing"], transport=mock) == code


def test_a_renamed_repository_is_followed():
    def handle(request):
        if request.url.path == "/repos/old-owner/old-name/pulls/5":
            return httpx.Response(301, headers={"Location": "https://api.github.com/repositories/42/pulls/5"})
        return httpx.Response(200, json=pull())

    mock, seen = transport(handle)
    assert typed_checks.main(["pr", "old-owner/old-name", "--pr", "5"], transport=mock) == 0
    assert [r.url.path for r in seen] == ["/repos/old-owner/old-name/pulls/5", "/repositories/42/pulls/5"]


def test_head_on_a_transferred_repository_filters_on_its_new_owner():
    mock, seen = transport(_repo_then(lambda request: httpx.Response(200, json=[pull(number=9)]), "new-owner/nous"))
    assert typed_checks.main(["pr", "old-owner/nous", "--head", "fix/thing"], transport=mock) == 0
    assert seen[1].url.path == "/repos/new-owner/nous/pulls"
    assert dict(seen[1].url.params)["head"] == "new-owner:fix/thing"


@pytest.mark.parametrize(
    ("argv", "body"),
    [
        (["pr", "tfatykhov/nous", "--pr", "5"], [1, 2, 3]),  # a list where a PR object belongs
        (["pr", "tfatykhov/nous", "--pr", "5"], "a string"),
        (["pr", "tfatykhov/nous", "--head", "fix/thing"], ["not", "an", "object"]),  # the repository lookup
    ],
)
def test_an_unexpected_answer_is_pending_never_a_failure(argv, body):
    mock, _ = transport(lambda request: httpx.Response(200, json=body))
    assert typed_checks.main(argv, transport=mock) == 2


# --job-file: a node that launches a Claude Code job whose work ends in the PR


@pytest.mark.parametrize(
    "job_file",
    [
        "job.id",  # relative
        "/tmp/elsewhere/job.id",  # outside the workspace
        "/tmp/nous-workspace/../etc/job.id",  # through '..'
        "/tmp/nous-workspace/./job.id",  # not normalized
        "/tmp/nous-workspace",  # the workspace itself
        "/tmp/nous-workspace/a b.id",  # a blank: the scorer would not read it as the PR check
        "/tmp/nous-workspace/$J",
    ],
)
def test_a_job_file_must_be_an_absolute_path_under_the_workspace(job_file, monkeypatch):
    monkeypatch.setenv("NOUS_WORKSPACE_DIR", "/tmp/nous-workspace")

    def unexpected(request):
        raise AssertionError("GitHub must not be called")

    mock, _ = transport(unexpected)
    argv = ["pr", "tfatykhov/nous", "--head", "fix/thing", "--job-file", job_file]
    assert typed_checks.main(argv, transport=mock) == 1


JOB = "job-20261024-002210-009d4cf6"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="job files are absolute POSIX paths")


def _job(workspace, status, age_seconds=600, job_id=JOB, id_text=None, pid=None):
    """A launched job as runner.sh leaves it: its id in a file the DAG wrote, its status word (and the pid of the
    process that will write the final one) in its own dir."""
    id_file = workspace / "dag-1" / "job.id"
    id_file.parent.mkdir(parents=True, exist_ok=True)
    id_file.write_text(id_text if id_text is not None else job_id + "\n")
    job_dir = workspace / "claude-jobs" / job_id
    if status is not None:
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "status").write_text(status + "\n")
        written = time.time() - age_seconds
        os.utime(job_dir / "status", (written, written))
    if pid is not None:
        (job_dir / "run_pid").write_text(f"{pid}\n")
    return str(id_file)


def _head_check(job_file, listing):
    mock, _ = transport(_repo_then(listing))
    return typed_checks.main(["pr", "tfatykhov/nous", "--head", "fix/thing", "--job-file", job_file], transport=mock)


OPEN = lambda request: httpx.Response(200, json=[pull(number=9)])  # noqa: E731
NONE_YET = lambda request: httpx.Response(200, json=[])  # noqa: E731
CLOSED = lambda request: httpx.Response(200, json=[pull("closed", number=9)])  # noqa: E731
MERGED = lambda request: httpx.Response(200, json=[pull("closed", "2026-10-24T00:00:00Z", number=9)])  # noqa: E731
DOWN = lambda request: httpx.Response(500, json={})  # noqa: E731


@posix_only
@pytest.mark.parametrize(
    ("listing", "status", "age_seconds", "code"),
    [
        (OPEN, "running", 600, 2),  # the PR is there but its job is still working on it
        (OPEN, "completed", 600, 0),
        (OPEN, "failed", 5, 0),  # the PR exists: whether it was good work is the scorer's question
        (OPEN, "unknown", 600, 0),  # only "running" holds a PR that exists
        (NONE_YET, "completed", 600, 1),  # the job ended and opened no PR: final
        (NONE_YET, "cancelled", 600, 1),
        (NONE_YET, "failed", 60, 2),  # it ended a minute ago: GitHub may not list its PR yet
        (NONE_YET, "completed", typed_checks.JOB_ENDED_GRACE_SECONDS - 5, 2),
        (NONE_YET, "completed", typed_checks.JOB_ENDED_GRACE_SECONDS + 5, 1),
        (MERGED, "running", 600, 0),  # a merged PR is done, whatever the job still does
        (NONE_YET, "running", 600, 2),
        (DOWN, "completed", 600, 2),  # GitHub failing never fails the node, job or not
        (CLOSED, "running", 600, 1),  # closed without a merge stays final
    ],
)
def test_a_job_file_waits_for_the_job_and_fails_once_it_ended_with_no_pr(
    listing, status, age_seconds, code, tmp_path, monkeypatch
):
    monkeypatch.setenv("NOUS_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("NOUS_DAG_CREATED_AT", raising=False)
    assert _head_check(_job(tmp_path, status, age_seconds), listing) == code


@posix_only
@pytest.mark.parametrize("case", ["no id file", "not a job id", "no status file"])
@pytest.mark.parametrize(("listing", "code"), [(OPEN, 0), (NONE_YET, 2)])
def test_a_job_file_the_check_cannot_read_changes_nothing(case, listing, code, tmp_path, monkeypatch):
    monkeypatch.setenv("NOUS_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("NOUS_DAG_CREATED_AT", raising=False)
    if case == "no id file":
        job_file = str(tmp_path / "dag-1" / "job.id")
    elif case == "not a job id":
        job_file = _job(tmp_path, "completed", job_id="not-a-job")
    else:
        job_file = _job(tmp_path, None)
    assert _head_check(job_file, listing) == code


@posix_only
@pytest.mark.parametrize(
    "argv_tail",
    [
        ["--head", "fix/thing"],  # the repository lookup 404s
        ["--pr", "5"],  # the PR lookup 404s
    ],
)
def test_a_404_never_ends_the_node_even_after_its_job_ended(argv_tail, tmp_path, monkeypatch):
    """GitHub answers 404 for a repository or PR the token cannot see, too: that is no answer that the PR is absent."""
    monkeypatch.setenv("NOUS_WORKSPACE_DIR", str(tmp_path))
    mock, _ = transport(lambda request: httpx.Response(404, json={}))
    argv = ["pr", "tfatykhov/nous", *argv_tail, "--job-file", _job(tmp_path, "completed", 3600)]
    assert typed_checks.main(argv, transport=mock) == 2


@posix_only
def test_an_unreadable_pr_time_proves_no_absence(tmp_path, monkeypatch):
    monkeypatch.setenv("NOUS_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("NOUS_DAG_CREATED_AT", "2026-10-19T12:00:00+00:00")
    body = [{"number": 9, "state": "open", "merged_at": None, "created_at": "garbage"}]
    assert _head_check(_job(tmp_path, "completed", 3600), lambda request: httpx.Response(200, json=body)) == 2
    older = [{"number": 9, "state": "open", "merged_at": None, "created_at": "2026-10-18T00:00:00Z"}]
    assert _head_check(_job(tmp_path, "completed", 3600), lambda request: httpx.Response(200, json=older)) == 1


@posix_only
def test_the_id_file_may_hold_the_launch_output(tmp_path, monkeypatch):
    """`runner.sh launch ... > job.id` writes its log lines too; the id they name is the job."""
    monkeypatch.setenv("NOUS_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("NOUS_DAG_CREATED_AT", raising=False)
    output = (
        f"[2026-10-24 00:22:10 UTC] Launching job {JOB} in worktree /tmp/w\n"
        "[2026-10-24 00:22:10 UTC] Model: opus | Effort: high | Branch: fix/thing (base: main)\n"
        f"[2026-10-24 00:22:12 UTC] Job {JOB} started (PID: 4242)\n{JOB}\n"
    )
    assert _head_check(_job(tmp_path, "completed", 600, id_text=output), NONE_YET) == 1
    assert _head_check(_job(tmp_path, "running", 600, id_text=output), OPEN) == 2


@posix_only
def test_a_running_job_whose_worker_is_gone_holds_nothing(tmp_path, monkeypatch):
    """A job killed with its runner (a container restart) says "running" for ever: an open PR it made is done, and
    no PR waits for the node's own timeout, as before the job file."""
    import subprocess

    monkeypatch.setenv("NOUS_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("NOUS_DAG_CREATED_AT", raising=False)
    child = subprocess.Popen(["true"])
    child.wait()  # reaped: its pid names no process now
    assert _head_check(_job(tmp_path, "running", 600, pid=child.pid), OPEN) == 0
    assert _head_check(_job(tmp_path, "running", 600, pid=child.pid), NONE_YET) == 2
    assert _head_check(_job(tmp_path, "running", 600, pid=os.getpid()), OPEN) == 2  # alive: still working
