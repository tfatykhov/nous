"""F114: the evidence a goal session cites to close a plan step, checked against the rows and files of that session's
own chain.

A ref names one thing the harness can look up, scoped to the agent and to the session's root (the root and every
intention under it, its lineage):

- ``ledger:<uuid>``: a durable execution ledger row that succeeded, written since the session started by a turn of
  the lineage (the root's continuation thread, a lineage subtask, or a lineage DAG's nodes);
- ``subtask:<uuid>``: a subtask of the lineage that completed (the evidence a read-only step can have);
- ``dag:<uuid>``: a DAG of the lineage that completed;
- ``file:<path>``: an existing regular file under the root's own ``intentions/<root id>/`` directory of the
  workspace, named by a path with no symbolic link and no step out of that directory. A file is one ref however it
  is named: by its device and inode on POSIX (a hard link is the same file), by its resolved path elsewhere.

An id may be given in full or by its first 8 characters when exactly one ref of that kind in scope starts with them.
A ref that names a row of another agent, another root or another goal is refused like one that names nothing. The
check proves that the thing exists and belongs to the session, not that it is good work. Every query runs in the
caller's transaction, so the check is as of the moment of the call.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Text, cast, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain import intentions
from nous.brain.continuation import INTENT_SESSION_PREFIX
from nous.storage.models import ExecutionDAG, ExecutionLedgerEntry, Intention, Subtask

PREFIX_CHARS = 8
FILES_LISTED = 30  # the files under the root's directory the citeable list gives at most
FILES_SCANNED = 500  # the directory entries it looks at, at most (a file not looked at is still citeable by path)
REF_ECHO_CHARS = 80  # a refused ref, as the refusal repeats it
FORMS = (
    "ledger:<id>, subtask:<id> or dag:<id> (a full id, or its first 8 characters) from the goal block's list, or "
    "file:intentions/<root id>/<name> for a file this work wrote"
)
_ID = re.compile(r"[0-9a-f]{8}(?:-?[0-9a-f]{4}){3}-?[0-9a-f]{12}|[0-9a-f]{8}")


@dataclass(frozen=True)
class Scope:
    """Whose evidence: the agent, the session's root (``intentions/<root>/`` is its directory) and when the session
    started (nothing older counts). ``workspace_dir`` is the agent's workspace."""

    agent_id: str
    root_id: UUID
    started_at: datetime
    workspace_dir: str

    @property
    def root_dir(self) -> str:
        """The root's directory, relative to the workspace."""
        return f"intentions/{self.root_id}"


@dataclass(frozen=True)
class Citeable:
    """A ref the session may cite to close a plan step, and what it is, in the harness's words."""

    ref: str
    label: str


@dataclass(frozen=True)
class Checked:
    """A checked ref: ``ref`` is its canonical form when it was accepted, else ``refusal`` says why. ``identity`` is
    what one-use compares (``identity``)."""

    ref: str | None = None
    refusal: str | None = None
    identity: str | None = None


def _echo(raw: str) -> str:
    return repr(raw[:REF_ECHO_CHARS])


def _lineage(scope: Scope, kind: str, id_column: Any) -> Any:
    """EXISTS: an intention of the session's root whose source is ``id_column`` (a uuid column) of ``kind``."""
    return exists().where(
        Intention.agent_id == scope.agent_id,
        Intention.root_id == scope.root_id,
        Intention.source_kind == kind,
        Intention.source_id == cast(id_column, Text),
    )


def _ledger_where(scope: Scope) -> list[Any]:
    E = ExecutionLedgerEntry
    return [
        E.agent_id == scope.agent_id,
        E.status == "success",
        E.created_at >= scope.started_at,
        or_(
            E.session_id == f"{INTENT_SESSION_PREFIX}{scope.root_id}",
            _lineage(scope, intentions.SOURCE_SUBTASK, E.subtask_id),
            _lineage(scope, intentions.SOURCE_DAG, E.dag_id),
        ),
    ]


def _subtask_where(scope: Scope) -> list[Any]:
    return [
        Subtask.agent_id == scope.agent_id,
        Subtask.status == "completed",
        _lineage(scope, intentions.SOURCE_SUBTASK, Subtask.id),
    ]


def _dag_where(scope: Scope) -> list[Any]:
    return [
        ExecutionDAG.agent_id == scope.agent_id,
        ExecutionDAG.status == "completed",
        _lineage(scope, intentions.SOURCE_DAG, ExecutionDAG.id),
    ]


_TABLES: dict[str, tuple[Any, Any]] = {
    "ledger": (ExecutionLedgerEntry, _ledger_where),
    "subtask": (Subtask, _subtask_where),
    "dag": (ExecutionDAG, _dag_where),
}
_WHAT = {
    "ledger": "a successful recorded call of this work since the session started",
    "subtask": "a completed subtask of this work",
    "dag": "a completed DAG of this work",
}


async def _check_id(s: AsyncSession, scope: Scope, kind: str, raw: str, value: str) -> Checked:
    table, where = _TABLES[kind]
    text_id = value.lower()
    if _ID.fullmatch(text_id) is None:
        return Checked(refusal=f"{_echo(raw)} is not a {kind} id: give the full id or its first 8 characters.")
    if len(text_id) == PREFIX_CHARS:
        found = list(
            (
                await s.execute(
                    select(table.id).where(*where(scope), cast(table.id, Text).like(f"{text_id}%")).limit(2)
                )
            ).scalars()
        )
        if len(found) > 1:
            return Checked(refusal=f"{_echo(raw)} matches more than one {kind} of this work: give the full id.")
    else:
        found = list((await s.execute(select(table.id).where(*where(scope), table.id == UUID(text_id)))).scalars())
    if not found:
        return Checked(refusal=f"{_echo(raw)} is not {_WHAT[kind]}.")
    ref = f"{kind}:{found[0]}"
    return Checked(ref=ref, identity=ref)


