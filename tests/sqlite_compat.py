"""SQLite compatibility layer for tests.

Provides:
1. Type compilation overrides (Vector -> Text, ARRAY -> JSON, JSONB -> JSON)
2. sqlite3 type adapters for list/dict serialization
3. TestDatabase class matching production Database interface
4. SQL rewriting hook to strip PG-specific syntax
5. Table creation with schema remapping
6. Pure-Python search helpers
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timezone
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


# ---------------------------------------------------------------------------
# 0. sqlite3 type adapters
# ---------------------------------------------------------------------------

def _adapt_list(val):
    return json.dumps(val)

def _adapt_dict(val):
    return json.dumps(val)

def _adapt_uuid(val):
    return str(val).replace("-", "") if val else None

sqlite3.register_adapter(list, _adapt_list)
sqlite3.register_adapter(dict, _adapt_dict)
sqlite3.register_adapter(uuid.UUID, _adapt_uuid)


# ---------------------------------------------------------------------------
# 1. Type compilation overrides
# ---------------------------------------------------------------------------

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from pgvector.sqlalchemy import Vector


@compiles(Vector, "sqlite")
def _compile_vector_sqlite(type_, compiler, **kw):
    return "TEXT"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(type_, compiler, **kw):
    return "JSON"


# ---------------------------------------------------------------------------
# 2. TestDatabase
# ---------------------------------------------------------------------------


class TestDatabase:
    """In-memory SQLite database matching the production Database interface."""

    def __init__(self, engine):
        self.engine = engine
        self.session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False,
        )

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.disconnect()


# ---------------------------------------------------------------------------
# 3. Engine and table creation
# ---------------------------------------------------------------------------

_SCHEMA_PREFIXES = re.compile(r'\b(heart|brain|nous_system)\.')


def _rewrite_sql_for_sqlite(sql_text: str) -> str:
    """Strip PG schema prefixes and rewrite PG functions for SQLite."""
    # Remove schema prefixes (heart.facts -> facts)
    sql_text = _SCHEMA_PREFIXES.sub('', sql_text)
    # ON CONFLICT DO NOTHING with constraint name -> simpler form
    sql_text = re.sub(
        r"ON CONFLICT\s*\([^)]+\)\s*DO\s+NOTHING",
        "ON CONFLICT DO NOTHING",
        sql_text,
        flags=re.IGNORECASE
    )
    # Replace NOW() with CURRENT_TIMESTAMP
    sql_text = sql_text.replace("NOW()", "CURRENT_TIMESTAMP")
    sql_text = sql_text.replace("now()", "CURRENT_TIMESTAMP")
    # Replace make_interval(hours => :hours) with datetime(:hours, '-' || :hours || ' hours')
    sql_text = re.sub(
        r"NOW\(\)\s*-\s*make_interval\(hours\s*=>\s*:hours\)",
        "datetime('now', '-' || :hours || ' hours')",
        sql_text,
        flags=re.IGNORECASE
    )
    return sql_text


async def create_test_engine():
    """Create async SQLite engine with schema remapping and SQL rewriting."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={
            "schema_translate_map": {
                "brain": None,
                "heart": None,
                "nous_system": None,
            }
        },
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # Register gen_random_uuid as SQLite function
        dbapi_connection.create_function(
            "gen_random_uuid", 0,
            lambda: str(uuid.uuid4()).replace("-", ""),
        )
        # Register stddev aggregate (SQLite doesn't have it natively)
        class StddevAggregate:
            def __init__(self):
                self.values = []
            def step(self, value):
                if value is not None:
                    self.values.append(float(value))
            def finalize(self):
                if len(self.values) < 2:
                    return None
                mean = sum(self.values) / len(self.values)
                variance = sum((x - mean) ** 2 for x in self.values) / (len(self.values) - 1)
                return math.sqrt(variance)

        # Register aggregates/functions via raw connection
        # Use check_same_thread=False workaround
        try:
            raw_conn = dbapi_connection._connection._conn
            raw_conn.create_aggregate("stddev", 1, StddevAggregate)
            raw_conn.create_function("power", 2, lambda base, exp: float(base) ** float(exp) if base is not None and exp is not None else None)
        except Exception:
            # Fallback: register via the adapter which proxies create_function
            dbapi_connection.create_function("power", 2, lambda base, exp: float(base) ** float(exp) if base is not None and exp is not None else None)
        cursor.close()

    @event.listens_for(engine.sync_engine, "before_cursor_execute", retval=True)
    def _rewrite_sql(conn, cursor, statement, parameters, context, executemany):
        """Rewrite PG-specific SQL for SQLite compatibility."""
        new_stmt = _rewrite_sql_for_sqlite(statement)
        return new_stmt, parameters

    return engine


async def create_tables(engine):
    """Create all ORM tables in SQLite."""
    from nous.storage.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ---------------------------------------------------------------------------
# 4. ORM event listeners for Python-side defaults
# ---------------------------------------------------------------------------


