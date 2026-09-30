"""Built-in tools for the Nous agent: bash, read_file, write_file.

These tools give the agent system access capabilities, gated by
cognitive frames (D5).  All tools return MCP-format responses for
consistent handling by ToolDispatcher.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import tempfile
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


def _atomic_write_sync(target: Path, content: str) -> None:
    """Atomically write content to target via temp file + os.replace().

    Writes to a temp file in the same directory, flushes, fsyncs, preserves
    the original file's mode if it existed, then atomically replaces the
    target. On any failure the original file is untouched and no temp file
    remains (codex P1 on #652: prevents corruption on I/O failures mid-write).
    """
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    original_mode: int | None = None
    if target.exists():
        original_mode = stat.S_IMODE(target.stat().st_mode)

    fd = None
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(parent), prefix=".write_file_")
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = None

        if original_mode is not None:
            os.chmod(tmp_path, original_mode)

        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


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
        # Phase 2.8: bound to the path its compensation snapshot recorded.
        outcome = current_outcome()
        if outcome is not None and outcome.write_target is not None and str(target) != outcome.write_target:
            return _tool_error(
                f"Path '{path}' no longer resolves to the file that was snapshotted before this write; "
                "refused so the write stays revertible."
            )

        await asyncio.to_thread(_atomic_write_sync, target, content)

        return _mcp_response(
            f"File written successfully: {target}\n"
            f"Size: {len(content):,} bytes"
        )

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
