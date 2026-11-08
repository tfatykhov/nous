"""F108 Phase 1, PR 2: every background model call names what it was for.

The three background helpers (``call_background_llm``, ``call_background_llm_structured``, ``call_with_tool_choice``)
take a keyword ``purpose`` and pass it to the client as a ``CallContext`` through ``context_kwargs``, so a client
double that takes only the payload keeps working. ``call_with_tool_choice`` gives its requests one ``group_id`` and
marks the request that asks for the tool in the prompt ``is_fallback``. Every model call site in ``nous/`` passes a
purpose from ``PURPOSES``. The source guard below takes every call of a helper and every method call named ``call``,
``stream`` or ``call_streaming_aggregated``, whatever its receiver is called: a site that passes no purpose fails
unless it is reviewed as one that passes none by design, and a site whose purpose is not written there as a literal
fails unless it is reviewed.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import nous
import nous.handlers
from nous.config import Settings
from nous.handlers import call_background_llm, call_background_llm_structured, call_with_tool_choice
from nous.observability.model_calls import PURPOSES, CallContext, context_kwargs

# How the httpx backend reports the API's rejection of a forced tool_choice.
_REJECTION = RuntimeError(
    'Anthropic API error (400): invalid_request_error - tool_choice: type "tool" and "any" are not supported '
    "for this model."
)
_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


@pytest.fixture(autouse=True)
def _no_model_remembered(monkeypatch):
    monkeypatch.setattr(nous.handlers, "_REJECTS_FORCED_TOOL_CHOICE", set())


def _reply(payload: dict[str, Any]) -> SimpleNamespace:
    """A whole answer to ``payload``: its one tool's call, or text."""
    if "tools" in payload:
        name = payload["tools"][0]["name"]
        block = {"type": "tool_use", "id": "t1", "name": name, "input": {"answer": "yes"}}
        return SimpleNamespace(content=[block], stop_reason="tool_use")
    return SimpleNamespace(content=[{"type": "text", "text": "ok"}], stop_reason="end_turn")


class _Client:
    """A model client that takes ``call_context``, as both Anthropic clients do."""

    supports_call_context = True

    def __init__(self, rejects_forcing: bool = False) -> None:
        self.rejects_forcing = rejects_forcing
        self.calls: list[tuple[dict[str, Any], CallContext | None]] = []

    async def call(self, payload: dict[str, Any], call_context: CallContext | None = None) -> Any:
        self.calls.append((payload, call_context))
        if self.rejects_forcing and "tool_choice" in payload:
            raise _REJECTION
        return _reply(payload)


