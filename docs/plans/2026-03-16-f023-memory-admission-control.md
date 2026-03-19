# F023 Memory Admission Control — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 5-dimension admission gate to `FactManager._learn()` that scores candidate facts and rejects low-value ones before storage, with shadow mode for safe rollout.

**Architecture:** New `AdmissionController` class in `nous/heart/admission.py` evaluates facts across utility (LLM-scored), confidence (ROUGE-L grounding), novelty, recency (exponential decay), and type prior. Wired into `FactManager._learn()` after dedup check. Shadow mode (default on) scores everything but admits all.

**Tech Stack:** Python 3.12+, async SQLAlchemy, httpx (LLM calls), PostgreSQL + pgvector, pytest + real Postgres

**Design doc:** `docs/plans/2026-03-16-f023-memory-admission-control-design.md`
**Feature spec:** `docs/features/F023-memory-admission-control.md`

---

## Chunk 1: Schema, Config, and AdmissionController

### Task 1: SQL Migration

**Files:**
- Create: `sql/migrations/017_memory_admission_control.sql`

- [ ] **Step 1: Create migration file**

```sql
-- F023: Memory Admission Control (A-MAC)
-- Adds admission scoring columns to heart.facts.
-- Safe for existing installations: IF NOT EXISTS + DEFAULT values.
-- Existing facts get admission_score=NULL, recall_count=0, last_recalled_at=NULL.

ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS admission_score FLOAT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS recall_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_recalled_at TIMESTAMPTZ DEFAULT NULL;

COMMENT ON COLUMN heart.facts.admission_score IS
    'A-MAC composite score at time of admission. NULL for pre-F023 facts.';
COMMENT ON COLUMN heart.facts.recall_count IS
    'Number of times this fact was recalled and used in a response.';
COMMENT ON COLUMN heart.facts.last_recalled_at IS
    'Last time this fact was recalled and used.';
```

- [ ] **Step 2: Verify migration runs**

Run: `docker compose up -d postgres && uv run python -c "import asyncio; from nous.storage.database import Database; from nous.storage.migrator import run_migrations; from nous.config import Settings; s=Settings(); db=Database(s); asyncio.run(run_migrations(db._engine))"`
Expected: `Migration 017_memory_admission_control applied`

- [ ] **Step 3: Commit**

```bash
git add sql/migrations/017_memory_admission_control.sql
git commit -m "feat(f023): add admission_score, recall_count, last_recalled_at columns to heart.facts"
```

---

### Task 2: ORM Model Update

**Files:**
- Modify: `nous/storage/models.py:370-401` (Fact class)

- [ ] **Step 1: Add new columns to Fact model**

Add after line 395 (the `updated_at` column), before the relationships block:

```python
    # F023: Memory Admission Control
    admission_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    recall_count: Mapped[int | None] = mapped_column(Integer, server_default="0")
    last_recalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 2: Commit**

```bash
git add nous/storage/models.py
git commit -m "feat(f023): add admission columns to Fact ORM model"
```

---

### Task 3: Schema Updates (FactInput, FactRejected)

**Files:**
- Modify: `nous/heart/schemas.py:80-92` (FactInput)
- Modify: `nous/heart/schemas.py` (add FactRejected after FactDetail)

- [ ] **Step 1: Add source_timestamp to FactInput**

In `nous/heart/schemas.py`, add to `FactInput` class after `tags` field (line 91):

```python
    # F023: Timestamp of source context for accurate recency scoring.
    # For live conversation facts: None (defaults to now → recency ≈ 1.0)
    # For sleep-reflected facts: timestamp of the oldest source episode
    # For knowledge_extractor: timestamp of the compacted episode
    source_timestamp: datetime | None = None
```

Also add `datetime` import at the top if not already imported:

```python
from datetime import datetime
```

- [ ] **Step 2: Add FactRejected class**

Add after `FactDetail` class (after line 128):

```python
class FactRejected(BaseModel):
    """Returned when a fact is rejected by admission control."""

    admitted: bool = False
    content: str
    composite_score: float
    threshold: float
    scores: dict[str, float]
    explanation: str
```

- [ ] **Step 3: Update `__init__.py` export**

Check `nous/heart/__init__.py` and add `FactRejected` to exports if the module uses explicit exports.

- [ ] **Step 4: Commit**

```bash
git add nous/heart/schemas.py nous/heart/__init__.py
git commit -m "feat(f023): add source_timestamp to FactInput, add FactRejected schema"
```

---

### Task 4: Settings

**Files:**
- Modify: `nous/config.py:240-251` (after F012 settings block)

- [ ] **Step 1: Add admission control settings**

Add after the F012 procedure learning block (after line 250):

```python
    # F023: Memory Admission Control (A-MAC)
    admission_control_enabled: bool = True
    admission_shadow_mode: bool = True  # Safe default: score all, admit all
    admission_threshold: float = 0.55  # Zhang et al. validated
    admission_w_utility: float = 0.25
    admission_w_confidence: float = 0.15
    admission_w_novelty: float = 0.20
    admission_w_recency: float = 0.10
    admission_w_type_prior: float = 0.30  # Most influential per ablation
    admission_recency_lambda: float = 0.01  # λ=0.01/hr → half-life ~3 days
    admission_utility_model: str = ""  # Empty = fall back to background_model
    admission_utility_llm_enabled: bool = True
```

- [ ] **Step 2: Verify settings load**

Run: `uv run python -c "from nous.config import Settings; s=Settings(); print(s.admission_control_enabled, s.admission_shadow_mode, s.admission_threshold)"`
Expected: `True True 0.55`

- [ ] **Step 3: Commit**

```bash
git add nous/config.py
git commit -m "feat(f023): add admission control settings to config"
```

---

### Task 5: AdmissionController — Core Class

**Files:**
- Create: `nous/heart/admission.py`
- Test: `tests/test_admission.py`

This is the largest task. We build the controller with all 5 scoring dimensions, TDD style.

- [ ] **Step 1: Write ROUGE-L + LCS test**

Create `tests/test_admission.py`:

```python
"""Tests for F023 Memory Admission Control (A-MAC).

Unit tests for the AdmissionController scoring dimensions,
ROUGE-L grounding, bypass logic, and shadow mode.
"""

import math
from datetime import UTC, datetime, timedelta

import pytest

from nous.heart.admission import (
    AdmissionConfig,
    AdmissionController,
    AdmissionResult,
    DEFAULT_TYPE_PRIORS,
    DEFAULT_WEIGHTS,
)
from nous.heart.schemas import FactInput


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fact(
    content: str = "Tim prefers dark mode in his IDE",
    category: str | None = "preference",
    subject: str | None = "Tim",
    confidence: float = 0.9,
    source: str | None = "fact_extractor",
    **kwargs,
) -> FactInput:
    return FactInput(
        content=content,
        category=category,
        subject=subject,
        confidence=confidence,
        source=source,
        **kwargs,
    )


def _controller(**overrides) -> AdmissionController:
    config = AdmissionConfig(**overrides)
    return AdmissionController(config=config)


# ---------------------------------------------------------------------------
# ROUGE-L / LCS
# ---------------------------------------------------------------------------


