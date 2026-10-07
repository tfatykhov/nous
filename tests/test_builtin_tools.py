"""Unit tests for nous/api/builtin_tools.py -- bash, read_file, write_file.

All tests are pure async (no database required). Filesystem tests use
the pytest tmp_path fixture for workspace isolation. Platform-aware
commands use python -c for cross-platform compatibility.
"""

import contextlib
import hashlib
import re
import sys

import pytest

from nous.api import call_outcome
from nous.api.builtin_tools import (
    _MAX_FILE_SIZE,
    _READ_FILE_MAX_CHARS,
    _READ_FILE_MAX_LINES,
    bash_tool,
    read_file_tool,
    write_file_tool,
)


def _extract_text(result: dict) -> str:
    """Extract text from MCP-format response."""
    return result["content"][0]["text"]


@contextlib.contextmanager
def _snapshot_bound(target, prior: str):
    """Bind the call to a compensation snapshot of ``target`` holding
    ``prior``, as the runner does for a write that can be reverted: only
    such a write takes the atomic compare-and-replace path."""
    token = call_outcome._current.set(
        call_outcome.CallOutcome(
            write_target=str(target.resolve()),
            write_expected=hashlib.sha256(prior.encode("utf-8")).hexdigest(),
        )
    )
    try:
        yield
    finally:
        call_outcome._current.reset(token)


# ---------------------------------------------------------------------------
# bash_tool tests
# ---------------------------------------------------------------------------


