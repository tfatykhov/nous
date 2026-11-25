"""F120 tool loading, On: every tool a prompt tells the model to call by name is kept in full.

In mode On a tool outside the kept set reaches the model as a name in the loader's list only, so a prompt that names a
tool the model should call must name a pinned one (``tool_loading.PINNED``). This scan is wider than the backtick scan
of ``test_tool_loading.py``: a tool name with an underscore matches with or without backticks, a bare name (``bash``,
``commit``) only after a verb that calls it, in every string Nous builds (not a docstring, not the value of a
``"description"`` key: a tool's own description naming another tool is covered by the loader's list), in the builtin
skills and in the seed and init SQL. A hit must be pinned, initiation-only (that frame is never deferred) or in
``EXEMPT`` with the reason it never reaches a deferred turn; an exemption that is no longer a hit fails.

Not covered: a procedure or skill written to the database at run time can name any tool. The loader's list names every
deferred tool, so the model can load it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from nous.api.tool_classes import TOOL_CLASSES
from nous.api.tool_loading import PINNED
from nous.api.tools import _INITIATION_ONLY_TOOLS

ROOT = Path(__file__).resolve().parents[1]
MIN_CHARS = 40
VERBS = r"(?:call|use|run|invoke|using|calling)(?:\s+the)?"
# Calls whose string arguments never reach the model: logging, SQL text, a regular expression.
_NOT_READ_CALLS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "text", "compile"})

_INTERNAL = "goal sessions and chain decisions are of internal authority, and such a turn never defers"
_OWN = "the tool's own result or refusal: the model has called it already"
# A (tool, file) the scan finds that is not pinned, and why it needs no pin there.
EXEMPT: dict[tuple[str, str], str] = {
    ("heartbeat_check_create", "nous/heartbeat/dynamic.py"): "a dynamic check names its own tools (a tool_filter)",
    ("heartbeat_check_manage", "nous/heartbeat/dynamic.py"): "a dynamic check names its own tools (a tool_filter)",
    ("spawn_task", "nous/goals/default_prompt.py"): _INTERNAL,
    ("dag_create", "nous/goals/default_prompt.py"): _INTERNAL,
    ("spawn_task", "nous/handlers/continuation_runner.py"): _INTERNAL,
    ("dag_create", "nous/handlers/continuation_runner.py"): _INTERNAL,
    ("send_email", "nous/api/tool_policy.py"): "a goal grant's rule: " + _INTERNAL,
    ("send_file", "nous/api/tool_policy.py"): "a goal grant's rule: " + _INTERNAL,
    ("send_email", "nous/goals/grants.py"): "a goal grant's warning: " + _INTERNAL,
    ("send_file", "nous/goals/grants.py"): "a goal grant's warning: " + _INTERNAL,
    ("browser_task", "nous/browser/loop.py"): _OWN,
    ("cancel_task", "nous/api/tools.py"): _OWN,
    ("ingest_document", "nous/api/tools.py"): _OWN,
    ("db_query", "nous/api/db_query.py"): _OWN,
    ("send_email", "nous/api/email_tools.py"): _OWN,
    ("resolve_decisions", "nous/tool_names.py"): "the note on a call made by the old name: " + _OWN,
    ("send_file", "nous/cognitive/claim_verifier.py"): "a word of a claim pattern, not an instruction",
    ("push_surface", "nous/dag/approval.py"): "a refusal of a rare step (asking a declined approval again)",
}


def _registered_names() -> set[str]:
    """The literal first argument of every ``dispatcher.register("name", ...)`` in nous/."""
    names: set[str] = set()
    for path in (ROOT / "nous").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register"
                and ast.unparse(node.func.value).endswith("dispatcher")
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.add(node.args[0].value)
    return names


def _tool_names() -> set[str]:
    return (set(TOOL_CLASSES) | _registered_names() | set(PINNED)) - set(_INITIATION_ONLY_TOOLS)


def _python_strings(path: Path) -> list[tuple[str, int]]:
    """Every string constant of ``path`` longer than ``MIN_CHARS`` that the model can read, with its line. Left out:
    a docstring, the value of a ``"description"`` key or keyword (a tool's own description, a setting's help), and
    the arguments of a log call, of ``text()`` (SQL), of ``re.compile`` and of a ``raise``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skipped: set[int] = set()

    def skip(node: ast.AST | None) -> None:
        if node is not None:
            skipped.update(id(n) for n in ast.walk(node))

    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skipped.add(id(body[0].value))
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "description":
                    skip(value)
        if isinstance(node, ast.Raise):
            skip(node.exc)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in _NOT_READ_CALLS:
                for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                    skip(arg)
            for kw in node.keywords:
                if kw.arg == "description":
                    skip(kw.value)
    return [
        (node.value, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and len(node.value) > MIN_CHARS
        and id(node) not in skipped
    ]


def _sources() -> list[tuple[str, str]]:
    """(where, text) of every source the scan reads."""
    found: list[tuple[str, str]] = []
    for path in sorted((ROOT / "nous").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        found += [(f"{rel}:{line}", value) for value, line in _python_strings(path)]
    for path in sorted((ROOT / "nous" / "skills" / "builtin").rglob("*.md")):
        found.append((path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")))
    for rel in ("sql/seed.sql", "sql/init.sql"):
        found.append((rel, (ROOT / rel).read_text(encoding="utf-8")))
    return found


def _pattern(name: str) -> re.Pattern[str]:
    if "_" in name:
        return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
    return re.compile(rf"\b{VERBS}\s+`?{re.escape(name)}`?(?![A-Za-z0-9_])", re.IGNORECASE)


def _hits() -> dict[tuple[str, str], str]:
    """Each (tool name, file) where a source names the tool, with the first line it is named on."""
    patterns = {name: _pattern(name) for name in _tool_names()}
    hits: dict[tuple[str, str], str] = {}
    for where, text in _sources():
        path = where.split(":", 1)[0]
        for name, pattern in patterns.items():
            if (name, path) not in hits and pattern.search(text):
                hits[(name, path)] = where
    return hits


def test_every_tool_a_prompt_names_is_kept_in_full():
    hits = _hits()
    named = {name for name, _path in hits}
    assert {"record_decision", "recall_deep", "get_procedure"} <= named  # the scan reaches the prompts
    missing = sorted(where for (name, path), where in hits.items() if name not in PINNED and (name, path) not in EXEMPT)
    assert not missing, f"a prompt names a tool that is not pinned: {missing}"


def test_every_exemption_is_still_named_where_it_says():
    hits = _hits()
    stale = sorted(pair for pair in EXEMPT if pair not in hits)
    assert not stale, f"an exemption the scan no longer finds there: {stale}"
    assert not {name for name, _path in EXEMPT} & set(PINNED)


def test_the_pins_the_wider_scan_added():
    """The tools this scan found that the backtick scan did not, kept in full in every kind of call: the companion
    tap's prompt (compose_surface), the identity prompt (create_censor), the marker of a compressed result
    (cache_retrieve) and the result inbox's line on older results (list_tasks, dag_manage)."""
    assert {"compose_surface", "create_censor", "cache_retrieve", "list_tasks", "dag_manage"} <= set(PINNED)


def test_the_scan_matches_names_with_and_without_backticks_and_bare_names_after_a_verb():
    assert _pattern("compose_surface").search("then call compose_surface with the layout")
    assert _pattern("compose_surface").search("call `compose_surface`.")
    assert not _pattern("compose_surface").search("recompose_surfaces")
    assert _pattern("bash").search("Use the bash tool to list files")
    assert _pattern("bash").search("run `bash` here")
    assert not _pattern("bash").search("a bash script the user wrote")
