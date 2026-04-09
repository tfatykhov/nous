"""Unit tests for nous/api/builtin_tools.py -- bash, read_file, write_file.

All tests are pure async (no database required). Filesystem tests use
the pytest tmp_path fixture for workspace isolation. Platform-aware
commands use python -c for cross-platform compatibility.
"""

import sys

import pytest

from nous.api.builtin_tools import (
    _MAX_FILE_SIZE,
    bash_tool,
    read_file_tool,
    write_file_tool,
)


def _extract_text(result: dict) -> str:
    """Extract text from MCP-format response."""
    return result["content"][0]["text"]


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