class TestBashTool:
    """Tests for the bash shell execution tool."""

    @pytest.mark.asyncio
    async def test_bash_tool_success(self, tmp_path):
        """Simple command -> stdout captured in response."""
        result = await bash_tool(
            command=f"{sys.executable} -c \"print('hello from bash tool')\"",
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "hello from bash tool" in text

    @pytest.mark.asyncio
    async def test_bash_tool_timeout(self, tmp_path):
        """Command exceeding timeout -> killed and timeout message returned."""
        result = await bash_tool(
            command=f'{sys.executable} -c "import time; time.sleep(30)"',
            timeout=1,
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "timed out" in text.lower()
        assert "1s" in text

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
    async def test_bash_tool_cancel_kills_whole_command(self, tmp_path):
        """codex P1 (PR #656): cancelling the calling turn (a DAG-disabled
        heartbeat check) must stop the command, including a compound command's
        later steps, so no side effect lands after the cancel."""
        import asyncio

        marker = tmp_path / "side_effect"
        task = asyncio.create_task(
            bash_tool(
                command=f"sleep 1; {sys.executable} -c \"open('side_effect', 'w')\"",
                timeout=30,
                _workspace_dir=str(tmp_path),
            )
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(2)
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_bash_tool_output_truncation(self, tmp_path):
        """Output exceeding 100KB -> truncated with marker."""
        # Generate ~150KB of output (well over 100KB limit)
        result = await bash_tool(
            command=f"{sys.executable} -c \"print('x' * 200000)\"",
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "truncated" in text.lower()

    @pytest.mark.asyncio
    async def test_bash_tool_stderr(self, tmp_path):
        """Stderr output captured and labeled."""
        result = await bash_tool(
            command=f"{sys.executable} -c \"import sys; sys.stderr.write('warning msg\\n')\"",
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "STDERR" in text
        assert "warning msg" in text

    @pytest.mark.asyncio
    async def test_bash_tool_nonzero_exit(self, tmp_path):
        """Non-zero exit code reported in output."""
        result = await bash_tool(
            command=f'{sys.executable} -c "import sys; sys.exit(42)"',
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "Exit code: 42" in text

    @pytest.mark.asyncio
    async def test_bash_tool_zero_exit_reported(self, tmp_path):
        """#179 codex round 13: the exit-code line is ALWAYS appended (last
        line = authoritative wrapper status), so quoted 'Exit code: N' in a
        command's own output can be disambiguated downstream."""
        result = await bash_tool(
            command=f"{sys.executable} -c \"print('hello')\"",
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert text.rstrip().endswith("Exit code: 0")

    @pytest.mark.asyncio
    async def test_bash_tool_creates_workspace(self, tmp_path):
        """Workspace directory auto-created if it doesn't exist."""
        workspace = tmp_path / "deep" / "nested" / "workspace"
        assert not workspace.exists()

        result = await bash_tool(
            command=f"{sys.executable} -c \"print('created')\"",
            _workspace_dir=str(workspace),
        )
        text = _extract_text(result)
        assert "created" in text
        assert workspace.exists()


# ---------------------------------------------------------------------------
# read_file_tool tests
# ---------------------------------------------------------------------------


class TestReadFileTool:
    """Tests for the file reading tool."""

    @pytest.mark.asyncio
    async def test_read_file_success(self, tmp_path):
        """Read an existing file -> contents returned."""
        test_file = tmp_path / "hello.txt"
        test_file.write_text("Hello, world!\nLine 2\nLine 3", encoding="utf-8")

        result = await read_file_tool(
            path="hello.txt",
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "Hello, world!" in text
        assert "Line 2" in text
        assert "Line 3" in text

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, tmp_path):
        """Missing file -> 'File not found' message (not exception)."""
        result = await read_file_tool(
            path="nonexistent.txt",
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "File not found" in text

    @pytest.mark.asyncio
    async def test_read_file_size_limit(self, tmp_path):
        """File exceeding 1MB -> size limit message."""
        large_file = tmp_path / "large.bin"
        # Write just over 1MB
        large_file.write_bytes(b"x" * (_MAX_FILE_SIZE + 1))

        result = await read_file_tool(
            path="large.bin",
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "too large" in text.lower()
        assert "offset/limit" in text.lower()

    @pytest.mark.asyncio
    async def test_read_file_with_offset_and_limit(self, tmp_path):
        """offset/limit parameters slice file by lines."""
        test_file = tmp_path / "lines.txt"
        test_file.write_text("line0\nline1\nline2\nline3\nline4\n", encoding="utf-8")

        # Read lines 1-2 (0-indexed offset=1, limit=2)
        result = await read_file_tool(
            path="lines.txt",
            offset=1,
            limit=2,
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "line1" in text
        assert "line2" in text
        assert "line0" not in text
        assert "line3" not in text

    @pytest.mark.asyncio
    async def test_read_file_path_validation(self, tmp_path):
        """Path outside workspace -> rejection message."""
        # Try to escape workspace via parent traversal
        result = await read_file_tool(
            path="../../../etc/passwd",
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "outside workspace" in text.lower()

    @pytest.mark.asyncio
    async def test_read_file_absolute_path_outside_workspace(self, tmp_path):
        """Absolute path outside workspace -> rejection."""
        # Use an absolute path that's definitely outside tmp_path
        if sys.platform == "win32":
            outside_path = "C:\\Windows\\System32\\drivers\\etc\\hosts"
        else:
            outside_path = "/etc/passwd"

        result = await read_file_tool(
            path=outside_path,
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "outside workspace" in text.lower()

    @pytest.mark.asyncio
    async def test_read_file_empty(self, tmp_path):
        """Empty file -> '(empty file)' message."""
        empty_file = tmp_path / "empty.txt"
        empty_file.write_text("", encoding="utf-8")

        result = await read_file_tool(
            path="empty.txt",
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "empty file" in text.lower()


# ---------------------------------------------------------------------------
# read_file pagination: a contiguous window plus a trailer, never a sample
# ---------------------------------------------------------------------------

_TRAILER = re.compile(
    r"\n\[read_file: showing lines (\d+)–(\d+) of (\d+)\. "
    r"Call read_file\(path, offset=(\d+)(?:, limit=(\d+))?\) for the next part\.\]$"
)


def _prose_report(n_lines: int = 300, width: int = 200) -> str:
    """Prose markdown shaped like the 2026-10-07 incident report: it contains
    "failed"/"error", which made SmartCompress sample it."""
    lines = []
    for i in range(n_lines):
        word = "failed" if i % 7 == 0 else "error" if i % 11 == 0 else "passed"
        line = f"Line {i}: the harness comparison {word} on criterion {i % 13}. "
        lines.append((line * (width // len(line) + 1))[:width])
    return "\n".join(lines) + "\n"


class TestReadFilePagination:
    """A file the model asked to read must arrive as contiguous lines; an
    oversize one is cut at a page boundary the trailer names exactly."""

    @pytest.mark.asyncio
    async def test_oversize_prose_is_a_contiguous_window_with_trailer(self, tmp_path):
        content = _prose_report()  # 300 lines, ~60 KB: over the char cap
        assert len(content) > _READ_FILE_MAX_CHARS
        (tmp_path / "report.md").write_text(content, encoding="utf-8")

        text = _extract_text(await read_file_tool(path="report.md", _workspace_dir=str(tmp_path)))

        m = _TRAILER.search(text)
        assert m, text[-300:]
        first, last, total, next_offset = map(int, m.groups()[:4])
        assert (first, total, next_offset, m.group(5)) == (1, 300, last, None)
        page = text[: m.start() + 1]
        assert page == "".join(content.splitlines(keepends=True)[:last]), "not a contiguous prefix"
        assert len(page) <= _READ_FILE_MAX_CHARS

    @pytest.mark.asyncio
    async def test_paging_by_trailer_reaches_the_end_byte_identical(self, tmp_path):
        content = _prose_report(n_lines=1000, width=60)  # over the line cap
        (tmp_path / "big.md").write_text(content, encoding="utf-8")

        assert await self._page_through(tmp_path, "big.md", offset=0, limit=0) == (content, 3)  # 400+400+200

    @pytest.mark.asyncio
    async def test_paging_an_explicit_range_by_trailer_stops_at_its_end(self, tmp_path):
        content = _prose_report(n_lines=1000, width=60)
        (tmp_path / "big.md").write_text(content, encoding="utf-8")

        text, calls = await self._page_through(tmp_path, "big.md", offset=100, limit=850)

        assert text == "".join(content.splitlines(keepends=True)[100:950])
        assert calls == 3  # 400 + 400 + 50

    @staticmethod
    async def _page_through(tmp_path, path: str, offset: int, limit: int) -> tuple[str, int]:
        """Follow trailers exactly as the model would; return (text, calls)."""
        pieces, calls = [], 0
        while True:
            calls += 1
            text = _extract_text(
                await read_file_tool(path=path, offset=offset, limit=limit, _workspace_dir=str(tmp_path))
            )
            m = _TRAILER.search(text)
            if not m:
                pieces.append(text)
                return "".join(pieces), calls
            assert int(m.group(1)) == offset + 1
            pieces.append(text[: m.start() + 1])
            offset, limit = int(m.group(4)), int(m.group(5) or 0)

    @pytest.mark.asyncio
    async def test_explicit_offset_limit_is_exact_and_untrailed(self, tmp_path):
        content = _prose_report(n_lines=1000, width=60)
        (tmp_path / "big.md").write_text(content, encoding="utf-8")
        lines = content.splitlines(keepends=True)

        text = _extract_text(await read_file_tool(path="big.md", offset=950, limit=10, _workspace_dir=str(tmp_path)))
        assert text == "".join(lines[950:960])

        # A limit past EOF returns the rest, with no spurious trailer.
        text = _extract_text(await read_file_tool(path="big.md", offset=990, limit=500, _workspace_dir=str(tmp_path)))
        assert text == "".join(lines[990:])

    @pytest.mark.asyncio
    async def test_explicit_limit_over_cap_is_capped_with_trailer(self, tmp_path):
        content = _prose_report(n_lines=1000, width=60)
        (tmp_path / "big.md").write_text(content, encoding="utf-8")

        text = _extract_text(await read_file_tool(path="big.md", offset=100, limit=900, _workspace_dir=str(tmp_path)))

        m = _TRAILER.search(text)
        assert m
        assert m.groups() == ("101", "500", "1000", "500", "500")
        assert text[: m.start() + 1] == "".join(content.splitlines(keepends=True)[100:500])

    @pytest.mark.asyncio
    async def test_file_at_the_caps_is_unchanged(self, tmp_path):
        content = "".join(f"row {i}\n" for i in range(_READ_FILE_MAX_LINES))
        (tmp_path / "exact.txt").write_text(content, encoding="utf-8")

        text = _extract_text(await read_file_tool(path="exact.txt", _workspace_dir=str(tmp_path)))
        assert text == content

    @pytest.mark.asyncio
    async def test_single_line_over_char_cap_is_whole_then_trailed(self, tmp_path):
        content = "x" * (_READ_FILE_MAX_CHARS + 10) + "\nsecond\n"
        (tmp_path / "wide.txt").write_text(content, encoding="utf-8")

        text = _extract_text(await read_file_tool(path="wide.txt", _workspace_dir=str(tmp_path)))

        m = _TRAILER.search(text)
        assert m
        assert m.groups() == ("1", "1", "2", "1", None)
        assert text[: m.start() + 1] == "x" * (_READ_FILE_MAX_CHARS + 10) + "\n"

    @pytest.mark.asyncio
    async def test_offset_past_end_is_reported(self, tmp_path):
        (tmp_path / "short.txt").write_text("a\nb\n", encoding="utf-8")

        text = _extract_text(await read_file_tool(path="short.txt", offset=5, _workspace_dir=str(tmp_path)))
        assert text == "[read_file: offset 5 is past the end of the file (2 lines).]"


# ---------------------------------------------------------------------------
# write_file_tool tests
# ---------------------------------------------------------------------------


class TestWriteFileTool:
    """Tests for the file writing tool."""

    @pytest.mark.asyncio
    async def test_write_file_success(self, tmp_path):
        """Write content to a new file, verify contents on disk."""
        result = await write_file_tool(
            path="output.txt",
            content="Written by test",
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "written successfully" in text.lower()

        # Verify actual file contents
        written = (tmp_path / "output.txt").read_text(encoding="utf-8")
        assert written == "Written by test"

    @pytest.mark.asyncio
    async def test_write_file_creates_dirs(self, tmp_path):
        """Write to nested path -> parent directories auto-created."""
        result = await write_file_tool(
            path="deep/nested/dir/file.txt",
            content="Nested content",
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "written successfully" in text.lower()

        # Verify nested file exists
        nested_file = tmp_path / "deep" / "nested" / "dir" / "file.txt"
        assert nested_file.exists()
        assert nested_file.read_text(encoding="utf-8") == "Nested content"

    @pytest.mark.asyncio
    async def test_write_file_path_validation(self, tmp_path):
        """Path outside workspace -> rejection message."""
        result = await write_file_tool(
            path="../../../tmp/evil.txt",
            content="malicious content",
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "outside workspace" in text.lower()

    @pytest.mark.asyncio
    async def test_write_file_overwrites_existing(self, tmp_path):
        """Writing to existing file overwrites it."""
        target = tmp_path / "overwrite.txt"
        target.write_text("original content", encoding="utf-8")

        result = await write_file_tool(
            path="overwrite.txt",
            content="new content",
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "written successfully" in text.lower()

        assert target.read_text(encoding="utf-8") == "new content"

    @pytest.mark.asyncio
    async def test_write_file_reports_size(self, tmp_path):
        """Response includes file size in bytes."""
        content = "Hello " * 100  # 600 bytes
        result = await write_file_tool(
            path="sized.txt",
            content=content,
            _workspace_dir=str(tmp_path),
        )
        text = _extract_text(result)
        assert "600" in text  # Size: 600 bytes

    @pytest.mark.asyncio
    async def test_write_file_atomic_preserves_original_on_failure(self, tmp_path, monkeypatch):
        """Codex P1 on #652: a snapshotted write_file is atomic — an I/O failure
        mid-write (e.g. ENOSPC during fsync) must leave the original file untouched."""
        import errno
        import os

        target = tmp_path / "existing.txt"
        original_content = "original valuable content"
        target.write_text(original_content, encoding="utf-8")

        call_count = {"fsync": 0}

        def failing_fsync(fd):
            call_count["fsync"] += 1
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(os, "fsync", failing_fsync)

        with _snapshot_bound(target, original_content):
            result = await write_file_tool(
                path="existing.txt",
                content="new content that should not stick",
                _workspace_dir=str(tmp_path),
            )

        text = _extract_text(result)
        assert "Error" in text
        assert "No space left" in text

        assert target.read_text(encoding="utf-8") == original_content
        assert sorted(p.name for p in tmp_path.iterdir()) == ["existing.txt"], "temp file was not cleaned up"
        assert call_count["fsync"] >= 1

    @pytest.mark.asyncio
    async def test_write_file_atomic_preserves_mode(self, tmp_path):
        """Atomic write preserves the original file's mode."""
        import stat

        target = tmp_path / "executable.sh"
        target.write_text("#!/bin/bash\necho hello", encoding="utf-8")
        target.chmod(0o755)

        with _snapshot_bound(target, "#!/bin/bash\necho hello"):
            result = await write_file_tool(
                path="executable.sh",
                content="#!/bin/bash\necho updated",
                _workspace_dir=str(tmp_path),
            )
        text = _extract_text(result)
        assert "written successfully" in text.lower()

        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o755, f"expected 0o755, got {oct(mode)}"
