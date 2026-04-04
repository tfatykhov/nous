# F025 Amnesia Prevention Phase 2+3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 5 remaining root causes of structural amnesia (RC-1, RC-4, RC-5, RC-6, RC-7) and implement 3 structural improvements for long-term memory quality.

**Architecture:** Each fix targets a specific point in the retrieval/extraction pipeline. P2 items are config+code tweaks (1-40 lines each). P3 items add per-type staleness config, chunked summarization, and transcript persistence via DB migration. All changes are independently deployable and testable.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 async, pydantic-settings, PostgreSQL 17, pytest + pytest-asyncio

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `nous/config.py` | Modify | Add 4 new config fields: `staleness_exempt_types`, `transcript_max_chars`, `fact_dedup_threshold`, `transcript_persistence_enabled` |
| `nous/cognitive/context.py` | Modify | P2-A: type-aware staleness skip. P2-B: scale user_profile budget |
| `nous/handlers/episode_summarizer.py` | Modify | P2-C: configurable transcript limit. P3-B: chunked summarization |
| `nous/handlers/fact_extractor.py` | Modify | P2-D: configurable dedup threshold. P2-E: pass source_text to FactInput |
| `nous/heart/schemas.py` | Modify | P2-E: add `source_text` field to FactInput |
| `nous/heart/facts.py` | Modify | P2-E: use FactInput.source_text as override in `_get_source_text` |
| `nous/storage/models.py` | Modify | P3-C: add `transcript` column to Episode model |
| `nous/heart/episodes.py` | Modify | P3-C: populate transcript on episode close |
| `sql/migrations/017_add_episode_transcript.sql` | Create | P3-C: migration adding transcript column |
| `tests/test_f025_amnesia_prevention.py` | Create | All tests for P2+P3 changes |

---

### Task 1: P2-A — Per-Type Staleness Exemption for Facts (RC-1)

**Files:**
- Modify: `nous/config.py:62-64`
- Modify: `nous/cognitive/context.py:720-746` (`_apply_staleness_penalty`)
- Test: `tests/test_f025_amnesia_prevention.py`

**Context:** The staleness penalty applies `0.5^(age/half_life)` decay to all memory types. Facts like "Tim lives in Silver Spring" shouldn't decay. Current code exempts categories `rule`, `preference`, `technical`, `concept` but NOT `person` — and the exemption is category-based, not type-based. The spec wants facts entirely exempt from staleness.

- [ ] **Step 1: Write failing tests for type-aware staleness**

```python
# tests/test_f025_amnesia_prevention.py
"""Tests for F025 Amnesia Prevention Phase 2+3."""

from __future__ import annotations

import pytest
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from nous.config import Settings
from nous.cognitive.context import ContextEngine


@dataclass
class MockMemoryItem:
    """Minimal mock for memory items passed through staleness pipeline."""
    score: float | None = 0.8
    created_at: datetime | None = None
    category: str = ""
    _type: str = "fact"  # memory type tag for type-aware staleness


def _make_engine(staleness_enabled: bool = True, half_life: int = 20, exempt_types: str = "fact") -> ContextEngine:
    """Create a minimal ContextEngine with staleness settings."""
    settings = Settings(
        staleness_penalty_enabled=staleness_enabled,
        staleness_half_life_days=half_life,
        staleness_exempt_types=exempt_types,
        openai_api_key="test",
        anthropic_api_key="test",
    )
    engine = ContextEngine.__new__(ContextEngine)
    engine._settings = settings
    return engine


class TestStalenessExemptTypes:
    """P2-A: Facts should be exempt from staleness penalty."""

    def test_fact_type_exempt_from_staleness(self):
        """Old facts tagged as type=fact should keep original score."""
        engine = _make_engine(exempt_types="fact")
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, _type="fact")]
        result = engine._apply_staleness_penalty(items, memory_type="fact")
        assert result[0].score == 0.8  # unchanged

    def test_decision_type_still_decayed(self):
        """Old decisions should still get staleness penalty."""
        engine = _make_engine(exempt_types="fact")
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, _type="decision")]
        result = engine._apply_staleness_penalty(items, memory_type="decision")
        assert result[0].score < 0.8  # decayed

    def test_multiple_exempt_types(self):
        """Can exempt multiple types via comma-separated config."""
        engine = _make_engine(exempt_types="fact,procedure")
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        fact = MockMemoryItem(score=0.8, created_at=old_date, _type="fact")
        proc = MockMemoryItem(score=0.8, created_at=old_date, _type="procedure")
        dec = MockMemoryItem(score=0.8, created_at=old_date, _type="decision")

        facts_result = engine._apply_staleness_penalty([fact], memory_type="fact")
        procs_result = engine._apply_staleness_penalty([proc], memory_type="procedure")
        decs_result = engine._apply_staleness_penalty([dec], memory_type="decision")

        assert facts_result[0].score == 0.8
        assert procs_result[0].score == 0.8
        assert decs_result[0].score < 0.8

    def test_empty_exempt_types_decays_all(self):
        """Empty exempt_types means all types get staleness."""
        engine = _make_engine(exempt_types="")
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, _type="fact")]
        result = engine._apply_staleness_penalty(items, memory_type="fact")
        assert result[0].score < 0.8

    def test_category_exemption_still_works(self):
        """Existing category exemptions (rule, preference, etc.) still apply even for non-exempt types."""
        engine = _make_engine(exempt_types="")  # no type exemptions
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="rule", _type="decision")]
        result = engine._apply_staleness_penalty(items, memory_type="decision")
        assert result[0].score == 0.8  # category exemption still works
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py -v -x`
Expected: FAIL — `_apply_staleness_penalty` doesn't accept `memory_type` parameter

