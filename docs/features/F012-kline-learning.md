# F012 — K-Line Learning

**Status:** Shipped
**Spec:** `docs/plans/2026-03-09-f012-kline-learning-design.md`
**Implementation:** `docs/plans/2026-03-09-f012-kline-learning.md`

## Summary

Auto-creates procedures (K-lines with Minsky's level-bands) from repeated patterns in the agent's own experience. Four learning pathways detect patterns and crystallize them into reusable procedures.

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

### 4. Job lessons (Sleep)

Behind `NOUS_JOB_LESSONS_ENABLED` (off by default). For a recurring background job (a `heart.schedules` row), a bad run is reviewed in a fresh context: one stateless background LLM call with a pinned prompt that sees that run's own record only (its `heart.subtasks` row and its execution ledger rows) next to at least two comparison runs of the same job. No identity, memory, procedures, tools or session. The answer goes through the lesson gate like the other pathways. A lesson that passes is stored as a procedure with the tag `auto:job_lesson` and kind `job_lesson`. The evidence label is the run's own `final_outcome` (the run's `status` if it has none), never the scorer's output: scores only choose which runs and jobs to review, and no score, outcome label or penalty reaches any text an agent can read. A job lesson shows only on the same job's scheduled runs and is hidden everywhere else. Lessons, baselines and retirements read only settled scores (final once a run is 14 days old), so a run is looked at between 14 and 30 days after it finished.

### Planned next: an independent validator

Not built yet. The next design starts from these constraints, so it does not re-decide them. A checker (possibly an LLM agent) verifies background runs so that more of them earn an outcome that is not the agent's own word. It runs as its own process under its own database role, reads its sources and writes only its own table, which the app role can only read. It uses a pinned model and prompt stored outside any path the agent can write, and the agent never sees its prompt, rubric or verdicts (a validator run by Nous would be Nous grading itself). It earns credit the way the scorer's checks do: its verdicts are sampled against owner audits, trusted per kind after consecutive right verdicts, one wrong verdict revokes the trust, and until then its verdicts can only take credit away. It feeds the scorer through a new scorer version, never by rewriting old rows. Once it exists, job lessons compare against validated good runs, and the retire rule and check-and-fix judge on validated outcomes.

### Lesson gate (F012.1, all four pathways)

Each input item is shown to the LLM on one line with an ID: the decision or episode UUID with its recorded outcome, or `r-<hash>` for a monitor error→recovery pair (a hash of the tool, the error and the recovery tools, so the same observation has the same ID in every session). The LLM answers either `{"verdict": "NO_ENTRY", "reason": ...}` or a procedure with:

- `cue`: when the lesson applies (stored as the first entry of `goals`);
- `lesson`: the change in behaviour (stored in `implementation_notes` as `lesson: …`);
- `evidence`: a list of `{"id", "outcome"}` citing input IDs (stored as `evidence: <id> (<recorded outcome>) — <observed outcome>` lines).

A procedure is stored only if the cue and lesson are non-empty and at least one evidence item cites an ID from the same prompt (exactly, or a unique 8+ character prefix of a UUID). Citations of other IDs are dropped. A decision or episode cluster with two or more distinct IDs needs evidence citing two of them (lessons of one episode share its ID, and two spellings of one ID count once): a lesson only one item shows is a one-off. A monitor entry needs one. `NO_ENTRY` stores nothing and is preferred over an invented lesson.

The gate checks that the evidence is a real input item, not that the recorded outcome is true. Decision outcomes come from reviews, and conversation episodes are closed as `success`.

`run_sleep_learning()` returns `no_entry`, `gate_rejected`, `confounded` (weak review), `covered` (see below) and `llm_failed` (no answer, an answer that isn't a JSON object, or a weak-review revision with fields of the wrong type; a failed covered check counts too, and the procedure is then stored as new) for each sleep. The `sleep_completed` event and the generalize log line carry them, the event as one `procedure_gate` object. They are kept out of the consolidation audit's attempted-mutation total. If `procedures_created` stays at 0 while `gate_rejected` or `llm_failed` is high, the gate or the model's answers are blocking learning; while `covered` is high, the lessons are already stored.

## Check-and-fix for a job that keeps failing

Behind `NOUS_JOB_FIXES_ENABLED` (off by default; needs intentions, continuation, an owner chat and an LLM client, otherwise the sleep phase logs once that it is skipped). A recurring job is a candidate when three of its latest five scored runs are bad, or when one of its job lessons was retired as not helping and a bad run is among its latest five. The scorer only chooses the job and the runs. The check reads fresh (provisional) scores on purpose: waiting until a score settles would leave a failing job running for two weeks.

- **One attempt, one run.** At most one fix run starts per sleep cycle and a job gets one attempt in its lifetime (its record is kept on the schedule row). The fix run is a fire under the job's own container, with that container's authority and never more. Its brief is built from the job's own records (the runs' `heart.subtasks` rows and ledger rows and the job's text), never from a score, outcome label or evidence.
- **Verdict.** The fix run ends its report with a fenced JSON block holding one of four verdicts: `changed_job` (new instructions and why), `fixed_elsewhere` (what was changed outside the job's text), `needs_owner` (a diagnosis and what the owner must do), `no_fault_found` (what was checked). Parsing fails closed: a missing, malformed, unknown or incomplete verdict counts as an unreadable report.
- **Review of a change.** New instructions pass a fresh-context review first (one stateless call with a pinned prompt that sees the old and new text only). Only an explicit accept applies them; the old text is kept in the record.
- **Judging.** After five scored runs of the job since the change, the bad rate is compared with the baseline stored when the attempt started. Strictly lower keeps the change.
- **When it cannot fix.** The old text is restored, the owner gets one report in the chat the job came from (the default chat when it has none) and the job is stopped. Stopping is one-way: to restart the job, recreate it. The report is written before the job is deactivated because it hangs on the job's still-open container. A stuck fix run is cancelled after two days and counts as an unreadable report. A job stopped by the owner during an attempt has its fix run cancelled and gets no message.

## Lifecycle

- **Reinforcement:** Procedures in context get `activate()` + `record_outcome()` in post_turn based on turn success/failure
- **Weak review:** During sleep, procedures with effectiveness < 0.30 or inactive 30+ days get LLM review → keep/revise/retire/confounded. `confounded` (the effectiveness can't be attributed to the procedure) keeps it as is. `revise` retires the procedure and stores the revision; a malformed revision leaves the procedure unchanged.
- **Already covered:** Before storing a gated procedure (any pathway), probe the 3 closest active how-to procedures by cosine, on the text a stored row embeds (all body fields), so the cosines compare with stored rows. A closest one above 0.85 covers it without an LLM call. Neighbours from 0.5 are shown to the LLM (`[ID] name: description | cue | lesson`), which answers `{"verdict": "COVERED", "id": ...}` or `{"verdict": "NEW"}`; NEW, an ID it wasn't shown or no usable answer stores the new procedure. A covered lesson is counted as `covered` and stores nothing new. If the covering procedure is auto-learned (an `auto:` tag), its new evidence lines are appended to it (each input ID once, at most 10 evidence lines per procedure, both decided on the notes read under a row lock, so sleep and monitor learning can add to one procedure at once; nothing else changes, not even the embedding). A skill or hand-written procedure is left as it is.

- **Job lesson retire rule:** A job lesson is retired when at least 5 runs of its job have been scored since the lesson was stored and the bad rate is not lower than the baseline stored with the lesson (the lesson did not help). Fewer scored runs keeps it.

## Caps

- Max 3 procedures per sleep cycle (pathways 1+2 combined)
- Max 1 procedure per session (pathway 3)
- Max `NOUS_JOB_LESSONS_PER_SLEEP` (2) job cases sent to the review per sleep cycle (a failed call counts) (pathway 4), outside the 3 above. `0` stops learning but not the retire rule

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
| `NOUS_JOB_LESSONS_ENABLED` | `false` | Job lessons pathway (needs `NOUS_PROCEDURE_LEARNING_ENABLED`) |
| `NOUS_JOB_LESSONS_PER_SLEEP` | `2` | Job cases sent to the review per sleep (a failed call counts), outside `NOUS_PROCEDURE_MAX_PER_SLEEP` |
| `NOUS_JOB_FIXES_ENABLED` | `false` | Check-and-fix for a job that keeps failing (needs intentions, continuation, an owner chat) |

## Files

| File | Role |
|------|------|
| `nous/handlers/procedure_learner.py` | ProcedureLearner (pathways 1+2, weak review, LLM calls) |
| `nous/handlers/job_lessons.py` | JobLessonLearner (pathway 4: review, gate, store, retire rule) |
| `nous/handlers/job_fixes.py` | JobFixer (check-and-fix: trigger, verdict parsing, change review, judging, stop and ask) |
| `nous/heart/job_runs.py` | A job's runs from `heart.subtasks` and the execution ledger |
| `nous/cognitive/monitor.py` | Pathway 3 (error→recovery tracking) |
| `nous/handlers/sleep_handler.py` | Generalize phase delegation, and the job lessons and job fixes phases |
| `nous/cognitive/layer.py` | Procedure reinforcement in post_turn |
| `nous/main.py` | Wiring ProcedureLearner, JobLessonLearner and JobFixer into sleep + monitor |
| `nous/config.py` | 13 configuration fields |

## Minsky Connection

Procedures are K-lines (Ch 8) with level-bands:
- **Upper fringe (goals):** When to use this procedure
- **Core (patterns/tools/concepts):** The transferable knowledge
- **Lower fringe (implementation notes):** Specific details, easily displaced

F012 implements automatic K-line creation — the agent notices what it keeps doing successfully and formalizes it. This is Papert's Principle (Ch 10) in action: growth through better administrative use of existing knowledge.
