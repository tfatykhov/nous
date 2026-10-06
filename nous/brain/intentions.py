"""F099: intentions — why a piece of background work was spawned.

One ``brain.intentions`` row per spawn: the "K-line" that re-activates the
state of mind the work was started in. The store that creates the work row
writes it in the same transaction (I1), through :func:`prepare_intention` and
:func:`insert_prepared`. A subtask row also carries the lineage stamp
(``metadata.intention``) that ``ExecutionContext.for_subtask`` reads (I3).

Phase 1 (``NOUS_INTENTIONS_ENABLED``) records and closes. Every intention
closes as ``legacy`` when its source finishes, because F098 or the legacy path
still consumes the result. Nothing here routes a result or narrows a tool
set; that is Phase 2.

Callers use the module, not its names (``intentions.insert_prepared(...)``),
so one monkeypatch reaches every store in the fault-injection tests.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, cast, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from nous.storage.database import Database
from nous.storage.models import ExecutionDAG, Intention, Schedule, Subtask

logger = logging.getLogger(__name__)

WAKE_CONTINUE = "continue"
WAKE_REMEMBER = "remember"
WAKE_REPORT = "report"
WAKE_NONE = "none"
WAKE_CONTAINER = "container"
# What a spawn tool's wake_policy argument may ask for (a container is a schedule's alone).
MODEL_WAKE_POLICIES: tuple[str, ...] = (WAKE_CONTINUE, WAKE_REMEMBER, WAKE_REPORT, WAKE_NONE)

AUTHORITY_OWNER = "owner"
AUTHORITY_INTERNAL = "internal_only"
AUTHORITIES: tuple[str, ...] = (AUTHORITY_OWNER, AUTHORITY_INTERNAL)

SOURCE_SUBTASK = "subtask"
SOURCE_DAG = "dag"
SOURCE_SCHEDULE = "schedule"

STATE_PENDING = "pending"
STATE_CLOSED = "closed"
# Phase 1: F098 Phase A or the legacy path consumed the result.
CLOSE_LEGACY = "legacy"

# origin_kind of a spawn no model turn made; a model spawn's is its ContextKind.
ORIGIN_SCHEDULER = "scheduler"
ORIGIN_WORK_QUEUE = "work_queue"
ORIGIN_APP_ACT = "app_act"
ORIGIN_REST = "rest"  # REST POST /schedules: an operator's or another agent's call, no turn
_CODE_PATH_POLICY = {
    ORIGIN_SCHEDULER: WAKE_NONE,
    ORIGIN_WORK_QUEUE: WAKE_REMEMBER,
    ORIGIN_APP_ACT: WAKE_NONE,
    ORIGIN_REST: WAKE_NONE,
}

# Turns whose spawns default to continue: someone is waiting on the turn, or
# it is Nous's own follow-up work (section 4.1 table).
_CONTINUE_KINDS = frozenset({"interactive", "mcp", "heartbeat_check", "heartbeat_callback"})
_FOREGROUND_KINDS = frozenset({"interactive", "mcp"})

# Kept here rather than imported: nous.dag.store imports this module.
# tests/test_f099_intentions.py pins the DAG set to nous.dag.store's.
TERMINAL_SUBTASK_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")
TERMINAL_DAG_STATUSES: tuple[str, ...] = ("completed", "failed", "partial", "cancelled")
# A subtask that ended without a result (cancelled) never gets a result_at.
RESULT_SUBTASK_STATUSES: tuple[str, ...] = ("completed", "failed")

# Sent as _intention_id when the turn's lineage stamp could not be read: the
# spawn is refused instead of becoming a new owner root.
UNREADABLE_LINEAGE = "unreadable-lineage"

INTENT_MAX_CHARS = 500
INTENT_HELP = (
    "one line saying why this work is needed and what you will do with its result, "
    "e.g. 'Check the snow report so I can tell the user whether to drive up tomorrow'."
)
INTENT_REQUIRED_ERROR = f"intent is required: {INTENT_HELP}"
# I2: only these turns are refused a spawn without an intent. Every other
# kind's prompt was written before intent existed, so a missing one is
# generated (a refused spawn there, such as the F087 summary turn's email
# subtask, would silently break delivery).
INTENT_REFUSING_KINDS = frozenset({"interactive", "mcp", "continuation"})


class IntentionRootClosed(ValueError):
    """The lineage's root was cancelled or expired: nothing new may join it (I1)."""


