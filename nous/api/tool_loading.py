"""F120 tool loading, watch only: what a model call would leave out if only a core set of tools were sent in full.

Nothing here changes a request. The owner chooses on the Limits and tools screen a mode (``off`` or ``watch``), the
kinds of call it applies to (``KINDS``) and the core set. ``RuntimeConfig`` holds the choice, stored in
``nous_system.config`` under ``tool_loading:<agent_id>`` (``config_key``), and the runner reads it at each model call.
In ``watch`` mode a call of a switched-on kind gets a record (``call_record``) in ``model_call_log.tool_loading``: how
many of the tools it sent would have been left out, their estimated tokens, the estimated tokens of the list of names
that would replace them (``index_text``, built for its size only), and the names kept in full. The recorder adds
whether the call read the tools from the prompt cache or wrote them.

A tool is kept in full when it is in the core set or pinned (``PINNED``): the harness's own per-turn tools, a tool a
harness step forces, a tool the prompts tell the model to call by name, and ``commit``. A turn of internal authority,
a turn with its own ``tool_filter`` and the initiation frame are never watched (``kind_of_turn``).

Pure: no database and no I/O. ``parse_tool_loading`` never raises.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from nous.api.execution_context import ExecutionContext
from nous.api.tool_classes import MCP_TOOL_PREFIX
from nous.autonomy.tool_guard import HARNESS_TOOLS
from nous.brain.intentions import AUTHORITY_INTERNAL

MODE_OFF = "off"
MODE_WATCH = "watch"
MODES: tuple[str, ...] = (MODE_OFF, MODE_WATCH)
VERSION = 1
KEY_PREFIX = "tool_loading:"

# The kinds of call the owner switches, in the screen's order, with their words.
KINDS: MappingProxyType[str, str] = MappingProxyType(
    {
        "chat": "Chat and calls from other agents",
        "subtask": "Background tasks and scheduled tasks",
        "workflow_step": "Workflow steps",
        "heartbeat": "Heartbeat triage, checks and their callbacks",
        "other_background": "Workflow summaries, companion taps and other background turns",
    }
)
# Every execution-context kind and the kind of call it is; None is never watched. A test holds the keys equal to
# CONTEXT_KINDS.
KIND_OF_CONTEXT: MappingProxyType[str, str | None] = MappingProxyType(
    {
        "interactive": "chat",
        "mcp": "chat",
        "subtask": "subtask",
        "scheduled": "subtask",
        "dag_node": "workflow_step",
        "heartbeat_triage": "heartbeat",
        "heartbeat_check": "heartbeat",
        "heartbeat_callback": "heartbeat",
        "dag_summary": "other_background",
        "background": "other_background",
        "agent_action": "other_background",
        "continuation": None,  # a chain decision: always internal authority
        "approved_action": None,  # runs one tool with no model call
    }
)
INITIATION_FRAME = "initiation"

# Tools a harness step forces with tool_choice: the hardened subtask's final report (subtask_executor.py).
FORCED_TOOLS: frozenset[str] = frozenset({"submit_final_report"})
# Tools the prompts tell the model to call by name: the frame instructions (AgentRunner._get_frame_instructions) and
# the procedure lines of the system prompt (cognitive/context.py). A test reads every prompt string in nous/ and fails
# when a tool named there in backticks is not pinned.
PROMPT_TOOLS: frozenset[str] = frozenset(
    {
        "bash",
        "get_procedure",
        "learn_fact",
        "read_file",
        "recall_deep",
        "record_decision",
        "run_python",
        "web_fetch",
        "web_search",
        "write_file",
    }
)
# commit: every frame may record an obligation (FRAME_TOOLS), and the promise check reads a commit call as its record.
OBLIGATION_TOOLS: frozenset[str] = frozenset({"commit"})
# Each pinned tool and why it is always kept in full, in words. A tool in two groups is given the later group's reason
# (the subtask's final report: the harness's).
PINNED: MappingProxyType[str, str] = MappingProxyType(
    {
        **dict.fromkeys(sorted(OBLIGATION_TOOLS), "It records an obligation the promise check reads."),
        **dict.fromkeys(sorted(PROMPT_TOOLS), "The instructions tell the model to call it by name."),
        **dict.fromkeys(sorted(FORCED_TOOLS), "A harness step can require it."),
        **dict.fromkeys(sorted(HARNESS_TOOLS), "The harness adds it to a turn to end or record that turn."),
    }
)
# The core set when the owner has chosen none: the tools used in at least 3 percent of the sessions that used a tool,
# over 30 days of one deployment's tool-call log.
DEFAULT_CORE: tuple[str, ...] = (
    "bash",
    "db_query",
    "learn_fact",
    "read_file",
    "recall_deep",
    "run_python",
    "web_fetch",
    "web_search",
    "write_file",
)
CORE_MAX = 200
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# What a cached input token weighs against an uncached one, for the card's saving: read from the cache, and written to
# it. Approximations of the published ratios, not prices; the read ratio is lower on some models.
READ_RATIO = 0.1
WRITE_RATIO = 1.25

# The list that would replace the left-out tools: one line per tool of Nous, the names of each other server's tools.
INDEX_HEADER = (
    "Some of your tools are not attached to save space. To use one, call load_tools with its name; its full "
    "definition is attached from your next step. You can load several at once. A name listed here is a real tool you "
    "may use."
)
INDEX_LINE_CHARS = 90
INDEX_NAMES_PER_SERVER = 40
LOADER_NAME = "load_tools"
LOADER_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "properties": {"names": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 40}},
        "required": ["names"],
    }
)


@dataclass(frozen=True)
class ToolLoadingConfig:
    """The owner's setting: the mode, the kinds of call switched on, the core set (None: ``DEFAULT_CORE``), and when
    and by whom it was last changed (the proof the owner guard accepted)."""

    mode: str = MODE_OFF
    kinds: frozenset[str] = frozenset()
    core: tuple[str, ...] | None = None
    updated_at: str | None = None
    updated_by: str | None = None

    def core_names(self) -> tuple[str, ...]:
        """The core set in effect."""
        return DEFAULT_CORE if self.core is None else self.core

    def kept(self) -> frozenset[str]:
        """Every name kept in full: the core set and the pinned tools."""
        return frozenset(self.core_names()) | frozenset(PINNED)

    def to_json(self) -> dict[str, Any]:
        """The stored object."""
        return {
            "version": VERSION,
            "mode": self.mode,
            "kinds": [kind for kind in KINDS if kind in self.kinds],
            "core": None if self.core is None else list(self.core),
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


OFF = ToolLoadingConfig()
_FIELDS = frozenset({"version", "mode", "kinds", "core", "updated_at", "updated_by"})


def config_key(agent_id: str) -> str:
    """The ``nous_system.config`` key of one agent's setting."""
    return f"{KEY_PREFIX}{agent_id}"


