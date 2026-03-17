# F023 — Memory Admission Control (A-MAC)

> **Status:** Draft v3 (post-Emerson review)
> **Priority:** P1
> **Depends on:** F002 (Heart Module), F022 (Graph-Augmented Recall)
> **Research:** 016 — Agent Memory Synthesis, Gap G3 (Admission Control)
> **Papers:** Zhang et al. (2026) "Adaptive Memory Admission Control for LLM Agents" (arXiv:2603.04549), ACC (Admission-Controlled Caching), A-MEM (Zettelkasten), Memory Survey (Hatalis et al., 2025)
> **Review:** 3-agent review completed Mar 17 2026. Research reviewer scored 6.5/10 on v1 draft. Emerson review on PR #155: Approve with minor revisions (0 P1, 3 P2, 3 P3). All findings incorporated in v3.

---

## Problem Statement

Nous stores facts too aggressively. Every extraction pipeline — `KnowledgeExtractor`, `FactExtractor`, `EpisodeSummarizer`, sleep reflection — creates facts with no quality gate beyond deduplication.

**What exists today:**

Three gates in `FactManager._learn()`:
- **Dedup** — cosine similarity > 0.95 → confirm existing fact, don't create new one
- **Contradiction** — similarity 0.85–0.95 → warn, but still store the new fact
- **Subject supersession** — same subject + similarity > 0.80 → retire older version

**What's missing:**
- No assessment of whether a fact is _worth storing_ in the first place
- No evaluation of future utility — will this be useful later?
- No novelty check beyond exact dedup — similar-but-not-identical facts accumulate
- No hallucination grounding — LLM-inferred facts not verified against source text
- No category-aware priors — an ephemeral observation stored same as a hard rule

**Result:** Memory noise increases over time. `recall_deep` returns low-value facts that consume context window tokens but don't help reasoning. The agent pays an "Agency Tax" — increased latency and reduced accuracy from processing irrelevant memories during retrieval.

**Current fact count trajectory:** With ~2–5 facts stored per conversation across `KnowledgeExtractor` + `FactExtractor` + sleep reflection, fact count grows linearly with usage. No natural pruning occurs (Phase 2/3 of sleep are stubs). At current rates, noise will degrade recall quality within weeks.

---

## Research Backing

**Zhang et al. (2026) — "Adaptive Memory Admission Control for LLM Agents":** The most directly relevant paper. Published March 2026, proposes an identical 5-dimension scoring framework. Benchmarked on LoCoMo dataset achieving F1=0.583. Key findings that shaped this spec:
- Type prior is the most influential dimension (ablation shows -0.107 F1 drop when removed)
- Utility scoring requires at least one LLM call — heuristics alone are insufficient
- Confidence should measure hallucination grounding (ROUGE-L against source), not just extractor certainty
- Recency should use exponential time-decay, not categorical lookup
- Optimal threshold found via grid search over [0.3, 0.6], landing at 0.55

**ACC (Admission-Controlled Caching):** Treats memory admission as a structured decision problem. Every candidate memory is scored across interpretable dimensions. Only memories exceeding a composite threshold are promoted to long-term storage. Key insight: "The cost of storing a low-value memory is not just storage — it's retrieval pollution."

**A-MEM (Zettelkasten):** When encoding a new memory, the agent evaluates its connections to existing knowledge. Isolated, unconnected memories are less valuable. This informs our novelty dimension — facts that connect to nothing existing are either truly novel (high value) or noise (low value), and confidence helps disambiguate.

**Memory Survey (Hatalis et al., 2025):** Identifies "memory bloat" as a primary failure mode in long-running agents. Recommends admission scoring with temporal decay and utility prediction.

**MemoryBank (Zhong et al., 2024):** Confirms exponential time-decay for recency scoring. Uses Ebbinghaus forgetting curve as biological grounding.

---

## Design Principles

1. **Gate, not rank** — Binary admit/reject at a threshold, not "pick top N." We're filtering noise, not rationing storage.
2. **Scores persist** — Every fact stores its admission score. Sleep consolidation can re-evaluate old facts with updated weights.
3. **User bypass** — Explicit `learn_fact` tool calls (user said "remember this") always pass. The gate only applies to automated extraction.
4. **Transparent** — Rejection reasons are logged with per-dimension scores. Tuning is data-driven, not guesswork.
5. **Configurable** — Weights and threshold are settings, not hardcoded. Can tune without deploys.
6. **Grounded** — Confidence measures whether a fact is actually supported by source text, not just how certain the extractor felt. Prevents hallucination propagation.
7. **Research-aligned** — Dimension implementations follow Zhang et al. (2026) validated approach, adapted for Nous's architecture.
8. **Observable from day one** — Shadow mode enables data collection before the gate enforces. Every score is logged whether or not the gate is active.

---

## Architecture

### New File: `nous/nous/heart/admission.py`

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

from sqlalchemy.ext.asyncio import AsyncSession

from nous.config import Settings
from nous.heart.schemas import FactInput

logger = logging.getLogger(__name__)


