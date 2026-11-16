"""The dashboard's writes go through ``apiSend`` or ``apiRequest`` (both send ``Content-Type: application/json`` on
every method but GET, pinned by ``dashboard-app/src/lib/api.test.ts``), and each names a route whose declared rule
in ``test_route_policy.ROUTE_RULE`` is the owner's or the proxy's secret. No database: the sources are read.

A write is read where it calls the helper, also inside a wrapper of its own (the Goals screen's ``createGoal`` and
``goalAction`` call ``apiRequest`` inside ``timed``). A path segment the source fills from a variable (``{}``) may be
any segment, so every route of that shape and method must apply a check. Reads (GET) are not checked here.

The helpers must be seen to be read: ``lib/api.ts`` exports exactly the three helpers below, they are imported by
their own names (no alias, no namespace import, no re-export), a write helper is only ever called (never taken as a
value), and no source calls ``fetch``, ``navigator.sendBeacon`` or ``XMLHttpRequest`` itself. The web companion
(``src/companion``) is scanned too: its one ``fetch`` is its transport's default ``fetchImpl``, and its writes go
through that transport to a route whose rule is a check (its two card routes check per verb and per function)."""

from __future__ import annotations

import re
from pathlib import Path

from test_route_policy import OWNER, PER_FUNCTION, PER_VERB, PROXY_SECRET, ROUTE_RULE

SRC = Path(__file__).resolve().parents[1] / "dashboard-app" / "src"
API = SRC / "lib" / "api.ts"
TRANSPORT = SRC / "companion" / "transport.ts"
CHECKED = {OWNER, PROXY_SECRET}
COMPANION_CHECKED = CHECKED | {PER_VERB, PER_FUNCTION}
HELPERS = {"apiGet", "apiSend", "apiRequest"}  # every function lib/api.ts exports
WRITE_HELPERS = {"apiSend", "apiRequest"}

# The writes known today, by (method, path with each parameter as {}): the parser must find at least these.
KNOWN_WRITES = {
    ("PUT", "/identity/{}"),
    ("PUT", "/facts/{}"),
    ("POST", "/facts/{}/core"),
    ("DELETE", "/facts/{}"),
    ("PUT", "/censors/{}"),
    ("POST", "/dashboard/memory-write-health/acknowledge"),
    ("POST", "/integrations/mcp/probe"),
    ("POST", "/integrations/mcp/servers"),
    ("PATCH", "/integrations/mcp/servers/{}"),
    ("POST", "/integrations/mcp/servers/{}/refresh"),
    ("DELETE", "/integrations/mcp/servers/{}"),
    ("POST", "/integrations/mcp/oauth/start"),
    ("POST", "/integrations/mcp/oauth/complete"),
    ("POST", "/integrations/mcp/servers/{}/sign-out"),
    ("POST", "/goals"),
    ("POST", "/goals/{}/{}"),
}
KNOWN_COMPANION_WRITES = {("POST", "/a2ui/action"), ("POST", "/a2ui/call")}

_CALL = re.compile(r"\b(apiSend|apiRequest)\s*(?:<[^()]*?>)?\s*\(")
_TRANSPORT_CALL = re.compile(r"\bthis\.fetchImpl\s*\(")
_CONST = re.compile(r"\bconst\s+([A-Za-z_]\w*)\s*=\s*'([^']*)'\s*;")
_TEMPLATE_FN = re.compile(r"\bconst\s+([A-Za-z_]\w*)\s*=\s*\([^)]*\)\s*(?::\s*\w+\s*)?=>\s*(`[^`]*`)\s*;")
_PARAM = re.compile(r"\{[^}]*\}")
_BANNED = re.compile(r"\bfetch\s*\(|\bsendBeacon\b|\bXMLHttpRequest\b")
_IMPORT = re.compile(r"\b(import|export)\s+([^;'\"]*?)\s*\bfrom\s*['\"]([^'\"]+)['\"]", re.S)
_DYNAMIC_IMPORT = re.compile(r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]")
_EXPORT = re.compile(
    r"\bexport\s+(?:default\s+)?(?:async\s+)?(?:function\s*\*?|const|let|var|class)\s+([A-Za-z_$][\w$]*)"
)
_EXPORT_LIST = re.compile(r"\bexport\s*\{([^}]*)\}")


def _sources() -> list[Path]:
    """The dashboard's own sources, the companion's included: not the tests, not ``lib/api.ts`` (the helpers)."""
    found = []
    for path in sorted([*SRC.rglob("*.ts"), *SRC.rglob("*.svelte")]):
        relative = path.relative_to(SRC).as_posix()
        if relative.endswith(".test.ts") or path == API:
            continue
        found.append(path)
    return found


