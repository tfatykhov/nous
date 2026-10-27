"""Typed waits for lineage DAGs: evaluate a wait node's ``wait_for``, in-process and read-only.

The orchestrator calls :func:`evaluate_wait` on every poll of a wait node. Nothing here runs a shell or a
subprocess, and nothing writes: a job is read from its status file (never through ``runner.sh status``, which
writes), a file is looked up under the workspace, and a pull request is read from GitHub with GET requests through
the task scorer's ``GitHubReader``. A GitHub error or rate limit is never a failure: the wait stays pending.

Each outcome is ``met`` (the node completes), ``impossible`` (it fails) or ``pending`` (poll again), with a one-line
text that becomes the node's result or error, and so reaches the next node's prompt: text from outside (a job's
status word, a path) is cut to one short printable line.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from nous.dag import typed_checks
from nous.dag.schemas import PR_REPO_RE, FileWait, JobWait, PrWait, file_wait_path_problem, parse_wait_for
from nous.observability.github_verify import GitHubUnavailable

if TYPE_CHECKING:
    from nous.observability.github_verify import GitHubReader

WaitState = Literal["met", "impossible", "pending"]


@dataclass(frozen=True)
class WaitOutcome:
    """What one poll of a wait found. ``text`` is one line."""

    state: WaitState
    text: str


def one_line(text: object, limit: int = 200) -> str:
    """``text`` as one printable line of at most ``limit`` characters."""
    clean = "".join(ch for ch in str(text) if ch.isprintable())
    return clean if len(clean) <= limit else clean[: limit - 3] + "..."


def resolve_workspace_file(path: str, workspace: str) -> str | None:
    """The real path (symlinks followed) of ``path``, absolute or relative to ``workspace``, when it lies strictly
    under the workspace with no hidden component, as given or as resolved; otherwise None."""
    if file_wait_path_problem(path) is not None:
        return None
    try:
        root = os.path.realpath(workspace)
        real = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
        if real == root or os.path.commonpath([root, real]) != root:
            return None
        relative = os.path.relpath(real, root)
    except (OSError, ValueError):  # ValueError: another drive on Windows
        return None
    if any(part.startswith(".") for part in relative.split(os.sep)):
        return None
    return real


def evaluate_job(wait: JobWait, workspace: str, *, now: float | None = None) -> WaitOutcome:
    """A Claude Code job, from its status file. A "running" job whose worker is gone finished, failed."""
    state = typed_checks.job_state_for(wait.job_id, workspace, now=now)
    if state is None:
        return WaitOutcome("pending", f"job {wait.job_id}: status not readable yet")
    word = state[0]
    if word == "running":
        return WaitOutcome("pending", f"job {wait.job_id} is running")
    outcome = "failed (process died)" if word == "gone" else one_line(word, 40)
    text = f"job {wait.job_id} finished: {outcome}"
    if wait.until == "completed" and word != "completed":
        return WaitOutcome("impossible", text)
    return WaitOutcome("met", text)


def evaluate_file(wait: FileWait, workspace: str) -> WaitOutcome:
    """A file or directory under the workspace; with ``nonempty``, a non-empty file or a directory with an entry."""
    real = resolve_workspace_file(wait.path, workspace)
    if real is None:
        return WaitOutcome(
            "impossible",
            f"file {one_line(wait.path, 100)}: not a path this wait may read (outside the workspace or hidden)",
        )
    name = one_line(os.path.relpath(real, os.path.realpath(workspace)).replace(os.sep, "/"), 120)
    try:
        info = os.stat(real)
    except OSError:
        return WaitOutcome("pending", f"file {name} does not exist yet")
    if not wait.nonempty:
        return WaitOutcome("met", f"file {name} exists")
    if stat.S_ISDIR(info.st_mode):
        try:
            with os.scandir(real) as entries:
                empty = next(entries, None) is None
        except OSError:
            return WaitOutcome("pending", f"file {name} cannot be listed yet")
    else:
        empty = info.st_size == 0
    if empty:
        return WaitOutcome("pending", f"file {name} exists but is empty")
    return WaitOutcome("met", f"file {name} exists and is not empty")


async def evaluate_wait(
    wait_for: object, *, workspace: str, github: GitHubReader | None, default_repo: str | None
) -> WaitOutcome:
    """One poll of a stored ``wait_for``. A value that is not a valid wait is impossible."""
    try:
        wait = parse_wait_for(wait_for)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        return WaitOutcome("impossible", one_line(f"wait_for is not valid: {where}: {first['msg']}"))
    if isinstance(wait, JobWait):
        return await asyncio.to_thread(evaluate_job, wait, workspace)
    if isinstance(wait, FileWait):
        return await asyncio.to_thread(evaluate_file, wait, workspace)
    assert isinstance(wait, PrWait)
    repo = wait.repo or default_repo
    if not repo or not PR_REPO_RE.fullmatch(repo):
        return WaitOutcome("impossible", "pr wait needs a repo (none in wait_for, and no NOUS_GITHUB_DEFAULT_REPO)")
    if github is None:
        return WaitOutcome("pending", f"PR #{wait.number}: no GitHub reader this poll; will retry")
    return await evaluate_pr(wait, github, repo)


# A check run that concluded one of these did not pass.
CHECK_FAILED_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
)
_NAMES_SHOWN = 5
_PAGE = 100  # GitHubReader reads one page of check runs or reviews
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")


def _names(values: list[object]) -> str:
    """Up to five GitHub-supplied names, each cut to one short printable line."""
    shown = [one_line(v, 40) for v in values[:_NAMES_SHOWN]]
    return ", ".join(shown + (["..."] if len(values) > _NAMES_SHOWN else []))


async def evaluate_pr(wait: PrWait, reader: GitHubReader, repo: str) -> WaitOutcome:
    """A pull request, read from GitHub (at most two requests). Any GitHub failure, a 404 and an answer that cannot be
    read are pending: only GitHub's own answer about the PR ends a wait."""
    n = wait.number
    try:
        pull = await reader.pull(repo, n)
        if pull is None:
            return WaitOutcome("pending", f"PR #{n} not visible yet")
        closed = pull.get("state") == "closed"
        if wait.until == "merged":
            if pull.get("merged_at"):
                return WaitOutcome("met", f"PR #{n} merged")
            if closed:
                return WaitOutcome("impossible", f"PR #{n} was closed without a merge")
            return WaitOutcome("pending", f"PR #{n} is open")
        if wait.until == "closed":
            return WaitOutcome("met", f"PR #{n} closed") if closed else WaitOutcome("pending", f"PR #{n} is open")
        head = pull.get("head")
        sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(sha, str) or not _FULL_SHA.fullmatch(sha):
            return WaitOutcome("pending", f"PR #{n}: GitHub gave no head sha; will retry")
        sha = sha.lower()
        sha8 = sha[:8]
        given = (wait.head or "").lower()
        if wait.until == "head_changed":
            if not sha.startswith(given):
                return WaitOutcome("met", f"PR #{n} head changed to {sha8}")
            if closed:
                return WaitOutcome("impossible", f"PR #{n} closed with its head unchanged")
            return WaitOutcome("pending", f"PR #{n} head is still {sha8}")
        if wait.until == "checks_done":
            if given and not sha.startswith(given):
                return WaitOutcome(
                    "impossible", f"PR #{n} head moved to {sha8} while waiting for checks on {given[:8]}"
                )
            return _checks_done(n, sha8, await reader.check_runs(repo, sha))
        return _review_on_head(wait, sha, closed, await reader.reviews(repo, n))
    except GitHubUnavailable as exc:
        return WaitOutcome("pending", f"PR #{n}: GitHub did not answer ({one_line(exc, 60)}); will retry")


