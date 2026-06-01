"""Live Nous instance bound to the faculty-eval corpus (Phase 0 agentic lens).

Same as scripts/diag/run_eval_instance.py but agent=nous-faculty-eval, port=8078, so
the agentic recall_deep loop runs against the faculty corpus without disturbing the
nous-eval-live instance on 8077. Association consumers ON (best shot, matching the
bare lens). Heartbeat/scheduler/telegram off.

  uv run python scripts/diag/faculty/run_faculty_instance.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

SNAPSHOT = REPO / ".env.prod-snapshot"


def _load(path: Path) -> int:
    n = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ[k] = v
        n += 1
    return n


loaded = _load(SNAPSHOT)
os.environ.update({
    "DB_HOST": "127.0.0.1", "DB_PORT": "5433", "DB_NAME": "nous_eval_live",
    "DB_USER": "nous", "DB_PASSWORD": "nous_eval",
    "NOUS_AGENT_ID": "nous-faculty-eval",
    "NOUS_HOST": "127.0.0.1", "NOUS_PORT": "8078",
    "NOUS_TELEGRAM_BOT_TOKEN": "",
    "NOUS_HEARTBEAT_ENABLED": "false", "NOUS_SCHEDULE_ENABLED": "false",
    "NOUS_MCP_ENABLED": "false", "NOUS_ACTIONABILITY_BACKFILL_ON_STARTUP": "false",
    # Association consumers ON — same best-shot config as the bare lens.
    "NOUS_GRAPH_NEIGHBOR_SEED_SCORE_ENABLED": "true",
    "NOUS_HEART_GRAPH_ALL_TYPES_ENABLED": "true",
    "NOUS_GRAPH_ADJACENCY_BOOST_ENABLED": "true",
    # SDK backend = prod default. The runner.py:1595/1205 fix (dispatch tool_use even on
    # stop_reason='end_turn') is what makes opus-4.8 tool calls execute here.
    "NOUS_API_BACKEND": "sdk",
})
print(f"[run_faculty_instance] loaded {loaded} prod flags; agent=nous-faculty-eval "
      f"port=8078 model={os.environ.get('NOUS_MODEL')}", flush=True)

from nous.main import main  # noqa: E402

main()
