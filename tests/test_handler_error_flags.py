"""Harness Phase 1b (codex r1 on #645): handler failures must be FLAGGED.

The durable execution ledger closes a side-effecting call ``success`` or
``error`` from the dispatch ``is_error`` bit. Every tool module outside
``tools.py`` built failures with the same helper as successes -- ``_error()``
in email/telegram, ``_mcp_response()`` in builtin/identity/web -- and neither
set ``is_error``. A write refused outside the workspace, or an email the
allowlist rejected, was therefore durably recorded as ``success``, and the
model was told the call had worked.

Bash's non-zero exit stays unflagged on purpose: the exit code is in the
output, and a failing ``grep`` is not a failed tool call.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.tools import ToolDispatcher

# ---- builtin: bash / read_file / write_file ----


def _builtin(tmp_path: Path) -> ToolDispatcher:
    from nous.api.builtin_tools import register_builtin_tools

    d = ToolDispatcher()
    register_builtin_tools(d, SimpleNamespace(workspace_dir=str(tmp_path / "ws")))
    return d


class _Proc:
    def __init__(self, *, hang: bool = False, returncode: int = 0):
        self.hang = hang
        self.returncode = returncode

    async def communicate(self):
        if self.hang:
            await asyncio.sleep(30)
        return b"out", b""

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_write_file_outside_the_workspace_is_an_error(tmp_path):
    text, is_error = await _builtin(tmp_path).dispatch(
        "write_file", {"path": str(tmp_path / "outside.txt"), "content": "x"},
    )
    assert is_error, text
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.asyncio
async def test_write_file_success_is_not_an_error(tmp_path):
    text, is_error = await _builtin(tmp_path).dispatch("write_file", {"path": "ok.txt", "content": "x"})
    assert not is_error, text


@pytest.mark.asyncio
async def test_read_file_missing_is_an_error(tmp_path):
    text, is_error = await _builtin(tmp_path).dispatch("read_file", {"path": "missing.txt"})
    assert is_error, text


@pytest.mark.asyncio
async def test_read_file_empty_file_is_not_an_error(tmp_path):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "empty.txt").write_text("")
    text, is_error = await _builtin(tmp_path).dispatch("read_file", {"path": "empty.txt"})
    assert not is_error and text == "(empty file)"


@pytest.mark.asyncio
async def test_bash_spawn_failure_is_an_error(tmp_path, monkeypatch):
    async def boom(*a, **k):
        raise OSError("no shell")

    monkeypatch.setattr("nous.api.builtin_tools.asyncio.create_subprocess_shell", boom)
    text, is_error = await _builtin(tmp_path).dispatch("bash", {"command": "rm x"})
    assert is_error and "Error executing command" in text


@pytest.mark.asyncio
async def test_bash_timeout_is_an_error(tmp_path, monkeypatch):
    async def spawn(*a, **k):
        return _Proc(hang=True)

    monkeypatch.setattr("nous.api.builtin_tools.asyncio.create_subprocess_shell", spawn)
    text, is_error = await _builtin(tmp_path).dispatch("bash", {"command": "rm x", "timeout": 1})
    assert is_error and "timed out" in text


@pytest.mark.asyncio
async def test_bash_nonzero_exit_stays_unflagged(tmp_path, monkeypatch):
    async def spawn(*a, **k):
        return _Proc(returncode=1)

    monkeypatch.setattr("nous.api.builtin_tools.asyncio.create_subprocess_shell", spawn)
    text, is_error = await _builtin(tmp_path).dispatch("bash", {"command": "grep x f"})
    assert not is_error and "Exit code: 1" in text


# ---- send_email ----


def _email(**overrides) -> ToolDispatcher:
    from nous.api.email_tools import register_email_tools
    from nous.config import Settings

    fields = dict(
        email="nous@example.com", email_user="nous@example.com", email_password="app-password",
        email_allowlist="tim@example.com", email_tool_enabled=True, email_max_per_hour=5,
        email_smtp_host="smtp.example.com", email_smtp_port=587, email_content_gate="strict",
    )
    fields.update(overrides)
    d = ToolDispatcher()
    register_email_tools(d, Settings(_env_file=None, **fields))
    return d


@pytest.mark.asyncio
async def test_send_email_disabled_is_an_error():
    text, is_error = await _email(email_tool_enabled=False).dispatch(
        "send_email", {"to": "tim@example.com", "subject": "hi", "body": "hello"},
    )
    assert is_error and "disabled" in text


@pytest.mark.asyncio
async def test_send_email_off_allowlist_is_an_error():
    text, is_error = await _email().dispatch(
        "send_email", {"to": "eve@example.com", "subject": "hi", "body": "hello"},
    )
    assert is_error, text


@pytest.mark.asyncio
async def test_send_email_smtp_failure_is_an_error(monkeypatch):
    import smtplib

    class Boom:
        def __init__(self, *a, **k):
            raise smtplib.SMTPServerDisconnected("gone")

    monkeypatch.setattr("nous.api.email_tools.smtplib.SMTP", Boom)
    text, is_error = await _email().dispatch(
        "send_email", {"to": "tim@example.com", "subject": "hi", "body": "hello"},
    )
    assert is_error and "email send failed" in text


# ---- send_file ----


def _telegram(tmp_path: Path, *, token="t", http=None) -> ToolDispatcher:
    from nous.api.telegram_tools import register_telegram_tools

    settings = MagicMock()
    settings.telegram_bot_token = token
    settings.telegram_chat_id = "12345"
    d = ToolDispatcher()
    register_telegram_tools(d, settings, http or AsyncMock())
    return d


@pytest.mark.asyncio
async def test_send_file_without_a_token_is_an_error(tmp_path):
    text, is_error = await _telegram(tmp_path, token="").dispatch("send_file", {"file_path": "x.png"})
    assert is_error, text


@pytest.mark.asyncio
async def test_send_file_missing_file_is_an_error(tmp_path):
    text, is_error = await _telegram(tmp_path).dispatch(
        "send_file", {"file_path": str(tmp_path / "missing.png")},
    )
    assert is_error and "File not found" in text


@pytest.mark.asyncio
async def test_send_file_api_rejection_is_an_error(tmp_path):
    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    response = MagicMock()
    response.status_code = 400
    response.json.return_value = {"ok": False, "description": "chat not found"}
    http = AsyncMock()
    http.post = AsyncMock(return_value=response)
    text, is_error = await _telegram(tmp_path, http=http).dispatch("send_file", {"file_path": str(png)})
    assert is_error and "chat not found" in text


# ---- identity ----


def _identity(manager) -> ToolDispatcher:
    from nous.identity.tools import register_identity_tools

    d = ToolDispatcher()
    register_identity_tools(d, manager)
    return d


@pytest.mark.asyncio
async def test_store_identity_invalid_section_is_an_error():
    text, is_error = await _identity(MagicMock()).dispatch(
        "store_identity", {"section": "nope", "content": "x"},
    )
    assert is_error and "Invalid section" in text


@pytest.mark.asyncio
async def test_store_identity_write_failure_is_an_error():
    manager = MagicMock()
    manager.update_section = AsyncMock(side_effect=RuntimeError("db down"))
    text, is_error = await _identity(manager).dispatch(
        "store_identity", {"section": "character", "content": "x"},
    )
    assert is_error and "Error storing" in text


@pytest.mark.asyncio
async def test_store_identity_success_is_not_an_error():
    manager = MagicMock()
    manager.update_section = AsyncMock()
    text, is_error = await _identity(manager).dispatch(
        "store_identity", {"section": "character", "content": "x"},
    )
    assert not is_error, text


@pytest.mark.asyncio
async def test_complete_initiation_missing_sections_is_an_error():
    manager = MagicMock()
    manager.get_current = AsyncMock(return_value={})
    text, is_error = await _identity(manager).dispatch("complete_initiation", {})
    assert is_error and "missing required sections" in text
    manager.mark_initiated.assert_not_called()


# ---- web ----


@pytest.mark.asyncio
async def test_web_fetch_rejected_url_is_an_error():
    from nous.api.web_tools import register_web_tools
    from nous.config import Settings

    d = ToolDispatcher()
    register_web_tools(d, Settings(_env_file=None), AsyncMock())
    text, is_error = await d.dispatch("web_fetch", {"url": "ftp://example.com/x"})
    assert is_error and "http" in text


# ---- structural lock ----


_HANDLER_MODULES = [
    "nous/api/builtin_tools.py",
    "nous/api/email_tools.py",
    "nous/api/telegram_tools.py",
    "nous/identity/tools.py",
    "nous/api/web_tools.py",
]


@pytest.mark.parametrize("path", _HANDLER_MODULES)
def test_except_branches_never_return_the_success_helper(path):
    """An ``except`` branch is a failure by construction. Returning it
    through the success helper is how every finding above was built, so the
    shape itself is locked out rather than each message."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    offenders = [
        f"{path}:{node.lineno}"
        for handler in ast.walk(tree) if isinstance(handler, ast.ExceptHandler)
        for node in ast.walk(handler)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id in {"_mcp_response", "_ok"}
    ]
    assert not offenders, "failures returned as successes:\n  " + "\n  ".join(offenders)