def _checks_done(n: int, sha8: str, runs: object) -> WaitOutcome:
    """Every check run on the head completed. None yet, a second page or one still running is pending."""
    items = runs.get("check_runs") if isinstance(runs, dict) else None
    total = runs.get("total_count") if isinstance(runs, dict) else None
    if not isinstance(items, list) or not isinstance(total, int):
        return WaitOutcome("pending", f"PR #{n}: no check runs listed on {sha8} yet")
    if total == 0 or total > len(items):
        return WaitOutcome("pending", f"PR #{n}: {total} check runs on {sha8}, not all readable yet")
    if any(not isinstance(run, dict) or run.get("status") != "completed" for run in items):
        return WaitOutcome("pending", f"PR #{n}: checks on {sha8} still running")
    failed = [run.get("name") or "?" for run in items if run.get("conclusion") in CHECK_FAILED_CONCLUSIONS]
    text = f"PR #{n} checks done on {sha8}"
    if failed:
        text += f" ({len(failed)} failed: {_names(failed)})"
    return WaitOutcome("met", text)


def _review_on_head(wait: PrWait, sha: str, closed: bool, reviews: object) -> WaitOutcome:
    """A submitted review on the current head, from a login starting with ``reviewer`` when one is given. With a full
    page of reviews, not finding one is never final: a later page may hold it."""
    n, sha8 = wait.number, sha[:8]
    prefix = (wait.reviewer or "").lower()
    listed = reviews if isinstance(reviews, list) else []
    for review in listed:
        if not isinstance(review, dict) or review.get("state") == "PENDING":
            continue
        commit = review.get("commit_id")
        user = review.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        if isinstance(commit, str) and commit.lower() == sha and isinstance(login, str):
            if login.lower().startswith(prefix):
                return WaitOutcome("met", f"PR #{n} has a review on {sha8} by {one_line(login, 40)}")
    if closed and isinstance(reviews, list) and len(listed) < _PAGE:
        return WaitOutcome("impossible", f"PR #{n} closed with no review on its head")
    return WaitOutcome("pending", f"PR #{n}: no matching review on {sha8} yet")
