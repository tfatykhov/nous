"""Heartbeat data structures (F034).

Lightweight dataclasses for check results and triage findings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class Finding:
    """A single finding from a heartbeat check."""

    source: str
    summary: str
    urgency: Literal["high", "normal", "low"] = "normal"
    needs_action: bool = False
    raw_data: dict[str, Any] = field(default_factory=dict)
    check_name: str = ""


@dataclass
class CheckResult:
    """Result of running a single check."""

    has_updates: bool = False
    findings: list[Finding] = field(default_factory=list)


@dataclass
class HeartbeatResult:
    """Result of a cognitive triage session."""

    response: str = ""
    tokens_used: int = 0
