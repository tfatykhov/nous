"""Run guards for heartbeat dynamic checks (F107 phase 3).

A dynamic check whose run mostly executes a script and reports "nothing" can carry a guard in its row's
``metadata``, in the shape of a schedule's guard (``nous/handlers/schedule_guard.py``)::

    {"guard": {"name": "gridbot_tick", "mode": "shadow", "is_action": true, "timeout_seconds": 540}}

The name picks an entry of the same owner allowlist, ``NOUS_SCHEDULE_GUARD_COMMANDS``, and
``NOUS_SCHEDULE_GUARDS_ENABLED`` switches these guards off too. ``DynamicCheck.run`` runs the command once,
before the model turn, through ``schedule_guard.run_guard``; the skip, shadow and fall-through rules are the
schedule guard's. The guard runs inside the check's own timeout (the heartbeat's ``wait_for`` over ``run()``),
so a guard whose timeout plus ``TURN_RESERVE_SECONDS`` and the kill bound does not fit in the check's
``timeout_seconds`` is refused before it starts (``invalid``): the outer timeout must never be what kills it.
So is a guard on a check a DAG owns: a skip counts as a successful run, which the DAG reads as the node's
completion. A refused or unresolved guard is not run, and the model is told so.

Every run of the command is recorded in a persisted ``check_guard_ran`` event (skips included, as the check has
no row of its own to hold it), a refused one in ``check_guard_unresolved``, and after the turn of a shadow run
whether the model acted (``schedule_guard.acted_signals``) in ``check_guard_acted``.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from nous.handlers import schedule_guard
from nous.utils import _KILL_WAIT_SECONDS

if TYPE_CHECKING:
    from nous.config import Settings
    from nous.events import EventBus
    from nous.storage.database import Database

logger = logging.getLogger(__name__)

RAN, UNRESOLVED, ACTED = "check_guard_ran", "check_guard_unresolved", "check_guard_acted"
# The least the check's model turn keeps of the check's timeout after its guard.
TURN_RESERVE_SECONDS = 60
# How long run_guard may still wait after a timeout (kill_process_group's bound).
KILL_BOUND_SECONDS = int(_KILL_WAIT_SECONDS)

Emit = Callable[[str, dict[str, Any], str], Awaitable[None]]  # event type, data, session id
Ledger = Callable[[str], Awaitable[Sequence[tuple[str, str, Mapping[str, Any]]]]]  # session id -> entries


@dataclass
class CheckGuardContext:
    """What a check needs to run its guard, built once by the loader and shared by its checks."""

    enabled: bool
    commands: Mapping[str, str]
    emit: Emit
    ledger: Ledger
    # With the durable ledger off no entry is ever written, so "ledger" cannot show up in an acted record.
    ledger_persisted: bool = True
    # Names of the checks whose guard is running now: a second run of one meanwhile is skipped.
    running: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class Resolved:
    spec: schedule_guard.GuardSpec | None
    reason: str | None = None  # not_allowlisted or invalid, when spec is None
    detail: str | None = None


def max_guard_timeout(check_timeout: int) -> int:
    """The largest guard timeout that still leaves the turn its reserve inside the check's timeout."""
    return check_timeout - TURN_RESERVE_SECONDS - KILL_BOUND_SECONDS


