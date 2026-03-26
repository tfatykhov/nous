"""F024 Phase 3b — Rubric Evolver.

Runs periodic correlation analysis between rubric dimensions and
outcome signals. Proposes weight adjustments (Phase 1), splits/merges
(Phase 2), and new dimensions (Phase 3).

Not event-driven — called on a schedule (weekly) or manually via REST.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func as sa_func, select

from nous.cognitive.correlation import correlate_dimensions_with_outcomes, suggest_weights
from nous.cognitive.rubric import RubricManager
from nous.cognitive.rubric_schemas import CorrelationReport
from nous.config import Settings
from nous.storage.database import Database
from nous.storage.models import OutcomeSignal, RubricVersion

logger = logging.getLogger(__name__)


class RubricEvolver:
    """Evolves rubric dimensions based on outcome correlation analysis."""

    def __init__(
        self,
        rubric_manager: RubricManager,
        db: Database,
        settings: Settings,
        agent_id: str,
    ) -> None:
        self._rubric = rubric_manager
        self._db = db
        self._settings = settings
        self._agent_id = agent_id

    async def run_evolution_cycle(self) -> CorrelationReport | None:
        """Run one evolution cycle: correlate, suggest, apply if warranted."""
        if not self._settings.rubric_evolution_enabled:
            logger.debug("F024-3b: Rubric evolution disabled")
            return None

        active = await self._rubric.get_active()
        if not active:
            logger.warning("F024-3b: No active rubric, skipping evolution")
            return None

        async with self._db.session() as session:
            count_result = await session.execute(
                select(sa_func.count(OutcomeSignal.id)).where(
                    OutcomeSignal.agent_id == self._agent_id,
                )
            )
            total_signals = count_result.scalar_one_or_none() or 0

            if total_signals < self._settings.rubric_min_episodes_for_correlation:
                logger.info(
                    "F024-3b: Only %d signals, need %d for correlation",
                    total_signals, self._settings.rubric_min_episodes_for_correlation,
                )
                return None

            sig_result = await session.execute(
                select(OutcomeSignal).where(
                    OutcomeSignal.agent_id == self._agent_id,
                    OutcomeSignal.self_improvement_scores.isnot(None),
                )
            )
            signals = sig_result.scalars().all()

            week_ago = datetime.now(UTC) - timedelta(days=7)
            recent_result = await session.execute(
                select(RubricVersion).where(
                    RubricVersion.agent_id == self._agent_id,
                    RubricVersion.created_at >= week_ago,
                )
            )
            recent_versions = recent_result.scalars().all()
            if len(recent_versions) >= self._settings.rubric_max_versions_per_week:
                logger.info("F024-3b: Rate limited — %d versions this week", len(recent_versions))
                return None

        episode_signals: dict[UUID, dict] = defaultdict(lambda: {"scores": {}, "signals": []})
        for sig in signals:
            ep = episode_signals[sig.episode_id]
            ep["signals"].append(sig.signal_type)
            if sig.self_improvement_scores and not ep["scores"]:
                ep["scores"] = sig.self_improvement_scores

        episodes = [ep for ep in episode_signals.values() if ep["scores"]]

        if len(episodes) < 3:
            logger.info("F024-3b: Only %d episodes with scores, need at least 3", len(episodes))
            return None

        dim_names = [d["name"] for d in active.dimensions]
        correlations = correlate_dimensions_with_outcomes(episodes, dim_names)

        current_weights = {d["name"]: d["weight"] for d in active.dimensions}
        suggested = suggest_weights(
            correlations, current_weights,
            cap=self._settings.rubric_weight_change_cap,
        )

        report = CorrelationReport(
            rubric_version=active.version,
            correlations=correlations,
            suggested_weights=suggested,
            episode_count=len(episodes),
        )

        weight_changed = any(
            abs(suggested.get(d, 0) - current_weights.get(d, 0)) > 0.001
            for d in current_weights
        )

        if not weight_changed:
            logger.info("F024-3b: No meaningful weight changes suggested")
            return report

        # Anti-Goodhart check
        if self.check_goodhart(episodes):
            logger.warning("F024-3b: Anti-Goodhart triggered — scores high but outcomes poor. Pausing evolution.")
            report.suggested_weights = None
            return report

        new_dims = []
        for d in active.dimensions:
            updated = dict(d)
            updated["weight"] = suggested.get(d["name"], d["weight"])
            new_dims.append(updated)

        base_version = active.version.split("-")[0]
        parts = base_version.split(".")
        new_version = f"{parts[0]}.{int(parts[1]) + 1}.0"

        await self._rubric.create_version(
            new_version=new_version,
            dimensions=new_dims,
            change_reason=f"Phase 1 weight adjustment based on {len(episodes)} episodes",
            outcome_correlations={
                c.dimension: {
                    **(active.outcome_correlations or {}).get(c.dimension, {}),
                    c.signal_type: {"pearson_r": c.pearson_r, "spearman_rho": c.spearman_rho},
                }
                for c in correlations
            },
        )

        logger.info("F024-3b: Created rubric %s — weights: %s", new_version, suggested)
        return report

    async def execute_split(
        self,
        dimension_name: str,
        sub_names: list[str],
        sub_descriptions: list[str],
    ) -> bool:
        """Split a dimension into sub-dimensions. Phase 2."""
        active = await self._rubric.get_active()
        if not active:
            return False

        dims = list(active.dimensions)
        parent = None
        parent_idx = -1
        for i, d in enumerate(dims):
            if d["name"] == dimension_name:
                parent = d
                parent_idx = i
                break

        if parent is None:
            logger.warning("F024-3b: Cannot split '%s' — not found", dimension_name)
            return False

        if len(dims) - 1 + len(sub_names) > self._settings.rubric_max_dimensions:
            logger.warning("F024-3b: Split would exceed max dimensions")
            return False

        sub_weight = round(parent["weight"] / len(sub_names), 4)
        new_dims = []
        for name, desc in zip(sub_names, sub_descriptions):
            new_dims.append({
                "name": name,
                "weight": sub_weight,
                "description": desc,
                "scoring_criteria": parent.get("scoring_criteria", "1-10 scale"),
                "min_weight": 0.10,
                "max_weight": 0.40,
            })

        result_dims = dims[:parent_idx] + new_dims + dims[parent_idx + 1:]

        total = sum(d["weight"] for d in result_dims)
        for d in result_dims:
            d["weight"] = round(d["weight"] / total, 4)

        base_version = active.version.split("-")[0]
        parts = base_version.split(".")
        new_version = f"{int(parts[0]) + 1}.0.0"

        await self._rubric.create_version(
            new_version=new_version,
            dimensions=result_dims,
            change_reason=f"Phase 2 split: '{dimension_name}' -> {sub_names}",
        )
        return True

    async def execute_merge(
        self,
        dim_a: str,
        dim_b: str,
        merged_name: str,
        merged_description: str,
    ) -> bool:
        """Merge two dimensions into one. Phase 2."""
        active = await self._rubric.get_active()
        if not active:
            return False

        dims = list(active.dimensions)
        a_dim = None
        b_dim = None

        for d in dims:
            if d["name"] == dim_a:
                a_dim = d
            elif d["name"] == dim_b:
                b_dim = d

        if not a_dim or not b_dim:
            logger.warning("F024-3b: Cannot merge — dimensions not found")
            return False

        if len(dims) - 1 < self._settings.rubric_min_dimensions:
            logger.warning("F024-3b: Merge would go below min dimensions")
            return False

        merged_weight = round(a_dim["weight"] + b_dim["weight"], 4)
        merged = {
            "name": merged_name,
            "weight": min(merged_weight, 0.40),
            "description": merged_description,
            "scoring_criteria": a_dim.get("scoring_criteria", "1-10 scale"),
            "min_weight": 0.10,
            "max_weight": 0.40,
        }

        result_dims = [d for d in dims if d["name"] not in (dim_a, dim_b)]
        result_dims.append(merged)

        total = sum(d["weight"] for d in result_dims)
        for d in result_dims:
            d["weight"] = round(d["weight"] / total, 4)

        base_version = active.version.split("-")[0]
        parts = base_version.split(".")
        new_version = f"{int(parts[0]) + 1}.0.0"

        await self._rubric.create_version(
            new_version=new_version,
            dimensions=result_dims,
            change_reason=f"Phase 2 merge: '{dim_a}' + '{dim_b}' -> '{merged_name}'",
        )
        return True

    @staticmethod
    def find_gap_episodes(
        episodes: list[dict],
        score_threshold: int = 7,
    ) -> list[dict]:
        """Find episodes where all dimensions scored >= threshold but outcome was poor."""
        negative_signals = {"corrected", "reworked"}
        gaps = []

        for ep in episodes:
            scores = ep.get("scores", {})
            signals = set(ep.get("signals", []))

            if not scores or not signals:
                continue

            all_high = all(v >= score_threshold for v in scores.values())
            has_negative = bool(signals & negative_signals)

            if all_high and has_negative:
                gaps.append(ep)

        return gaps

    @staticmethod
    def check_goodhart(
        episodes: list[dict],
        score_threshold: int = 8,
    ) -> bool:
        """Anti-Goodhart: detect score inflation without outcome improvement."""
        if not episodes:
            return False

        negative_signals = {"corrected", "reworked"}
        high_score_negative = 0

        for ep in episodes:
            scores = ep.get("scores", {})
            signals = set(ep.get("signals", []))
            if not scores:
                continue

            all_high = all(v >= score_threshold for v in scores.values())
            has_negative = bool(signals & negative_signals)

            if all_high and has_negative:
                high_score_negative += 1

        return high_score_negative > len(episodes) * 0.5

    @staticmethod
    def check_degradation(
        before: list[dict],
        after: list[dict],
        threshold: float = 0.15,
    ) -> bool:
        """Check if outcomes have degraded by > threshold after rubric change."""
        positive_signals = {"completed", "praised"}

        def positive_ratio(eps: list[dict]) -> float:
            if not eps:
                return 0.0
            pos = sum(1 for ep in eps if set(ep.get("signals", [])) & positive_signals)
            return pos / len(eps)

        before_ratio = positive_ratio(before)
        after_ratio = positive_ratio(after)

        if before_ratio == 0:
            return False

        degradation = (before_ratio - after_ratio) / before_ratio
        return degradation > threshold
