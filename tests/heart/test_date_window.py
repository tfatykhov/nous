import datetime
import pytest
from types import SimpleNamespace
from nous.heart.date_window import DateWindow, has_temporal_signal

def test_temporal_signal_detects_month_year():
    assert has_temporal_signal("What happened in late April 2026?") is True
    assert has_temporal_signal("changes around mid-May") is True
    assert has_temporal_signal("events on 2026-06-24") is True

def test_temporal_signal_rejects_non_temporal():
    assert has_temporal_signal("How does the calibration gate work?") is False
    assert has_temporal_signal("summarize the trading bot design") is False

def test_datewindow_is_frozen():
    w = DateWindow(start=datetime.date(2026, 4, 20), end=datetime.date(2026, 4, 30))
    assert w.start < w.end


# ---------------------------------------------------------------------------
# Task 3: DateWindowParser tests
# ---------------------------------------------------------------------------
from nous.heart.date_window import DateWindowParser


class _FakeClient:
    def __init__(self, tool_input, raises=False, calls=None):
        self._input = tool_input; self._raises = raises; self.calls = calls if calls is not None else []
    async def call(self, payload):
        self.calls.append(payload)
        if self._raises:
            raise RuntimeError("boom")
        return SimpleNamespace(content=[{"type": "tool_use", "name": "emit_window", "input": self._input}])

def _settings(**kw):
    base = dict(date_leg_model="claude-haiku-4-5-20251001", date_leg_timeout_seconds=2.0,
                date_leg_max_per_hour=500, date_leg_pad_days=2)
    base.update(kw); return SimpleNamespace(**base)

TODAY = datetime.date(2026, 7, 1)

@pytest.mark.asyncio
async def test_parse_returns_padded_window():
    client = _FakeClient({"has_date": True, "start_date": "2026-04-20", "end_date": "2026-04-30"})
    p = DateWindowParser(client, _settings())
    w = await p.parse("what happened in late April 2026?", TODAY)
    assert w == DateWindow(start=datetime.date(2026, 4, 18), end=datetime.date(2026, 5, 2))  # +/- 2 pad

@pytest.mark.asyncio
async def test_pregate_skips_llm_for_non_temporal():
    client = _FakeClient({"has_date": True, "start_date": "2026-01-01", "end_date": "2026-01-02"})
    p = DateWindowParser(client, _settings())
    assert await p.parse("how does calibration work?", TODAY) is None
    assert client.calls == []  # no LLM call

@pytest.mark.asyncio
async def test_parser_system_prompt_is_non_empty():
    # codex P1: an empty system text block is rejected by Anthropic's prompt cache
    # (400), which would fail every parse open to None. Assert no system block is blank.
    client = _FakeClient({"has_date": True, "start_date": "2026-04-20", "end_date": "2026-04-30"})
    p = DateWindowParser(client, _settings())
    await p.parse("what happened in late April 2026?", TODAY)
    assert client.calls, "LLM was not called"
    sys_blocks = client.calls[-1]["system"]
    assert sys_blocks and all(b["text"].strip() for b in sys_blocks), \
        "an empty system block would 400 on Anthropic's prompt cache"

@pytest.mark.asyncio
async def test_cache_key_includes_today_for_relative_queries():
    # codex P2: a relative query cached on one day must re-parse on the next —
    # "yesterday" resolves to a different window per `today`. Cache key includes today.
    client = _FakeClient({"has_date": True, "start_date": "2026-07-01", "end_date": "2026-07-01"})
    p = DateWindowParser(client, _settings())
    q = "what happened yesterday?"
    await p.parse(q, datetime.date(2026, 7, 2))
    await p.parse(q, datetime.date(2026, 7, 2))   # same day -> cache hit, no new call
    assert len(client.calls) == 1
    await p.parse(q, datetime.date(2026, 7, 3))   # next day -> different key -> re-parse
    assert len(client.calls) == 2

@pytest.mark.asyncio
async def test_has_date_false_returns_none():
    client = _FakeClient({"has_date": False})
    p = DateWindowParser(client, _settings())
    assert await p.parse("something about last quarter's vibe", TODAY) is None

@pytest.mark.asyncio
async def test_fail_open_on_client_error():
    client = _FakeClient(None, raises=True)
    p = DateWindowParser(client, _settings())
    assert await p.parse("events in April 2026", TODAY) is None

@pytest.mark.asyncio
async def test_cache_hit_skips_second_llm_call():
    client = _FakeClient({"has_date": True, "start_date": "2026-04-20", "end_date": "2026-04-30"})
    p = DateWindowParser(client, _settings())
    q = "what happened in late April 2026?"
    await p.parse(q, TODAY); await p.parse(q, TODAY)
    assert len(client.calls) == 1  # second served from cache

@pytest.mark.asyncio
async def test_budget_cap_fails_open():
    client = _FakeClient({"has_date": True, "start_date": "2026-04-20", "end_date": "2026-04-30"})
    p = DateWindowParser(client, _settings(date_leg_max_per_hour=1))
    await p.parse("events in April 2026", TODAY)          # uses the 1 budget
    assert await p.parse("events in May 2026", TODAY) is None  # budget exhausted -> fail open

@pytest.mark.asyncio
async def test_inverted_dates_return_none():
    client = _FakeClient({"has_date": True, "start_date": "2026-04-30", "end_date": "2026-04-20"})
    p = DateWindowParser(client, _settings())
    assert await p.parse("events in April 2026", TODAY) is None

@pytest.mark.asyncio
async def test_none_result_is_cached():
    client = _FakeClient({"has_date": False})
    p = DateWindowParser(client, _settings())
    q = "some vague temporal-ish query mentioning last quarter"
    assert await p.parse(q, TODAY) is None
    assert await p.parse(q, TODAY) is None
    assert len(client.calls) == 1  # second call served from cache, no LLM


# ---------------------------------------------------------------------------
# I-1: bounded cache + TTL flag
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cache_size_bounded(monkeypatch):
    """Cache must not grow past _CACHE_MAX_ENTRIES; oldest entry is evicted."""
    import nous.heart.date_window as dw_mod
    monkeypatch.setattr(dw_mod, "_CACHE_MAX_ENTRIES", 2)
    client = _FakeClient({"has_date": True, "start_date": "2026-04-20", "end_date": "2026-04-30"})
    p = DateWindowParser(client, _settings())
    await p.parse("events in April 2026", TODAY)
    await p.parse("events in May 2026", TODAY)
    await p.parse("events in June 2026", TODAY)  # should evict oldest
    assert len(p._cache) <= 2


@pytest.mark.asyncio
async def test_ttl_zero_disables_caching():
    """date_leg_cache_ttl_days=0 means never cache; same query hits LLM every time."""
    client = _FakeClient({"has_date": True, "start_date": "2026-04-20", "end_date": "2026-04-30"})
    p = DateWindowParser(client, _settings(date_leg_cache_ttl_days=0))
    q = "events in April 2026"
    await p.parse(q, TODAY)
    await p.parse(q, TODAY)
    assert len(client.calls) == 2  # no cache hit when ttl=0
