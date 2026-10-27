"""Migration 093 (typed waits for lineage DAGs): the wait node type and its wait_for column. The file the migrator runs
matches the ORM model the SQLite tests build."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from nous.dag.schemas import DAGNodeType
from nous.storage.migrator import _split_sql_statements
from nous.storage.models import DAGNode, ExecutionDAG

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "093_dag_wait_nodes.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(ln for ln in _sql().splitlines() if not ln.lstrip().startswith("--"))


def _type_names(check_sql: str) -> set[str]:
    inside = re.search(r"node_type IN \(([^)]*)\)", check_sql)
    assert inside is not None, check_sql
    return set(re.findall(r"'([a-z_]+)'", inside.group(1)))


def test_the_migration_swaps_the_type_constraint_and_adds_the_column():
    statements = _split_sql_statements(_sql())
    assert len(statements) == 4
    assert statements[0].startswith("ALTER TABLE nous_system.dag_nodes")
    assert "DROP CONSTRAINT IF EXISTS dag_nodes_node_type_check" in statements[0]
    assert "DROP CONSTRAINT IF EXISTS chk_dag_node_type" in statements[1]
    assert "ADD CONSTRAINT chk_dag_node_type" in statements[2]
    assert "ADD COLUMN IF NOT EXISTS wait_for JSONB" in statements[3]


def test_the_migration_has_nothing_the_splitter_would_mangle():
    sql = _sql()
    assert "/*" not in sql and "$$" not in sql
    assert not re.search(r"(?<!:):[A-Za-z_]", _code())  # text() reads :name as a bind parameter
    assert all(";" not in ln for ln in sql.splitlines() if ln.lstrip().startswith("--"))


def test_the_migration_creates_no_grants():
    # Grants belong to scripts/ops/db_roles.sql: the migrator runs as the application role.
    code = _code().upper()
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "AUTHORIZATION"):
        assert not re.search(rf"\b{word}\b", code), word


def test_the_type_list_matches_the_orm_model_and_the_enum():
    statement = _split_sql_statements(_sql())[2]
    (constraint,) = [c for c in DAGNode.__table__.constraints if c.name == "chk_dag_node_type"]
    in_model = _type_names(str(constraint.sqltext))
    assert _type_names(statement) == in_model == {t.value for t in DAGNodeType}
    assert "wait" in in_model


def test_wait_for_is_a_nullable_jsonb_column():
    column = DAGNode.__table__.c.wait_for
    assert column.nullable
    assert isinstance(column.type, JSONB)


def _dag(agent: str) -> ExecutionDAG:
    return ExecutionDAG(agent_id=agent, name="wait-migration", description="", source="conversation")


@pytest.mark.postgres_only
async def test_a_wait_node_row_round_trips(db):
    agent = f"test-wait-mig-{uuid.uuid4().hex[:8]}"
    try:
        async with db.session() as s:
            dag = _dag(agent)
            s.add(dag)
            await s.flush()
            s.add(DAGNode(dag_id=dag.id, name="w", node_type="wait", wait_for={"kind": "file", "path": "x"}))
            await s.commit()
            dag_id = dag.id
        async with db.session() as s:
            row = (await s.execute(text("SELECT node_type, wait_for FROM nous_system.dag_nodes WHERE dag_id = :d"),
                                   {"d": dag_id})).one()
        assert row.node_type == "wait"
        assert row.wait_for == {"kind": "file", "path": "x"}
    finally:
        async with db.session() as s:
            await s.execute(delete(ExecutionDAG).where(ExecutionDAG.agent_id == agent))
            await s.commit()


@pytest.mark.postgres_only
async def test_an_unknown_node_type_is_still_refused(db):
    agent = f"test-wait-mig-{uuid.uuid4().hex[:8]}"
    try:
        with pytest.raises(IntegrityError):
            async with db.session() as s:
                dag = _dag(agent)
                s.add(dag)
                await s.flush()
                s.add(DAGNode(dag_id=dag.id, name="b", node_type="bogus"))
                await s.commit()
    finally:
        async with db.session() as s:
            await s.execute(delete(ExecutionDAG).where(ExecutionDAG.agent_id == agent))
            await s.commit()


@pytest.mark.postgres_only
async def test_the_migration_is_idempotent(db):
    async with db.engine.begin() as conn:
        for _ in range(2):
            for statement in _split_sql_statements(_sql()):
                await conn.execute(text(statement))
