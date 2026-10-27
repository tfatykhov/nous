"""Typed waits for lineage DAGs: evaluate a wait node's ``wait_for``, in-process and read-only.

The orchestrator calls :func:`evaluate_wait` on every poll of a wait node. Nothing here runs a shell or a
subprocess, and nothing writes: a job is read from its status file (never through ``runner.sh status``, which
writes), a file is looked up under the workspace.

Each outcome is ``met`` (the node completes), ``impossible`` (it fails) or ``pending`` (poll again), with a one-line
text that becomes the node's result or error, and so reaches the next node's prompt: text from outside (a job's
status word, a path) is cut to one short printable line.
"""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from nous.dag import typed_checks
from nous.dag.schemas import FileWait, JobWait, PrWait, file_wait_path_problem, parse_wait_for

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
    raise NotImplementedError("pr waits")
