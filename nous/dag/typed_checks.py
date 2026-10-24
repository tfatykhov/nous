"""Built-in completion checks for DAG nodes, run by the orchestrator like any shell ``completion_check``.

    python -m nous.dag.typed_checks pr <owner/repo> (--pr <n> | --head <branch>) [--job-file <path>]

``pr`` passes once the pull request exists, open or merged, because a merge can take longer than a node may wait.
``--head`` names it by its head branch, for a node created before the PR is: the newest PR on ``<owner>:<branch>``,
and only one opened since the node's DAG was created (the orchestrator passes ``NOUS_DAG_CREATED_AT``), so a
branch name an older PR used cannot settle the node first.
``--job-file`` is for a node that launches a Claude Code job (``claude-jobs/runner.sh launch``) whose work ends in
the PR: the absolute path of a file under the workspace (``NOUS_WORKSPACE_DIR``) holding the job's id
(``runner.sh launch``'s output, redirected to the file, will do). While the job is running an open PR is not done
yet; a merged one is. Once the job has ended and GitHub lists no such PR, the node fails (after a grace period,
since the job may have opened it a moment before it ended). A file that cannot be read, or holds no job id,
changes nothing, and neither does a 404, which GitHub also gives for what the token cannot see.
Exit codes follow the completion_check contract:
- 0: the PR exists (an open one only once its job, if one is named, is no longer running);
- 1: it was closed without a merge, the named job ended without it, or the arguments are wrong (all final);
- 2: not found yet, or GitHub could not answer (an outage never fails a node).

``GITHUB_TOKEN`` from the environment is used when set. Whether the work was accepted is not decided here: the task
scorer reads the PR from GitHub itself (``nous/observability/github_verify.py``).
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = 10.0
EXISTS, FAILED, PENDING = 0, 1, 2
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,200}")
_JOB_FILE = re.compile(r"/[A-Za-z0-9._/-]{1,300}")  # the scorer reads the check with the same charset
_JOB_ID = re.compile(r"job-\d{8}-\d{6}-[0-9a-f]{6,64}")  # runner.sh's job directory names
_JOB_ENDED = frozenset({"completed", "failed", "cancelled"})  # runner.sh's final status words
_PID = re.compile(r"[0-9]{1,10}")  # ASCII digits only: str.isdigit() also takes "²"
# A job writes its status after its last step, so a PR it opened just before may not be listed yet: an ended job
# fails the node only once its status is this old.
JOB_ENDED_GRACE_SECONDS = 300
DEFAULT_WORKSPACE = "/tmp/nous-workspace"  # Settings.workspace_dir's default; the orchestrator passes the real one


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse would exit 2, which reads as "still pending"
        raise _UsageError(message)


def _parser() -> _Parser:
    # No --help: it would exit 0, which reads as a passed check.
    parser = _Parser(prog="python -m nous.dag.typed_checks", add_help=False)
    sub = parser.add_subparsers(dest="kind", required=True)
    pr = sub.add_parser("pr", add_help=False)
    pr.add_argument("repo")
    which = pr.add_mutually_exclusive_group(required=True)
    which.add_argument("--pr", type=int)
    which.add_argument("--head")
    pr.add_argument("--job-file")
    return parser


def _workspace() -> str:
    return posixpath.normpath(os.environ.get("NOUS_WORKSPACE_DIR", "").strip() or DEFAULT_WORKSPACE)


def _job_file_error(path: str) -> str | None:
    """Why ``path`` is not a job file this check reads, or None: an absolute path under the workspace."""
    if not _JOB_FILE.fullmatch(path) or posixpath.normpath(path) != path:
        return f"--job-file must be an absolute path with no '..', got {path!r}"
    workspace = _workspace()
    try:
        inside = posixpath.commonpath([workspace, path]) == workspace and path != workspace
    except ValueError:  # a relative NOUS_WORKSPACE_DIR
        inside = False
    if not inside:
        return f"--job-file must be under the workspace {workspace}, got {path!r}"
    return None


def _read_head(path: str, size: int = 256) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(size).strip() or None
    except OSError:
        return None


def _gone(job_dir: str) -> bool:
    """Whether the job's worker is known to be gone: its pid file names no process. runner.sh writes the final status
    from that process, so a job killed with it (a container restart) stays "running" forever. Unknown is not gone."""
    if os.name != "posix":  # os.kill(pid, 0) is a probe only on POSIX
        return False
    for name in ("run_pid", "pid"):  # runner.sh status anchors on run.sh's pid, then the wrapper's
        word = _read_head(posixpath.join(job_dir, name), 32)
        # The job's own uid can write this file: only a plain pid a kernel can hold is probed.
        if word is not None and _PID.fullmatch(word) and 1 < int(word) < 2**22:
            try:
                os.kill(int(word), 0)
            except ProcessLookupError:
                return True
            except (OSError, OverflowError):
                return False
            return False
    return False


def job_state(job_file: str, *, now: float | None = None) -> tuple[str, float] | None:
    """(status word, seconds since it was written) of the job whose id ``job_file`` holds; None when not known.

    The file may hold ``runner.sh launch``'s whole output (it ends with the id): the last job id in it is the job, so
    a retry appended to the same file names the new job. Its status file ``<workspace>/claude-jobs/<id>/status`` must
    be readable, else None: a file the check cannot read never ends the node. A "running" job whose worker is gone
    reads "gone": it will never write a final word.
    """
    text = _read_head(job_file, 65536)
    found = _JOB_ID.findall(text) if text is not None else []
    if not found:
        return None
    job_dir = posixpath.join(_workspace(), "claude-jobs", found[-1])
    status_path = posixpath.join(job_dir, "status")
    status = _read_head(status_path)
    try:
        written = os.stat(status_path).st_mtime
    except OSError:
        return None
    if status is None:
        return None
    word = status.split()[0]
    if word == "running" and _gone(job_dir):
        word = "gone"
    return word, (time.time() if now is None else now) - written


def _client(transport: httpx.BaseTransport | None) -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # A renamed or transferred repository answers its old name with a redirect.
    return httpx.Client(
        base_url=GITHUB_API, headers=headers, timeout=REQUEST_TIMEOUT, transport=transport, follow_redirects=True
    )


def _dag_created_at() -> datetime | None:
    """When this node's DAG was created: the orchestrator passes it to every completion check."""
    value = os.environ.get("NOUS_DAG_CREATED_AT", "").strip()
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _opened(pull: dict) -> datetime | None:
    value = pull.get("created_at")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
    except ValueError:
        return None


