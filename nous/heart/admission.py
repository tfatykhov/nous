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
            "identity",
            "censor",
            "supersede",
            "contradict",
        ]
    )
    utility_llm_enabled: bool = True
    utility_llm_model: str = ""  # Empty = use background_model fallback
    shadow_mode: bool = False


class AdmissionLLMClient:
    """LLM client adapter for admission utility scoring.

    Wraps a shared AnthropicClient (same one used by runner and handlers)
    to provide the simple complete(model, prompt, max_tokens) interface
    that AdmissionController expects.
    """

    def __init__(self, api_client=None):
        self._client = api_client

    async def complete(self, model: str, prompt: str, max_tokens: int = 10) -> str:
        """Single-turn completion. Returns raw text response."""
        if not self._client:
            raise RuntimeError("No API client configured for admission LLM")

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": "You are Claude Code, Anthropic's official CLI for Claude.",
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": "You are scoring the utility of a candidate fact for an AI agent's long-term memory.",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        }

        response = await self._client.call(payload)
        for block in response.content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
        return ""


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
        """Exponential time-decay: e^(-lambda * hours_since_source).

        lambda = 0.01/hour -> half-life ~ 69 hours (~3 days).
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

    # ------------------------------------------------------------------
    # Utility scoring
    # ------------------------------------------------------------------

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
- "Tim's birthday is March 15" -> 0.9 (personal, reusable, specific)
- "Tim mentioned he had a busy week" -> 0.3 (vague, ephemeral, no future use)
- "The meeting went well" -> 0.2 (subjective, no actionable content)
- "Tim prefers Celsius for temperatures" -> 0.95 (preference, used every time)
- "It was raining during our conversation" -> 0.1 (irrelevant ephemeral context)

Fact: {fact_input.content}
Category: {fact_input.category or "unknown"}
Subject: {fact_input.subject or "none"}

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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

        # F038-2.4: Scoring bonus for agent-initiated facts (replaces bypass)
        if fact_input.source == "user_direct":
            composite = min(1.0, composite + 0.15)

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
