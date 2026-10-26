"""F105: with every F105 flag off, the spreading activation SQL is byte-identical.

``spreading_activation_search`` builds its recursive CTE from fragments, and
each F105 change adds one that exists only while its flag is on (Task 3 fan
normalisation and ``parent_out``; Tasks 4, 7 and 8 later). The fixture was
rendered from the code before any F105 change to the CTE. This one assertion
stands for every task's "SQL unchanged with the flag off" claim, so it must
never need regenerating while those flags stay off. Regenerate it only in a PR
that changes the default SQL on purpose, and say so in that PR. From the
worktree root (so ``nous`` is imported from the worktree):

    uv run python -c \
        "import runpy; runpy.run_path('tests/test_spreading_sql_identity.py', run_name='__main__')"
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

from nous.brain.spreading_activation import spreading_activation_search
from nous.config import Settings

SNAPSHOT = Path(__file__).parent / "fixtures" / "spreading_sql_default.sql"
# Two seeds of two types and a non-empty exclusion, so every default fragment
# (the VALUES list and the excluded_ids condition) is in the rendered text.
SEEDS: list[tuple[UUID, str, float]] = [
    (UUID("00000000-0000-0000-0000-0000000000a1"), "fact", 0.9),
    (UUID("00000000-0000-0000-0000-0000000000a2"), "decision", 0.6),
]
EXCLUDE: set[UUID] = {SEEDS[0][0]}


class _EmptyResult:
    def all(self) -> list[Any]:
        return []


class RecordingSession:
    """Fake session: ``execute`` stores the rendered SQL and returns no rows."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, sql: Any, params: dict[str, Any] | None = None) -> _EmptyResult:
        self.statements.append(str(sql))
        return _EmptyResult()


async def render(settings: Any, **kwargs: Any) -> str:
    """The SQL ``spreading_activation_search`` sends for the fixed call shape."""
    session = RecordingSession()
    await spreading_activation_search(
        session, "agent-x", SEEDS, settings, exclude_ids=EXCLUDE, **kwargs,  # type: ignore[arg-type]
    )
    assert len(session.statements) == 1, session.statements
    return session.statements[0]


async def test_default_sql_is_byte_identical_to_snapshot():
    snapshot = SNAPSHOT.read_text(encoding="utf-8")
    # A snapshot regenerated with a flag on would pass silently without these.
    assert "LATERAL" not in snapshot
    assert "parent_id" not in snapshot
    assert await render(Settings(_env_file=None)) == snapshot


if __name__ == "__main__":
    import nous

    repo = Path(__file__).resolve().parents[1]
    assert Path(nous.__file__).resolve().is_relative_to(repo), f"wrong nous imported: {nous.__file__}"
    SNAPSHOT.write_text(asyncio.run(render(Settings(_env_file=None))), encoding="utf-8", newline="\n")
    print(f"wrote {SNAPSHOT}")