# Category-based priors: how likely is a fact in this category to be useful?
# Type prior is the MOST influential dimension per Zhang et al. ablation study.
DEFAULT_TYPE_PRIORS = {
    "rule": 0.95,        # User directives — almost always admit
    "preference": 0.90,  # User preferences — high value, loaded every turn
    "person": 0.85,      # People facts — high value, loaded every turn
    "technical": 0.70,   # Architecture/implementation — useful when relevant
    "tool": 0.65,        # Tool behavior/config — useful when relevant
    "concept": 0.60,     # General knowledge — lower prior, needs strong other signals
}

# Updated weights per Zhang et al. ablation findings:
# type_prior is most influential → highest weight (0.30)
# utility uses LLM scoring → second highest (0.25)
# novelty is well-validated → third (0.20)
# confidence (grounding) matters but is binary-ish → fourth (0.15)
# recency is least discriminative for admission → lowest (0.10)
DEFAULT_WEIGHTS = {
    "utility": 0.25,
    "confidence": 0.15,
    "novelty": 0.20,
    "recency": 0.10,
    "type_prior": 0.30,
}

DEFAULT_THRESHOLD = 0.55

# Exponential decay parameter for recency scoring
# λ = 0.01/hour → half-life ≈ 69 hours (~3 days)
# Per A-MEM, MemoryBank, and Zhang et al. recommendations
RECENCY_DECAY_LAMBDA = 0.01


@dataclass
class AdmissionResult:
    """Result of admission scoring."""
    admitted: bool
    composite_score: float
    threshold: float
    scores: dict[str, float]  # Per-dimension scores
    explanation: str
    bypassed: bool = False    # True if user bypass applied
    shadow_mode: bool = False # True if gate is in shadow mode (admit all, but score)


@dataclass
class AdmissionConfig:
    """Tunable admission parameters."""
    weights: dict[str, float] = field(default_factory=lambda: DEFAULT_WEIGHTS.copy())
    type_priors: dict[str, float] = field(default_factory=lambda: DEFAULT_TYPE_PRIORS.copy())
    threshold: float = DEFAULT_THRESHOLD
    recency_lambda: float = RECENCY_DECAY_LAMBDA
    bypass_sources: list[str] = field(default_factory=lambda: [
        "user_stated",    # User explicitly said "remember this"
        "user_direct",    # learn_fact tool call
        "identity",       # Identity protocol facts
        "censor",         # Censor-derived facts
    ])
    # LLM utility scoring
    utility_llm_enabled: bool = True
    utility_llm_model: str = "haiku"  # Cheap model for utility prediction
    # Shadow mode: score everything but admit all — for baseline data collection
    shadow_mode: bool = False