class TestRougeL:
    def test_exact_match(self):
        ctrl = _controller()
        score = ctrl._rouge_l_score("hello world", "hello world")
        assert score == 1.0

    def test_no_overlap(self):
        ctrl = _controller()
        score = ctrl._rouge_l_score("hello world", "foo bar baz")
        assert score == 0.0

    def test_partial_overlap(self):
        ctrl = _controller()
        # "Tim prefers dark mode" vs source containing those words
        fact_text = "Tim prefers dark mode"
        source_text = "In our conversation Tim mentioned he prefers dark mode for coding"
        score = ctrl._rouge_l_score(fact_text, source_text)
        # LCS should find "Tim prefers dark mode" (4 tokens) in source
        assert score > 0.5

    def test_empty_fact(self):
        ctrl = _controller()
        score = ctrl._rouge_l_score("", "hello world")
        assert score == 0.5  # Neutral fallback

    def test_empty_source(self):
        ctrl = _controller()
        score = ctrl._rouge_l_score("hello world", "")
        assert score == 0.5  # Neutral fallback


class TestLCS:
    def test_empty(self):
        ctrl = _controller()
        assert ctrl._lcs_length([], []) == 0

    def test_single_match(self):
        ctrl = _controller()
        assert ctrl._lcs_length(["a"], ["a"]) == 1

    def test_no_match(self):
        ctrl = _controller()
        assert ctrl._lcs_length(["a"], ["b"]) == 0

    def test_subsequence(self):
        ctrl = _controller()
        assert ctrl._lcs_length(["a", "b", "c"], ["a", "x", "b", "y", "c"]) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_admission.py -v -k "TestRougeL or TestLCS"`
Expected: FAIL — `ImportError: cannot import name 'AdmissionController'`

- [ ] **Step 3: Create admission.py with dataclasses and ROUGE-L**

Create `nous/heart/admission.py`:

```python
"""Memory Admission Control — A-MAC implementation.

Evaluates candidate facts across 5 dimensions before storage.
Returns admit/reject decision with per-dimension scores and explanation.

Based on Zhang et al. (2026) "Adaptive Memory Admission Control for LLM Agents"
adapted for the Nous architecture.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from nous.heart.schemas import FactInput

logger = logging.getLogger(__name__)


# Category-based priors: how likely is a fact in this category to be useful?
# Type prior is the MOST influential dimension per Zhang et al. ablation study.
DEFAULT_TYPE_PRIORS = {
    "rule": 0.95,
    "preference": 0.90,
    "person": 0.85,
    "technical": 0.70,
    "tool": 0.65,
    "concept": 0.60,
}

# Weights per Zhang et al. ablation findings
DEFAULT_WEIGHTS = {
    "utility": 0.25,
    "confidence": 0.15,
    "novelty": 0.20,
    "recency": 0.10,
    "type_prior": 0.30,
}

DEFAULT_THRESHOLD = 0.55

# Exponential decay: λ = 0.01/hour → half-life ≈ 69 hours (~3 days)
RECENCY_DECAY_LAMBDA = 0.01


@dataclass
class AdmissionResult:
    """Result of admission scoring."""

    admitted: bool
    composite_score: float
    threshold: float
    scores: dict[str, float]
    explanation: str
    bypassed: bool = False
    shadow_mode: bool = False


@dataclass
class AdmissionConfig:
    """Tunable admission parameters."""

    weights: dict[str, float] = field(default_factory=lambda: DEFAULT_WEIGHTS.copy())
    type_priors: dict[str, float] = field(default_factory=lambda: DEFAULT_TYPE_PRIORS.copy())
    threshold: float = DEFAULT_THRESHOLD
    recency_lambda: float = RECENCY_DECAY_LAMBDA
    bypass_sources: list[str] = field(
        default_factory=lambda: [
            "user_stated",
            "user_direct",
            "identity",
            "censor",
            "supersede",
            "contradict",
        ]
    )
    utility_llm_enabled: bool = True
    utility_llm_model: str = ""  # Empty = use background_model fallback
    shadow_mode: bool = False


class AdmissionController:
    """Evaluates candidate facts for admission to long-term memory.

    Scores across 5 dimensions (per Zhang et al., 2026):
    - Utility: Will this be useful in future turns? (LLM-scored)
    - Confidence: Is this grounded in source text? (ROUGE-L)
    - Novelty: How different is this from what we already know?
    - Recency: How fresh is the source context? (exponential decay)
    - Type Prior: Category-based prior probability of usefulness.
    """

    def __init__(self, config: AdmissionConfig | None = None, llm_client=None):
        self.config = config or AdmissionConfig()
        self.llm_client = llm_client

    # ------------------------------------------------------------------
    # ROUGE-L grounding
    # ------------------------------------------------------------------

    def _rouge_l_score(self, fact_text: str, source_text: str) -> float:
        """Compute ROUGE-L F1 between fact and source text.

        Lightweight implementation — whitespace tokenization, no external deps.
        """
        fact_tokens = fact_text.lower().split()
        source_tokens = source_text.lower().split()

        if not fact_tokens or not source_tokens:
            return 0.5  # Can't assess — neutral

        lcs_len = self._lcs_length(fact_tokens, source_tokens)

        precision = lcs_len / len(fact_tokens)
        recall = lcs_len / len(source_tokens)

        if precision + recall == 0:
            return 0.0

        return 2 * precision * recall / (precision + recall)

    def _lcs_length(self, x: list[str], y: list[str]) -> int:
        """Length of longest common subsequence. O(min(m,n)) space."""
        if len(x) > len(y):
            x, y = y, x

        m, n = len(x), len(y)
        prev = [0] * (m + 1)
        curr = [0] * (m + 1)

        for j in range(1, n + 1):
            for i in range(1, m + 1):
                if x[i - 1] == y[j - 1]:
                    curr[i] = prev[i - 1] + 1
                else:
                    curr[i] = max(curr[i - 1], prev[i])
            prev, curr = curr, [0] * (m + 1)

        return prev[m]
```

- [ ] **Step 4: Run ROUGE-L tests**

Run: `uv run pytest tests/test_admission.py -v -k "TestRougeL or TestLCS"`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add nous/heart/admission.py tests/test_admission.py
git commit -m "feat(f023): AdmissionController skeleton with ROUGE-L scoring"
```

---

### Task 6: Scoring Dimensions — Type Prior, Recency, Novelty, Confidence

**Files:**
- Modify: `nous/heart/admission.py`
- Modify: `tests/test_admission.py`

- [ ] **Step 1: Write tests for all 4 heuristic dimensions**

Add to `tests/test_admission.py`:

```python
# ---------------------------------------------------------------------------
# Type Prior
# ---------------------------------------------------------------------------


class TestTypePrior:
    def test_known_categories(self):
        ctrl = _controller()
        assert ctrl._score_type_prior(_fact(category="rule")) == 0.95
        assert ctrl._score_type_prior(_fact(category="preference")) == 0.90
        assert ctrl._score_type_prior(_fact(category="person")) == 0.85
        assert ctrl._score_type_prior(_fact(category="technical")) == 0.70
        assert ctrl._score_type_prior(_fact(category="tool")) == 0.65
        assert ctrl._score_type_prior(_fact(category="concept")) == 0.60

    def test_unknown_category(self):
        ctrl = _controller()
        assert ctrl._score_type_prior(_fact(category="other")) == 0.50

    def test_none_category(self):
        ctrl = _controller()
        assert ctrl._score_type_prior(_fact(category=None)) == 0.50


# ---------------------------------------------------------------------------
# Recency
# ---------------------------------------------------------------------------


class TestRecency:
    def test_current_conversation(self):
        """No source_timestamp → hours=0 → score ≈ 1.0."""
        ctrl = _controller()
        score = ctrl._score_recency(_fact())
        assert score == pytest.approx(1.0, abs=0.01)

    def test_three_days_ago(self):
        """~69 hours → half-life → score ≈ 0.5."""
        ctrl = _controller()
        ts = datetime.now(UTC) - timedelta(hours=69)
        score = ctrl._score_recency(_fact(source_timestamp=ts))
        assert score == pytest.approx(0.5, abs=0.05)

    def test_one_week_ago(self):
        """168 hours → score ≈ 0.19."""
        ctrl = _controller()
        ts = datetime.now(UTC) - timedelta(hours=168)
        score = ctrl._score_recency(_fact(source_timestamp=ts))
        assert score == pytest.approx(0.19, abs=0.05)

    def test_custom_lambda(self):
        """Faster decay with higher lambda."""
        ctrl = _controller(recency_lambda=0.1)
        ts = datetime.now(UTC) - timedelta(hours=10)
        score = ctrl._score_recency(_fact(source_timestamp=ts))
        expected = math.exp(-0.1 * 10)
        assert score == pytest.approx(expected, abs=0.01)


# ---------------------------------------------------------------------------
# Novelty
# ---------------------------------------------------------------------------


class TestNovelty:
    def test_no_existing_facts(self):
        """No similarity data → neutral 0.5."""
        ctrl = _controller()
        assert ctrl._score_novelty(None) == 0.5

    def test_highly_similar(self):
        """0.90 similarity → 0.10 novelty."""
        ctrl = _controller()
        assert ctrl._score_novelty(0.90) == pytest.approx(0.10, abs=0.01)

    def test_moderately_similar(self):
        """0.50 similarity → 0.50 novelty."""
        ctrl = _controller()
        assert ctrl._score_novelty(0.50) == pytest.approx(0.50, abs=0.01)

    def test_completely_novel(self):
        """0.0 similarity → 1.0 novelty."""
        ctrl = _controller()
        assert ctrl._score_novelty(0.0) == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# Confidence (ROUGE-L grounding)
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_with_source_text(self):
        """Source text available → ROUGE-L score."""
        ctrl = _controller()
        fact = _fact(content="Tim prefers dark mode")
        source = "Tim mentioned he prefers dark mode for coding"
        score = ctrl._score_confidence(fact, source)
        assert score > 0.5  # Good grounding

    def test_no_source_text_knowledge_extractor(self):
        """No source → fallback penalty for knowledge_extractor."""
        ctrl = _controller()
        fact = _fact(source="knowledge_extractor", confidence=0.8)
        score = ctrl._score_confidence(fact, None)
        assert score == pytest.approx(0.70, abs=0.01)  # 0.8 - 0.10

    def test_no_source_text_sleep_reflection(self):
        """Sleep reflection gets highest penalty."""
        ctrl = _controller()
        fact = _fact(source="sleep_reflection", confidence=0.8)
        score = ctrl._score_confidence(fact, None)
        assert score == pytest.approx(0.65, abs=0.01)  # 0.8 - 0.15

    def test_no_source_text_no_penalty(self):
        """Unknown source without source text → use raw confidence."""
        ctrl = _controller()
        fact = _fact(source="some_other_source", confidence=0.8)
        score = ctrl._score_confidence(fact, None)
        assert score == pytest.approx(0.80, abs=0.01)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_admission.py -v -k "TestTypePrior or TestRecency or TestNovelty or TestConfidence"`
Expected: FAIL — `AttributeError: 'AdmissionController' has no attribute '_score_type_prior'`

- [ ] **Step 3: Implement the 4 scoring methods**

Add to `AdmissionController` in `nous/heart/admission.py`:

```python
    # ------------------------------------------------------------------
    # Scoring dimensions
    # ------------------------------------------------------------------

    def _score_type_prior(self, fact_input: FactInput) -> float:
        """Category-based prior probability of usefulness.

        Most influential dimension per Zhang et al. ablation (-0.107 F1 drop).
        """
        if not fact_input.category:
            return 0.50
        return self.config.type_priors.get(fact_input.category, 0.50)

    def _score_recency(self, fact_input: FactInput) -> float:
        """Exponential time-decay: e^(-λ * hours_since_source).

        λ = 0.01/hour → half-life ≈ 69 hours (~3 days).
        """
        now = datetime.now(UTC)

        if fact_input.source_timestamp is None:
            hours_elapsed = 0.0
        else:
            delta = now - fact_input.source_timestamp
            hours_elapsed = delta.total_seconds() / 3600.0

        return max(0.0, min(1.0, math.exp(-self.config.recency_lambda * hours_elapsed)))

    def _score_novelty(self, max_existing_similarity: float | None) -> float:
        """Inverse of max similarity to existing facts."""
        if max_existing_similarity is None:
            return 0.5
        return max(0.0, min(1.0, 1.0 - max_existing_similarity))

    def _score_confidence(self, fact_input: FactInput, source_text: str | None) -> float:
        """Hallucination grounding via ROUGE-L, with source-penalty fallback."""
        if source_text:
            return self._rouge_l_score(fact_input.content, source_text)

        base = fact_input.confidence
        source_penalties = {
            "knowledge_extractor": 0.10,
            "episode_summarizer": 0.05,
            "sleep_reflection": 0.15,
            "compaction_extraction": 0.10,
        }
        penalty = source_penalties.get(fact_input.source or "", 0.0)
        return max(0.0, min(1.0, base - penalty))
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_admission.py -v -k "TestTypePrior or TestRecency or TestNovelty or TestConfidence"`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add nous/heart/admission.py tests/test_admission.py
git commit -m "feat(f023): type_prior, recency, novelty, confidence scoring dimensions"
```

---

### Task 7: Utility Scoring (LLM + Heuristic Fallback)

**Files:**
- Modify: `nous/heart/admission.py`
- Modify: `tests/test_admission.py`

- [ ] **Step 1: Write utility scoring tests**

Add to `tests/test_admission.py`:

```python
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Utility — Heuristic
# ---------------------------------------------------------------------------


class TestUtilityHeuristic:
    def test_baseline(self):
        ctrl = _controller(utility_llm_enabled=False)
        fact = _fact(content="A basic fact about something", subject=None, tags=[])
        score = ctrl._heuristic_utility_score(fact)
        assert score == pytest.approx(0.5, abs=0.01)

    def test_subject_bonus(self):
        ctrl = _controller(utility_llm_enabled=False)
        fact = _fact(subject="Tim")
        score = ctrl._heuristic_utility_score(fact)
        assert score > 0.5

    def test_tags_bonus(self):
        ctrl = _controller(utility_llm_enabled=False)
        fact = _fact(subject=None, tags=["python", "ide", "settings"])
        score = ctrl._heuristic_utility_score(fact)
        assert score > 0.5

    def test_short_content_penalty(self):
        ctrl = _controller(utility_llm_enabled=False)
        fact = _fact(content="yes", subject=None, tags=[])
        score = ctrl._heuristic_utility_score(fact)
        assert score < 0.5

    def test_long_content_penalty(self):
        ctrl = _controller(utility_llm_enabled=False)
        fact = _fact(content="x " * 300, subject=None, tags=[])
        score = ctrl._heuristic_utility_score(fact)
        assert score < 0.5

    def test_clamped_to_0_1(self):
        ctrl = _controller(utility_llm_enabled=False)
        # Very short, no subject, no tags → should be low but >= 0
        fact = _fact(content="x", subject=None, tags=[])
        score = ctrl._heuristic_utility_score(fact)
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Utility — LLM
# ---------------------------------------------------------------------------


class TestUtilityLLM:
    @pytest.mark.asyncio
    async def test_llm_score_success(self):
        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(return_value="0.85")
        ctrl = AdmissionController(
            config=AdmissionConfig(utility_llm_enabled=True),
            llm_client=mock_client,
        )
        score = await ctrl._score_utility(_fact())
        assert score == pytest.approx(0.85, abs=0.01)
        mock_client.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_score_clamp(self):
        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(return_value="1.5")
        ctrl = AdmissionController(
            config=AdmissionConfig(utility_llm_enabled=True),
            llm_client=mock_client,
        )
        score = await ctrl._score_utility(_fact())
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_llm_score_fallback_on_error(self):
        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(side_effect=Exception("API error"))
        ctrl = AdmissionController(
            config=AdmissionConfig(utility_llm_enabled=True),
            llm_client=mock_client,
        )
        score = await ctrl._score_utility(_fact())
        # Falls back to heuristic
        assert 0.0 <= score <= 1.0

    @pytest.mark.asyncio
    async def test_llm_score_fallback_on_unparseable(self):
        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(return_value="I think about 0.7")
        ctrl = AdmissionController(
            config=AdmissionConfig(utility_llm_enabled=True),
            llm_client=mock_client,
        )
        score = await ctrl._score_utility(_fact())
        assert score == 0.5  # Neutral fallback

    @pytest.mark.asyncio
    async def test_llm_disabled_uses_heuristic(self):
        ctrl = _controller(utility_llm_enabled=False)
        score = await ctrl._score_utility(_fact())
        assert 0.0 <= score <= 1.0

    @pytest.mark.asyncio
    async def test_llm_prompt_includes_calibration_anchors(self):
        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(return_value="0.7")
        ctrl = AdmissionController(
            config=AdmissionConfig(utility_llm_enabled=True),
            llm_client=mock_client,
        )
        await ctrl._score_utility(_fact())
        call_args = mock_client.complete.call_args
        prompt = call_args.kwargs.get("prompt", "") or call_args.args[0] if call_args.args else ""
        # Check for calibration anchors
        assert "0.9" in str(call_args) or "birthday" in str(call_args)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_admission.py -v -k "TestUtility"`
Expected: FAIL — `AttributeError: 'AdmissionController' has no attribute '_score_utility'`

- [ ] **Step 3: Implement utility scoring**

Add to `AdmissionController` in `nous/heart/admission.py`:

```python
    async def _score_utility(self, fact_input: FactInput) -> float:
        """Predict future utility. LLM-scored with heuristic fallback."""
        if self.config.utility_llm_enabled and self.llm_client:
            try:
                return await self._llm_utility_score(fact_input)
            except Exception as e:
                logger.warning("LLM utility scoring failed, falling back to heuristic: %s", e)

        return self._heuristic_utility_score(fact_input)

    async def _llm_utility_score(self, fact_input: FactInput) -> float:
        """Score utility via LLM call with calibration anchors."""
        prompt = f"""Rate how useful this fact will be in future conversations.
