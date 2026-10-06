"""Which harness path is running a turn (harness-autonomy roadmap, Phase 1a).

Before this module the only context signal reaching tool dispatch was
``is_background: bool`` — heartbeat triage, dynamic checks, callbacks,
schedules, DAG nodes, companion agent actions and spawn_task all collapsed to
the same value. A capability policy (Phase 2a), an idempotency key (Phase 2b)
and the durable ledger (Phase 1b) each need to know WHICH path is running, so
the caller that starts a turn says so once, here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args
from uuid import UUID

from nous.brain.intentions import AUTHORITIES, AUTHORITY_INTERNAL, AUTHORITY_OWNER

ContextKind = Literal[
    "interactive",         # REST /chat, /chat/stream — a person is in the loop
    "mcp",                 # MCP tool call — another agent is waiting on the answer
    "subtask",             # spawn_task / spawn_sync (worker or inline)
    "dag_node",            # a subtask launched by the DAG orchestrator
    "scheduled",           # a schedule_task fire
    "agent_action",        # F092.2 companion app.act tap
    "heartbeat_triage",    # heartbeat cognitive triage of findings
    "heartbeat_check",     # F034.5 DynamicCheck run
    "heartbeat_callback",  # F034.6 on_complete callback
    "dag_summary",         # F087 agent-authored DAG summary
    "background",          # a background turn whose caller named no kind
    "continuation",        # F099: Nous's own turn on a background result (internal_only)
    "approved_action",     # F099: one owner-approved proposal, run with no model
]
CONTEXT_KINDS: tuple[str, ...] = get_args(ContextKind)
# A caller is waiting on the turn. Everything else runs with nobody in the loop.
FOREGROUND_KINDS: frozenset[str] = frozenset({"interactive", "mcp"})


def _as_uuid(value: Any) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Immutable description of the harness path running a turn."""

    kind: ContextKind
    session_id: str | None = None
    # The session that CREATED this turn's subtask (heart.subtasks.parent_session_id).
    # Prod has subtasks spawned by dag-summary turns; without this their origin
    # is invisible and they look like plain spawns.
    parent_session_id: str | None = None
    subtask_id: UUID | None = None
    dag_id: UUID | None = None
    dag_node_id: UUID | None = None
    dag_node_name: str | None = None
    schedule_id: str | None = None
    surface_id: str | None = None
    # Tools a heartbeat check or callback declared (Phase 2a policy). None:
    # nothing declared -- an empty list is the DB default and means "all".
    declared_tools: tuple[str, ...] | None = None
    check_name: str | None = None  # the check a callback belongs to
    # One logical run across its retries (a heartbeat callback's attempts):
    # the Phase 2b idempotency scope, so a retry cannot re-send.
    run_id: str | None = None
    # Phase 2.8: the DAG node is declared undoable — every tool call from
    # this turn must use a compensable tool.
    undoable: bool = False
    # F098: where the conversation lives ('telegram:<chat_id>'), never a
    # session id. Set by REST chat; spawn_task / dag_create store it so the
    # result reaches the conversation after its session has expired.
    channel: str | None = None
    # F099 Phase 0a: the spawning turn's Plan decision (TurnContext.decision_id).
    # pre_turn sets it after this frozen context was built, so the runner
    # carries it in with dataclasses.replace once pre_turn returns.
    decision_id: str | None = None
    # F099 I3: the intention this turn works under, read from the subtask
    # row's lineage stamp (metadata.intention) or a DAG check's. A spawn from
    # this turn joins that lineage. Phase 1 only carries it: nothing narrows
    # a tool set on ``authority`` until Phase 2.
    intention_id: UUID | None = None
    root_intention_id: UUID | None = None
    authority: str = AUTHORITY_OWNER
    # F099 Phase 2: set only by the continuation runner (a continuation turn) and
    # by execute_approved_proposal (an approved_action call). Defaults leave every
    # other context unchanged.
    proposal_id: UUID | None = None  # approved_action: the proposal being run
    arrival_id: UUID | None = None  # continuation: the arrival this turn decides
    claim_token: UUID | None = None  # continuation: the claim this turn runs under
    spawn_blocked: bool = False  # continuation: the root is at its depth or spawn limit

    def __post_init__(self) -> None:
        if self.kind not in CONTEXT_KINDS:
            raise ValueError(f"unknown execution context kind {self.kind!r}")
        if self.authority not in AUTHORITIES:
            raise ValueError(f"unknown authority {self.authority!r}")
        if self.kind == "approved_action" and (self.proposal_id is None or len(self.declared_tools or ()) != 1):
            raise ValueError("an approved_action context needs a proposal_id and exactly one declared tool")
        if self.kind == "continuation" and (
            self.authority != AUTHORITY_INTERNAL or self.intention_id is None or self.root_intention_id is None
        ):
            raise ValueError("a continuation context is internal_only and names its intention and its root")

    @property
    def is_background(self) -> bool:
        return self.kind not in FOREGROUND_KINDS

    @classmethod
    def for_subtask(cls, subtask: Any, session_id: str) -> ExecutionContext:
        """Derive the context of a subtask turn from its ``heart.subtasks`` row.

        The row says who created it: the DAG orchestrator stamps ``dag_node_id``
        + ``metadata.dag_id``/``node_name``; companion agent actions stamp
        ``metadata.a2ui_surface_id``; the scheduler stamps
        ``metadata.schedule_id``. A row with none of them is a plain spawn —
        ``parent_session_id`` still says which session spawned it.
        ``getattr`` defaults keep SimpleNamespace test doubles working.
        ``metadata.intention`` (F099) is the lineage stamp: no lookup, so
        nothing here can fail open.
        """
        meta = getattr(subtask, "metadata_", None)
        if not isinstance(meta, dict):
            meta = {}
        dag_node_id = _as_uuid(getattr(subtask, "dag_node_id", None))
        kind: ContextKind
        if dag_node_id is not None or meta.get("dag_id"):
            kind = "dag_node"
        elif meta.get("a2ui_surface_id"):
            kind = "agent_action"
        elif meta.get("schedule_id"):
            kind = "scheduled"
        else:
            kind = "subtask"
        intention_id, root_intention_id, authority = lineage_from_stamp(meta.get("intention"))
        return cls(
            kind=kind,
            session_id=session_id,
            parent_session_id=getattr(subtask, "parent_session_id", None) or None,
            subtask_id=_as_uuid(getattr(subtask, "id", None)),
            dag_id=_as_uuid(meta.get("dag_id")),
            dag_node_id=dag_node_id,
            dag_node_name=meta.get("node_name") or None,
            schedule_id=meta.get("schedule_id") or None,
            surface_id=meta.get("a2ui_surface_id") or None,
            undoable=bool(meta.get("undoable")),
            intention_id=intention_id,
            root_intention_id=root_intention_id,
            authority=authority,
        )