class _PlainClient:
    """A test double that takes only the payload and says nothing about a call context."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def call(self, payload: dict[str, Any]) -> Any:
        self.payloads.append(payload)
        return _reply(payload)


def _forced_payload() -> dict[str, Any]:
    return {
        "model": "claude-test",
        "max_tokens": 50,
        "system": [{"type": "text", "text": "sys"}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "tools": [{"name": "answer_tool", "description": "d", "input_schema": _SCHEMA}],
        "tool_choice": {"type": "tool", "name": "answer_tool"},
    }


async def _text(client: Any, purpose: str | None) -> Any:
    kwargs = {} if purpose is None else {"purpose": purpose}
    return await call_background_llm(client, "claude-test", "sys", "user", **kwargs)


async def _structured(client: Any, purpose: str | None) -> Any:
    kwargs = {} if purpose is None else {"purpose": purpose}
    return await call_background_llm_structured(
        client, "claude-test", "sys", "user", "answer_tool", "d", _SCHEMA, **kwargs
    )


async def _tool_choice(client: Any, purpose: str | None) -> Any:
    kwargs = {} if purpose is None else {"purpose": purpose}
    return await call_with_tool_choice(client, _forced_payload(), **kwargs)


_HELPER_RUNS = [
    pytest.param(_text, "ok", id="call_background_llm"),
    pytest.param(_structured, {"answer": "yes"}, id="call_background_llm_structured"),
    pytest.param(_tool_choice, {"answer": "yes"}, id="call_with_tool_choice"),
]


# ---------------------------------------------------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("run", "expected"), _HELPER_RUNS)
async def test_each_helper_forwards_its_purpose_to_the_client(run, expected):
    client = _Client()

    assert await run(client, "sleep_merge") == expected

    [(_payload, ctx)] = client.calls
    assert isinstance(ctx, CallContext)
    assert ctx.purpose == "sleep_merge"
    assert ctx.is_fallback is False


@pytest.mark.asyncio
async def test_call_background_llm_carries_the_session_id_in_the_same_context():
    client = _Client()

    await call_background_llm(client, "claude-test", "sys", "user", purpose="critic", session_id="sess-1")

    [(_payload, ctx)] = client.calls
    assert (ctx.purpose, ctx.session_id) == ("critic", "sess-1")


@pytest.mark.asyncio
async def test_the_correction_monitor_labels_its_call_with_its_session():
    # monitor.py holds the turn's session_id where it calls the helper, so the row carries it.
    from nous.cognitive.monitor import MonitorEngine

    engine = MonitorEngine(MagicMock(), MagicMock(), Settings(correction_extraction_enabled=True))
    engine._llm_client = client = _Client()

    await engine.detect_and_extract_correction(
        user_message="no, actually that's wrong", ai_response="some response", session_id="sess-7"
    )

    [(_payload, ctx)] = client.calls
    assert (ctx.purpose, ctx.session_id) == ("correction_extractor", "sess-7")


@pytest.mark.asyncio
async def test_the_dedup_tiebreaker_is_fact_dedup_not_contradiction():
    from nous.heart.facts import FactManager

    facts = FactManager(MagicMock(), None, "test-agent")
    facts._llm = client = _Client()

    await facts.is_distinct_fact("The user lives in Lisbon.", "The user's home is Lisbon.")

    [(_payload, ctx)] = client.calls
    assert ctx.purpose == "fact_dedup"


@pytest.mark.asyncio
async def test_the_procedure_learner_labels_distilling_and_the_covered_check_apart():
    from test_procedure_learner import _covered_setup, _llm_answers

    # A probe hit below the no-check line: the lesson is distilled, then the model is asked whether it is covered.
    learner, _heart, llm_client, _summaries, detail, extraction = _covered_setup(0.6, ["auto:decision_cluster"])
    llm_client.supports_call_context = True
    _llm_answers(llm_client, extraction, {"verdict": "COVERED", "id": str(detail.id)[:8]})

    stats = await learner.run_sleep_learning()

    assert stats["covered"] == 1
    purposes = [c.kwargs["call_context"].purpose for c in llm_client.call.call_args_list]
    assert purposes == ["procedure_learner", "procedure_coverage"]


def test_the_llm_client_protocol_takes_the_context_the_helpers_pass():
    import inspect
    import typing

    from nous.api.anthropic_client import HttpxAnthropicClient, SdkAnthropicClient
    from nous.handlers import LLMClient

    declared = inspect.signature(LLMClient.call).parameters["call_context"]
    assert declared.kind is inspect.Parameter.KEYWORD_ONLY and declared.default is None
    assert typing.get_type_hints(LLMClient.call)["call_context"] == CallContext | None
    for client in (HttpxAnthropicClient, SdkAnthropicClient):
        assert inspect.signature(client.call).parameters["call_context"] == declared


def test_the_is_fallback_column_comment_names_the_fallback_path():
    sql = (Path(nous.__file__).resolve().parent.parent / "sql/migrations/101_model_call_log.sql").read_text("utf-8")
    comment = " ".join(line.removeprefix("--").strip() for line in sql.splitlines() if line.startswith("--"))
    assert (
        "is_fallback marks a call made on a fallback path (a re-send that asks for the tool in the prompt, "
        "or a fallback model)"
    ) in comment
    assert not [line for line in sql.splitlines() if line.lstrip().startswith("--") and ";" in line]


@pytest.mark.asyncio
@pytest.mark.parametrize(("run", "expected"), _HELPER_RUNS)
async def test_a_helper_called_without_a_purpose_is_unlabelled(run, expected):
    # The default keeps the tests that call the helpers working; the recorder counts what it labels unlabelled.
    client = _Client()

    assert await run(client, None) == expected

    [(_payload, ctx)] = client.calls
    assert ctx is not None and ctx.purpose == "unlabelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(("run", "expected"), _HELPER_RUNS)
async def test_fake_client_without_support_still_works(run, expected):
    client = _PlainClient()

    assert await run(client, "critic") == expected
    assert len(client.payloads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("run", "expected"), _HELPER_RUNS)
async def test_a_mock_client_with_a_payload_only_call_still_works(run, expected):
    # A MagicMock answers every attribute, ``supports_call_context`` included, with a truthy mock: only a client that
    # says True is handed a context, or ``llm.call = <a function of the payload>`` raises TypeError.
    payloads: list[dict[str, Any]] = []
    llm = MagicMock()

    async def call(payload: dict[str, Any]) -> Any:
        payloads.append(payload)
        return _reply(payload)

    llm.call = call

    assert await run(llm, "dag_fix") == expected
    assert len(payloads) == 1
    assert context_kwargs(MagicMock(), CallContext(purpose="dag_fix")) == {}


@pytest.mark.asyncio
async def test_tool_choice_resend_shares_group_id_and_marks_fallback():
    client = _Client(rejects_forcing=True)

    assert await call_with_tool_choice(client, _forced_payload(), purpose="query_expansion") == {"answer": "yes"}

    (forced, first), (asked, second) = client.calls
    assert "tool_choice" in forced and "tool_choice" not in asked
    assert first.purpose == second.purpose == "query_expansion"
    assert first.group_id is not None and first.group_id == second.group_id
    assert first.is_fallback is False and second.is_fallback is True


@pytest.mark.asyncio
async def test_a_remembered_model_sends_only_the_fallback_request_marked():
    # A model that rejected forcing once is asked in the prompt from then on: one request, the fallback form.
    nous.handlers._REJECTS_FORCED_TOOL_CHOICE.add("claude-test")
    client = _Client(rejects_forcing=True)

    assert await call_with_tool_choice(client, _forced_payload(), purpose="dag_fix") == {"answer": "yes"}

    [(asked, ctx)] = client.calls
    assert "tool_choice" not in asked
    assert ctx.purpose == "dag_fix" and ctx.is_fallback is True and ctx.group_id is not None


@pytest.mark.asyncio
async def test_each_tool_choice_call_has_its_own_group():
    client = _Client()

    await call_with_tool_choice(client, _forced_payload(), purpose="dag_fix")
    await call_with_tool_choice(client, _forced_payload(), purpose="dag_fix")

    (_p1, first), (_p2, second) = client.calls
    assert first.group_id != second.group_id


@pytest.mark.asyncio
async def test_the_helpers_send_the_payload_they_sent_before():
    # Only the context is new: the request a plain client receives is the one a recording client receives.
    plain, recording = _PlainClient(), _Client()
    for run, _expected in [p.values for p in _HELPER_RUNS]:
        await run(plain, None)
        await run(recording, "critic")
    assert plain.payloads == [payload for payload, _ctx in recording.calls]


# ---------------------------------------------------------------------------------------------------------------------
# The source guard
# ---------------------------------------------------------------------------------------------------------------------

_HELPERS = frozenset({"call_background_llm", "call_background_llm_structured", "call_with_tool_choice"})
_CLIENT_METHODS = frozenset({"call", "stream", "call_streaming_aggregated"})

# Both lists below are keyed by (module, enclosing function, called expression); the value is how many such calls the
# function makes and why each is right. A new site fails the guard until it is reviewed and added; one that moves,
# goes away or is added beside a reviewed one fails it too.

# Sites that pass a purpose they do not write as a literal.
_REVIEWED_SITES: dict[tuple[str, str, str], tuple[int, str]] = {
    ("nous/handlers/__init__.py", "call_background_llm", "client.call"): (
        1,
        "passes its caller's purpose keyword, which this guard requires at every caller",
    ),
    ("nous/handlers/__init__.py", "call_with_tool_choice", "client.call"): (
        2,
        "the forced request and the request asking for the tool, both with the caller's purpose and one group_id",
    ),
    ("nous/handlers/__init__.py", "call_background_llm_structured", "call_with_tool_choice"): (
        1,
        "passes its caller's purpose keyword through",
    ),
    ("nous/api/runner.py", "AgentRunner._call_api", "self._api.call"): (
        1,
        "the call context its caller built with _call_context; "
        "test_runner_call_context.py::test_every_runner_call_api_site_passes_call_context guards every caller",
    ),
    ("nous/api/runner.py", "AgentRunner._call_api", "self._api.call_streaming_aggregated"): (
        1,
        "the same call context as the call() beside it",
    ),
    ("nous/api/runner.py", "AgentRunner._call_api_stream", "self._api.stream"): (
        1,
        "the call context its caller built; the runner's own guard checks every caller",
    ),
    ("nous/handlers/procedure_learner.py", "ProcedureLearner._call_llm", "call_background_llm"): (
        1,
        "the purpose its caller names: procedure_learner for distilling and reviewing procedures, "
        "procedure_coverage for the covered check",
    ),
}

# Calls named like a model call that pass no purpose, by design: the clients' own internals, which run inside the
# scope their method opened with its call context, and receivers that are not model clients.
_NO_PURPOSE_SITES: dict[tuple[str, str, str], tuple[int, str]] = {
    ("nous/api/anthropic_client.py", "HttpxAnthropicClient.stream", "self._http.stream"): (
        1,
        "the HTTP request of a stream() call, inside the scope stream() opened with its call_context",
    ),
    ("nous/api/anthropic_client.py", "HttpxAnthropicClient._aggregate", "self.stream"): (
        1,
        "hands stream() the scope call_streaming_aggregated() opened (_scope=), so the aggregated call is one row",
    ),
    ("nous/api/anthropic_client.py", "SdkAnthropicClient.call_streaming_aggregated", "self._client.messages.stream"): (
        1,
        "the SDK request of the call, inside the scope call_streaming_aggregated() opened with its call_context",
    ),
    ("nous/telegram_bot.py", "NousTelegramBot._chat_streaming", "self._http.stream"): (
        1,
        "not a model client: a plain httpx client posting to Nous's own REST endpoint /chat/stream",
    ),
}

_Site = tuple[str, str, str]


def _is_model_call(call: ast.Call) -> bool:
    """A call of a background helper, or any method call named like a client's (whatever the receiver is called)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in _HELPERS
    if isinstance(func, ast.Attribute):
        return func.attr in _HELPERS or func.attr in _CLIENT_METHODS
    return False


