# F012 — K-Line Learning

**Status:** Shipped
**Spec:** `docs/plans/2026-03-09-f012-kline-learning-design.md`
**Implementation:** `docs/plans/2026-03-09-f012-kline-learning.md`

## Summary

Auto-creates procedures (K-lines with Minsky's level-bands) from repeated patterns in the agent's own experience. Three learning pathways detect patterns and crystallize them into reusable procedures.

## Pathways

### 1. Decision Clustering (Sleep)

Groups similar successful decisions by bridge-function embedding similarity. When 3+ reviewed decisions cluster above 0.85 cosine similarity with >70% success rate and at least one within 7 days, LLM extracts a procedure.

**Tag:** `auto:decision_cluster`

### 2. Episode Lesson Learning (Sleep)

Clusters `lessons_learned` from completed episodes by embedding similarity (>0.80). When 3+ similar lessons appear, LLM extracts a generalized procedure.

**Tag:** `auto:episode_lesson`

### 3. Monitor Recovery Learning (Real-time)

Tracks error→recovery pairs in `MonitorEngine.learn()`. When the same error pattern is recovered from 3+ times in a session, LLM extracts a recovery procedure.

**Tag:** `auto:monitor_recovery`

## Lifecycle

- **Reinforcement:** Procedures in context get `activate()` + `record_outcome()` in post_turn based on turn success/failure
- **Weak review:** During sleep, procedures with effectiveness < 0.30 or inactive 30+ days get LLM review → keep/revise/retire
- **Dedup:** Before creating any procedure, search existing at >0.85 similarity. Skip if match found.

## Caps

- Max 3 procedures per sleep cycle (pathways 1+2 combined)
- Max 1 procedure per session (pathway 3)

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `NOUS_PROCEDURE_LEARNING_ENABLED` | `true` | Master switch |
| `NOUS_PROCEDURE_CLUSTER_MIN_SIZE` | `3` | Min cluster size |
| `NOUS_PROCEDURE_SIMILARITY_THRESHOLD` | `0.85` | Decision clustering similarity |
| `NOUS_PROCEDURE_EPISODE_SIMILARITY` | `0.80` | Episode lesson similarity |
| `NOUS_PROCEDURE_SUCCESS_RATE_MIN` | `0.70` | Min success rate in cluster |
| `NOUS_PROCEDURE_MONITOR_TRIGGER_COUNT` | `3` | Error→recovery repeats for real-time learning |
| `NOUS_PROCEDURE_MAX_PER_SLEEP` | `3` | Max new procedures per sleep cycle |
| `NOUS_PROCEDURE_MAX_PER_SESSION` | `1` | Max new procedures from monitor |
| `NOUS_PROCEDURE_STALENESS_DAYS` | `30` | Days inactive before review |
| `NOUS_PROCEDURE_WEAKNESS_THRESHOLD` | `0.30` | Effectiveness below which triggers review |

## Files

| File | Role |
|------|------|
| `nous/handlers/procedure_learner.py` | ProcedureLearner (pathways 1+2, weak review, LLM calls) |
| `nous/cognitive/monitor.py` | Pathway 3 (error→recovery tracking) |
| `nous/handlers/sleep_handler.py` | Generalize phase delegation |
| `nous/cognitive/layer.py` | Procedure reinforcement in post_turn |
| `nous/main.py` | Wiring ProcedureLearner into sleep + monitor |
| `nous/config.py` | 10 configuration fields |

## Minsky Connection

Procedures are K-lines (Ch 8) with level-bands:
- **Upper fringe (goals):** When to use this procedure
- **Core (patterns/tools/concepts):** The transferable knowledge
- **Lower fringe (implementation notes):** Specific details, easily displaced

F012 implements automatic K-line creation — the agent notices what it keeps doing successfully and formalizes it. This is Papert's Principle (Ch 10) in action: growth through better administrative use of existing knowledge.
