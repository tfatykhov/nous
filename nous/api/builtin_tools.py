"""Built-in tools for the Nous agent: bash, read_file, write_file.

These tools give the agent system access capabilities, gated by
cognitive frames (D5).  All tools return MCP-format responses for
consistent handling by ToolDispatcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import logging
import os
import signal
import stat
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nous.api.call_outcome import current_outcome
from nous.api.tools import ToolDispatcher, _tool_error
from nous.config import Settings

logger = logging.getLogger(__name__)

# Limits
_MAX_BASH_TIMEOUT = 300  # seconds
_MAX_OUTPUT_CHARS = 100 * 1024  # 100KB
_MAX_FILE_SIZE = 1 * 1024 * 1024  # 1MB


def _mcp_response(text: str) -> dict[str, Any]:
    """Build MCP-format response."""
    return {"content": [{"type": "text", "text": text}]}


def _validate_path(path_str: str, workspace_dir: str) -> Path:
    """Validate that a path is under workspace_dir.

    Raises ValueError if path escapes workspace.
    """
    workspace = Path(workspace_dir).resolve()
    target = (workspace / path_str).resolve() if not Path(path_str).is_absolute() else Path(path_str).resolve()

    if not target.is_relative_to(workspace):
        raise ValueError(
            f"Path '{path_str}' is outside workspace '{workspace_dir}'. "
            "Only paths within the workspace directory are allowed."
        )
    return target


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def bash_tool(
    command: str,
    timeout: int = 30,
    *,
    _workspace_dir: str = "/tmp/nous-workspace",
) -> dict[str, Any]:
    """Execute a shell command in the workspace directory.

    Args:
        command: Shell command to execute
        timeout: Timeout in seconds (default 30, max 300)
        _workspace_dir: Internal param set by registration closure

    Returns:
        MCP-format response with stdout + stderr
    """
    try:
        # Clamp timeout
        effective_timeout = max(1, min(timeout, _MAX_BASH_TIMEOUT))

        # Ensure workspace exists
        workspace = Path(_workspace_dir)
        workspace.mkdir(parents=True, exist_ok=True)

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
            # Own process group, so a cancelled turn can stop the whole
            # command (``a; b``, pipelines), not only the /bin/sh parent.
            start_new_session=True,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=effective_timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return _tool_error(
                f"Command timed out after {effective_timeout}s.\n"
                f"Command: {command}"
            )
        except asyncio.CancelledError:
            # The calling turn was cancelled (e.g. its heartbeat check was
            # disabled by the DAG mid-run): don't leave the command running.
            if proc.returncode is None:
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
            raise

        # Decode and truncate
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        # Truncate if needed
        if len(stdout_text) > _MAX_OUTPUT_CHARS:
            stdout_text = stdout_text[:_MAX_OUTPUT_CHARS] + "\n... [output truncated at 100KB]"
        if len(stderr_text) > _MAX_OUTPUT_CHARS:
            stderr_text = stderr_text[:_MAX_OUTPUT_CHARS] + "\n... [stderr truncated at 100KB]"

        parts = []
        if stdout_text:
            parts.append(stdout_text)
        if stderr_text:
            parts.append(f"STDERR:\n{stderr_text}")
        # Always appended (#179 codex round 13): the trailing line is the
        # AUTHORITATIVE wrapper status, so downstream consumers (compaction
        # bulk-failure detection) can disambiguate a quoted "Exit code: N"
        # inside the command's own output from the real return code.
        parts.append(f"Exit code: {proc.returncode}")

        output = "\n".join(parts) if parts else "(no output)"
        return _mcp_response(output)

    except Exception as e:
        logger.exception("bash_tool error")
        return _tool_error(f"Error executing command: {e}")


async def read_file_tool(
    path: str,
    offset: int = 0,
    limit: int = 0,
    *,
    _workspace_dir: str = "/tmp/nous-workspace",
) -> dict[str, Any]:
    """Read a file from the workspace directory.

    Args:
        path: File path (relative to workspace or absolute within workspace)
        offset: Line offset to start reading from (0-indexed)
        limit: Number of lines to read (0 = all)
        _workspace_dir: Internal param set by registration closure

    Returns:
        MCP-format response with file contents
    """
    try:
        target = _validate_path(path, _workspace_dir)

        if not target.exists():
            return _tool_error(f"File not found: {path}")

        if not target.is_file():
            return _tool_error(f"Not a file: {path}")

        # Check size
        file_size = target.stat().st_size
        if file_size > _MAX_FILE_SIZE:
            return _tool_error(
                f"File too large: {file_size:,} bytes (limit: {_MAX_FILE_SIZE:,} bytes). "
                f"Use offset/limit to read portions."
            )

        # Read file in thread
        content = await asyncio.to_thread(target.read_text, encoding="utf-8", errors="replace")

        # Apply offset/limit
        if offset > 0 or limit > 0:
            lines = content.splitlines(keepends=True)
            if offset > 0:
                lines = lines[offset:]
            if limit > 0:
                lines = lines[:limit]
            content = "".join(lines)

        return _mcp_response(content if content else "(empty file)")

    except ValueError as e:
        return _tool_error(str(e))
    except Exception as e:
        logger.exception("read_file_tool error")
        return _tool_error(f"Error reading file: {e}")


# ---------------------------------------------------------------------------
# Phase 2.8: one compare-and-replace primitive for write_file AND its revert
# ---------------------------------------------------------------------------

# Expected pre-state meaning "the target must not exist".
ABSENT = "absent"

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # 0 on Windows: symlink refusal is POSIX-only
_DIR_FD = os.open in os.supports_dir_fd and os.replace in os.supports_dir_fd


class PreconditionFailed(Exception):
    """The target is not in the expected state (or is a symlink / not a
    regular file); nothing was changed."""


class WriteFence:
    """Per-call fence between a write_file worker thread and a revert.

    The worker checks ``revoked`` and renames under ``lock``; a revert sets
    ``revoked`` under the same lock BEFORE it inspects the file. So a worker
    orphaned by a cancelled call either landed its write before the revert
    looked, or never lands it at all.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        self.lock = threading.Lock()
        self.revoked = False
        self.started = False