class AdmissionController:
    """Evaluates candidate facts for admission to long-term memory.

    Scores across 5 dimensions (per Zhang et al., 2026):
    - Utility: Will this be useful in future turns? (LLM-scored)
    - Confidence: Is this grounded in source text? (ROUGE-L)
    - Novelty: How different is this from what we already know?
    - Recency: How fresh is the source context? (exponential decay)
    - Type Prior: Category-based prior probability of usefulness.

    Supports three modes via configuration:
    - Disabled: NOUS_ADMISSION_CONTROL_ENABLED=false → no scoring, no gating
    - Shadow:   NOUS_ADMISSION_SHADOW_MODE=true → score everything, admit all, log results
    - Active:   Both enabled, shadow off → score and gate (reject below threshold)
    """

    def __init__(self, config: AdmissionConfig | None = None, llm_client=None):
        self.config = config or AdmissionConfig()
        self.llm_client = llm_client  # For utility scoring

    async def score(
        self,
        fact_input: FactInput,
        embedding: list[float] | None,
        max_existing_similarity: float | None,
        source_text: str | None,
        session: AsyncSession,
    ) -> AdmissionResult:
        """Score a candidate fact for admission.

        Args:
            fact_input: The candidate fact.
            embedding: Pre-computed embedding (already available from _learn).
            max_existing_similarity: Highest cosine similarity to any existing
                fact (from the dedup query we already run). None if no embedding.
            source_text: Original conversation/episode text this fact was
                extracted from. Used for ROUGE-L grounding. None if unavailable.
                NOTE: This MUST include tool call outputs (web_search results,
                run_python output, etc.) not just user/assistant messages.
                See _get_source_text() for implementation details.
            session: DB session (for future expansion, e.g., subject frequency).

        Returns:
            AdmissionResult with per-dimension scores and admit/reject decision.
        """
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

        # 1. Utility — LLM-scored future usefulness prediction
        scores["utility"] = await self._score_utility(fact_input)

        # 2. Confidence — ROUGE-L grounding against source text
        scores["confidence"] = self._score_confidence(fact_input, source_text)

        # 3. Novelty — inverse of max similarity to existing facts
        scores["novelty"] = self._score_novelty(max_existing_similarity)

        # 4. Recency — exponential time-decay
        scores["recency"] = self._score_recency(fact_input)

        # 5. Type Prior — category-based prior
        scores["type_prior"] = self._score_type_prior(fact_input)

        # Composite
        w = self.config.weights
        composite = (
            w["utility"] * scores["utility"]
            + w["confidence"] * scores["confidence"]
            + w["novelty"] * scores["novelty"]
            + w["recency"] * scores["recency"]
            + w["type_prior"] * scores["type_prior"]
        )

        # In shadow mode: always admit, but log what would have been rejected
        if self.config.shadow_mode:
            would_reject = composite < self.config.threshold
            dims = ", ".join(f"{k}={v:.2f}" for k, v in scores.items())
            action = "SHADOW_WOULD_REJECT" if would_reject else "SHADOW_WOULD_ADMIT"
            explanation = f"{action} (score={composite:.3f}, threshold={self.config.threshold}): {dims}"
            logger.info("Admission shadow: %s — %s", fact_input.content[:80], explanation)
            return AdmissionResult(
                admitted=True,  # Always admit in shadow mode
                composite_score=composite,
                threshold=self.config.threshold,
                scores=scores,
                explanation=explanation,
                shadow_mode=True,
            )

        admitted = composite >= self.config.threshold

        # Build explanation
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

    async def _score_utility(self, fact_input: FactInput) -> float:
        """Predict future utility of this fact using LLM scoring.

        Per Zhang et al. (2026): heuristics alone are insufficient for utility.
        A single cheap LLM call asking "will this be useful in future conversations?"
        provides significantly better signal.

        At Nous's volume (2-5 facts/conversation), one Haiku call per candidate
        is negligible in cost and latency (~100ms, ~$0.0001).

        Falls back to heuristic scoring if LLM is unavailable.
        """
        if self.config.utility_llm_enabled and self.llm_client:
            try:
                return await self._llm_utility_score(fact_input)
            except Exception as e:
                logger.warning("LLM utility scoring failed, falling back to heuristic: %s", e)

        # Heuristic fallback
        return self._heuristic_utility_score(fact_input)

    async def _llm_utility_score(self, fact_input: FactInput) -> float:
        """Score utility via LLM call.

        Prompt asks the model to rate 0.0-1.0 how likely this fact
        is to be useful in future conversations with this user.

        Includes calibration anchors per Emerson review P3 #1 —
        without anchored examples, scores drift across sessions.
        """
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
            return 0.5  # Neutral fallback

    def _heuristic_utility_score(self, fact_input: FactInput) -> float:
        """Heuristic fallback for utility scoring.

        Used when LLM is unavailable. Less accurate but zero-latency.
        """
        score = 0.5  # baseline

        if fact_input.subject:
            score += 0.15  # Subject makes it retrievable

        if fact_input.tags:
            score += 0.05 * min(len(fact_input.tags), 3)  # Tags help discovery

        content_len = len(fact_input.content)
        if content_len < 20:
            score -= 0.2  # Too short — probably noise
        elif content_len > 500:
            score -= 0.1  # Too long — probably not a fact
        elif 50 <= content_len <= 300:
            score += 0.1  # Sweet spot

        if fact_input.source_decision_id:
            score += 0.1  # Linked to a decision = more connected

        if fact_input.source_episode_id:
            score += 0.05  # Linked to an episode = has provenance

        return max(0.0, min(1.0, score))

    def _score_confidence(self, fact_input: FactInput, source_text: str | None) -> float:
        """Score based on hallucination grounding — is this fact actually supported?

        Per Zhang et al. (2026) and research review: confidence should measure
        whether the fact content is grounded in the source text, NOT just how
        certain the extractor felt. This catches hallucinated facts — things
        the LLM inferred but nobody actually said.

        Uses ROUGE-L overlap between fact content and source text.
        Falls back to source-penalty heuristic if source text unavailable.

        NOTE on source_text scope (Emerson review P2 #1):
        source_text MUST include tool call outputs (web_search results,
        run_python output, bash output, web_fetch content) — not just
        user/assistant message text. Facts derived from web research
        or code execution need their tool outputs in the grounding window,
        otherwise ROUGE-L will incorrectly flag them as hallucinations.
        See _get_source_text() for how episode.content is structured.
        """
        if source_text:
            return self._rouge_l_score(fact_input.content, source_text)

        # Fallback: source credibility penalties when source text unavailable
        base = fact_input.confidence

        source_penalties = {
            "knowledge_extractor": 0.10,
            "episode_summarizer": 0.05,
            "sleep_reflection": 0.15,
            "compaction_extraction": 0.10,
        }

        penalty = source_penalties.get(fact_input.source or "", 0.0)
        return max(0.0, min(1.0, base - penalty))

    def _rouge_l_score(self, fact_text: str, source_text: str) -> float:
        """Compute ROUGE-L F1 score between fact and source text.

        ROUGE-L measures longest common subsequence overlap.
        High score = fact is well-grounded in source.
        Low score = fact may be hallucinated or heavily inferred.

        Lightweight implementation — no external dependencies.
        Uses whitespace tokenization (sufficient for our scale;
        proper tokenizer not worth the dependency — see Open Questions #4).
        """
        fact_tokens = fact_text.lower().split()
        source_tokens = source_text.lower().split()

        if not fact_tokens or not source_tokens:
            return 0.5  # Can't assess — neutral

        # LCS length via dynamic programming
        lcs_len = self._lcs_length(fact_tokens, source_tokens)

        # Precision: what fraction of fact tokens appear in LCS
        precision = lcs_len / len(fact_tokens) if fact_tokens else 0

        # Recall: what fraction of source tokens appear in LCS
        recall = lcs_len / len(source_tokens) if source_tokens else 0

        # F1
        if precision + recall == 0:
            return 0.0

        f1 = 2 * precision * recall / (precision + recall)
        return f1

    def _lcs_length(self, x: list[str], y: list[str]) -> int:
        """Compute length of longest common subsequence.

        Optimized to O(min(m,n)) space.
        For typical fact (10-50 tokens) vs source (100-2000 tokens), this is fast.
        """
        # Ensure x is the shorter sequence for space optimization
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

    def _score_novelty(self, max_existing_similarity: float | None) -> float:
        """Score based on how different this is from existing facts.

        If max_existing_similarity is None (no embedding), return neutral 0.5.
        If most similar existing fact is 0.90 similar, novelty = 0.10.
        If most similar is 0.50, novelty = 0.50.
        """
        if max_existing_similarity is None:
            return 0.5  # Can't assess — neutral

        # Raw novelty: 1 - similarity
        raw = 1.0 - max_existing_similarity

        # Scale: we want 0.0 similarity → 1.0 novelty, 0.95 → 0.05
        # But below dedup threshold (0.95), so range is 0.0–0.95 → 0.05–1.0
        return max(0.0, min(1.0, raw))

    def _score_recency(self, fact_input: FactInput) -> float:
        """Score based on temporal freshness using exponential decay.

        Per Zhang et al. (2026), A-MEM, and MemoryBank: recency should be
        time-based exponential decay, NOT categorical source lookup.

        Formula: recency = e^(-λ * hours_since_source)
        λ = 0.01/hour → half-life ≈ 69 hours (~3 days)

        For facts from the current conversation, hours_since_source ≈ 0 → score ≈ 1.0
        For facts from 3 days ago → score ≈ 0.5
        For facts from 1 week ago → score ≈ 0.19
        For facts from 2 weeks ago → score ≈ 0.035
        """
        now = datetime.now(UTC)

        # Use source_timestamp if provided on FactInput (added in PR 1)
        # This field is explicitly typed on the schema, not discovered via hasattr.
        source_time = fact_input.source_timestamp if fact_input.source_timestamp else None

        if source_time is None:
            # For facts being created right now from live conversation,
            # hours_elapsed ≈ 0, so score ≈ 1.0. This is correct behavior.
            # For sleep-extracted facts, the source episode may be old —
            # we should ideally pass the episode timestamp. For now,
            # use creation time (which is "now" for new facts).
            hours_elapsed = 0.0
        else:
            delta = now - source_time
            hours_elapsed = delta.total_seconds() / 3600.0

        score = math.exp(-self.config.recency_lambda * hours_elapsed)
        return max(0.0, min(1.0, score))

    def _score_type_prior(self, fact_input: FactInput) -> float:
        """Category-based prior probability of usefulness.

        Per Zhang et al. ablation study, this is the MOST influential dimension.
        Removing it causes the largest F1 drop (-0.107).
        Weight accordingly: 0.30 (highest).
        """
        if not fact_input.category:
            return 0.50  # Unknown category — neutral
        return self.config.type_priors.get(fact_input.category, 0.50)
