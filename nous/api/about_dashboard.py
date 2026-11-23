"""GET /dashboard/about: what this Nous is and where it runs, for the dashboard's About screen. Read-only.

The answer has a fixed set of keys, each built from a named value: the package version, the two optional build values a
deployment sets (``NOUS_BUILD_COMMIT``, ``NOUS_BUILD_TIME``, each shown only when it has the expected shape), when the
REST app was loaded at start, the Python version, the agent's id and name, the configured model names, and from the
database its version, the pgvector version and the highest numeric migration applied. When the database cannot be
read, its three values are null and the rest is answered as usual.
"""

from __future__ import annotations

import logging
import platform
import re
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from nous import __version__
from nous.config import Settings

logger = logging.getLogger(__name__)

# Taken when this module is first imported: rest.py imports it at the top, and main.py imports rest.py inside
# build_app, just before create_app.
_STARTED_AT = datetime.now(UTC)
_STARTED_MONO = time.monotonic()

_COMMIT = re.compile(r"[0-9a-fA-F]{7,40}")
COMMIT_CHARS = 12

# The {1,9} bound keeps the cast inside an int.
_DATABASE_SQL = text(
    "SELECT current_setting('server_version') AS server, "
    "(SELECT extversion FROM pg_extension WHERE extname = 'vector') AS pgvector, "
    "(SELECT max(version::int) FROM nous_system.schema_migrations WHERE version ~ '^[0-9]{1,9}$') AS latest"
)


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_commit(value: str) -> str | None:
    """The commit hash cut to 12 lowercase characters, or None for anything that is not 7 to 40 hex characters."""
    value = value.strip()
    return value.lower()[:COMMIT_CHARS] if _COMMIT.fullmatch(value) else None


def build_time(value: str) -> str | None:
    """The build time as UTC ``YYYY-MM-DDTHH:MM:SSZ``, or None for anything that is not a timestamp with a timezone."""
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(value)
        return _utc_text(moment) if moment.tzinfo is not None else None
    except (ValueError, OverflowError):  # not a timestamp, or one that falls outside the calendar in UTC
        return None


async def _database_part(database: Any) -> tuple[str | None, str | None, int | None]:
    """(server version, pgvector version, highest migration), or three None when the database cannot be read."""
    try:
        async with database.session() as session:
            row = (await session.execute(_DATABASE_SQL)).one()
    except Exception as e:
        logger.warning("About: the database part could not be read (%s)", type(e).__name__)
        return None, None, None
    return row.server.split(" ", 1)[0], row.pgvector, row.latest


async def build_about(settings: Settings, database: Any) -> dict[str, Any]:
    """The About screen's answer. Never raises: a database that cannot be read gives null database values."""
    server, pgvector, latest = await _database_part(database)
    return {
        "name": "Nous",
        "version": __version__,
        "build": {"commit": build_commit(settings.build_commit), "time": build_time(settings.build_time)},
        "started_at": _utc_text(_STARTED_AT),
        "uptime_seconds": max(0, int(time.monotonic() - _STARTED_MONO)),
        "python": platform.python_version(),
        "schema": {"latest": latest},
        "database": {"server": server, "pgvector": pgvector},
        "agent": {"id": settings.agent_id, "name": settings.agent_name},
        "models": {
            "main": settings.model,
            "background": settings.background_model,
            "critic": settings.critic_model,
            "heartbeat": settings.heartbeat_model or settings.background_model,
            "embeddings": settings.embedding_model,
        },
    }
