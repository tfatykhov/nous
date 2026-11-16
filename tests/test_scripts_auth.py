"""The diagnostic scripts and the benchmark adapters send the owner's secret (NOUS_OWNER_ROUTE_SECRET, when set) on
every request to chat and to the sleep trigger. No database: the helper is called and the sources are read."""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
SECRET = "q" * 64

# Every file under scripts/ and benchmarks/ that calls chat or the sleep trigger.
CHAT_CALLERS = [
    "scripts/diag/followup_probe.py",
    "scripts/diag/faculty/abstract.py",
    "scripts/diag/faculty/baseline_agentic.py",
    "scripts/diag/faculty/baseline_confirm.py",
    "scripts/diag/faculty/baseline_formation.py",
    "scripts/diag/faculty/continual.py",
    "scripts/diag/faculty/experiential.py",
    "scripts/diag/faculty/fc_phase0.py",
    "scripts/diag/faculty/measure_agentic.py",
    "benchmarks/nous_locomo_adapter.py",
    "benchmarks/nous_longmemeval_adapter.py",
]
HELPERS = {"owner_headers", "_owner_headers"}
# A string that names Nous's chat route ("/chat" alone or "/chat/<id>", not another API's ".../chat/completions") or
# the sleep trigger.
_CHAT_PATH = re.compile(r"/chat(/|$)(?!completions)|/sleep/trigger")


def _load(relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_under_test_{Path(relative).stem}", REPO / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("relative", ["scripts/_nous_auth.py"])
def test_owner_headers_is_empty_without_the_variable_and_one_header_with_it(
    monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    helper = _load(relative).owner_headers
    monkeypatch.delenv("NOUS_OWNER_ROUTE_SECRET", raising=False)
    assert helper() == {}
    monkeypatch.setenv("NOUS_OWNER_ROUTE_SECRET", "")
    assert helper() == {}
    monkeypatch.setenv("NOUS_OWNER_ROUTE_SECRET", SECRET)
    assert helper() == {"X-Nous-Owner-Secret": SECRET}


@pytest.mark.parametrize("relative", ["benchmarks/nous_locomo_adapter.py", "benchmarks/nous_longmemeval_adapter.py"])
def test_each_benchmark_adapter_sends_the_secret_when_it_is_set(monkeypatch: pytest.MonkeyPatch, relative: str) -> None:
    # The adapters use requests, which the test environment may lack: a stand-in module is enough here.
    monkeypatch.setitem(sys.modules, "requests", ModuleType("requests"))
    module = _load(relative)
    sent: list[object] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"response": "ok"}

    def fake_post(url: str, **kw: object) -> _Response:
        sent.append((url, kw.get("headers")))
        return _Response()

    monkeypatch.setattr(module.requests, "post", fake_post, raising=False)
    monkeypatch.setenv("NOUS_OWNER_ROUTE_SECRET", SECRET)
    assert module.NousClient("http://nous.test").chat("hi", "s-1") == "ok"
    monkeypatch.delenv("NOUS_OWNER_ROUTE_SECRET")
    module.NousClient("http://nous.test").chat("hi", "s-1")
    assert sent == [("http://nous.test/chat", {"X-Nous-Owner-Secret": SECRET}), ("http://nous.test/chat", {})]


def test_the_follow_up_probe_sends_the_secret_when_it_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load("scripts/diag/followup_probe.py")
    monkeypatch.setattr(module, "BASE", "http://nous.test")
    seen: list[tuple[str, str, str | None]] = []

    class _Answer:
        def read(self) -> bytes:
            return b'{"response": "ok"}'

    def fake_urlopen(request, timeout: float = 0) -> _Answer:
        seen.append((request.get_method(), request.full_url, request.get_header("X-nous-owner-secret")))
        return _Answer()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("NOUS_OWNER_ROUTE_SECRET", SECRET)
    module.chat("hi", "s-1")
    module.end_session("s-1")
    monkeypatch.delenv("NOUS_OWNER_ROUTE_SECRET")
    module.chat("hi", "s-2")
    assert seen == [
        ("POST", "http://nous.test/chat", SECRET),
        ("DELETE", "http://nous.test/chat/s-1", SECRET),
        ("POST", "http://nous.test/chat", None),
    ]


def _sends_owner_headers(node: ast.AST) -> bool:
    """Whether ``node`` has a ``headers=`` keyword whose value calls one of the helpers."""
    for keyword in getattr(node, "keywords", []):
        if keyword.arg == "headers" and any(
            isinstance(inner, ast.Call)
            and (getattr(inner.func, "id", None) in HELPERS or getattr(inner.func, "attr", None) in HELPERS)
            for inner in ast.walk(keyword.value)
        ):
            return True
    return False


def _names_chat(expr: ast.AST, names: set[str]) -> bool:
    """Whether ``expr`` holds a string naming the chat route anywhere (a literal, an f-string, a concatenation), or a
    variable or attribute in ``names``."""
    for node in ast.walk(expr):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _CHAT_PATH.search(node.value):
            return True
        if (isinstance(node, ast.Name) and node.id in names) or (
            isinstance(node, ast.Attribute) and node.attr in names
        ):
            return True
    return False


def _is_url_expression(value: ast.AST) -> bool:
    """A value that builds a URL: a string, an f-string, a concatenation, another variable or attribute, or a
    ``format``/``join``/``urljoin`` call. Not a request's result, which would carry the name to every later value."""
    if isinstance(value, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Name, ast.Attribute)):
        return True
    return isinstance(value, ast.Call) and getattr(value.func, "attr", getattr(value.func, "id", None)) in {
        "format",
        "join",
        "urljoin",
    }


