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
    compensable: bool = False  # P2.8: a compensator can undo a successful call


_READ = ToolClass("none")
_WRITE = ToolClass("write")
_WRITE_C = ToolClass("write", compensable=True)
_SPAWN = ToolClass("write", spawns=True)
_SPAWN_C = ToolClass("write", spawns=True, compensable=True)
_EXTERNAL = ToolClass("external")

TOOL_CLASSES: Mapping[str, ToolClass] = MappingProxyType({
    # reads
    "recall_deep": _READ, "recall_recent": _READ, "read_file": _READ, "get_procedure": _READ,
    "web_search": _READ, "web_fetch": _READ, "list_tasks": _READ, "cache_retrieve": _READ,
    "recall_hubs": _READ, "list_decisions": _READ,
    "submit_final_report": _READ,  # injected via extra_tools in hardened subtasks
    # local writes — compensable where a compensator exists
    "write_file": _WRITE_C, "learn_fact": _WRITE, "record_decision": _WRITE, "create_censor": _WRITE,
    "store_identity": _WRITE, "learn_skill": _WRITE, "complete_initiation": _WRITE,
    "cancel_task": _WRITE, "heartbeat_check_manage": _WRITE_C, "ingest_document": _WRITE,
    "resolve_decision": _WRITE_C, "resolve_decisions": _WRITE, "push_surface": _WRITE,
    "compose_surface": _WRITE, "dag_manage": _WRITE,
    "run_python": _WRITE,  # the floor; code that reaches the network is external
    "bash": _WRITE,        # the floor; classify_side_effect reads the command itself
    # start other agent work — schedule_task + heartbeat_check_create are compensable
    "spawn_task": _SPAWN, "spawn_sync": _SPAWN, "schedule_task": _SPAWN_C,
    "heartbeat_check_create": _SPAWN_C, "dag_create": _SPAWN,
    # leave the host — NOT compensable (cannot unsend)
    "send_file": _EXTERNAL, "send_email": _EXTERNAL,
})

# Python that opens a connection to another host: a network module imported
# or used (`requests.get`, `socket.socket` -- not the English word in a
# string), a connection helper, a URL literal, or a network program run
# through a shell. Errs toward external by design: a URL in a comment counts,
# even when nothing is sent.
_NETWORK_MODULES = (
    r"(?:smtplib|requests|httpx|aiohttp|urllib3?|http\.client|socket|ssl|ftplib|paramiko|telnetlib"
    r"|imaplib|poplib|nntplib|websockets?|pycurl|xmlrpc\.client)")
_NETWORK_CODE = re.compile(
    r"^\s*(?:import|from)\s+" + _NETWORK_MODULES + r"\b"        # import smtplib / from urllib.request import
    r"|^\s*from\s+http\s+import\b"                               # from http import client
    r"|\b" + _NETWORK_MODULES + r"\."                            # requests.get(...), socket.socket()
    r"|\b(?:open_connection|create_connection|create_server)\("  # asyncio / socket helpers
    r"|https?://|\bapi\.telegram\.org\b"
    r"|\b(?:curl|wget|ssh|scp|sftp|rsync|telnet)\b",             # a network program run by the script
    re.MULTILINE)


def tool_class(name: str) -> ToolClass | None:
    """The declared class, or None for a tool nobody classified."""
    return TOOL_CLASSES.get(name)


def code_reaches_network(code: str) -> bool:
    """True when Python source can open a connection to another host."""
    return bool(_NETWORK_CODE.search(code))


def refuse_denylist() -> frozenset[str]:
    """Tools an F078 refuse-tier censor strips: everything that is not a read."""
    return frozenset(name for name, cls in TOOL_CLASSES.items() if cls.side_effect != "none")
