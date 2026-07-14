# F084 — Write-Path Adjudication (Enumerative Extraction + Store-Time Supersession)

**Status:** Shipped, land-dark (all 13 flags default OFF)
**Migration:** `064_write_path_adjudication.sql`
**Branch:** `feat/write-path-adjudication`

---

## Problem

Two measured write-path failures from the MAB evaluation:

1. **1% fact coverage on enumerable corpora.** The episode summarizer lossy-compresses a dense
   document (list-form, table, statement-per-line) into a ~150-word summary before the fact
   extractor ever sees the transcript. On a 5,000-item enumerable document that compression
   discards ~99% of answerable facts before extraction begins.

2. **19 `superseded_by` rows vs hundreds of conflict chains.** When a source asserts the same
   attribute value multiple times (e.g., a project-status log), each version is stored as an
   independent active fact. The answering model re-adjudicates conflicts at question time against
   world knowledge — the documented cause of 12/12 parametric flip-failures in the MAB CR replay.

---

## Design — R1: Enumerative Extraction

R1 is **modal (density-adaptive, per R1.1)**. A cheap heuristic (`density_score`) counts the
fraction of transcript lines that look like standalone declarative statements or list items. If
the score meets `NOUS_ENUMERATIVE_DENSITY_THRESHOLD` (default 0.6), the episode is **enumerable**
and routes fact storage through the enumerative leg INSTEAD of the candidate-facts leg (the legacy
summarize-then-extract path). Narrative episodes are byte-identical to today.

