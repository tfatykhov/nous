"""Every tool's risk class, declared once (harness Phase 2a).

A leaf module: no imports from the rest of ``nous``, so the ledger, ActionGate,
the claim verifier and the runner can all read it at import time. Registration
happens later and conditionally in ``main.py``; ``tests/test_tool_classes.py``
fails if any ``dispatcher.register("<name>", ...)`` has no entry here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

SideEffect = Literal["none", "write", "external", "irreversible"]


@dataclass(frozen=True)
class ToolClass:
    side_effect: SideEffect   # a level: none < write < external < irreversible
    spawns: bool = False      # starts other agent work: a subtask, schedule, check or DAG


_READ = ToolClass("none")
_WRITE = ToolClass("write")
_SPAWN = ToolClass("write", spawns=True)
_EXTERNAL = ToolClass("external")

TOOL_CLASSES: Mapping[str, ToolClass] = MappingProxyType({
    # reads
    "recall_deep": _READ, "recall_recent": _READ, "read_file": _READ, "get_procedure": _READ,
    "web_search": _READ, "web_fetch": _READ, "list_tasks": _READ, "cache_retrieve": _READ,
    "recall_hubs": _READ, "list_decisions": _READ,
    "submit_final_report": _READ,  # injected via extra_tools in hardened subtasks
    # local writes
    "write_file": _WRITE, "learn_fact": _WRITE, "record_decision": _WRITE, "create_censor": _WRITE,
    "store_identity": _WRITE, "learn_skill": _WRITE, "complete_initiation": _WRITE,
    "cancel_task": _WRITE, "heartbeat_check_manage": _WRITE, "ingest_document": _WRITE,
    "resolve_decision": _WRITE, "resolve_decisions": _WRITE, "push_surface": _WRITE,
    "compose_surface": _WRITE, "dag_manage": _WRITE,
    "run_python": _WRITE,  # the floor; code that reaches the network is external
    "bash": _WRITE,        # the floor; classify_side_effect reads the command itself
    # start other agent work
    "spawn_task": _SPAWN, "spawn_sync": _SPAWN, "schedule_task": _SPAWN,
    "heartbeat_check_create": _SPAWN, "dag_create": _SPAWN,
    # leave the host
    "send_file": _EXTERNAL, "send_email": _EXTERNAL,
})

# Python that opens a connection to another host. Errs toward external by
# design: any URL literal or a bare `socket` mention counts, even when
# nothing is sent.
_NETWORK_CODE = re.compile(
    r"\b(?:smtplib|requests|httpx|aiohttp|urllib|http\.client|socket|ftplib|paramiko|telnetlib)\b"
    r"|https?://")


def tool_class(name: str) -> ToolClass | None:
    """The declared class, or None for a tool nobody classified."""
    return TOOL_CLASSES.get(name)


def code_reaches_network(code: str) -> bool:
    """True when Python source can open a connection to another host."""
    return bool(_NETWORK_CODE.search(code))


def refuse_denylist() -> frozenset[str]:
    """Tools an F078 refuse-tier censor strips: everything that is not a read."""
    return frozenset(name for name, cls in TOOL_CLASSES.items() if cls.side_effect != "none")