def _literal_purpose(value: ast.expr) -> str | None:
    """The purpose a keyword value names as a literal, or None."""
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None


def _purpose_at(call: ast.Call) -> tuple[bool, str | None]:
    """``(passes a purpose, the literal it names)``: a helper's ``purpose=`` keyword, or a client call's
    ``**context_kwargs(client, CallContext(purpose=...))``. ``purpose="unlabelled"`` or ``None`` passes none."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name in _HELPERS:
        values = [k.value for k in call.keywords if k.arg == "purpose"]
    else:
        values = []
        for k in call.keywords:
            v = k.value
            if k.arg is None and isinstance(v, ast.Call) and getattr(v.func, "id", None) == "context_kwargs":
                ctx = v.args[1] if len(v.args) > 1 else None
                if isinstance(ctx, ast.Call) and getattr(ctx.func, "id", None) == "CallContext":
                    values = [kw.value for kw in ctx.keywords if kw.arg == "purpose"] or [ast.Constant(None)]
                else:
                    values = [ctx] if ctx is not None else [ast.Constant(None)]
    if not values:
        return False, None
    [value] = values
    if isinstance(value, ast.Constant) and value.value in (None, "unlabelled"):
        return False, None
    return True, _literal_purpose(value)


def _scan(tree: ast.AST, module: str) -> tuple[list[tuple[_Site, int]], Counter[_Site]]:
    """``(sites that pass no purpose, with their line; sites whose purpose is not a literal, counted)``."""
    missing: list[tuple[_Site, int]] = []
    not_literal: Counter[_Site] = Counter()

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, (*scope, child.name))
                continue
            if isinstance(child, ast.Call) and _is_model_call(child):
                site = (module, ".".join(scope), ast.unparse(child.func))
                passes, literal = _purpose_at(child)
                if not passes:
                    missing.append((site, child.lineno))
                elif literal is None:
                    not_literal[site] += 1
            visit(child, scope)

    visit(tree, ())
    return missing, not_literal


def _scan_nous() -> tuple[list[tuple[_Site, int]], Counter[_Site]]:
    root = Path(nous.__file__).resolve().parent
    missing: list[tuple[_Site, int]] = []
    not_literal: Counter[_Site] = Counter()
    for path in sorted(root.rglob("*.py")):
        module = path.relative_to(root.parent).as_posix()
        m, n = _scan(ast.parse(path.read_text(encoding="utf-8")), module)
        missing += m
        not_literal += n
    return missing, not_literal


def _unreviewed(
    found: Counter[_Site], reviewed_sites: dict[_Site, tuple[int, str]]
) -> tuple[dict[_Site, int], dict[_Site, int]]:
    """``(sites found that the list does not hold with that count, listed sites not found with their count)``."""
    reviewed = {site: count for site, (count, _reason) in reviewed_sites.items()}
    new = {site: n for site, n in found.items() if reviewed.get(site) != n}
    gone = {site: n for site, n in reviewed.items() if found.get(site) != n}
    return new, gone


def test_every_model_call_site_names_a_purpose():
    """Every model call in nous/ passes a purpose; one that passes none, or none written as a literal, is reviewed."""
    missing, not_literal = _scan_nous()
    new, gone = _unreviewed(Counter(site for site, _line in missing), _NO_PURPOSE_SITES)
    lines = {site: [line for s, line in missing if s == site] for site in new}
    assert not new and not gone, (
        f"model calls without a purpose (pass purpose= from PURPOSES, or review _NO_PURPOSE_SITES): "
        f"unreviewed or changed: {new} at lines {lines}; reviewed but not found: {gone}"
    )
    new, gone = _unreviewed(not_literal, _REVIEWED_SITES)
    assert not new and not gone, (
        f"sites passing a purpose that is not a literal changed; review _REVIEWED_SITES. "
        f"unreviewed or changed: {new}; reviewed but not found: {gone}"
    )


def test_the_guard_fails_for_a_new_site():
    # Mutation check on source text, so nothing in the tree is touched: a new unlabelled helper call and a new client
    # call each fail, whatever the client is called or however it is reached; a second call beside a reviewed one, or
    # a reviewed one gone, changes the count.
    source = (
        "async def summarise(client):\n"
        "    await call_background_llm(client, 'm', 's', 'u')\n"
        "    await call_background_llm(client, 'm', 's', 'u', purpose='unlabelled')\n"
        "    await call_background_llm(client, 'm', 's', 'u', purpose='episode_summarizer')\n"
        "async def classify(self):\n"
        "    await self._llm.call(payload)\n"
        "    await self._llm.call(payload, **context_kwargs(self._llm, CallContext()))\n"
        "    await self._llm.call(payload, **context_kwargs(self._llm, CallContext(purpose='critic')))\n"
        "    await self._llm.stream(payload, **context_kwargs(self._llm, ctx))\n"
        "    await self._model.call(payload)\n"
        "    c = self._llm\n"
        "    await c.call(payload)\n"
        "    await (await self._get_client()).call(payload)\n"
        "    await self.clients['x'].stream(payload)\n"
        "    await self._backend.call_streaming_aggregated(payload)\n"
    )
    missing, not_literal = _scan(ast.parse(source), "nous/x.py")
    assert [line for _site, line in missing] == [2, 3, 6, 7, 10, 12, 13, 14, 15]
    assert not_literal == Counter({("nous/x.py", "classify", "self._llm.stream"): 1})

    real_missing, real = _scan_nous()
    for found, listed in ((real, _REVIEWED_SITES), (Counter(s for s, _line in real_missing), _NO_PURPOSE_SITES)):
        site = next(iter(listed))
        assert _unreviewed(found, listed) == ({}, {})
        assert _unreviewed(found + Counter({site: 1}), listed)[0] == {site: listed[site][0] + 1}
        assert _unreviewed(found - Counter({site: 1}), listed)[1] == {site: listed[site][0]}


def test_every_purpose_literal_is_in_the_vocabulary():
    root = Path(nous.__file__).resolve().parent
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "purpose":
                literal = _literal_purpose(node.value)
                if literal is not None:
                    found.setdefault(literal, []).append(f"{path.relative_to(root).as_posix()}:{node.value.lineno}")
    assert found, "no purpose literal found: the scan is broken"
    assert {p: where for p, where in found.items() if p not in PURPOSES or p == "unlabelled"} == {}
