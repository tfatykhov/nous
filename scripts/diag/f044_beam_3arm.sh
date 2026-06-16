#!/bin/bash
# Hardened 3-arm F044 BEAM A/B. Isolation guarantees:
#  - reset self-verifies clean (consolidate.py exits 2 on residue) -> we abort.
#  - edge-count snapshot asserts `answer` never re-ingests/wipes the graph.
#  - per-arm cost-cap-hit guard asserts every arm answered all 5 convs.
#  - PERMUTED arm order (content, OFF, self) so a position effect (leakage)
#    is distinguishable from a real condition effect.
cd /e/Projects/nous
export DB_HOST=localhost DB_PORT=5433 DB_NAME=nous_eval_scratch DB_USER=nous DB_PASSWORD=nous_eval
export NOUS_MODEL=claude-sonnet-4-6
export NOUS_BEAM_BEAM_PYTHON='E:\Projects\nous\tools\beam\.venv\Scripts\python.exe'
export PYTHONPATH=E:/Projects/nous
SAVE=reports/beam/_f044ab
rm -rf $SAVE; mkdir -p $SAVE

snap () { # echoes "CONSOLIDATED TOTAL_EDGES" for beam agents
  uv run python -c "
import asyncio,asyncpg
async def m():
  c=await asyncpg.connect('postgresql://nous:nous_eval@localhost:5433/nous_eval_scratch')
  co=await c.fetchval(\"SELECT count(*) FROM brain.graph_edges WHERE agent_id LIKE 'beam-100K-%' AND consolidation_state='consolidated'\")
  tot=await c.fetchval(\"SELECT count(*) FROM brain.graph_edges WHERE agent_id LIKE 'beam-100K-%'\")
  print(co, tot)
  await c.close()
asyncio.run(m())"
}

set -- $(snap); EDGES0=$2
echo "baseline beam edges=$EDGES0"

arm () { # $1=name  $2=tinyhippo(true/false)  $3=warmup ("none"|"questions"|"content 40")
  local name=$1 flag=$2 mode="$3"
  echo "===== ARM $name (tinyhippo=$flag warmup='$mode') ====="
  NOUS_TINYHIPPO_LITE_ENABLED=true uv run python scripts/diag/f044_beam_consolidate.py reset || { echo "ABORT: reset isolation failed for $name"; exit 1; }
  if [ "$mode" != "none" ]; then
    NOUS_TINYHIPPO_LITE_ENABLED=true uv run python scripts/diag/f044_beam_consolidate.py $mode 2>&1 | grep -iE "warm-up:|FATAL"
  fi
  set -- $(snap); local cons=$1 edges=$2
  echo "  pre-answer: consolidated=$cons edges=$edges"
  [ "$edges" = "$EDGES0" ] || { echo "ABORT: edge count changed ($edges != $EDGES0) — re-ingest/wipe detected"; exit 1; }
  local out
  out=$(NOUS_TINYHIPPO_LITE_ENABLED=$flag NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=true uv run python -m nous_eval.beam --conv-limit 5 --cost-cap-usd 9 answer 2>&1)
  echo "$out" | grep -iE "answer: total cost|cost cap hit"
  echo "$out" | grep -qi "cost cap hit" && { echo "ABORT: $name answer hit cost cap (incomplete)"; exit 1; }
  set -- $(snap); echo "  post-answer: consolidated=$1 edges=$2"
  [ "$2" = "$EDGES0" ] || { echo "ABORT: edge count changed during answer ($2 != $EDGES0)"; exit 1; }
  uv run python -m nous_eval.beam --conv-limit 5 --cost-cap-usd 6 evaluate 2>&1 | grep -iE "evaluator OK|results under|error"
  mkdir -p $SAVE/$name
  for c in 1 2 3 4 5; do cp reports/beam/answers/100K/$c/evaluation-result.json $SAVE/$name/conv$c.json 2>/dev/null; done
}

# PERMUTED order: if results track POSITION (leakage) not CONDITION, OFF (run 2nd)
# would not be the lowest. Clean isolation => order is irrelevant.
arm arm3_oncontent true  "content 40"
arm arm1_off       false "none"
arm arm2_onself    true  "questions"

echo "===== 3-WAY COMPARE ====="
uv run python scripts/diag/f044_beam_3way.py
echo "===== DONE ====="
