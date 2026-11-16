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

``collect`` also records, for bash with the lineage shell off, the refusal ``_authorize_tool_call`` returns in each
mode (``auth_shell_off``; the offered names are read with the shell off too).

``collect_seams`` (async) records three more places, per context:

- ``tool_loop``: the ``goal_lifted`` value ``_tool_loop`` hands to ``_dispatch_with_ledger`` for each tool that reaches
  it, in one real tool loop whose model calls every tool once (the dispatch itself is a spy);
- ``dispatch``: the dispatcher's own arguments (those starting with ``_``) a real ``ToolDispatcher.dispatch`` passes to
  a recording bash handler, for a plain command, a build command, a runner.sh command, and a call that sends ``_``
  arguments of its own;
- ``bash_env``: the environment's variable NAMES and the stdin the registered bash tool starts its command with
  (``asyncio.create_subprocess_shell`` replaced by a recorder), for the same calls, while the process environment is
  ``SYNTHETIC_ENV``: names invented here, so the record never holds a real machine's variable names.

For a turn whose goal may run any command (none in this record), a bash call is meant to reach the tool with
``_lineage`` true and ``_lineage_build`` false, to start with stdin at end of file, and to see the lineage variables
(``_LINEAGE_BASH_ENV``) and nothing else.

Run as a script, it writes ``tests/fixtures/goal_grants_parity_baseline.json`` (``dumps``); ``load`` reads it back in
the shape ``collect`` returns, ``load_seams`` in the shape ``collect_seams`` returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from nous.api import tool_classes, tool_policy
from nous.api.builtin_tools import _BASH_SCHEMA, register_builtin_tools
from nous.api.execution_context import CONTEXT_KINDS, FOREGROUND_KINDS, ExecutionContext
from nous.api.models import Conversation, Message
from nous.api.runner import AgentRunner, Dispatched
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
    return {"offered": offered, "calls": calls, "auth_shell_off": _auth_shell_off(runner, settings, workspace_dir)}


def _auth_shell_off(runner: AgentRunner, settings: Settings, workspace_dir: str) -> dict[str, Any]:
    """bash's refusal (code and text, or None) in each mode with the lineage shell off, the offered names read with
    the shell off as well."""
    object.__setattr__(settings, "f099_lineage_shell", False)
    record: dict[str, Any] = {}
    try:
        for key, ctx in contexts("bash"):
            if ctx.kind == "approved_action":
                names = frozenset({"bash"})
            else:
                names = frozenset(_offered(runner, ctx, is_subtask=ctx.kind in ("subtask", "dag_node")))
            for index, sample in enumerate(({}, *SAMPLES["bash"])):
                refusals: dict[str, Any] = {}
                for mode in MODES:
                    object.__setattr__(settings, "tool_offered_set_enforcement_mode", mode)
                    object.__setattr__(settings, "tool_context_policy_mode", mode)
                    refusal = runner._authorize_tool_call(ctx, "bash", names, ctx.session_id, dict(sample))
                    refusals[mode] = (
                        None if refusal is None else [refusal.code, _normalised(refusal.text, workspace_dir)]
                    )
                record[f"{key}|bash|{index}"] = refusals
    finally:
        object.__setattr__(settings, "f099_lineage_shell", True)
        object.__setattr__(settings, "tool_offered_set_enforcement_mode", "warn")
        object.__setattr__(settings, "tool_context_policy_mode", "warn")
    return record


# ---- the tool loop, the dispatcher and the bash tool ----------------------------------------------------------