```

### Schema Change: `FactInput` — Add `source_timestamp`

Per Emerson review P3 #2: the `hasattr` check for `source_timestamp` is fragile. The field must be explicitly typed on the schema so callers know it exists and can populate it.

```python
# In nous/heart/schemas.py — FactInput

class FactInput(BaseModel):
    content: str
    category: str | None = None
    subject: str | None = None
    confidence: float = 1.0
    source: str | None = None
    source_episode_id: UUID | None = None
    source_decision_id: UUID | None = None
    tags: list[str] | None = None
    # NEW: Timestamp of the source context this fact was extracted from.
    # Used by admission controller for accurate recency scoring.
    # For live conversation facts: None (defaults to now → recency ≈ 1.0)
    # For sleep-reflected facts: timestamp of the oldest source episode
    # For knowledge_extractor: timestamp of the compacted episode
    source_timestamp: datetime | None = None
```

### Integration Point: `FactManager._learn()`

The gate inserts **after** dedup check passes but **before** `session.add(fact)`:

```python
async def _learn(self, input, exclude_ids, check_contradictions, session, ...):
    # 1. Generate embedding (existing)
    embedding = await self.embeddings.embed(input.content)

    # 2. Dedup check (existing)
    dupe = await self._find_duplicate(embedding, exclude_ids, session)
    if dupe is not None:
        return await self._confirm(dupe.id, session)

    # 3. *** NEW: Admission gate ***
    # Initialize result to None before the conditional block
    # to avoid NameError when controller is disabled (Emerson P2 #3)
    admission_result: AdmissionResult | None = None

    if self._admission_controller is not None:
        # Reuse the similarity data from dedup search
        max_sim = await self._find_max_similarity(embedding, exclude_ids, session)

        # Retrieve source text for ROUGE-L grounding
        source_text = await self._get_source_text(input, session)

        admission_result = await self._admission_controller.score(
            input, embedding, max_sim, source_text, session
        )
        if not admission_result.admitted:
            logger.info("Fact rejected by admission: %s — %s",
                        input.content[:80], admission_result.explanation)
            await self._emit_event(session, "fact_rejected", {
                "content": input.content[:200],
                "source": input.source,
                "scores": admission_result.scores,
                "composite_score": admission_result.composite_score,
            })
            return self._rejected_detail(input, admission_result)

    # 4. Create and store fact (existing)
    fact = Fact(...)
    fact.admission_score = admission_result.composite_score if admission_result else None
    session.add(fact)
    ...