class IntentionLimitReached(ValueError):
    """The new child would exceed its root's depth or spawn limit (spec 4.6). The text is what
    the model sees: it says what to do instead."""


class IntentionParentMissing(ValueError):
    """The spawning turn names an intention that does not exist. Refused, never
    turned into a new root: a spawn inside a lineage is always a child (I3)."""


class IntentArgumentError(ValueError):
    """A spawn tool's intent or wake_policy is unusable; the text is what the model sees."""


def intent_line(text: Any) -> str:
    """The one-line intent: whitespace collapsed, capped at INTENT_MAX_CHARS."""
    return " ".join(str(text or "").split())[:INTENT_MAX_CHARS]


def parse_uuid(value: Any) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def lineage_stamp(intention_id: UUID, root_id: UUID, authority: str) -> dict[str, str]:
    """The lineage a subtask row carries as ``metadata.intention`` (I3)."""
    return {"id": str(intention_id), "root_id": str(root_id), "authority": authority}


@dataclass(frozen=True, slots=True)
class IntentionSpec:
    """What a spawn path knows about the intention it records. The store
    resolves lineage, authority and wake policy inside its own transaction."""

    intent: str
    # The spawning turn's ContextKind, or ORIGIN_SCHEDULER / ORIGIN_WORK_QUEUE / ORIGIN_APP_ACT.
    origin_kind: str
    inline: bool = False  # spawn_task(await_result=true) and spawn_sync
    container: bool = False  # schedule_task: the schedule's container
    wake_policy: str | None = None  # a tool's argument, or a code path's own policy
    parent_id: UUID | None = None  # the spawning turn's own intention: this one is its child
    # A schedule fire: its container, by source. Lineage only; the fire is a root.
    parent_source: tuple[str, str] | None = None
    origin_session_id: str | None = None
    origin_channel: str | None = None
    origin_decision_id: UUID | None = None
    # The spawning turn's authority (F099 Phase 2). A child is never wider than the turn
    # that spawned it: this narrows what the parent ROW says, and never widens it. None
    # for a code path (scheduler, work queue, app.act, REST): owner.
    origin_authority: str | None = None
    # F099 Phase 2c: the lineage limits (max_depth, max_spawns) a child is checked against, and
    # the root TTL its deadline is computed from. Filled by ``with_bounds``; None leaves the row
    # exactly as Phase 1 wrote it.
    limits: tuple[int, int] | None = None
    ttl_hours: float | None = None


def intention_kwargs(spec: IntentionSpec | None) -> dict[str, IntentionSpec]:
    """``{"intention": spec}`` for a store call, or ``{}``: with the flag off the
    call is exactly the one it was before F099."""
    return {"intention": spec} if spec is not None else {}


def enabled(settings: Any) -> bool:
    """NOUS_INTENTIONS_ENABLED, read so that a mocked Settings counts as off."""
    return getattr(settings, "intentions_enabled", False) is True


def _continuation_on(settings: Any) -> bool:
    """NOUS_CONTINUATION_ENABLED with intentions on. nous.brain.continuation imports this module,
    so it cannot be asked here."""
    return enabled(settings) and getattr(settings, "continuation_enabled", False) is True


def ttl_for(settings: Any) -> float | None:
    """The root TTL in hours a new intention's deadline is computed from; None with continuation off."""
    return float(settings.intention_root_ttl_hours) if _continuation_on(settings) else None


def limits_for(settings: Any) -> tuple[int, int] | None:
    """(max_depth, max_spawns) a child is checked against; None with continuation off."""
    if not _continuation_on(settings):
        return None
    return int(settings.continuation_max_depth), int(settings.continuation_max_spawns_per_root)