Consider: Will the user or agent need this information again?
Is it specific and actionable, or vague and ephemeral?

Calibration examples:
- "Tim's birthday is March 15" → 0.9 (personal, reusable, specific)
- "Tim mentioned he had a busy week" → 0.3 (vague, ephemeral, no future use)
- "The meeting went well" → 0.2 (subjective, no actionable content)
- "Tim prefers Celsius for temperatures" → 0.95 (preference, used every time)
- "It was raining during our conversation" → 0.1 (irrelevant ephemeral context)

Fact: {fact_input.content}
Category: {fact_input.category or 'unknown'}
Subject: {fact_input.subject or 'none'}

Respond with ONLY a number between 0.0 and 1.0."""

        response = await self.llm_client.complete(
            model=self.config.utility_llm_model,
            prompt=prompt,
            max_tokens=10,
        )

        try:
            score = float(response.strip())
            return max(0.0, min(1.0, score))
        except ValueError:
            logger.warning("LLM utility score not parseable: %s", response)
            return 0.5

    def _heuristic_utility_score(self, fact_input: FactInput) -> float:
        """Heuristic fallback for utility scoring."""
        score = 0.5

        if fact_input.subject:
            score += 0.15

        if fact_input.tags:
            score += 0.05 * min(len(fact_input.tags), 3)

        content_len = len(fact_input.content)
        if content_len < 20:
            score -= 0.2
        elif content_len > 500:
            score -= 0.1
        elif 50 <= content_len <= 300:
            score += 0.1

        if fact_input.source_decision_id:
            score += 0.1

        if fact_input.source_episode_id:
            score += 0.05

        return max(0.0, min(1.0, score))
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_admission.py -v -k "TestUtility"`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add nous/heart/admission.py tests/test_admission.py
git commit -m "feat(f023): utility scoring with LLM + heuristic fallback"
```

---

### Task 8: Composite Scoring, Bypass, Shadow Mode

**Files:**
- Modify: `nous/heart/admission.py`
- Modify: `tests/test_admission.py`

- [ ] **Step 1: Write composite, bypass, and shadow tests**

Add to `tests/test_admission.py`:

```python
# ---------------------------------------------------------------------------
# Composite Scoring
# ---------------------------------------------------------------------------


class TestCompositeScoring:
    @pytest.mark.asyncio
    async def test_above_threshold_admits(self):
        """High-quality fact should be admitted."""
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(category="preference", confidence=0.9),
            embedding=None,
            max_existing_similarity=0.3,
            source_text="Tim mentioned he prefers dark mode for coding",
            session=None,
        )
        assert result.admitted is True
        assert result.composite_score >= 0.55
        assert "ADMIT" in result.explanation
        assert len(result.scores) == 5

    @pytest.mark.asyncio
    async def test_below_threshold_rejects(self):
        """Low-quality fact should be rejected."""
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(
                content="ok",
                category=None,
                subject=None,
                confidence=0.3,
                source="sleep_reflection",
                tags=[],
            ),
            embedding=None,
            max_existing_similarity=0.89,  # Very similar to existing
            source_text=None,
            session=None,
        )
        assert result.admitted is False
        assert result.composite_score < 0.55
        assert "REJECT" in result.explanation

    @pytest.mark.asyncio
    async def test_threshold_boundary(self):
        """Exact threshold value should admit (>=)."""
        # We can't easily set an exact score, but we test the >= logic
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=False, threshold=0.0)
        result = await ctrl.score(
            fact_input=_fact(),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert result.admitted is True  # threshold=0.0, any score passes

    @pytest.mark.asyncio
    async def test_weights_sum_correctly(self):
        """Verify weighted sum calculation."""
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        # Manually verify: sum of (weight * score) for each dimension
        w = DEFAULT_WEIGHTS
        expected = sum(w[k] * result.scores[k] for k in w)
        assert result.composite_score == pytest.approx(expected, abs=0.001)


# ---------------------------------------------------------------------------
# Bypass
# ---------------------------------------------------------------------------


class TestBypass:
    @pytest.mark.asyncio
    async def test_user_direct_bypasses(self):
        ctrl = _controller(shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(source="user_direct"),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert result.admitted is True
        assert result.bypassed is True
        assert result.composite_score == 1.0

    @pytest.mark.asyncio
    async def test_user_stated_bypasses(self):
        ctrl = _controller(shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(source="user_stated"),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert result.admitted is True
        assert result.bypassed is True

    @pytest.mark.asyncio
    async def test_supersede_bypasses(self):
        ctrl = _controller(shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(source="supersede"),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert result.admitted is True
        assert result.bypassed is True

    @pytest.mark.asyncio
    async def test_contradict_bypasses(self):
        ctrl = _controller(shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(source="contradict"),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert result.admitted is True
        assert result.bypassed is True

    @pytest.mark.asyncio
    async def test_non_bypass_source_scored(self):
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(source="fact_extractor"),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert result.bypassed is False
        assert len(result.scores) == 5


# ---------------------------------------------------------------------------
# Shadow Mode
# ---------------------------------------------------------------------------


class TestShadowMode:
    @pytest.mark.asyncio
    async def test_shadow_always_admits(self):
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=True)
        result = await ctrl.score(
            fact_input=_fact(
                content="ok",
                category=None,
                subject=None,
                confidence=0.1,
                tags=[],
            ),
            embedding=None,
            max_existing_similarity=0.94,
            source_text=None,
            session=None,
        )
        assert result.admitted is True
        assert result.shadow_mode is True

    @pytest.mark.asyncio
    async def test_shadow_logs_would_reject(self):
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=True)
        result = await ctrl.score(
            fact_input=_fact(
                content="ok",
                category=None,
                subject=None,
                confidence=0.1,
                tags=[],
            ),
            embedding=None,
            max_existing_similarity=0.94,
            source_text=None,
            session=None,
        )
        assert "SHADOW_WOULD_REJECT" in result.explanation or "SHADOW_WOULD_ADMIT" in result.explanation

    @pytest.mark.asyncio
    async def test_shadow_still_scores(self):
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=True)
        result = await ctrl.score(
            fact_input=_fact(),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert len(result.scores) == 5
        assert result.composite_score > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_admission.py -v -k "TestComposite or TestBypass or TestShadow"`
Expected: FAIL — `AdmissionController has no attribute 'score'`

- [ ] **Step 3: Implement the score() method**

Add the public `score()` method to `AdmissionController` in `nous/heart/admission.py`:

```python
    async def score(
        self,
        fact_input: FactInput,
        embedding: list[float] | None,
        max_existing_similarity: float | None,
        source_text: str | None,
        session,  # AsyncSession | None — not used yet, reserved for future
    ) -> AdmissionResult:
        """Score a candidate fact for admission."""
        # Check bypass first
        if fact_input.source in self.config.bypass_sources:
            return AdmissionResult(
                admitted=True,
                composite_score=1.0,
                threshold=self.config.threshold,
                scores={},
                explanation=f"Bypassed: source '{fact_input.source}' is in bypass list",
                bypassed=True,
            )

        scores = {}

        scores["utility"] = await self._score_utility(fact_input)
        scores["confidence"] = self._score_confidence(fact_input, source_text)
        scores["novelty"] = self._score_novelty(max_existing_similarity)
        scores["recency"] = self._score_recency(fact_input)
        scores["type_prior"] = self._score_type_prior(fact_input)

        # Weighted composite
        w = self.config.weights
        composite = sum(w[k] * scores[k] for k in w)

        # Shadow mode: always admit, log what would have happened
        if self.config.shadow_mode:
            would_reject = composite < self.config.threshold
            dims = ", ".join(f"{k}={v:.2f}" for k, v in scores.items())
            action = "SHADOW_WOULD_REJECT" if would_reject else "SHADOW_WOULD_ADMIT"
            explanation = f"{action} (score={composite:.3f}, threshold={self.config.threshold}): {dims}"
            logger.info("Admission shadow: %s — %s", fact_input.content[:80], explanation)
            return AdmissionResult(
                admitted=True,
                composite_score=composite,
                threshold=self.config.threshold,
                scores=scores,
                explanation=explanation,
                shadow_mode=True,
            )

        admitted = composite >= self.config.threshold
        dims = ", ".join(f"{k}={v:.2f}" for k, v in scores.items())
        action = "ADMIT" if admitted else "REJECT"
        explanation = f"{action} (score={composite:.3f}, threshold={self.config.threshold}): {dims}"

        return AdmissionResult(
            admitted=admitted,
            composite_score=composite,
            threshold=self.config.threshold,
            scores=scores,
            explanation=explanation,
        )
```

- [ ] **Step 4: Run all tests**

Run: `uv run pytest tests/test_admission.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add nous/heart/admission.py tests/test_admission.py
git commit -m "feat(f023): composite scoring with bypass and shadow mode"
```

---

## Chunk 2: Wiring, Integration, and Handler Updates

### Task 9: Wire AdmissionController into FactManager

**Files:**
- Modify: `nous/heart/facts.py:39-47` (__init__), `nous/heart/facts.py:144-223` (_learn)
- Modify: `nous/heart/heart.py:62-87` (__init__), `nous/heart/heart.py:203-223` (learn)

- [ ] **Step 1: Update FactManager.__init__ to accept controller**

In `nous/heart/facts.py`, update `__init__` (lines 39-47):

```python
    def __init__(
        self,
        db: Database,
        embeddings: EmbeddingProvider | None,
        agent_id: str,
        admission_controller: AdmissionController | None = None,
    ) -> None:
        self.db = db
        self.embeddings = embeddings
        self.agent_id = agent_id
        self._admission_controller = admission_controller
```

Add import at top of file (line 18 area):

```python
from nous.heart.admission import AdmissionController, AdmissionResult
from nous.heart.schemas import ContradictionWarning, FactDetail, FactInput, FactRejected, FactSummary
```

- [ ] **Step 2: Add _find_max_similarity and _get_source_text methods**

Add to `FactManager` in `nous/heart/facts.py`, after `_find_duplicate` method:

```python
    async def _find_max_similarity(
        self,
        embedding: list[float],
        exclude_ids: list[UUID],
        session: AsyncSession,
    ) -> float | None:
        """Find highest cosine similarity to any existing active fact.

        Used by admission controller for novelty scoring.
        Returns None if no facts exist or no embedding available.
        """
        if not embedding:
            return None

        embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
        params: dict = {"embedding": embedding_str, "agent_id": self.agent_id}

        exclude_clause = ""
        if exclude_ids:
            placeholders = ", ".join(f":excl_{i}" for i in range(len(exclude_ids)))
            exclude_clause = f"AND id NOT IN ({placeholders})"
            for i, eid in enumerate(exclude_ids):
                params[f"excl_{i}"] = eid

        sql = text(f"""
            SELECT 1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
              {exclude_clause}
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 1
        """)

        result = await session.execute(sql, params)
        row = result.first()
        return float(row.similarity) if row else None

    async def _get_source_text(
        self,
        fact_input: FactInput,
        session: AsyncSession,
    ) -> str | None:
        """Retrieve original source text for ROUGE-L grounding check.

        Fetches episode.content by PK if source_episode_id present.
        Episode content includes tool call outputs (web_search, bash, etc.).
        """
        if not fact_input.source_episode_id:
            return None

        from nous.storage.models import Episode
        episode = await session.get(Episode, fact_input.source_episode_id)
        if episode and episode.content:
            return episode.content

        return None
```

- [ ] **Step 3: Update _learn() to insert admission gate**

Update `_learn` return type and add gate logic. In `nous/heart/facts.py`, replace the `_learn` method (lines 144-223):

Change return type on line 153 from `-> FactDetail:` to `-> FactDetail | FactRejected:`.

Insert gate logic after the dedup check (after line 167) and before fact creation (line 169):

```python
        # F023: Admission gate — score candidate before storage
        admission_result: AdmissionResult | None = None
        if self._admission_controller is not None:
            max_sim = await self._find_max_similarity(embedding, exclude_ids, session) if embedding else None
            source_text = await self._get_source_text(input, session)

            admission_result = await self._admission_controller.score(
                input, embedding, max_sim, source_text, session
            )
            if not admission_result.admitted:
                logger.info(
                    "Fact rejected by admission: %s — %s",
                    input.content[:80], admission_result.explanation,
                )
                await self._emit_event(session, "fact_rejected", {
                    "content": input.content[:200],
                    "source": input.source,
                    "scores": admission_result.scores,
                    "composite_score": admission_result.composite_score,
                })
                return FactRejected(
                    content=input.content,
                    composite_score=admission_result.composite_score,
                    threshold=admission_result.threshold,
                    scores=admission_result.scores,
                    explanation=admission_result.explanation,
                )
```

Then in the fact creation block (line 169), add `admission_score`:

```python
        fact = Fact(
            agent_id=self.agent_id,
            content=input.content,
            category=input.category,
            subject=input.subject,
            confidence=input.confidence,
            source=input.source,
            source_episode_id=input.source_episode_id,
            source_decision_id=input.source_decision_id,
            contradiction_of=input.contradiction_of,
            tags=input.tags or None,
            embedding=embedding,
            encoded_frame=encoded_frame,
            encoded_censors=encoded_censors,
            admission_score=admission_result.composite_score if admission_result else None,
        )
```

- [ ] **Step 4: Update learn() public method return type**

In `nous/heart/facts.py`, change `learn()` (line 110) return type:

```python
    ) -> FactDetail | FactRejected:
```

- [ ] **Step 5: Update _supersede to bypass gate**

In `nous/heart/facts.py`, in `_supersede` (line 472), replace the `_learn` call. **Keep the rest of the method body intact** — only patch the call site:

Replace line 472:
```python
        new_detail = await self._learn(new_fact, [old_fact_id], False, session)
```
With:
```python
        # F023: Bypass admission gate for intentional replacements
        bypass_input = new_fact.model_copy(update={"source": "supersede"})
        new_detail = await self._learn(bypass_input, [old_fact_id], False, session)
        if isinstance(new_detail, FactRejected):
            raise RuntimeError("Supersede bypass failed — admission should not reject bypassed sources")
```

All subsequent lines (`old_fact.superseded_by = ...`, graph edge, event emission) remain unchanged.

- [ ] **Step 6: Update _contradict to bypass gate**

In `nous/heart/facts.py`, in `_contradict` (line 525), replace the `_learn` call. **Keep the rest of the method body intact:**

Replace line 525:
```python
        new_detail = await self._learn(contradicting_fact, [fact_id], False, session)
```
With:
```python
        # F023: Bypass admission gate for intentional contradictions
        bypass_input = contradicting_fact.model_copy(update={"source": "contradict"})
        new_detail = await self._learn(bypass_input, [fact_id], False, session)
        if isinstance(new_detail, FactRejected):
            raise RuntimeError("Contradict bypass failed — admission should not reject bypassed sources")
```

All subsequent lines (`new_fact_orm.contradiction_of = ...`, graph edge, confidence reduction) remain unchanged.

- [ ] **Step 7: Update Heart.__init__ to construct controller**

In `nous/heart/heart.py`, update `__init__` (lines 62-87):

Add import near the top:

```python
from nous.heart.admission import AdmissionConfig, AdmissionController
```

Update FactManager construction (line 77):

```python
        # F023: Construct admission controller if enabled
        admission_controller = None
        if settings.admission_control_enabled:
            admission_config = AdmissionConfig(
                weights={
                    "utility": settings.admission_w_utility,
                    "confidence": settings.admission_w_confidence,
                    "novelty": settings.admission_w_novelty,
                    "recency": settings.admission_w_recency,
                    "type_prior": settings.admission_w_type_prior,
                },
                threshold=settings.admission_threshold,
                recency_lambda=settings.admission_recency_lambda,
                utility_llm_enabled=settings.admission_utility_llm_enabled,
                utility_llm_model=settings.admission_utility_model or settings.background_model,
                shadow_mode=settings.admission_shadow_mode,
            )
            # LLM client injected post-init (same pattern as EventBus)
            admission_controller = AdmissionController(config=admission_config)

        self.facts = FactManager(database, embedding_provider, settings.agent_id, admission_controller)
```

- [ ] **Step 8: Update Heart.learn() return type**

In `nous/heart/heart.py` (line 209), update return type:

```python
from nous.heart.schemas import FactRejected
...
    ) -> FactDetail | FactRejected:
```

Also update the body — add a guard **immediately after** the `result = await self.facts.learn(...)` call (line 223) and **before** the `if self._bus is not None:` block (line 227). This prevents accessing `result.id` on a FactRejected which has no `id` attribute:

```python
        result = await self.facts.learn(
            input,
            session=session,
            encoded_frame=encoded_frame,
            encoded_censors=encoded_censors,
        )

        # F023: Skip event emission for rejected facts (FactRejected has no .id)
        if isinstance(result, FactRejected):
            return result

        # F022 Phase 2: Emit on in-process EventBus for cross-type graph linking.
        # (existing code continues unchanged from here)
```

**Critical:** Without this guard, `result.id` on line ~232 would crash with `AttributeError` when the gate rejects a fact and `self._bus` is not None.

- [ ] **Step 9: Commit**

```bash
git add nous/heart/facts.py nous/heart/heart.py
git commit -m "feat(f023): wire AdmissionController into FactManager._learn()"
```

---

### Task 10: learn_fact Tool Update

**Files:**
- Modify: `nous/api/tools.py:211-283`

- [ ] **Step 1: Set source="user_direct" and handle FactRejected**

In `nous/api/tools.py`, update the `learn_fact` function:

Add import:

```python
from nous.heart.schemas import FactRejected
```

Update FactInput construction (line 241) to set source:

```python
            input_data = FactInput(
                content=content,
                category=category,
                subject=subject,
                confidence=confidence,
                source="user_direct",  # F023: Always bypass admission gate for user tool calls
                source_episode_id=episode_uuid,
                source_decision_id=decision_uuid,
                tags=tags or [],
            )
```

After `result = await heart.learn(input_data)` (line 253), add FactRejected handling:

```python
            result = await heart.learn(input_data)

            # F023: Handle rejected facts (should not happen with user_direct bypass,
            # but handle gracefully in case bypass list changes)
            if isinstance(result, FactRejected):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Fact not stored (admission score {result.composite_score:.2f} "
                                f"< {result.threshold} threshold).\n"
                                f"Scores: {', '.join(f'{k}={v:.2f}' for k, v in result.scores.items())}\n"
                                f"Override with explicit instruction if this should be stored."
                            ),
                        }
                    ]
                }
```

- [ ] **Step 2: Commit**

```bash
git add nous/api/tools.py
git commit -m "feat(f023): learn_fact tool bypasses admission gate, handles FactRejected"
```

---

### Task 11: Handler Updates

**Files:**
- Modify: `nous/handlers/knowledge_extractor.py:129-137`
- Modify: `nous/handlers/fact_extractor.py:126-134`, `nous/handlers/fact_extractor.py:159-165`

- [ ] **Step 1: Update KnowledgeExtractor**

In `nous/handlers/knowledge_extractor.py`, add import and update the learn call (around line 129):

```python
from nous.heart.schemas import FactRejected
```

Update the FactInput construction and learn call (lines 129-137):

```python
                fact_input = FactInput(
                    subject=fact.get("subject", "unknown"),
                    content=content,
                    source="knowledge_extractor",
                    confidence=confidence,
                    category=fact.get("category"),
                    source_timestamp=event.data.get("episode_timestamp"),  # F023: recency accuracy
                )
                result = await self._heart.learn(fact_input)
                # F023: Don't count rejected facts as stored
                if isinstance(result, FactRejected):
                    logger.debug("Admission rejected knowledge fact: %s", content[:50])
                    continue
                stored += 1
```

- [ ] **Step 2: Update FactExtractor**

In `nous/handlers/fact_extractor.py`, add import:

```python
from nous.heart.schemas import FactRejected
```

Update the `_extract_and_store` path (lines 126-134):

```python
                result = await self._heart.learn(fact_input)
                if isinstance(result, FactRejected):
                    logger.debug("Admission rejected extracted fact: %s", content[:50])
                    continue
                stored += 1
```

Update `_store_candidate_facts` (lines 159-165):

```python
            result = await self._heart.learn(fact_input)
            if isinstance(result, FactRejected):
                logger.debug("Admission rejected candidate fact: %s", fact_text[:50])
                continue
            stored += 1
```

- [ ] **Step 3: Update SleepHandler**

In `nous/handlers/sleep_handler.py`, find the reflection phase where it calls `heart.learn()`. Add import:

```python
from nous.heart.schemas import FactRejected
```

Where sleep reflection calls `await self._heart.learn(fact_input)`, capture the result and log rejections:

```python
                result = await self._heart.learn(fact_input)
                if isinstance(result, FactRejected):
                    logger.debug("Admission rejected sleep-reflected fact: %s", fact_input.content[:50])
                    continue
```

- [ ] **Step 4: Commit**

```bash
git add nous/handlers/knowledge_extractor.py nous/handlers/fact_extractor.py nous/handlers/sleep_handler.py
git commit -m "feat(f023): handlers handle FactRejected from admission gate"
```

---

### Task 12: LLM Client Injection

**Files:**
- Modify: `nous/heart/heart.py` or `nous/main.py`

The AdmissionController needs an LLM client for utility scoring. Check how the existing LLM client is structured in the codebase (runner/httpx pattern) and inject it.

- [ ] **Step 1: Check existing LLM client pattern**

Read `nous/api/runner.py` and `nous/main.py` to understand how LLM clients are created and injected. The AdmissionController's `llm_client` needs a `.complete(model, prompt, max_tokens)` method.

- [ ] **Step 2: Create a lightweight LLM wrapper or inject post-init**

If the existing LLM client has a compatible interface, inject it. If not, create a minimal adapter in `nous/heart/admission.py`:

```python
class AdmissionLLMClient:
    """Minimal LLM client for admission utility scoring.

    Supports both API key (x-api-key) and OAT token (Bearer auth) patterns,
    matching the dual-auth approach used in runner.py.
    """

    def __init__(
        self,
        http_client,
        api_key: str = "",
        auth_token: str = "",
        api_base_url: str = "https://api.anthropic.com",
    ):
        self._http = http_client
        self._api_key = api_key
        self._auth_token = auth_token
        self._api_base_url = api_base_url

    def _headers(self) -> dict[str, str]:
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if self._auth_token:
            # OAT/Max subscription: Bearer auth
            headers["authorization"] = f"Bearer {self._auth_token}"
        elif self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    async def complete(self, model: str, prompt: str, max_tokens: int = 10) -> str:
        """Single-turn completion. Returns raw text response."""
        response = await self._http.post(
            f"{self._api_base_url}/v1/messages",
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers=self._headers(),
        )
        response.raise_for_status()
        data = response.json()
        return data["content"][0]["text"]
```

Then inject in `main.py` after Heart is created (following the EventBus post-init pattern):

```python
# F023: Inject LLM client into admission controller
if heart.facts._admission_controller is not None:
    import httpx
    llm_client = AdmissionLLMClient(
        http_client=httpx.AsyncClient(http2=True),
        api_key=settings.anthropic_api_key,
        auth_token=settings.anthropic_auth_token,
        api_base_url=settings.api_base_url,
    )
    heart.facts._admission_controller.llm_client = llm_client
```

- [ ] **Step 3: Commit**

```bash
git add nous/heart/admission.py nous/main.py
git commit -m "feat(f023): inject LLM client for admission utility scoring"
```

---

### Task 13: Integration Tests

**Files:**
- Create: `tests/test_admission_integration.py`

- [ ] **Step 1: Write integration tests**

Create `tests/test_admission_integration.py`:

```python
"""Integration tests for F023 Memory Admission Control.