- [ ] **Step 3: Add `staleness_exempt_types` config field**

In `nous/config.py`, after line 64 (`staleness_half_life_days`), add:

```python
    staleness_exempt_types: str = "fact"  # F025: comma-separated types exempt from staleness (fact,procedure,etc.)
```

- [ ] **Step 4: Modify `_apply_staleness_penalty` to accept memory_type**

In `nous/cognitive/context.py`, change the method signature and add type check:

```python
    def _apply_staleness_penalty(self, results: list, memory_type: str = "") -> list:
        """Apply time-decay penalty to relevance scores (F017 Phase 5).

        Args:
            results: Memory items with score and created_at attributes.
            memory_type: The memory type (fact, decision, procedure, episode).
                         If in staleness_exempt_types config, skip decay entirely.
        """
        if not self._settings.staleness_penalty_enabled:
            return results
        # F025 P2-A: Type-level exemption (e.g. facts don't decay)
        exempt_types = {t.strip() for t in self._settings.staleness_exempt_types.split(",") if t.strip()}
        if memory_type in exempt_types:
            return results
        half_life = self._settings.staleness_half_life_days
        now = datetime.now(timezone.utc)
        adjusted = []
        for r in results:
            score = getattr(r, "score", None)
            if score is None:
                adjusted.append(r)
                continue
            created = getattr(r, "created_at", None)
            if not created:
                adjusted.append(r)
                continue
            category = getattr(r, "category", "")
            if category in {"rule", "preference", "technical", "concept"}:
                adjusted.append(r)
                continue
            age_days = (now - created).days
            if age_days > 0:
                decay = 0.5 ** (age_days / half_life)
                adjusted.append(_wrap_with_score(r, score * max(decay, 0.3)))
            else:
                adjusted.append(r)
        return adjusted
```

- [ ] **Step 5: Update all 4 call sites to pass memory_type**

In `nous/cognitive/context.py`, update each call:

Line ~337 (decisions):
```python
    decisions = self._apply_staleness_penalty(decisions, memory_type="decision")
```

Line ~383 (facts):
```python
    facts = self._apply_staleness_penalty(facts, memory_type="fact")
```

Line ~475 (procedures):
```python
    embedding_procedures = self._apply_staleness_penalty(embedding_procedures, memory_type="procedure")
```

Line ~571 (episodes):
```python
    episodes = self._apply_staleness_penalty(episodes, memory_type="episode")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py::TestStalenessExemptTypes -v`
Expected: All 5 tests PASS

- [ ] **Step 7: Run existing context tests for regression**

