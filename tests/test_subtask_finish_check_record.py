"""F113 fixed finish checks: the record is merged into ``heart.subtasks.metadata`` by ``complete``.

The database tests use real Postgres (the merge is a JSONB ``||``), each with its own agent id.
The statement tests use a stand-in session that keeps the UPDATE ``complete`` builds, to pin
that without a patch the statement is the one ``complete`` built before the patch existed.
"""

from __future__ import annotations

import inspect
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from nous.heart.subtasks import SubtaskManager

RECORD = {"v": 1, "mode": "shadow", "verdict": "flagged", "failed": ["named_file_missing"], "why": None}


def _manager(db) -> SubtaskManager:
    return SubtaskManager(db, f"test-finish-check-{uuid.uuid4().hex[:8]}")


@pytest.mark.postgres_only
async def test_a_patch_adds_the_record_and_keeps_the_other_keys(db):
    manager = _manager(db)
    subtask = await manager.create("research X", metadata={"max_attempts": 1, "session_id": "shared-1"})

    await manager.complete(subtask.id, "done", final_outcome="completed", metadata_patch={"finish_check": RECORD})

    row = await manager.get(subtask.id)
    assert row.status == "completed"
    assert row.metadata_ == {"max_attempts": 1, "session_id": "shared-1", "finish_check": RECORD}


@pytest.mark.postgres_only
async def test_a_patch_on_a_row_with_empty_metadata(db):
    manager = _manager(db)
    subtask = await manager.create("research X")

    await manager.complete(subtask.id, "done", final_outcome="completed", metadata_patch={"finish_check": RECORD})

    assert (await manager.get(subtask.id)).metadata_ == {"finish_check": RECORD}


@pytest.mark.postgres_only
async def test_a_row_cancelled_first_keeps_its_metadata(db):
    manager = _manager(db)
    subtask = await manager.create("research X", metadata={"max_attempts": 1})
    assert await manager.cancel(subtask.id) is True

    await manager.complete(subtask.id, "done", final_outcome="completed", metadata_patch={"finish_check": RECORD})

    row = await manager.get(subtask.id)
    assert row.status == "cancelled"
    assert row.metadata_ == {"max_attempts": 1}


@pytest.mark.postgres_only
async def test_without_a_patch_the_metadata_is_untouched(db):
    manager = _manager(db)
    subtask = await manager.create("research X", metadata={"max_attempts": 1})

    await manager.complete(subtask.id, "done", final_outcome="completed")

    assert (await manager.get(subtask.id)).metadata_ == {"max_attempts": 1}


# ---------------------------------------------------------------------------
# The statement complete builds
# ---------------------------------------------------------------------------


class _StatementKeeper:
    """A database whose sessions keep each statement and report one row updated."""

    def __init__(self) -> None:
        self.statements: list = []

    @asynccontextmanager
    async def session(self):
        keeper = self

        class _Session:
            async def execute(self, statement):
                keeper.statements.append(statement)
                return SimpleNamespace(rowcount=1)

            async def commit(self) -> None:
                return None

        yield _Session()


def _bound_names(statement) -> list[str]:
    return sorted(statement.compile(dialect=postgresql.dialect()).params)


ALL_ARGS = dict(
    final_outcome="completed",
    report_jsonb={"summary": "s"},
    attempts=2,
    tokens_in=10,
    tokens_out=5,
    tool_calls_made=3,
    payload_schema_valid=True,
    delivered=False,
)
# The bound names of the UPDATE that complete() built before metadata_patch existed (bf260047).
BASE_NAMES_MINIMAL = ["completed_at", "id_1", "result", "status", "status_1"]
BASE_NAMES_ALL = sorted(
    [
        "attempts",
        "completed_at",
        "delivered",
        "final_outcome",
        "id_1",
        "payload_schema_valid",
        "report_jsonb",
        "result",
        "status",
        "status_1",
        "tokens_in",
        "tokens_out",
        "tool_calls_made",
    ]
)


async def test_without_a_patch_the_statement_is_unchanged():
    keeper = _StatementKeeper()
    manager = SubtaskManager(keeper, "agent-test")

    await manager.complete(uuid.uuid4(), "done")
    await manager.complete(uuid.uuid4(), "done", **ALL_ARGS)
    await manager.complete(uuid.uuid4(), "done", metadata_patch=None, **ALL_ARGS)

    assert [_bound_names(s) for s in keeper.statements] == [BASE_NAMES_MINIMAL, BASE_NAMES_ALL, BASE_NAMES_ALL]
    assert all("metadata" not in str(s.compile(dialect=postgresql.dialect())) for s in keeper.statements)


async def test_with_a_patch_the_same_statement_also_sets_the_metadata():
    keeper = _StatementKeeper()
    manager = SubtaskManager(keeper, "agent-test")

    await manager.complete(uuid.uuid4(), "done", metadata_patch={"finish_check": RECORD}, **ALL_ARGS)

    [statement] = keeper.statements
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "metadata=(coalesce(heart.subtasks.metadata, jsonb_build_object()) ||" in " ".join(sql.split())
    assert set(BASE_NAMES_ALL) < set(_bound_names(statement))


def test_fail_takes_no_patch():
    assert "metadata_patch" not in inspect.signature(SubtaskManager.fail).parameters
