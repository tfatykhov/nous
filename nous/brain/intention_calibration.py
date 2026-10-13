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
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from nous.brain import continuation
from nous.storage.models import Decision, Intention, IntentionArrival, IntentionProposal

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


def _candidates(agent_id: str, limit: int):
    """Arrivals with a Brain record, not graded by anybody, whose rows already hold a signal (S3). The signal
    predicate is in the WHERE so an open root with nothing to say cannot take the page's slots; oldest first."""
    root = aliased(Intention)
    open_row = aliased(Intention)
    staged = aliased(IntentionProposal)
    return (
        select(IntentionArrival.id, IntentionArrival.root_id, IntentionArrival.decision_record_id)
        .join(
            Decision,
            and_(Decision.id == IntentionArrival.decision_record_id, Decision.agent_id == IntentionArrival.agent_id),
        )
        .join(root, and_(root.agent_id == IntentionArrival.agent_id, root.id == IntentionArrival.root_id))
        .where(
            IntentionArrival.agent_id == agent_id,
            or_(Decision.outcome.is_(None), Decision.outcome.in_(REGRADED_OUTCOMES)),
            or_(
                root.root_cancelled_at.is_not(None),
                root.root_expired_at.is_not(None),
                ~exists().where(
                    open_row.agent_id == agent_id,
                    open_row.root_id == IntentionArrival.root_id,
                    open_row.state.in_(continuation.OPEN_STATES),
                ),
                exists().where(
                    staged.agent_id == agent_id,
                    staged.arrival_id == IntentionArrival.id,
                    staged.state.in_(PROPOSAL_SIGNAL_STATES),
                ),
            ),
        )
        .order_by(IntentionArrival.decided_at, IntentionArrival.id)
        .limit(limit)
    )


async def _load_evidence(session: AsyncSession, agent_id: str, root_ids: list[UUID]) -> dict[UUID, RootEvidence]:
    """One bulk read per fact, for every root of the page."""
    markers = {
        row.id: row
        for row in (
            await session.execute(
                select(Intention.id, Intention.root_cancelled_at, Intention.root_expired_at).where(
                    Intention.agent_id == agent_id, Intention.id.in_(root_ids)
                )
            )
        ).all()
    }
    open_roots = set(
        (
            await session.execute(
                select(Intention.root_id)
                .where(
                    Intention.agent_id == agent_id,
                    Intention.root_id.in_(root_ids),
                    Intention.state.in_(continuation.OPEN_STATES),
                )
                .distinct()
            )
        ).scalars()
    )
    ttl_roots = set(
        (
            await session.execute(
                select(Intention.root_id)
                .where(
                    Intention.agent_id == agent_id,
                    Intention.root_id.in_(root_ids),
                    Intention.state == continuation.STATE_EXPIRED,
                )
                .distinct()
            )
        ).scalars()
    )
    lasts = {
        row.root_id: LastArrival(row.n, row.decision, row.outcome, row.gate_reason)
        for row in (
            await session.execute(
                select(
                    IntentionArrival.root_id,
                    IntentionArrival.n,
                    IntentionArrival.decision,
                    IntentionArrival.outcome,
                    IntentionArrival.gate_reason,
                )
                .where(IntentionArrival.agent_id == agent_id, IntentionArrival.root_id.in_(root_ids))
                .distinct(IntentionArrival.root_id)
                .order_by(IntentionArrival.root_id, IntentionArrival.n.desc())
            )
        ).all()
    }
    proposals: dict[UUID, dict[UUID, list[tuple[UUID, str, str | None]]]] = defaultdict(lambda: defaultdict(list))
    for row in (
        await session.execute(
            select(
                IntentionProposal.root_id,
                IntentionProposal.arrival_id,
                IntentionProposal.id,
                IntentionProposal.state,
                IntentionProposal.decided_by,
            ).where(
                IntentionProposal.agent_id == agent_id,
                IntentionProposal.root_id.in_(root_ids),
                IntentionProposal.arrival_id.is_not(None),
                IntentionProposal.state.in_(PROPOSAL_SIGNAL_STATES),
            )
        )
    ).all():
        proposals[row.root_id][row.arrival_id].append((row.id, row.state, row.decided_by))
    evidence: dict[UUID, RootEvidence] = {}
    for root_id in root_ids:
        mark = markers.get(root_id)
        cancelled = mark is not None and mark.root_cancelled_at is not None
        expired = mark is not None and mark.root_expired_at is not None
        evidence[root_id] = RootEvidence(
            cancelled=cancelled,
            expired=expired,
            ttl=root_id in ttl_roots,
            closed=not cancelled and not expired and root_id not in open_roots,
            last=lasts.get(root_id),
            proposals=dict(proposals.get(root_id, {})),
        )
    return evidence


async def grade_arrivals(
    session: AsyncSession, agent_id: str, *, brain: Any, settings: Any, limit: int = GRADE_BATCH
) -> int:
    """Grade up to ``limit`` arrival decisions whose rows now hold a signal. Returns how many reviews it wrote.

    ``session`` only reads. Each review is its own transaction (``Brain.review`` with no session, which also emits
    ``decision_reviewed`` after its commit), written with ``preserve_graded=True``: a ``success``, ``partial`` or
    ``failure`` written by anyone, the owner in an interactive turn included, is never replaced, while ``pending``
    and ``noise`` are (that is the repair). ``only_if_unreviewed`` is NOT used: it would leave every background
    ``noise`` row as it is. A review that lands between the read and the write raises ``ValueError`` (a graded row,
    or ``DecisionNotFound``, its subclass): that row is skipped and the pass goes on. Idempotent: a graded row is
    no longer a candidate. Does nothing with the flag off or without a Brain."""
    if brain is None or not continuation.calibration_enabled(settings):
        return 0
    rows = (await session.execute(_candidates(agent_id, limit))).all()
    if not rows:
        return 0
    evidence = await _load_evidence(session, agent_id, sorted({row.root_id for row in rows}))
    written = 0
    for row in rows:
        grade = grade_for(row.id, evidence[row.root_id])
        if grade is None:
            continue
        try:
            await brain.review(
                row.decision_record_id,
                outcome=grade.outcome,
                result=grade.result,
                reviewer=INTENTION_REVIEWER,
                preserve_graded=True,
            )
        except ValueError:  # graded since the read, or deleted: it stands
            logger.info("F099: arrival decision %s was settled by somebody else; left as it is", row.decision_record_id)
            continue
        except Exception:
            logger.warning("F099: could not grade arrival decision %s", row.decision_record_id, exc_info=True)
            continue
        written += 1
    if written == 0 and len(rows) >= limit:
        # Every candidate of a full page was skipped: a page that cannot move. Visible, never silent.
        logger.warning("F099: a calibration pass of %d candidates graded none; the page may be stuck", len(rows))
    return written
