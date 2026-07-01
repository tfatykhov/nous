# F075 Layer 3 — Date-Window Retrieval Leg Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a seed-independent, date-keyed retrieval leg to fact recall — parse a date window from the query, retrieve in-window facts ranked by cosine, and fuse them into the results via `_rrf_merge_n`.

**Architecture:** A new `nous/heart/date_window.py` (a `DateWindow` value + a `DateWindowParser` with a regex pre-gate → Haiku fallback, budget-capped, cached, fail-open). `run_recall_pipeline` parses the window once per query when `NOUS_DATE_LEG_ENABLED=true` and threads it through `heart.recall` → `FactStore.search` → `FactStore._search`, where a date-window SQL leg is fused with the hybrid results by `_rrf_merge_n`. Off by default → byte-identical to today.

**Tech Stack:** Python 3.12+, async SQLAlchemy/asyncpg, pgvector (`<=>`), pydantic-settings, pytest + pytest-asyncio, Anthropic Haiku via the existing `nous.api.anthropic_client` client.

## Global Constraints

- **Land-dark:** every new flag defaults OFF/inert. `NOUS_DATE_LEG_ENABLED=false` → recall output byte-identical to today.
- **Every new boolean Settings field must be pinned in bare-MagicMock fixtures** (`tests/test_streaming.py::_make_mock_settings` + siblings) — a bare MagicMock returns a truthy child and defeats default-off. `date_leg_enabled` must be pinned `False`.
- **Fail-open:** the parser never raises into the recall path — timeout / budget / error / no-date all return `None` → no leg → fused == vanilla.
- **Fusion is position-based:** `_rrf_merge_n` ranks by list position, not score magnitude, so mixing the normalized hybrid list with a raw date-leg list is correct.
- Ranking within the date window is **cosine-to-query**, never date-distance alone (the validated mechanism).
- Validated params (do not change without re-measuring): window pad = 2 days, leg depth K = 15.

---

## File Structure

- **Create** `nous/heart/date_window.py` — `DateWindow` dataclass + `DateWindowParser` (pre-gate, Haiku parse, in-process budget + TTL cache, fail-open).
- **Modify** `nous/config.py` — 7 `date_leg_*` settings.
- **Modify** `nous/heart/facts.py` — `_date_window_leg()` SQL helper; thread `date_window` into `search`/`_search`; fuse via `_rrf_merge_n`.
- **Modify** `nous/heart/heart.py` — thread `date_window` through `recall` and the fact dispatch.
- **Modify** `nous/api/retrieval_pipeline.py` — parse the window once (flag-gated) and pass into `heart.recall`.
- **Create** `nous_eval/date_leg_rescue.py` — the rescue-metric dev harness.
- **Tests:** `tests/heart/test_date_window.py`, `tests/heart/test_date_window_leg.py`, `tests/api/test_date_leg_pipeline.py`.

---

## Task 1: Config flags

**Files:**
- Modify: `nous/config.py`
- Modify: `tests/test_streaming.py` (pin the bool in the mock)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.date_leg_enabled: bool`, `date_leg_model: str`, `date_leg_k: int`, `date_leg_pad_days: int`, `date_leg_timeout_seconds: float`, `date_leg_max_per_hour: int`, `date_leg_cache_ttl_days: int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py  (add)
def test_date_leg_settings_defaults():
    from nous.config import Settings
    s = Settings()
    assert s.date_leg_enabled is False
    assert s.date_leg_model == "claude-haiku-4-5-20251001"
    assert s.date_leg_k == 15
    assert s.date_leg_pad_days == 2
    assert s.date_leg_timeout_seconds == 2.0
    assert s.date_leg_max_per_hour == 500
    assert s.date_leg_cache_ttl_days == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_date_leg_settings_defaults -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'date_leg_enabled'`.

- [ ] **Step 3: Add the settings**

In `nous/config.py`, add to the `Settings` class (near the other retrieval flags):
```python
    date_leg_enabled: bool = Field(
        default=False,
        description="F075 Layer 3: enable the date-window retrieval leg. "
        "Off = byte-identical to today. Land-dark; flip after the A/B gate.",
    )
    date_leg_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="F075 L3: Haiku model for parsing the query's date window.",
    )
    date_leg_k: int = Field(
        default=15, description="F075 L3: date-leg retrieval depth (validated).",
    )
    date_leg_pad_days: int = Field(
        default=2, description="F075 L3: +/- days padding on the parsed window (validated).",
    )
    date_leg_timeout_seconds: float = Field(
        default=2.0, description="F075 L3: parser timeout; breach fails open to no-date.",
    )
    date_leg_max_per_hour: int = Field(
        default=500, description="F075 L3: per-hour Haiku budget cap on the parser.",
    )
    date_leg_cache_ttl_days: int = Field(
        default=30, description="F075 L3: parsed-window in-process cache retention.",
    )
