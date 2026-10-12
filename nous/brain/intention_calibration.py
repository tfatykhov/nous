"""F099 Phase 3d: the grade of an arrival's Brain decision, derived from the lineage's own rows.

Spec 4.7 says the arrival decisions are "graded through the existing decision reviewer". That reviewer grades
the model's stated confidence and a PR the intent happens to name, and model sweeps label the rest ``noise``
(28 of 48 in prod). So the arrival decisions are taken out of those feeds (``Brain`` readers,
``exclude_intention_arrivals``) and this module is their one grader. A lineage grade is evidence about
autonomy, not a strategy: the card distiller and the procedure learner ignore it.

``grade_for`` is the signal table and nothing else; ``grade_arrivals`` reads the rows and writes the reviews.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

from nous.brain import continuation

logger = logging.getLogger(__name__)

# The reviewer tag of every review this module writes. The distiller reads it (decision_reviewer re-exports it).
INTENTION_REVIEWER = "f099"
GRADE_BATCH = 100  # candidates per pass
# The outcomes this grader may replace. `success`, `partial` and `failure` are grades somebody made and are never
# replaced (Brain.review preserve_graded). `superseded` carries a replaced-by link that a review would clear.
REGRADED_OUTCOMES = ("pending", "noise")
BOUNDS_GATES = ("budget_turns", "budget_tokens", "budget_stall", "limit_depth", "limit_spawns", "past_deadline")
# The proposal states that are a signal about the arrival that staged them.
PROPOSAL_SIGNAL_STATES = (
    continuation.PROPOSAL_EXECUTED,
    continuation.PROPOSAL_FAILED,
    continuation.PROPOSAL_REJECTED,
)
SYSTEM_ACTOR = "system"


@dataclass(frozen=True, slots=True)
class Grade:
    outcome: str
    result: str


@dataclass(frozen=True, slots=True)
class LastArrival:
    """The root's highest-n arrival, of any kind (a gate, a fallback and a failed report included)."""

    n: int
    decision: str | None
    outcome: str
    gate_reason: str | None


@dataclass(frozen=True, slots=True)
class RootEvidence:
    """What one root's rows say, read once per pass. ``proposals`` maps an arrival id to
    ``(proposal id, state, decided_by)`` of its proposals in a signal state."""

    cancelled: bool
    expired: bool
    ttl: bool  # some intention of the lineage is `expired`: the TTL sweep ran (end_hanging_root writes the marker only)
    closed: bool  # no open intention and no root marker
    last: LastArrival | None
    proposals: dict[UUID, list[tuple[UUID, str, str | None]]] = field(default_factory=dict)


def _after(n: int) -> str:
    return f"{n} arrival" if n == 1 else f"{n} arrivals"


def grade_for(arrival_id: UUID, ev: RootEvidence) -> Grade | None:
    """The grade for one candidate arrival, or None when the rows hold no signal yet. THE signal table
    (3d plan): the first row that matches wins."""
    owned = ev.proposals.get(arrival_id, ())
    for proposal_id, state, _by in owned:  # row 1
        if state in (continuation.PROPOSAL_EXECUTED, continuation.PROPOSAL_FAILED):
            return Grade("success", f"owner approved proposal {continuation.short_id(proposal_id)} ({state})")
    for proposal_id, state, by in owned:  # row 2
        if state == continuation.PROPOSAL_REJECTED and by != SYSTEM_ACTOR:
            return Grade("failure", f"owner rejected proposal {continuation.short_id(proposal_id)}")
    n = ev.last.n if ev.last is not None else 0
    if ev.cancelled:  # row 3 (owner answer 4: partial, not failure)
        return Grade("partial", f"chain cancelled after {_after(n)}")
    if ev.expired:  # row 4
        cause = "ttl" if ev.ttl else "hanging"
        return Grade("failure", f"chain ended by {cause} after {_after(n)}")
    last = ev.last
    if last is None or not ev.closed:
        return None
    if last.gate_reason in BOUNDS_GATES:  # row 5
        return Grade("failure", f"chain ended by {last.gate_reason} after {_after(n)}")
    if last.gate_reason == "plan_resolved":  # row 6
        return Grade("partial", f"plan resolved elsewhere after {_after(n)}")
    if last.gate_reason is None:
        if last.outcome in (continuation.OUTCOME_FALLBACK, continuation.OUTCOME_FAILED):  # row 7
            return Grade("failure", f"chain could not report ({last.outcome}) after {_after(n)}")
        if last.outcome == continuation.OUTCOME_RESOLVED:
            if last.decision == "report":  # row 8
                return Grade("success", f"chain reported after {_after(n)}")
            if last.decision == "drop":  # row 9
                return Grade("partial", f"chain dropped after {_after(n)}")
    return None
