"""Calibration engine for computing decision accuracy metrics.

All metrics are computed via SQL aggregation — no row-level loading into Python.
"""

from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.schemas import CalibrationReport
from nous.storage.models import Decision, DecisionReason


class CalibrationEngine:
    """Compute calibration metrics from Postgres data."""

    async def compute(self, session: AsyncSession, agent_id: str) -> CalibrationReport:
        """Compute full calibration report.

        Metrics:
        1. Brier Score = mean((confidence - outcome_binary)^2)
           - outcome_binary: success=1, partial=0.5, failure=0
        2. Accuracy = directional agreement (confidence >= 0.5 AND outcome in success/partial,
                      OR confidence < 0.5 AND outcome = failure)
        3. Confidence stats = mean and stddev of all confidence values
        4. Per-category breakdown = {category: {count, accuracy, brier_score}}
        5. Per-reason-type breakdown = {type: {count, brier_score}}
        """
        # Outcome binary mapping for SQL
        outcome_binary = case(
            (Decision.outcome == "success", 1.0),
            (Decision.outcome == "partial", 0.5),
            else_=0.0,
        )

        # Directional accuracy: correct if confidence direction matches outcome
        is_correct = case(
            (
                (Decision.confidence >= 0.5) & (Decision.outcome.in_(["success", "partial"])),
                1.0,
            ),
            (
                (Decision.confidence < 0.5) & (Decision.outcome == "failure"),
                1.0,
            ),
            else_=0.0,
        )

        # Base filter: reviewed decisions for this agent
        # Exclude abandoned decisions (outcome='failure', confidence=0.0) which
        # contribute (0.0 - 0.0)^2 = 0.0 to Brier score, artificially improving it
        reviewed_filter = (
            (Decision.agent_id == agent_id)
            & (Decision.outcome != "pending")
            & (Decision.outcome.is_not(None))
            & ~((Decision.outcome == "failure") & (Decision.confidence == 0.0))
        )

        # --- Total and reviewed counts ---
        # Exclude abandoned decisions (outcome='failure', confidence=0.0) from
        # reviewed count, consistent with the Brier score filter
        count_result = await session.execute(
            select(
                func.count().label("total"),
                func.count().filter(
                    (Decision.outcome != "pending")
                    & (Decision.outcome.is_not(None))
                    & ~((Decision.outcome == "failure") & (Decision.confidence == 0.0))
                ).label("reviewed"),
            ).where(Decision.agent_id == agent_id)
        )
        counts = count_result.one()
        total_decisions = counts.total
        reviewed_decisions = counts.reviewed

        # If no reviewed decisions, return early with nulls
        if reviewed_decisions == 0:
            return CalibrationReport(
                total_decisions=total_decisions,
                reviewed_decisions=0,
            )

        # --- Brier score, accuracy, confidence stats ---
        brier_expr = func.power(Decision.confidence - outcome_binary, 2)

        stats_result = await session.execute(
            select(
                func.avg(brier_expr).label("brier_score"),
                func.avg(is_correct).label("accuracy"),
                func.avg(Decision.confidence).label("confidence_mean"),
                func.stddev(Decision.confidence).label("confidence_stddev"),
            ).where(reviewed_filter)
        )
        stats = stats_result.one()

        # --- Per-category breakdown ---
        cat_result = await session.execute(
            select(
                Decision.category,
                func.count().label("count"),
                func.avg(is_correct).label("accuracy"),
                func.avg(brier_expr).label("brier_score"),
            )
            .where(reviewed_filter)
            .group_by(Decision.category)
        )
        category_stats = {}
        for row in cat_result:
            category_stats[row.category] = {
                "count": row.count,
                "accuracy": float(row.accuracy) if row.accuracy is not None else None,
                "brier_score": (float(row.brier_score) if row.brier_score is not None else None),
            }

        # --- Per-reason-type breakdown ---
        # Use a subquery to deduplicate: one row per (decision, reason_type)
        # so a decision with N reasons of the same type only contributes its
        # Brier error once per type.
        reason_brier = func.power(Decision.confidence - outcome_binary, 2)

        deduped = (
            select(
                DecisionReason.type.label("reason_type"),
                Decision.id.label("decision_id"),
                reason_brier.label("brier"),
            )
            .join(DecisionReason, DecisionReason.decision_id == Decision.id)
            .where(reviewed_filter)
            .group_by(DecisionReason.type, Decision.id, Decision.confidence, Decision.outcome)
            .subquery()
        )

        reason_result = await session.execute(
            select(
                deduped.c.reason_type,
                func.count().label("count"),
                func.avg(deduped.c.brier).label("brier_score"),
            ).group_by(deduped.c.reason_type)
        )
        reason_type_stats = {}
        for row in reason_result:
            reason_type_stats[row.reason_type] = {
                "count": row.count,
                "brier_score": (float(row.brier_score) if row.brier_score is not None else None),
            }

        return CalibrationReport(
            total_decisions=total_decisions,
            reviewed_decisions=reviewed_decisions,
            brier_score=(float(stats.brier_score) if stats.brier_score is not None else None),
            accuracy=float(stats.accuracy) if stats.accuracy is not None else None,
            confidence_mean=(float(stats.confidence_mean) if stats.confidence_mean is not None else None),
            confidence_stddev=(float(stats.confidence_stddev) if stats.confidence_stddev is not None else None),
            category_stats=category_stats,
            reason_type_stats=reason_type_stats,
        )
