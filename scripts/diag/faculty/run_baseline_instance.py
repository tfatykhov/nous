"""Live instance for the capability-baseline agentic lens (docs/research/018).

agent=nous-baseline-eval (facts already direct-inserted by baseline.py), port 8079.
Includes the rest.py debug change (recalled_*_ids) for mechanism attribution.

Env overrides let the flag-arm rerun flip recency/temporal flags:
  NOUS_RECENCY_RESOLVER_ENABLED / NOUS_TEMPORAL_EXTRACTION_ENABLED (set before launch).

  uv run python scripts/diag/faculty/run_baseline_instance.py
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
        os.environ.setdefault(k, v)  # setdefault: don't clobber flag-arm overrides
        n += 1
    return n


# flag-arm overrides (if the caller set them) win over the snapshot via setdefault
loaded = _load(REPO / ".env.prod-snapshot")
os.environ.update({
    "DB_HOST": "127.0.0.1", "DB_PORT": "5433", "DB_NAME": "nous_eval_live",
    "DB_USER": "nous", "DB_PASSWORD": "nous_eval",
    "NOUS_AGENT_ID": "nous-baseline-eval",
    "NOUS_HOST": "127.0.0.1", "NOUS_PORT": "8079",
    "NOUS_TELEGRAM_BOT_TOKEN": "",
    "NOUS_HEARTBEAT_ENABLED": "false", "NOUS_SCHEDULE_ENABLED": "false",
    "NOUS_MCP_ENABLED": "false", "NOUS_ACTIONABILITY_BACKFILL_ON_STARTUP": "false",
    "NOUS_GRAPH_NEIGHBOR_SEED_SCORE_ENABLED": "true",
    "NOUS_HEART_GRAPH_ALL_TYPES_ENABLED": "true",
    "NOUS_GRAPH_ADJACENCY_BOOST_ENABLED": "true",
})
print(f"[run_baseline_instance] agent=nous-baseline-eval port=8079 "
      f"recency={os.environ.get('NOUS_RECENCY_RESOLVER_ENABLED')} "
      f"temporal={os.environ.get('NOUS_TEMPORAL_EXTRACTION_ENABLED')} "
      f"model={os.environ.get('NOUS_MODEL')}", flush=True)

from nous.main import main  # noqa: E402

main()
