# F104: Outcome Credit for Recalled Memory

*Proposed as F103 in PR #806; renumbered because #805 (the injection screen) keeps F103.*

**Status:** spec, not started. Two flags: `NOUS_OUTCOME_CREDIT_CAPTURE_ENABLED` (default `true`, writes
only) and `NOUS_OUTCOME_CREDIT_BOOST_ENABLED` (default `false` until the Phase 2 gate passes).
**Planned code:** `nous/cognitive/layer.py` (post_turn snapshot), `nous/handlers/outcome_credit.py`
(new `decision_reviewed` subscriber), `nous/cognitive/context.py` (`_apply_outcome_boost`),
`sql/migrations/093_outcome_credit.sql`, `scripts/diag/outcome_credit_report.py`.

**Credit:** the idea comes from Memento (Memento-Teams/Memento; Zhou et al., *Fine-tuning LLM Agents
without Fine-tuning LLMs*, arXiv:2508.16153). Memento saves each solved task to a case bank with a
binary reward and learns a retrieval policy Q(state, case) from those rewards. We take the **feedback
path**, not the model. Memento trains a BERT classifier on about 7k labelled rows. Nous grades about
30 decisions a week, so this design uses Bayesian counts, the same style as F037.

## Problem

Nous learns which memories to recall from two signals, and neither one is the outcome.

1. **Citation (005.1 / F017 Phase 6).** `UsageTracker` checks whether the response text overlaps a
   recalled memory. The ratio of references to retrievals sets a boost between 0.3x and 2.0x in
   `ContextEngine._apply_usage_boost`. The tracker keeps this **only in memory** (`_stats` /
   `_records`), so every restart wipes it. It also measures citation, not usefulness: a stale fact that
   gets paraphrased into a bad answer still scores as "used".
2. **Procedure reinforcement (F012 / F037).** Procedures in context get `success`/`failure` from
   "did any tool error this turn" (`record_procedure_outcome` → `procedure_task_affinity`). That is a
   per-turn proxy, and it covers procedures only.

Graded outcomes exist. `brain.decisions.outcome` is set by `Brain.review`, which emits
`decision_reviewed` after commit. But nothing records **which memories were in context** when the
decision was made. `TurnContext.recalled_{fact,decision,episode,procedure}_ids` are sent back in the
REST turn response (`api/rest.py`), passed to F071 dedup, and then dropped. So when a decision is
graded `failure`, the facts and past decisions that informed it get no blame. When it's graded
`success`, they get no credit.

### Data volume (live, read-only, 28 days to 2026-10-25)

Graded outcomes by reviewer:

- `agent`: 76 success, 14 partial, 2 failure
- `a2ui` (owner taps): 35 success, 1 partial, 1 failure
- `auto`: 129 success, 1 failure. This is a heuristic grade, excluded (see below).
- `f099`: 64 success, 15 partial, 14 failure. Lineage grades, excluded.

That leaves about **129 usable graded decisions per 4 weeks** (about 32 a week): 111 success, 15
partial, 3 failure. The **success share is 86%** (111/129), and the **mean reward is about 0.92**
(partials counted as 0.5, see Reward below). Only **18 are not success**, about 4.5 a week. Two design
constraints follow:

- **No learned model.** There are too few rows. Use counts with a prior.
- **Measure against the base rate, not the raw rate.** A memory with a mean reward of 0.92 is
  *average*, not good. With so few failures, the signal is mostly "this memory shows up in partials and failures
  more than chance".

## Design

### 1. Recall snapshot (capture, Phase 1)

New table `brain.decision_recall_context` (the only new table):

- `agent_id` VARCHAR(100) NOT NULL: the decision's agent. Every table is agent-scoped, and the read
  path filters on it.