```

- [ ] **Step 4: Pin the bool in the mock fixture**

In `tests/test_streaming.py`, inside `_make_mock_settings` (and any sibling bare-MagicMock settings factory), add:
```python
    settings.date_leg_enabled = False
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py::test_date_leg_settings_defaults tests/test_streaming.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add nous/config.py tests/test_config.py tests/test_streaming.py
git commit -m "feat(config): add F075 L3 date-leg settings, land-dark"
```

---

## Task 2: `DateWindow` + regex pre-gate (pure, no LLM)

**Files:**
- Create: `nous/heart/date_window.py`
- Test: `tests/heart/test_date_window.py`

**Interfaces:**
- Produces: `DateWindow` (frozen dataclass: `start: datetime.date`, `end: datetime.date`); `has_temporal_signal(query: str) -> bool` (regex pre-gate; True means "worth an LLM parse").

- [ ] **Step 1: Write the failing test**

```python
# tests/heart/test_date_window.py
import datetime
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/heart/test_date_window.py -v`
Expected: FAIL — module does not exist (ImportError).

- [ ] **Step 3: Implement `DateWindow` + `has_temporal_signal`**

```python
# nous/heart/date_window.py
"""F075 Layer 3: parse a date window from a query for the date-window retrieval leg."""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

# Month names, 4-digit years, ISO dates, and vague-period words. A hit means the
# query is worth a Haiku parse; a miss skips the LLM entirely (hot-path guard).
_MONTHS = (r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|"
           r"aug(ust)?|sep(tember)?|oct(ober)?|nov(ember)?|dec(ember)?")