def install_sqlite_defaults():
    """Install listeners for PG function defaults (gen_random_uuid, now)."""
    from nous.storage.models import Base

    @event.listens_for(Base, "init", propagate=True)
    def _set_defaults(target, args, kwargs):
        mapper = inspect(type(target))
        for col in mapper.columns:
            attr_name = col.key
            if attr_name in kwargs and kwargs[attr_name] is not None:
                continue
            current = getattr(target, attr_name, None)
            if current is not None:
                continue

            sd = col.server_default
            if sd is None:
                continue

            sd_text = str(sd.arg) if hasattr(sd, "arg") else str(sd)

            if "gen_random_uuid" in sd_text:
                setattr(target, attr_name, uuid.uuid4())
            elif "now()" in sd_text.lower() or "current_timestamp" in sd_text.lower():
                setattr(target, attr_name, datetime.now(UTC))
            elif sd_text == "true":
                setattr(target, attr_name, True)
            elif sd_text == "false":
                setattr(target, attr_name, False)
            elif sd_text in ("'pending'", "'active'", "'raw'", "'manual'", "'warn'"):
                setattr(target, attr_name, sd_text.strip("'"))
            elif sd_text == "{}":
                setattr(target, attr_name, {})
            elif sd_text == "[]":
                setattr(target, attr_name, [])
            elif sd_text == "'1.0'":
                setattr(target, attr_name, 1.0)
            else:
                try:
                    setattr(target, attr_name, int(sd_text))
                except (ValueError, TypeError):
                    pass


# ---------------------------------------------------------------------------
# 5. Timezone helpers
# ---------------------------------------------------------------------------


def ensure_aware(dt):
    """Ensure a datetime is timezone-aware (UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# 6. Pure-Python search helpers
# ---------------------------------------------------------------------------


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def keyword_match_score(query: str, text: str) -> float:
    if not query or not text:
        return 0.0
    query_words = set(query.lower().split())
    text_words = set(text.lower().split())
    if not query_words:
        return 0.0
    overlap = len(query_words & text_words)
    return overlap / len(query_words)


def _parse_embedding(val: Any) -> list[float] | None:
    if val is None:
        return None
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# 7. ARRAY column deserialization for SQLite
# ---------------------------------------------------------------------------

def install_array_deserializer():
    """Register a load listener that deserializes JSON strings in ARRAY columns.

    SQLite stores Python lists as JSON strings (via sqlite3.register_adapter).
    When read back, SQLAlchemy returns raw strings instead of lists because
    ARRAY type has no result_processor for SQLite. This listener fixes that.
    """
    from nous.storage.models import Base
    from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY

    @event.listens_for(Base, "load", propagate=True)
    def _deserialize_arrays(target, context):
        mapper = inspect(type(target))
        for col in mapper.columns:
            if isinstance(col.type, PG_ARRAY):
                attr_name = col.key
                val = getattr(target, attr_name, None)
                if isinstance(val, str):
                    try:
                        parsed = json.loads(val)
                        if isinstance(parsed, list):
                            # Use object.__setattr__ to avoid SA dirty tracking
                            object.__setattr__(target, attr_name, parsed)
                    except (json.JSONDecodeError, ValueError):
                        pass

    # Also handle Vector (embedding) columns — stored as JSON string in SQLite
    @event.listens_for(Base, "load", propagate=True)
    def _deserialize_vectors(target, context):
        mapper = inspect(type(target))
        for col in mapper.columns:
            if isinstance(col.type, Vector):
                attr_name = col.key
                val = getattr(target, attr_name, None)
                if isinstance(val, str):
                    try:
                        parsed = json.loads(val)
                        if isinstance(parsed, list):
                            object.__setattr__(target, attr_name, parsed)
                    except (json.JSONDecodeError, ValueError):
                        pass



# ---------------------------------------------------------------------------
# 8. TypeDecorator wrappers for transparent ARRAY/Vector serialization
# ---------------------------------------------------------------------------

from sqlalchemy import TypeDecorator, Text as SAText_TD
from sqlalchemy.types import JSON as SA_JSON


class JSONEncodedList(TypeDecorator):
    """Stores Python lists as JSON strings in SQLite TEXT columns."""
    impl = SAText_TD
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            return json.dumps(value)
        return None

    def process_result_value(self, value, dialect):
        if value is not None:
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, ValueError):
                    return None
        return None


class JSONEncodedVector(TypeDecorator):
    """Stores embedding vectors as JSON strings in SQLite TEXT columns."""
    impl = SAText_TD
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if isinstance(value, list):
                return json.dumps(value)
            return str(value)
        return None

    def process_result_value(self, value, dialect):
        if value is not None:
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, ValueError):
                    return None
        return None


def patch_model_columns_for_sqlite():
    """Replace ARRAY and Vector column types with TypeDecorator versions.

    Must be called AFTER models are imported but BEFORE create_all.
    Modifies the Column.type in-place on the Table metadata objects.
    """
    from nous.storage.models import Base

    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, ARRAY):
                column.type = JSONEncodedList()
            elif isinstance(column.type, Vector):
                column.type = JSONEncodedVector()