- `decision_id` UUID: FK to `brain.decisions(id)`, ON DELETE CASCADE.
- `memory_type`: `fact` | `decision` | `episode` | `procedure`
- `memory_id` UUID
- `rank` SMALLINT: 0-based position within its type, in final context order (after boosts and the
  relevance filter, so it matches what the model actually saw). Rank restarts at 0 for each type, so
  the rank weight below is applied **per type**: the first fact and the first episode both get
  weight 1.0. The Phase 2 analysis compares types, so a per-type comparison must keep this in mind
  (a decision with recalls of four types carries up to four weight-1.0 rows).
- `strength` REAL: the F017 overlap strength (0, 0.2, 0.5 or 1.0) that post_turn already computes.
  NULL if usage tracking is off.
- `source`: `context` (Phase 1). `recall_deep` is added in Phase 3.
- `credited_reward` REAL NULL, `credited_weight` REAL NULL: the row's current credit. NULL means the
  row contributes nothing (not reviewed yet, or relabelled out).
- `credited_at` TIMESTAMPTZ NULL: when the row was **first** credited by an eligible review. Written
  once and never changed afterwards (see re-grades below). The decay is computed from it.
- `created_at`
- PK `(decision_id, memory_type, memory_id)`, plus an index on `(agent_id, memory_type, memory_id)`
  for the read path.

**Implementation notes:** migration 093 adds one table, so the expected set in
`tests/test_database.py::test_all_tables_exist` grows from 59 to 60 (brain 12 → 13), and the table
count in CLAUDE.md (Database) changes to match, both in the implementation PR. The round-1 review
counted 56 → 58; on current main the base is 59 (F102 added `heart.procedure_versions`), and this
design adds one table, not two, because `heart.memory_outcome_stats` is dropped (section 2).

**When it's written:** in `CognitiveLayer.post_turn`, after step 4 (usage tracking), in the same session.
That's the one point where the final recalled IDs, their order and their overlap strengths are all
in hand. Two kinds of decision get a snapshot:

- **The turn's deliberation decision** (`turn_context.decision_id`). Only if it survives finalize:
  informational turns delete their deliberation, and the snapshot goes with it.
- **Decisions the `record_decision` tool wrote during this turn.** Rows with
  `agent_id = <agent>`, `session_id = <session>` and `<turn start> <= created_at <= <turn end>`, where
  turn end is the time post_turn starts. We don't use a ContextVar here: F091 documents that
  ContextVars don't work on the streaming path (`stream_chat` resumes in a fresh copied context). A
  post_turn query keyed on the session needs no threading at all.

  Why turns in one session don't overlap: the runner holds a per-session `asyncio.Lock` across the
  whole of `run_turn` and `stream_chat` (`_get_session_lock`, `nous/api/runner.py`), so two chat
  turns of one session run one after the other in a process. Subtasks run under `subtask-<id>`, or
  under a scheduled continuation's `schedule-…` session (`subtask_worker.py`, `task_scheduler.py`),
  and either way go through `run_turn`, so they take the same per-session lock. The upper bound is defence in depth for a writer
  outside that lock (a second process on the same database, which the execution ledger already
  assumes away): it keeps out decisions written after this turn ended. A writer that runs inside
  the window and outside the lock would still match; there is no such writer today.

Decisions **skipped**: continuation turns (F099 lineage decisions are graded by `f099`, which this
design excludes anyway), and turns with no recalled IDs.

Size: four types, top-k each, about 30 rows per decision at most. That's about 15k rows a month at
current volume, so no retention policy is needed in v1.

### 2. Credit on review (capture, Phase 1)

New handler `OutcomeCreditHandler` subscribes to `decision_reviewed`, using the same wiring pattern as
`StrategyCardDistiller`: fire-and-forget task, coalesce per decision in flight.

**Eligible reviews:** `outcome ∈ GRADED_OUTCOMES` and `reviewer ∉ NON_DISTILLING_REVIEWERS` (`auto`,
`f099`), reusing the existing constant. The auto-reviewer's grade comes from stated confidence or PR
state, and an f099 grade says how a chain ended. Neither is evidence about the memories in context.
Crediting from them would launder heuristics into ranking.