_TEMPORAL_RE = re.compile(
    r"\b(" + _MONTHS + r")\b"
    r"|\b(19|20)\d{2}\b"                 # 4-digit year
    r"|\d{4}-\d{2}-\d{2}"                # ISO date
    r"|\b(yesterday|today|tomorrow|last|recent(ly)?|ago|"
    r"early|mid|late|around|when|during|after|before|since)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DateWindow:
    start: datetime.date
    end: datetime.date


def has_temporal_signal(query: str) -> bool:
    """Cheap pre-gate: True when the query plausibly references a time period."""
    return bool(_TEMPORAL_RE.search(query or ""))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/heart/test_date_window.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add nous/heart/date_window.py tests/heart/test_date_window.py
git commit -m "feat(heart): DateWindow + regex temporal pre-gate (F075 L3)"
```

---

## Task 3: `DateWindowParser` — Haiku parse, budget, cache, fail-open

**Files:**
- Modify: `nous/heart/date_window.py`
- Test: `tests/heart/test_date_window.py`

**Interfaces:**
- Consumes: `has_temporal_signal`, `DateWindow`; an LLM client exposing `async call(payload: dict)` returning an object whose `.content` is `list[dict]` with `{"type": "tool_use", "input": {...}}` blocks (the `nous.api.anthropic_client` contract).
- Produces: `DateWindowParser(llm_client, settings)`, `async parse(query: str, today: datetime.date) -> DateWindow | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/heart/test_date_window.py  (add)
import datetime
import pytest
from types import SimpleNamespace
from nous.heart.date_window import DateWindowParser, DateWindow

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/heart/test_date_window.py -k Parser -v` (and the new tests)
Expected: FAIL — `DateWindowParser` not defined (ImportError).

- [ ] **Step 3: Implement `DateWindowParser`**

Append to `nous/heart/date_window.py`:
```python
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

_TOOL = {
    "name": "emit_window",
    "description": "Extract the time period a question asks about.",
    "input_schema": {
        "type": "object",
        "properties": {
            "has_date": {"type": "boolean"},
            "start_date": {"type": "string", "description": "YYYY-MM-DD or empty"},
            "end_date": {"type": "string", "description": "YYYY-MM-DD or empty"},
        },
        "required": ["has_date"],
        "additionalProperties": False,
    },
}


def _prompt(query: str, today: datetime.date) -> str:
    return (
        f"Today is {today.isoformat()}. Read the question and extract the TIME "
        "PERIOD it asks about. If it references a date/month/period, has_date=true "
        "and give start_date/end_date (YYYY-MM-DD) that generously bound it: "
        "'late April 2026'->2026-04-20..2026-04-30; 'mid-May 2026'->2026-05-10.."
        "2026-05-20; 'April 2026'->2026-04-01..2026-04-30; 'around June 25 2026'->"
        "2026-06-22..2026-06-28. If no time reference, has_date=false.\n\n"
        f"Question: {query}"
    )


def _parse_iso(x: object) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(x).strip())
    except (ValueError, TypeError):
        return None


