"""F120 tool loading: what a model call leaves out, or would leave out, when only a core set of tools is sent in full.

The owner chooses on the Limits and tools screen a mode (``off``, ``watch`` or ``on``), the kinds of call it applies
to (``KINDS``) and the core set. ``RuntimeConfig`` holds the choice, stored in ``nous_system.config`` under
``tool_loading:<agent_id>`` (``config_key``), and the runner reads it at each model call. In ``watch`` mode nothing
changes a request: a call of a switched-on kind gets a record (``call_record``): how many of the tools it sent would
have been left out, their estimated tokens, the estimated tokens of the list of names that would replace them
(``index_text``), and the names kept in full. The model-call recorder writes it, with the call's model and whether the
call read the tools from the prompt cache or wrote them, as a row of ``nous_system.tool_loading_watch`` keyed by the
call's id, after the call's own ``model_call_log`` row.

In ``on`` mode a turn of a switched-on kind opens a ``ToolWindow`` when every condition holds (``why_not``: the model
client can defer, the call's model is in ``DEFERRING_MODELS``, something would be left out). Its requests send every
tool the turn may use: the kept ones in full, the others with ``defer_loading``, and last Nous's own loader
(``LOADER_NAME``, not a registered tool), whose description lists the deferred names. The loader answers with
``tool_reference`` blocks, which the API expands into the tools' definitions. The array is fixed for the turn. A call
of a deferred tool the model had not loaded is never run (``ToolWindow.answer``). The API refusing a request (a 400)
or the window's own code failing sends the same tools in full for the rest of the turn. Every call of a switched-on
kind records what it sent (``on_record``).

A tool is kept in full when it is in the core set or pinned (``PINNED``): the harness's own per-turn tools, a tool a
harness step forces, a tool the prompts tell the model to call by name, and ``commit``. A turn of internal authority,
a turn with its own ``tool_filter`` and the initiation frame are never watched or deferred (``kind_of_turn``).

Pure: no database and no I/O. ``parse_tool_loading`` never raises.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from nous.api.execution_context import ExecutionContext
from nous.api.tool_classes import MCP_TOOL_PREFIX
from nous.autonomy.tool_guard import HARNESS_TOOLS
from nous.brain.intentions import AUTHORITY_INTERNAL

MODE_OFF = "off"
MODE_WATCH = "watch"
MODE_ON = "on"
MODES: tuple[str, ...] = (MODE_OFF, MODE_WATCH, MODE_ON)
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
# Tools the prompts tell the model to call by name: the frame instructions (AgentRunner._get_frame_instructions), the
# procedure lines of the system prompt (cognitive/context.py), the identity prompt (create_censor), the companion tap's
# prompt (compose_surface), the marker of a compressed tool result (cache_retrieve) and the result inbox's line on
# older results (list_tasks, dag_manage). Two tests read every prompt string in nous/ and fail when a tool named there
# is not pinned (test_tool_loading.py in backticks, test_tool_loading_prompt_names.py with or without).
PROMPT_TOOLS: frozenset[str] = frozenset(
    {
        "bash",
        "cache_retrieve",
        "compose_surface",
        "create_censor",
        "dag_manage",
        "get_procedure",
        "learn_fact",
        "list_tasks",
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

# The list that replaces the left-out tools (the loader's description): one line per tool of Nous, and per other server
# the full name of each of its tools, every name as the loader takes it.
INDEX_HEADER = (
    "Some of your tools are not attached to save space. To use one, call load_tools with its name; its full "
    "definition is attached from your next step. You can load several at once. A name listed here is a real tool you "
    "may use."
)
INDEX_SERVERS_LINE = "Tools of other servers (load one by its full name, as listed):"
INDEX_LINE_CHARS = 90
LOADER_NAME = "load_tools"
LOADER_MAX_NAMES = 20
LOADER_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "properties": {
            "names": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": LOADER_MAX_NAMES}
        },
        "required": ["names"],
    }
)

# On. The model ids a request may send deferred tools to. Each passed scripts/ops/tool_loading_smoke.py in production
# through the SDK client (deferred definitions, the loader's references, adaptive thinking, streaming and the
# aggregated stream): claude-sonnet-5-5 and claude-opus-5-5 the checks it holds, claude-fable-5-2 the whole script.
# An id is added here only after the smoke script passes on it.
DEFERRING_MODELS: frozenset[str] = frozenset({"claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5"})
# The loader's calls and not-loaded answers of a turn that are not counted as tool calls; each later one is.
FREE_LOADS_PER_TURN = 8
# The characters of a name the model sent that an answer repeats.
NAME_ECHO_CHARS = 64
# The tool_call_log outcomes of a loader call that loaded a tool, of one that loaded none, and of a call of a deferred
# tool the model had not loaded (never run). None of them is a tool failure.
OUTCOME_LOADED = "loaded"
OUTCOME_NOTHING_LOADED = "nothing_loaded"
OUTCOME_NOT_LOADED = "not_loaded"
LOADER_OUTCOMES: tuple[str, ...] = (OUTCOME_LOADED, OUTCOME_NOTHING_LOADED, OUTCOME_NOT_LOADED)
# Why On sent a call of a switched-on kind in full (``why_not``): the model client cannot defer, the call's model is
# not in DEFERRING_MODELS, a tool the turn may use is named like the loader, every tool is kept, the turn began before
# On was chosen, or opening the turn's window failed.
WHY_CLIENT = "client"
WHY_MODEL = "model"
WHY_NAME = "name"
WHY_NOTHING = "nothing"
WHY_TURN = "turn"
WHY_FAULT = "fault"
# Why a turn that deferred sent its later calls in full: the API refused a request (a 400), or the window's own code
# failed.
FALLBACK_REFUSED = "refused"
FALLBACK_FAULT = "fault"
LOADER_BAD_INPUT = f"Nothing was loaded: {LOADER_NAME} takes names, a list of the tool names its description lists."
LOADER_ALL_ATTACHED = (
    "Nothing needs loading: every tool is attached in full from your next step. Call the one you need."
)
LOADER_UNREADABLE = (
    f"Nothing was loaded: this call's input could not be read. Call {LOADER_NAME} as "
    '{"names": ["tool_name"]}, with names from its description.'
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
            return OFF, "the stored mode is not off, watch or on"
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
    """The kind of call a turn's model calls are, or None when the turn is never watched or deferred: internal
    authority, its own ``tool_filter``, the initiation frame, or a kind with no kind of call."""
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


def _server_of(name: str) -> tuple[str, str] | None:
    """``(server, short name)`` of another server's tool (``mcp__<server>__<tool>``), else None."""
    if not name.startswith(MCP_TOOL_PREFIX):
        return None
    server, _, short = name[len(MCP_TOOL_PREFIX) :].partition("__")
    return server, short or name