**Reward:** `success` = 1.0, `partial` = 0.5, `failure` = 0.0.

**Per-memory weight** (credit assignment, an improvement over Memento, which gives every retrieved case
the task label in full):

```
w = rank_w(rank) × use_w(strength)
rank_w(r)   = 1 / log2(r + 2)            # 1.0, 0.63, 0.5, 0.43, ...
use_w(s)    = 0.4 + 0.6 × s              # never 0: an unused memory still diluted the context
            = 0.7 when strength is NULL
```

**Update:** the handler writes only the decision's own snapshot rows:
`credited_reward = r`, `credited_weight = w`, `credited_at = COALESCE(credited_at, now())`. There is
**no stored per-memory total**. An earlier draft upserted running `alpha`/`beta` sums into a
`heart.memory_outcome_stats` table; that table is dropped. Per-memory stats are computed at read time
from the snapshot rows (section 3), each row decayed by its own `credited_at`.

**Re-grades and relabels:** a re-grade overwrites `credited_reward` and `credited_weight` with the new
values. Nothing is subtracted, because nothing was summed. `credited_at` keeps its first value, so a
correction to an old grade doesn't make old evidence count as fresh. A relabel to `noise` or
`superseded`, or a grade from an excluded reviewer, sets `credited_reward` and `credited_weight` to
NULL (the row drops out of the read) and leaves `credited_at` alone. A later eligible grade of the
same decision then still decays from the first credit.

**Locking:** the step runs in one transaction that locks the decision's snapshot rows with
`SELECT ... ORDER BY memory_type, memory_id FOR UPDATE`. Two reviews of the same decision serialize on
those rows, and the later one's values win. Two reviews of *different* decisions touch disjoint rows
(the PK starts with `decision_id`), so sharing memories can't make them wait on each other. The
deadlock the round-1 review found came from the cross-decision stats upsert; with that upsert gone it
can't happen. The fixed-order rule stays for anything that writes per-memory rows: if a cache of the
read-time stats is ever added (see section 3), every refresh writes its rows in
`(agent_id, memory_type, memory_id)` order, or takes their locks first in that order.

No FK from the snapshot's `memory_id` to the memory tables. It is polymorphic, like `graph_edges`.
Retired or superseded memories keep their rows, and they cost nothing.

### 3. Ranking boost (Phase 2, flag off by default)

`ContextEngine._apply_outcome_boost(items, memory_type)` runs straight after `_apply_usage_boost` at
its fact and episode call sites, and is newly added at the decisions block (`build` step 5, which has
no usage boost today). It reuses the `_wrap_with_score` and sort-by-score pattern, so the gap-cut still sees the
relevance order (the 3a fix).

```
p_base  = agent's mean reward over eligible graded decisions in the last 90 days  (cached 1 h)
          # mean of r per decision, partial = 0.5: about 0.92 today, not the 86% success share
for each credited snapshot row i of the memory (credited_weight IS NOT NULL):
  d_i   = 0.5 ** (age_days(credited_at_i) / 60)             # each row decays by its own credit time
alpha   = Σ d_i · w_i · r_i
beta    = Σ d_i · w_i · (1 − r_i)
n_eff   = alpha + beta
p_mem   = (alpha + m·p_base) / (n_eff + m),   m = 4
boost   = 1.0                                    if n_eff < 2
        = clip(1 + k·(p_mem − p_base), 0.80, 1.20), k = 2.0
```

The sums are one batched query per type per turn over the read index:

```sql
SELECT memory_id,
       SUM(d * credited_weight * credited_reward)       AS alpha,
       SUM(d * credited_weight * (1 - credited_reward)) AS beta
FROM (SELECT memory_id, credited_weight, credited_reward,
             power(0.5, extract(epoch FROM now() - credited_at) / 86400 / 60) AS d
      FROM brain.decision_recall_context
      WHERE agent_id = :agent_id AND memory_type = :type AND memory_id = ANY(:ids)
        AND credited_weight IS NOT NULL) s
GROUP BY memory_id
```

