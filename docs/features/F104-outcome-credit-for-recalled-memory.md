# F104: Outcome Credit for Recalled Memory

*Proposed as F103 in PR #806; renumbered because #805 (the injection screen) keeps F103.*

**Status:** spec, not started. Two flags: `NOUS_OUTCOME_CREDIT_CAPTURE_ENABLED` (default `true`, writes
only) and `NOUS_OUTCOME_CREDIT_BOOST_ENABLED` (default `false` until the Phase 2 gate passes).
**Planned code:** `nous/cognitive/layer.py` (post_turn snapshot), `nous/handlers/outcome_credit.py`
(new `decision_reviewed` subscriber), `nous/cognitive/context.py` (delivered ids in `BuildResult`,
`_apply_outcome_boost`), `nous/api/call_outcome.py` + `nous/api/tools.py` + `nous/api/runner.py` +
`nous/cognitive/schemas.py` (the turn's `record_decision` ids, threaded to post_turn),
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
REST turn response (`api/rest.py`), passed to F071 dedup, and then dropped (they also include items
the section budget cut, see section 1). So when a decision is
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
- `rank` SMALLINT: 0-based position among the memories **delivered** to the model with the same
  `memory_type` and `source` (after boosts, the relevance filter and the section budget cut; see
  "Delivered ids only" below). Ranks are counted after the cut, so they have no gaps. Rank restarts
  at 0 for each (type, source) pair, so the rank weight below is applied **per pair**: the first
  Relevant Facts fact, the first User Profile fact and the first episode all get weight 1.0. A
  per-type comparison must keep this in mind (a decision can carry one weight-1.0 row per pair).
- `strength` REAL: the F017 overlap strength (0, 0.2, 0.5 or 1.0) that post_turn already computes.
  NULL if usage tracking is off, and always NULL on `profile` and `session_profile` rows: post_turn
  computes strength only for the ids in `recalled_*_ids` (`nous/cognitive/layer.py:1302-1316`), and
  F104 doesn't widen that.
- `source`: which block delivered the memory. Phase 1 writes `context` (the ranked recall sections:
  Relevant Facts, decisions, episodes, procedures), `profile` (the User Profile block) and
  `session_profile` (the Session Profile intent leg). `recall_deep` is added in Phase 3.
- `credited_reward` REAL NULL, `credited_weight` REAL NULL: the row's current credit. NULL means the
  row contributes nothing (not reviewed yet, or relabelled out).
- `credited_at` TIMESTAMPTZ NULL: when the row was **first** credited from an eligible review, by the
  handler or at insert (section 2). Written once and never changed afterwards (see re-grades below).
  The decay is computed from it.
- `created_at`
- PK `(decision_id, memory_type, memory_id)`, plus an index on `(agent_id, memory_type, memory_id)`
  for the read path.

**Implementation notes:** migration 093 adds one table, so the expected set in
`tests/test_database.py::test_all_tables_exist` grows from 59 to 60 (brain 12 → 13), and the table
count in CLAUDE.md (Database) changes to match, both in the implementation PR. The round-1 review
counted 56 → 58; on current main the base is 59 (F102 added `heart.procedure_versions`), and this
design adds one table, not two, because `heart.memory_outcome_stats` is dropped (section 2).

**When it's written:** in `CognitiveLayer.post_turn`, after step 4 (usage tracking). That's the one
point where the delivered IDs, their order and their overlap strengths are all in hand. Each
decision's snapshot is written in **its own short transaction** (its own DB session), using the
insert protocol in section 2. Every runner path calls post_turn without a session
(`nous/api/runner.py:1508`, `:1731`, `:2190`, `:2935`), and steps 1–4 commit their own work, so the
deliberation decision's finalize or delete (step 3, `nous/cognitive/layer.py:1253`) is committed
before the snapshot transaction locks the decision row. Two kinds of decision get a snapshot:

- **The turn's deliberation decision** (`turn_context.decision_id`). Only if it survives finalize:
  informational turns delete their deliberation, and the snapshot goes with it. If the row is gone
  when the snapshot transaction locks it, nothing is written.
- **Decisions the `record_decision` tool wrote during this turn**, by explicit id. The tool does
  **not** store the turn's session today: `record_decision` passes `session_id=None` unless
  `decision_session_id_enabled` is on (`nous/api/tools.py:1380`), and that flag defaults to off
  (`nous/config.py:899`). So a post_turn query by `session_id` would find none of them. Instead the
  ids travel with the turn, in the F091 style (explicit threading, not a ContextVar: `stream_chat`
  resumes in a fresh copied context, `nous/api/tools.py:600`):
  1. `CallOutcome` (`nous/api/call_outcome.py:17`) gains `recorded_decision_id: str | None`. The
     runner creates one `CallOutcome` per tool call (`nous/api/runner.py:2708` streaming,
     `:3003` non-streaming), and the dispatcher sets it as the handler's current outcome inside the
     handler's own task (`nous/api/tools.py:619`), so this works on both paths.
  2. `record_decision` sets it straight after `brain.record` returns (`nous/api/tools.py:1384`),
     before building its reply, so a timeout that cancels the call after the commit still leaves
     the id. Only on success.
  3. The runner collects the ids into a new `TurnResult.recorded_decision_ids: list[str]`
     (`nous/cognitive/schemas.py:221`), and post_turn snapshots those ids. The collection has to
     survive every way a turn ends that still reaches post_turn:
     - **Non-streaming.** `_tool_loop` returns a 4-tuple (`nous/api/runner.py:1670`), and when it
       raises, the `except` at `:1699` builds the `TurnResult` without it. So the ids go out through
       a collector list passed in, the way `usage_out` already is (`:1415`, `:1688`), not through
       the return value. `_dispatch_with_ledger` keeps its `CallOutcome` local, so the collector is
       passed down to it and filled there (a field on `Dispatched`, `:123`, would be lost whenever
       the call raises or is cancelled, since nothing is returned).
     - **Streaming.** Read `outcome.recorded_decision_id` straight after the dispatch, also in the
       `CancelledError`/`GeneratorExit` branch (`:2765`). post_turn runs shielded from `finally`
       (`:2935`), so ids collected there still reach it.

  Not covered, by design: `record_decision` calls made inside `run_python`, or dispatched over MCP or
  REST, run with their own outcome or none and belong to no chat turn; an approved action run
  through `execute_single_call` (`:3128`) has no post_turn. Nor does a **cancelled** non-streaming
  turn (for example the subtask timeout, `nous/handlers/subtask_worker.py:156`): `run_turn` reaches post_turn only
  after `except Exception` (`:1699`, `:1731`), and `CancelledError` is not an `Exception`, so that
  turn writes no snapshot at all, the deliberation decision's included. Those decisions get no
  snapshot.

  This is the same mechanism Phase 3 plans for `recall_deep` results.

  **Why not turn the flag on instead.** Enabling `decision_session_id_enabled` changes a write path
  that has nothing to do with F104: session-scoped decisions enter the pool of
  `DeliberationProtocol._is_duplicate`, where a match silently drops the turn's deliberation
  decision (the flag's description, `nous/config.py:899`). Making it a prerequisite would ship that
  behaviour change inside this feature. The explicit ids also need no turn time window, so a
  decision written in another turn of the same session can't match.

**Delivered ids only.** `TurnContext.recalled_{fact,decision,episode,procedure}_ids` over-state what
the model saw. `ContextEngine.build` fills `recalled_ids` **before** `_truncate_to_budget`
(`nous/cognitive/context.py:325`). Items whose lines the section budget removed land in
`_tr_budget_cut` (`context.py:331`), and only the F091 trace excludes them (`context.py:1696`).
`BuildResult.recalled_ids` still returns them (`context.py:1716`). So:

- `BuildResult` gains `delivered_ids: dict[str, list[tuple[str, str]]]`: per type, `(id, source)`
  pairs in order. For `source = "context"` they are `recalled_ids` with every `(id, type)` in
  `_tr_budget_cut` removed, order kept. `delivered_ids["fact"]` also holds the delivered profile
  facts (see "Profile facts" below). `TurnContext` carries it to post_turn.
  `_tr_budget` runs whether or not the retrieval trace is on (with tracing off `tr` is `NULL_TRACE`
  and only the trace write is skipped), and it is called for all four types (`context.py:937`,
  `:1063`, `:1503`, `:1637`).
- "Order" is the order in which `build` appended ids to `recalled_ids`. For procedures that is
  collection order, not strictly render order: they are appended in two places (`context.py:1293`
  and `:1463`).
- The snapshot is built from `delivered_ids` only, and `rank` is the index among the pairs of the
  same source in that filtered list, so the survivors of a cut close the gap (ranks 0, 1, 2, not
  0, 2, 3).
- `recalled_ids` and its consumers (F071 dedup, `UsageTracker`, the REST turn response) are not
  changed.
- `_tr_budget` (`context.py:334`) is a conservative probe: an item whose line survives in part
  counts as delivered. The snapshot inherits that. If the probe itself raises, `_tr_guard` swallows
  it and the cut set is incomplete; the snapshot then over-states delivery for that section, exactly
  as the trace does.

**Profile facts.** Two fact blocks never enter `recalled_ids`, so filtering it alone would drop
facts the model did see, the always-recalled identity and preference facts included:

- **User Profile** (`build` step 1b, `context.py:670-811`): `profile_facts`, rendered by
  `_format_facts` and cut by `_truncate_to_budget_lines` (`:789`). `profile_fact_ids` (`:785`) is
  taken **before** that cut on purpose (it is the leg's double-injection guard), so it is not the
  delivered set either.
- **Session Profile** (step 6a, `:1077-1150`, behind `profile_intent_leg_enabled`, default off):
  `leg_facts`, cut by `_truncate_to_budget_lines` against `profile_intent_leg_budget` (`:1137`).

`recalled_ids["fact"]` holds only Tier-3 facts (`context.py:676`, `:1051`): Tier-3 search excludes
the tier-1 categories (`exclude_categories=TIER1_FACT_CATEGORIES`, `:968`), the leg searches only
them (`:1101`), and the leg drops every id in `profile_fact_ids` (`:1131`). So the three blocks
are disjoint within one build, and the PK can't collide.

Each block's delivered facts are the ones whose content probe survives in that block's **truncated**
text, the same test `_tr_budget` applies (first 40 characters of the content, `context.py:348-349`),
so a fact whose line `_truncate_to_budget_lines` dropped gets no row. The check is a plain helper,
not `_tr_budget` itself, because these facts are not in the F091 trace's rendered set and must not
add `tr.drop` events. It also covers the helper's char-slice fallback when the first line alone
exceeds the budget (`:2018-2021`): a partly sliced first fact counts as delivered when its probe
survives the slice, as with `_tr_budget`. Block order is kept: `delivered_ids["fact"]` holds the User Profile pairs, then the
Relevant Facts pairs, then the Session Profile pairs, which is the render order (priority 1, then
the two priority-6 sections in append order).

**Ranking rule: a source tag, not a merged order.** Each block gets its own `source` and its own
rank sequence from 0. A merged order was ruled out: User Profile renders first, so up to
`profile_fact_limit` (20) always-injected facts would push every Relevant Facts fact down the rank
weights, and the always-present facts would hold weight 1.0. The blocks are also chosen differently
(standing profile vs. relevance search vs. intent leg), so a rank in one says nothing about a rank in
another.

**What profile rows are used for.** They are written and credited like any other row, and the diag
report shows them as their own group (section 2 and Phases). They are **not** recall wins: Phase 2
neither boosts them nor scores them in its gate. The boost read and the gate use `source = 'context'`
rows only (section 3, Phase 2 gate). Tier-1 facts can't reach the ranked fact list anyway (the
`:968` exclusion), so a profile row could only ever add to a memory that Phase 2 never ranks. The
explicit `source` filter keeps that true if a fact's category later changes: the turns in which the
fact was injected unconditionally still don't count as evidence for ranking it.

Not covered, as before: temporal-tier episodes in "Recent Conversations" also reach the model
without entering `recalled_ids` (F091 adds them to its rendered set from `_temporal_episode_ids`,
`context.py:1697-1703`). F104 v1 writes no rows for them; giving them their own `source` would follow
the profile-fact pattern and is left for a later change.

Decisions **skipped**: continuation turns (F099 lineage decisions are graded by `f099`, which this
design excludes anyway), and turns with no delivered IDs.

Size: four types, top-k each, about 30 `context` rows per decision at most, plus up to 20 User
Profile rows (`profile_fact_limit`, or 12 with `profile_core_enabled`) and up to 5 Session Profile
rows (`profile_intent_leg_limit`). That's about 15k `context` rows and up to about 12k profile rows a
month at current volume, so no retention policy is needed in v1.

### 2. Credit on review (capture, Phase 1)

New handler `OutcomeCreditHandler` subscribes to `decision_reviewed`, using the same wiring pattern as
`StrategyCardDistiller`: fire-and-forget task, coalesce per decision in flight
(`nous/handlers/strategy_card_distiller.py:210-273`). The event only says *which* decision to look
at. Its `outcome` and `reviewer` are never used (see "The row is the grade" below), so a coalesced
re-run needs nothing but the decision id.

**The row is the grade, not the event.** `Brain.review` commits and then emits
(`nous/brain/brain.py:1120-1127`) as two separate steps, so two reviews of one decision can emit in
the opposite order to their commits. Applying event values in arrival order could leave an older
grade in place, or restore credit after a newer relabel to `noise`. So the handler reads the
decision row's **current** `outcome` and `reviewer` after taking the decision lock (below), and
writes from those. `StrategyCardDistiller._do_distil` already works this way: "The decision row is
the truth, not the event that queued this run" (`strategy_card_distiller.py:301`, reading the row
in `_retire_stale_cards`, `:500`). `Brain.review` emits only when it opened the session itself
(`brain.py:1119-1127`), and every current caller lets it (`tools.py:2279`, `rest.py:1099`,
`a2ui/actions.py:605` and `:824`, `decision_reviewer.py:320`, `intention_calibration.py:261`).
`Brain.review_many` emits one event per reviewed item on the same terms (`brain.py:1150-1162`), and
its callers (`tools.py:2329`, `:2349`) don't pass a session either. So
every review commit is followed by an event, and the last handler run starts after the last commit and leaves the rows matching the final decision row.

One writer breaks that today: the compensation revert of a review (`nous/api/compensation.py:778-797`)
restores the prior `outcome` and `reviewer` with a direct `UPDATE` and emits nothing, so credit (and,
already today, the distiller's card) would keep the reverted grade. The implementation PR makes the
revert emit `decision_reviewed` after its commit when it restored a row. `StrategyCardDistiller` is
the only other subscriber, and it also reads the row, so the extra event only brings its card in
line with the row.

**Eligible reviews:** the decision row has `outcome ∈ GRADED_OUTCOMES` and
`reviewer ∉ NON_DISTILLING_REVIEWERS` (`auto`, `f099`; `nous/handlers/decision_reviewer.py:36`),
reusing the existing constant. The auto-reviewer's grade comes from stated confidence or PR
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

**Update:** the handler writes only the decision's own snapshot rows, from the row's grade:
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

**Locking: one lock per decision, shared by the handler and the snapshot insert.** Both run in one
short transaction per decision that starts by locking the **decision row**:
`SELECT outcome, reviewer FROM brain.decisions WHERE id = :decision_id AND agent_id = :agent_id FOR NO KEY UPDATE`.
That lock exists even before any snapshot row does, which a lock on the snapshot rows alone would
not (see "Review before snapshot" below). It also orders the read against reviews: every review
takes a row lock on the decision at least as strong as `FOR NO KEY UPDATE` (its `UPDATE` changes no
key column; `Brain._review` first takes `FOR UPDATE` when `preserve_graded` or `capture` is set,
`brain.py:1241-1242` via `_get_decision_orm`, `:2381`, which `resolve_decision` does through `capture` whenever
it runs under the dispatcher, `tools.py:2269-2286`). Either conflicts with ours, so under READ COMMITTED the select waits
for an in-flight review and then returns its committed grade. `FOR NO KEY UPDATE`, not `FOR UPDATE`: `FOR UPDATE` would also conflict with the
`KEY SHARE` lock that every FK-child insert of the decision takes (tags, reasons, thoughts, and the
snapshot's own rows), so those inserts would queue behind credit. (While a review holding
`FOR UPDATE` runs, FK-child inserts, the snapshot insert included, wait for that review; that is
today's behaviour and F104 adds nothing to it.) After the decision
row, the handler locks the decision's snapshot rows with
`SELECT ... ORDER BY memory_type, memory_id FOR UPDATE`, as before. Lock order is therefore always
decision row, then its snapshot rows in `(memory_type, memory_id)` order, and no F104 transaction
holds more than one decision row (post_turn writes each decision in its own transaction, section 1),
so a writer that locks several decision rows at once (`Brain.review_many`) can't deadlock with it.
Two reviews of the same decision serialize on the decision row, and the rows end up matching the
row's final grade. Two reviews of *different* decisions touch disjoint rows (the PK starts with
`decision_id`), so sharing memories can't make them wait on each other. The
deadlock the round-1 review found came from the cross-decision stats upsert; with that upsert gone it
can't happen. The fixed-order rule stays for anything that writes per-memory rows: if a cache of the
read-time stats is ever added (see section 3), every refresh writes its rows in
`(agent_id, memory_type, memory_id)` order, or takes their locks first in that order.

**Review before snapshot.** A review can commit before the snapshot exists: the agent calls
`record_decision` and then `resolve_decision` in the same turn (`resolve_decision` calls
`Brain.review`, `nous/api/tools.py:2229-2290`), or an owner grades a decision during a long turn.
The handler then finds no snapshot rows and does nothing, and the snapshot written later gets no
event of its own. So the **snapshot insert reconciles at insert time**, under the same decision-row
lock:

1. Lock the decision row and read `outcome` and `reviewer` (the statement above). Row gone (a
   deleted deliberation): write nothing.
2. Insert the delivered rows with `ON CONFLICT (decision_id, memory_type, memory_id) DO NOTHING`.
   If the row's grade is eligible, each row gets `credited_reward`, `credited_weight` and
   `credited_at = now()` in the insert. Otherwise they stay NULL.
3. Commit.

**Exactly one credit, in either order.** Both writers compute a row's credit from the decision
row's grade read under the lock, and neither adds to anything, so the result depends only on the
grade at the last write, not on which writer ran first:

- *Review, then snapshot:* the handler runs on zero rows (no-op). The insert reads the committed
  grade and credits at insert.
- *Snapshot, then review:* the insert stores NULL credit. The review's handler locks the row after
  the review commits and credits the rows.
- *Overlapping:* whichever takes the decision-row lock second sees the other's committed work. A
  handler that runs after an insert that already credited rewrites the same values, and
  `COALESCE` keeps the first `credited_at`.

Each (decision, memory) pair holds one row (the PK), so one decision contributes one credit per
memory to the read-time sums however many events or inserts arrive.

**Limit: convergence assumes every event is handled.** The handler is fire-and-forget and logs and
swallows its errors, as the distiller does, and the emit is a separate step after the review's
commit (`brain.py:1124-1127`). A crash between commit and emit, or a failed handler run, leaves the
rows on an older grade until the decision's next review. v1 does not add a repair sweep. Instead
`scripts/diag/outcome_credit_report.py` counts **mismatched decisions**: snapshot rows whose
credit isn't what the decision row's current grade gives. A non-zero count is the signal to add a
reconcile pass (which would reuse the handler's locked step per decision).

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
for each credited context row i of the memory (source = 'context', credited_weight IS NOT NULL):
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
        AND source = 'context' AND credited_weight IS NOT NULL) s
GROUP BY memory_id
```

Per-row decay is why the stored totals are gone. Decaying a stored total by its latest credit time
(the earlier draft) brings old mass back to full weight: a memory credited once a year ago and once
today would count last year's credit in full. Here each contribution ages on its own.

Only `source = 'context'` rows count (section 1, "What profile rows are used for"), so the boost
never treats an always-injected profile fact's presence as evidence. Read cost: one row per
(decision, memory) pair, about 15k `context` rows a month. A memory recalled in nearly every
decision gathers about 130 rows a month, so a top-k batch reads a few thousand index rows at most.
If that ever shows in turn latency, a cache table keyed by `(agent_id, memory_type, memory_id)` can
be rebuilt from the snapshot rows (refreshed in that key order, per section 2). v1 has no cache.

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
already ranks them, and two outcome signals on one item would double-count. For the same reason the
Phase 2 gate doesn't score procedure rows (see the gate). The procedure comparison is a separate
Phase 3 analysis with its own decision rule, written down before it runs.

## Phases

**Phase 1: capture (flag on, no ranking change)**

- Migration 093, `post_turn` snapshot (delivered ids, credit at insert), the `record_decision` id
  threading, `OutcomeCreditHandler` (row grade, decision-row lock), re-grade overwrite.
- `scripts/diag/outcome_credit_report.py`: top and bottom memories by `p_mem − p_base` with
  `n_eff ≥ 2`, the snapshot row count, eligible reviews credited per week, and the count of
  mismatched decisions (credit that disagrees with the decision row, section 2). `context` rows and
  profile rows (`profile`, `session_profile`) are listed as separate groups, and procedures as their
  own group; the report computes no gate statistic.
- Tests:
  - snapshot written for the deliberation decision and for tool-recorded decisions in the turn,
    with `decision_session_id_enabled` off (the default), on both `run_turn` and `stream_chat`
  - a decision recorded in an earlier turn of the same session is not in this turn's snapshot, and a
    failed `record_decision` call adds no id
  - budget cut: with a section budget that cuts one of three recalled facts, the cut fact gets no
    snapshot row, and the two survivors have ranks 0 and 1
  - profile facts: with three User Profile facts and a `user_profile` budget whose line cut drops the
    third, plus two Relevant Facts facts, the two delivered profile facts get `source = 'profile'`
    rows at ranks 0 and 1 (strength NULL), the cut profile fact gets no row, and the Relevant Facts
    rows keep `source = 'context'` at ranks 0 and 1. The same for a Session Profile fact cut by
    `profile_intent_leg_budget` (`source = 'session_profile'`)
  - no snapshot for a deleted deliberation or a continuation turn
  - auto and f099 reviews credit nothing
  - success → partial re-grade leaves the row at the partial values, with `credited_at` unchanged
  - relabel to noise clears reward and weight and keeps `credited_at`
  - per-row decay: a memory credited 365 days ago and today has the old row at weight ≈ 0.015, not 1
  - review before snapshot: `record_decision` then `resolve_decision(success)` in one turn; the
    handler runs on zero rows, and post_turn's insert credits the rows at insert from the row's grade
  - snapshot before review: the insert stores NULL credit, and the later review's handler credits the
    rows. Both orders end with identical rows and one `credited_at` per row
  - review before snapshot by an excluded reviewer (`auto`): the insert stores NULL credit
  - out-of-order events: review A (`success`) and then review B (`noise`) commit, and their events are
    delivered B first, then A. The rows end matching the final decision row (`noise`: NULL credit),
    not A's event values. The same with B = `partial`: rows at the partial values
  - two concurrent reviews of one decision: after both handlers finish, every snapshot row's credit
    equals what the decision row's final `outcome` and `reviewer` give, not merely one review's values
  - a snapshot insert that waits on an in-flight review (review holds the decision row, uncommitted)
    reads the review's committed grade once it commits. Pinned deterministically: hold the review's
    transaction open, wait until `pg_stat_activity` shows the insert as a lock waiter, then commit
    (a gather of two coroutines can pass with the lock removed if they happen not to overlap)
  - a compensation revert of a credited review emits `decision_reviewed`, and the rows end matching
    the restored grade
  - the decision-row lock doesn't block an FK-child insert (a `decision_tags` row) while credit runs
  - lock order: two decisions sharing memories A, B, C, recalled in reverse rank order (A, B, C and
    C, B, A), reviewed concurrently from two sessions, both commit with no deadlock or lock timeout,
    and the read-time stats for A, B and C equal the sum of both decisions' credit
  - rows of another `agent_id` are not read
  - profile rows don't feed the read: the section 3 stats query, which the report also uses for its
    `context` group, returns no row for a fact whose only credited rows are `profile` rows
  - an empty delivered set writes nothing

**Phase 2 gate: turn on the boost.** All three must hold:

1. **At least 300** credited (decision, memory) pairs among the scored rows (`source = 'context'`,
   type fact, decision or episode).
2. **Offline rolling-origin check**, with the decision rule fixed here, before any data arrives:
   - **Rows scored.** Only rows Phase 2 can re-rank: `source = 'context'` rows of type `fact`,
     `decision` and `episode`. Procedure rows are out (Phase 2 doesn't boost them, see Procedures),
     and so are `profile` and `session_profile` rows (never boosted, section 1). A decision with no
     such row is not scored and doesn't count toward the pool.
   - **Score.** A decision's score is the `w`-weighted mean of `p_mem − p_base` over its scored
     rows, with `p_mem` from the same `source = 'context'` rows as the boost read. For the fold
     that scores week k+1, `p_mem` and `p_base` use only rows with `credited_at` before that week
     starts, decayed from the week's start (not from `now()`). Re-grades overwrite,
     so a fit row carries its current grade, not the grade it had at the fold's start. That is
     accepted: a re-grade corrects an earlier decision's label and carries nothing about the scored
     week's outcomes.
   - **Folds.** For every k across at least 8 weeks of capture: fit on weeks 1..k, score the eligible
     graded decisions of week k+1. A decision is scored once, in the fold for its week.
   - **Pool.** Pool the scored decisions over all folds. The pool must hold **at least 30
     non-success** (partial or failure) scored decisions.
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

   **Why only the boosted types.** The gate decides whether to turn on a boost that touches facts,
   decisions and episodes only. If procedure credit predicted outcomes and the others didn't, an
   all-row score could pass the gate on a signal the boost never uses; procedure noise could
   instead fail it and, under the stop rule, end Phase 2 for good.

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
- **Procedure credit analysis**, separate from the Phase 2 gate. Before it runs, the Phase 3 spec
  writes down its own decision rule (score, folds, test and threshold, as the Phase 2 gate does) for
  whether graded procedure credit predicts outcomes better than F037's tool-error proxy. Only if it
  passes: replace the F037 proxy with graded credit for procedures, or blend the two.
- Persist `UsageTracker` (separate small fix; worth doing anyway, since citation stats reset on every
  deploy).
- An "avoid" cue: when a recalled decision has `outcome=failure` *and* its snapshot memories have low
  `p_mem`, add a one-line warning in context. This is Memento's negative-case prompting. Low priority:
  censors and outcome-tagged related decisions already cover most of it.

## Risks

- **Confounding.** Memories that are always delivered (identity facts, Tim's preferences in the User
  Profile) are present in nearly every decision, so their credit tracks `p_base`. Their rows are
  `source = 'profile'`: credited and reported, but neither boosted nor scored in the gate (section
  1). Base-rate centring would neutralise them anyway; the source filter makes it explicit. Memories
  recalled very often through the ranked sections get the same centring.
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
