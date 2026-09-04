"""Finding lifecycle store — dedup, escalation, digest, outcomes (F034.1).

In-memory store that tracks findings through their lifecycle:
NEW -> ACKNOWLEDGED -> RESOLVED, with SUPPRESSED for dedup.

Provides outcome signals for the self-tuner (F034.3) and
accumulation escalation for noisy checks.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta

from nous.heartbeat.schemas import (
    EscalationConfig,
    Finding,
    FindingAction,
    FindingState,
    OutcomeSignal,
    TrackedFinding,
)

logger = logging.getLogger(__name__)


class FindingStore:
    """Tracks finding lifecycle — dedup, escalation, digest, outcomes (F034.1)."""

    def __init__(self, escalation_config: EscalationConfig | None = None) -> None:
        self._findings: dict[str, TrackedFinding] = {}  # fingerprint -> tracked
        self._escalation = escalation_config or EscalationConfig()
        self._accumulation_escalated: dict[str, datetime | None] = {}  # check_name -> last escalation time
        self._startup_time: datetime = datetime.now(UTC)
        self._startup_suppression_seconds: int = 300  # 5-min suppress after restart

    # ------------------------------------------------------------------
    # Core lifecycle
    # ------------------------------------------------------------------

    def ingest(self, finding: Finding) -> FindingAction:
        """Route finding through state machine.

        Returns the action the runner should take:
        - TRIAGE: new finding, process normally
        - SUPPRESS: duplicate or startup suppression, skip
        - ESCALATE: overdue finding, upgrade urgency
        """
        fp = finding.fingerprint()

        # Startup suppression: treat all findings as suppress for 5 min after restart
        if self._in_startup_suppression():
            if fp not in self._findings:
                now = datetime.now(UTC)
                self._findings[fp] = TrackedFinding(
                    finding=finding,
                    fingerprint=fp,
                    state=FindingState.SUPPRESSED,
                    first_seen=now,
                    last_seen=now,
                )
            return FindingAction.SUPPRESS

        if fp in self._findings:
            existing = self._findings[fp]
            existing.last_seen = datetime.now(UTC)
            existing.absent_ticks = 0  # Reset — finding is still active

            if existing.state == FindingState.RESOLVED:
                # Re-opened — check flapping
                existing.reopen_count += 1
                if existing.reopen_count >= 3:
                    # Flapping: route to digest, not immediate triage
                    existing.state = FindingState.ACKNOWLEDGED
                    existing.resolved_at = None
                    return FindingAction.SUPPRESS
                existing.state = FindingState.NEW
                existing.resolved_at = None
                return FindingAction.TRIAGE

            existing.seen_count += 1

            # Escalation check (time-based)
            if self._should_escalate(existing):
                existing.escalated = True
                existing.last_escalated_at = datetime.now(UTC)
                return FindingAction.ESCALATE

            return FindingAction.SUPPRESS

        # Brand new finding
        now = datetime.now(UTC)
        self._findings[fp] = TrackedFinding(
            finding=finding,
            fingerprint=fp,
            state=FindingState.NEW,
            first_seen=now,
            last_seen=now,
        )
        return FindingAction.TRIAGE

    def acknowledge(self, fingerprint: str) -> bool:
        """Mark a finding as acknowledged (triaged)."""
        if fingerprint not in self._findings:
            return False
        self._findings[fingerprint].state = FindingState.ACKNOWLEDGED
        return True

    def resolve(self, fingerprint: str) -> bool:
        """Mark a finding as resolved."""
        if fingerprint not in self._findings:
            return False
        tracked = self._findings[fingerprint]
        tracked.state = FindingState.RESOLVED
        tracked.resolved_at = datetime.now(UTC)
        return True

    def dismiss(self, fingerprint: str) -> bool:
        """Explicit dismiss — records strong_negative outcome and resolves."""
        if fingerprint not in self._findings:
            return False
        tracked = self._findings[fingerprint]
        tracked.state = FindingState.RESOLVED
        tracked.resolved_at = datetime.now(UTC)
        tracked.outcome = OutcomeSignal.STRONG_NEGATIVE
        tracked.outcome_at = datetime.now(UTC)
        return True

    def record_outcome(self, fingerprint: str, signal: OutcomeSignal) -> bool:
        """Record an outcome signal on a finding (for tuner feedback)."""
        if fingerprint not in self._findings:
            return False
        tracked = self._findings[fingerprint]
        tracked.outcome = signal
        tracked.outcome_at = datetime.now(UTC)
        return True

    # ------------------------------------------------------------------
    # Digest + query
    # ------------------------------------------------------------------

    def get_digest_items(self) -> list[TrackedFinding]:
        """Get acknowledged findings for daily digest."""
        return [f for f in self._findings.values() if f.state == FindingState.ACKNOWLEDGED]

    def get_outcomes_for_check(self, check_name: str, since_seconds: int = 2592000) -> list[TrackedFinding]:
        """Get findings with outcomes for a check (for tuner). Default 30 days."""
        cutoff = datetime.now(UTC) - timedelta(seconds=since_seconds)
        return [
            f
            for f in self._findings.values()
            if f.finding.check_name == check_name
            and f.outcome is not None
            and f.outcome_at is not None
            and f.outcome_at >= cutoff
        ]

    # ------------------------------------------------------------------
    # Accumulation escalation
    # ------------------------------------------------------------------

    def check_accumulation_escalation(self, check_name: str) -> bool:
        """5+ acknowledged findings from same check -> escalate collection.

        Has a 12-hour cooldown per check_name to avoid spam.
        """
        # Check cooldown (12 hours)
        last = self._accumulation_escalated.get(check_name)
        if last and (datetime.now(UTC) - last).total_seconds() < 43200:
            return False

        ack_count = sum(
            1
            for f in self._findings.values()
            if f.finding.check_name == check_name and f.state == FindingState.ACKNOWLEDGED
        )
        if ack_count >= self._escalation.accumulation_threshold:
            self._accumulation_escalated[check_name] = datetime.now(UTC)
            return True
        return False

    # ------------------------------------------------------------------
    # Auto-resolve helpers
    # ------------------------------------------------------------------

    def get_active_by_check(self, check_name: str) -> set[str]:
        """Return fingerprints of active (non-resolved) findings for a check."""
        active = set()
        for fp, tracked in self._findings.items():
            if tracked.state == FindingState.RESOLVED:
                continue
            if tracked.finding.check_name == check_name:
                active.add(fp)
        return active

    def mark_absent_tick(self, fingerprint: str) -> None:
        """Increment absent_ticks counter for a finding not reported this tick."""
        if fingerprint in self._findings:
            self._findings[fingerprint].absent_ticks += 1

    def get_auto_resolvable(self, threshold: int = 2) -> set[str]:
        """Return fingerprints of ACKNOWLEDGED findings absent for >= threshold ticks.

        Only ACKNOWLEDGED findings are eligible — NEW findings haven't been
        triaged yet (auto-resolving them would silently drop issues), and
        SUPPRESSED findings were never actionable.
        """
        resolvable = set()
        for fp, tracked in self._findings.items():
            if tracked.state != FindingState.ACKNOWLEDGED:
                continue
            if tracked.absent_ticks >= threshold:
                resolvable.add(fp)
        return resolvable

    def get_tracked(self, fingerprint: str) -> TrackedFinding | None:
        """Get a tracked finding by fingerprint."""
        return self._findings.get(fingerprint)

    # ------------------------------------------------------------------
    # Transactional compensation
    # ------------------------------------------------------------------

    def snapshot(self, fingerprints: Iterable[str]) -> dict[str, TrackedFinding | None]:
        """Deep-copy the tracked state of ``fingerprints`` for later restore.

        The store is in-memory and therefore cannot participate in the DB
        transaction that publishes a surface. A caller that ingests findings
        as part of a fallible push (see ``a2ui.tools.push_surface``) takes a
        snapshot first and calls :meth:`restore` if the push fails, so a
        rolled-back surface never leaves an orphaned finding exposed through
        ``GET /heartbeat/findings``. ``None`` records "was not tracked".
        """
        return {
            fp: copy.deepcopy(self._findings[fp]) if fp in self._findings else None
            for fp in fingerprints
        }

    def restore(self, snapshot: Mapping[str, TrackedFinding | None]) -> None:
        """Undo ingests by restoring a :meth:`snapshot` taken before them."""
        for fp, tracked in snapshot.items():
            if tracked is None:
                self._findings.pop(fp, None)
            else:
                self._findings[fp] = tracked

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def sweep_weak_negatives(self, hours: int = 72) -> int:
        """Mark acknowledged findings with no action after N hours as weak_negative."""
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        count = 0
        for tracked in self._findings.values():
            if (
                tracked.state == FindingState.ACKNOWLEDGED
                and tracked.outcome is None
                and tracked.last_seen is not None
                and tracked.last_seen < cutoff
            ):
                tracked.outcome = OutcomeSignal.WEAK_NEGATIVE
                tracked.outcome_at = datetime.now(UTC)
                count += 1
        return count

    def prune(self, resolved_ttl_days: int = 7) -> int:
        """Remove resolved findings older than TTL."""
        cutoff = datetime.now(UTC) - timedelta(days=resolved_ttl_days)
        to_remove = [
            fp
            for fp, t in self._findings.items()
            if t.state == FindingState.RESOLVED and t.resolved_at is not None and t.resolved_at < cutoff
        ]
        for fp in to_remove:
            del self._findings[fp]
        return len(to_remove)

    # ------------------------------------------------------------------
    # Serialization (for REST API)
    # ------------------------------------------------------------------

    def to_list(self) -> list[dict]:
        """Serialize all findings for REST API."""
        result = []
        for fp, t in self._findings.items():
            result.append(
                {
                    "fingerprint": fp,
                    "check_name": t.finding.check_name,
                    "source": t.finding.source,
                    "summary": t.finding.summary,
                    "urgency": t.finding.urgency,
                    "state": t.state.value,
                    "first_seen": t.first_seen.isoformat() if t.first_seen else None,
                    "last_seen": t.last_seen.isoformat() if t.last_seen else None,
                    "seen_count": t.seen_count,
                    "escalated": t.escalated,
                    "outcome": t.outcome.value if t.outcome else None,
                    "reopen_count": t.reopen_count,
                }
            )
        return result

    def stats(self) -> dict:
        """Summary stats for status endpoints."""
        by_state: dict[str, int] = {}
        by_check: dict[str, int] = {}
        for t in self._findings.values():
            by_state[t.state.value] = by_state.get(t.state.value, 0) + 1
            by_check[t.finding.check_name] = by_check.get(t.finding.check_name, 0) + 1
        return {"total": len(self._findings), "by_state": by_state, "by_check": by_check}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _should_escalate(self, tracked: TrackedFinding) -> bool:
        """Check if a finding should be escalated based on age and urgency."""
        if tracked.first_seen is None:
            return False
        now = datetime.now(UTC)
        age_hours = (now - tracked.first_seen).total_seconds() / 3600
        urgency = tracked.finding.urgency

        if urgency == "low" and age_hours >= self._escalation.low_to_normal_hours:
            return not tracked.escalated
        elif urgency == "normal" and age_hours >= self._escalation.normal_to_high_hours:
            return not tracked.escalated
        elif urgency == "high" and age_hours >= self._escalation.high_realert_hours:
            # High can re-alert periodically, but throttled by last_escalated_at
            if tracked.last_escalated_at is None:
                return True
            hours_since = (now - tracked.last_escalated_at).total_seconds() / 3600
            return hours_since >= self._escalation.high_realert_hours
        return False

    def _in_startup_suppression(self) -> bool:
        """Check if we're still in the post-restart suppression window."""
        return (datetime.now(UTC) - self._startup_time).total_seconds() < self._startup_suppression_seconds