def _file_identity(path: Path) -> str:
    """One file however it is named: its device and inode on POSIX, its resolved path elsewhere."""
    if os.name == "posix":
        stat = path.stat()
        return f"file#{stat.st_dev}:{stat.st_ino}"
    return f"file#{os.path.normcase(str(path))}"


def identity(scope: Scope, ref: str) -> str:
    """What one-use compares a stored canonical ref by: for a file ref, the file it names while it exists (a hard
    link of it, or another spelling, is the same); for anything else, the ref itself."""
    if not ref.startswith("file:"):
        return ref
    try:
        target = (Path(scope.workspace_dir).resolve() / ref[len("file:") :]).resolve()
        if target.is_file():
            return _file_identity(target)
    except (OSError, ValueError, RuntimeError):
        pass
    return ref


def _check_file(scope: Scope, raw: str, value: str) -> Checked:
    """An existing regular file under the root's own directory, named relative to the workspace (or absolutely) by
    a path that, once ``..`` is collapsed, stays in that directory and reaches the file with no symbolic link."""
    refused = Checked(
        refusal=(
            f"{_echo(raw)} is not a file of this work: a file ref names an existing regular file under "
            f"{scope.root_dir}/, by a path with no symbolic link and no step out of that directory."
        )
    )
    if not value or "\x00" in value:  # Windows' realpath drops a NUL instead of raising (gh-106242)
        return refused
    try:
        workspace = Path(scope.workspace_dir).resolve()
        root_dir = workspace / "intentions" / str(scope.root_id)
        given = Path(value)
        spelled = Path(os.path.normpath(given if given.is_absolute() else workspace / given))
        if not spelled.is_relative_to(root_dir):
            return refused
        real = spelled.resolve(strict=True)
        if real != spelled or not real.is_file():  # a symbolic link on the way, or not a regular file
            return refused
        return Checked(ref=f"file:{real.relative_to(workspace).as_posix()}", identity=_file_identity(real))
    except (OSError, ValueError, RuntimeError):  # missing, unresolvable, a link loop
        return refused


async def check(s: AsyncSession, scope: Scope, raw: Any) -> Checked:
    """Check one ref against the session's lineage as of now: its canonical form, or why it is refused (a sentence
    the model can act on). One-use is the caller's: it compares canonical forms."""
    if not isinstance(raw, str) or not raw.strip():
        return Checked(refusal=f"evidence must be a ref: {FORMS}.")
    kind, colon, value = raw.strip().partition(":")
    kind = kind.strip().lower()
    if not colon or (kind not in _TABLES and kind != "file"):
        return Checked(refusal=f"{_echo(raw)} is not a ref the harness can check: use {FORMS}.")
    if kind == "file":
        return _check_file(scope, raw, value.strip())
    return await _check_id(s, scope, kind, raw, value.strip())


async def citeable(s: AsyncSession, scope: Scope, *, limit: int) -> list[Citeable]:
    """The refs the session's lineage can cite now, at most ``limit`` of each kind: its completed subtasks and DAGs,
    then its successful recorded calls newest first, then the files under its directory (at most
    ``FILES_LISTED``)."""
    found: list[Citeable] = []
    for kind in ("subtask", "dag"):
        table, where = _TABLES[kind]
        rows = await s.execute(select(table.id).where(*where(scope)).order_by(table.created_at.desc()).limit(limit))
        found += [Citeable(f"{kind}:{row_id}", _WHAT[kind]) for row_id in rows.scalars()]
    E = ExecutionLedgerEntry
    rows = await s.execute(
        select(E.id, E.created_at).where(*_ledger_where(scope)).order_by(E.created_at.desc()).limit(limit)
    )
    found += [
        # Never the tool's name: an MCP server names its own tools (mcp__<server>__<tool>).
        Citeable(f"ledger:{row.id}", f"a recorded call at {row.created_at:%Y-%m-%d %H:%M} UTC")
        for row in rows
    ]
    found += _files(scope)
    return found


def _files(scope: Scope) -> list[Citeable]:
    """Files under the root's directory (each once, however many names it has), at most ``FILES_LISTED`` of the
    first ``FILES_SCANNED`` entries."""
    try:
        workspace = Path(scope.workspace_dir).resolve()
        root_dir = workspace / "intentions" / str(scope.root_id)
        if not root_dir.is_dir():
            return []
        listed: list[Citeable] = []
        seen: set[str | None] = set()
        scanned = 0
        for base, _dirs, names in os.walk(root_dir):
            for name in sorted(names):
                scanned += 1
                if scanned > FILES_SCANNED:
                    return listed
                checked = _check_file(scope, name, str(Path(base, name)))
                if checked.ref is not None and checked.identity not in seen:
                    seen.add(checked.identity)
                    listed.append(Citeable(checked.ref, "a file this work wrote"))
                if len(listed) >= FILES_LISTED:
                    return listed
        return listed
    except OSError:
        return []
