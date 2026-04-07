"""Heartbeat data structures (F034 + F034.1).

Lightweight dataclasses for check results, triage findings,
and finding lifecycle management.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal


# ------------------------------------------------------------------
# Core check/finding types (F034)
# ------------------------------------------------------------------


@dataclass
class Finding:
    """A single finding from a heartbeat check."""

    source: str
    summary: str
    urgency: Literal["high", "normal", "low"] = "normal"
    needs_action: bool = False
    raw_data: dict[str, Any] = field(default_factory=dict)
    check_name: str = ""

    def fingerprint(self) -> str:
        """Stable hash for dedup. Strips volatile parts (counts, timestamps)."""
        normalized = re.sub(r"\d+", "N", self.summary)
        key = f"{self.check_name}:{self.source}:{normalized}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass
class CheckResult:
    """Result of running a single check."""

    has_updates: bool = False
    findings: list[Finding] = field(default_factory=list)
    tokens_used: int = 0  # F034.5: token consumption for budget tracking


@dataclass
class HeartbeatResult:
    """Result of a cognitive triage session."""

    response: str = ""
    tokens_used: int = 0


# ------------------------------------------------------------------
# Finding lifecycle types (F034.1)
# ------------------------------------------------------------------


class FindingState(str, Enum):
    """State machine for tracked findings."""

    NEW = "new"
    SUPPRESSED = "suppressed"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class FindingAction(str, Enum):
    """Action returned by FindingStore.ingest()."""

    TRIAGE = "triage"
    SUPPRESS = "suppress"
    ESCALATE = "escalate"


class OutcomeSignal(str, Enum):
    """Outcome signal for tuner feedback (F034.3)."""

    STRONG_POSITIVE = "strong_positive"
    POSITIVE = "positive"
    WEAK_NEGATIVE = "weak_negative"
    NEGATIVE = "negative"
    STRONG_NEGATIVE = "strong_negative"
    NEUTRAL = "neutral"


@dataclass
class EscalationConfig:
    """Time thresholds for finding escalation."""

    low_to_normal_hours: int = 72
    normal_to_high_hours: int = 24
    high_realert_hours: int = 12
    accumulation_threshold: int = 5


@dataclass
class TunableParam:
    """A tunable check parameter with bounds for self-tuning (F034.3)."""

    name: str
    value: float
    min_val: float
    max_val: float
    step: float
    pinned: bool = False  # manual override, skip auto-tuning


@dataclass
class TrackedFinding:
    """A finding tracked through its lifecycle."""

    finding: Finding
    fingerprint: str
    state: FindingState = FindingState.NEW
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    seen_count: int = 1
    escalated: bool = False
    resolved_at: datetime | None = None
    outcome: OutcomeSignal | None = None
    outcome_at: datetime | None = None
    reopen_count: int = 0  # flapping detection
    last_escalated_at: datetime | None = None  # for periodic re-alert throttling
    absent_ticks: int = 0  # consecutive ticks where check ran but didn't report this finding


@dataclass
class TuningAdjustment:
    """Record of a single parameter adjustment from the tuner."""

    check_name: str
    param_name: str
    old_value: float
    new_value: float
    direction: str  # "relax" | "tighten" | "rollback"
    sample_count: int
    positive_rate: float
    negative_rate: float


@dataclass
class TuningReport:
    """Result of a tuning pass."""

    adjustments: list[TuningAdjustment] = field(default_factory=list)
    skipped_checks: list[str] = field(default_factory=list)
    timestamp: datetime | None = None
