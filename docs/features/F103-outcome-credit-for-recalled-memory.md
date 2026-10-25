# F103: Outcome Credit for Recalled Memory

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

That leaves about **129 usable graded decisions per 4 weeks** (about 32 a week), and about **90% of them
are `success`**. Two design constraints follow:

- **No learned model.** There are too few rows. Use counts with a prior.
- **Measure against the base rate, not the raw rate.** A memory with a 90% success rate is *average*,
  not good. With so few failures, the signal is mostly "this memory shows up in partials and failures
  more than chance".

## Design

### 1. Recall snapshot (capture, Phase 1)

New table `brain.decision_recall_context`:

- `decision_id` UUID: FK to `brain.decisions(id)`, ON DELETE CASCADE.
- `memory_type`: `fact` | `decision` | `episode` | `procedure`
- `memory_id` UUID
- `rank` SMALLINT: 0-based position within its type, in final context order (after boosts and the
  relevance filter, so it matches what the model actually saw).
- `strength` REAL: the F017 overlap strength (0, 0.2, 0.5 or 1.0) that post_turn already computes.
  NULL if usage tracking is off.
- `source`: `context` (Phase 1). `recall_deep` is added in Phase 3.
- `credited_reward` REAL NULL, `credited_weight` REAL NULL: what this row last added to the memory's
  stats. Used for idempotent re-grades.
- `created_at`
- PK `(decision_id, memory_type, memory_id)`, plus an index on `(memory_type, memory_id)`.

**When it's written:** in `CognitiveLayer.post_turn`, after step 4 (usage tracking), in the same session.
That's the one point where the final recalled IDs, their order and their overlap strengths are all
in hand. Two kinds of decision get a snapshot:

- **The turn's deliberation decision** (`turn_context.decision_id`). Only if it survives finalize:
  informational turns delete their deliberation, and the snapshot goes with it.
- **Decisions the `record_decision` tool wrote during this turn.** Rows with
  `session_id = <session>` and `created_at >= <turn start>`. We don't use a ContextVar here: F091
  documents that ContextVars don't work on the streaming path (`stream_chat` resumes in a fresh copied
  context). A post_turn query keyed on the session needs no threading at all.

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

**Update:** upsert into `heart.memory_outcome_stats`:

- `agent_id`, `memory_type`, `memory_id`
- `alpha` REAL, `beta` REAL: weighted success and failure mass
- `n_obs` INT
- `last_credited_at`
- PK `(agent_id, memory_type, memory_id)`

`alpha += w·r`, `beta += w·(1−r)`. Each snapshot row is updated in the same transaction:
`credited_reward = r`, `credited_weight = w`.

**Re-grades and relabels:** if a snapshot row already has `credited_*`, subtract that contribution
before applying the new one. A relabel to `noise` or `superseded`, or to an excluded reviewer, only
subtracts. The whole step runs under `SELECT ... FOR UPDATE` on the snapshot rows, so two reviews that
race can't double-credit.

No FK from `memory_outcome_stats` to the memory tables. It is polymorphic, like `graph_edges`.
Retired or superseded memories keep their row, and it costs nothing.

### 3. Ranking boost (Phase 2, flag off by default)

`ContextEngine._apply_outcome_boost(items, memory_type)` runs straight after `_apply_usage_boost` at
its fact and episode call sites, and is newly added at the decisions block (`build` step 5, which has
no usage boost today). It reuses the `_wrap_with_score` and sort-by-score pattern, so the gap-cut still sees the
relevance order (the 3a fix).

```
p_base  = agent's mean reward over eligible graded decisions in the last 90 days  (cached 1 h)
decay   = 0.5 ** (age_days(last_credited_at) / 60)          # applied to alpha and beta at read
p_mem   = (decay·alpha + m·p_base) / (decay·(alpha+beta) + m),   m = 4
n_eff   = decay·(alpha + beta)
boost   = 1.0                                    if n_eff < 2
        = clip(1 + k·(p_mem − p_base), 0.80, 1.20), k = 2.0
```

- **Shrinks toward the base rate.** With only a few observations, `p_mem ≈ p_base` and boost ≈ 1, so a
  memory isn't punished for a single bad day.
- **Narrower band than the usage boost** (±20% against 0.3x to 2.0x). Outcome evidence is sparse and
  confounded, so it should nudge rankings, not override them.
- **Stays relevant over time** through the 60-day half-life. Memories that were right in May and
  stale by October lose their credit.
- Stats are loaded in one batched query per type per turn: `WHERE memory_id = ANY(:ids)`.

**Shadow mode (flag off):** the boost is computed and written to the recall trace (`trace` stage
`outcome_boost`, with old rank, new rank and boost), but rankings are not changed. Phase 2 starts by
running shadow mode for at least 4 weeks.

### Procedures

Phase 1 still writes procedure snapshot rows and stats. That's cheap, and it gives a graded signal
we can compare with F037's tool-error proxy. Phase 2 does **not** boost procedures: F037 affinity
already ranks them, and two outcome signals on one item would double-count. Phase 3 evaluates
replacing F037's proxy with graded credit.

## Phases

**Phase 1: capture (flag on, no ranking change)**

- Migration 093, `post_turn` snapshot, `OutcomeCreditHandler`, re-grade reversal.
- `scripts/diag/outcome_credit_report.py`: top and bottom memories by `p_mem − p_base` with
  `n_eff ≥ 2`, the snapshot row count, and eligible reviews credited per week.
- Tests:
  - snapshot written for the deliberation decision and for tool-recorded decisions in the turn
  - no snapshot for a deleted deliberation or a continuation turn
  - auto and f099 reviews credit nothing
  - success → partial re-grade nets correctly
  - relabel to noise only reverses
  - concurrent reviews don't double-credit
  - an empty recall set writes nothing

**Phase 2 gate: turn on the boost.** All three must hold:

1. **At least 300** credited (decision, memory) pairs and **at least 15** non-success eligible grades.
2. **Offline time-split check:** fit the stats on weeks 1–3 and score week 4's graded decisions by the
   mean `p_mem − p_base` of their snapshot. The partial and failure decisions must score lower than
   the successes (one-sided Mann–Whitney p < 0.1, or AUC ≥ 0.6). If they don't, the signal is noise at
   our volume, and F103 stops at Phase 1 as an analytics feature.
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
  cleanest. v1 weights all eligible grades the same. If the Phase 2 check fails, a cheap second try is
  to weight `a2ui` grades ×2 before giving up.
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
