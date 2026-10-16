"""Autonomy inventory: per tool, who was in the loop and how often it worked."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio

from nous.api.execution_context import CONTEXT_KINDS
from nous.observability import autonomy_inventory as inv
from nous.storage.models import Censor, ExecutionLedgerEntry

NOW = datetime.now(UTC)


@pytest_asyncio.fixture
async def agent() -> str:
    return f"test-inv-{uuid.uuid4().hex[:8]}"


def row(agent, tool, context, *, status="success", effect="write", age_days=1) -> ExecutionLedgerEntry:
    return ExecutionLedgerEntry(
        agent_id=agent,
        context_kind=context,
        tool_name=tool,
        side_effect_type=effect,
        status=status,
        created_at=NOW - timedelta(days=age_days),
    )


def censor(agent, provenance, *, active=True, created_days=1, updated_days=1) -> Censor:
    return Censor(
        agent_id=agent,
        trigger_pattern=f"p-{uuid.uuid4().hex[:6]}",
        reason="r",
        provenance=provenance,
        active=active,
        created_at=NOW - timedelta(days=created_days),
        updated_at=NOW - timedelta(days=updated_days),
    )


async def inventory(db, agent, **kw):
    async with db.session() as s:
        return {t["tool_name"]: t for t in await inv.get_autonomy_inventory(s, agent, now=NOW, **kw)}


def test_every_context_kind_has_exactly_one_bucket():
    buckets = {kind: inv.context_bucket(kind) for kind in CONTEXT_KINDS}
    # agent_action is the owner's companion tap (F092.2): the owner starts the turn, as in a chat
    assert {k for k, b in buckets.items() if b == "owner_present"} == {"interactive", "mcp", "agent_action"}
    assert {k for k, b in buckets.items() if b == "approved"} == {"approved_action"}
    assert {"scheduled", "dag_node", "subtask", "heartbeat_triage", "continuation"} <= {
        k for k, b in buckets.items() if b == "autonomous"
    }
    assert inv.context_bucket("a_kind_added_later") == "autonomous"  # an unknown path is not "asked"


def test_the_de_facto_level():
    assert inv.de_facto_level(0) == "L0"
    assert inv.de_facto_level(1) == "acts_without_asking"


async def test_counts_by_status_context_bucket_effect_and_compensable(db, agent):
    async with db.session() as s:
        s.add_all(
            [
                row(agent, "send_email", "interactive", effect="external"),
                row(agent, "send_email", "approved_action", effect="external"),
                row(agent, "send_email", "dag_node", effect="external"),
                row(agent, "send_email", "scheduled", effect="external"),
                row(agent, "write_file", "interactive"),
                row(agent, "write_file", "mcp"),
                row(agent, "bash", "interactive"),
                row(agent, "bash", "heartbeat_triage", effect="external"),
                row(agent, "learn_fact", "agent_action"),
            ]
        )
        await s.commit()

    got = await inventory(db, agent)

    mail = got["send_email"]
    assert mail["total"] == 4 and mail["by_status"] == {"success": 4, "error": 0, "blocked": 0, "other": 0}
    assert mail["by_context"] == {"owner_present": 1, "approved": 1, "autonomous": 2}
    assert mail["side_effects"] == {"external": 4}
    assert (mail["compensable"], mail["de_facto_level"]) == (False, "acts_without_asking")
    wf = got["write_file"]
    assert wf["by_context"] == {"owner_present": 2, "approved": 0, "autonomous": 0}
    assert (wf["compensable"], wf["de_facto_level"]) == (True, "L0")  # write_file has a compensator
    assert got["bash"]["side_effects"] == {"write": 1, "external": 1}
    tapped = got["learn_fact"]  # run only from the owner's companion tap
    assert tapped["by_context"] == {"owner_present": 1, "approved": 0, "autonomous": 0}
    assert tapped["de_facto_level"] == "L0"
    async with db.session() as s:
        ordered = [t["tool_name"] for t in await inv.get_autonomy_inventory(s, agent, now=NOW)]
    assert ordered == ["send_email", "bash", "write_file", "learn_fact"]


async def test_a_tool_that_mostly_fails_is_counted_by_its_successes(db, agent):
    async with db.session() as s:
        s.add_all(
            [
                row(agent, "dag_create", "scheduled", status="error"),
                row(agent, "dag_create", "scheduled", status="error"),
                row(agent, "dag_create", "scheduled", status="blocked"),  # refused: it did not run
                row(agent, "dag_create", "scheduled", status="unknown"),
                row(agent, "dag_create", "scheduled"),
                row(agent, "dag_create", "interactive"),
            ]
        )
        s.add_all([row(agent, "schedule_task", "scheduled", status="error")])  # only failures
        await s.commit()
    got = await inventory(db, agent)
    dag = got["dag_create"]
    assert dag["total"] == 6 and dag["by_status"] == {"success": 2, "error": 2, "blocked": 1, "other": 1}
    assert dag["by_context"] == {"owner_present": 1, "approved": 0, "autonomous": 1}
    only_errors = got["schedule_task"]
    assert only_errors["by_context"]["autonomous"] == 0 and only_errors["de_facto_level"] == "L0"  # it never ran


async def test_old_and_foreign_rows_do_not_count(db, agent):
    async with db.session() as s:
        s.add_all(
            [
                row(agent, "dag_create", "scheduled", age_days=45),
                row("someone-else", "dag_create", "scheduled"),
                row(agent, "dag_create", "interactive"),
            ]
        )
        await s.commit()
    assert (await inventory(db, agent))["dag_create"]["total"] == 1
    assert (await inventory(db, agent, days=60))["dag_create"]["total"] == 2


async def test_a_tool_nobody_classified_is_not_compensable(db, agent):
    async with db.session() as s:
        s.add(row(agent, "a_tool_added_later", "scheduled"))
        await s.commit()
    assert (await inventory(db, agent))["a_tool_added_later"]["compensable"] is False


async def test_censors_created_and_deactivated_by_provenance(db, agent):
    async with db.session() as s:
        s.add_all(
            [
                censor(agent, "auto"),
                censor(agent, "agent"),
                censor(agent, "agent"),
                censor(agent, "agent"),
                censor(agent, "agent", created_days=60, updated_days=2, active=False),  # old, deactivated in the window
                censor(agent, "human", created_days=60, updated_days=60),  # untouched
                censor(agent, "auto", created_days=60, updated_days=40, active=False),  # deactivated before the window
                censor("someone-else", "agent"),
            ]
        )
        await s.commit()
    async with db.session() as s:
        got = await inv.get_censor_activity(s, agent, now=NOW)
    assert got == {
        "days": 30,
        "by_provenance": {
            "auto": {"created": 1, "deactivated": 0},
            "agent": {"created": 3, "deactivated": 1},
            "human": {"created": 0, "deactivated": 0},
        },
    }
