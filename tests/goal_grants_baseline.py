"""What the tool policy and the runner decide for a background turn, recorded as data.

``collect()`` builds a real ``AgentRunner`` over a dispatcher that registers every classified tool, a tool nobody
classified and three MCP client tools (one of a server available to background work, one of a server that is not, and
one the MCP client has not registered), with every mode and flag that these decisions read set explicitly. For every
execution-context kind, authority and root (none, or a root), every tool and a few sample inputs it records:

- ``internal_only_allowed`` with the lineage shell off and on;
- ``internal_only_call_violation`` with the lineage shell off and on;
- ``evaluate`` and ``mcp_client_violation``;
- the refusal ``_authorize_tool_call`` returns (its code and text, or None) with both enforcement modes off, at
  ``warn`` and at ``enforce``;

and, per context, the names ``_offered_tools`` offers with ``is_subtask`` and the lineage shell each off and on.
A refusal's text names the workspace as ``<ws>``, with ``/`` as the separator, so the record is the same on every
platform.

Run as a script, it writes ``tests/fixtures/goal_grants_parity_baseline.json`` with the commit it ran at (``dumps``);
``load`` reads it back in the shape ``collect`` returns.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from nous.api import tool_classes, tool_policy
from nous.api.execution_context import CONTEXT_KINDS, FOREGROUND_KINDS, ExecutionContext
from nous.api.runner import AgentRunner
from nous.api.tool_classes import TOOL_CLASSES
from nous.api.tools import ToolDispatcher
from nous.brain.intentions import AUTHORITIES, AUTHORITY_INTERNAL
from nous.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "goal_grants_parity_baseline.json"

ROOT = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
INTENTION = uuid.UUID("00000000-0000-4000-8000-0000000000b2")
PROPOSAL = uuid.UUID("00000000-0000-4000-8000-0000000000c3")
TASK = uuid.UUID("00000000-0000-4000-8000-0000000000d4")

UNKNOWN = "not_a_classified_tool"
MCP_ON = "mcp__notes__search"  # its server is available to background work
MCP_OFF = "mcp__mail__send"  # its server is not
MCP_GONE = "mcp__gone__thing"  # registered with the dispatcher, not with the MCP client
MCP_NOTED = {MCP_ON: True, MCP_OFF: False}
TOOLS: tuple[str, ...] = (*sorted(TOOL_CLASSES), UNKNOWN, MCP_ON, MCP_OFF, MCP_GONE)
SCHEMA = {"type": "object", "properties": {}}
MODES = ("off", "warn", "enforce")

# The inputs a call is recorded with: an empty one for every tool, and these besides.
SAMPLES: dict[str, tuple[dict[str, Any], ...]] = {
    "bash": ({"command": "ls"}, {"command": "curl https://example.com"}),
    "run_python": ({"code": "print(1)"}, {"code": "import requests"}),
    "write_file": (
        {"path": f"intentions/{ROOT}/note.md", "content": "x"},
        {"path": "elsewhere/note.md", "content": "x"},
    ),
    "cancel_task": ({"task_id": "not-a-uuid"}, {"task_id": str(TASK)}),
    "heartbeat_check_manage": ({"action": "disable", "name": "c"}, {"action": "enable", "name": "c"}),
}


def workspace() -> str:
    """A workspace directory that exists, so it resolves the same way at every call."""
    path = Path(tempfile.gettempdir()) / "nous-goal-grants-workspace"
    path.mkdir(exist_ok=True)
    return str(path)


def settings_for(workspace_dir: str) -> Settings:
    """Settings with every value these decisions read set here, not from the environment."""
    return Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key",
        workspace_dir=workspace_dir,
        f026_persistence_enabled=False,
        f099_lineage_shell=True,
        f099_lineage_build_commands=True,
        f099_lineage_write_dirs=[],
        tool_reason_enabled=False,
        tool_offered_set_enforcement_mode="warn",
        tool_context_policy_mode="warn",
    )


def build(workspace_dir: str) -> tuple[AgentRunner, Settings]:
    """A runner over a dispatcher with every tool of ``TOOLS`` registered."""
    dispatcher = ToolDispatcher()
    for name in TOOLS:
        dispatcher.register(name, MagicMock(), SCHEMA)
    settings = settings_for(workspace_dir)
    runner = AgentRunner(MagicMock(), None, MagicMock(), settings)
    runner.set_dispatcher(dispatcher)
    return runner, settings


def contexts(tool: str) -> Iterator[tuple[str, ExecutionContext]]:
    """Every valid (kind, authority, root) context, keyed ``kind|authority|root``. An approved action declares
    ``tool``, the one call it runs."""
    for kind in CONTEXT_KINDS:
        for authority in AUTHORITIES:
            for rooted in (False, True):
                if kind == "continuation" and not (authority == AUTHORITY_INTERNAL and rooted):
                    continue
                extra: dict[str, Any] = {}
                if rooted:
                    extra = {"intention_id": INTENTION, "root_intention_id": ROOT}
                if kind == "approved_action":
                    extra |= {"proposal_id": PROPOSAL, "declared_tools": (tool,)}
                session_id = "chat-1" if kind in FOREGROUND_KINDS else "subtask-1"
                ctx = ExecutionContext(kind=kind, session_id=session_id, authority=authority, **extra)
                yield f"{kind}|{authority}|{'root' if rooted else 'none'}", ctx


def _normalised(text: str, workspace_dir: str) -> str:
    resolved = str(Path(workspace_dir).resolve())
    return text.replace(resolved, "<ws>").replace(workspace_dir, "<ws>").replace("\\", "/")


def _offered(runner: AgentRunner, ctx: ExecutionContext, *, is_subtask: bool) -> list[str]:
    tools = runner._offered_tools(ctx, "task", is_subtask=is_subtask, tool_filter=None, refuse_active=False)
    return [tool["name"] for tool in tools]


def collect(prepare: Callable[[AgentRunner], None] | None = None) -> dict[str, Any]:
    """The record: ``offered`` (per context, ``is_subtask`` and lineage shell) and ``calls`` (per context, tool and
    sample). ``prepare`` is called with the runner before anything is recorded."""
    saved = dict(tool_classes._MCP_TOOLS)
    tool_classes._MCP_TOOLS.clear()
    tool_classes._MCP_TOOLS.update(MCP_NOTED)
    try:
        return _collect(prepare)
    finally:
        tool_classes._MCP_TOOLS.clear()
        tool_classes._MCP_TOOLS.update(saved)


def _collect(prepare: Callable[[AgentRunner], None] | None) -> dict[str, Any]:
    workspace_dir = workspace()
    runner, settings = build(workspace_dir)
    if prepare is not None:
        prepare(runner)

    offered: dict[str, list[str]] = {}
    for key, ctx in contexts("send_email"):
        for is_subtask in (False, True):
            for shell in (False, True):
                object.__setattr__(settings, "f099_lineage_shell", shell)
                offered[f"{key}|subtask={is_subtask}|shell={shell}"] = _offered(runner, ctx, is_subtask=is_subtask)
    object.__setattr__(settings, "f099_lineage_shell", True)

    calls: dict[str, Any] = {}
    for tool in TOOLS:
        samples = ({}, *SAMPLES.get(tool, ()))
        for key, ctx in contexts(tool):
            if ctx.kind == "approved_action":
                names = frozenset({tool})
            else:
                is_subtask = ctx.kind in ("subtask", "dag_node")
                names = frozenset(_offered(runner, ctx, is_subtask=is_subtask))
            for index, sample in enumerate(samples):
                row: dict[str, Any] = {
                    "allowed": [
                        tool_policy.internal_only_allowed(tool, ctx=ctx, lineage_shell=shell) for shell in (False, True)
                    ],
                    "call": [
                        tool_policy.internal_only_call_violation(
                            ctx,
                            tool,
                            sample,
                            workspace_dir=workspace_dir,
                            lineage_shell=shell,
                            build_commands=True,
                        )
                        for shell in (False, True)
                    ],
                    "evaluate": tool_policy.evaluate(ctx, tool, sample, workspace_dir=workspace_dir),
                    "mcp": tool_policy.mcp_client_violation(ctx.kind, tool, names),
                }
                refusals: dict[str, Any] = {}
                for mode in MODES:
                    object.__setattr__(settings, "tool_offered_set_enforcement_mode", mode)
                    object.__setattr__(settings, "tool_context_policy_mode", mode)
                    refusal = runner._authorize_tool_call(ctx, tool, names, ctx.session_id, dict(sample))
                    refusals[mode] = (
                        None if refusal is None else [refusal.code, _normalised(refusal.text, workspace_dir)]
                    )
                row["auth"] = refusals
                calls[f"{key}|{tool}|{index}"] = row
    object.__setattr__(settings, "tool_offered_set_enforcement_mode", "warn")
    object.__setattr__(settings, "tool_context_policy_mode", "warn")
    return {"offered": offered, "calls": calls}


def dumps(commit: str, record: dict[str, Any]) -> str:
    """The file's text: each refusal text once, in ``texts``, and each call's refusals by their index there; one
    entry per line."""
    texts: list[str] = sorted(
        {refusal[1] for row in record["calls"].values() for refusal in row["auth"].values() if refusal is not None}
    )
    index = {text: position for position, text in enumerate(texts)}
    calls = {
        key: {
            **row,
            "auth": {
                mode: None if refusal is None else [refusal[0], index[refusal[1]]]
                for mode, refusal in row["auth"].items()
            },
        }
        for key, row in record["calls"].items()
    }

    def block(items: list[str]) -> str:
        return ",\n".join(f"  {item}" for item in items)

    def entries(mapping: dict[str, Any]) -> str:
        return block([f"{json.dumps(key)}: {json.dumps(mapping[key], sort_keys=True)}" for key in sorted(mapping)])

    return (
        "{\n"
        f' "generated_at_commit": {json.dumps(commit)},\n'
        f' "texts": [\n{block([json.dumps(text) for text in texts])}\n ],\n'
        f' "offered": {{\n{entries(record["offered"])}\n }},\n'
        f' "calls": {{\n{entries(calls)}\n }}\n'
        "}\n"
    )


def load() -> tuple[str, dict[str, Any]]:
    """The commit the file was written at, and the record as ``collect`` returns it."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    texts = data["texts"]
    calls = {
        key: {
            **row,
            "auth": {
                mode: None if refusal is None else [refusal[0], texts[refusal[1]]]
                for mode, refusal in row["auth"].items()
            },
        }
        for key, row in data["calls"].items()
    }
    return data["generated_at_commit"], {"offered": data["offered"], "calls": calls}


def main() -> int:
    logging.disable(logging.CRITICAL)  # each refusal logs a line
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, cwd=Path(__file__).parent
    ).stdout.strip()
    record = collect()
    FIXTURE.write_text(dumps(commit, record), encoding="utf-8", newline="\n")
    print(f"wrote {FIXTURE} ({len(record['calls'])} calls, {len(record['offered'])} offered sets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