Run: `uv run pytest tests/test_context.py tests/test_context_quality.py tests/test_context_smart.py -v --timeout=60`
Expected: All existing tests PASS (existing calls without memory_type default to "" which isn't exempt)

- [ ] **Step 8: Commit**

```bash
git add nous/config.py nous/cognitive/context.py tests/test_f025_amnesia_prevention.py
git commit -m "feat(f025): P2-A per-type staleness exemption — facts exempt from decay"
```

---

### Task 2: P2-B — Scale User Profile Budget (RC-4)

**Files:**
- Modify: `nous/cognitive/context.py:249` (user_profile budget usage)
- Test: `tests/test_f025_amnesia_prevention.py`

**Context:** `user_profile` budget is 200 tokens, used raw without `_scaled_budget()`. Every other dynamic section (decisions, facts, procedures, episodes) passes through scaling. At 700K context window, other sections get 2.5x but user_profile stays at 200.

- [ ] **Step 1: Write failing test for user_profile scaling**

Append to `tests/test_f025_amnesia_prevention.py`:

```python
class TestUserProfileBudgetScaling:
    """P2-B: user_profile budget should pass through _scaled_budget."""

    def test_scaled_budget_applied_to_user_profile(self):
        """user_profile=200 should scale to 500 at 1M context window."""
        engine = ContextEngine.__new__(ContextEngine)
        settings = Settings(
            budget_scale_enabled=True,
            context_window=1_000_000,
            openai_api_key="test",
            anthropic_api_key="test",
        )
        engine._settings = settings
        # _scaled_budget at 1M window = 2.5x
        assert engine._scaled_budget(200) == 500

    def test_user_profile_budget_not_raw_200(self):
        """Verify the context assembly uses scaled budget, not raw 200."""
        # This is a code-reading verification test.
        # After the fix, context.py line ~249 should call _scaled_budget.
        import inspect
        from nous.cognitive.context import ContextEngine
        source = inspect.getsource(ContextEngine._assemble_user_profile)
        assert "_scaled_budget" in source or "scaled_budget" in source
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py::TestUserProfileBudgetScaling -v -x`
Expected: Second test FAILS — `_assemble_user_profile` doesn't exist as separate method, and source doesn't contain `_scaled_budget`

- [ ] **Step 3: Apply the one-line fix**

In `nous/cognitive/context.py`, find the line (around L249):
```python
            profile_text = self._truncate_to_budget(profile_text, budget.user_profile)
```

Replace with:
```python
            profile_text = self._truncate_to_budget(profile_text, self._scaled_budget(budget.user_profile))
```

- [ ] **Step 4: Update the test to check actual source**

Since there's no separate `_assemble_user_profile` method, update the test to check the `prepare` method source:

```python
    def test_user_profile_budget_scaled_in_source(self):
        """Verify context.py applies _scaled_budget to user_profile."""
        import inspect
        from nous.cognitive.context import ContextEngine
        source = inspect.getsource(ContextEngine.prepare)
        # Find the user_profile truncation — should use _scaled_budget
        assert "_scaled_budget(budget.user_profile)" in source
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py::TestUserProfileBudgetScaling -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nous/cognitive/context.py tests/test_f025_amnesia_prevention.py
git commit -m "feat(f025): P2-B scale user_profile budget through _scaled_budget()"
```

---

### Task 3: P2-C — Configurable Transcript Truncation Limit (RC-5)

**Files:**
- Modify: `nous/config.py`
- Modify: `nous/handlers/episode_summarizer.py:183,205`
- Test: `tests/test_f025_amnesia_prevention.py`

**Context:** `_truncate_transcript` has hardcoded `max_chars=8000`. Long technical sessions lose 60-80% of content before summarization. Raise default to 16000 and make configurable.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_f025_amnesia_prevention.py`:

```python
class TestTranscriptTruncation:
    """P2-C: Configurable transcript truncation limit."""

    def test_default_max_chars_is_16000(self):
        """Default transcript limit should be 16000, not 8000."""
        settings = Settings(
            openai_api_key="test",
            anthropic_api_key="test",
        )
        assert settings.transcript_max_chars == 16000

    def test_configurable_via_env(self):
        """Should be settable via NOUS_TRANSCRIPT_MAX_CHARS."""
        settings = Settings(
            transcript_max_chars=24000,
            openai_api_key="test",
            anthropic_api_key="test",
        )
        assert settings.transcript_max_chars == 24000

    def test_truncation_respects_config(self):
        """_truncate_transcript should use configured limit."""
        from nous.handlers.episode_summarizer import EpisodeSummarizer

        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        # 20K chars of text (alternating turns)
        turns = [f"User: Question {i}\n\nAssistant: Answer {i} with details" for i in range(200)]
        transcript = "\n\n".join(turns)
        assert len(transcript) > 16000

        # With limit=16000, result should be <= 16000
        result = summarizer._truncate_transcript(transcript, max_chars=16000)
        assert len(result) <= 16000

        # With limit=8000, result should be <= 8000 (old behavior still works)
        result_old = summarizer._truncate_transcript(transcript, max_chars=8000)
        assert len(result_old) <= 8000

    def test_short_transcripts_unchanged(self):
        """Transcripts under limit should pass through unchanged."""
        from nous.handlers.episode_summarizer import EpisodeSummarizer

        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        transcript = "User: Hello\n\nAssistant: Hi there"
        result = summarizer._truncate_transcript(transcript, max_chars=16000)
        assert result == transcript
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py::TestTranscriptTruncation -v -x`
Expected: FAIL — `transcript_max_chars` field doesn't exist on Settings

- [ ] **Step 3: Add config field**

In `nous/config.py`, after the `staleness_exempt_types` field (added in Task 1), add:

```python
    # F025 P2-C: Transcript truncation limit for episode summarization
    transcript_max_chars: int = 16000
```

- [ ] **Step 4: Wire config into EpisodeSummarizer**

In `nous/handlers/episode_summarizer.py`, find the call to `_truncate_transcript` (line ~183):
```python
        transcript = self._truncate_transcript(transcript)
```

Replace with:
```python
        transcript = self._truncate_transcript(transcript, max_chars=self._settings.transcript_max_chars)
```

Verify `self._settings` is already available on the summarizer instance (it should be — check `__init__`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py::TestTranscriptTruncation -v`
Expected: All 4 tests PASS

- [ ] **Step 6: Commit**

```bash
git add nous/config.py nous/handlers/episode_summarizer.py tests/test_f025_amnesia_prevention.py
git commit -m "feat(f025): P2-C configurable transcript limit, raise default 8000→16000"
```

---

### Task 4: P2-D — Relax Fact Extractor Dedup Threshold (RC-6)

**Files:**
- Modify: `nous/config.py`
- Modify: `nous/handlers/fact_extractor.py:119,170`
- Test: `tests/test_f025_amnesia_prevention.py`

**Context:** Fact extractor dedup threshold at 0.85 blocks updated values (price changes, version bumps) before the smarter supersession logic at 0.95 in `facts.py` can evaluate them. Raise to 0.92.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_f025_amnesia_prevention.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch


@dataclass
class MockSearchResult:
    """Mock for heart.search_facts results."""
    score: float | None = None
    content: str = "existing fact"


class TestFactDedupThreshold:
    """P2-D: Configurable fact extractor dedup threshold."""

    def test_default_threshold_is_092(self):
        """Default dedup threshold should be 0.92."""
        settings = Settings(
            openai_api_key="test",
            anthropic_api_key="test",
        )
        assert settings.fact_dedup_threshold == 0.92

    def test_configurable_via_env(self):
        """Should be settable via NOUS_FACT_DEDUP_THRESHOLD."""
        settings = Settings(
            fact_dedup_threshold=0.88,
            openai_api_key="test",
            anthropic_api_key="test",
        )
        assert settings.fact_dedup_threshold == 0.88

    @pytest.mark.asyncio
    async def test_fact_at_088_not_blocked_at_092(self):
        """A fact with 0.88 similarity should pass when threshold is 0.92."""
        from nous.handlers.fact_extractor import FactExtractor
        from nous.events import Event

        heart = MagicMock()
        heart.search_facts = AsyncMock(return_value=[MockSearchResult(score=0.88)])
        heart.learn = AsyncMock(return_value=MagicMock(spec=["id", "content"]))

        settings = Settings(
            fact_dedup_threshold=0.92,
            openai_api_key="test",
            anthropic_api_key="test",
        )
        bus = MagicMock()
        bus.on = MagicMock()

        extractor = FactExtractor(heart, settings, bus)

        event = Event(
            name="episode_summarized",
            data={
                "summary": {"summary": "Test summary", "key_points": ["point1"]},
                "episode_id": "test-ep",
                "candidate_facts": [{"content": "New version is 2.0", "subject": "version", "category": "technical"}],
            },
        )

        await extractor.handle(event)
        # Fact at 0.88 < threshold 0.92 → should be stored
        heart.learn.assert_called_once()

    @pytest.mark.asyncio
    async def test_fact_at_095_blocked_at_092(self):
        """A fact with 0.95 similarity should be blocked when threshold is 0.92."""
        from nous.handlers.fact_extractor import FactExtractor
        from nous.events import Event

        heart = MagicMock()
        heart.search_facts = AsyncMock(return_value=[MockSearchResult(score=0.95)])
        heart.learn = AsyncMock()

        settings = Settings(
            fact_dedup_threshold=0.92,
            openai_api_key="test",
            anthropic_api_key="test",
        )
        bus = MagicMock()
        bus.on = MagicMock()

        extractor = FactExtractor(heart, settings, bus)

        event = Event(
            name="episode_summarized",
            data={
                "summary": {},
                "episode_id": "test-ep",
                "candidate_facts": [{"content": "Same old fact", "subject": "test", "category": "technical"}],
            },
        )

        await extractor.handle(event)
        # Fact at 0.95 > threshold 0.92 → should be blocked
        heart.learn.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py::TestFactDedupThreshold -v -x`
Expected: FAIL — `fact_dedup_threshold` field doesn't exist on Settings

- [ ] **Step 3: Add config field**

In `nous/config.py`, after `transcript_max_chars` (added in Task 3), add:

```python
    # F025 P2-D: Fact extractor dedup threshold (raised from 0.85)
    fact_dedup_threshold: float = 0.92
```

- [ ] **Step 4: Wire config into FactExtractor**

In `nous/handlers/fact_extractor.py`, replace both hardcoded `0.85` thresholds:

Line ~119 (LLM extraction path):
```python
                if existing and existing[0].score is not None and existing[0].score > self._settings.fact_dedup_threshold:
```

Line ~170 (candidate facts path):
```python
            if existing and existing[0].score is not None and existing[0].score > self._settings.fact_dedup_threshold:
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py::TestFactDedupThreshold -v`
Expected: All 4 tests PASS

- [ ] **Step 6: Commit**

```bash
git add nous/config.py nous/handlers/fact_extractor.py tests/test_f025_amnesia_prevention.py
git commit -m "feat(f025): P2-D configurable fact dedup threshold, raise 0.85→0.92"
```

---

### Task 5: P2-E — Source Text Passthrough for Admission Grounding (RC-7)

**Files:**
- Modify: `nous/heart/schemas.py:81-93` (FactInput)
- Modify: `nous/heart/facts.py:483-500` (`_get_source_text`)
- Modify: `nous/handlers/fact_extractor.py` (pass transcript text)
- Modify: `nous/handlers/episode_summarizer.py` (include transcript in event data)
- Test: `tests/test_f025_amnesia_prevention.py`

**Context:** `_get_source_text` returns `episode.summary` instead of the original transcript. Facts extracted from truncated portions score low on ROUGE-L grounding and get rejected by admission. Fix: add `source_text` to `FactInput` so the extractor can pass the transcript directly; `_get_source_text` uses it as override.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_f025_amnesia_prevention.py`:

```python
class TestSourceTextPassthrough:
    """P2-E: Fact admission should ground against transcript, not summary."""

    def test_fact_input_has_source_text_field(self):
        """FactInput should accept optional source_text."""
        from nous.heart.schemas import FactInput
        inp = FactInput(
            content="Tim uses Python",
            subject="Tim",
            source_text="User: I mainly use Python for everything\n\nAssistant: Got it.",
        )
        assert inp.source_text == "User: I mainly use Python for everything\n\nAssistant: Got it."

    def test_fact_input_source_text_defaults_none(self):
        """source_text should default to None for backward compatibility."""
        from nous.heart.schemas import FactInput
        inp = FactInput(content="test", subject="test")
        assert inp.source_text is None

    @pytest.mark.asyncio
    async def test_get_source_text_prefers_source_text_field(self):
        """When FactInput.source_text is set, use it instead of DB lookup."""
        from nous.heart.facts import FactManager

        manager = FactManager.__new__(FactManager)

        inp_with_text = MagicMock()
        inp_with_text.source_text = "the original transcript text"
        inp_with_text.source_episode_id = None

        session = AsyncMock()
        result = await manager._get_source_text(inp_with_text, session)
        assert result == "the original transcript text"
        # Should NOT query the database
        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_source_text_falls_back_to_episode(self):
        """When source_text is None, fall back to episode.summary lookup."""
        from nous.heart.facts import FactManager
        from uuid import uuid4

        manager = FactManager.__new__(FactManager)

        inp_without_text = MagicMock()
        inp_without_text.source_text = None
        inp_without_text.source_episode_id = uuid4()

        mock_episode = MagicMock()
        mock_episode.summary = "episode summary text"

        session = AsyncMock()
        session.get = AsyncMock(return_value=mock_episode)

        result = await manager._get_source_text(inp_without_text, session)
        assert result == "episode summary text"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py::TestSourceTextPassthrough -v -x`
Expected: FAIL — `source_text` field doesn't exist on FactInput

- [ ] **Step 3: Add source_text to FactInput**

In `nous/heart/schemas.py`, after line 93 (`source_timestamp`), add:

```python
    source_text: str | None = None  # F025 P2-E: original transcript for admission grounding (not persisted)
```

- [ ] **Step 4: Update `_get_source_text` to use source_text override**

In `nous/heart/facts.py`, replace the `_get_source_text` method (lines 483-500):

```python
    async def _get_source_text(
        self,
        fact_input: FactInput,
        session: AsyncSession,
    ) -> str | None:
        """Retrieve source text for ROUGE-L grounding check.

        Prefers fact_input.source_text (F025 P2-E: transcript passthrough)
        over episode.summary DB lookup. This avoids grounding against the
        lossy summary when the original transcript is available.
        """
        # F025 P2-E: Use passed-through transcript if available
        if fact_input.source_text:
            return fact_input.source_text

        if not fact_input.source_episode_id:
            return None

        episode = await session.get(Episode, fact_input.source_episode_id)
        if episode and episode.summary:
            return episode.summary

        return None
```

- [ ] **Step 5: Thread transcript through episode_summarizer → fact_extractor**

In `nous/handlers/episode_summarizer.py`, find where the `episode_summarized` event is emitted (search for `emit` or `bus.emit`). Add `transcript` to the event data:

```python
    # In the summarize method, after generating summary, include transcript in event:
    await self._bus.emit(Event(
        name="episode_summarized",
        data={
            "episode_id": episode_id,
            "summary": summary,
            "candidate_facts": summary.get("candidate_facts", []),
            "transcript": transcript,  # F025 P2-E: pass for fact grounding
        },
    ))
```

In `nous/handlers/fact_extractor.py`, in `handle()` method, extract transcript and pass as source_text to FactInput. Update the `_store_candidate_facts` signature to accept transcript:

In `handle()` (around line 87):
```python
        transcript = event.data.get("transcript")
```

In the LLM extraction path (around line 124), update FactInput creation:
```python
                fact_input = FactInput(
                    subject=fact.get("subject", "unknown"),
                    content=content,
                    source="fact_extractor",
                    confidence=confidence,
                    category=fact.get("category"),
                    source_text=transcript,  # F025 P2-E
                )
```

In `_store_candidate_facts`, add `transcript` parameter and pass to FactInput:

```python
    async def _store_candidate_facts(self, candidates: list[str | dict], episode_id: str, transcript: str | None = None) -> None:
```

Update the FactInput in `_store_candidate_facts`:
```python
            fact_input = FactInput(
                content=content,
                subject=subject or "unknown",
                category=category,
                source="episode_summarizer",
                confidence=0.8,
                source_text=transcript,  # F025 P2-E
            )
```

Update the call to `_store_candidate_facts` in `handle()`:
```python
                await self._store_candidate_facts(
                    candidate_facts, event.data.get("episode_id", "?"), transcript=transcript
                )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py::TestSourceTextPassthrough -v`
Expected: All 4 tests PASS

- [ ] **Step 7: Run existing fact tests for regression**

Run: `uv run pytest tests/test_facts.py -v --timeout=60`
Expected: All existing tests PASS

- [ ] **Step 8: Commit**

```bash
git add nous/heart/schemas.py nous/heart/facts.py nous/handlers/episode_summarizer.py nous/handlers/fact_extractor.py tests/test_f025_amnesia_prevention.py
git commit -m "feat(f025): P2-E source text passthrough for admission grounding"
```

---

### Task 6: P3-A — Per-Type Staleness Configuration (Full)

**Files:**
- Modify: `nous/cognitive/context.py` (already done in Task 1 — this is verification + docs)
- Test: `tests/test_f025_amnesia_prevention.py`

**Context:** Task 1 already implemented the per-type staleness via `staleness_exempt_types` config. P3-A extends this with a test for the `NOUS_STALENESS_TYPES` alternative pattern from the spec. Since the comma-separated exempt types approach is already more flexible, P3-A is essentially complete. Add edge case tests.

- [ ] **Step 1: Write edge case tests**

Append to `tests/test_f025_amnesia_prevention.py`:

```python
class TestStalenessConfigEdgeCases:
    """P3-A: Additional staleness configuration tests."""

    def test_staleness_disabled_skips_all(self):
        """When staleness_penalty_enabled=False, no decay applied regardless of type."""
        engine = _make_engine(staleness_enabled=False, exempt_types="")
        old_date = datetime.now(timezone.utc) - timedelta(days=100)
        items = [MockMemoryItem(score=0.8, created_at=old_date, _type="decision")]
        result = engine._apply_staleness_penalty(items, memory_type="decision")
        assert result[0].score == 0.8

    def test_staleness_minimum_floor_030(self):
        """Decay should never go below 0.3 floor."""
        engine = _make_engine(exempt_types="")
        very_old = datetime.now(timezone.utc) - timedelta(days=365)
        items = [MockMemoryItem(score=1.0, created_at=very_old, category="person", _type="decision")]
        result = engine._apply_staleness_penalty(items, memory_type="decision")
        # person category NOT exempt, very old → hits 0.3 floor
        assert result[0].score == pytest.approx(0.3, abs=0.01)

    def test_no_score_items_pass_through(self):
        """Items without score attribute pass through unchanged."""
        engine = _make_engine(exempt_types="")
        items = [MockMemoryItem(score=None, created_at=datetime.now(timezone.utc), _type="fact")]
        result = engine._apply_staleness_penalty(items, memory_type="fact")
        assert result[0].score is None

    def test_exempt_types_whitespace_handling(self):
        """Whitespace in exempt_types should be trimmed."""
        engine = _make_engine(exempt_types=" fact , procedure ")
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, _type="fact")]
        result = engine._apply_staleness_penalty(items, memory_type="fact")
        assert result[0].score == 0.8
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py::TestStalenessConfigEdgeCases -v`
Expected: All 4 tests PASS (these test existing behavior from Task 1)

- [ ] **Step 3: Commit**

```bash
git add tests/test_f025_amnesia_prevention.py
git commit -m "test(f025): P3-A staleness config edge case tests"
```

---

### Task 7: P3-B — Chunked Summarization for Long Episodes

**Files:**
- Modify: `nous/handlers/episode_summarizer.py`
- Test: `tests/test_f025_amnesia_prevention.py`

**Context:** Even with 16K limit (P2-C), very long sessions still lose content. Split transcripts >16K into chunks, summarize each independently, then merge summaries.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_f025_amnesia_prevention.py`:

```python
class TestChunkedSummarization:
    """P3-B: Chunked summarization for long episodes."""

    def test_chunk_transcript_short_returns_single(self):
        """Short transcript returns a single chunk."""
        from nous.handlers.episode_summarizer import EpisodeSummarizer
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        transcript = "User: Hello\n\nAssistant: Hi"
        chunks = summarizer._chunk_transcript(transcript, max_chars=16000)
        assert len(chunks) == 1
        assert chunks[0] == transcript

    def test_chunk_transcript_long_splits_on_turn_boundaries(self):
        """Long transcript splits into multiple chunks at turn boundaries."""
        from nous.handlers.episode_summarizer import EpisodeSummarizer
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        # Build transcript with 20 turns of ~1000 chars each = ~20K total
        turns = [f"User: Question {i}\n\nAssistant: {'x' * 950}" for i in range(20)]
        transcript = "\n\n".join(turns)
        assert len(transcript) > 16000

        chunks = summarizer._chunk_transcript(transcript, max_chars=16000)
        assert len(chunks) >= 2
        # Each chunk should be within limit
        for chunk in chunks:
            assert len(chunk) <= 16000
        # All content should be preserved (no loss)
        reconstructed = "\n\n".join(chunks)
        # Turns from original should appear in reconstructed
        assert "Question 0" in reconstructed
        assert "Question 19" in reconstructed

    def test_chunk_transcript_preserves_turn_integrity(self):
        """Chunks should not split in the middle of a turn."""
        from nous.handlers.episode_summarizer import EpisodeSummarizer
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        turns = [f"User: Turn {i} content here" for i in range(100)]
        transcript = "\n\n".join(turns)

        chunks = summarizer._chunk_transcript(transcript, max_chars=500)
        for chunk in chunks:
            # Each chunk should contain complete turns
            # No turn should be cut in the middle
            if "Turn" in chunk:
                # Every "User: Turn X" should be complete
                for line in chunk.split("\n\n"):
                    if line.startswith("User: Turn"):
                        assert "content here" in line
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py::TestChunkedSummarization -v -x`
Expected: FAIL — `_chunk_transcript` method doesn't exist

- [ ] **Step 3: Implement `_chunk_transcript` method**

In `nous/handlers/episode_summarizer.py`, add after the `_truncate_transcript` method:

```python
    def _chunk_transcript(self, transcript: str, max_chars: int = 16000) -> list[str]:
        """F025 P3-B: Split long transcript into chunks at turn boundaries.

        Returns a list of chunks, each within max_chars. Splits on
        double-newline turn boundaries to preserve turn integrity.
        Short transcripts return as a single-element list.
        """
        if len(transcript) <= max_chars:
            return [transcript]

        turns = transcript.split("\n\n")
        chunks: list[str] = []
        current_turns: list[str] = []
        current_len = 0

        for turn in turns:
            turn_len = len(turn) + 2  # +2 for \n\n separator
            if current_len + turn_len > max_chars and current_turns:
                chunks.append("\n\n".join(current_turns))
                current_turns = []
                current_len = 0
            current_turns.append(turn)
            current_len += turn_len

        if current_turns:
            chunks.append("\n\n".join(current_turns))

        return chunks
```

- [ ] **Step 4: Wire chunked summarization into `_generate_summary`**

In `nous/handlers/episode_summarizer.py`, find `_generate_summary` method. Replace the single-pass summarization with chunk-aware logic:

```python
    async def _generate_summary(self, transcript: str, decision_context: str) -> dict | None:
        """Generate structured summary from transcript using LLM."""
        if not self._llm:
            logger.warning("No LLM client for episode summarizer")
            return None

        max_chars = self._settings.transcript_max_chars
        chunks = self._chunk_transcript(transcript, max_chars=max_chars)

        if len(chunks) == 1:
            # Single chunk: truncate and summarize directly (original path)
            truncated = self._truncate_transcript(chunks[0], max_chars=max_chars)
            return await self._summarize_single(truncated, decision_context)

        # Multi-chunk: summarize each chunk, then merge
        chunk_summaries = []
        for i, chunk in enumerate(chunks):
            truncated = self._truncate_transcript(chunk, max_chars=max_chars)
            summary = await self._summarize_single(truncated, decision_context)
            if summary:
                chunk_summaries.append(summary)

        if not chunk_summaries:
            return None

        if len(chunk_summaries) == 1:
            return chunk_summaries[0]

        return self._merge_summaries(chunk_summaries)
```

Extract the existing LLM call into `_summarize_single`:

```python
    async def _summarize_single(self, transcript: str, decision_context: str) -> dict | None:
        """Summarize a single transcript chunk via LLM."""
        prompt = _SUMMARY_PROMPT.format(transcript=transcript, decision_context=decision_context)

        text = await call_background_llm(
            self._llm,
            model=self._settings.background_model,
            system_prompt="You are summarizing a conversation episode for an AI agent's long-term memory.",
            user_message=prompt,
            max_tokens=1500,
        )

        if not text:
            logger.warning("Summary LLM returned empty text")
            return None

        try:
            return parse_llm_json(text)
        except json.JSONDecodeError as e:
            logger.warning("Summary generation failed: %s", e)
            return None

    def _merge_summaries(self, summaries: list[dict]) -> dict:
        """Merge multiple chunk summaries into one consolidated summary."""
        merged_summary_parts = []
        merged_key_points: list[str] = []
        merged_candidate_facts: list[dict] = []

        for s in summaries:
            if s.get("summary"):
                merged_summary_parts.append(s["summary"])
            merged_key_points.extend(s.get("key_points", []))
            merged_candidate_facts.extend(s.get("candidate_facts", []))

        return {
            "summary": " ".join(merged_summary_parts),
            "key_points": merged_key_points[:10],  # cap at 10
            "candidate_facts": merged_candidate_facts[:5],  # cap at 5
        }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py::TestChunkedSummarization -v`
Expected: All 3 tests PASS

- [ ] **Step 6: Commit**

```bash
git add nous/handlers/episode_summarizer.py tests/test_f025_amnesia_prevention.py
git commit -m "feat(f025): P3-B chunked summarization for long episodes"
```

---

### Task 8: P3-C — Transcript Persistence on Episode Model

**Files:**
- Create: `sql/migrations/017_add_episode_transcript.sql`
- Modify: `nous/storage/models.py` (Episode model)
- Modify: `nous/heart/episodes.py` (populate transcript on close)
- Modify: `nous/heart/facts.py` (update `_get_source_text` fallback)
- Test: `tests/test_f025_amnesia_prevention.py`

**Context:** The Episode model has no `transcript` column. Raw conversation text is lost after summarization. Adding a nullable `transcript` column enables future search-within-conversations and gives `_get_source_text` access to full source material.

- [ ] **Step 1: Create migration SQL**

```sql
-- sql/migrations/017_add_episode_transcript.sql
-- F025 P3-C: Add transcript column to episodes for full-text persistence.
-- Nullable TEXT — only populated for episodes closed after this migration.

ALTER TABLE heart.episodes
    ADD COLUMN IF NOT EXISTS transcript TEXT;

COMMENT ON COLUMN heart.episodes.transcript IS 'F025: Raw conversation transcript, populated on episode close';
```

- [ ] **Step 2: Write failing tests**

Append to `tests/test_f025_amnesia_prevention.py`:

```python
class TestTranscriptPersistence:
    """P3-C: Episode model should have transcript column."""

    def test_episode_model_has_transcript(self):
        """Episode ORM model should have transcript column."""
        from nous.storage.models import Episode
        assert hasattr(Episode, "transcript")

    def test_episode_transcript_nullable(self):
        """Transcript column should be nullable."""
        from nous.storage.models import Episode
        col = Episode.__table__.columns["transcript"]
        assert col.nullable is True

    @pytest.mark.asyncio
    async def test_get_source_text_prefers_source_text_over_transcript(self):
        """source_text field > transcript column > summary."""
        from nous.heart.facts import FactManager

        manager = FactManager.__new__(FactManager)

        # source_text takes priority
        inp = MagicMock()
        inp.source_text = "from FactInput"
        inp.source_episode_id = None
        session = AsyncMock()

        result = await manager._get_source_text(inp, session)
        assert result == "from FactInput"

    @pytest.mark.asyncio
    async def test_get_source_text_uses_transcript_over_summary(self):
        """When no source_text, prefer episode.transcript over episode.summary."""
        from nous.heart.facts import FactManager
        from uuid import uuid4

        manager = FactManager.__new__(FactManager)

        inp = MagicMock()
        inp.source_text = None
        inp.source_episode_id = uuid4()

        mock_episode = MagicMock()
        mock_episode.transcript = "full transcript text here"
        mock_episode.summary = "short summary"

        session = AsyncMock()
        session.get = AsyncMock(return_value=mock_episode)

        result = await manager._get_source_text(inp, session)
        assert result == "full transcript text here"

    @pytest.mark.asyncio
    async def test_get_source_text_falls_back_to_summary(self):
        """When no transcript, fall back to summary."""
        from nous.heart.facts import FactManager
        from uuid import uuid4

        manager = FactManager.__new__(FactManager)

        inp = MagicMock()
        inp.source_text = None
        inp.source_episode_id = uuid4()

        mock_episode = MagicMock()
        mock_episode.transcript = None
        mock_episode.summary = "summary text"

        session = AsyncMock()
        session.get = AsyncMock(return_value=mock_episode)

        result = await manager._get_source_text(inp, session)
        assert result == "summary text"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py::TestTranscriptPersistence -v -x`
Expected: FAIL — Episode model doesn't have `transcript` attribute

- [ ] **Step 4: Add transcript column to Episode model**

In `nous/storage/models.py`, find the Episode class. After the `structured_summary` column, add:

```python
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)  # F025 P3-C
```

Make sure `Text` is imported from `sqlalchemy` (it likely already is).

- [ ] **Step 5: Update `_get_source_text` to prefer transcript over summary**

In `nous/heart/facts.py`, update the method (already modified in Task 5):

```python
    async def _get_source_text(
        self,
        fact_input: FactInput,
        session: AsyncSession,
    ) -> str | None:
        """Retrieve source text for ROUGE-L grounding check.

        Priority: FactInput.source_text > Episode.transcript > Episode.summary.
        """
        # F025 P2-E: Use passed-through transcript if available
        if fact_input.source_text:
            return fact_input.source_text

        if not fact_input.source_episode_id:
            return None

        episode = await session.get(Episode, fact_input.source_episode_id)
        if not episode:
            return None

        # F025 P3-C: Prefer persisted transcript over lossy summary
        if episode.transcript:
            return episode.transcript
        if episode.summary:
            return episode.summary

        return None