def _names(value: Any, allowed: Any = None) -> tuple[str, ...] | None:
    """A list of distinct names (each in ``allowed`` when given, else a tool name), or None when it is not one."""
    if not isinstance(value, list) or len(value) > CORE_MAX:
        return None
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or item in names:
            return None
        if (item not in allowed) if allowed is not None else not NAME_PATTERN.match(item):
            return None
        names.append(item)
    return tuple(names)


def parse_tool_loading(value: Any) -> tuple[ToolLoadingConfig, str | None]:
    """The stored value as a setting, and None; or Off and a sentence that says which part could not be read (never
    the value). Never raises."""
    try:
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            return OFF, "the stored value is not an object"
        unknown = sorted(set(value) - _FIELDS)
        if unknown:
            return OFF, "the stored value has a field this version does not know"
        if value.get("version") != VERSION:
            return OFF, "the stored value has another version"
        mode = value.get("mode")
        if mode not in MODES:
            return OFF, "the stored mode is not off or watch"
        kinds = _names(value.get("kinds", []), KINDS)
        if kinds is None:
            return OFF, "the stored kinds of call are not a list of known kinds"
        core_value = value.get("core")
        core = None if core_value is None else _names(core_value)
        if core_value is not None and core is None:
            return OFF, "the stored core set is not a list of tool names"
        updated_at = value.get("updated_at")
        updated_by = value.get("updated_by")
        if not (updated_at is None or isinstance(updated_at, str)) or updated_by not in (None, "owner", "proxy"):
            return OFF, "the stored last change is not readable"
        return ToolLoadingConfig(mode, frozenset(kinds), core, updated_at, updated_by), None
    except Exception as exc:  # noqa: BLE001 -- a stored value must never stop a start or a call
        return OFF, f"the stored value could not be read ({type(exc).__name__})"