# The process environment while the bash tool runs: every name invented here, each value a placeholder.
SYNTHETIC_ENV: dict[str, str] = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/home/synthetic",
    "LANG": "C.UTF-8",
    "TZ": "UTC",
    "TERM": "dumb",
    "USER": "synthetic",
    "LOGNAME": "synthetic",
    "SHELL": "/bin/sh",
    "TMPDIR": "/tmp",
    "GITHUB_TOKEN": "synthetic-code-host-token",
    "GH_TOKEN": "synthetic-code-host-token",
    "GH_HOST": "code-host.example",
    "GH_REPO": "process/repo",
    "DB_PASSWORD": "synthetic-db-password",
    "DATABASE_URL": "postgresql://synthetic@db.example/nous",
    "ANTHROPIC_API_KEY": "synthetic-model-key",
    "OPENAI_API_KEY": "synthetic-model-key",
    "NOUS_TELEGRAM_BOT_TOKEN": "synthetic-chat-token",
    "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
    "NOUS_OWNER_ROUTE_SECRET": "synthetic-owner-secret",
    "NOUS_SYNTHETIC_OTHER": "synthetic",
}
# The bash calls the dispatcher and the bash tool are recorded with: a plain command, a build command, a runner.sh
# command, and a call that sends arguments named like the dispatcher's own.
BASH_CALLS: dict[str, dict[str, Any]] = {
    "plain": {"command": "ls"},
    "build": {"command": "git status"},
    "runner": {"command": "claude-jobs/runner.sh list"},
    "sent": {"command": "ls", "_lineage": False, "_lineage_build": True, "_goal_commands": True, "_gh_repo": "x/y"},
}
GH_REPO = "settings/repo"


def _jsonable(value: Any) -> Any:
    """A dispatcher argument as recorded: a cancel record without its time."""
    if isinstance(value, dict):
        return {k: v for k, v in sorted(value.items()) if k != "at"}
    return value


class _Recorder:
    """A bash handler that keeps the dispatcher's own arguments of each call."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.seen.append({k: _jsonable(v) for k, v in sorted(kwargs.items()) if k.startswith("_")})
        return {"content": [{"type": "text", "text": "ok"}]}


class _Process:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


class _Shell:
    """``asyncio.create_subprocess_shell`` as the bash tool calls it: keeps the variable names and the stdin."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def __call__(self, command: str, **kwargs: Any) -> _Process:
        stdin = kwargs.get("stdin")
        self.seen.append(
            {
                "env": sorted(kwargs["env"]),
                "stdin": "devnull" if stdin == asyncio.subprocess.DEVNULL else repr(stdin),
            }
        )
        return _Process()


async def collect_seams(
    prepare: Callable[[AgentRunner, list[ToolDispatcher]], None] | None = None,
) -> dict[str, Any]:
    """The record of the tool loop, the dispatcher and the bash tool (see the module). ``prepare`` is called with the
    tool-loop runner and the two dispatchers (the recording one and the one with the real bash tool) before anything
    is recorded."""
    saved = dict(tool_classes._MCP_TOOLS)
    tool_classes._MCP_TOOLS.clear()
    tool_classes._MCP_TOOLS.update(MCP_NOTED)
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)  # each refusal logs a line
    try:
        workspace_dir = workspace()
        runner, _settings = build(workspace_dir)
        bash_settings = settings_for(workspace_dir)
        object.__setattr__(bash_settings, "github_default_repo", GH_REPO)
        recording = ToolDispatcher()
        recorder = _Recorder()
        recording.register("bash", recorder, _BASH_SCHEMA)
        real = ToolDispatcher()
        register_builtin_tools(real, bash_settings)
        if prepare is not None:
            prepare(runner, [recording, real])
        return {
            "tool_loop": await _tool_loop_record(runner),
            **await _bash_records(recording, recorder, real),
        }
    finally:
        logging.disable(previous)
        tool_classes._MCP_TOOLS.clear()
        tool_classes._MCP_TOOLS.update(saved)


