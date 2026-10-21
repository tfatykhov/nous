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

### Lesson gate (F012.1, all three pathways)

Each input item is shown to the LLM on one line with an ID: the decision or episode UUID with its recorded outcome, or `r-<hash>` for a monitor error→recovery pair (a hash of the tool, the error and the recovery tools, so the same observation has the same ID in every session). The LLM answers either `{"verdict": "NO_ENTRY", "reason": ...}` or a procedure with:

- `cue`: when the lesson applies (stored as the first entry of `goals`);
- `lesson`: the change in behaviour (stored in `implementation_notes` as `lesson: …`);
- `evidence`: a list of `{"id", "outcome"}` citing input IDs (stored as `evidence: <id> (<recorded outcome>) — <observed outcome>` lines).

A procedure is stored only if the cue and lesson are non-empty and at least one evidence item cites an ID from the same prompt (exactly, or a unique 8+ character prefix of a UUID). Citations of other IDs are dropped. A decision or episode cluster with two or more distinct IDs needs evidence citing two of them (lessons of one episode share its ID, and two spellings of one ID count once): a lesson only one item shows is a one-off. A monitor entry needs one. `NO_ENTRY` stores nothing and is preferred over an invented lesson.

The gate checks that the evidence is a real input item, not that the recorded outcome is true. Decision outcomes come from reviews, and conversation episodes are closed as `success`.

`run_sleep_learning()` returns `no_entry`, `gate_rejected`, `confounded` (weak review), `covered` (see below) and `llm_failed` (no answer, an answer that isn't a JSON object, or a weak-review revision with fields of the wrong type; a failed covered check counts too, and the procedure is then stored as new) for each sleep. The `sleep_completed` event and the generalize log line carry them, the event as one `procedure_gate` object. They are kept out of the consolidation audit's attempted-mutation total. If `procedures_created` stays at 0 while `gate_rejected` or `llm_failed` is high, the gate or the model's answers are blocking learning; while `covered` is high, the lessons are already stored.

## Lifecycle

- **Reinforcement:** Procedures in context get `activate()` + `record_outcome()` in post_turn based on turn success/failure
- **Weak review:** During sleep, procedures with effectiveness < 0.30 or inactive 30+ days get LLM review → keep/revise/retire/confounded. `confounded` (the effectiveness can't be attributed to the procedure) keeps it as is. `revise` retires the procedure and stores the revision; a malformed revision leaves the procedure unchanged.
- **Already covered:** Before storing a gated procedure (any pathway), probe the 3 closest active how-to procedures by cosine, on the text a stored row embeds (all body fields), so the cosines compare with stored rows. A closest one above 0.85 covers it without an LLM call. Neighbours from 0.5 are shown to the LLM (`[ID] name: description | cue | lesson`), which answers `{"verdict": "COVERED", "id": ...}` or `{"verdict": "NEW"}`; NEW, an ID it wasn't shown or no usable answer stores the new procedure. A covered lesson is counted as `covered` and stores nothing new. If the covering procedure is auto-learned (an `auto:` tag), its new evidence lines are appended to it (each input ID once, at most 10 evidence lines per procedure; nothing else changes, not even the embedding). A skill or hand-written procedure is left as it is.

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
