"""F105: with every F105 flag off, the spreading activation SQL is byte-identical.

``spreading_activation_search`` builds its recursive CTE from fragments, and
each F105 change adds one that exists only while its flag is on (Task 3 fan
normalisation and ``parent_out``; Tasks 4, 7 and 8 later). The fixture was
rendered from the code before any F105 change to the CTE. This one assertion
stands for every task's "SQL unchanged with the flag off" claim, so it must
never need regenerating while those flags stay off. Regenerate it only in a PR
that changes the default SQL on purpose, and say so in that PR. From the
worktree root (so ``nous`` is imported from the worktree):

    uv run python -c "import runpy, sys; sys.path.insert(0, 'tests'); \
        runpy.run_path('tests/test_spreading_sql_identity.py', run_name='__main__')"

``SNAPSHOT`` and ``render`` live in ``tests/spreading_v2_helpers.py``, shared by
every F105 test that checks the flags-off SQL.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from spreading_v2_helpers import SNAPSHOT, render

from nous.config import Settings


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
