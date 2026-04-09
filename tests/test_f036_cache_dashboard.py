"""Tests for F036.1 Cache Dashboard endpoint (GET /dashboard/cache)."""

from __future__ import annotations

from unittest.mock import MagicMock

from starlette.testclient import TestClient

from nous.api.rest import create_app
from nous.config import Settings
from nous.observability.context_logger import ContextLogEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(
    *,
    session_id: str = "sess-1",
    turn_number: int = 1,
    input_tokens: int = 10000,
    cache_read: int = 5000,
    cache_created: int = 2000,
    cache_break: bool = False,
    break_components: list[str] | None = None,
    break_tokens_lost: int = 0,
    model: str = "claude-sonnet-4-5",
    timestamp: str = "2026-04-05T12:00:00Z",
) -> ContextLogEntry:
    entry = ContextLogEntry(
        id="test-" + str(turn_number),
        session_id=session_id,
        turn_number=turn_number,
        timestamp=timestamp,
        call_type="chat",
        model=model,
        frame_id="conversation",
    )
    entry.input_tokens_actual = input_tokens
    entry.cache_read_tokens = cache_read
    entry.cache_creation_tokens = cache_created
    entry.cache_break = cache_break
    entry.cache_break_components = break_components or []
    entry.cache_break_tokens_lost = break_tokens_lost
    return entry


def _make_logger(entries: list[ContextLogEntry]) -> MagicMock:
    mock = MagicMock()
    mock.get_recent.return_value = entries
    return mock


def _make_client(entries: list[ContextLogEntry] | None = None, context_logger: MagicMock | None = ...) -> TestClient:
    """Create a test client with a mocked context_logger.

    If context_logger is ... (sentinel), build one from entries.
    If context_logger is explicitly None, pass None.
    """
    if context_logger is ...:
        mock_logger = _make_logger(entries or [])
    else:
        mock_logger = context_logger

    settings = Settings()
    app = create_app(
        runner=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        cognitive=MagicMock(),
        database=MagicMock(),
        settings=settings,
        context_logger=mock_logger,
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_empty_logger_returns_zeroes() -> None:
    """No entries -> all summary fields are 0, empty sessions/timeline."""
    client = _make_client(entries=[])
    resp = client.get("/dashboard/cache")
    assert resp.status_code == 200
    data = resp.json()
    summary = data["summary"]
    assert summary["total_calls"] == 0
    assert summary["total_input_tokens"] == 0
    assert summary["total_cache_read"] == 0
    assert summary["total_cache_created"] == 0
    assert summary["overall_hit_rate"] == 0
    assert summary["total_breaks"] == 0
    assert summary["break_rate"] == 0
    assert summary["tokens_lost_to_breaks"] == 0
    assert data["sessions"] == []
    assert data["timeline"] == []


def test_entries_without_token_data_filtered() -> None:
    """Entries where input_tokens_actual is None are excluded from aggregation."""
    good = _make_entry(turn_number=1, input_tokens=8000, cache_read=4000)
    no_tokens = ContextLogEntry(
        id="test-none",
        session_id="sess-1",
        turn_number=2,
        timestamp="2026-04-05T12:01:00Z",
        call_type="chat",
        model="claude-sonnet-4-5",
        frame_id="conversation",
    )
    # input_tokens_actual defaults to None — should be filtered out
    client = _make_client(entries=[good, no_tokens])
    resp = client.get("/dashboard/cache")
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["total_calls"] == 1
    # total_input = input_tokens(8000) + cache_read(4000) + cache_created(2000)
    assert data["summary"]["total_input_tokens"] == 14000


def test_hit_rate_calculation() -> None:
    """Total input = input + cache_read + cache_created. Hit rate = cache_read / total * 100."""
    # total_input = 1000 + 6000 + 3000 = 10000, hit_rate = 6000/10000 = 60%
    entry = _make_entry(input_tokens=1000, cache_read=6000, cache_created=3000)
    client = _make_client(entries=[entry])
    resp = client.get("/dashboard/cache")
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["overall_hit_rate"] == 60.0
    assert data["summary"]["total_input_tokens"] == 10000
    # Timeline entry should also reflect per-call hit rate
    assert len(data["timeline"]) == 1
    assert data["timeline"][0]["hit_rate"] == 60.0


def test_cache_breaks_counted() -> None:
    """3 entries, 1 with cache_break=True -> total_breaks=1."""
    entries = [
        _make_entry(turn_number=1, cache_break=False),
        _make_entry(turn_number=2, cache_break=True, break_tokens_lost=500),
        _make_entry(turn_number=3, cache_break=False),
    ]
    client = _make_client(entries=entries)
    resp = client.get("/dashboard/cache")
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["total_breaks"] == 1
    assert data["summary"]["tokens_lost_to_breaks"] == 500
    # break_rate = 1/3 * 100 = 33.3%
    assert data["summary"]["break_rate"] == 33.3


def test_break_components_distributed() -> None:
    """Entries with different break_components -> correct counts."""
    entries = [
        _make_entry(
            turn_number=1,
            cache_break=True,
            break_components=["identity", "frame"],
        ),
        _make_entry(
            turn_number=2,
            cache_break=True,
            break_components=["frame", "tools"],
        ),
        _make_entry(turn_number=3, cache_break=False),
    ]
    client = _make_client(entries=entries)
    resp = client.get("/dashboard/cache")
    assert resp.status_code == 200
    data = resp.json()
    components = data["break_components"]
    assert components["identity"] == 1
    assert components["frame"] == 2
    assert components["tools"] == 1


def test_per_session_grouping() -> None:
    """Entries from 2 sessions -> 2 session entries with correct aggregates."""
    entries = [
        _make_entry(session_id="sess-a", turn_number=1, input_tokens=5000, cache_read=3000, cache_created=1000),
        _make_entry(session_id="sess-a", turn_number=2, input_tokens=5000, cache_read=4000, cache_created=500),
        _make_entry(session_id="sess-b", turn_number=1, input_tokens=8000, cache_read=2000, cache_created=3000),
    ]
    client = _make_client(entries=entries)
    resp = client.get("/dashboard/cache")
    assert resp.status_code == 200
    data = resp.json()
    sessions = {s["session_id"]: s for s in data["sessions"]}
    assert len(sessions) == 2

    a = sessions["sess-a"]
    assert a["calls"] == 2
    # total_input = (5000+3000+1000) + (5000+4000+500) = 18500
    assert a["input_tokens"] == 18500
    assert a["cache_read"] == 7000
    assert a["cache_created"] == 1500
    # hit_rate = 7000/18500 * 100 = 37.8%
    assert a["hit_rate"] == 37.8

    b = sessions["sess-b"]
    assert b["calls"] == 1
    # total_input = 8000+2000+3000 = 13000
    assert b["input_tokens"] == 13000
    assert b["cache_read"] == 2000
    # hit_rate = 2000/13000 * 100 = 15.4%
    assert b["hit_rate"] == 15.4


def test_timeline_newest_first_max_50() -> None:
    """60 entries -> timeline has 50 (capped)."""
    entries = [
        _make_entry(turn_number=i, timestamp=f"2026-04-05T{12 + i // 60:02d}:{i % 60:02d}:00Z")
        for i in range(60)
    ]
    client = _make_client(entries=entries)
    resp = client.get("/dashboard/cache")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["timeline"]) == 50


def test_context_logger_none_returns_503() -> None:
    """Pass context_logger=None -> 503 response."""
    client = _make_client(context_logger=None)
    resp = client.get("/dashboard/cache")
    assert resp.status_code == 503
    assert "not enabled" in resp.json()["error"].lower()