async def _tool_loop_record(runner: AgentRunner) -> dict[str, dict[str, bool]]:
    """Per context: each tool that reached ``_dispatch_with_ledger`` in one tool loop, with its ``goal_lifted``."""
    record: dict[str, dict[str, bool]] = {}
    for key, ctx in contexts("bash"):
        lifted: dict[str, bool] = {}

        async def spy(_ctx: Any, tool_name: str, _tool_input: Any, *, goal_lifted: bool = False, **_kw: Any):
            lifted[tool_name] = goal_lifted
            return Dispatched("ok", False, None, None, None)

        calls = [
            {"type": "tool_use", "id": f"toolu_{index:04d}", "name": tool, "input": dict(SAMPLES.get(tool, ({},))[0])}
            for index, tool in enumerate(TOOLS)
        ]
        steps = [calls, [{"type": "text", "text": "done"}]]

        async def model(*_args: Any, **_kwargs: Any) -> Any:
            from nous.api.models import ApiResponse

            blocks = steps.pop(0)
            stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
            return ApiResponse(content=blocks, stop_reason=stop, usage={"input_tokens": 1, "output_tokens": 1})

        runner._call_api = model  # type: ignore[method-assign]
        runner._dispatch_with_ledger = spy  # type: ignore[method-assign]
        conversation = Conversation(session_id=ctx.session_id)
        conversation.messages.append(Message(role="user", content="work"))
        await runner._tool_loop(
            "system",
            conversation,
            "task",
            session_id=ctx.session_id,
            is_subtask=ctx.kind in ("subtask", "dag_node"),
            context=ctx,
            is_background=ctx.is_background,
        )
        record[key] = dict(sorted(lifted.items()))
    return record


async def _bash_records(
    recording: ToolDispatcher, recorder: _Recorder, real: ToolDispatcher
) -> dict[str, dict[str, Any]]:
    """``dispatch`` and ``bash_env`` (see the module), per context and call."""
    dispatch: dict[str, Any] = {}
    bash_env: dict[str, Any] = {}
    shell = _Shell()
    for key, ctx in contexts("bash"):
        for name, call in BASH_CALLS.items():
            recorder.seen.clear()
            await recording.dispatch("bash", dict(call), session_id=ctx.session_id, context=ctx)
            [seen] = recorder.seen
            dispatch[f"{key}|{name}"] = seen
            shell.seen.clear()
            with patch.dict(os.environ, SYNTHETIC_ENV, clear=True), patch("asyncio.create_subprocess_shell", shell):
                await real.dispatch("bash", dict(call), session_id=ctx.session_id, context=ctx)
            [started] = shell.seen
            bash_env[f"{key}|{name}"] = started
    return {"dispatch": dispatch, "bash_env": bash_env}


def dumps(commit: str, record: dict[str, Any], seams: dict[str, Any], seams_commit: str) -> str:
    """The file's text: each refusal text of ``calls`` once, in ``texts``, and each call's refusals by their index
    there; then the sections added later (``auth_shell_off`` and ``seams``, texts written out, at ``seams_commit``);
    one entry per line."""
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
        f' "calls": {{\n{entries(calls)}\n }},\n'
        f' "seams_generated_at_commit": {json.dumps(seams_commit)},\n'
        f' "auth_shell_off": {{\n{entries(record["auth_shell_off"])}\n }},\n'
        f' "tool_loop": {{\n{entries(seams["tool_loop"])}\n }},\n'
        f' "dispatch": {{\n{entries(seams["dispatch"])}\n }},\n'
        f' "bash_env": {{\n{entries(seams["bash_env"])}\n }}\n'
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
    return data["generated_at_commit"], {
        "offered": data["offered"],
        "calls": calls,
        "auth_shell_off": data["auth_shell_off"],
    }


def load_seams() -> dict[str, Any]:
    """The record as ``collect_seams`` returns it."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {name: data[name] for name in ("tool_loop", "dispatch", "bash_env")}


def main() -> int:
    logging.disable(logging.CRITICAL)  # each refusal logs a line
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, cwd=Path(__file__).parent
    ).stdout.strip()
    # The first sections keep the commit they were first written at; the later ones are written at HEAD.
    commit = json.loads(FIXTURE.read_text(encoding="utf-8"))["generated_at_commit"] if FIXTURE.exists() else head
    record = collect()
    seams = asyncio.run(collect_seams())
    FIXTURE.write_text(dumps(commit, record, seams, head), encoding="utf-8", newline="\n")
    print(f"wrote {FIXTURE} ({len(record['calls'])} calls, {len(record['offered'])} offered sets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
