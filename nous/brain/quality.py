"""Quality scoring for decisions based on metadata completeness."""

from __future__ import annotations


def compute_quality_score(
    tags: list[str],
    reasons: list[dict],
    pattern: str | None,
    context: str | None,
) -> float:
    """Score decision quality based on metadata completeness.

    Scoring:
    - Has tags (>=1):             +0.25
    - Has reasons (>=1):          +0.25
    - Has pattern:                +0.25
    - Has context:                +0.15
    - Reason diversity (>=2 types): +0.10

    Total possible: 1.0

    NOTE: this score is advisory (surfaced for human review on the decision
    detail / dashboard). Nothing gates on it — there is no block threshold.
    """
    score = 0.0

    if tags:
        score += 0.25

    if reasons:
        score += 0.25

    if pattern:
        score += 0.25

    if context:
        score += 0.15

    # Reason diversity: count unique reason types
    if reasons:
        unique_types = {r.get("type") for r in reasons if r.get("type")}
        if len(unique_types) >= 2:
            score += 0.10

    return score


class QualityScorer:
    """Wrapper class for quality scoring, used by Brain.__init__."""

    def compute(
        self,
        tags: list[str],
        reasons: list[dict],
        pattern: str | None,
        context: str | None,
    ) -> float:
        """Compute quality score for a decision."""
        return compute_quality_score(tags, reasons, pattern, context)