def kind_of_turn(ctx: ExecutionContext, frame_id: str | None, tool_filter: Sequence[str] | None) -> str | None:
    """The kind of call a turn's model calls are, or None when the turn is never watched: internal authority, its
    own ``tool_filter``, the initiation frame, or a kind with no kind of call."""
    if ctx.authority == AUTHORITY_INTERNAL or tool_filter is not None or frame_id == INITIATION_FRAME:
        return None
    return KIND_OF_CONTEXT.get(ctx.kind)


def tokens_est(value: Any) -> int:
    """Estimated tokens of a value sent as JSON: 4 characters a token, the estimate ``model_call_log`` uses."""
    return len(json.dumps(value)) // 4


def first_sentence(description: Any) -> str:
    """A tool's index line: the first sentence of its description, an MCP ``[server]`` prefix removed, whitespace
    collapsed, cut at a word to at most ``INDEX_LINE_CHARS`` characters."""
    text = " ".join(str(description or "").split())
    text = re.sub(r"^\[[^\]]*\]\s*", "", text)
    end = text.find(". ")
    if end != -1:
        text = text[: end + 1]
    if len(text) > INDEX_LINE_CHARS:
        cut = text[: INDEX_LINE_CHARS + 1].rsplit(" ", 1)[0]
        text = cut if 0 < len(cut) <= INDEX_LINE_CHARS else text[:INDEX_LINE_CHARS]
    return text


def index_text(deferred: Sequence[Mapping[str, Any]]) -> str:
    """The list of names that would replace ``deferred`` (sorted, so a reload never changes it): a line per tool of
    Nous, and per other server its tool names, at most ``INDEX_NAMES_PER_SERVER``."""
    nous_lines: list[str] = []
    servers: dict[str, list[str]] = {}
    for tool in deferred:
        name = str(tool.get("name", ""))
        if name.startswith(MCP_TOOL_PREFIX):
            server, _, short = name[len(MCP_TOOL_PREFIX) :].partition("__")
            servers.setdefault(server, []).append(short or name)
        else:
            nous_lines.append(f"- {name}: {first_sentence(tool.get('description'))}")
    lines = [INDEX_HEADER, "", "Tools:", *sorted(nous_lines)]
    if servers:
        lines += ["", "Tools of other servers (call one by its full name, mcp__<server>__<tool>):"]
        for server in sorted(servers):
            names = sorted(servers[server])
            shown = names[:INDEX_NAMES_PER_SERVER]
            more = f", and {len(names) - len(shown)} more" if len(names) > len(shown) else ""
            lines.append(f"- {server} ({len(names)} tools): {', '.join(shown)}{more}")
    return "\n".join(lines)


def index_tokens(deferred: Sequence[Mapping[str, Any]]) -> int:
    """Estimated tokens of the loader tool that would carry the list of ``deferred``; 0 when nothing is left out."""
    if not deferred:
        return 0
    return tokens_est({"name": LOADER_NAME, "description": index_text(deferred), "input_schema": dict(LOADER_SCHEMA)})


def split(tools: Sequence[Mapping[str, Any]], config: ToolLoadingConfig) -> tuple[list[str], list[Mapping[str, Any]]]:
    """``(names kept in full, definitions left out)`` of ``tools``, each in the order sent. Reads ``tools`` only."""
    kept = config.kept()
    names: list[str] = []
    deferred: list[Mapping[str, Any]] = []
    for tool in tools:
        name = tool.get("name")
        if name in kept:
            names.append(str(name))
        else:
            deferred.append(tool)
    return names, deferred


def call_record(config: ToolLoadingConfig, kind: str | None, tools: Sequence[Mapping[str, Any]] | None) -> dict | None:
    """The record of one model call that sends ``tools``, or None when this call is not watched: the mode is not
    ``watch``, the turn is never watched (``kind`` None), its kind is switched off, or it sends no tool."""
    if config.mode != MODE_WATCH or kind is None or kind not in config.kinds or not tools:
        return None
    kept, deferred = split(tools, config)
    return {
        "mode": MODE_WATCH,
        "kind": kind,
        "sent": len(tools),
        "deferred": len(deferred),
        "deferred_tokens_est": sum(tokens_est(tool) for tool in deferred),
        "index_tokens_est": index_tokens(deferred),
        "kept": sorted(kept),
    }