def with_bounds(spec: IntentionSpec | None, settings: Any) -> IntentionSpec | None:
    """``spec`` carrying this process's TTL and limits. A schedule's container has no TTL (spec 4.1
    Schedules). With continuation off nothing is added and the same object comes back, so every
    construction site is Phase 1's call, unchanged."""
    if spec is None:
        return None
    ttl = None if spec.container else ttl_for(settings)
    limits = limits_for(settings)
    if ttl is None and limits is None:
        return spec
    return replace(spec, ttl_hours=ttl, limits=limits)


@dataclass(frozen=True, slots=True)
class ParentView:
    """What a child's resolution needs from its parent intention."""

    id: UUID
    root_id: UUID
    depth: int
    authority: str
    wake_policy: str
    deadline: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PreparedIntention:
    """An intention resolved inside the spawning transaction, not yet inserted."""

    id: UUID
    root_id: UUID
    parent_id: UUID | None
    depth: int
    authority: str
    wake_policy: str
    spec: IntentionSpec
    deadline: datetime | None = None

    @property
    def stamp(self) -> dict[str, str]:
        return lineage_stamp(self.id, self.root_id, self.authority)


def resolve_wake_policy(spec: IntentionSpec, parent: ParentView | None) -> str:
    """Section 4.1, "Default wake policy, by origin". ``parent`` is the spawning
    turn's own intention (None for a root, and for a schedule fire)."""
    if spec.container:
        # A schedule's intention is always its container, whatever its parent
        # (D3). schedule_task is denylisted inside an internal-only lineage,
        # so that case is unreachable, but it still resolves to container.
        return WAKE_CONTAINER
    if (parent is not None and parent.authority == AUTHORITY_INTERNAL) or spec.origin_authority == AUTHORITY_INTERNAL:
        # Inside an internal-only lineage, or from an internal-only turn, the argument is
        # ignored: every result goes back to the continuation (an inline one returns in-turn).
        return WAKE_NONE if spec.inline else WAKE_CONTINUE
    if spec.inline:
        return WAKE_NONE  # the result comes back in the same turn
    if spec.origin_kind in _CODE_PATH_POLICY:
        default = _CODE_PATH_POLICY[spec.origin_kind]
    elif spec.origin_kind in _CONTINUE_KINDS:
        default = WAKE_CONTINUE
    elif spec.origin_kind == "dag_summary":
        default = WAKE_NONE  # delivery mechanics
    elif parent is not None and parent.wake_policy in MODEL_WAKE_POLICIES:
        default = parent.wake_policy  # a background turn's spawn follows its own intention
    else:
        default = WAKE_NONE
    requested = spec.wake_policy
    if requested is None:
        return default
    if requested == WAKE_CONTINUE and default != WAKE_CONTINUE and spec.origin_kind not in _FOREGROUND_KINDS:
        # D7: the ARGUMENT may ask for continue only from a foreground turn, or
        # where the origin's default is continue already. A code path or a
        # background turn (a scheduled notify=true fire's remember included)
        # falls back to its default, so it cannot start an autonomous chain.
        logger.info(
            "F099: wake_policy=continue from a %s spawn whose default is %s; keeping %s (D7)",
            spec.origin_kind,
            default,
            default,
        )
        return default
    return requested


def generated_intent(origin_kind: str, text: Any) -> str:
    """``"<origin_kind>: <first non-blank line of text>"``, capped: the intent
    of a spawn whose caller wrote none (a background turn, REST)."""
    first = next((part for part in str(text or "").splitlines() if part.strip()), "")
    return intent_line(f"{origin_kind}: {first}")


def intent_refused(intent: Any, origin_kind: str | None) -> bool:
    """I2: a blank intent is refused only in a foreground or continuation turn."""
    return not intent_line(intent) and (origin_kind or "background") in INTENT_REFUSING_KINDS


def _known_authority(value: Any) -> str | None:
    """No claim is None; an unknown value fails closed to internal_only."""
    if value is None:
        return None
    return value if value in AUTHORITIES else AUTHORITY_INTERNAL


