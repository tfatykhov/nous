"""F099 Phase 2e-4: the tokens of failed attempts count against the root's token budget (carry-over 2), and
migration 085."""

from __future__ import annotations

from pathlib import Path

import pytest
from f099_support import (
    CONT,
    claim,
    env_factory,  # noqa: F401
    make_child,
    make_root,
    record,
    runner_env,  # noqa: F401
    say,
    set_intention,
)
from sqlalchemy import select, text

from nous.brain import continuation
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import Intention, IntentionArrival

pytestmark = pytest.mark.postgres_only  # FOR NO KEY UPDATE, savepoints, = ANY(array)
MIGRATION = (
    Path(__file__).resolve().parents[1] / "sql" / "migrations" / "085_intention_failed_tokens_and_cancel_index.sql"
)


async def _fail(env, got, *, max_attempts=3, tokens=(0, 0)):
    async with env.db.session() as s:
        out = await continuation.fail_attempt(
            s, env.agent, got, max_attempts=max_attempts, settings=env.settings, tokens=tokens
        )
        await s.commit()
    return out


async def _limits(env, root_id):
    async with env.db.session() as s:
        return await continuation.root_limits(s, env.agent, root_id, settings=env.settings)


async def _failed_tokens(env, *intentions):
    async with env.db.session() as s:
        rows = await s.execute(
            select(Intention.id, Intention.failed_tokens).where(Intention.id.in_([i.id for i in intentions]))
        )
    return dict(rows.all())


async def _pair(env):
    """A root and its child, both with a result, claimed together: the child is the deepest."""
    root = await make_root(env)
    child = await make_child(env, root)
    await record(env, root)
    await record(env, child)
    got = await claim(env, root.id)
    assert got.deepest.id == child.id and len(got.intentions) == 2
    return root, child, got


# ---- migration 085 ---------------------------------------------------------------------------------------------


async def test_migration_085_adds_the_column_and_the_index(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    async with env.db.session() as s:
        column = (
            await s.execute(
                text(
                    "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_schema = 'brain' AND table_name = 'intentions' AND column_name = 'failed_tokens'"
                )
            )
        ).one()
        index = (
            await s.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = 'brain' AND indexname = 'idx_intentions_cancelled'"
                )
            )
        ).scalar_one()
    assert (column.data_type, column.is_nullable, column.column_default) == ("integer", "NO", "0")
    assert "root_cancelled_at IS NOT NULL" in index


def test_the_migration_keeps_the_runners_conventions():
    """The migrator splits on a semicolon, so no comment may hold one; and it must be idempotent."""
    lines = MIGRATION.read_text(encoding="utf-8").splitlines()
    assert all(";" not in line for line in lines if line.startswith("--"))
    body = "\n".join(line for line in lines if not line.startswith("--"))
    assert "ADD COLUMN IF NOT EXISTS" in body and "CREATE INDEX IF NOT EXISTS" in body and "DO $$" not in body


# ---- fail_attempt ----------------------------------------------------------------------------------------------


async def test_a_retried_attempt_charges_the_deepest_intention_once(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, child, got = await _pair(env)
    assert await _fail(env, got, tokens=(100, 10)) == "retry"
    assert await _failed_tokens(env, root, child) == {root.id: 0, child.id: 110}
    assert (await _limits(env, root.id)).tokens == 110  # the root's budget counts it once, not once per claimed row


async def test_failed_attempts_add_up(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, child, got = await _pair(env)
    await _fail(env, got, tokens=(100, 10))
    again = await claim(env, root.id)
    await _fail(env, again, tokens=(50, 5))
    assert (await _limits(env, root.id)).tokens == 165


async def test_a_failure_that_spent_nothing_writes_no_charge(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, child, got = await _pair(env)
    assert await _fail(env, got) == "retry"
    assert await _failed_tokens(env, root, child) == {root.id: 0, child.id: 0}


async def test_a_stale_claim_charges_nothing(env_factory):  # noqa: F811
    """The charge goes through the same fence as the release: a claim that was released or cancelled is not charged."""
    env = await env_factory(**CONT)
    root, child, got = await _pair(env)
    await set_intention(env, root.id, state="cancelled", claim_token=None)
    await set_intention(env, child.id, state="cancelled", claim_token=None)
    assert await _fail(env, got, tokens=(100, 10)) == continuation.FAIL_LOST
    assert await _failed_tokens(env, root, child) == {root.id: 0, child.id: 0}


async def test_the_failed_report_books_the_last_attempts_tokens_on_its_arrival_and_keeps_the_earlier_ones(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, child, got = await _pair(env)
    await _fail(env, got, max_attempts=2, tokens=(100, 10))  # attempt 1 of 2: retried, charged on the child
    again = await claim(env, root.id)
    assert await _fail(env, again, max_attempts=2, tokens=(70, 7)) == continuation.CLOSE_FAILED_REPORT
    async with env.db.session() as s:
        (arrival,) = (await s.execute(select(IntentionArrival).where(IntentionArrival.root_id == root.id))).scalars()
    assert (arrival.outcome, arrival.tokens_in, arrival.tokens_out) == ("failed_report", 70, 7)
    assert (await _limits(env, root.id)).tokens == 110 + 77  # each attempt once: no double count at the cap


async def test_a_lease_release_knows_no_usage_and_charges_nothing(env_factory):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    env = await env_factory(**CONT)
    root, child, got = await _pair(env)
    async with env.db.session() as s:
        released = await continuation.release_stale_claims(
            s,
            env.agent,
            lease_s=900,
            max_attempts=3,
            settings=env.settings,
            now=datetime.now(UTC) + timedelta(hours=1),
        )
        await s.commit()
    assert set(released) == {root.id, child.id}
    assert (await _limits(env, root.id)).tokens == 0


# ---- the runner -------------------------------------------------------------------------------------------------


async def test_a_lineage_whose_turns_keep_failing_reaches_its_token_budget(runner_env):  # noqa: F811
    """Each attempt answers in prose (110 tokens), then the follow-up raises: a failed attempt that spent 110. Before
    2e those tokens were counted nowhere, so this lineage would fail for ever. After ten of them the budget (1000)
    is spent and the next claim escalates to the owner instead of running another turn."""
    steps = []
    for _ in range(10):
        steps += [[say("let me think")], RuntimeError("the follow-up failed")]
    env = await runner_env(*steps, continuation_max_tokens_per_root=1000, continuation_max_attempts=50)
    cont = ContinuationRunner(
        database=env.db, settings=env.settings, runner=env.runner, heart=env.heart, brain=env.brain, bus=env.bus
    )
    root = await make_root(env)
    await record(env, root)
    for _ in range(10):
        assert await cont.run_arrival(root.id) is None  # a failed attempt
    assert (await _limits(env, root.id)).tokens == 1100 and (await _limits(env, root.id)).escalate == "budget_tokens"
    assert len(env.model.calls) == 20
    done = await cont.run_arrival(root.id)  # the gate: no model call, a report with the reason
    assert done is not None and len(env.model.calls) == 20
    async with env.db.session() as s:
        (arrival,) = (await s.execute(select(IntentionArrival).where(IntentionArrival.root_id == root.id))).scalars()
    assert (arrival.gate_reason, arrival.decision) == ("budget_tokens", "report")