def resolve(raw: Any, commands: Mapping[str, str], *, check_timeout: int | None, dag_owned: bool) -> Resolved:
    """The guard a check's ``metadata.guard`` describes, or why it may not run."""
    if not isinstance(raw, dict):
        return Resolved(None, "invalid", "metadata.guard is not a JSON object")
    if dag_owned:
        return Resolved(None, "invalid", "a DAG owns this check: a skip would read as the node's completion")
    if not isinstance(check_timeout, int) or isinstance(check_timeout, bool) or check_timeout < 1:
        # dynamic_checks.timeout_seconds is nullable
        return Resolved(None, "invalid", "the check has no timeout to fit the guard in")
    if "is_action" in raw and not isinstance(raw["is_action"], bool):
        # parse_guard would read it as not an action: the default timeout and a receipt that does not say so
        return Resolved(None, "invalid", f"metadata.guard.is_action must be true or false (got {raw['is_action']!r:.40})")
    timeout = raw.get("timeout_seconds")
    if raw.get("is_action") is True and (not isinstance(timeout, int) or isinstance(timeout, bool)):
        # parse_guard would fall back to 60 s, which would cut a long action short on every run
        return Resolved(None, "invalid", "a guard that is the check's action needs an integer timeout_seconds")
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout < 1:
        # parse_guard would clamp it to 1 s and kill the command on every run
        return Resolved(None, "invalid", f"guard timeout_seconds must be at least 1 (got {timeout})")
    # No cap here: a timeout too long for the check is refused below, never cut down to fit it silently.
    spec = schedule_guard.parse_guard(raw, commands, max_timeout=sys.maxsize)
    if spec is None:
        return Resolved(None, "not_allowlisted")
    limit = max_guard_timeout(check_timeout)
    if spec.timeout_seconds > limit:
        return Resolved(
            None,
            "invalid",
            f"guard timeout {spec.timeout_seconds}s + {TURN_RESERVE_SECONDS}s turn reserve + {KILL_BOUND_SECONDS}s "
            f"kill bound exceeds the check timeout {check_timeout}s (largest guard timeout: {limit}s)",
        )
    return Resolved(spec)


def not_run_note(raw: Any, resolved: Resolved) -> str:
    """What the model is told about a guard that was configured but not run."""
    raw = raw if isinstance(raw, dict) else {}
    why = "it names no allowlisted command" if resolved.reason == "not_allowlisted" else resolved.detail
    lines = ["", "", f"[Run guard receipt] This check's guard {raw.get('name')!r} did NOT run: {why}."]
    if raw.get("is_action") is True:
        lines.append(
            "The guard is this check's action, so the action has NOT happened in this run. Report this configuration "
            "problem as a finding; whether to run the action yourself (once at most) is up to your instructions."
        )
    return "\n".join(lines)


def acted(
    findings: list[dict[str, Any]], *, ledger: Sequence[tuple[str, str, Mapping[str, Any]]], command: str
) -> list[str]:
    """``schedule_guard.acted_signals`` for a check's run: its findings and its ledger entries.

    A check has no report summary, inbox row or push of its own (its findings go to triage), so only
    ``finding`` and ``ledger`` can show up.
    """
    row = SimpleNamespace(status="completed", report_jsonb={"findings": findings})
    return schedule_guard.acted_signals(row, inbox_written=False, pushed=False, ledger=ledger, guard_command=command)


def observed(response: str | None) -> bool:
    """Whether "no finding" could be seen in a turn's answer: it is the check's JSON object.

    An answer that does not parse yields no findings either way, so, like a run with no structured
    report (``schedule_guard.observed``), it cannot show the model did nothing.
    """
    from nous.handlers import parse_llm_json

    try:
        data = parse_llm_json(response or "")
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and "has_findings" in data


def context_for(settings: Settings, bus: EventBus | None, db: Database) -> CheckGuardContext:
    """The loader's guard context: records go out on the bus (which persists them), the ledger is read by session."""
    from nous.events import Event
    from nous.storage.models import ExecutionLedgerEntry as L

    async def emit(event_type: str, data: dict[str, Any], session_id: str) -> None:
        if bus is not None:  # with the bus off nothing is persisted; the dashboard says so
            # events.session_id is 100 characters; the data's run_id keeps the whole session id
            await bus.emit(Event(type=event_type, agent_id=settings.agent_id, session_id=session_id[:100], data=data))

    async def ledger(session_id: str) -> list[tuple[str, str, Mapping[str, Any]]]:
        async with db.session() as session:
            rows = await session.execute(
                select(L.tool_name, L.status, L.key_args)
                .where(L.agent_id == settings.agent_id, L.session_id == session_id)
                .order_by(L.created_at, L.id)
            )
            return [(tool, status, key_args or {}) for tool, status, key_args in rows.all()]

    return CheckGuardContext(
        enabled=settings.schedule_guards_enabled,
        commands=settings.schedule_guard_commands,
        emit=emit,
        ledger=ledger,
        ledger_persisted=settings.execution_ledger_persist_enabled,
    )


async def emit_safely(ctx: CheckGuardContext, event_type: str, data: dict[str, Any], session_id: str) -> None:
    """Persist one guard record. Never raises: a lost record must not cost the run."""
    try:
        await ctx.emit(event_type, data, session_id)
    except Exception:
        logger.exception("Check %s: could not record %s", data.get("check_name"), event_type)