def _chat_names(tree: ast.AST) -> set[str]:
    """The variables and attributes assigned, anywhere in the file, a value that names the chat route (followed
    through further assignments)."""
    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if not _is_url_expression(value) or not _names_chat(value, names):
                continue
            for target in targets:
                for part in ast.walk(target):
                    name = (
                        part.id
                        if isinstance(part, ast.Name)
                        else part.attr
                        if isinstance(part, ast.Attribute)
                        else None
                    )
                    if name is not None and name not in names:
                        names.add(name)
                        changed = True
    return names


# The calls that make an HTTP request, by the name of what is called (httpx, requests, urllib), and any function the
# file defines (a local helper such as followup_probe's _req).
_REQUEST_CALLEES = {"post", "delete", "put", "patch", "get", "request", "stream", "Request", "urlopen"}


def _chat_requests(tree: ast.AST) -> list[ast.Call]:
    """Every call in the file that makes a request, or calls one of the file's functions, with a URL naming the chat
    route: in any positional argument or a ``url=`` keyword."""
    names = _chat_names(tree)
    local = {fn.name for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if callee not in _REQUEST_CALLEES and callee not in local:
            continue
        urls = [*node.args, *(kw.value for kw in node.keywords if kw.arg == "url")]
        if any(_names_chat(url, names) for url in urls):
            found.append(node)
    return found


@pytest.mark.parametrize("relative", CHAT_CALLERS)
def test_every_request_to_chat_in_the_file_sends_the_owners_secret(relative: str) -> None:
    """Every request whose URL names the chat route or the sleep trigger (a literal, a concatenation, a variable or
    attribute that holds one, or a ``url=`` keyword) passes ``headers=`` built by the helper, or goes through a function
    of the file whose every request does."""
    tree = ast.parse((REPO / relative).read_text(encoding="utf-8"))
    sending_helpers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(call, ast.Call) and _sends_owner_headers(call) for call in ast.walk(fn))
    }
    requests = _chat_requests(tree)
    assert requests, "the file no longer calls chat: take it off the list"
    for call in requests:
        callee = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
        assert _sends_owner_headers(call) or callee in sending_helpers, (relative, call.lineno)


# The chat route in a file's plain text: "/chat" followed by a quote, a slash, a space or the end, not "/chats" nor
# another API's "/chat/completions"; or the sleep trigger.
_CHAT_IN_TEXT = re.compile(r"/chat(?!s)(?!/completions)(?=[/\"'` ?)\]]|$)|/sleep/trigger", re.M)


def test_the_list_is_every_file_under_scripts_and_benchmarks_that_names_chat_or_the_sleep_trigger() -> None:
    """A plain-text search, so a file that reaches chat in a way the rule above does not read still has to be listed
    and use the helper."""
    found = set()
    for path in [*REPO.glob("scripts/**/*.py"), *REPO.glob("benchmarks/**/*.py"), *REPO.glob("scripts/**/*.sh")]:
        text = path.read_text(encoding="utf-8")
        if _CHAT_IN_TEXT.search(text):
            found.add(path.relative_to(REPO).as_posix())
            assert "owner_headers()" in text, path
    assert found == set(CHAT_CALLERS)


def test_the_rule_reads_a_url_held_in_a_variable_a_keyword_or_a_concatenation() -> None:
    """The cases the rule must catch, each with no header."""
    cases = [
        ['url = f"{BASE}/chat"', "httpx.post(url, json={})"],
        ['chat_url = BASE + "/chat"', "u = chat_url", "requests.post(u, json={})"],
        ['httpx.post(url=f"{BASE}/chat", json={})'],
        ['httpx.delete(BASE + "/chat/" + sid)'],
        ['self.endpoint = f"{self.base}/chat"', "requests.post(self.endpoint, json={})"],
    ]
    for lines in cases:
        tree = ast.parse("\n".join(lines))
        requests = _chat_requests(tree)
        assert requests, lines
        assert not any(_sends_owner_headers(call) for call in requests), lines
    unrelated = ['print(f"POST {BASE}/chat failed")', 'httpx.get(f"{BASE}/health")']
    assert _chat_requests(ast.parse("\n".join(unrelated))) == []
