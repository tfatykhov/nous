"""Tests for F034.1 Finding Lifecycle — FindingStore, fingerprinting, escalation, digest.

25 test cases across 7 test classes:
- TestFindingFingerprint (4): stable hash, count-invariant, check-name-scoped, different sources differ
- TestFindingStoreIngest (5): new->TRIAGE, dup->SUPPRESS, resolved->reopen, flapping->SUPPRESS, startup suppression
- TestFindingStoreEscalation (4): low escalation after 72h, normal escalation after 24h, high re-alert, accumulation escalation with cooldown
- TestFindingStoreLifecycle (3): acknowledge, resolve, dismiss with strong_negative outcome
- TestOutcomeRecording (3): record_outcome, sweep_weak_negatives, get_outcomes_for_check
- TestFindingStoreMaintenance (3): prune resolved TTL, prune keeps active, stats
- TestDailyDigest (2): get_digest_items, empty when no acknowledged
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nous.heartbeat.finding_store import FindingStore
from nous.heartbeat.schemas import (
    EscalationConfig,
    Finding,
    FindingAction,
    FindingState,
    OutcomeSignal,
    TrackedFinding,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_finding(
    source: str = "test",
    summary: str = "Something happened",
    urgency: str = "normal",
    check_name: str = "health",
    needs_action: bool = False,
) -> Finding:
    return Finding(
        source=source,
        summary=summary,
        urgency=urgency,
        check_name=check_name,
        needs_action=needs_action,
    )


def _make_store(
    suppress_startup: bool = True,
    **escalation_overrides,
) -> FindingStore:
    """Create a FindingStore, optionally bypassing startup suppression."""
    cfg = EscalationConfig(**escalation_overrides)
    store = FindingStore(escalation_config=cfg)
    if suppress_startup:
        # Move startup time far into the past to bypass 5-min suppression
        store._startup_time = datetime.now(UTC) - timedelta(hours=1)
    return store


# ===========================================================================
# TestFindingFingerprint — 4 tests
# ===========================================================================


class TestFindingFingerprint:
    """Tests for Finding.fingerprint() stability and scoping."""

    def test_stable_hash(self):
        """Same finding produces the same fingerprint across calls."""
        f = _make_finding(summary="3 decisions pending review")
        fp1 = f.fingerprint()
        fp2 = f.fingerprint()
        assert fp1 == fp2
        assert len(fp1) == 16  # truncated sha256

    def test_count_invariant(self):
        """Fingerprint is invariant to count changes in the summary."""
        f1 = _make_finding(summary="3 decisions pending review")
        f2 = _make_finding(summary="7 decisions pending review")
        assert f1.fingerprint() == f2.fingerprint()

    def test_check_name_scoped(self):
        """Different check_name produces different fingerprints."""
        f1 = _make_finding(summary="Something happened", check_name="health")
        f2 = _make_finding(summary="Something happened", check_name="email")
        assert f1.fingerprint() != f2.fingerprint()

    def test_different_sources_differ(self):
        """Different sources produce different fingerprints."""
        f1 = _make_finding(source="brain", summary="test")
        f2 = _make_finding(source="facts", summary="test")
        assert f1.fingerprint() != f2.fingerprint()


# ===========================================================================
# TestFindingStoreIngest — 5 tests
# ===========================================================================


class TestFindingStoreIngest:
    """Tests for FindingStore.ingest() state machine."""

    def test_new_finding_returns_triage(self):
        """Brand new finding returns TRIAGE."""
        store = _make_store()
        f = _make_finding(summary="New issue found")
        action = store.ingest(f)
        assert action == FindingAction.TRIAGE
        assert f.fingerprint() in store._findings
        assert store._findings[f.fingerprint()].state == FindingState.NEW

    def test_duplicate_returns_suppress(self):
        """Same finding ingested twice returns SUPPRESS on second."""
        store = _make_store()
        f = _make_finding(summary="Duplicate finding")
        assert store.ingest(f) == FindingAction.TRIAGE
        assert store.ingest(f) == FindingAction.SUPPRESS

    def test_resolved_reopen_returns_triage(self):
        """Resolved finding re-ingested returns TRIAGE (reopen)."""
        store = _make_store()
        f = _make_finding(summary="Resolved then back")
        store.ingest(f)
        fp = f.fingerprint()
        store.resolve(fp)
        assert store._findings[fp].state == FindingState.RESOLVED
        action = store.ingest(f)
        assert action == FindingAction.TRIAGE
        assert store._findings[fp].state == FindingState.NEW
        assert store._findings[fp].reopen_count == 1

    def test_flapping_returns_suppress(self):
        """Finding with reopen_count >= 3 returns SUPPRESS (flapping)."""
        store = _make_store()
        f = _make_finding(summary="Flappy finding")
        store.ingest(f)
        fp = f.fingerprint()

        # Resolve and re-ingest 3 times to trigger flapping
        for _ in range(3):
            store.resolve(fp)
            store.ingest(f)

        assert store._findings[fp].reopen_count == 3
        # Now resolve and re-ingest one more — should suppress
        store.resolve(fp)
        action = store.ingest(f)
        assert action == FindingAction.SUPPRESS

    def test_startup_suppression(self):
        """All findings are SUPPRESS during the first 5 minutes after restart."""
        store = FindingStore()  # Don't bypass startup suppression
        f = _make_finding(summary="Early finding")
        action = store.ingest(f)
        assert action == FindingAction.SUPPRESS
        # Finding should be tracked but in SUPPRESSED state
        fp = f.fingerprint()
        assert fp in store._findings
        assert store._findings[fp].state == FindingState.SUPPRESSED


# ===========================================================================
# TestFindingStoreEscalation — 4 tests
# ===========================================================================


class TestFindingStoreEscalation:
    """Tests for time-based and accumulation escalation."""

    def test_low_escalation_after_72h(self):
        """Low-urgency finding escalates after 72 hours."""
        store = _make_store()
        f = _make_finding(urgency="low", summary="Stale low finding")
        store.ingest(f)
        fp = f.fingerprint()
        tracked = store._findings[fp]

        # Simulate aging: move first_seen 73 hours into the past
        tracked.first_seen = datetime.now(UTC) - timedelta(hours=73)

        action = store.ingest(f)
        assert action == FindingAction.ESCALATE
        assert tracked.escalated is True

    def test_normal_escalation_after_24h(self):
        """Normal-urgency finding escalates after 24 hours."""
        store = _make_store()
        f = _make_finding(urgency="normal", summary="Stale normal finding")
        store.ingest(f)
        fp = f.fingerprint()
        tracked = store._findings[fp]

        tracked.first_seen = datetime.now(UTC) - timedelta(hours=25)

        action = store.ingest(f)
        assert action == FindingAction.ESCALATE
        assert tracked.escalated is True

    def test_high_realert(self):
        """High-urgency finding re-alerts after 12 hours, throttled by last_escalated_at."""
        store = _make_store()
        f = _make_finding(urgency="high", summary="Critical high finding")
        store.ingest(f)
        fp = f.fingerprint()
        tracked = store._findings[fp]

        tracked.first_seen = datetime.now(UTC) - timedelta(hours=13)
        # First escalation
        action = store.ingest(f)
        assert action == FindingAction.ESCALATE
        assert tracked.last_escalated_at is not None

        # Immediate re-ingest should NOT escalate (throttled)
        action = store.ingest(f)
        assert action == FindingAction.SUPPRESS

        # After 12+ hours since last escalation, should re-alert
        tracked.last_escalated_at = datetime.now(UTC) - timedelta(hours=13)
        action = store.ingest(f)
        assert action == FindingAction.ESCALATE

    def test_accumulation_escalation_with_cooldown(self):
        """5+ acknowledged findings from same check triggers accumulation escalation."""
        store = _make_store(accumulation_threshold=3)

        # Add 3 acknowledged findings from same check (use distinct non-numeric summaries)
        labels = ["alpha", "beta", "gamma"]
        for label in labels:
            f = _make_finding(
                summary=f"Accumulated finding {label}",
                check_name="health",
            )
            store.ingest(f)
            store.acknowledge(f.fingerprint())

        # First call should trigger
        assert store.check_accumulation_escalation("health") is True

        # Second call within 12h should be on cooldown
        assert store.check_accumulation_escalation("health") is False

        # Move cooldown into the past
        store._accumulation_escalated["health"] = datetime.now(UTC) - timedelta(hours=13)
        assert store.check_accumulation_escalation("health") is True


# ===========================================================================
# TestFindingStoreLifecycle — 3 tests
# ===========================================================================


class TestFindingStoreLifecycle:
    """Tests for acknowledge, resolve, dismiss operations."""

    def test_acknowledge(self):
        """Acknowledge sets state to ACKNOWLEDGED."""
        store = _make_store()
        f = _make_finding(summary="To acknowledge")
        store.ingest(f)
        fp = f.fingerprint()
        assert store.acknowledge(fp) is True
        assert store._findings[fp].state == FindingState.ACKNOWLEDGED

    def test_resolve(self):
        """Resolve sets state to RESOLVED and records timestamp."""
        store = _make_store()
        f = _make_finding(summary="To resolve")
        store.ingest(f)
        fp = f.fingerprint()
        assert store.resolve(fp) is True
        tracked = store._findings[fp]
        assert tracked.state == FindingState.RESOLVED
        assert tracked.resolved_at is not None

    def test_dismiss_records_strong_negative(self):
        """Dismiss sets state to RESOLVED and records strong_negative outcome."""
        store = _make_store()
        f = _make_finding(summary="To dismiss")
        store.ingest(f)
        fp = f.fingerprint()
        assert store.dismiss(fp) is True
        tracked = store._findings[fp]
        assert tracked.state == FindingState.RESOLVED
        assert tracked.outcome == OutcomeSignal.STRONG_NEGATIVE
        assert tracked.outcome_at is not None


# ===========================================================================
# TestOutcomeRecording — 3 tests
# ===========================================================================


class TestOutcomeRecording:
    """Tests for outcome recording and sweep."""

    def test_record_outcome(self):
        """record_outcome sets outcome and timestamp."""
        store = _make_store()
        f = _make_finding(summary="With outcome")
        store.ingest(f)
        fp = f.fingerprint()
        assert store.record_outcome(fp, OutcomeSignal.POSITIVE) is True
        tracked = store._findings[fp]
        assert tracked.outcome == OutcomeSignal.POSITIVE
        assert tracked.outcome_at is not None

    def test_sweep_weak_negatives(self):
        """Sweep marks old acknowledged findings with no action as weak_negative."""
        store = _make_store()
        f = _make_finding(summary="Old acknowledged finding")
        store.ingest(f)
        fp = f.fingerprint()
        store.acknowledge(fp)

        # Make it old enough (>72 hours)
        tracked = store._findings[fp]
        tracked.last_seen = datetime.now(UTC) - timedelta(hours=80)

        count = store.sweep_weak_negatives(hours=72)
        assert count == 1
        assert tracked.outcome == OutcomeSignal.WEAK_NEGATIVE

    def test_get_outcomes_for_check(self):
        """get_outcomes_for_check returns findings with outcomes for a given check."""
        store = _make_store()

        # Create findings with and without outcomes
        f1 = _make_finding(summary="Has outcome", check_name="health")
        store.ingest(f1)
        store.record_outcome(f1.fingerprint(), OutcomeSignal.POSITIVE)

        f2 = _make_finding(summary="No outcome", check_name="health")
        store.ingest(f2)

        f3 = _make_finding(summary="Different check", check_name="email")
        store.ingest(f3)
        store.record_outcome(f3.fingerprint(), OutcomeSignal.NEGATIVE)

        results = store.get_outcomes_for_check("health")
        assert len(results) == 1
        assert results[0].fingerprint == f1.fingerprint()


# ===========================================================================
# TestFindingStoreMaintenance — 3 tests
# ===========================================================================


class TestFindingStoreMaintenance:
    """Tests for prune, stats, and serialization."""

    def test_prune_removes_old_resolved(self):
        """Prune removes resolved findings older than TTL."""
        store = _make_store()
        f = _make_finding(summary="Old resolved")
        store.ingest(f)
        fp = f.fingerprint()
        store.resolve(fp)

        # Move resolved_at 10 days into the past
        store._findings[fp].resolved_at = datetime.now(UTC) - timedelta(days=10)

        count = store.prune(resolved_ttl_days=7)
        assert count == 1
        assert fp not in store._findings

    def test_prune_keeps_active(self):
        """Prune does not remove active (non-resolved) findings."""
        store = _make_store()
        f = _make_finding(summary="Active finding")
        store.ingest(f)
        fp = f.fingerprint()

        count = store.prune(resolved_ttl_days=0)
        assert count == 0
        assert fp in store._findings

    def test_stats(self):
        """Stats returns correct counts by state and check."""
        store = _make_store()

        f1 = _make_finding(summary="Finding one", check_name="health")
        store.ingest(f1)
        store.acknowledge(f1.fingerprint())

        f2 = _make_finding(summary="Finding two", check_name="email")
        store.ingest(f2)

        stats = store.stats()
        assert stats["total"] == 2
        assert stats["by_state"]["acknowledged"] == 1
        assert stats["by_state"]["new"] == 1
        assert stats["by_check"]["health"] == 1
        assert stats["by_check"]["email"] == 1


# ===========================================================================
# TestDailyDigest — 2 tests
# ===========================================================================


class TestDailyDigest:
    """Tests for digest item retrieval."""

    def test_get_digest_items(self):
        """get_digest_items returns only acknowledged findings."""
        store = _make_store()

        f1 = _make_finding(summary="Acknowledged one")
        store.ingest(f1)
        store.acknowledge(f1.fingerprint())

        f2 = _make_finding(summary="Still new")
        store.ingest(f2)

        items = store.get_digest_items()
        assert len(items) == 1
        assert items[0].fingerprint == f1.fingerprint()

    def test_empty_when_no_acknowledged(self):
        """get_digest_items returns empty when no acknowledged findings."""
        store = _make_store()

        f = _make_finding(summary="Just new")
        store.ingest(f)

        items = store.get_digest_items()
        assert len(items) == 0


# ===========================================================================
# TestFindingStoreSerialize — 2 tests
# ===========================================================================


class TestFindingStoreSerialize:
    """Tests for to_list serialization."""

    def test_to_list_format(self):
        """to_list returns dicts with expected keys."""
        store = _make_store()
        f = _make_finding(summary="Serializable finding", check_name="health")
        store.ingest(f)
        fp = f.fingerprint()

        items = store.to_list()
        assert len(items) == 1
        item = items[0]
        assert item["fingerprint"] == fp
        assert item["check_name"] == "health"
        assert item["state"] == "new"
        assert item["seen_count"] == 1
        assert "first_seen" in item
        assert "last_seen" in item

    def test_record_outcome_not_found(self):
        """record_outcome returns False for unknown fingerprint."""
        store = _make_store()
        assert store.record_outcome("nonexistent", OutcomeSignal.POSITIVE) is False

    def test_acknowledge_not_found(self):
        """acknowledge returns False for unknown fingerprint."""
        store = _make_store()
        assert store.acknowledge("nonexistent") is False

    def test_resolve_not_found(self):
        """resolve returns False for unknown fingerprint."""
        store = _make_store()
        assert store.resolve("nonexistent") is False

    def test_dismiss_not_found(self):
        """dismiss returns False for unknown fingerprint."""
        store = _make_store()
        assert store.dismiss("nonexistent") is False