class DateWindowParser:
    """Parse a padded DateWindow from a query. Fail-open, budgeted, cached."""

    def __init__(self, llm_client, settings) -> None:
        self._client = llm_client
        self._settings = settings
        self._cache: dict[str, DateWindow | None] = {}
        self._budget_lock = asyncio.Lock()
        self._calls: list[float] = []  # unix timestamps within the trailing hour

    async def _within_budget(self) -> bool:
        cap = int(getattr(self._settings, "date_leg_max_per_hour", 500))
        now = time.monotonic()
        async with self._budget_lock:
            self._calls = [t for t in self._calls if now - t < 3600.0]
            if len(self._calls) >= cap:
                return False
            self._calls.append(now)
            return True

    async def parse(self, query: str, today: datetime.date) -> DateWindow | None:
        if not query or not has_temporal_signal(query):
            return None
        if query in self._cache:
            return self._cache[query]
        if not await self._within_budget():
            logger.warning("date-leg parser budget exhausted; failing open")
            return None
        window = await self._parse_llm(query, today)
        self._cache[query] = window
        return window

    async def _parse_llm(self, query: str, today: datetime.date) -> DateWindow | None:
        payload = {
            "model": self._settings.date_leg_model,
            "max_tokens": 256,
            "system": "",
            "tools": [_TOOL],
            "tool_choice": {"type": "tool", "name": "emit_window"},
            "messages": [{"role": "user", "content": _prompt(query, today)}],
        }
        try:
            resp = await asyncio.wait_for(
                self._client.call(payload),
                timeout=float(self._settings.date_leg_timeout_seconds),
            )
        except (asyncio.TimeoutError, Exception):  # fail-open on any error
            logger.warning("date-leg parse failed; failing open", exc_info=True)
            return None
        data: dict = {}
        for block in (getattr(resp, "content", None) or []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                data = block.get("input") or {}
                break
        if not data.get("has_date"):
            return None
        start, end = _parse_iso(data.get("start_date")), _parse_iso(data.get("end_date"))
        if start is None or end is None or start > end:
            return None
        pad = datetime.timedelta(days=int(getattr(self._settings, "date_leg_pad_days", 2)))
        return DateWindow(start=start - pad, end=end + pad)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/heart/test_date_window.py -v`
Expected: PASS (all parser + pre-gate tests).

- [ ] **Step 5: Commit**

```bash
git add nous/heart/date_window.py tests/heart/test_date_window.py
git commit -m "feat(heart): DateWindowParser (Haiku, budgeted, cached, fail-open) (F075 L3)"
```

---

## Task 4: `_date_window_leg` — in-window fact retrieval ranked by cosine

**Files:**
- Modify: `nous/heart/facts.py`
- Test: `tests/heart/test_date_window_leg.py`

**Interfaces:**
- Consumes: `DateWindow`, a query embedding `list[float]`, an `AsyncSession`.
- Produces: `FactStore._date_window_leg(session, embedding, window, limit) -> list[tuple[UUID, float]]` — in-window active dated facts ordered by cosine similarity to the embedding, score = cosine.

- [ ] **Step 1: Write the failing test**

```python
# tests/heart/test_date_window_leg.py
import datetime
import pytest
from nous.heart.date_window import DateWindow

@pytest.mark.asyncio
async def test_date_window_leg_returns_in_window_ranked(fact_store, seed_dated_facts):
    # seed_dated_facts inserts: F_in (event_date 2026-04-25, embedding ~query),
    # F_out (event_date 2026-06-01), F_undated (event_date NULL). Query embed ~F_in.
    window = DateWindow(start=datetime.date(2026, 4, 20), end=datetime.date(2026, 4, 30))
    async with fact_store.db.session() as s:
        emb = await fact_store.embeddings.embed("calibration work in late April")
        leg = await fact_store._date_window_leg(s, emb, window, limit=15)
    ids = [row[0] for row in leg]
    assert seed_dated_facts["F_in"] in ids
    assert seed_dated_facts["F_out"] not in ids      # out of window
    assert seed_dated_facts["F_undated"] not in ids  # no event_date
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/heart/test_date_window_leg.py::test_date_window_leg_returns_in_window_ranked -v`
Expected: FAIL — `AttributeError: 'FactStore' object has no attribute '_date_window_leg'`.

- [ ] **Step 3: Implement `_date_window_leg`**

In `nous/heart/facts.py`, add a method on `FactStore` (near `_search`):
```python
    async def _date_window_leg(
        self,
        session: "AsyncSession",
        embedding: list[float],
        window: "DateWindow",
        limit: int,
    ) -> list[tuple["UUID", float]]:
        """F075 L3: in-window active dated facts, ranked by cosine to the query.

        Returns (id, cosine) tuples ordered best-first. Ranking is cosine-to-query
        (relevance within the window), never date-distance alone.
        """
        qvec = "[" + ",".join(str(float(v)) for v in embedding) + "]"
        sql = text("""
            SELECT t.id, 1 - (t.embedding <=> CAST(:qvec AS vector)) AS score
            FROM heart.facts t
            WHERE t.agent_id = :agent_id
              AND t.active = true
              AND t.event_date IS NOT NULL
              AND t.event_date BETWEEN :lo AND :hi
              AND t.embedding IS NOT NULL
            ORDER BY t.embedding <=> CAST(:qvec AS vector)
            LIMIT :limit
        """)
        result = await session.execute(sql, {
            "agent_id": self.agent_id, "qvec": qvec,
            "lo": window.start, "hi": window.end, "limit": limit,
        })
        return [(row.id, float(row.score)) for row in result.all()]
```
Add the import at the top of `facts.py` if not present: `from nous.heart.date_window import DateWindow` (under TYPE_CHECKING is fine for the annotation; the method body doesn't construct one).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/heart/test_date_window_leg.py::test_date_window_leg_returns_in_window_ranked -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/heart/facts.py tests/heart/test_date_window_leg.py
git commit -m "feat(heart): _date_window_leg cosine-ranked in-window fact retrieval (F075 L3)"
```

---

## Task 5: Fuse the date leg into `_search` via `_rrf_merge_n`

**Files:**
- Modify: `nous/heart/facts.py` (`search`, `_search`)
- Test: `tests/heart/test_date_window_leg.py`

**Interfaces:**
- Consumes: `_date_window_leg`, `_rrf_merge_n`, `_resolve_rrf_k`.
- Produces: `FactStore.search(..., date_window: DateWindow | None = None)` and `_search(..., date_window=None)`. When `date_window` and `embedding` are present, the hybrid result list is fused with the date leg via `_rrf_merge_n`.

- [ ] **Step 1: Write the failing test**

```python
# tests/heart/test_date_window_leg.py  (add)
@pytest.mark.asyncio
async def test_date_window_fusion_rescues_missed_fact(fact_store, seed_rescue_case):
    # seed_rescue_case: a dated gold fact whose content does NOT lexically/semantically
    # match a time-framed query, so vanilla ranks it outside `limit`, but it IS in the
    # date window. Fusion must pull it into the returned top-`limit`.
    q = seed_rescue_case["query"]; window = seed_rescue_case["window"]; gold = seed_rescue_case["gold"]
    vanilla = await fact_store.search(q, limit=5)
    fused = await fact_store.search(q, limit=5, date_window=window)
    assert gold not in [f.id for f in vanilla]
    assert gold in [f.id for f in fused]

@pytest.mark.asyncio
async def test_no_window_is_unchanged(fact_store, seed_rescue_case):
    q = seed_rescue_case["query"]
    a = await fact_store.search(q, limit=5)
    b = await fact_store.search(q, limit=5, date_window=None)
    assert [f.id for f in a] == [f.id for f in b]  # None window == today's behavior
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/heart/test_date_window_leg.py -k fusion -v`
Expected: FAIL — `search()` got an unexpected keyword argument `date_window`.

- [ ] **Step 3: Thread `date_window` + fuse**

In `nous/heart/facts.py`:
1. Add `date_window: "DateWindow | None" = None` to the `search` signature and pass it into `_search`:
```python
    async def search(self, query, limit=10, category=None, active_only=True,
                     exclude_categories=None, session=None, variant_pairs=None,
                     date_window=None):
        if session is None:
            async with self.db.session() as session:
                return await self._search(query, limit, category, active_only,
                                          exclude_categories, session, variant_pairs, date_window)
        return await self._search(query, limit, category, active_only,
                                  exclude_categories, session, variant_pairs, date_window)
```
2. Add `date_window=None` to `_search`'s signature, and after `results` is computed (facts.py:1696, before `if not results:`), fuse the leg:
```python
        # F075 L3: fuse the date-window leg (position-based RRF). Present only when
        # the caller parsed a window and we have a query embedding. Empty leg is a
        # no-op, so this preserves today's ordering when the window finds nothing.
        if date_window is not None and embedding is not None:
            from nous.heart.search import _rrf_merge_n, _resolve_rrf_k
            date_leg = await self._date_window_leg(session, embedding, date_window, self.settings.date_leg_k)
            if date_leg:
                results = _rrf_merge_n([results, date_leg], _resolve_rrf_k(), limit)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/heart/test_date_window_leg.py -v`
Expected: PASS (fusion rescues the gold; None-window is unchanged).

- [ ] **Step 5: Commit**

```bash
git add nous/heart/facts.py tests/heart/test_date_window_leg.py
git commit -m "feat(heart): fuse date-window leg into fact search via _rrf_merge_n (F075 L3)"
```

---

## Task 6: Thread through `heart.recall` and wire the parser into `run_recall_pipeline`

**Files:**
- Modify: `nous/heart/heart.py` (`recall`, and the fact dispatch at ~:981)
- Modify: `nous/api/retrieval_pipeline.py` (parse the window, pass into `heart.recall`)
- Test: `tests/api/test_date_leg_pipeline.py`

**Interfaces:**
- Consumes: `DateWindowParser`, `Heart.recall`, `run_recall_pipeline`.
- Produces: `Heart.recall(..., date_window: DateWindow | None = None)` passes `date_window` to `self.facts.search`. `run_recall_pipeline` parses the window once when `settings.date_leg_enabled` and threads it in.

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_date_leg_pipeline.py
import datetime
import pytest

@pytest.mark.asyncio
async def test_pipeline_flag_off_is_byte_identical(pipeline_env):
    # date_leg_enabled=False -> no parse, no leg, identical result ids
    off = pipeline_env.settings(date_leg_enabled=False)
    r1, _ = await pipeline_env.run("what changed in late April 2026?", off)
    r2, _ = await pipeline_env.run_baseline("what changed in late April 2026?")
    assert [x.id for x in r1] == [x.id for x in r2]

@pytest.mark.asyncio
async def test_pipeline_flag_on_parses_and_passes_window(pipeline_env, monkeypatch):
    captured = {}
    async def fake_recall(query, *a, date_window=None, **k):
        captured["window"] = date_window
        return []
    monkeypatch.setattr(pipeline_env.heart, "recall", fake_recall)
    on = pipeline_env.settings(date_leg_enabled=True)
    await pipeline_env.run("what happened in late April 2026?", on)
    assert isinstance(captured["window"], object) and captured["window"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/api/test_date_leg_pipeline.py -v`
Expected: FAIL — `recall()` got an unexpected keyword `date_window` (and pipeline doesn't parse).

- [ ] **Step 3: Thread `date_window` through `Heart.recall`**

In `nous/heart/heart.py`, add `date_window: "DateWindow | None" = None` to `recall`'s signature (heart.py:835) and pass it to the fact branch (heart.py:981):
```python
                elif memory_type == "fact":
                    result = await self.facts.search(
                        query, fetch_limit, session=session,
                        variant_pairs=variant_pairs, date_window=date_window,
                    )
```

- [ ] **Step 4: Parse + pass in `run_recall_pipeline`**

In `nous/api/retrieval_pipeline.py`, near the top of `run_recall_pipeline` (before stage execution), parse the window once and thread it into `heart.recall`. The parser needs an LLM client and settings; construct/reuse the background LLM client already available to the pipeline (mirror how contradiction detection obtains its Haiku client). Add:
```python
    date_window = None
    if getattr(settings, "date_leg_enabled", False):
        try:
            from nous.heart.date_window import DateWindowParser
            import datetime as _dt
            parser = DateWindowParser(_get_background_llm_client(settings), settings)
            date_window = await parser.parse(query, _dt.date.today())
        except Exception:
            logger.warning("date-leg parse errored in pipeline; continuing without", exc_info=True)
            date_window = None
```
Then, at every `heart.recall(...)` call inside `_run_stages` for the fact path, pass `date_window=date_window`. (Thread `date_window` as a param of `_run_stages`, defaulting `None`, and forward it to `heart.recall`.)

> Implementation note: `_get_background_llm_client` is the existing helper the pipeline/heart uses for Haiku calls (contradiction detection at `retrieval_pipeline.py` uses one — reuse that exact accessor; do not create a second client). If the pipeline holds the client differently, pass it in from the caller rather than constructing a new one.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/api/test_date_leg_pipeline.py -v`
Expected: PASS (flag off = identical; flag on = window parsed + passed).

- [ ] **Step 6: Run the recall snapshot test**

Run: `uv run pytest tests/api/ -k "recall and snapshot" -v`
Expected: PASS — `date_leg_enabled=False` default keeps `recall_deep` output byte-identical.

- [ ] **Step 7: Commit**

```bash
git add nous/heart/heart.py nous/api/retrieval_pipeline.py tests/api/test_date_leg_pipeline.py
git commit -m "feat(api): wire date-window parser into run_recall_pipeline, flag-gated (F075 L3)"
```

---

## Task 7: Rescue-metric dev harness

**Files:**
- Create: `nous_eval/date_leg_rescue.py`
- Test: `tests/nous_eval/test_date_leg_rescue.py`

**Interfaces:**
- Produces: `python -m nous_eval.date_leg_rescue --sample N` — samples dated facts, generates time-framed queries, compares vanilla vs vanilla+leg top-K, prints `rescued` / `lost` / `fused_top_k` counts. This is the dev instrument behind the A/B gate; it is NOT shipped in the prod image (lives under `nous_eval/`).

- [ ] **Step 1: Write the failing test**

```python
# tests/nous_eval/test_date_leg_rescue.py
from nous_eval.date_leg_rescue import rrf_fuse

def test_rrf_fuse_is_position_based():
    # gold at rank 3 in vanilla, rank 1 in the leg -> fusion lifts it
    vanilla = ["a", "b", "gold", "c"]
    leg = ["gold", "x"]
    fused = rrf_fuse(vanilla, leg)
    assert fused.index("gold") < 2  # lifted into the head
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/nous_eval/test_date_leg_rescue.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the harness**

Create `nous_eval/date_leg_rescue.py` with a pure `rrf_fuse(vanilla_ids, leg_ids, k=60)` (position-based RRF over two id lists, returns fused id list) plus an `async def main()` that reuses `_settings_for_eval_db`, `_build_heart_for_eval`, `DateWindowParser`, and `_date_window_leg` to run the rescue comparison over a sample of dated facts and print `n / vanilla_top_k / fused_top_k / rescued / lost`. (The `rrf_fuse` body is the same 8-line function validated in the 2026-07-01 probe.)
```python
def rrf_fuse(vanilla_ids, leg_ids, k=60):
    score = {}
    for rank, i in enumerate(vanilla_ids, 1):
        score[i] = score.get(i, 0.0) + 1.0 / (k + rank)
    for rank, i in enumerate(leg_ids, 1):
        score[i] = score.get(i, 0.0) + 1.0 / (k + rank)
    return [i for i, _ in sorted(score.items(), key=lambda kv: kv[1], reverse=True)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/nous_eval/test_date_leg_rescue.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nous_eval/date_leg_rescue.py tests/nous_eval/test_date_leg_rescue.py
git commit -m "feat(eval): date-leg rescue-metric dev harness (F075 L3)"
```

---

## Post-implementation: A/B gate (not a code task — operator step)

Run before flipping `NOUS_DATE_LEG_ENABLED=true` in prod:
1. `python -m nous_eval.date_leg_rescue --sample 40` on `:5433/nous_eval_prod` → assert `rescued > 0`, `lost == 0`.
2. LongMemEval temporal-subset A/B (Opus generator, one variable = the flag), guardrail = no regression on non-temporal sources. Gate on temporal uplift + zero non-temporal regression. Record the numbers before flip.

---

## Self-Review

**Spec coverage:**
- §3.1 DateWindowParser → Tasks 2 (pre-gate) + 3 (Haiku/budget/cache/fail-open) ✅
- §3.2 date-window fact retrieval → Task 4 ✅
- §3.3 RRF fusion (Nth leg) → Task 5 ✅
- §3.4 wiring point → Task 6 ✅
- §5 config flags → Task 1 ✅
- §6 zero-regression → Task 5 (`None`-window unchanged) + Task 6 (flag-off byte-identical) ✅
- §7 measurement → Task 7 + the operator A/B step ✅
- §2 bounds/non-goals → respected (facts-only, no coverage work, no Approach A).

**Placeholder scan:** the only deferred detail is `_get_background_llm_client` in Task 6 Step 4 — flagged explicitly as "reuse the existing accessor the pipeline already uses for contradiction-detection Haiku calls; do not create a second client," which is a concrete instruction, not a TBD. The implementer must confirm the exact accessor name when they open `retrieval_pipeline.py`.

**Type consistency:** `DateWindow(start, end)` used identically in Tasks 2–6; `_date_window_leg -> list[tuple[UUID, float]]` matches `_rrf_merge_n`'s `ranked_lists` element type; `date_window` param name consistent across `search`/`_search`/`recall`/`_run_stages`.
