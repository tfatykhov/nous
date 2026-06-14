#!/usr/bin/env bash
# QA flip-gate: matched BEAM-100K n=5 A/B for extraction_coverage_broadened.
# Both arms: same corpus, same session, Opus. OFF then ON. Reports official
# BEAM scores per arm to reports/beam/flipgate_{off,on}.json.
#
# Fail-closed: any failure (scratch clear, BEAM run/cost-cap, report) ABORTS the
# gate rather than continuing — a half-cleared DB or a stale summary would
# silently invalidate the matched A/B (codex PR #525).
cd /e/Projects/nous
export NOUS_BEAM_BEAM_PYTHON='E:\Projects\nous\tools\beam\.venv\Scripts\python.exe'
export NOUS_TEMPORAL_EXTRACTION_ENABLED=true   # held constant (matches prod)
export PYTHONUTF8=1
export PYTHONPATH=.

# Revive the harness from bytecode (source is gitignored/lost; .pyc remain).
( cd nous_eval/beam && for f in __pycache__/*.cpython-314.pyc; do
    cp "$f" "$(basename "$f" .cpython-314.pyc).pyc"; done )

# Clears all beam-agent rows. Errors propagate (no per-table swallow) so a
# partial clear aborts the gate instead of contaminating the next arm.
clear_scratch() {
  uv run python - <<'PY'
import asyncio, asyncpg
async def m():
    c = await asyncpg.connect(host='localhost', port=5433, user='nous',
                              password='nous_eval', database='nous_eval_scratch')
    try:
        for t in ['brain.graph_edges','heart.episode_chunks','heart.facts',
                  'heart.episodes','heart.procedures','heart.working_memory','brain.decisions']:
            await c.execute(f"DELETE FROM {t} WHERE agent_id LIKE 'beam%'")
    finally:
        await c.close()
asyncio.run(m())
PY
}

run_arm() {
  local flag=$1 name=$2
  echo "================ ARM $name (extraction_coverage_broadened=$flag) ================"
  clear_scratch        || { echo "ABORT: scratch clear failed (arm $name)"; exit 1; }
  # Remove any prior summary so a failed report can't archive a stale one.
  rm -f reports/beam/100K_summary.json
  NOUS_EXTRACTION_COVERAGE_BROADENED=$flag \
    uv run python -m nous_eval.beam --conv-limit 5 --cost-cap-usd 35 run \
    || { echo "ABORT: BEAM run failed/cost-capped (arm $name)"; exit 1; }
  echo "---- report $name ----"
  uv run python -m nous_eval.beam --conv-limit 5 report \
    || { echo "ABORT: report failed (arm $name)"; exit 1; }
  [ -f reports/beam/100K_summary.json ] \
    || { echo "ABORT: report produced no summary (arm $name)"; exit 1; }
  cp reports/beam/100K_summary.json "reports/beam/flipgate_${name}.json"
  echo "================ ARM $name DONE ================"
}

run_arm false off
run_arm true on
echo "================ FLIP-GATE COMPLETE ================"