def _state(pull: dict) -> tuple[int, str, str]:
    number = pull.get("number")
    if pull.get("merged_at"):
        return EXISTS, f"PR #{number} is merged", "merged"
    if pull.get("state") == "open":
        return EXISTS, f"PR #{number} is open", "open"
    return FAILED, f"PR #{number} was closed without a merge", "closed"


def check_pr(
    repo: str, number: int | None, head: str | None, *, transport: httpx.BaseTransport | None = None
) -> tuple[int, str, str]:
    """(exit code, one-line reason, answer) for the ``pr`` check. ``answer`` is what GitHub said: ``open``,
    ``merged``, ``closed``, ``absent`` (it listed the branch and this DAG's PR is not there, the one answer an ended
    job turns into a failure) or ``unknown`` (an outage, an unexpected answer, or a 404, which GitHub also gives for
    a repository or PR the token cannot see)."""
    try:
        with _client(transport) as client:
            if number is not None:
                response = client.get(f"/repos/{repo}/pulls/{number}")
                if response.status_code == 404:
                    return PENDING, f"no PR #{number} in {repo} yet", "unknown"
                response.raise_for_status()
                return _state(response.json())
            # A transferred repository's branches are under its new owner: filter on the repository as it is now.
            found = client.get(f"/repos/{repo}")
            if found.status_code == 404:
                return PENDING, f"no repository {repo} visible yet", "unknown"
            found.raise_for_status()
            repo = found.json().get("full_name") or repo
            owner = repo.split("/", 1)[0]
            response = client.get(
                f"/repos/{repo}/pulls",
                params={
                    "state": "all",
                    "head": f"{owner}:{head}",
                    "sort": "created",
                    "direction": "desc",
                    "per_page": 1,
                },
            )
            response.raise_for_status()
            pulls = response.json()
            if not pulls:
                return PENDING, f"no PR from {owner}:{head} in {repo} yet", "absent"
            since, opened = _dag_created_at(), _opened(pulls[0])
            if since is not None and (opened is None or opened < since):
                # The branch name was used by an older PR: this DAG's own PR is not there yet.
                answer = "absent" if opened is not None else "unknown"  # an unreadable time proves nothing
                return PENDING, f"no PR from {owner}:{head} in {repo} opened since this DAG started", answer
            return _state(pulls[0])
    except Exception as exc:  # noqa: BLE001 - anything unexpected is "ask again", never a definite failure
        # An outage, a non-JSON body, JSON of an unexpected shape: only a PR closed without a merge (or bad
        # arguments) may fail the node, so everything else reads as "not decided yet".
        return PENDING, f"GitHub did not answer as expected ({type(exc).__name__}); will retry", "unknown"


def main(argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if not _REPO.fullmatch(args.repo) or any(part in (".", "..") for part in args.repo.split("/")):
            raise _UsageError(f"repo must be owner/name, got {args.repo!r}")
        if args.pr is not None and args.pr < 1:
            raise _UsageError("--pr must be a positive number")
        if args.head is not None and not _BRANCH.fullmatch(args.head):
            raise _UsageError(f"--head is not a branch name: {args.head!r}")
        if args.job_file is not None and (problem := _job_file_error(args.job_file)) is not None:
            raise _UsageError(problem)
    except _UsageError as exc:
        print(f"typed_checks: {exc}", file=sys.stderr)
        return FAILED
    code, reason, answer = check_pr(args.repo, args.pr, args.head, transport=transport)
    try:
        job = job_state(args.job_file) if args.job_file is not None else None
    except Exception:  # noqa: BLE001 - files the job can write must never turn into a crash, which exits 1
        job = None
    if job is not None:
        status, age = job
        if answer == "open" and status == "running":  # a merged PR is never held
            code, reason = PENDING, f"{reason}; its job is still running"
        elif answer == "absent" and status in _JOB_ENDED and age >= JOB_ENDED_GRACE_SECONDS:
            code, reason = FAILED, f"{reason}, and its job ended ({status}) {int(age // 60)} min ago"
    print(reason, file=sys.stderr if code == FAILED else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