```

- [ ] **Step 6: Wire transcript persistence into episode close**

In `nous/heart/episodes.py`, find the `close` or `end_episode` method. Add transcript parameter:

First check the method signature — read the file to find the exact method. Then add `transcript: str | None = None` parameter and persist it:

```python
    # In the close/end_episode method, add:
    if transcript:
        episode.transcript = transcript
```

In `nous/cognitive/layer.py` or wherever `end_episode` is called with session data, pass the transcript. Find where the transcript is available and thread it through.

**Note to implementer:** Read `nous/heart/episodes.py` to find the exact close method signature. Then find the caller in `layer.py` or `handlers/` that has access to the transcript text.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_f025_amnesia_prevention.py::TestTranscriptPersistence -v`
Expected: All 5 tests PASS

- [ ] **Step 8: Run full test suite for regression**

Run: `uv run pytest tests/ -v --timeout=120 -x`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add sql/migrations/017_add_episode_transcript.sql nous/storage/models.py nous/heart/facts.py nous/heart/episodes.py tests/test_f025_amnesia_prevention.py
git commit -m "feat(f025): P3-C transcript persistence on Episode model"
```

---

## Post-Implementation Checklist

- [ ] All 8 tasks committed
- [ ] Full test suite passes: `uv run pytest tests/ -v --timeout=120`
- [ ] Update `docs/features/F025-amnesia-prevention.md` status:
  - Phase 2: all items → ✅ Fixed
  - Phase 3: all items → ✅ Shipped
  - RC-1,4,5,6,7 status → ✅ Fixed
- [ ] Update `docs/features/INDEX.md` to add F025 entry
- [ ] Update `CLAUDE.md` env vars table with new config fields