def spec_from_tool_call(
    *,
    intent: Any,
    wake_policy: Any,
    origin_kind: str | None,
    fallback_text: str | None = None,
    inline: bool = False,
    container: bool = False,
    origin_session_id: str | None = None,
    origin_channel: str | None = None,
    decision_id: str | None = None,
    intention_id: str | None = None,
    origin_authority: str | None = None,
) -> IntentionSpec:
    """The intention a model spawn tool records (I2).

    A blank ``intent`` is refused in an INTENT_REFUSING_KINDS turn. In any
    other kind it is generated as ``"<origin_kind>: <first line of
    fallback_text>"`` (the task, or a DAG's description), and the spawn goes
    ahead. Raises IntentArgumentError, whose text is what the model is told,
    and IntentionParentMissing when the injected ``_intention_id`` cannot be
    read (a lineage spawn is never turned into a new root).
    """
    kind = origin_kind or "background"
    if intent_refused(intent, kind):
        # One INFO line per refusal, so the rollout read (Review Focus 1) can
        # count them with one grep, whichever tool refused.
        logger.info("F099: intent is required: a %s turn spawned without one; refused", kind)
        raise IntentArgumentError(INTENT_REQUIRED_ERROR)
    line = intent_line(intent) or generated_intent(kind, fallback_text)
    policy = wake_policy or None
    if policy is not None and policy not in MODEL_WAKE_POLICIES:
        raise IntentArgumentError(f"wake_policy must be one of: {', '.join(MODEL_WAKE_POLICIES)}")
    if intention_id == UNREADABLE_LINEAGE:
        raise IntentionParentMissing(
            "this turn's lineage could not be read, so spawning is refused in this turn; "
            "finish the turn without spawning"
        )
    parent_id = parse_uuid(intention_id)
    if intention_id and parent_id is None:
        raise IntentionParentMissing(f"intention {intention_id!r} is not an id")
    return IntentionSpec(
        intent=line,
        origin_kind=kind,
        inline=inline,
        container=container,
        wake_policy=policy,
        parent_id=parent_id,
        origin_session_id=origin_session_id,
        origin_channel=origin_channel,
        origin_decision_id=parse_uuid(decision_id),
        origin_authority=_known_authority(origin_authority),
    )


def _view(row: Intention) -> ParentView:
    return ParentView(
        id=row.id,
        root_id=row.root_id,
        depth=row.depth,
        authority=row.authority,
        wake_policy=row.wake_policy,
        deadline=row.deadline,
        created_at=row.created_at,
    )


async def _hold_open_root(session: AsyncSession, agent_id: str, root_id: UUID) -> None:
    """Read the root FOR SHARE: a cancel locks it to write root_cancelled_at,
    so the two conflict and a cancel stops new spawns (I1)."""
    row = (
        await session.execute(
            select(Intention.root_cancelled_at, Intention.root_expired_at)
            .where(Intention.agent_id == agent_id, Intention.id == root_id)
            .with_for_update(read=True)
        )
    ).first()
    if row is None or row.root_cancelled_at is not None or row.root_expired_at is not None:
        raise IntentionRootClosed(f"intention root {root_id} is cancelled or expired; nothing new may join it")