Per-row decay is why the stored totals are gone. Decaying a stored total by its latest credit time
(the earlier draft) brings old mass back to full weight: a memory credited once a year ago and once
today would count last year's credit in full. Here each contribution ages on its own.

Read cost: one row per (decision, memory) pair, about 15k a month in total. The memory most often
recalled (an identity fact in nearly every decision) gathers about 130 rows a month, so a top-k batch
reads a few thousand index rows at most. If that ever shows in turn latency, a cache table keyed by
`(agent_id, memory_type, memory_id)` can be rebuilt from the snapshot rows (refreshed in that key
order, per section 2). v1 has no cache.

- **Shrinks toward the base rate.** With only a few observations, `p_mem ≈ p_base` and boost ≈ 1, so a
  memory isn't punished for a single bad day.
- **Narrower band than the usage boost** (±20% against 0.3x to 2.0x). Outcome evidence is sparse and
  confounded, so it should nudge rankings, not override them.
- **Stays relevant over time** through the 60-day half-life, applied per credit. Memories that were
  right in May and stale by October lose their May credit, even if they were also credited last week.

**Shadow mode (flag off):** the boost is computed and written to the recall trace (`trace` stage
`outcome_boost`, with old rank, new rank and boost), but rankings are not changed. Phase 2 starts by
running shadow mode for at least 4 weeks.

### Procedures

Phase 1 still writes and credits procedure snapshot rows. That's cheap, and it gives a graded signal
we can compare with F037's tool-error proxy. Phase 2 does **not** boost procedures: F037 affinity
already ranks them, and two outcome signals on one item would double-count. Phase 3 evaluates
replacing F037's proxy with graded credit.

## Phases

**Phase 1: capture (flag on, no ranking change)**

- Migration 093, `post_turn` snapshot, `OutcomeCreditHandler`, re-grade overwrite.
- `scripts/diag/outcome_credit_report.py`: top and bottom memories by `p_mem − p_base` with
  `n_eff ≥ 2`, the snapshot row count, and eligible reviews credited per week.
- Tests:
  - snapshot written for the deliberation decision and for tool-recorded decisions in the turn
  - a decision in the same session created after the turn's end is not in its snapshot
  - no snapshot for a deleted deliberation or a continuation turn
  - auto and f099 reviews credit nothing
  - success → partial re-grade leaves the row at the partial values, with `credited_at` unchanged
  - relabel to noise clears reward and weight and keeps `credited_at`
  - per-row decay: a memory credited 365 days ago and today has the old row at weight ≈ 0.015, not 1
  - two concurrent reviews of one decision leave its rows at one review's values, not a mix
  - lock order: two decisions sharing memories A, B, C, recalled in reverse rank order (A, B, C and
    C, B, A), reviewed concurrently from two sessions, both commit with no deadlock or lock timeout,
    and the read-time stats for A, B and C equal the sum of both decisions' credit
  - rows of another `agent_id` are not read
  - an empty recall set writes nothing

**Phase 2 gate: turn on the boost.** All three must hold:

