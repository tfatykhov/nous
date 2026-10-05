"""F099 I2: with NOUS_INTENTIONS_ENABLED off, the four spawn tools' schemas are byte-identical.

Tool definitions sit at the front of the prompt-cache prefix, and F099's
``intent`` argument exists only while the flag is on. The fixture was rendered
from the code before any F099 change. Regenerate it only in a PR that changes
one of these schemas on purpose, and say so in that PR. From the worktree root
(so ``nous`` is imported from the worktree):

    UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen python -c \
        "import runpy; runpy.run_path('tests/test_f099_tool_schemas.py', run_name='__main__')"
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from nous.api.tools import ToolDispatcher, register_dag_tools, register_subtask_tools
from nous.config import Settings

SNAPSHOT = Path(__file__).parent / "fixtures" / "f099_spawn_tool_schemas.json"
SPAWN_TOOLS = ("spawn_task", "schedule_task", "spawn_sync", "dag_create")


def _settings(**over) -> Settings:
    """Hermetic: every setting the four schemas read is fixed here."""
    base = dict(
        _env_file=None,
        subtask_payload_schema_enabled=True,
        subtask_hardening_enabled=True,
        dag_approval_nodes_enabled=False,
        a2ui_enabled=False,
        compensation_enabled=False,
    )
    base.update(over)
    return Settings(**base)


def render(settings: Settings) -> str:
    """The four spawn tools' definitions, as the dispatcher serves them."""
    dispatcher = ToolDispatcher()
    register_subtask_tools(dispatcher, MagicMock(), settings, runner=object())
    orchestrator = SimpleNamespace(clock_wired=True, approvals_wired=False, _settings=settings, start_dag=AsyncMock())
    register_dag_tools(dispatcher, MagicMock(), orchestrator, settings=settings)
    defs = [d for d in dispatcher.tool_definitions() if d["name"] in SPAWN_TOOLS]
    assert [d["name"] for d in defs] == list(SPAWN_TOOLS), [d["name"] for d in defs]
    return json.dumps(defs, indent=2, ensure_ascii=False) + "\n"


def test_spawn_tool_schemas_are_byte_identical_with_the_flag_off():
    assert render(_settings()) == SNAPSHOT.read_text(encoding="utf-8")


if __name__ == "__main__":
    import nous

    repo = Path(__file__).resolve().parents[1]
    assert Path(nous.__file__).resolve().is_relative_to(repo), f"wrong nous imported: {nous.__file__}"
    SNAPSHOT.write_text(render(_settings()), encoding="utf-8", newline="\n")
    print(f"wrote {SNAPSHOT}")
