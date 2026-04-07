"""Tests for F032 execution ledger dashboard endpoint."""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from nous.brain.brain import Brain
from nous.cognitive.execution_ledger import (
    ExecutedAction,
    ExecutionLedger,
    redact_key_args,
)
from nous.cognitive.layer import CognitiveLayer

pytestmark = pytest.mark.integration




class MockAgentRunner:
    def __init__(self):
        self._conversations = {}
        self._ledgers: dict[str, ExecutionLedger] = {}
        self._pending_corrections: dict[str, list] = {}

    async def start(self):
        pass

    async def close(self):
        pass


@pytest_asyncio.fixture
async def brain(db, settings):
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def cognitive(brain, heart, settings):
    return CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")


@pytest.fixture
def runner():
    return MockAgentRunner()


@pytest.fixture
def app(runner, brain, heart, cognitive, db, settings):
    from nous.api.rest import create_app

    return create_app(runner, brain, heart, cognitive, db, settings)


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _make_ledger(session_id: str, actions: list[dict] | None = None) -> ExecutionLedger:
    """Create a ledger with pre-populated actions."""
    ledger = ExecutionLedger(session_id=session_id)
    if actions:
        for a in actions:
            ledger.set_turn(a.get("turn", 0))
            ledger.record(
                tool_name=a["tool_name"],
                tool_input=a.get("tool_input", {}),
                result=a.get("result", "ok"),
                status=a.get("status", "success"),
            )
    return ledger


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


async def test_ledger_empty(client):
    """No active ledgers returns empty sessions array."""
    resp = await client.get("/dashboard/ledger")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sessions"] == []
    assert "enabled" in data
    assert "modes" in data


async def test_ledger_with_session(client, runner):
    """Active session returns full action detail."""
    ledger = _make_ledger("test-session-1", [
        {"tool_name": "recall_deep", "tool_input": {"query": "test"}, "turn": 1},
        {"tool_name": "write_file", "tool_input": {"path": "/tmp/f.py"}, "turn": 2},
        {"tool_name": "bash", "tool_input": {"command": "ls -la"}, "turn": 2, "status": "success"},
    ])
    runner._ledgers["test-session-1"] = ledger

    resp = await client.get("/dashboard/ledger")
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["sessions"]) == 1
    session = data["sessions"][0]
    assert session["session_id"] == "test-session-1"
    assert session["total_actions"] == 3
    assert session["success_actions"] == 3
    assert session["blocked_actions"] == 0
    assert session["error_actions"] == 0
    assert session["timeout_actions"] == 0
    assert session["actions_truncated"] is False

    actions = session["actions"]
    assert len(actions) == 3

    # Verify action fields
    a0 = actions[0]
    assert a0["tool_name"] == "recall_deep"
    assert a0["status"] == "success"
    assert a0["side_effect_type"] == "none"
    assert "timestamp" in a0
    # Verify timestamp is ISO format string (not a raw datetime)
    assert isinstance(a0["timestamp"], str)
    assert "T" in a0["timestamp"]


async def test_ledger_status_counts(client, runner):
    """Verify per-status counts are computed correctly."""
    ledger = _make_ledger("count-session", [
        {"tool_name": "recall_deep", "tool_input": {"query": "a"}, "turn": 1, "status": "success"},
        {"tool_name": "write_file", "tool_input": {"path": "f"}, "turn": 1, "status": "blocked"},
        {"tool_name": "bash", "tool_input": {"command": "x"}, "turn": 2, "status": "error"},
        {"tool_name": "bash", "tool_input": {"command": "y"}, "turn": 2, "status": "timeout"},
    ])
    runner._ledgers["count-session"] = ledger

    resp = await client.get("/dashboard/ledger")
    session = resp.json()["sessions"][0]
    assert session["success_actions"] == 1
    assert session["blocked_actions"] == 1
    assert session["error_actions"] == 1
    assert session["timeout_actions"] == 1


async def test_ledger_action_limit(client, runner):
    """action_limit param truncates actions and sets truncated flag."""
    actions = [
        {"tool_name": "recall_deep", "tool_input": {"query": f"q{i}"}, "turn": i}
        for i in range(10)
    ]
    ledger = _make_ledger("limit-session", actions)
    runner._ledgers["limit-session"] = ledger

    resp = await client.get("/dashboard/ledger?action_limit=3")
    session = resp.json()["sessions"][0]
    assert session["total_actions"] == 10
    assert len(session["actions"]) == 3
    assert session["actions_truncated"] is True


async def test_ledger_action_limit_max_200(client, runner):
    """action_limit is capped at 200."""
    ledger = _make_ledger("cap-session", [
        {"tool_name": "recall_deep", "tool_input": {"query": "q"}, "turn": 1},
    ])
    runner._ledgers["cap-session"] = ledger

    # Even with limit=9999, should be capped at 200
    resp = await client.get("/dashboard/ledger?action_limit=9999")
    assert resp.status_code == 200


async def test_ledger_multiple_sessions(client, runner):
    """Multiple sessions are all returned."""
    for i in range(3):
        sid = f"multi-{i}"
        runner._ledgers[sid] = _make_ledger(sid, [
            {"tool_name": "recall_deep", "tool_input": {"query": "x"}, "turn": 1},
        ])

    resp = await client.get("/dashboard/ledger")
    data = resp.json()
    assert len(data["sessions"]) == 3


# ---------------------------------------------------------------------------
# Redaction tests
# ---------------------------------------------------------------------------


def test_redact_key_args_bash_env_var():
    """Bash commands with env vars are redacted."""
    result = redact_key_args("bash", {"command": "OPENAI_API_KEY=sk-abc123 python train.py"})
    assert "sk-abc123" not in result["command"]
    assert "[REDACTED_ENV]" in result["command"]


def test_redact_key_args_bash_bearer():
    """Bash commands with Bearer tokens are redacted."""
    result = redact_key_args("bash", {"command": "curl -H 'Bearer sk-prod-secret' https://api.example.com"})
    assert "sk-prod-secret" not in result["command"]
    assert "Bearer [REDACTED]" in result["command"]


def test_redact_key_args_bash_connection_string():
    """Bash commands with connection strings are redacted."""
    result = redact_key_args("bash", {"command": "psql postgres://user:hunter2@localhost/db"})
    assert "hunter2" not in result["command"]
    assert "[REDACTED]@" in result["command"]


def test_redact_key_args_non_bash_unchanged():
    """Non-bash tools are not redacted."""
    args = {"query": "OPENAI_API_KEY=sk-abc123"}
    result = redact_key_args("recall_deep", args)
    assert result == args


# ---------------------------------------------------------------------------
# ExecutionLedger.current_turn property test
# ---------------------------------------------------------------------------


def test_current_turn_property():
    """current_turn property exposes _current_turn."""
    ledger = ExecutionLedger(session_id="test")
    assert ledger.current_turn == 0
    ledger.set_turn(5)
    assert ledger.current_turn == 5