```

### New Method: `_get_source_text()`

```python
async def _get_source_text(
    self,
    fact_input: FactInput,
    session: AsyncSession,
) -> str | None:
    """Retrieve original source text for ROUGE-L grounding check.

    If the fact has a source_episode_id, fetch the episode's content.
    This is the conversation/summary text the fact was extracted from.

    IMPORTANT (Emerson review P2 #1): episode.content includes the FULL
    conversation log — user messages, assistant messages, AND tool call
    outputs (web_search results, run_python output, bash output, web_fetch
    content). This means facts derived from tool outputs (e.g., "Tim's
    server runs Ubuntu 24.04" learned from a bash command output) will
    have their source text available for ROUGE-L grounding.

    If the episode's content field does NOT include tool outputs (e.g.,
    due to content truncation or a format change), the ROUGE-L score
    will undercount grounding for tool-derived facts, and the fallback
    source-penalty heuristic will apply instead. This is a known
    limitation — see Open Questions #5.

    Returns None if no source episode available (user-stated facts, etc.).
    Cost: ~1ms (single row lookup by PK).
    """
    if not fact_input.source_episode_id:
        return None

    from nous.heart.models import Episode
    episode = await session.get(Episode, fact_input.source_episode_id)
    if episode and episode.content:
        return episode.content

    return None
```

### New Method: `_find_max_similarity()`

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
    Cost: ~2ms (single HNSW lookup, top-1).
    """
    if not embedding:
        return None

    embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
    params = {"embedding": embedding_str, "agent_id": self.agent_id}

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
```

### Return Type for Rejected Facts

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

The `learn_fact` tool handler checks the return type. If `FactRejected`, it returns a message like:
"Fact not stored (admission score 0.42 < 0.55 threshold). Scores: utility=0.50, confidence=0.60, novelty=0.15, recency=0.90, type_prior=0.60. Override with explicit instruction if this should be stored."

---

## Schema Changes

### Migration: Add `admission_score` to `heart.facts`

```sql
ALTER TABLE heart.facts
    ADD COLUMN admission_score FLOAT DEFAULT NULL;

COMMENT ON COLUMN heart.facts.admission_score IS
    'A-MAC composite score at time of admission. NULL for pre-F023 facts.';
```

### Migration: Add feedback loop columns to `heart.facts`

Per Emerson review P3 #3: the feedback loop columns were specified in the design
but the migration SQL was missing. Both columns are needed from PR 1 so the schema
is ready when PR 4 wires up the tracking logic.

```sql
ALTER TABLE heart.facts
    ADD COLUMN recall_count INTEGER DEFAULT 0,
    ADD COLUMN last_recalled_at TIMESTAMPTZ DEFAULT NULL;

COMMENT ON COLUMN heart.facts.recall_count IS
    'Number of times this fact was recalled and used in a response. For admission feedback loop.';
COMMENT ON COLUMN heart.facts.last_recalled_at IS
    'Last time this fact was recalled and used. For admission feedback loop and sleep pruning.';
```

No indexes needed on these columns — they're updated on write and read during sleep batch jobs, not queried directly.

---

## Integration Points

### 1. `FactManager.__init__()` — Inject Controller

```python
def __init__(self, db, embeddings, agent_id, admission_controller=None):
    ...
    self._admission_controller = admission_controller
```

Constructed in `Heart.__init__()` from settings:

```python
if settings.admission_control_enabled:
    admission_config = AdmissionConfig(
        weights=settings.admission_weights or DEFAULT_WEIGHTS,
        threshold=settings.admission_threshold or DEFAULT_THRESHOLD,
        recency_lambda=settings.admission_recency_lambda or RECENCY_DECAY_LAMBDA,
        utility_llm_enabled=settings.admission_utility_llm_enabled,
        shadow_mode=settings.admission_shadow_mode,
    )
    self._admission = AdmissionController(admission_config, llm_client=self.llm)
```

### 2. `KnowledgeExtractor` — Minor Change

The extractor calls `heart.learn()` which calls `FactManager._learn()`.
The gate fires automatically. The extractor should pass `source_episode_id` on FactInput so the admission controller can retrieve source text for ROUGE-L grounding. If extractor already has the source conversation text in memory, it can pass it directly via a new `source_text` field on FactInput to avoid the extra DB lookup.

**Must also populate `source_timestamp`** from the compacted episode's timestamp so recency scoring is accurate (not defaulting to "now").

### 3. `FactExtractor` (episode candidate facts) — No Changes Needed

Same path: calls `heart.learn()` → `_learn()` → admission gate fires.
Source episode ID is already available.