_write_fences: dict[str, WriteFence] = {}


def register_write_fence(key: str) -> WriteFence:
    fence = _write_fences[key] = WriteFence(key)
    return fence


def drop_write_fence(fence: WriteFence) -> None:
    if _write_fences.get(fence.key) is fence:
        del _write_fences[fence.key]


def revoke_write_fence(key: str) -> None:
    """Stop any still-running write for call ``key`` from landing (blocking;
    run it off the event loop). A no-op once that write finished."""
    fence = _write_fences.get(key)
    if fence is not None:
        with fence.lock:
            fence.revoked = True


@dataclass(frozen=True)
class _State:
    digest: str  # ABSENT, a sha256 hex, or "oversized"
    stat: os.stat_result | None


@contextlib.contextmanager
def _parent_dir(target: Path) -> Iterator[tuple[int | None, str]]:
    """(dir_fd, name) for ``target``: every later operation is relative to
    ONE open descriptor of the parent, so the path is never re-resolved.
    Path-based (dir_fd None) where the platform has no dir_fd support."""
    if not _DIR_FD:
        yield None, str(target)
        return
    dfd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW)
    try:
        yield dfd, target.name
    finally:
        os.close(dfd)


def _read_state(dfd: int | None, name: str, limit: int) -> _State:
    try:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=dfd)
    except FileNotFoundError:
        return _State(ABSENT, None)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PreconditionFailed(f"{name!r} is a symlink") from exc
        raise
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise PreconditionFailed(f"{name!r} is not a regular file")
        data = f.read(limit + 1)
    if len(data) > limit:
        return _State("oversized", st)
    return _State(hashlib.sha256(data).hexdigest(), st)