def lineage_from_stamp(stamp: Any) -> tuple[UUID | None, UUID | None, str]:
    """``(intention_id, root_intention_id, authority)`` from a lineage stamp.

    No stamp: a row from before F099, or made with the flag off, so owner. A
    stamp that cannot be read fails CLOSED to internal_only: a damaged
    lineage may only lose tools, never gain them (I3).
    """
    if stamp is None:
        return None, None, AUTHORITY_OWNER
    if not isinstance(stamp, dict):
        return None, None, AUTHORITY_INTERNAL
    intention_id = _as_uuid(stamp.get("id"))
    root_id = _as_uuid(stamp.get("root_id"))
    authority = stamp.get("authority")
    if intention_id is None or root_id is None or authority not in AUTHORITIES:
        return intention_id, root_id, AUTHORITY_INTERNAL
    return intention_id, root_id, authority


def resolve_context(
    context: ExecutionContext | None,
    *,
    is_background: bool,
    session_id: str | None,
) -> ExecutionContext:
    """The context a turn actually runs under.

    An explicit context wins; its ``is_background`` is authoritative. Callers
    that pass only the legacy flag get ``interactive`` or the generic
    ``background`` kind. ``is_background=True`` with a foreground context is a
    programming error — the two disagree about whether anyone is waiting.
    """
    if context is None:
        return ExecutionContext(
            kind="background" if is_background else "interactive",
            session_id=session_id,
        )
    if is_background and not context.is_background:
        raise ValueError(f"is_background=True contradicts a foreground ExecutionContext ({context.kind})")
    return context
