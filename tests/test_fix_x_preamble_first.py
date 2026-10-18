"""Every hand-built Messages request puts the Claude Code preamble at system block 0.

Under an OAuth token the API answers a request to any model but Haiku 4.5
(Haiku 5.5 included) with a 429 unless system block 0 is the Claude Code
preamble. The builders in nous/handlers, nous/heart/admission.py and the
runner add it; the fix-node dispatcher, query expansion and the scanned-PDF
transcription built their own payloads without it. Every request here goes to a stubbed
transport under the real backends: nothing reaches the network. A source
guard lists every site in nous/ that sends a request, so a new one fails
until it is reviewed for the preamble.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

import nous
import nous.handlers
from nous.api import anthropic_client
from nous.api.attachment_store import _transcribe_and_ingest_pdf
from nous.config import Settings
from nous.dag.fix_executor import choose_action_llm
from nous.heart.query_expansion import QueryExpander

_PREAMBLE = {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}
_MODEL = "claude-sonnet-5-5"
_REJECTION = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": 'tool_choice: type "tool" and "any" are not supported for this model.',
    },
}


@pytest.fixture(autouse=True)
def _no_model_remembered(monkeypatch):
    monkeypatch.setattr(nous.handlers, "_REJECTS_FORCED_TOOL_CHOICE", set())


async def _fix_dispatch(client: Any, monkeypatch) -> Any:
    result = await choose_action_llm(
        parent_name="export_report",
        parent_instructions="Export the weekly failed-jobs report as CSV.",
        parent_error="TimeoutError: the export query exceeded 30 s",
        parent_result=None,
        fix_instructions=None,
        fix_actions=["skip", "abort"],
        llm_client=client,
        model=_MODEL,
        timeout_seconds=5.0,
    )
    return result.action


async def _query_expansion(client: Any, monkeypatch) -> Any:
    expander = QueryExpander(client, Settings(_env_file=None), model=_MODEL)
    return await expander._call_haiku("how did we fix the flaky staging deploy")


async def _pdf_transcription(client: Any, monkeypatch) -> Any:
    ingest = AsyncMock(return_value={})
    monkeypatch.setattr("nous.api.tools.ingest_document_text", ingest)
    await _transcribe_and_ingest_pdf(
        b"%PDF raw",
        object(),
        Settings(_env_file=None),
        client,
        source_ref="scan.pdf",
        model=_MODEL,
        max_tokens=512,
        filename="scan.pdf",
    )
    return ingest.await_args.kwargs["content"] if ingest.await_args else None


_FIX = {"type": "tool_use", "id": "t", "name": "choose_fix_action", "input": {"action": "skip", "rationale": "ok"}}
_EXPAND = {"type": "tool_use", "id": "t", "name": "expand_query", "input": {"alternative_queries": ["a b", "c d"]}}
_SITES = [
    (_fix_dispatch, [_FIX], "skip", []),
    (_query_expansion, [_EXPAND], ["a b", "c d"], ["You rewrite search queries into semantic variants."]),
    (
        _pdf_transcription,
        [{"type": "text", "text": "Invoice 4471 due 2026-10-10"}],
        "Invoice 4471 due 2026-10-10",
        ["You are a document transcription engine."],
    ),
]
_IDS = ["fix-node dispatch", "query expansion", "scanned-PDF transcription"]


async def _run(site, reply, backend: str, credentials: dict[str, str], monkeypatch) -> tuple[Any, list[dict]]:
    bodies: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        status, payload = reply(body)
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(
        anthropic_client, "_build_transport_with_env_proxies", lambda **_: (httpx.MockTransport(answer), {})
    )
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    client = anthropic_client.create_client(Settings(_env_file=None, api_backend=backend, **credentials))
    await client.start()
    try:
        return await site(client, monkeypatch), bodies
    finally:
        await client.close()


def _message(content: list[dict[str, Any]]) -> dict[str, Any]:
    message = {"id": "msg_1", "type": "message", "role": "assistant", "model": _MODEL}
    message |= {"content": content, "stop_reason": "end_turn", "stop_sequence": None}
    return message | {"usage": {"input_tokens": 1, "output_tokens": 1}}


@pytest.mark.parametrize("auth", ["oauth", "api_key"])
@pytest.mark.parametrize("backend", ["httpx", "sdk"])
@pytest.mark.parametrize(("site", "content", "expected", "own_system"), _SITES, ids=_IDS)
async def test_the_request_opens_with_the_claude_code_preamble(
    site, content, expected, own_system, backend, auth, monkeypatch
):
    credentials = (
        {"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat-test-token"} if auth == "oauth" else {"ANTHROPIC_API_KEY": "test-key"}
    )
    result, bodies = await _run(site, lambda body: (200, _message(content)), backend, credentials, monkeypatch)

    assert result == expected
    (body,) = bodies
    system = body["system"]
    assert isinstance(system, list) and system[0] == _PREAMBLE, (
        f"system block 0 is not the Claude Code preamble: {str(system)[:120]}"
    )
    assert [block["text"][: len(start)] for block, start in zip(system[1:], own_system)] == own_system
    assert len(system) == 1 + len(own_system)


@pytest.mark.parametrize("backend", ["httpx", "sdk"])
@pytest.mark.parametrize(("site", "content", "expected", "own_system"), _SITES[:2], ids=_IDS[:2])
async def test_the_request_asking_for_the_tool_opens_with_the_preamble_too(
    site, content, expected, own_system, backend, monkeypatch
):
    """On a model that rejects a forced tool_choice the request is sent again without it; that one needs the
    preamble too, or it is a 429 under an OAuth token."""

    def reply(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return (400, _REJECTION) if "tool_choice" in body else (200, _message(content))

    result, bodies = await _run(site, reply, backend, {"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat-test-token"}, monkeypatch)

    assert result == expected
    forced, asked = bodies
    for body in (forced, asked):
        assert isinstance(body["system"], list) and body["system"][0] == _PREAMBLE, (
            f"system block 0 is not the Claude Code preamble: {str(body['system'])[:120]}"
        )
    assert asked["system"][-1]["text"].startswith("Respond only by calling the")


# Every place in nous/ that sends a request through an LLM client, outside the client itself
# (nous/api/anthropic_client.py), and every caller of call_with_tool_choice, which sends the payload its caller
# built. Keyed by (module, enclosing function, called expression); the value is how many such calls the function
# makes and why each request opens with the Claude Code preamble. A new site fails the guard below until it is
# reviewed and added here; a site that moves or goes away fails it too, so the list stays true.
_REVIEWED_SITES: dict[tuple[str, str, str], tuple[int, str]] = {
    ("nous/api/runner.py", "AgentRunner._call_api", "self._api.call"): (
        1,
        "the payload comes from _build_api_payload, whose system block 0 is the preamble on both prompt paths",
    ),
    ("nous/api/runner.py", "AgentRunner._call_api", "self._api.call_streaming_aggregated"): (
        1,
        "the same _build_api_payload payload as the call() beside it",
    ),
    ("nous/api/runner.py", "AgentRunner._call_api_stream", "self._api.stream"): (
        1,
        "the payload comes from _build_api_payload",
    ),
    ("nous/handlers/__init__.py", "call_background_llm", "client.call"): (
        1,
        "builds its own payload with the preamble at block 0",
    ),
    ("nous/handlers/__init__.py", "call_with_tool_choice", "client.call"): (
        2,
        "sends the payload of one of the reviewed call_with_tool_choice callers below, all of which put the "
        "preamble at block 0; the request asking for the tool appends to that list, so block 0 stays",
    ),
    ("nous/handlers/__init__.py", "call_background_llm_structured", "call_with_tool_choice"): (
        1,
        "builds its own payload with the preamble at block 0",
    ),
    ("nous/dag/fix_executor.py", "choose_action_llm", "call_with_tool_choice"): (
        1,
        "system is [preamble]; test_the_request_opens_with_the_claude_code_preamble sends it",
    ),
    ("nous/heart/query_expansion.py", "QueryExpander._call_haiku", "call_with_tool_choice"): (
        1,
        "system is [preamble, own prompt]; test_the_request_opens_with_the_claude_code_preamble sends it",
    ),
    ("nous/api/attachment_store.py", "_transcribe_and_ingest_pdf", "llm_client.call"): (
        1,
        "system is [preamble, own prompt]; test_the_request_opens_with_the_claude_code_preamble sends it",
    ),
    ("nous/heart/admission.py", "AdmissionLLMClient.complete", "self._client.call"): (
        1,
        "builds its own payload with the preamble at block 0",
    ),
    ("nous/telegram_bot.py", "NousTelegramBot._chat_streaming", "self._http.stream"): (
        1,
        "not an LLM client: a plain httpx client posting to Nous's own REST endpoint /chat/stream",
    ),
}
_REQUEST_METHODS = frozenset({"call", "stream", "call_streaming_aggregated"})


def _request_sites() -> Counter[tuple[str, str, str]]:
    root = Path(nous.__file__).parent
    found: Counter[tuple[str, str, str]] = Counter()
    for path in sorted(root.rglob("*.py")):
        module = path.relative_to(root.parent).as_posix()
        if module == "nous/api/anthropic_client.py":
            continue

        def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    visit(child, (*scope, child.name))
                    continue
                if isinstance(child, ast.Call):
                    func = child.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                    if (
                        isinstance(func, ast.Attribute) and name in _REQUEST_METHODS
                    ) or name == "call_with_tool_choice":
                        found[(module, ".".join(scope), ast.unparse(func))] += 1
                visit(child, scope)

        visit(ast.parse(path.read_text("utf-8")), ())
    return found


def test_every_request_site_is_reviewed_for_the_preamble():
    """A request built anywhere in nous/ must open with the preamble; this lists every site that sends one."""
    found = _request_sites()
    reviewed = {site: count for site, (count, _reason) in _REVIEWED_SITES.items()}
    new = {site: n for site, n in found.items() if reviewed.get(site) != n}
    gone = {site: n for site, n in reviewed.items() if found.get(site) != n}
    assert not new and not gone, (
        f"request sites changed; put the preamble at system block 0 and review _REVIEWED_SITES. "
        f"unreviewed or changed: {new}; reviewed but not found: {gone}"
    )


def test_only_the_client_module_talks_to_the_messages_api_directly():
    # The call-site guard above follows calls on an LLM client. A module could still reach the API without one:
    # its own SDK client, or an HTTP post to the endpoint. Neither is reviewed for the preamble or for
    # with_default_thinking, so both may appear only in nous/api/anthropic_client.py.
    root = Path(nous.__file__).resolve().parent
    allowed = root / "api" / "anthropic_client.py"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path == allowed:
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        sdk = any(
            (isinstance(node, ast.Import) and any(a.name.split(".")[0] == "anthropic" for a in node.names))
            or (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "anthropic")
            for node in ast.walk(tree)
        )
        if sdk or "/v1/messages" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], offenders