async def _hold_open_container(session: AsyncSession, agent_id: str, container: Intention) -> None:
    """A schedule fire commits only while its container is open and its
    schedule still fires (Codex: the fire-vs-cancel race).

    The tick loaded the schedule earlier, without a lock. The caller read the
    container FOR SHARE, and the schedule is read FOR SHARE here, in that
    order. A cancel writes the container row and a deactivation writes the
    schedule row, so each one conflicts with the fire. Either it commits
    first and the fire sees it, or it waits for the fire, which was then due
    before the cancel and is part of the cancel's cascade.
    """
    if (
        container.state != STATE_PENDING
        or container.root_cancelled_at is not None
        or container.root_expired_at is not None
    ):
        raise IntentionRootClosed(f"the container of schedule {container.source_id} is closed; no new fire")
    active = (
        await session.execute(
            select(Schedule.active)
            .where(Schedule.agent_id == agent_id, Schedule.id == parse_uuid(container.source_id))
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if not active:
        raise IntentionRootClosed(f"schedule {container.source_id} no longer fires; no new fire")


async def _check_limits(session: AsyncSession, agent_id: str, parent: ParentView, limits: tuple[int, int]) -> None:
    """Spec 4.6: refuse a child that would exceed its root's depth (exact, from the parent's
    depth) or spawn limit (one indexed count, in the spawning transaction, so two concurrent
    spawns can overshoot by the concurrency width: the gate re-checks at the next claim)."""
    max_depth, max_spawns = limits
    if parent.depth + 1 > max_depth:
        raise IntentionLimitReached(
            f"this work is already {parent.depth} step(s) deep and the depth limit is {max_depth}, so nothing more "
            "can be spawned under it; end the turn with resolve_intention (report, drop or ask) instead"
        )
    spawned = (
        await session.execute(
            select(func.count(Intention.id)).where(
                Intention.agent_id == agent_id, Intention.root_id == parent.root_id, Intention.depth > 0
            )
        )
    ).scalar_one()
    if spawned >= max_spawns:
        raise IntentionLimitReached(
            f"this work has already spawned {spawned} piece(s) of work and the spawn limit is {max_spawns}, so "
            "nothing more can be spawned under it; end the turn with resolve_intention (report, drop or ask) instead"
        )


def _deadline(spec: IntentionSpec, parent: ParentView | None) -> datetime | None:
    """A root gets created + TTL; a child gets the earlier of its parent's deadline and its own (spec 4.1).
    A parent with no deadline (a Phase 1 row) counts as its created_at + TTL (ruling R3), the rule the
    expiry judges a root by, so a child of an old root does not outlive it."""
    if spec.ttl_hours is None:
        return None
    ttl = timedelta(hours=spec.ttl_hours)
    own = datetime.now(UTC) + ttl
    if parent is None:
        return own
    if parent.deadline is not None:
        return min(parent.deadline, own)
    if parent.created_at is not None:
        return min(parent.created_at + ttl, own)
    return own


async def prepare_intention(session: AsyncSession, agent_id: str, spec: IntentionSpec) -> PreparedIntention:
    """Resolve ``spec`` inside the caller's transaction (I1).

    A child (``spec.parent_id``) joins its parent's lineage: the same root,
    one level deeper, never wider authority than the parent row or the
    spawning turn (I3, min of the two), and only while the root is
    open. A schedule fire (``spec.parent_source``) is a new root that keeps
    its container as ``parent_id``, for lineage only, and only while the
    container is open and its schedule active. A deadline is written only
    when the spec carries ``ttl_hours`` (Phase 2c).
    """
    parent: ParentView | None = None
    lineage_parent: ParentView | None = None
    if spec.parent_id is not None:
        row = await session.get(Intention, spec.parent_id)
        if row is None or row.agent_id != agent_id:
            raise IntentionParentMissing(f"intention {spec.parent_id} does not exist")
        parent = lineage_parent = _view(row)
        await _hold_open_root(session, agent_id, parent.root_id)
        if spec.limits is not None:
            await _check_limits(session, agent_id, parent, spec.limits)
    elif spec.parent_source is not None:
        kind, source_id = spec.parent_source
        row = (
            await session.execute(
                select(Intention)
                .where(
                    Intention.agent_id == agent_id,
                    Intention.source_kind == kind,
                    Intention.source_id == str(source_id),
                )
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if row is not None:
            await _hold_open_container(session, agent_id, row)
        # A schedule from before the flag has no container: no parent, no check.
        lineage_parent = _view(row) if row is not None else None
    new_id = uuid.uuid4()
    narrowed = (
        lineage_parent is not None and lineage_parent.authority == AUTHORITY_INTERNAL
    ) or spec.origin_authority == AUTHORITY_INTERNAL
    return PreparedIntention(
        id=new_id,
        root_id=parent.root_id if parent is not None else new_id,
        parent_id=lineage_parent.id if lineage_parent is not None else None,
        depth=parent.depth + 1 if parent is not None else 0,
        authority=AUTHORITY_INTERNAL if narrowed else AUTHORITY_OWNER,
        wake_policy=resolve_wake_policy(spec, parent),
        spec=spec,
        deadline=_deadline(spec, parent),
    )


async def insert_prepared(
    session: AsyncSession, agent_id: str, prepared: PreparedIntention, *, source_kind: str, source_id: Any
) -> None:
    """Insert the intention of the work row ``source_id`` in the caller's transaction."""
    spec = prepared.spec
    session.add(
        Intention(
            id=prepared.id,
            agent_id=agent_id,
            root_id=prepared.root_id,
            parent_id=prepared.parent_id,
            depth=prepared.depth,
            source_kind=source_kind,
            source_id=str(source_id),
            intent=intent_line(spec.intent),
            origin_kind=spec.origin_kind,
            origin_session_id=spec.origin_session_id,
            origin_channel=spec.origin_channel,
            origin_decision_id=spec.origin_decision_id,
            wake_policy=prepared.wake_policy,
            authority=prepared.authority,
            state=STATE_PENDING,
            deadline=prepared.deadline,
        )
    )
    await session.flush()


async def close_for_source(
    session: AsyncSession,
    agent_id: str,
    source_kind: str,
    source_id: Any,
    *,
    reason: str = CLOSE_LEGACY,
    with_result: bool = True,
) -> UUID | None:
    """Close the pending intention of a finished source.

    Returns the intention's id, closed now or earlier, or None when the
    source recorded none. result_at is set only when a result arrived: a
    subtask that completed or failed, or a DAG; never a container
    (``with_result=False``) or a cancelled subtask.
    """
    found = (
        await session.execute(
            select(Intention.id).where(
                Intention.agent_id == agent_id,
                Intention.source_kind == source_kind,
                Intention.source_id == str(source_id),
            )
        )
    ).scalar_one_or_none()
    if found is None:
        return None
    now = datetime.now(UTC)
    values: dict[str, Any] = {"state": STATE_CLOSED, "close_reason": reason, "closed_at": now, "updated_at": now}
    if with_result:
        if source_kind == SOURCE_SUBTASK:
            values["result_at"] = case(
                (
                    exists().where(
                        Subtask.agent_id == agent_id,
                        Subtask.id == cast(str(source_id), PG_UUID(as_uuid=True)),
                        Subtask.status.in_(RESULT_SUBTASK_STATUSES),
                    ),
                    now,
                ),
                else_=None,
            )
        else:
            values["result_at"] = now
    await session.execute(
        update(Intention).where(Intention.id == found, Intention.state == STATE_PENDING).values(**values)
    )
    return found


async def lineage_for_source(
    session: AsyncSession, agent_id: str, source_kind: str, source_id: Any
) -> dict[str, str] | None:
    """The lineage stamp of a source's intention (a DAG's, for its node launches), or None."""
    row = (
        await session.execute(
            select(Intention.id, Intention.root_id, Intention.authority).where(
                Intention.agent_id == agent_id,
                Intention.source_kind == source_kind,
                Intention.source_id == str(source_id),
            )
        )
    ).first()
    return lineage_stamp(row.id, row.root_id, row.authority) if row is not None else None


async def wake_policy_for_source(session: AsyncSession, agent_id: str, source_kind: str, source_id: Any) -> str | None:
    """The wake policy recorded for a source's intention, or None: what a spawn tool reports back (D7)."""
    return (
        await session.execute(
            select(Intention.wake_policy).where(
                Intention.agent_id == agent_id,
                Intention.source_kind == source_kind,
                Intention.source_id == str(source_id),
            )
        )
    ).scalar_one_or_none()


async def close_finished_sources(
    session: AsyncSession,
    agent_id: str,
    *,
    limit: int,
    reason: str = CLOSE_LEGACY,
    exclude_policies: tuple[str, ...] = (),
) -> list[UUID]:
    """Close pending intentions whose subtask or DAG has finished, oldest first.

    The backstop for a writer's one-shot close (a worker cancelled at
    shutdown, a failed hook, an interrupted inline close). Containers are
    close_finished_containers' job. result_at is set only when a result
    arrived: not for a cancelled subtask. ``exclude_policies``: wake policies
    this pass leaves to their own writer: F099 Phase 2's ``continue`` and
    ``report``.
    """
    source_uuid = cast(Intention.source_id, PG_UUID(as_uuid=True))
    subtask_done = exists().where(
        Subtask.agent_id == agent_id, Subtask.id == source_uuid, Subtask.status.in_(TERMINAL_SUBTASK_STATUSES)
    )
    dag_done = exists().where(
        ExecutionDAG.agent_id == agent_id,
        ExecutionDAG.id == source_uuid,
        ExecutionDAG.status.in_(TERMINAL_DAG_STATUSES),
    )
    conditions = [Intention.agent_id == agent_id, Intention.state == STATE_PENDING]
    if exclude_policies:
        conditions.append(Intention.wake_policy.notin_(exclude_policies))
    due = (
        select(Intention.id)
        .where(*conditions)
        .where(
            or_(
                and_(Intention.source_kind == SOURCE_SUBTASK, subtask_done),
                and_(Intention.source_kind == SOURCE_DAG, dag_done),
            )
        )
        .order_by(Intention.created_at)
        .limit(limit)
    )
    now = datetime.now(UTC)
    got_result = or_(
        Intention.source_kind == SOURCE_DAG,
        exists().where(
            Subtask.agent_id == agent_id, Subtask.id == source_uuid, Subtask.status.in_(RESULT_SUBTASK_STATUSES)
        ),
    )
    result = await session.execute(
        update(Intention)
        .where(Intention.id.in_(due), Intention.state == STATE_PENDING)
        .values(
            state=STATE_CLOSED,
            close_reason=reason,
            result_at=case((got_result, now), else_=None),
            closed_at=now,
            updated_at=now,
        )
        .returning(Intention.id)
        .execution_options(synchronize_session=False)
    )
    return list(result.scalars().all())


async def close_finished_containers(
    session: AsyncSession, agent_id: str, *, limit: int, reason: str = CLOSE_LEGACY
) -> list[UUID]:
    """Close pending containers whose schedule no longer fires (inactive or deleted).

    The repair for ScheduleManager._close_container, which runs after the
    deactivation commits and may fail or never run (the process exits in
    between). A container has no TTL, so without this it would stay pending
    for good. It never received a result, so result_at stays empty.
    """
    still_firing = exists().where(
        Schedule.agent_id == agent_id,
        Schedule.id == cast(Intention.source_id, PG_UUID(as_uuid=True)),
        Schedule.active.is_(True),
    )
    due = (
        select(Intention.id)
        .where(Intention.agent_id == agent_id, Intention.state == STATE_PENDING)
        .where(Intention.source_kind == SOURCE_SCHEDULE, ~still_firing)
        .order_by(Intention.created_at)
        .limit(limit)
    )
    now = datetime.now(UTC)
    result = await session.execute(
        update(Intention)
        .where(Intention.id.in_(due), Intention.state == STATE_PENDING)
        .values(state=STATE_CLOSED, close_reason=reason, closed_at=now, updated_at=now)
        .returning(Intention.id)
        .execution_options(synchronize_session=False)
    )
    return list(result.scalars().all())


class IntentionStore:
    """Reads and closes for callers that hold no transaction of their own."""

    def __init__(self, database: Database, agent_id: str) -> None:
        self._db = database
        self._agent_id = agent_id

    async def get_for_source(self, source_kind: str, source_id: Any) -> Intention | None:
        async with self._db.session() as session:
            return (
                await session.execute(
                    select(Intention).where(
                        Intention.agent_id == self._agent_id,
                        Intention.source_kind == source_kind,
                        Intention.source_id == str(source_id),
                    )
                )
            ).scalar_one_or_none()

    async def lineage_for_source(self, source_kind: str, source_id: Any) -> dict[str, str] | None:
        async with self._db.session() as session:
            return await lineage_for_source(session, self._agent_id, source_kind, source_id)

    async def wake_policy_for_source(self, source_kind: str, source_id: Any) -> str | None:
        async with self._db.session() as session:
            return await wake_policy_for_source(session, self._agent_id, source_kind, source_id)

    async def close_for_source(self, source_kind: str, source_id: Any, *, reason: str = CLOSE_LEGACY) -> UUID | None:
        async with self._db.session() as session:
            found = await close_for_source(session, self._agent_id, source_kind, source_id, reason=reason)
            await session.commit()
        return found
