"""Harness Phase 2a: a tool's class is declared once, and every registration has one."""

import ast
from pathlib import Path

from nous.api.tool_classes import TOOL_CLASSES, code_reaches_network, refuse_denylist, tool_class
from nous.cognitive import execution_ledger as el
from nous.cognitive.bash_side_effect import classify_bash_command


def _registered_names() -> set[str]:
    """Every literal tool name passed to ``dispatcher.register(...)`` in production code."""
    names: set[str] = set()
    for path in Path("nous").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "register"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "dispatcher"):
                first = node.args[0] if node.args else None
                assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
                    f"{path}:{node.lineno}: register() must take a literal name")
                names.add(first.value)
    return names


def test_every_registered_tool_is_classified():
    missing = _registered_names() - set(TOOL_CLASSES)
    assert not missing, f"registered without a class in nous/api/tool_classes.py: {sorted(missing)}"


def test_the_drift_scan_sees_the_known_registrations():
    assert {"bash", "send_email", "dag_create", "compose_surface", "store_identity"} <= _registered_names()


def test_injected_extra_tools_are_classified():
    assert tool_class("submit_final_report").side_effect == "none"


def test_ledger_sets_are_derived_from_the_table():
    for name, cls in TOOL_CLASSES.items():
        if name in ("bash", "run_python"):
            continue
        expected = {"none": el.READ_TOOLS, "write": el.WRITE_TOOLS,
                    "external": el.EXTERNAL_TOOLS, "irreversible": el.IRREVERSIBLE_TOOLS}[cls.side_effect]
        assert name in expected, name


def test_previously_unclassified_tools_are_writes_now():
    for name in ("ingest_document", "resolve_decision", "resolve_decisions", "spawn_sync",
                 "dag_create", "dag_manage", "push_surface", "compose_surface"):
        assert el.classify_side_effect(name, {}) == "write", name


def test_bash_is_still_classified_per_command():
    assert el.classify_side_effect("bash", {"command": "ls"}) == "none"
    assert el.classify_side_effect("bash", {"command": "curl https://x"}) == "external"


def test_a_script_that_reaches_the_network_is_external():
    assert el.classify_side_effect("run_python", {"code": "import smtplib\nsmtplib.SMTP('h')"}) == "external"
    assert el.classify_side_effect("run_python", {"code": "import requests\nrequests.get(u)"}) == "external"
    assert el.classify_side_effect("run_python", {"code": "print(1 + 1)"}) == "write"
    assert classify_bash_command("python3 -c \"import smtplib; smtplib.SMTP('h')\"") == "external"
    assert classify_bash_command("python3 -c 'print(1)'") == "write"
    assert classify_bash_command("python3.12 -c \"import smtplib\"") == "external"


def test_the_session_ledger_classifies_the_same_way():
    ledger = el.ExecutionLedger(session_id="s")
    assert ledger._classify_side_effect("run_python", {"code": "import httpx"}) == "external"
    assert ledger._classify_side_effect("dag_create", {}) == "write"
    assert ledger._classify_side_effect("bash", {"command": "ls"}) == "none"


def test_network_detector():
    assert code_reaches_network("from urllib.request import urlopen")
    assert code_reaches_network("httpx.post('https://api.telegram.org/bot')")
    assert not code_reaches_network("import json\njson.loads(s)")


def test_spawn_flags():
    assert {n for n, c in TOOL_CLASSES.items() if c.spawns} == {
        "spawn_task", "spawn_sync", "schedule_task", "heartbeat_check_create", "dag_create"}


# --- Task 2: the refuse denylist ------------------------------------------------


def test_refuse_strips_every_non_read_tool():
    denied = refuse_denylist()
    assert {"dag_create", "push_surface", "compose_surface", "ingest_document",
            "resolve_decision", "spawn_sync", "send_email", "bash"} <= denied
    assert not denied & {"recall_deep", "read_file", "submit_final_report"}