def _unchanged_since(dfd: int | None, name: str, pre: _State) -> bool:
    """The target is still the very file ``pre`` hashed (or still absent)."""
    try:
        now = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return pre.stat is None
    if pre.stat is None:
        return False
    return (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns) == (
        pre.stat.st_dev,
        pre.stat.st_ino,
        pre.stat.st_size,
        pre.stat.st_mtime_ns,
    )


def _fsync_dir(dfd: int | None) -> None:
    if dfd is not None:
        try:
            os.fsync(dfd)
        except OSError:
            pass


def file_digest(target: Path, limit: int) -> str:
    """ABSENT, the sha256 of ``target``'s bytes, or "oversized" (> ``limit``).
    Raises PreconditionFailed for a symlink or non-regular file."""
    with _parent_dir(target) as (dfd, name):
        return _read_state(dfd, name, limit).digest


def atomic_replace_bytes(
    target: Path,
    data: bytes,
    *,
    expected: str | None = None,
    limit: int = _MAX_FILE_SIZE,
    fence: WriteFence | None = None,
) -> None:
    """Replace ``target`` with ``data`` atomically, or change nothing.

    The bytes go to a fresh temp file in the same directory (every byte
    written, fsynced, the existing mode kept -- a new file gets the umask
    default), which is then renamed over the target. With ``expected`` (a
    sha256 hex, or ABSENT) the target must hold exactly that right before
    the rename -- an ABSENT target is created no-clobber -- else
    PreconditionFailed. A symlink target is never followed. The one window
    left, between that last check and the rename, cannot be closed against
    a writer that takes no lock (POSIX has no compare-and-swap rename).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with _parent_dir(target) as (dfd, name):
        # Without an expected state only the mode is needed: nothing is hashed.
        pre = _read_state(dfd, name, limit if expected is not None else 0)
        if expected is not None and pre.digest != expected:
            raise PreconditionFailed(f"{target} changed since its state was recorded")
        mode = stat.S_IMODE(pre.stat.st_mode) if pre.stat is not None else None
        tmp_name = f".write_file_{uuid.uuid4().hex}.tmp"
        tmp: str | None = tmp_name if dfd is not None else str(target.parent / tmp_name)
        fd: int | None = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o666 if mode is None else 0o600,
            dir_fd=dfd,
        )
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            if mode is not None and hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            os.fsync(fd)
            os.close(fd)
            fd = None
            if mode is not None and not hasattr(os, "fchmod"):
                os.chmod(tmp, mode)  # only reached path-based (no fchmod => no dir_fd either)
            with fence.lock if fence is not None else contextlib.nullcontext():
                if fence is not None and fence.revoked:
                    raise PreconditionFailed("the write was revoked by a revert")
                if expected is not None and not _unchanged_since(dfd, name, pre):
                    raise PreconditionFailed(f"{target} changed while it was being written")
                if expected == ABSENT:
                    try:
                        os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
                    except FileExistsError as exc:
                        raise PreconditionFailed(f"{target} appeared while it was being written") from exc
                    except (OSError, NotImplementedError):
                        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)  # no hard links here
                        tmp = None
                else:
                    os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
                    tmp = None
            _fsync_dir(dfd)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp is not None:
                try:
                    os.unlink(tmp, dir_fd=dfd)
                except OSError:
                    pass


def remove_if_matches(target: Path, expected: str, *, limit: int) -> bool:
    """Delete ``target`` only while it holds exactly ``expected`` (sha256).
    False when it is already absent; PreconditionFailed when it differs."""
    with _parent_dir(target) as (dfd, name):
        pre = _read_state(dfd, name, limit)
        if pre.digest == ABSENT:
            return False
        if pre.digest != expected or not _unchanged_since(dfd, name, pre):
            raise PreconditionFailed(f"{target} changed since its state was recorded")
        os.unlink(name, dir_fd=dfd)
        _fsync_dir(dfd)
        return True


async def write_file_tool(
    path: str,
    content: str,
    *,
    _workspace_dir: str = "/tmp/nous-workspace",
) -> dict[str, Any]:
    """Write content to a file in the workspace directory.

    Args:
        path: File path (relative to workspace or absolute within workspace)
        content: Content to write
        _workspace_dir: Internal param set by registration closure

    Returns:
        MCP-format response confirming write
    """
    try:
        target = _validate_path(path, _workspace_dir)
        # Phase 2.8: a snapshotted write is bound to the path its snapshot
        # recorded AND to the state it recorded there -- a change made since
        # (another writer) is refused, never overwritten and later "restored"
        # away by a revert.
        outcome = current_outcome()
        expected: str | None = None
        fence: WriteFence | None = None
        if outcome is not None and outcome.write_target is not None:
            if str(target) != outcome.write_target:
                return _tool_error(
                    f"Path '{path}' no longer resolves to the file that was snapshotted before this write; "
                    "refused so the write stays revertible."
                )
            expected = outcome.write_expected
            fence = outcome.write_fence
        data = content.encode("utf-8")

        def _run() -> None:
            try:
                atomic_replace_bytes(target, data, expected=expected, fence=fence)
            finally:
                if fence is not None:
                    drop_write_fence(fence)

        if fence is not None:
            fence.started = True
        await asyncio.to_thread(_run)

        return _mcp_response(
            f"File written successfully: {target}\n"
            f"Size: {len(content):,} bytes"
        )

    except PreconditionFailed as e:
        return _tool_error(f"Refused to write '{path}': {e}; nothing was written.")
    except ValueError as e:
        return _tool_error(str(e))
    except Exception as e:
        logger.exception("write_file_tool error")
        return _tool_error(f"Error writing file: {e}")


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_BASH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Execute a shell command in the workspace directory",
    "properties": {
        "command": {"type": "string", "description": "Shell command to execute"},
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds (default 30, max 300)",
            "default": 30,
            "minimum": 1,
            "maximum": 300,
        },
    },
    "required": ["command"],
}

_READ_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Read a file from the workspace directory",
    "properties": {
        "path": {"type": "string", "description": "File path (relative or absolute within workspace)"},
        "offset": {
            "type": "integer",
            "description": "Line offset to start reading from (0-indexed)",
            "default": 0,
            "minimum": 0,
        },
        "limit": {
            "type": "integer",
            "description": "Number of lines to read (0 = all)",
            "default": 0,
            "minimum": 0,
        },
    },
    "required": ["path"],
}

_WRITE_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Write content to a file in the workspace directory",
    "properties": {
        "path": {"type": "string", "description": "File path (relative or absolute within workspace)"},
        "content": {"type": "string", "description": "Content to write to the file"},
    },
    "required": ["path", "content"],
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_builtin_tools(dispatcher: ToolDispatcher, settings: Settings) -> None:
    """Register built-in tools (bash, read_file, write_file) with the dispatcher.

    Creates closure wrappers that inject workspace_dir from settings.
    """
    workspace = settings.workspace_dir

    async def _bash(command: str, timeout: int = 30) -> dict[str, Any]:
        return await bash_tool(command, timeout, _workspace_dir=workspace)

    async def _read_file(path: str, offset: int = 0, limit: int = 0) -> dict[str, Any]:
        return await read_file_tool(path, offset, limit, _workspace_dir=workspace)

    async def _write_file(path: str, content: str) -> dict[str, Any]:
        return await write_file_tool(path, content, _workspace_dir=workspace)

    dispatcher.register("bash", _bash, _BASH_SCHEMA)
    dispatcher.register("read_file", _read_file, _READ_FILE_SCHEMA)
    dispatcher.register("write_file", _write_file, _WRITE_FILE_SCHEMA)