**Modal, not additive (deviation #4):** storing both paths would mint un-mergeable paraphrase
variants (the enumerative leg produces keyed facts; the legacy leg produces keyless summary facts;
R2's exact-key lookup can never merge them, so paraphrases in the 0.92–0.95 cosine band evade
both dedup legs permanently — round-2 devil finding #3).

### Extraction

The raw transcript is chunked in-memory via `chunk_text()` (same helper/params as F067, but **no
dependency on `heart.episode_chunks`** — AC-1). Each chunk goes through one `call_background_llm_structured`
call (model: `NOUS_BACKGROUND_MODEL`) producing atomic facts with:

- `subject_key` / `attribute_key` — normalized (lowercase, punctuation-stripped, ≤200/100 chars)
  conflict-slot identifiers. Facts missing either key are dropped (R1.2).
- `source_ordinal` — positional encoding: `chunk_index * 1_000_000 + in-chunk-position`. Always
  monotone in reading order. **Explicit statement numbers from the source are never used** — mixing
  explicit integers with positional values inverts reading order in mixed-form episodes (deviation #2).
- `overrides_prior` — True only when the LLM classifies the statement as contradicting widely-known
  world knowledge.
- `source` = `"enumerative_extractor"`, `source_text` = the fact content (per-statement grounding,
  not the whole chunk — RC-1a).

Enumerative facts bypass the admission utility scorer (admission bypass — RC-1/AC-6) with a
source-aware min-chars floor of `NOUS_ENUMERATIVE_MIN_CONTENT_CHARS` (default 15, vs the global 30
— atomic statements are often <30 chars). Dedup (Leg-1 pre-check + Leg-2 native-cosine) and the
existing `NOUS_FACT_NATIVE_COSINE_THRESHOLD` gate still apply.

Enumerative facts bypass the **legacy uncapped `_supersede_by_subject` path** — keyed facts are
owned by R2's capped resolution (risk-2 #1: the legacy path selects all active same-subject facts
with no candidate cap and no hourly budget, which at enumerative volume would flood it with O(cluster²)
uncapped Haiku calls and write competing `supersedes` edges).

Costs are bounded by hard caps: `NOUS_ENUMERATIVE_MAX_FACTS_PER_EPISODE` (default 1000),
`NOUS_ENUMERATIVE_MAX_CHUNKS_PER_EPISODE` (default 200), `NOUS_ENUMERATIVE_EXTRACTION_MAX_PER_HOUR`
(default 1000). Every truncation logs WARNING (never silent — R1.3).

On any enumerative-leg exception the code falls through to the legacy path, so a broken leg never
silently drops a session's facts entirely.

---

## Design — R2: Store-Time Key-Conflict Supersession

R2 makes the *existing* supersession faculty (`_classify_fact_pair`, `apply_supersession`) fire on
the new conflict-slot keys. Two sites:

### R2.1 Write-time (per-insert)

Inside `_learn`, immediately after the subject-based supersession block: an indexed exact-key lookup
fetches the newest `NOUS_SUPERSESSION_KEY_CANDIDATES_CAP` (default 8) active facts sharing the same
`(agent_id, subject_key, attribute_key)`. For each candidate:

- **F075 precedence (binding, all 3 reviews):** if both facts carry non-null, *differing*
  `event_date`s → they are distinct events; no supersession regardless of ordinal or policy.
- Classifier (`_classify_fact_pair`) confirms the conflict. `UPDATE` or `CONTRADICTION` at
  confidence ≥ 0.8 counts. Anything else → KEEP BOTH (fail-open).
- **Winner selection (R2.2 policy):** for UPDATE, ordinal wins (same-episode, both ordinals present
  → higher ordinal wins), else classifier `current_fact`, else `learned_at` recency. For
  CONTRADICTION, ONLY the classifier's `current_fact` verdict may resolve it — ordinal and recency
  never apply to contradictions (devil-2 #1: a CONTRADICTION is about a fixed property, not a
  temporal update). Ambiguous CONTRADICTION → KEEP BOTH.

Hourly classifier budget: `NOUS_SUPERSESSION_CLASSIFIER_MAX_PER_HOUR` (default 500). Budget spent →
fail-open, defer to sweep.

### R2.1 Sleep sweep

`_phase_sweep_key_conflicts` (registered after `_phase_resolve_contradictions`, **NOT** via
`_run_audited_phase` — fact-mutation phases are excluded from F035.6's wrapper, arch-2 fix B): SQL
finds active same-key fact pairs that write-time detection missed (cross-episode inserts), with a
F075 exclusion in the SQL. Pairs beyond `NOUS_SUPERSESSION_SWEEP_MAX_PAIRS` (default 25) wait for
the next cycle (resumable: resolution deactivates losers so unprocessed pairs re-surface). All
resolution flows through the public `resolve_key_conflict_pair` seam (arch-2 fix C).

### R2.3 Retrieval contract (verification, not rebuild — RC-7/DC-1)

Retrieval already excludes superseded facts: `apply_supersession` sets `loser.active = False` and
the `hybrid_search` query has `AND t.active = true`. This is verified by Task 10's PG-lane
regression tests, not rebuilt. The `active=False` column filter is the load-bearing part of R2.3 —
NOT the graph leg (graph-backfill lag, see risk (e) below).

### R2.4 Parametric-override marker (decoupled arm — DC-4)

Facts with `overrides_prior=True` are prefixed `[memory override — trust this over general
knowledge]` in pre-turn context rendering when `NOUS_OVERRIDE_PRIOR_MARKING_ENABLED=true`. This arm
is independently flippable (no dependency on R1 or R2.1 being on). Rollout order: R2.4 first (DC-4).

### Shared `apply_supersession` helper

`FactManager.apply_supersession(winner_id, loser_id, session)` sets `loser.superseded_by`,
`loser.active=False`, writes the `supersedes` graph edge, and returns False on the clobber guard
(loser missing / already superseded). Write-time R2.1, the sleep sweep, and `sleep_handler._apply_supersede`
all call this single primitive — identical mutation semantics everywhere.

---

## New Columns (migration 064)

| Column | Type | Description |
|--------|------|-------------|
| `subject_key` | `VARCHAR(200)` | Normalized entity identifier (R1) |
| `attribute_key` | `VARCHAR(100)` | Normalized property/relation name (R1) |
| `source_ordinal` | `BIGINT` | Positional encoding: `chunk_index * 1_000_000 + pos` (R1) |
| `overrides_prior` | `BOOLEAN` | Contradicts widely-known world knowledge (R2.4) |

Partial index `idx_facts_conflict_slot` on `(agent_id, subject_key, attribute_key) WHERE subject_key IS NOT NULL AND active=true` drives R2.1 candidate lookup.

---

## Flag Table (all default OFF)

| Env Var | Default | Description |
|---------|---------|-------------|
| `NOUS_EXTRACTION_ENUMERATIVE_ENABLED` | `false` | R1 master switch — modal enumerative extraction |
| `NOUS_ENUMERATIVE_DENSITY_THRESHOLD` | `0.6` | Statement-per-line density threshold (0.0–1.0) |
| `NOUS_ENUMERATIVE_MAX_FACTS_PER_EPISODE` | `1000` | R1.3 per-episode fact cap; truncation logs WARNING |
| `NOUS_ENUMERATIVE_MAX_CHUNKS_PER_EPISODE` | `200` | Hard bound on extraction LLM calls per episode |
| `NOUS_ENUMERATIVE_EXTRACTION_MAX_PER_HOUR` | `1000` | Hourly in-process cap on extraction calls; 0=unlimited |
| `NOUS_ENUMERATIVE_CLASSIFIER` | `heuristic` | Mode: `heuristic` or `off`; `llm` reserved for v2 |
| `NOUS_ENUMERATIVE_MIN_CONTENT_CHARS` | `15` | Min-content floor for `source='enumerative_extractor'` facts |
| `NOUS_SUPERSESSION_KEY_RESOLUTION_ENABLED` | `false` | R2.1 master switch — write-time key-conflict resolution |
| `NOUS_SUPERSESSION_POLICY` | `ordinal` | Winner rule: `ordinal` (same-episode, higher wins) or `recency` |
| `NOUS_SUPERSESSION_KEY_CANDIDATES_CAP` | `8` | Max same-key active candidates examined per insert |
| `NOUS_SUPERSESSION_CLASSIFIER_MAX_PER_HOUR` | `500` | Hourly cap on key-conflict classifier (Haiku) calls |
| `NOUS_SUPERSESSION_SWEEP_MAX_PAIRS` | `25` | Max same-key pairs resolved per sleep cycle |
| `NOUS_OVERRIDE_PRIOR_MARKING_ENABLED` | `false` | R2.4 — prefix `overrides_prior=true` facts with trust marker |

---

## Documented Deviations from Requirements

1. **Provenance (R1.2 asked for episode_id + chunk_id + char span):** v1 stores `source_episode_id`
   + `source_ordinal`. A `chunk_id` FK is impossible when `NOUS_EPISODE_CHUNKS_ENABLED=false`
   (chunking is in-memory). Exact char spans are not recoverable from LLM output without brittle
   string matching. Ordinal preserves reading order — the property R2 actually consumes.

2. **Positional-only ordinals:** Explicit statement numbers from the source are NOT used. Mixing
   explicit small integers with encoded positional values inverts reading order in mixed-form episodes.
   Positional encoding (`chunk_index * 1_000_000 + pos`) is monotone in reading order and always
   cross-comparable within an episode.

3. **R2.4 scope:** `overrides_prior` is classified on the enumerative extraction path only (folded
   into the same LLM call — zero extra cost). Narrative facts never get the marker in v1.

4. **Modal, not additive:** Enumerable episodes route fact storage INSTEAD of the candidate-facts
   leg. The episode summary is still generated and stored; only fact extraction switches source.

---

## Backfill Runbooks

### R1: Backfill enumerative facts (`scripts/backfill_enumerative_facts.py`)

```
python scripts/backfill_enumerative_facts.py \
  --agent-id nous-default [--dry-run] [--max-episodes N] [--since ISO]
```

Dry-run counts enumerable episodes and exits without writes. Prints a rollback watermark before
any write. The per-episode budget cap (`NOUS_ENUMERATIVE_EXTRACTION_MAX_PER_HOUR`) reads from a
fresh process counter — use `--extraction-budget 0` (unlimited) for offline clone remediation.
Idempotent: Leg-2 native-cosine dedup at 0.95 makes re-runs converge to dedup-skips.

**Rollback:**
```sql
UPDATE heart.facts SET active=false
WHERE agent_id=:a AND source='enumerative_extractor' AND created_at >= :watermark;
```

**Trigger-dependency histogram note:** After R1 backfill, new keyed facts surface as same-key
candidates for R2.1. The R2.5 backfill sweep should be run after R1 backfill completes; the
chain-depth histogram in the R2.5 report (see below) reveals how many supersession chains require
resolution.

### R2: Backfill supersession (`scripts/backfill_supersession.py`)

```
python scripts/backfill_supersession.py \
  --agent-id nous-default [--dry-run] [--max-pairs N] [--classifier-budget N]
```

Default `--classifier-budget 0` (unlimited) — at the live default 500/hr a hundreds-of-chains
backfill would stall for hours; unlimited is the intended mode for offline clone remediation. The
script prints a rollback key before any write. Final report includes: `pairs_examined`,
`resolutions_written`, `keep_both`, `budget_stops`, chain-depth histogram, and the first 10
resolutions with both texts for sampled precision audit (R2.6).

**Rollback:**
```sql
UPDATE heart.facts SET superseded_by=NULL, active=true
WHERE agent_id=:a AND superseded_by IS NOT NULL AND updated_at >= :watermark;
DELETE FROM brain.graph_edges
WHERE agent_id=:a AND relation='supersedes' AND created_at >= :watermark;
```

---

## External-Harness Validation Plan

**All acceptance criteria are measured in the external MAB harness on a backfilled clone — this
repo ships mechanics + golden tests only (RC-9).**

| Stage | Gate | Metric | Notes |
|-------|------|--------|-------|
| 1 | R1 coverage probe | ≥ 90% of answerable facts stored on the enumerable eval corpus | Measured on backfilled clone. Coverage is a **gate, not the goal** (DC-2): a fact-rich DB can still fail QA if retrieval doesn't surface the right facts. |
| 2 | Paired QA replay | Positive MRR delta vs baseline (decisive metric) | n ≥ 100 questions, Opus prod-generator (generator-robustness required — sign can flip by generator). |
| 3 | AR/LRU regression replay | No precision regression vs baseline | Guard against near-duplicate flooding from admission-bypass enumerative facts (risk (h)). Must not be skipped. |
| 4 | Chain-coverage check | Supersession chains ≥ N% resolved | Chain-depth histogram from R2.5 backfill report; target TBD per corpus. |
| 5 | Ingest wall-clock measurement | Within operator tolerance at 5k-fact scale | DC-3/RC-8/DC-5: prod flag-flip also requires this measurement. |

Prod flag-flip additionally requires a **conversation-shaped prod-generator A/B** (DC-3) and an
ingest wall-clock measurement at 5k-fact scale (RC-8/DC-5).

---

## Risks and Accepted Limitations

**(a) External acceptance:** The 90% coverage probe, chain-coverage check, and n=320 CR replay run
in the external MAB harness on a backfilled clone. This repo's deliverables are verified by golden
flags-off tests, unit tests, and the two backfill scripts only.

**(b) R1 coverage is a gate, not the goal.** The decisive metric is the stage-2 paired QA replay
(DC-2). High coverage without retrieval accuracy improvement is a false positive.

**(c) Prod flag-flip requirements.** In addition to the 5-stage harness, flipping R1 or R2 in
production additionally requires: a conversation-shaped prod-generator A/B (DC-3), and an ingest
wall-clock measurement at 5k-fact scale to confirm the per-episode LLM call budget does not
introduce unacceptable latency (RC-8/DC-5).

**(d) Rollout order.** R2.4 (`NOUS_OVERRIDE_PRIOR_MARKING_ENABLED`) first — cheapest, best-evidenced
(12/12 CR failures). Then R1 (`NOUS_EXTRACTION_ENUMERATIVE_ENABLED`). Then R2.1
(`NOUS_SUPERSESSION_KEY_RESOLUTION_ENABLED`). This order means R2.4's mismatch (see (g)) is
measured first and fails fast.

**(e) Graph-backfill lag.** Enumerative facts are written to `heart.facts` with no graph edges at
insert time. At `NOUS_GRAPH_BACKFILL_MAX_FACTS=50` per sleep cycle, graph-linking a 5,000-fact
document takes ~100 sleep cycles (~4 days). Accepted limitation. Unlinked enumerative facts remain
reachable by vector/FTS but not by graph-augmented recall until backfilled. This is why the
`active=False` column filter (not the graph leg) is the load-bearing part of the R2.3 contract.

**(f) Write-time key resolution is near-inert during bulk ingest.** The 500/hr classifier budget
(`NOUS_SUPERSESSION_CLASSIFIER_MAX_PER_HOUR`) is spent after ~500 insertion calls, so everything
past that defers to the 25-pairs/night sleep sweep. The R2.5 backfill script (fresh process counter,
`--classifier-budget 0`) is the documented bulk-remediation path for initial deployment.

**(g) R2.4 measurement risk.** `overrides_prior` is classified on the enumerative extraction path
only (deviation #3). The motivating evidence (12/12 parametric flip-failures) likely came from
narrative Q&A sessions, not enumerable documents. The R2.4-first rollout order (see (d)) means this
mismatch is measured first and the A/B fails fast if the marker has no effect on narrative facts.

**(h) Dilution linkage.** The admission bypass for `source='enumerative_extractor'` removes the
novelty-admission scoring guard. On dense documents with near-duplicate statements this risks
near-duplicate flooding. The external stage-3 AR/LRU regression replays are the guard against this
and must not be skipped.