def index_text(deferred: Sequence[Mapping[str, Any]]) -> str:
    """The list of names that replaces ``deferred`` (sorted, so a reload never changes it): a line per tool of Nous,
    and per other server the full name of every one of its tools, each as the loader takes it."""
    nous_lines: list[str] = []
    servers: dict[str, list[str]] = {}
    for tool in deferred:
        name = str(tool.get("name", ""))
        of = _server_of(name)
        if of is not None:
            servers.setdefault(of[0], []).append(name)
        else:
            nous_lines.append(f"- {name}: {first_sentence(tool.get('description'))}")
    lines = [INDEX_HEADER, "", "Tools:", *sorted(nous_lines)]
    if servers:
        lines += ["", INDEX_SERVERS_LINE]
        for server in sorted(servers):
            names = sorted(servers[server])
            lines.append(f"- {server} ({len(names)} tools): {', '.join(names)}")
    return "\n".join(lines)


def loader_tool(deferred: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The loader's definition: its description is the list of ``deferred``."""
    return {"name": LOADER_NAME, "description": index_text(deferred), "input_schema": dict(LOADER_SCHEMA)}


def index_tokens(deferred: Sequence[Mapping[str, Any]]) -> int:
    """Estimated tokens of the loader tool that would carry the list of ``deferred``; 0 when nothing is left out."""
    if not deferred:
        return 0
    return tokens_est(loader_tool(deferred))


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


def call_record(
    config: ToolLoadingConfig, kind: str | None, tools: Sequence[Mapping[str, Any]] | None, why: str | None = None
) -> dict | None:
    """The record of one model call that sends ``tools`` outside a ``ToolWindow``, or None when this call is not
    watched: the mode is Off, the turn is never watched (``kind`` None), its kind is switched off, or it sends no tool.
    In Watch only, what it would have left out; in On, that it was sent in full and why (``why``, else ``WHY_TURN``)."""
    if kind is None or kind not in config.kinds or not tools:
        return None
    if config.mode == MODE_ON:
        return on_record(kind, tools, why=why or WHY_TURN)
    if config.mode != MODE_WATCH:
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


# ---- On ----------------------------------------------------------------------------------------------------------


def _is_deferred(tool: Mapping[str, Any]) -> bool:
    return tool.get("defer_loading") is True


def _definition(tool: Mapping[str, Any]) -> dict[str, Any]:
    """A tool's definition without the deferral flag."""
    return {key: value for key, value in tool.items() if key != "defer_loading"}


def on_record(
    kind: str,
    tools: Sequence[Mapping[str, Any]],
    *,
    why: str | None = None,
    loaded: Sequence[str] = (),
    fallback: str | None = None,
) -> dict[str, Any]:
    """The record of one model call in On that sends ``tools``: whether it sent tools deferred (``applied``), why not
    (``why``, outside a window) or after which fault (``fallback``), how many tools it sent in full (the loader not
    counted), how many it left out and their definitions' estimated tokens, the loader's (its list of names), and how
    many tools the turn had loaded when the call was made (``loaded``, the names) with the estimated tokens of the
    loaded definitions the call carried in its messages. A deferred tool already loaded is not left out: its definition
    reaches the model through the messages, so it is counted there and not in the saving (``deferred_tokens_est`` less
    ``index_tokens_est``)."""
    names = set(loaded)
    flagged = [tool for tool in tools if _is_deferred(tool)]
    deferred = [tool for tool in flagged if tool.get("name") not in names]
    carried = [tool for tool in flagged if tool.get("name") in names]
    loader = [tool for tool in tools if tool.get("name") == LOADER_NAME and not _is_deferred(tool)]
    return {
        "mode": MODE_ON,
        "kind": kind,
        "sent": len(tools),
        "applied": bool(flagged),
        "why": why,
        "fallback": fallback,
        "full": len(tools) - len(flagged) - len(loader),
        "deferred": len(deferred),
        "deferred_tokens_est": sum(tokens_est(_definition(tool)) for tool in deferred),
        "index_tokens_est": sum(tokens_est(tool) for tool in loader),
        "loaded": len(loaded),
        "loaded_tokens_est": sum(tokens_est(_definition(tool)) for tool in carried),
    }


def why_not(
    config: ToolLoadingConfig, tools: Sequence[Mapping[str, Any]] | None, *, deferring_client: bool, model: str | None
) -> str | None:
    """Why a call in On that sends ``tools`` to ``model`` cannot defer any, or None when it can: the model client
    cannot (only the SDK client was tested), the model is not in ``DEFERRING_MODELS``, a tool is already named like
    the loader (a request may not name two tools alike), or every tool is kept."""
    if not deferring_client:
        return WHY_CLIENT
    if model not in DEFERRING_MODELS:
        return WHY_MODEL
    if any(tool.get("name") == LOADER_NAME for tool in tools or ()):
        return WHY_NAME
    kept = config.kept()
    if not any(tool.get("name") not in kept for tool in tools or ()):
        return WHY_NOTHING
    return None


def sent_in_full(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """``tools`` without the definitions sent deferred, which are neither in the cached prefix nor read as sent;
    ``tools`` itself when none is deferred."""
    if not tools or not any(_is_deferred(tool) for tool in tools):
        return tools
    return [tool for tool in tools if not _is_deferred(tool)]


def _references(block: Any) -> list[str]:
    """The tool names a tool result's ``tool_reference`` blocks name; none for any other block."""
    if not isinstance(block, Mapping) or block.get("type") != "tool_result":
        return []
    content = block.get("content")
    if not isinstance(content, list):
        return []
    return [
        str(item.get("tool_name"))
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "tool_reference"
    ]


def holds_reference(block: Any) -> bool:
    """Whether ``block`` is a tool result that loaded tools (holds ``tool_reference`` blocks)."""
    return bool(_references(block))


def carries_reference(messages: Sequence[Mapping[str, Any]]) -> bool:
    """Whether any of ``messages`` holds a tool result that loaded tools."""
    return any(
        holds_reference(block)
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
    )


def references_outside(
    messages: list[dict[str, Any]], tools: Sequence[Mapping[str, Any]] | None
) -> list[dict[str, Any]]:
    """``messages`` with every tool result that references a tool ``tools`` does not hold replaced by one text block
    naming what it loaded: the API refuses (400) a request whose messages reference a tool it does not send. A
    request that sends no tools gets every reference replaced. ``messages`` itself when no reference needs it; the
    list and its dicts are never changed."""
    names = {tool.get("name") for tool in tools or ()}
    out: list[dict[str, Any]] | None = None
    for i, message in enumerate(messages):
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            continue
        blocks: list[Any] | None = None
        for j, block in enumerate(content):
            loaded = _references(block)
            if loaded and any(name not in names for name in loaded):
                blocks = list(content) if blocks is None else blocks
                blocks[j] = {**block, "content": [{"type": "text", "text": f"Loaded: {', '.join(loaded)}."}]}
        if blocks is not None:
            out = list(messages) if out is None else out
            out[i] = {**message, "content": blocks}
    return messages if out is None else out


def note_block(notes: Sequence[str]) -> list[dict[str, Any]]:
    """The text block that follows a step's tool results with what its loads did not load (``LoaderAnswer.note``);
    none when there is nothing to say. A result that loads a tool may hold nothing but its references."""
    return [{"type": "text", "text": "\n".join(notes)}] if notes else []


@dataclass(frozen=True)
class LoaderAnswer:
    """The answer to a call of the loader or of a deferred tool not loaded yet: the tool result's ``content`` (only
    ``tool_reference`` blocks, or text) and ``is_error`` as sent, the ``tool_call_log`` outcome and text, the words for
    the text block after the step's tool results (``note``: what a call that loaded something did not load), and
    whether the call counts as a tool call (``counted``: past ``FREE_LOADS_PER_TURN``)."""

    content: str | list[dict[str, Any]]
    is_error: bool
    outcome: str
    text: str
    note: str | None = None
    counted: bool = False

    def result(self, tool_use_id: str) -> dict[str, Any]:
        """The ``tool_result`` block for the call ``tool_use_id``."""
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": self.content, "is_error": self.is_error}


def _nothing(text: str) -> LoaderAnswer:
    return LoaderAnswer(content=text, is_error=False, outcome=OUTCOME_NOTHING_LOADED, text=text)


def _echo(names: Sequence[str]) -> str:
    return ", ".join(name[:NAME_ECHO_CHARS] for name in names)


def _same(name: str) -> str:
    return name


@dataclass(eq=False)
class ToolWindow:
    """One turn's tool loading in On (``open_window``), kept in the turn's own locals, never between turns.

    ``tools`` is what every request of the turn sends, fixed when the turn starts whatever changes later (the mode, the
    core set, the registered tools): the turn's tools in their order, those outside the kept set with
    ``defer_loading``, and the loader last. ``fallback`` is the same tools in full, sent once the window is broken (the
    API refused a request, or the window's own code failed) for the rest of the turn. ``loaded`` are the tools the
    turn's loader calls referenced; the references in the turn's own messages are what keeps them loaded."""

    kind: str
    tools: list[dict[str, Any]]
    fallback: list[dict[str, Any]]
    deferred: frozenset[str]
    in_full: frozenset[str]
    # Each short name of another server's deferred tool, and the full names that have it.
    short_names: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    loaded: list[str] = field(default_factory=list)
    loads: int = 0
    fault: str | None = None
    # Set when a request is built: the deferred tools not loaded then, and how many were.
    unloaded: frozenset[str] = frozenset()
    loaded_at_request: int = 0

    @property
    def broken(self) -> bool:
        return self.fault is not None

    def request_tools(self) -> list[dict[str, Any]]:
        """The tools of the request about to be built: the window's array, or after a fault the same tools in full.
        Notes which deferred tools were not loaded then: a call the reply makes to one was written without its
        definition."""
        self.unloaded = frozenset() if self.broken else self.deferred - frozenset(self.loaded)
        self.loaded_at_request = len(self.loaded)
        return self.fallback if self.broken else self.tools

    def request_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The messages of the request about to be built: the turn's own, or after a fault a copy with words in place
        of every reference (every tool is in full then, so nothing is lost, and no reference is left for the API to
        refuse). ``messages`` itself is never changed."""
        return references_outside(messages, None) if self.broken else messages

    def refused(self, status: int | None) -> bool:
        """Whether a request the API refused with ``status`` is sent once more with every tool in full: a 400, the
        first of the turn. The window is then broken for the rest of the turn."""
        if status != 400 or self.broken:
            return False
        self.fault = FALLBACK_REFUSED
        return True

    def fail(self) -> None:
        """The window's own code failed: every later request of the turn sends every tool in full."""
        if not self.broken:
            self.fault = FALLBACK_FAULT

    def answer(self, name: str, tool_input: Any, resolve: Callable[[str], str] = _same) -> LoaderAnswer | None:
        """The answer to a call of ``name`` (an old name already resolved): a call of the loader, or of a deferred tool
        that was not loaded when the request was built, which never runs. None for any other call, which goes on as
        today. ``resolve`` gives the tool an old name became."""
        if name == LOADER_NAME:
            found = self._load(tool_input, resolve)
        elif name in self.unloaded:
            found = self._not_loaded(name)
        else:
            return None
        return self._counted(found)

    def unreadable(self) -> LoaderAnswer:
        """The answer to a loader call whose input could not be read (malformed JSON): text with ``is_error`` that says
        how to call it. Counted like any other loader call."""
        answer = LoaderAnswer(
            content=LOADER_UNREADABLE, is_error=True, outcome=OUTCOME_NOTHING_LOADED, text=LOADER_UNREADABLE
        )
        return self._counted(answer)

    def fault_answer(self, name: str) -> LoaderAnswer | None:
        """``answer`` once the window's own code failed on this call (the window is broken): plain words."""
        if name == LOADER_NAME:
            return self._counted(_nothing(LOADER_ALL_ATTACHED))
        if name in self.deferred:
            text = f"{name} did not run. Every tool is attached in full from your next step: call {name} again."
            return self._counted(LoaderAnswer(content=text, is_error=True, outcome=OUTCOME_NOT_LOADED, text=text))
        return None

    def record(self, tools: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """The record of a request of this turn that sends ``tools``."""
        applied = any(_is_deferred(tool) for tool in tools)
        loaded = self.loaded[: self.loaded_at_request]
        return on_record(self.kind, tools, loaded=loaded, fallback=None if applied else self.fault)

    def _counted(self, found: LoaderAnswer) -> LoaderAnswer:
        self.loads += 1
        return dataclasses.replace(found, counted=self.loads > FREE_LOADS_PER_TURN)

    def _not_loaded(self, name: str) -> LoaderAnswer:
        if name in self.loaded:
            text = (
                f"{name} was loaded in this same step, after this call was written, so it did not run. Call it again."
            )
        else:
            text = (
                f"{name} is not loaded yet, so this call did not run. "
                f'Call {LOADER_NAME} with ["{name}"], then call {name} again.'
            )
        return LoaderAnswer(content=text, is_error=True, outcome=OUTCOME_NOT_LOADED, text=text)

    def _load(self, tool_input: Any, resolve: Callable[[str], str]) -> LoaderAnswer:
        names = tool_input.get("names") if isinstance(tool_input, Mapping) else None
        if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
            return _nothing(LOADER_BAD_INPUT)
        if self.broken:
            return _nothing(LOADER_ALL_ATTACHED)
        found: list[str] = []
        present: list[str] = []
        missing: list[str] = []
        ambiguous: list[str] = []
        for asked in dict.fromkeys(names[:LOADER_MAX_NAMES]):
            name = resolve(asked)
            if name not in self.deferred and name not in self.in_full:
                # Another server's tool asked by its short name: the one deferred tool that has it. Every name a
                # match can give is one this turn may use.
                matches = self.short_names.get(name, ())
                if len(matches) > 1:
                    full = " or ".join(matches)
                    ambiguous.append(f"{_echo([asked])} names {len(matches)} tools: load one by its full name, {full}.")
                    continue
                name = matches[0] if matches else name
            if name in self.deferred and name not in self.loaded and name not in found:
                found.append(name)
            elif name in self.in_full or name in self.loaded or name in found:
                present.append(asked)
            else:  # unknown, or not one this turn may use: the same words
                missing.append(asked)
        notes = []
        if present:
            notes.append(f"Already in your tool list: {_echo(present)}.")
        if missing:
            notes.append(f"Not available: {_echo(missing)}.")
        notes += ambiguous
        if len(names) > LOADER_MAX_NAMES:
            notes.append(f"Only the first {LOADER_MAX_NAMES} names of a call are read.")
        if not found:
            return _nothing(" ".join(["Nothing was loaded.", *notes]))
        self.loaded.extend(found)
        return LoaderAnswer(
            content=[{"type": "tool_reference", "tool_name": name} for name in found],
            is_error=False,
            outcome=OUTCOME_LOADED,
            text=f"Loaded: {', '.join(found)}.",
            note=f"{LOADER_NAME}: {' '.join(notes)}" if notes else None,
        )


def open_window(config: ToolLoadingConfig, kind: str, tools: Sequence[Mapping[str, Any]]) -> ToolWindow:
    """The window of a turn of ``kind`` that may use ``tools`` (every tool it is offered, in order): each tool outside
    ``config.kept()`` deferred, the loader last with the list of the deferred ones. The caller checks ``why_not``
    first. ``tools`` and its dicts are never changed."""
    kept = config.kept()
    deferred = [tool for tool in tools if tool.get("name") not in kept]
    loader = loader_tool(deferred)
    short_names: dict[str, list[str]] = {}
    for tool in deferred:
        of = _server_of(str(tool.get("name", "")))
        if of is not None:
            short_names.setdefault(of[1], []).append(str(tool.get("name")))
    return ToolWindow(
        kind=kind,
        tools=[*({**tool, "defer_loading": True} if tool.get("name") not in kept else tool for tool in tools), loader],
        fallback=[*tools, loader],
        deferred=frozenset(str(tool.get("name")) for tool in deferred),
        in_full=frozenset(str(tool.get("name")) for tool in tools if tool.get("name") in kept) | {LOADER_NAME},
        short_names={short: tuple(sorted(names)) for short, names in short_names.items()},
    )
