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