def _without_comments(text: str) -> str:
    """The source with its block, HTML and line comments removed (not the ``//`` of an address); line numbers kept."""
    text = re.sub(r"/\*.*?\*/|<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return re.sub(r"(^|[^:])//.*$", r"\1", text, flags=re.M)


def _arguments(text: str, start: int) -> list[str]:
    """The top-level arguments of the call whose ``(`` is at ``start - 1``, as source text."""
    args: list[str] = []
    depth = 0
    current = []
    i = start
    stack: list[str] = []  # unclosed template literals and their ${ } nesting
    while i < len(text):
        char = text[i]
        if stack and stack[-1] == "`":
            if char == "\\":
                current.append(text[i : i + 2])
                i += 2
                continue
            if char == "`":
                stack.pop()
            elif text.startswith("${", i):
                stack.append("${")
                current.append("${")
                i += 2
                continue
            current.append(char)
            i += 1
            continue
        if char in "'\"":
            end = i + 1
            while text[end] != char:
                end += 2 if text[end] == "\\" else 1
            current.append(text[i : end + 1])
            i = end + 1
            continue
        if char == "`":
            stack.append("`")
        elif char == "}" and stack and stack[-1] == "${":
            stack.pop()
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                args.append("".join(current).strip())
                return [arg for arg in args if arg]
            depth -= 1
        elif char == "," and depth == 0 and not stack:
            args.append("".join(current).strip())
            current = []
            i += 1
            continue
        current.append(char)
        i += 1
    raise AssertionError("unterminated call")


def _literal(argument: str) -> str | None:
    """A string or template literal's text, else None."""
    if len(argument) >= 2 and argument[0] == argument[-1] and argument[0] in "'\"`":
        return argument[1:-1]
    return None


def _resolve(argument: str, constants: dict[str, str], templates: dict[str, str]) -> str:
    """The path an argument names, with each parameter as ``{}``: a literal, one of the file's constants, a template
    literal whose ``${...}`` are the file's constants or template functions, or a call of such a function. A path
    with a ``.`` or ``..`` segment (also percent-encoded) is refused: the route it reaches is not the one it names."""
    if argument in constants:
        text = constants[argument]
    else:
        call = re.fullmatch(r"([A-Za-z_]\w*)\(.*\)", argument, re.S)
        if call and call.group(1) in templates:
            argument = templates[call.group(1)]
        text = _literal(argument)
        assert text is not None, f"a write whose path the test cannot read: {argument!r}; write it as a literal"

        def substitute(match: re.Match[str]) -> str:
            inner = match.group(1).strip()
            if inner in constants:
                return constants[inner]
            inner_call = re.fullmatch(r"([A-Za-z_]\w*)\(.*\)", inner, re.S)
            if inner_call and inner_call.group(1) in templates:
                return _resolve(templates[inner_call.group(1)], constants, templates)
            return "{}"

        previous = None
        while previous != text:
            previous, text = text, re.sub(r"\$\{([^{}]*(?:\([^()]*\))?[^{}]*)\}", substitute, text)
    dots = [segment for segment in text.split("?")[0].split("/") if segment.lower().replace("%2e", ".") in {".", ".."}]
    assert not dots, f"a write path with a dot segment: {text!r}"
    return text


def _where(path: Path, text: str, at: int) -> str:
    return f"{path.relative_to(SRC).as_posix()}:{text.count(chr(10), 0, at) + 1}"


def _helper_writes(path: Path, text: str) -> list[tuple[str, str, str]]:
    """The apiSend and apiRequest writes of one source, as (method, path with {} parameters, where)."""
    constants = dict(_CONST.findall(text))
    templates = dict(_TEMPLATE_FN.findall(text))
    writes = []
    for match in _CALL.finditer(text):
        args = _arguments(text, match.end())
        where = _where(path, text, match.start())
        if match.group(1) == "apiSend":
            method = _literal(args[2]) if len(args) > 2 else "PUT"
        else:
            method = _literal(args[1]) if len(args) > 1 else "GET"
        assert method is not None, f"{where}: a method the test cannot read; write it as a literal"
        if method.upper() != "GET":
            writes.append((method.upper(), _resolve(args[0], constants, templates), where))
    return writes


def _transport_writes(path: Path, text: str) -> list[tuple[str, str, str]]:
    """The companion transport's writes: ``this.fetchImpl(path, {method: ...})`` calls with a method other than GET."""
    writes = []
    for match in _TRANSPORT_CALL.finditer(text):
        args = _arguments(text, match.end())
        where = _where(path, text, match.start())
        method = "GET"
        if len(args) > 1:
            found = re.search(r"\bmethod\s*:\s*([^,}\s]+)", args[1])
            if found:
                method = _literal(found.group(1)) or ""
                assert method, f"{where}: a method the test cannot read; write it as a literal"
        if method.upper() != "GET":
            writes.append((method.upper(), _resolve(args[0], {}, {}), where))
    return writes


def _sources_text() -> list[tuple[Path, str]]:
    return [(path, _without_comments(path.read_text(encoding="utf-8"))) for path in _sources()]


def _writes() -> list[tuple[str, str, str]]:
    """Every dashboard write outside the companion, as (method, path with {} parameters, where)."""
    return [write for path, text in _sources_text() if not _is_companion(path) for write in _helper_writes(path, text)]


def _is_companion(path: Path) -> bool:
    return path.relative_to(SRC).as_posix().startswith("companion/")


def _imports_api(path: Path, spec: str) -> bool:
    """Whether a module specifier in ``path`` names ``lib/api``."""
    if not spec.startswith("."):
        return False
    target = (path.parent / spec).resolve()
    return target.with_suffix("") == API.with_suffix("") or target == API.with_suffix("")


def _helper_problems(path: Path, text: str) -> list[str]:
    """How a source reaches the helpers other than by importing them by name and calling them."""
    problems = []
    spans = []
    for match in _IMPORT.finditer(text):
        spans.append(match.span())
        kind, clause, spec = match.groups()
        if not _imports_api(path, spec):
            continue
        where = _where(path, text, match.start())
        clause = clause.strip()
        if kind == "export":
            problems.append(f"{where}: re-exports lib/api")
        elif clause.startswith("type "):
            continue
        elif not (clause.startswith("{") and clause.endswith("}")):
            problems.append(f"{where}: imports lib/api other than by name ({clause!r})")
        elif re.search(r"\bas\b", clause):
            problems.append(f"{where}: imports a lib/api name under an alias ({clause!r})")
    for match in _DYNAMIC_IMPORT.finditer(text):
        if _imports_api(path, match.group(1)):
            problems.append(f"{_where(path, text, match.start())}: imports lib/api at run time")
    for match in re.finditer(r"\b(apiSend|apiRequest)\b", text):
        if any(start <= match.start() < end for start, end in spans):
            continue
        if not re.match(r"\s*(?:<[^()]*?>)?\s*\(", text[match.end() :]):
            problems.append(f"{_where(path, text, match.start())}: takes {match.group(1)} as a value; call it")
    return problems


def _banned_calls(path: Path, text: str) -> list[str]:
    """The fetch, sendBeacon and XMLHttpRequest uses of a source (the companion transport's default fetchImpl
    aside)."""
    allowed = None
    if path == TRANSPORT:
        defaults = list(re.finditer(r"\(\.\.\.args\)\s*=>\s*(fetch)\(\.\.\.args\)", text))
        assert len(defaults) == 1, "the companion transport's default fetchImpl moved: update this test"
        allowed = defaults[0].start(1)
    return [
        f"{_where(path, text, m.start())}: {m.group(0).strip()}" for m in _BANNED.finditer(text) if m.start() != allowed
    ]


def _api_exports(text: str) -> set[str]:
    names = set(_EXPORT.findall(text))
    for listed in _EXPORT_LIST.findall(text):
        names |= {item.split(" as ")[-1].strip() for item in listed.split(",") if item.strip()}
    return names


def _segments_match(write: str, route: str) -> bool:
    """Whether a write's path can reach a route's path: a ``{}`` of the write matches any one segment, a ``{param}``
    of the route any one segment of the write."""
    ours, theirs = write.split("/"), route.split("/")
    return len(ours) == len(theirs) and all(
        a == "{}" or _PARAM.fullmatch(b) or a == b for a, b in zip(ours, theirs, strict=True)
    )


def _rules_reached(method: str, path: str) -> dict[str, str]:
    """The rule of every route a write can reach, by its path. Every non-GET route has a rule (test_route_policy)."""
    return {
        route: rule
        for (route_method, route), rule in ROUTE_RULE.items()
        if route_method == method and _segments_match(path, route)
    }


def _unchecked(writes: list[tuple[str, str, str]], allowed: set[str]) -> list[str]:
    unchecked = []
    for method, path, where in writes:
        reached = _rules_reached(method, path)
        if not reached:
            unchecked.append(f"{where}: {method} {path} (no such route)")
        unchecked += [f"{where}: {method} {route} ({rule})" for route, rule in reached.items() if rule not in allowed]
    return unchecked


def test_lib_api_exports_exactly_the_helpers_this_test_reads() -> None:
    assert _api_exports(API.read_text(encoding="utf-8")) == HELPERS, (
        "lib/api.ts exports another function: a new request helper must be read by this test"
    )


def test_every_dashboard_write_goes_through_apisend_or_apirequest() -> None:
    problems = []
    for path, text in _sources_text():
        problems += _banned_calls(path, text) + _helper_problems(path, text)
    assert problems == [], "call apiSend or apiRequest (lib/api.ts), imported by name"
    assert re.findall(r"\bfetch\s*\(", API.read_text(encoding="utf-8"))  # the helpers themselves


def test_every_dashboard_write_names_a_route_that_applies_a_check() -> None:
    writes = _writes()
    found = {(method, path) for method, path, _where in writes}
    assert KNOWN_WRITES <= found, f"the parser missed: {sorted(KNOWN_WRITES - found)}"
    assert _unchecked(writes, CHECKED) == []
    # The goal action's path ends in a variable: it reaches every goal action route, and nothing else.
    assert set(_rules_reached("POST", "/goals/{}/{}")) == {
        route for method, route in ROUTE_RULE if method == "POST" and route.startswith("/goals/{ref}/")
    }


def test_every_companion_write_names_a_route_that_applies_a_check() -> None:
    writes = [
        write
        for path, text in _sources_text()
        if _is_companion(path)
        for write in [*_helper_writes(path, text), *_transport_writes(path, text)]
    ]
    assert {(method, path) for method, path, _where in writes} >= KNOWN_COMPANION_WRITES
    assert _unchecked(writes, COMPANION_CHECKED) == []


def test_the_parser_reads_a_multi_line_call_a_default_method_and_a_path_helper() -> None:
    text = (
        "const BASE = '/integrations/mcp';\n"
        "export const serverPath = (name: string) => `${BASE}/servers/${encodeURIComponent(name)}`;\n"
        "apiSend<Thing>(\n  `/facts/${row.id}`,\n  { a: [1, 2], b: 'x, y' },\n  'DELETE',\n);\n"
        "apiSend(`/identity/${name}`, { content });\n"
        "apiRequest<{ ok: boolean }>(`${serverPath(name)}/refresh`, 'POST');\n"
        "const make = (body: B) => timed((signal) => apiRequest<Made>(BASE, 'POST', body, signal));\n"
    )
    constants = dict(_CONST.findall(text))
    templates = dict(_TEMPLATE_FN.findall(text))
    calls = [(m.group(1), _arguments(text, m.end())) for m in _CALL.finditer(text)]
    assert [len(args) for _helper, args in calls] == [3, 2, 2, 4]
    assert _resolve(calls[0][1][0], constants, templates) == "/facts/{}"
    assert _literal(calls[0][1][2]) == "DELETE"
    assert _resolve(calls[1][1][0], constants, templates) == "/identity/{}"
    assert _resolve(calls[2][1][0], constants, templates) == "/integrations/mcp/servers/{}/refresh"
    assert _resolve(calls[3][1][0], constants, templates) == "/integrations/mcp"
    assert _segments_match("/goals/{}/{}", "/goals/{ref}/pause")
    assert not _segments_match("/goals/{}/{}", "/goals/{ref}")
    assert not _segments_match("/goals/{}/pause", "/goals/{ref}/stop")


def test_the_scan_refuses_each_way_around_the_helpers() -> None:
    """Each evasion, in a source of its own: the scan names it."""
    view = SRC / "views" / "Evasion.svelte"
    cases = {
        "import { apiSend as send } from '../lib/api';\nsend('/a2ui/push/test', {}, 'POST');": "under an alias",
        "import * as api from '../lib/api';\napi.apiSend('/a2ui/push/test', {}, 'POST');": "other than by name",
        "import api from '../lib/api';": "other than by name",
        "export { apiSend } from '../lib/api';": "re-exports",
        "const m = await import('../lib/api');": "at run time",
        "import { apiSend } from '../lib/api';\nconst send = apiSend;": "as a value",
        "import { apiRequest } from '../lib/api';\nrun(apiRequest);": "as a value",
        "navigator.sendBeacon('/sleep/trigger', '{}');": "sendBeacon",
        "const x = new XMLHttpRequest();": "XMLHttpRequest",
        "window.fetch('/sleep/trigger', { method: 'POST' });": "fetch(",
    }
    for source, named in cases.items():
        text = _without_comments(source)
        problems = _banned_calls(view, text) + _helper_problems(view, text)
        assert any(named in problem for problem in problems), (source, problems)
    fine = "import { apiGet, apiSend, type ApiError } from '../lib/api';\nimport type { X } from '../lib/types/api';\n"
    fine += "// fetch( in a comment\napiSend('/facts/1', undefined, 'DELETE');\n"
    assert _helper_problems(view, _without_comments(fine)) == []
    assert _banned_calls(view, _without_comments(fine)) == []
    for path in ("`/goals/${ref}/../stop`", "'/facts/./1'", "'/facts/%2E%2E/1'"):
        try:
            _resolve(path, {}, {})
        except AssertionError as exc:
            assert "dot segment" in str(exc)
        else:
            raise AssertionError(f"a dot segment passed: {path}")
    extra = "export async function apiGet() {}\nexport const apiPost = () => 1;\nexport interface ApiOpts {}\n"
    assert _api_exports(extra) == {"apiGet", "apiPost"}