### 4. `EpisodeSummarizer` — No Changes Needed

Extracts `candidate_facts` and passes to `FactExtractor`. Gate fires downstream.

### 5. `SleepHandler` Phase 4 (Reflect) — Source Text Consideration

Reflection stores facts via `heart.learn()`. Gate fires. Sleep-reflected facts have source `"sleep_reflection"` and may not have direct source text (they're cross-session inferences). For these, the confidence dimension falls back to the source-penalty heuristic (-0.15), appropriately raising the bar for speculative inferences. The `source_timestamp` should be set to the oldest episode being reflected on, so recency decay is accurate.

### 6. `learn_fact` Tool (user-facing) — Bypass

The tool handler in `tools.py` sets `source="user_direct"` on the FactInput.
`"user_direct"` is in the bypass list → always admitted.

### 7. Sleep Phase 2 (Prune) — Re-scoring Integration

Once Phase 2 is implemented, it can re-score existing facts:
```python
# In sleep prune phase:
for fact in old_facts:
    # Re-score with current weights and time decay
    result = admission_controller.score(fact_input, fact.embedding, max_sim, source_text, session)
    if result.composite_score < config.prune_threshold:  # Lower than admission threshold
        fact.active = False
        logger.info("Pruned fact %s (score decayed to %.3f): %s",
                     fact.id, result.composite_score, fact.content[:80])
```

This enables "forgetting" — facts that were marginal at admission time and haven't been confirmed or recalled can be retired.

### 8. Sleep Phase 2 — Feedback Loop

Track which admitted facts are actually recalled and used vs. never touched:
```python
# On every recall_deep that returns facts used in a response:
for fact_id in facts_used_in_response:
    await session.execute(
        text("""UPDATE heart.facts
                SET recall_count = recall_count + 1, last_recalled_at = NOW()
                WHERE id = :id"""),
        {"id": fact_id}
    )
```

Sleep Phase 2 can then use `recall_count` and `last_recalled_at` as signals:
- Facts admitted 2+ weeks ago with `recall_count = 0` → candidates for pruning
- Facts with high recall counts → reinforce (could increase admission score)
- Over time, this creates data for weight optimization

---

## Configuration

### Settings (env vars or config)

```python
# Feature flag
NOUS_ADMISSION_CONTROL_ENABLED: bool = True

# Shadow mode: score everything but admit all — for baseline data collection
# Use this BEFORE enabling the gate to collect real scoring data and
# validate threshold/weights without risking fact loss (Emerson P2 #2)
NOUS_ADMISSION_SHADOW_MODE: bool = False

# Composite threshold — facts below this are rejected
# Zhang et al. grid search optimal: 0.55
NOUS_ADMISSION_THRESHOLD: float = 0.55

# Per-dimension weights (must sum to 1.0)
# Weights based on Zhang et al. (2026) ablation study
NOUS_ADMISSION_W_UTILITY: float = 0.25
NOUS_ADMISSION_W_CONFIDENCE: float = 0.15
NOUS_ADMISSION_W_NOVELTY: float = 0.20
NOUS_ADMISSION_W_RECENCY: float = 0.10
NOUS_ADMISSION_W_TYPE_PRIOR: float = 0.30  # Most influential per ablation

# Exponential decay for recency
# λ = 0.01/hr → half-life ≈ 69 hours (~3 days)
NOUS_ADMISSION_RECENCY_LAMBDA: float = 0.01

# LLM utility scoring
NOUS_ADMISSION_UTILITY_LLM_ENABLED: bool = True
NOUS_ADMISSION_UTILITY_LLM_MODEL: str = "haiku"

# Type priors (overridable per category)
NOUS_ADMISSION_PRIOR_RULE: float = 0.95
NOUS_ADMISSION_PRIOR_PREFERENCE: float = 0.90
NOUS_ADMISSION_PRIOR_PERSON: float = 0.85
NOUS_ADMISSION_PRIOR_TECHNICAL: float = 0.70
NOUS_ADMISSION_PRIOR_TOOL: float = 0.65
NOUS_ADMISSION_PRIOR_CONCEPT: float = 0.60
```

### Recommended Rollout Sequence

1. **Week 1:** Enable with `SHADOW_MODE=true`. Collect baseline data. Review score distributions.
2. **Week 2:** Run pre-launch benchmark (100-fact labeling). Validate threshold.
3. **Week 3:** Disable shadow mode (`SHADOW_MODE=false`). Gate is now active.
4. **Week 4+:** Monitor rejection rate. Tune weights if needed based on feedback loop data.

---

## Implementation Plan (4 PRs)

### PR 1 — Schema + AdmissionController class (~4 hours)
- New file: `nous/nous/heart/admission.py`
- `AdmissionController` class with all 5 scoring dimensions
  - LLM-based utility scoring with heuristic fallback
  - Calibration anchors in utility prompt (Emerson P3 #1)
  - ROUGE-L grounding for confidence with source-penalty fallback
  - Exponential time-decay for recency (λ=0.01/hr)
  - Type prior as highest-weighted dimension (0.30)
  - Shadow mode support (score all, admit all, log results)
- `AdmissionResult` and `AdmissionConfig` dataclasses
- ROUGE-L implementation (lightweight, no external deps)
- Add `source_timestamp: datetime | None = None` to `FactInput` schema (Emerson P3 #2)
- Alembic migration: add `admission_score`, `recall_count`, `last_recalled_at` columns to `heart.facts` (Emerson P3 #3)
- Unit tests: scoring logic, ROUGE-L accuracy, bypass behavior, decay math, edge cases, shadow mode
- **No behavior change** — controller exists but isn't wired in

### PR 2 — Wire into FactManager._learn() (~3 hours)
- Add `_find_max_similarity()` method to FactManager
- Add `_get_source_text()` method for ROUGE-L grounding (with tool output documentation — Emerson P2 #1)
- Inject `AdmissionController` + LLM client into FactManager constructor
- Gate logic in `_learn()`: score → admit/reject → emit event
  - `admission_result` initialized to `None` before conditional block (Emerson P2 #3)
- `FactRejected` return type for rejected facts
- Feature flag: `NOUS_ADMISSION_CONTROL_ENABLED`
- Shadow mode flag: `NOUS_ADMISSION_SHADOW_MODE` (Emerson P2 #2)
- Integration tests: verified admit, verified reject, bypass for user facts, ROUGE-L grounding, shadow mode logging

### PR 3 — Tool handler + observability (~2 hours)
- Update `learn_fact` tool handler to set `source="user_direct"`
- Handle `FactRejected` return in tool response (inform user, suggest override)
- Log `fact_rejected` events with full score breakdown
- Add admission score to fact detail responses
- Dashboard query: rejection rate, score distributions, per-source breakdown

### PR 4 — Feedback loop + sleep integration + benchmark (~3 hours)
- Wire up `recall_count` / `last_recalled_at` tracking on every recall_deep
- Config loading for weights, threshold, type priors from settings
- Sleep Phase 2 integration: re-score existing facts, deactivate decayed ones
- **Pre-launch benchmark**: label 100 existing facts as valuable/noise, run scorer, measure precision/recall. Target: F1 ≥ 0.55 (matching Zhang et al. LoCoMo baseline)
- Document tuning methodology

**Total estimated effort: ~12 hours across 4 PRs**

---

## Pre-Launch Evaluation Plan

Per research review recommendation, we define a concrete benchmark before launch:

### Internal Benchmark
1. **Sample**: Random 100 existing facts from `heart.facts`
2. **Label**: Manually classify each as "valuable" (would want to recall this) or "noise" (low value, clutters recall)
3. **Score**: Run admission controller on all 100, record composite scores
4. **Measure**: Precision, Recall, F1 at threshold=0.55
5. **Target**: F1 ≥ 0.55 (matching Zhang et al. LoCoMo baseline)
6. **Tune**: If below target, adjust weights/threshold via grid search over [0.3, 0.7]

### Ongoing Metrics
- Weekly rejection rate report (target: 25-40%)
- Monthly fact usage audit (recall_count distribution)
- Quarterly weight recalibration based on feedback loop data

---

## Metrics

### Success Criteria

1. **Rejection rate: 25–40%** of automatically extracted facts should be rejected
   - Below 25% = threshold too low, not filtering enough
   - Above 40% = threshold too high, losing valuable facts

2. **Pre-launch benchmark: F1 ≥ 0.55** on 100-fact manual labeling exercise
   - Validates threshold and weights before production deployment

3. **Precision of admits: >85%** — of admitted facts, >85% should be genuinely useful
   - Measured by: manual review of 50 random admitted facts after 1 week

4. **No user-stated facts rejected** — bypass must work 100%

5. **Recall quality improvement** — `recall_deep` results should have higher signal-to-noise
   - Measured by: track % of recalled facts that are actually referenced in responses

6. **Latency impact:**
   - Without LLM utility scoring: <10ms per fact admission
     - ROUGE-L: ~1ms (text comparison)
     - `_find_max_similarity()`: ~2ms (HNSW lookup)
     - `_get_source_text()`: ~1ms (PK lookup)
   - With LLM utility scoring: ~105ms per fact admission
     - Above + LLM call: ~100ms (async, Haiku)
   - At 2-5 facts/conversation, total added latency: 10-525ms spread across session

### Observability

- `fact_rejected` events with full score breakdowns
- `admission_score` persisted on every admitted fact
- `recall_count` and `last_recalled_at` for feedback loop
- Shadow mode logging: "SHADOW_WOULD_REJECT" / "SHADOW_WOULD_ADMIT" log lines
- Log line for every rejection: content preview + per-dimension scores
- Periodic report: admission rate by source, by category, score distributions

---

## Risks and Mitigations

**Threshold too aggressive — rejects valuable facts**
Impact: Lost knowledge
Mitigation: Start with shadow mode to collect baseline data. Run pre-launch benchmark at 0.55 (validated by Zhang et al.), tune based on results.

**Threshold too permissive — doesn't filter enough**
Impact: No improvement
Mitigation: Monitor rejection rate, tune down if <20%

**ROUGE-L grounding too strict on valid inferences**
Impact: Rejects legitimate inferred facts that aren't verbatim in source
Mitigation: ROUGE-L is one of 5 dimensions (weight 0.15). A fact can score low on grounding but still pass on utility + type_prior + novelty. Also, fallback to source-penalty heuristic when source text unavailable.

**Tool output missing from episode content (Emerson P2 #1)**
Impact: Facts derived from web research / code execution flagged as ungrounded
Mitigation: Document requirement that episode.content includes tool outputs. Add validation check in _get_source_text(). If tool outputs are truncated, fall back to source-penalty heuristic.

**LLM utility scoring adds latency**
Impact: ~100ms per fact admission
Mitigation: Use cheapest model (Haiku). At 2-5 facts/conversation, total added latency is 200-500ms spread across the session. Can disable via feature flag. Heuristic fallback always available.

**Novelty scoring penalizes legitimate updates**
Impact: Missed updates
Mitigation: Subject supersession runs before admission gate — updates bypass novelty

**Source text unavailable for ROUGE-L**
Impact: Confidence dimension less accurate
Mitigation: Graceful fallback to source-penalty heuristic. Most facts from knowledge_extractor and episode_summarizer have source_episode_id available.

**Breaking change for existing integrations**
Impact: Service disruption
Mitigation: Feature flag, shadow mode for data collection, gradual rollout, bypass for explicit user actions

---

## Open Questions

1. **Should admission scores decay over time?** A fact admitted at 0.70 six months ago may have decayed in utility. Sleep Phase 2 re-scoring handles this, but should the decay be automatic (time-based) or only via re-evaluation?

2. **Batch admission for KnowledgeExtractor?** The extractor produces up to 5 facts per compaction. Should they be scored as a batch (penalizing redundancy within the batch) or independently?

3. **Recency for sleep-reflected facts:** When sleep Phase 4 reflects on episodes from last week, should the recency score use the episode timestamp (old → low recency) or the reflection timestamp (now → high recency)? Current design: episode timestamp, which seems more honest — a reflection about old data shouldn't get a freshness bonus.

4. **ROUGE-L token normalization:** Current implementation uses whitespace splitting. Should we use a proper tokenizer (e.g., word_tokenize) for more accurate overlap measurement? Probably not worth the dependency at current scale.

5. **Tool output completeness in episode content (Emerson P2 #1):** Need to audit whether all episode types consistently include tool call outputs in their content field. If some episode types truncate or exclude tool outputs, facts derived from those tools will have artificially low ROUGE-L scores. May need a separate tool_output store or expanded episode content format.

---

## Non-Goals

- **Admission for decisions, episodes, procedures** — Only facts have noise problems. Decisions are already gated by the decision frame. Episodes are structural. Procedures are explicitly registered. Fact admission only.
- **Retroactive re-scoring of all existing facts** — Existing facts keep `admission_score = NULL`. Sleep Phase 2 will re-score gradually. No bulk migration.
- **User-facing admission settings** — Users don't configure weights. This is an internal quality gate. Users can always override by saying "remember this."
- **ML-based scoring model** — The heuristic+LLM approach is sufficient at current scale. If Nous reaches thousands of facts per day, a learned model might be warranted. Not now.
- **Cross-validation weight learning** — Zhang et al. uses 5-fold CV. We use the feedback loop for gradual weight adjustment instead. Simpler, continuous, production-appropriate.

---

## Changelog

- **v1 (Mar 17 2026):** Initial draft with heuristic-only scoring
- **v2 (Mar 17 2026):** Post-review revision incorporating Zhang et al. (2026) findings:
  - Utility: heuristic → LLM-scored with heuristic fallback
  - Confidence: source-penalty → ROUGE-L grounding with source-penalty fallback
  - Recency: categorical lookup → exponential time-decay (λ=0.01/hr)
  - Type prior weight: 0.20 → 0.30 (most influential per ablation)
  - Added feedback loop (recall_count tracking) in PR 4
  - Added pre-launch benchmark (100-fact labeling, target F1 ≥ 0.55)
  - Added ROUGE-L implementation details
  - Estimated effort: 9h → 12h (LLM integration + grounding + benchmark)
  - Cited Zhang et al. (2026) throughout
- **v3 (Mar 17 2026):** Post-Emerson review (PR #155) — all P2/P3 findings addressed:
  - P2 #1: Documented tool output requirement in _get_source_text() and ROUGE-L grounding. Added Open Question #5 for audit.
  - P2 #2: Added shadow mode (NOUS_ADMISSION_SHADOW_MODE). Three-state config: disabled → shadow → active. Added rollout sequence.
  - P2 #3: Fixed NameError — `admission_result` initialized to `None` before conditional block.
  - P3 #1: Added calibration anchors to LLM utility prompt (5 examples with scores).
  - P3 #2: Added `source_timestamp: datetime | None = None` as explicit field on FactInput schema. Removed fragile `hasattr` check.
  - P3 #3: Added migration SQL for `recall_count` and `last_recalled_at` columns.
  - Fixed latency contradiction: split into "<10ms without LLM / ~105ms with LLM" in success criteria.
  - Added Design Principle #8 (observable from day one).
  - Added recommended rollout sequence (shadow → benchmark → active → tune).
