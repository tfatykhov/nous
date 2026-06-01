"""Live Nous instance for the continual-learning eval (Phase-1-eval track).

agent=nous-continual-eval, port=8079. Same prod flags as the faculty instance (incl.
the merged opus-4.8 tool-loop fix via the working tree).

  uv run python scripts/diag/faculty/run_continual_instance.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))


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


loaded = _load(REPO / ".env.prod-snapshot")
os.environ.update({
    "DB_HOST": "127.0.0.1", "DB_PORT": "5433", "DB_NAME": "nous_eval_live",
    "DB_USER": "nous", "DB_PASSWORD": "nous_eval",
    "NOUS_AGENT_ID": "nous-continual-eval",
    "NOUS_HOST": "127.0.0.1", "NOUS_PORT": "8079",
    "NOUS_TELEGRAM_BOT_TOKEN": "",
    "NOUS_HEARTBEAT_ENABLED": "false", "NOUS_SCHEDULE_ENABLED": "false",
    "NOUS_MCP_ENABLED": "false", "NOUS_ACTIONABILITY_BACKFILL_ON_STARTUP": "false",
    "NOUS_GRAPH_NEIGHBOR_SEED_SCORE_ENABLED": "true",
    "NOUS_HEART_GRAPH_ALL_TYPES_ENABLED": "true",
    "NOUS_GRAPH_ADJACENCY_BOOST_ENABLED": "true",
})
print(f"[run_continual_instance] loaded {loaded} prod flags; agent=nous-continual-eval "
      f"port=8079 model={os.environ.get('NOUS_MODEL')}", flush=True)

from nous.main import main  # noqa: E402

main()