Tests the full flow through FactManager._learn() with real Postgres.
Uses shadow mode and active mode to verify admission gate behavior.
"""

import pytest

from nous.heart.admission import AdmissionConfig, AdmissionController
from nous.heart.schemas import FactDetail, FactInput, FactRejected


def _fact(**overrides) -> FactInput:
    defaults = dict(
        content="Tim prefers dark mode in his IDE for reduced eye strain",
        category="preference",
        subject="Tim",
        confidence=0.9,
        source="fact_extractor",
        tags=["preference", "ide"],
    )
    defaults.update(overrides)
    return FactInput(**defaults)


@pytest.fixture
def active_controller():
    """Controller with shadow mode OFF and LLM disabled (heuristic only)."""
    config = AdmissionConfig(
        shadow_mode=False,
        utility_llm_enabled=False,
    )
    return AdmissionController(config=config)


@pytest.fixture
def shadow_controller():
    """Controller with shadow mode ON and LLM disabled."""
    config = AdmissionConfig(
        shadow_mode=True,
        utility_llm_enabled=False,
    )
    return AdmissionController(config=config)


@pytest.fixture
def strict_controller():
    """Controller with high threshold — rejects most facts."""
    config = AdmissionConfig(
        shadow_mode=False,
        utility_llm_enabled=False,
        threshold=0.99,
    )
    return AdmissionController(config=config)


# ---------------------------------------------------------------------------
# Admission via Heart
# ---------------------------------------------------------------------------


async def test_admitted_fact_stored(heart_with_admission, session):
    """High-quality fact should be stored with admission_score."""
    result = await heart_with_admission.learn(
        _fact(source="fact_extractor"),
        session=session,
    )
    assert isinstance(result, FactDetail)
    assert result.active is True


async def test_rejected_fact_not_stored(heart_with_strict_admission, session):
    """Low-quality fact with strict threshold should be rejected."""
    result = await heart_with_strict_admission.learn(
        _fact(
            content="ok",
            category=None,
            subject=None,
            confidence=0.3,
            source="sleep_reflection",
            tags=[],
        ),
        session=session,
    )
    assert isinstance(result, FactRejected)
    assert result.admitted is False
    assert result.composite_score < 0.99


async def test_user_direct_bypasses_strict_gate(heart_with_strict_admission, session):
    """User-invoked learn_fact always bypasses gate."""
    result = await heart_with_strict_admission.learn(
        _fact(source="user_direct"),
        session=session,
    )
    assert isinstance(result, FactDetail)


async def test_shadow_mode_admits_all(heart_with_shadow_admission, session):
    """Shadow mode stores all facts regardless of score."""
    result = await heart_with_shadow_admission.learn(
        _fact(
            content="ok",
            category=None,
            subject=None,
            confidence=0.1,
            source="sleep_reflection",
            tags=[],
        ),
        session=session,
    )
    assert isinstance(result, FactDetail)


async def test_disabled_admission_no_score(heart, session):
    """With no controller, facts stored with admission_score=None."""
    result = await heart.learn(
        _fact(source="fact_extractor"),
        session=session,
    )
    assert isinstance(result, FactDetail)


async def test_supersede_bypasses_gate(heart_with_strict_admission, session):
    """Supersede should bypass the admission gate."""
    # First, store an original fact via bypass
    original = await heart_with_strict_admission.learn(
        _fact(source="user_direct", content="Tim prefers light mode"),
        session=session,
    )
    assert isinstance(original, FactDetail)

    # Supersede with a new fact — should bypass the strict gate
    new_result = await heart_with_strict_admission.facts.supersede(
        original.id,
        FactInput(
            content="Tim prefers dark mode",
            category="preference",
            subject="Tim",
            source="fact_extractor",
        ),
        session=session,
    )
    assert isinstance(new_result, FactDetail)
```

Note: The `heart_with_admission`, `heart_with_strict_admission`, and `heart_with_shadow_admission` fixtures need to be added to `tests/conftest.py`. They follow the existing `heart` fixture pattern (uses `db` and `mock_embeddings` fixture names).

- [ ] **Step 2: Add test fixtures to conftest.py**

Add to `tests/conftest.py` (after the existing `heart` fixture at line ~110):

```python
from nous.heart.admission import AdmissionConfig, AdmissionController

@pytest_asyncio.fixture
async def heart_with_admission(db, mock_embeddings):
    """Heart with active admission control (LLM disabled, heuristic only)."""
    from nous.heart import Heart
    config = AdmissionConfig(shadow_mode=False, utility_llm_enabled=False)
    controller = AdmissionController(config=config)
    settings = Settings()
    h = Heart(db, settings, embedding_provider=mock_embeddings)
    h.facts._admission_controller = controller
    yield h
    await h.close()

@pytest_asyncio.fixture
async def heart_with_strict_admission(db, mock_embeddings):
    """Heart with strict admission (threshold=0.99)."""
    from nous.heart import Heart
    config = AdmissionConfig(shadow_mode=False, utility_llm_enabled=False, threshold=0.99)
    controller = AdmissionController(config=config)
    settings = Settings()
    h = Heart(db, settings, embedding_provider=mock_embeddings)
    h.facts._admission_controller = controller
    yield h
    await h.close()

@pytest_asyncio.fixture
async def heart_with_shadow_admission(db, mock_embeddings):
    """Heart with shadow mode admission."""
    from nous.heart import Heart
    config = AdmissionConfig(shadow_mode=True, utility_llm_enabled=False)
    controller = AdmissionController(config=config)
    settings = Settings()
    h = Heart(db, settings, embedding_provider=mock_embeddings)
    h.facts._admission_controller = controller
    yield h
    await h.close()
```

- [ ] **Step 3: Run integration tests**

Run: `uv run pytest tests/test_admission_integration.py -v`
Expected: ALL PASS

- [ ] **Step 4: Run full test suite to verify no regressions**

Run: `uv run pytest tests/ -v --timeout=120`
Expected: ALL PASS (existing tests unaffected since admission_controller=None by default)

- [ ] **Step 5: Commit**

```bash
git add tests/test_admission_integration.py tests/conftest.py
git commit -m "test(f023): integration tests for admission gate through FactManager"
```

---

### Task 14: Final Verification and PR-Ready Commit

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/ -v --timeout=120`
Expected: ALL PASS

- [ ] **Step 2: Verify migration on fresh database**

Run: `docker compose down -v && docker compose up -d postgres && sleep 3 && uv run python -c "import asyncio; from nous.main import main; asyncio.run(main())"`
Expected: Startup succeeds, migration 017 applied

- [ ] **Step 3: Verify shadow mode default**

Run: `uv run python -c "from nous.config import Settings; s=Settings(); print('shadow_mode:', s.admission_shadow_mode)"`
Expected: `shadow_mode: True`

- [ ] **Step 4: Review all changes**

Run: `git diff main --stat`
Verify:
- New files: `nous/heart/admission.py`, `sql/migrations/017_memory_admission_control.sql`, `tests/test_admission.py`, `tests/test_admission_integration.py`
- Modified: `nous/config.py`, `nous/storage/models.py`, `nous/heart/schemas.py`, `nous/heart/facts.py`, `nous/heart/heart.py`, `nous/api/tools.py`, `nous/handlers/knowledge_extractor.py`, `nous/handlers/fact_extractor.py`, `tests/conftest.py`

- [ ] **Step 5: Create feature branch and PR**

```bash
git checkout -b feat/f023-memory-admission-control
git push -u origin feat/f023-memory-admission-control
```