1. **At least 300** credited (decision, memory) pairs.
2. **Offline rolling-origin check**, with the decision rule fixed here, before any data arrives:
   - **Score.** A decision's score is the `w`-weighted mean of `p_mem − p_base` over its snapshot
     rows. For the fold that scores week k+1, `p_mem` and `p_base` use only rows with `credited_at`
     before that week starts, decayed from the week's start (not from `now()`). Re-grades overwrite,
     so a fit row carries its current grade, not the grade it had at the fold's start. That is
     accepted: a re-grade corrects an earlier decision's label and carries nothing about the scored
     week's outcomes.
   - **Folds.** For every k across at least 8 weeks of capture: fit on weeks 1..k, score the eligible
     graded decisions of week k+1. A decision is scored once, in the fold for its week.
   - **Pool.** Pool the scored decisions over all folds. The pool must hold **at least 30
     non-success** (partial or failure) decisions.
   - **When it runs.** Exactly once, at the first week boundary where both hold: at least 8 weeks of
     capture, and at least 30 non-success decisions in the pool. Before that the gate is shut, not
     failed, and nobody computes the AUC early.
   - **Test.** Pooled AUC of score for success against non-success, with the null distribution from
     10,000 label permutations within each scored week. Pass if the one-sided permutation
     **p < 0.05**.
   - **Stop rule.** If 8 weeks of capture don't reach 30 non-success decisions, the gate stays shut;
     the folds keep extending week by week, with the same rule, until the condition above holds. The
     single run is final: a failed check ends Phase 2, and F104 stays at Phase 1 as an analytics
     feature.

   **Why not "AUC ≥ 0.6 or Mann–Whitney p < 0.1" on one week** (the earlier draft): a week holds about
   32 eligible decisions, about 4 non-success against 28 success. The null AUC then has standard
   deviation √((n₁+n₂+1) / (12·n₁·n₂)) = √(33/1344) ≈ 0.16, so AUC ≥ 0.6 happens by chance about 26%
   of the time, and the "or" makes the gate as weak as its weaker half. At about 4.5 non-success a
   week, 30 needs roughly 7 scored weeks, which is why the check spans at least 8 weeks. At 30
   negatives the null standard deviation drops to about 0.06.
3. **No regression** on the LoCoMo and LongMemEval adapters (`/tmp/nous-workspace/benchmarks/`) with
   the boost on. Those benchmarks have no graded decisions, so this checks only that a boost which
   should be neutral is neutral.

**Phase 3: optional**

- Add in-turn `recall_deep` results as `source=recall_deep` rows. This needs the IDs the tool returned
  in the turn, threaded explicitly in the F091 style (the tool already reads the turn's recalled IDs for
  F071 dedup).
- Replace the F037 tool-error proxy with graded credit for procedures, or blend the two.
- Persist `UsageTracker` (separate small fix; worth doing anyway, since citation stats reset on every
  deploy).
- An "avoid" cue: when a recalled decision has `outcome=failure` *and* its snapshot memories have low
  `p_mem`, add a one-line warning in context. This is Memento's negative-case prompting. Low priority:
  censors and outcome-tagged related decisions already cover most of it.

## Risks

- **Confounding.** Memories that are always recalled (identity facts, Tim's preferences) are present
  in every decision, so they track `p_base` and get boost ≈ 1. That is the correct answer: base-rate
  centring is what neutralises them.
- **Rich get richer.** A boosted memory gets recalled more, collects more credit and gets boosted
  more. Three things limit this: the ±20% cap, prior shrinkage, and decay. The diag report watches the
  rank-shift distribution in shadow mode.
- **Grade quality.** Agent self-grades are biased toward success. `a2ui` (owner) grades are the
  cleanest. v1 weights all eligible grades the same. Weighting `a2ui` grades ×2 is a possible second
  design, but it would be a new analysis: it gets its own rule written down first and runs only on
  weeks the failed check didn't score, so trying it can't turn one failed test into two chances.
- **Wrong-reason credit.** A decision that succeeds *despite* a bad fact credits the fact. Rank and use
  weighting reduce this but can't remove it. Credit is a nudge, not a truth label. Fact correctness
  still goes through supersession and contradiction.

## Not adopted from Memento

- **The parametric Q-classifier** (BERT over (query, case) pairs). Our data volume is about 1% of
  what it needs.
- **The case bank as a separate store.** Nous's decisions plus episodes already are the case bank.
- **Ground-truth judging.** Memento grades against benchmark answers. We grade with `Brain.review`.
- **Full-bank scoring at query time.** Memento runs the classifier over every case on every query.
  The boost here is O(k) over already-retrieved items.
