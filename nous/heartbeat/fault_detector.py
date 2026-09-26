"""Fault detector heartbeat checks (decision 28f021a0).

Two checks, both land-dark (NOUS_FAULT_DETECTOR_ENABLED=false):

``ProcessFaultCheck``
    Queries ``nous_system.process_run_log`` for silent failures:
    - A sleep phase has not completed in ``fault_detector_sleep_max_gap_hours``.
    - A phase's last N runs all have status='error'.
    - stale_scan ran M consecutive times with zero changes while the
      eligible-fact population is non-zero.
    - items_changed/items_examined collapsed vs its 20-run trailing baseline.

``RetrievalCanaryCheck``
    Reads a configurable JSONL file; for each query, calls
    ``heart.search_facts`` and checks whether at least one gold_id appears
    in top-K results.  No-op when ``fault_detector_canary_path`` is empty.

Canary JSONL format (one JSON object per line)::

    {"query": "what is the deployment model?",
     "gold_ids": ["<uuid>", ...],
     "min_recall_at_k": 0.5}

To build a canary set: query your prod DB for high-confidence facts,
record their IDs alongside queries that should retrieve them, and write
the JSONL to a path configured via ``NOUS_FAULT_DETECTOR_CANARY_PATH``.
A helper script (future work) will automate this.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import TYPE_CHECKING

from nous.heartbeat.registry import BaseCheck
from nous.heartbeat.schemas import CheckResult, Finding
from nous.observability.process_recorder import ProcessRecorder

if TYPE_CHECKING:
    from nous.config import Settings
    from nous.heart.heart import Heart
    from nous.storage.database import Database

logger = logging.getLogger(__name__)

# Sleep phases we track (must match sleep_handler phase names).
# Only phases that can meaningfully fail silently are listed.
_SLEEP_PHASES = (
    "sleep/review",
    "sleep/prune",
    "sleep/reflect",
    "sleep/resolve_contradictions",
    "sleep/stale_scan",
    "sleep/cluster_consolidation",
    "sleep/graph_densification",
    "sleep/relink_open_episodes",
    "sleep/prune_dead_edges",
    "sleep/generalize",
)


class ProcessFaultCheck(BaseCheck):
    """Heartbeat check: detect silent failures in periodic memory processes.

    Registered only when ``fault_detector_enabled=True``.  Ships land-dark.
    """

    name = "process_fault_detector"

    def __init__(
        self,
        db: Database,
        settings: Settings,
        agent_id: str,
    ) -> None:
        super().__init__()
        self._db = db
        self._settings = settings
        self._agent_id = agent_id
        self.interval: int = getattr(settings, "fault_detector_check_interval", 3600)
        self.timeout: int = 60

    async def run(self) -> CheckResult:
        findings: list[Finding] = []

        max_gap_hours: int = getattr(
            self._settings, "fault_detector_sleep_max_gap_hours", 48
        )
        consec_err_threshold: int = getattr(
            self._settings, "fault_detector_consecutive_error_threshold", 3
        )
        zero_change_threshold: int = getattr(
            self._settings, "fault_detector_zero_change_threshold", 5
        )
        ratio_collapse_threshold: float = getattr(
            self._settings, "fault_detector_ratio_collapse_threshold", 0.30
        )
        baseline_window: int = getattr(
            self._settings, "fault_detector_ratio_baseline_window", 20
        )
        stale_scan_age_days: int = getattr(
            self._settings, "stale_scan_age_days", 60
        )

        recorder = ProcessRecorder(self._db, self._agent_id)

        for process_name in _SLEEP_PHASES:
            runs = await recorder.get_recent_runs(
                process_name, limit=max(baseline_window, consec_err_threshold + 1)
            )
            if not runs:
                # Phase has never run (or ran before migration 077).
                # Skip — not a finding until we have a baseline.
                continue

            finished_runs = [r for r in runs if r["status"] == "finished"]

            # ----------------------------------------------------------------
            # 1. Missed run: no finished row in the last N hours
            # ----------------------------------------------------------------
            now = datetime.now(UTC)
            last_finished = next(
                (r for r in runs if r["status"] == "finished"), None
            )
            if last_finished is None:
                gap_hours = max_gap_hours + 1  # trigger the check
            else:
                last_ts: datetime = last_finished["started_at"]
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=UTC)
                gap_hours = (now - last_ts).total_seconds() / 3600.0

            if gap_hours > max_gap_hours:
                findings.append(
                    Finding(
                        source="fault_detector",
                        summary=(
                            f"Process {process_name!r} has not completed "
                            f"in {int(gap_hours)}h (threshold {max_gap_hours}h)"
                        ),
                        urgency="normal",
                        needs_action=True,
                        check_name=self.name,
                    )
                )
                continue  # don't pile on with other findings for a missing phase

            # ----------------------------------------------------------------
            # 2. Consecutive errors
            # ----------------------------------------------------------------
            recent = runs[:consec_err_threshold]
            if (
                len(recent) >= consec_err_threshold
                and all(r["status"] == "error" for r in recent)
            ):
                findings.append(
                    Finding(
                        source="fault_detector",
                        summary=(
                            f"Process {process_name!r} failed with errors "
                            f"in {consec_err_threshold} consecutive runs"
                        ),
                        urgency="high",
                        needs_action=True,
                        check_name=self.name,
                    )
                )

            # ----------------------------------------------------------------
            # 3. stale_scan zero-change collapse + population check
            # ----------------------------------------------------------------
            if process_name == "sleep/stale_scan":
                recent_finished = finished_runs[:zero_change_threshold]
                if len(recent_finished) >= zero_change_threshold:
                    all_zero_changed = all(
                        r.get("items_changed") == 0 for r in recent_finished
                        if r.get("items_changed") is not None
                    )
                    has_changed_data = any(
                        r.get("items_changed") is not None for r in recent_finished
                    )
                    if all_zero_changed and has_changed_data:
                        pop_count = await self._count_stale_eligible(stale_scan_age_days)
                        if pop_count > 10:
                            findings.append(
                                Finding(
                                    source="fault_detector",
                                    summary=(
                                        f"stale_scan has had 0 deactivations in last "
                                        f"{zero_change_threshold} runs but "
                                        f"{pop_count} age-eligible facts exist"
                                    ),
                                    urgency="normal",
                                    needs_action=True,
                                    check_name=self.name,
                                )
                            )

            # ----------------------------------------------------------------
            # 4. Output/input ratio collapse vs trailing baseline
            # ----------------------------------------------------------------
            ratio_runs = [
                r for r in finished_runs
                if r.get("items_examined") is not None
                and r["items_examined"] > 0
                and r.get("items_changed") is not None
            ]
            if len(ratio_runs) >= baseline_window:
                baseline_ratios = [
                    r["items_changed"] / r["items_examined"]
                    for r in ratio_runs[5:]  # skip the 5 most recent for baseline
                ]
                recent_ratios = [
                    r["items_changed"] / r["items_examined"]
                    for r in ratio_runs[:5]
                ]
                if baseline_ratios and recent_ratios:
                    baseline_mean = mean(baseline_ratios)
                    recent_mean = mean(recent_ratios)
                    if (
                        baseline_mean > 0.01
                        and recent_mean < baseline_mean * ratio_collapse_threshold
                    ):
                        findings.append(
                            Finding(
                                source="fault_detector",
                                summary=(
                                    f"Process {process_name!r} output/input ratio "
                                    f"collapsed to {recent_mean:.1%} "
                                    f"(baseline {baseline_mean:.1%})"
                                ),
                                urgency="normal",
                                needs_action=True,
                                check_name=self.name,
                            )
                        )

        return CheckResult(
            has_updates=bool(findings),
            findings=findings,
        )

    async def _count_stale_eligible(self, age_days: int) -> int:
        """Count facts eligible for stale_scan (age > threshold, low recall)."""
        try:
            from sqlalchemy import text
            async with self._db.session() as session:
                result = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM heart.facts "
                        "WHERE agent_id = :agent_id "
                        "  AND active = TRUE "
                        "  AND created_at < now() - make_interval(days => :days) "
                        "  AND (last_recalled_at IS NULL "
                        "       OR last_recalled_at < now() - make_interval(days => :days))"
                    ),
                    {"agent_id": self._agent_id, "days": age_days},
                )
                count: int = result.scalar_one()
                return count
        except Exception:
            logger.warning("_count_stale_eligible failed", exc_info=True)
            return 0


class RetrievalCanaryCheck(BaseCheck):
    """Heartbeat check: verify a fixed set of queries still retrieves expected IDs.

    No LLM judge — pure recall@K against ``heart.search_facts``.

    Ships as a no-op when ``fault_detector_canary_path`` is empty or the
    file doesn't exist.  To seed the canary set, see the module docstring.
    """

    name = "retrieval_canary"

    def __init__(self, heart: Heart, settings: Settings) -> None:
        super().__init__()
        self._heart = heart
        self._settings = settings
        self.interval: int = getattr(settings, "fault_detector_canary_interval", 3600)
        self.timeout: int = 90  # canary may involve several DB queries

    def _load_canary(self) -> list[dict]:
        """Load canary entries from JSONL file; return [] if missing/empty."""
        path_str: str = getattr(self._settings, "fault_detector_canary_path", "")
        if not path_str:
            return []
        path = Path(path_str)
        if not path.exists():
            logger.warning("Canary path %s not found — canary check is a no-op", path)
            return []
        entries: list[dict] = []
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Canary JSONL: skipping malformed line: %r", line[:80])
        except OSError:
            logger.warning("Canary file %s unreadable", path, exc_info=True)
        return entries

    async def run(self) -> CheckResult:
        entries = self._load_canary()
        if not entries:
            return CheckResult(has_updates=False, findings=[])

        top_k: int = getattr(self._settings, "fault_detector_canary_top_k", 10)
        findings: list[Finding] = []

        for entry in entries:
            query: str = entry.get("query", "")
            gold_ids: list[str] = entry.get("gold_ids", [])
            min_recall: float = float(entry.get("min_recall_at_k", 0.5))
            if not query or not gold_ids:
                continue

            try:
                results = await self._heart.search_facts(query, limit=top_k)
            except Exception:
                logger.warning("Canary search failed for %r", query[:60], exc_info=True)
                continue

            returned_ids = {str(r.id) for r in results}
            gold_set = set(gold_ids)
            hits = len(returned_ids & gold_set)
            recall = hits / len(gold_set)

            if recall < min_recall:
                findings.append(
                    Finding(
                        source="retrieval_canary",
                        summary=(
                            f"Canary query {query[:60]!r}: "
                            f"recall@{top_k} = {recall:.0%} "
                            f"(expected >= {min_recall:.0%})"
                        ),
                        urgency="normal",
                        needs_action=True,
                        check_name=self.name,
                    )
                )

        return CheckResult(
            has_updates=bool(findings),
            findings=findings,
        )
