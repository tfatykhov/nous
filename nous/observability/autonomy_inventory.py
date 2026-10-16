"""Autonomy inventory (autonomy plan v2, A2): what each tool did, and who was in the loop when it did.

Read-only, from the durable execution ledger. A read-only call writes no ledger row, so only
side-effecting tools appear. The inventory is a measurement, not a policy: it grants nothing.

A row is a run only if it succeeded: prod's ``dag_create`` had 149 error rows among 315, so the
bucket counts (``by_context``, ``side_effects``) and the de-facto level read the SUCCESSES, and
``by_status`` shows how many attempts failed or were refused. A refused row (status ``blocked``)
did not run at all.

Buckets: ``owner_present`` is a turn the owner started (interactive, mcp, and ``agent_action``: the
owner's companion app.act tap, F092.2), ``approved`` is ``approved_action``, and every other kind,
including one added later, is ``autonomous``.

Limit: ``approved`` counts only the F099 ``approved_action`` path (a proposal the owner approved).
A call that ran inside a DAG after an approval node is bucketed ``autonomous`` here, because the
ledger row names the DAG node's context, not the approval card.

Censors are written outside the ledger (``heart.censors``), so they get their own block.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.api.execution_context import FOREGROUND_KINDS
from nous.api.tool_classes import TOOL_CLASSES
from nous.storage.models import Censor, ExecutionLedgerEntry

INVENTORY_DAYS = 30
# The owner started the turn. To the runtime agent_action is a background kind (is_background), so it
# is added here rather than to FOREGROUND_KINDS, which the tool policy and is_background read.
OWNER_PRESENT_KINDS: frozenset[str] = FOREGROUND_KINDS | {"agent_action"}
APPROVED_KINDS: frozenset[str] = frozenset({"approved_action"})
PROVENANCES: tuple[str, ...] = ("auto", "agent", "human")  # heart.censors.provenance


def context_bucket(context_kind: str) -> str:
    """``owner_present`` (interactive, mcp, agent_action), ``approved`` (approved_action), else ``autonomous``."""
    if context_kind in OWNER_PRESENT_KINDS:
        return "owner_present"
    if context_kind in APPROVED_KINDS:
        return "approved"
    return "autonomous"


def de_facto_level(autonomous_successes: int) -> str:
    """``L0`` (every run had a person in the loop) or ``acts_without_asking`` (at least one run did not)."""
    return "L0" if autonomous_successes == 0 else "acts_without_asking"


def _status_bucket(status: str) -> str:
    return status if status in ("success", "error", "blocked") else "other"  # other: pending, unknown


async def get_autonomy_inventory(
    session: AsyncSession, agent_id: str, *, days: int = INVENTORY_DAYS, now: datetime | None = None
) -> list[dict[str, Any]]:
    """One row per tool with a ledger row in the last ``days``, most-used first. One grouped query."""
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    L = ExecutionLedgerEntry
    result = await session.execute(
        select(L.tool_name, L.context_kind, L.side_effect_type, L.status, func.count().label("n"))
        .where(L.agent_id == agent_id, L.created_at >= since)
        .group_by(L.tool_name, L.context_kind, L.side_effect_type, L.status)
    )
    tools: dict[str, dict[str, Any]] = {}
    for row in result:
        tool = tools.setdefault(
            row.tool_name,
            {
                "tool_name": row.tool_name,
                "total": 0,
                "by_status": {"success": 0, "error": 0, "blocked": 0, "other": 0},
                "by_context": {"owner_present": 0, "approved": 0, "autonomous": 0},
                "side_effects": defaultdict(int),
            },
        )
        tool["total"] += row.n
        tool["by_status"][_status_bucket(row.status)] += row.n
        if row.status == "success":
            tool["by_context"][context_bucket(row.context_kind)] += row.n
            tool["side_effects"][row.side_effect_type] += row.n
    out = []
    for tool in tools.values():
        declared = TOOL_CLASSES.get(tool["tool_name"])
        tool["side_effects"] = dict(tool["side_effects"])
        tool["compensable"] = bool(declared and declared.compensable)
        tool["de_facto_level"] = de_facto_level(tool["by_context"]["autonomous"])
        out.append(tool)
    return sorted(out, key=lambda t: (-t["total"], t["tool_name"]))


async def get_censor_activity(
    session: AsyncSession, agent_id: str, *, days: int = INVENTORY_DAYS, now: datetime | None = None
) -> dict[str, Any]:
    """Censors created and deactivated in the last ``days``, by provenance (auto, agent, human).

    ``deactivated`` counts inactive censors last touched in the window: the table keeps no deactivation
    time, so ``updated_at`` stands in for it, and any later update to an inactive censor re-counts it.
    """
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    by_provenance = {p: {"created": 0, "deactivated": 0} for p in PROVENANCES}
    created = await session.execute(
        select(Censor.provenance, func.count())
        .where(Censor.agent_id == agent_id, Censor.created_at >= since)
        .group_by(Censor.provenance)
    )
    for provenance, n in created:
        by_provenance.setdefault(provenance, {"created": 0, "deactivated": 0})["created"] = n
    deactivated = await session.execute(
        select(Censor.provenance, func.count())
        .where(Censor.agent_id == agent_id, Censor.active.is_(False), Censor.updated_at >= since)
        .group_by(Censor.provenance)
    )
    for provenance, n in deactivated:
        by_provenance.setdefault(provenance, {"created": 0, "deactivated": 0})["deactivated"] = n
    return {"days": days, "by_provenance": by_provenance}
