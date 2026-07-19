# F085 — Keyed Fact Selection (Bidirectional Entity Indexing + Keyed Retrieval Leg)

**Status:** Shipped, land-dark (all 5 new flags default OFF; write-side entity emission rides F084's existing
`extraction_enumerative_enabled` flag — no new write-side master flag)
**Migration:** `065_fact_entity_keys.sql`
**Branch:** `feat/r3-keyed-selection`

---

## Why

F084 fixed *existence*: modal enumerative extraction routes dense/enumerable documents through a raw-chunked
extraction leg instead of the lossy summarize-then-extract path, so the atomic facts a corpus actually contains
get stored as `heart.facts` rows. That did not fix *selection*. Per the MAB team's R3 requirements
(`nous-r3-keyed-selection-requirements.md` — external to this repository, referenced by name in the
implementation plan but not committed here), measurement on a backfilled F084 corpus showed a **−5.0pp
selection failure**: facts existed but the retrieval pipeline could not reliably surface the right one for
the queries that needed it. The MAB team's keyed-similarity probe measured **0.20–0.23** for the affected
question class — well below a healthy retrieval score — and characterized it as an **indexing deficiency**,
not a coverage or ranking-quality one: cosine/FTS search over dense, enumerable content routinely fails to
rank the single correct atomic fact among hundreds of structurally near-identical siblings (e.g. "the capital
of X is Y" repeated across 200 rows with different X/Y), because there is no way to ask "give me exactly the
fact about X" — only "give me facts that resemble the phrase (X, Y)".

F085 (R3 in the requirements doc) closes that gap: it makes F084's atomic facts *selectable by exact entity
key*. It sits entirely downstream of and orthogonal to F084's existence fix — R3.1 bidirectional entity
indexing, R3.2 one canonical key normalizer used by every producer and consumer, R3.3 a land-dark keyed
retrieval leg.

---

## R3.1 — Bidirectional Entity Indexing

A new join table, `heart.fact_entity_keys` (migration `065_fact_entity_keys.sql`, DDL-only), indexes every
named entity participating in a keyed fact — not just the subject, but proper-noun object/value-side entities
too. A fact whose subject is "The Marriage of Figaro" and whose value is "Thomas Kyd" gets two entity-key
rows, so a query mentioning either the work or the person can find it by exact key.

- **Schema:** `fact_id UUID FK CASCADE`, `entity_key VARCHAR(200)`, `agent_id VARCHAR(100)`, `created_at`;
  composite PK `(fact_id, entity_key)`; index `idx_fact_entity_keys_agent_key(agent_id, entity_key)`. Plus
  `heart.facts.entity_keys_extracted_at TIMESTAMPTZ NULL` — the R3.2 backfill watermark, stamped by both the
  live write path and the backfill's extract phase.
- **Same-call emission:** entities are produced in the SAME extraction LLM call as F084's R1 schema — a new
  `entities` array property on the per-fact schema, zero additional LLM passes. `_to_fact_inputs` unions the
  subject key with the emitted entities, normalizes every candidate through `normalize_key`, and applies a
  stop-policy (`is_keyable_entity`) before building `FactInput.entity_keys`.
- **Stop-policy:** numeric/scalar values (`"1876"`, `"red"`, `"true"`) are never indexed as entities — R3.1
  indexes entities, not scalars, because a scalar subject key indexed as an entity buys nothing (R2's conflict
  lookup already reads `facts.subject_key` directly, not this table) and only creates junk retrieval buckets.
  The policy applies uniformly to subject AND object/value entities, at every producer — write path,
  `phase_seed`, `phase_extract` — with no subject exemption anywhere in the entity index. `facts.subject_key`
  itself is never filtered; the stop-policy only gates what gets an entity-index row.
- **Same-transaction write:** entity rows are inserted in the SAME transaction as the fact row (`Heart._learn`,
  immediately after flush) and in `_confirm_duplicate`'s dedup-backfill branch, via `ON CONFLICT DO NOTHING` —
  a seeded-but-not-yet-extracted duplicate has entity rows without the watermark stamp, so a plain `session.add`
  would PK-collide and abort a live `learn`.
- **Soft-delete invariant:** entity rows are never cleaned up on supersession. Every read of
  `fact_entity_keys` MUST join `heart.facts` on `active = true` — standalone reads of the entity table are
  forbidden.

## R3.2 — Canonical Key Normalizer v2

One normalizer, `normalize_key` in `nous/heart/keys.py`, is now the only place subject keys, attribute keys,
and entity keys get canonicalized — used by the write path (`enumerative_extractor.py`, which re-exports it
for backward compatibility), `Heart.learn`, and the backfill script alike. Rules: NFC-normalize, lowercase,
underscores → spaces, strip punctuation except intra-word hyphens (`"cross-encoder"` survives; a dangling
`"cross -encoder"` collapses to `"cross encoder"`), collapse whitespace, strip one leading article (a/an/the),
cap at `max_len` (200 for entity/subject keys, 100 for attribute keys).

The strip-and-cap sequence is **fixpoint-iterated**, not applied once:
`normalize_key(normalize_key(x)) == normalize_key(x)` is a hard property, hand-rolled tested (`hypothesis` is
not a dependency). A single pass on `"the a red car"` would incorrectly stop at `"a red car"`, and
length-capping can leave a dangling hyphen a second pass needs to clean up — both are why the loop exists.

**Why the backfill is load-bearing:** R2 (F084)'s conflict lookup and the sleep-cycle sweep compare
`subject_key`/`attribute_key` — and now `entity_key` — by **exact string equality**
(`facts.py:1727-1733`, sleep sweep `:1540-1605`). Normalization happens at the PRODUCER: the enumerative
extractor is today's only subject/attribute-key producer, and `Heart.learn` does not re-normalize those two
columns on its own — it only defensively re-normalizes `entity_keys` at insert. Until the backfill's
`phase_normalize` runs, facts written before this normalizer shipped (v1-format keys) and facts written after
(v2-format keys) silently fail to match each other in R2's exact-equality conflict detection. This is a
format-boundary gap, not a normalizer bug, and it is why running the backfill promptly post-deploy is a
deploy requirement, not an optional follow-up (see Deploy note below).

## R3.3 — Keyed Retrieval Leg (land-dark)

Gating is **flag + entity-presence**, not frame. `run_recall_pipeline` receives no frame/intent signal and the
MAB eval harness has no frame concept at all, so frame-gating would make the leg invisible to their own
acceptance replay — flag (`NOUS_KEYED_FACT_LEG_ENABLED`, default false) plus "did the query yield at least
one entity candidate" is the only gating that fires in their harness.

The leg extracts entity candidates from the query via NER-lite (`extract_entity_candidates`): quoted spans,
runs of capitalized words (skipping sentence-initial position), and n-gram matches against the agent's own
indexed-key vocabulary (`entity_key_vocabulary`, TTL-cached 300s) — the vocab leg recovers lowercase or
sentence-initial mentions the capitalization heuristic alone misses (`"the marriage of figaro"`). The leg
fires only when at least one candidate survives.

When it fires, `FactManager.fetch_by_entity_keys` returns active facts (again joined on `active = true`)
matching ANY candidate key, ranked by matched-key count then recency/ordinal, bounded to
`NOUS_KEYED_FACT_LEG_K` (default 8) rows.

**Merge is additive-only and never reorders existing results.** Every keyed hit becomes a fresh
`PipelineResult` (`type="fact"`, `source="heart"`,
`metadata={"retrieval_leg": "keyed", "matched_keys": N}`), id-deduplicated against every other leg's output,
and scored in a bounded band under `NOUS_KEYED_FACT_LEG_SCORE` (default 0.55 — mid-range on the RRF [0,1]
scale, below the direct-hit head) so a keyed hit can enter context without displacing an existing
higher-scoring result. Keyed hits are inserted at their **sorted position**, never tail-appended: because
`rerank_by_score` defaults False and the MAB harness's `multi_turn_eval` never sets it, a tail-appended hit
would sit at position 11+ and get sliced off by `[:top_k]` — the leg would measure as a no-op on the
acceptance path (the same failure mode that blinded an earlier internal graph-qrel mining effort). Provenance
surfaces to user-facing text via `_via_tag` returning `"[via keyed] "`.

---

## Backfill runbook: `scripts/backfill_r3_entity_keys.py`

```
python scripts/backfill_r3_entity_keys.py --agent-id nous-default [--dry-run] \
  [--phase normalize|seed|extract|all] [--batch-size 500] [--llm-batch 40] \
  [--max-llm-calls 2000] [--log-level INFO]
```

Three phases, run in this order (`--phase all`, the default, runs all three in sequence; each phase is
independently re-runnable and safe to invoke alone via `--phase`):

1. **`normalize`** — re-normalizes existing `facts.subject_key`/`attribute_key` and
   `fact_entity_keys.entity_key` rows in place through the current `normalize_key` (idempotent fixpoint: a
   fact already in canonical form is a no-op on re-run). **Must run before `seed`/`extract`** — otherwise R2's
   exact-key comparisons keep missing conflicts across the v1↔v2 key-format boundary (see R3.2 above).
   Entity-row rewrites only touch rows where the new key differs from the old one: `INSERT` the canonical
   row `ON CONFLICT DO NOTHING`, then `DELETE` the stale row — an unconditional insert+delete would instead
   risk deleting an already-canonical row after its insert silently conflicted.
2. **`seed`** — inserts a `fact_entity_keys` row for every fact's `subject_key`, subject to the same
   stop-policy as the live write path (a scalar subject like `"red"` is not indexed). `ON CONFLICT DO NOTHING`
   makes re-runs idempotent.
3. **`extract`** — the only phase that calls an LLM (Haiku via `call_background_llm_structured`). For facts
   with `subject_key IS NOT NULL AND entity_keys_extracted_at IS NULL`, batches of `--llm-batch` (default 40)
   statements are sent for value-side entity extraction. LLM indices are validated (out-of-range or
   duplicate → WARN + skip that item, never crash the batch); a round that stamps zero facts (every item
   malformed/omitted, or the call itself failed) stops the CLI loop rather than burning the remaining budget
   re-asking about the same persistently-omitted facts. Every fact the LLM returns an item for — even an
   empty one — gets `entity_keys_extracted_at` stamped; that stamp IS the resume marker.

**Watermark / rollback:** the CLI prints a `created_at` rollback watermark ("ROLLBACK KEY") before any write.
Phase-1 (`normalize`) key rewrites are value-idempotent (the fixpoint is the fixpoint) and preserve the
original row's `created_at` on rewrite, so they're transparent to rollback rather than exempt from it. Undo
a run with `--phase rollback --watermark <iso-ts>` (`phase_rollback`): it deletes entity rows created at/after
the watermark and resets `entity_keys_extracted_at` on facts stamped at/after it, so extract's `IS NULL`
resume predicate revisits them on the next run. `--dry-run` reports counts only. Raw SQL, for reference:

```sql
DELETE FROM heart.fact_entity_keys
WHERE agent_id = :agent_id AND created_at >= :watermark;
UPDATE heart.facts SET entity_keys_extracted_at = NULL
WHERE agent_id = :agent_id AND entity_keys_extracted_at >= :watermark;
```

**Rollback quiescence + live-write guard (codex round 12):** `created_at >= watermark` alone can't tell a
pre-existing fact whose entity rows the backfill touched apart from a fact some OTHER, concurrent
`Heart.learn` call created during the run — the latter's own entity rows would be wrongly swept up too.
Running the rollback against a quiesced system (no live traffic) avoids the ambiguity entirely and is the
recommended way to roll back. When that isn't possible, `phase_rollback` counts distinct facts whose
`learned_at` is *also* `>= watermark` among the rows about to be deleted and **aborts with no writes** when
that count is nonzero — pass `--include-live-writes` to proceed anyway (or leave them out and let a later
`--phase extract` re-derive their keys, since `entity_keys_extracted_at IS NULL` is its resume predicate).
`--dry-run` always reports the count and never aborts.

**Resume:** only phase 3 (`extract`) needs a resume story — it processes strictly
`entity_keys_extracted_at IS NULL` facts, so killing and re-running mid-extraction continues from the same
predicate with zero duplicate LLM spend. Phases 1 and 2 are naturally re-runnable by construction.

**Commit discipline:** `phase_normalize`/`phase_seed`/`phase_extract` never call `session.commit()` — only
the CLI's `main()` commits, one session per batch/round, so a kill mid-run loses at most the in-flight
batch, never the whole phase.

**Deploy note:** run this backfill promptly after deploying the v2 normalizer. Between deploy and
`phase_normalize` completing, R2's conflict detection silently misses conflicts between v1-keyed legacy facts
and v2-keyed new facts (no data loss — the sleep-cycle sweep still catches them once normalization catches
up — but exact-key matching is degraded for the gap window).

---

## New Columns / Table (migration 065)

| Object | Type | Description |
|--------|------|-------------|
| `heart.fact_entity_keys` | table: `fact_id UUID FK CASCADE`, `entity_key VARCHAR(200)`, `agent_id VARCHAR(100)`, `created_at TIMESTAMPTZ`; PK `(fact_id, entity_key)` | R3.1 entity index join table |
| `idx_fact_entity_keys_agent_key` | index on `(agent_id, entity_key)` | Drives R3.3's exact-key fetch |
| `heart.facts.entity_keys_extracted_at` | `TIMESTAMPTZ NULL` | R3.2 backfill watermark; stamped by both the live write path and backfill phase 3 |

## Flag Table (all default OFF except the two R3.1 index caps, which are non-zero defaults not master switches)

| Env Var | Default | Description |
|---------|---------|-------------|
| `NOUS_ENTITY_KEYS_MAX_PER_FACT` | `8` | R3.1: max entity-key index rows per fact (subject key always included, subject to the stop-policy). |
| `NOUS_ENTITY_KEY_MIN_CHARS` | `3` | R3.1: stop-policy floor — normalized entity keys shorter than this are not indexed (applies to subject keys too). |
| `NOUS_KEYED_FACT_LEG_ENABLED` | `false` | R3.3 master switch — land-dark exact entity-key retrieval leg in `run_recall_pipeline`. |
| `NOUS_KEYED_FACT_LEG_K` | `8` | R3.3: bounded allotment — max keyed facts merged per query. |
| `NOUS_KEYED_FACT_LEG_SCORE` | `0.55` | R3.3: score-band ceiling for keyed hits (RRF [0,1] scale, below the direct-hit head) — the non-displacement guard. |

---

## Documented Deviations from the R3 Requirements Text

1. **Gating is flag + entity-presence, not "question turns."** The pipeline receives no frame/intent signal,
   and the MAB harness has no frame concept at all — frame-gating would make their own acceptance replay
   blind to the leg regardless of whether the mechanism works.
2. **"The extractor already parses both sides" is false in code.** Entities are a net-new schema field (the
   `entities` array) — not something the existing extraction call already produced. It still costs zero extra
   LLM passes, because it is folded into the same call that already emits `subject_key`/`attribute_key`.
3. **Bounded fetch alone cannot guarantee non-displacement under a single global score sort.** F085
   additionally bounds the score band (default 0.55, below the RRF head) and merges additively rather than
   relying on the K cap alone. The MAB team's displacement check (acceptance gate 2 below) remains the
   empirical arbiter of whether this is sufficient; `NOUS_KEYED_FACT_LEG_SCORE`/`_K` are the tuning knobs if
   it isn't.

---

## Acceptance (MAB-owned)

Per the implementation plan's Global Constraints, acceptance for R3/F085 is evaluated by the MAB team using
their own scripts, **outside this repository** — this repo's deliverable is the mechanism (migration,
canonicalizer, write-path emission, backfill, and the land-dark retrieval leg) plus unit/integration tests,
not the eval run itself. The four gates below are quoted verbatim from the MAB requirements doc
(`nous-r3-keyed-selection-requirements.md`, 2026-07-17, "Acceptance gates (in order; 1–2 are free)"):

1. **Ceiling simulation (zero LLM, zero nous-runtime):** re-run `scripts/probe_keyed_lookup_sim.py`
   (adapted to the entity-key index) on the re-keyed `nous_mab_wp` clone. **Gate: single-hop gold
   retrieval ≥ 0.80** (vs 0.41 today) with median candidate set ≤ ~10 facts. If the gate fails, iterate
   keying — build no retrieval code.
2. **Displacement check (free):** injected-candidate composition on a probe sample must show chunk-channel
   content NOT reduced when the keyed leg contributes (the bounded allotment requirement working).
3. **Decisive replay (≈7M tokens, only after 1–2 pass):** CR n=320 on the re-keyed clone, flag on vs
   published 0.725. Prediction to beat: the 0.713–0.725 noise band; the existence+selection thesis predicts
   single-hop → ~0.95.
4. **Regression replays** (AR eventqa slice, detective) — the keyed leg must not perturb non-CR retrieval.

(The referenced probe script and `nous_mab_wp` clone live with the MAB evaluation program, not in this
repository.)

## Gate-1 fixes (2026-07-18)

The MAB team's first Gate-1 (ceiling simulation, acceptance gate 1 above) run measured **0.48** against the
**≥0.80** bar — a shortfall traced not to the keying/indexing mechanism itself but to two store-time
corruption defects that destroyed or mis-adjudicated facts before R3.3's retrieval leg ever had a chance to
look them up (`nous-f085-gate1-fix-spec-1.md`, external to this repo). Both fixes ship together in one PR
(`feat/f085-gate1-fixes`) — neither is independently sufficient, since D2 without D1 would still let the
classifier's world-truth belief pick the wrong winner once the pair reaches the resolver, and D1 without D2
never sees the pairs D2 stops from being silently swallowed.

- **D1 — CONTRADICTION truth-bias.** The F027 supersession classifier's `current_fact` verdict (which claim it
  believes is factually correct) was deciding CONTRADICTION resolution at the three write/sweep sites
  (`_resolve_key_conflicts`, the `resolve_key_conflict_pair` sleep sweep, and the legacy
  `_supersede_by_subject` fall-through). A memory store must record what was *said*, not adjudicate what is
  *true* — a benchmark's later, deliberately-wrong correction was losing to an earlier, world-true statement.
  Measured impact: **21/160 gold facts destroyed** by the classifier overriding a later-stated (gold) value
  with an earlier-stated (world-true) one. Fixed by `_pick_contradiction_winner` (same-episode `source_ordinal`
  → `learned_at` → `None`/KEEP-BOTH+flag, never consulting the classifier's verdict) at the two write/sweep
  sites (`_resolve_key_conflicts`, the `resolve_key_conflict_pair` sleep sweep). The third site, the legacy
  `_supersede_by_subject` fall-through, has no ordinal/`learned_at` signal to order by (it only fires for
  unkeyed facts or R2-off deployments) — it does not call `_pick_contradiction_winner` at all and instead
  KEEP-BOTH+flags every CONTRADICTION unconditionally, replacing its prior silent supersede via a wrong-type
  `supersedes` edge. All three sites also got an unconditional prompt/schema debias (the classifier's
  `current_fact` field is now documented as advisory only for CONTRADICTION).
- **D2 — dedup blind-confirm swallow.** `_learn`'s near-duplicate check confirmed any hit at cosine similarity
  ≥ `fact_native_cosine_threshold` (0.95) before the same-slot conflict machinery ever saw it — a same-
  `(subject_key, attribute_key)` pair with a *different* value (e.g. "author is Alice" → "author is Bob") was
  swallowed as a duplicate-confirmation instead of being routed to conflict resolution. Measured impact:
  **~39pp of MAB answer-statement facts existence-destroyed** by this swallow. Fixed by
  `FactManager._same_slot_value_variant` (folded-content prefilter → band-budget gate → the F377
  `is_distinct_fact` tiebreaker), gating a new branch in `_learn`'s dedup logic that routes same-slot
  value-variants past confirm-as-duplicate and into insert. With R2 on, `_resolve_key_conflicts` adjudicates
  the routed pair immediately. **With R2 off, the sleep sweep does NOT recover it** —
  `_phase_sweep_key_conflicts` early-returns as a no-op when `supersession_key_resolution_enabled` is false —
  so resolution falls to the legacy `_supersede_by_subject` path, and only when the caller also set
  `input.subject`; otherwise the pair simply accumulates KEEP-BOTH, unflagged. This is fail-safe rather than
  fully recovered: both facts exist and are retrievable, they are just unmerged. Default prod deployment runs
  routing ON with R2 OFF (see CLAUDE.md) — this accumulation is the expected, documented steady state there;
  the MAB acceptance harness runs with R2 ON. Kill-switch: `NOUS_SAME_SLOT_CONFLICT_ROUTING_ENABLED` (default
  `true` — see CLAUDE.md).
- **Order-resolution contract** (documented, not further coded): same-episode ordinal is the robust
  resolution path, since enumerative extraction always stamps `source_ordinal` within an episode.
  Cross-episode conflicts fall back to `learned_at`, which equals true statement order only when ingestion
  order matches wall-clock write order. KEEP-BOTH (no ordering signal — identical timestamps) is the fail-safe
  and is expected to be rare in the live write path. A KEEP-BOTH pair never sets `superseded_by`, so it stays
  active and keeps re-surfacing to the sleep sweep on every later cycle; `_flag_contradiction_pair` is
  idempotent against repeat re-runs of the same pair (final review C1) so this does not compound the
  confidence decrement, but it also means the pair is never removed from future sweep pages — a durable,
  by-design KEEP-BOTH, not a leak.
- **Pre-existing, unchanged:** `_resolve_key_conflicts` still never surfaces a `ContradictionWarning` to the
  `learn()` caller (deferred, not introduced by this fix). Backfill re-run idempotency still holds via the
  folded-content confirm shortcut, except for paraphrase-drift below the cosine dedup threshold — a
  pre-existing risk this change does not worsen.
- **MAB repair sequence** (owned by the MAB team, outside this repo): re-run R1 (enumerative extraction
  backfill) → R2 rollback + re-run — `scripts/backfill_supersession.py` has no `--phase` flag; rollback is the
  manual `UPDATE`/`DELETE` SQL printed in its header, run against the printed `ROLLBACK KEY` watermark, then
  re-invoke `python scripts/backfill_supersession.py --agent-id <a> --classifier-budget 0` (D1/D2 changed how
  conflicts adjudicate, so a stale prior run must be undone first) → acceptance gate 1 (ceiling simulation,
  expect ≥0.80) → gate 2 (displacement check) → gate 3 (decisive CR n=320 replay). Note: that rollback SQL only
  deletes `supersedes`/`contradicts` edges and resets `superseded_by`/`active` within the watermark window — a
  KEEP-BOTH pair's `contradiction_of` value and the (post-C1, at-most-one) `-0.2` confidence decrement are
  accepted residue it does not undo.

## R3 v2 — Bounded Iterative Round (2026-07-19)

**Status:** Shipped, land-dark (`NOUS_KEYED_FACT_LEG_ROUNDS` default `1` — byte-identical to R3.3 above)
**Branch:** `feat/r3v2-iterative-keyed`

R3.3's keyed leg above is exact single-hop: a query mentioning an entity finds facts keyed on
that same entity, and nothing more. The MAB team's bounded-simulation work on the R3v2
requirements doc (`nous-r3v2-iterative-keyed-requirements.md` — external to this repository,
referenced by name in the implementation plan but not committed here) measured a **0.759** first
above-noise arm on multi-hop-driven queries (queries whose gold fact is not directly keyed by
anything in the query text, but IS reachable by hopping through a round-1 hit's own entity keys),
and a simulated round-2 ceiling of **0.02 → 0.44** once that hop is modeled — with the actual
bounded policy (guards, ranking, K2 cap) measuring **0.39 @ K2=8** in simulation, close enough to
the ceiling to justify shipping the mechanism.

### Mechanism

**Trigger:** `keyed_fact_leg_rounds >= 2` AND round 1 produced at least one hit
(`acc.keyed_results` non-empty). Rounds defaults `1` — the round-2 code path is a hard early-exit
at that guard, so no new behavior, DB round-trips, or log lines occur unless an operator
explicitly raises the setting.

**Key derivation** (`nous/api/retrieval_pipeline.py`, Stage 1.6, inside `_run_stages`) — order
matters because the fan-out cap (below) truncates, so what fills the list FIRST determines what
survives:

1. The round-1 hits' own `fact_entity_keys` rows (`FactManager.entity_keys_for_facts`), in
   round-1 rank order, alphabetical within a fact — cheap, exact, and unconditional (this step is
   NOT itself capped by `keyed_fact_leg_r2_max_keys`).
2. A vocab-filtered scan of the round-1 hits' CONTENT via `extract_entity_candidates` — covers
   entities mentioned in a hit's content but not indexed as one of its own entity keys. Only keys
   that are ALSO members of the agent's entity-key vocabulary may enter the round-2 key set
   (`k in vocab`); the raw extractor's quoted-span and capitalized-span legs emit arbitrary
   non-indexed spans first, and unfiltered they would eat the entire key cap with candidates that
   match nothing in the database — silently dropping round-2 coverage below what was simulated.
   A shared early-break budget applies across ALL round-1 hits' content scans (not per-row), so
   the cap is a single fan-out ceiling, not `max_keys` per hit.

Both steps are MINUS the round-1 query's own keys (`seen_k = set(candidates)`), so a key that
already fired round 1 cannot re-derive itself into round 2.

**Fan-out guards** — `keyed_fact_leg_r2_max_keys` (default 32) caps the combined key set from
both derivation steps; `keyed_fact_leg_r2_max_candidates` (default 256) caps the candidate rows
`fetch_by_entity_keys` returns before ranking. Truncation at either cap sets
`PipelineStats.keyed_r2_truncated = True` — deliberately "possibly truncated" semantics: landing
exactly ON the candidate `LIMIT` is indistinguishable from "there were more beyond it", so the
flag fires either way and the `keyed_r2:` log line's separate counts (below) disambiguate for
anyone inspecting a specific run.

**THE ranking policy** (`_rank_r2_candidates` — **sim-parity contract: any change to this sort
key requires MAB re-simulation of the shipped policy before the gate-3 decisive replay**;
"already green" from the original bounded simulation does NOT transfer to a modified policy):

```
sort key = (
    -attribute_key_word_overlap_with_query,
    -content_word_overlap_with_query,
    -learned_at.timestamp(),
    str(id),                      # final tie-break, full determinism
)
```

Both overlaps fold identically: `normalize_key(field, max_len=1000).split()` intersected with the
same-folded query tokens, counted. The `str(id)` tie-break is the one documented liberty beyond
the three criteria the spec states explicitly — MAB should confirm their simulation's own
tie-handling matches, or re-simulate, before treating gate 1 as satisfied for this shipped policy.

Round-2 candidates are fetched via `fetch_by_entity_keys(..., track=False)` — untracked, since a
bulk candidate pool that gets filtered down by ranking/K2 before ever surfacing is not itself a
retrieval-leg result (mirrors the existing dedup-probe rationale in R3.3 above). Only the
top-`keyed_fact_leg_k2` survivors get `track_access()`, after ranking.

### Band derivation

Round-2 hits score in a band derived from round-1's own floor, not a fixed offset:

```
base  = max(0.0, keyed_fact_leg_score - 0.005 * (keyed_fact_leg_k + 1))
score = max(0.0, base - 0.005 * rank)
```

This sits **two decay steps** under round-1's worst-ranked hit (`k + 1`, not `k - 1`) as a
deliberate safety margin. Deriving from round-1's actual floor (rather than a fixed sub-band)
keeps the guarantee config-proof: raising `keyed_fact_leg_k` moves round-1's own floor down, and
the round-2 band automatically follows it down too, so round-2 can never outscore round-1
regardless of how the operator has configured K. The `max(0.0, ...)` clamp is an accepted
degenerate edge — an operator who has set `keyed_fact_leg_score` near 0 has, in effect, already
disabled the whole keyed leg.

### Provenance / telemetry

Round-2 hits carry `metadata["retrieval_leg"] = "keyed_r2"` (round-1 hits keep `"keyed"`), with
the same `matched_keys`/`subject`/`event_date`/`source_episode_id` conventions as round 1's
`_keyed_to_pipeline`. Two log lines surface the round:

- A dedicated `logger.info` at assembly time, fired only when round 2 actually ran (whether or
  not it found anything): `keyed_r2: r1_hits=%d keys_examined=%d candidates=%d selected=%d
  truncated=%s`. `candidates=` is the `fetch_by_entity_keys` result size — since codex round 5,
  round-1 ids are excluded IN THE SQL FETCH itself (`exclude_fact_ids`, cast to `uuid[]`), so this
  count is already r1-free by construction, not merely "before a later Python-side filter" as it
  was pre-round-5. `selected=` (the assembly-time K2 survivor count, after cross-leg + F071
  filtering) is the only count that can't be known until assembly.
- The existing consolidated `recall_deep` INFO line (`nous/api/tools.py`) gains
  `n_keyed_r2=%d keyed_r2_truncated=%s`, appended for grep parity with the round-1 fields already
  there.

The `rounds=1` (default) byte-identity invariant covers RESULTS only (pinned by
`test_rounds_1_default_byte_identical` + the untouched `recall_deep` text snapshot) — it does NOT
cover log text. The consolidated `recall_deep` line legitimately gains two fields on every call
regardless of the flag, since it only ever reads `stats.n_keyed_r2`/`stats.keyed_r2_truncated`,
which default to `0`/`False` when round 2 never ran.

### Flags

| Env Var | Default | Description |
|---------|---------|-------------|
| `NOUS_KEYED_FACT_LEG_ROUNDS` | `1` | Keyed-leg retrieval rounds. `1` = v1 behavior (byte-identical); `2` enables the bounded iterative round. |
| `NOUS_KEYED_FACT_LEG_K2` | `8` | Round-2 allotment — max round-2 keyed facts merged per query. |
| `NOUS_KEYED_FACT_LEG_R2_MAX_KEYS` | `32` | Fan-out guard — max round-2 keys examined (truncation counted, never silent). |
| `NOUS_KEYED_FACT_LEG_R2_MAX_CANDIDATES` | `256` | Fan-out guard — hard cap on round-2 candidates fetched before ranking. |

### Acceptance (MAB-owned, external to this repository)

As with R3.3 above, acceptance runs in the MAB team's own harness, outside this repository — this
repo's deliverable is the mechanism plus unit/integration tests, not the eval run itself. The four
gates below are quoted verbatim from the MAB R3v2 requirements doc
(`nous-r3v2-iterative-keyed-requirements.md`, 2026-07-19, "Acceptance gates (in cost order; 1–2
are free)"):

1. **Bounded-policy simulation (zero LLM), already green on the eval clone:** mh gold coverage
   ≥ **0.35 @ K2=8** (measured 0.39) with the fan-out guards ON. Implementation must reproduce
   ≥ this bar on `nous_mab_wp` before any live code review completes — if the shipped ranking
   differs from the simulated one, re-simulate first, build second.
   *(This repo's note: the shipped policy carries three specifics MAB must confirm or re-simulate —
   key-derivation order, the vocab filter, and the `str(id)` tie-break; see Documented Deviations.
   "Already green" from the original simulation does not transfer until that check happens.)*
2. **Displacement check (free):** candidate-pool composition on a probe sample — chunk-channel
   content unchanged when round-2 contributes; round-2 never evicts round-1 or direct hits (band
   ordering working).
   *(This repo's note: the non-eviction guarantee is POOL-COMPOSITION/POSITIONAL. Under
   `rerank_by_score=True` with the recency resolver enabled, a dated superseded round-1 fact can
   be ×0.3-down-ranked below the round-2 band in FINAL ordering — that is the resolver acting on
   the fact's own supersession state, orthogonal to round 2; expected, not eviction.)*
3. **Decisive replay (≈7M tokens):** CR n=320, rounds=2 vs the 0.759 rounds=1 result on the same
   repaired clone. Prediction to beat: mh 0.619; the bounded sim ceiling implies mh headroom to
   ~0.70 if conversion tracks the sh precedent. Aggregate prediction ~0.78 ± 0.02.
4. **Non-CR regression:** nous-side retrieval suite rounds=1 vs rounds=2 (the eval's AR/LRU agents
   carry no entity keys; that gate remains nous-side, as in v1). rounds=1 stays byte-identical by
   construction (see the byte-identity invariant above).

### Documented Deviations (R3v2, in addition to R3.3's list above)

1. **`PipelineOutcome` → `PipelineStats`.** The implementation's return-value naming is
   `PipelineStats` (matching the rest of this pipeline's existing naming), not whatever name the
   external spec used.
2. **`attribute_key` added to the `fetch_by_entity_keys` SELECT.** The spec's ranking policy
   assumes `attribute_key` is already available on the fetched row; Task 1 added it (and
   `subject_key`) to the SELECT/GROUP BY as additive columns.
3. **Band derived from round-1's floor, not a fixed sub-band.** See Band derivation above —
   chosen for config-proofness under an operator-raised `keyed_fact_leg_k`.
4. **Round-2 key derivation covers BOTH spec sentences.** The spec describes round-2 keys as
   entities associated with round-1 hits in two ways (indexed entity rows, and content mentions);
   this implementation does both — entity rows preferred (step 1), vocab-filtered content scan
   supplementing (step 2) — rather than picking one interpretation.
5. **Key-derivation order, the vocab filter, and the `id` tie-break are the sim-parity items.**
   These three specifics are exactly what MAB's re-simulation (gate 1 above) needs to confirm
   against the shipped policy — see THE ranking policy above for the full statement.
6. **Byte-identity covers results, not logs.** See Provenance / telemetry above.
7. **SQL-layer `f.id` tie-break added to `fetch_by_entity_keys`'s `ORDER BY` (final review).**
   `learned_at` is a server-default timestamp — on a bulk-backfilled corpus every row in a batch
   can share the identical transaction-constant value, so without a final deterministic column
   WHICH rows survive the `LIMIT` (round 1's 8, round 2's 256) was Postgres-unstable across runs.
   R3v2's key derivation and MAB's gate-1 re-simulation both lean on candidate-set determinism.
   This deliberately touches round 1's fetch too — only previously-tied rows can reorder, so it
   is not a byte-identity violation (pinned by the `recall_deep` snapshot test). MAB's
   re-simulation of the shipped policy should mirror this same total order.

## Non-goals

- **Multi-hop entity reasoning.** No pronoun/co-reference resolution across facts — v1 is exact single-hop
  key match only.
- **LLM entity linking in the read path.** Query-side NER-lite (`extract_entity_candidates`) is regex +
  vocab-membership only; no LLM call at query time, to keep the leg's latency and cost bounded.
- **Scalar keys.** Numbers, dates, colors, and common nouns are deliberately never indexed (the R3.1
  stop-policy) — a corpus needing "find the fact whose value is exactly 42" is out of scope for this
  mechanism.
- **Chunk-store changes.** `heart.episode_chunks` (F067) is untouched; this feature only reaches
  `heart.facts`.
- **Prod enablement.** All 5 flags ship default OFF. Flipping any of them in production requires the
  external MAB acceptance gates above to pass first, per the same land-dark discipline F084 established.

## Accepted v1 Risks (documented, not coded around)

- **Giant-bucket sort cost.** A single wildly over-shared entity key (e.g. a common word that slips past the
  stop-policy) could accumulate a large `fetch_by_entity_keys` candidate set before the `LIMIT`/ranking
  applies. Bounded in practice by the stop-policy, `NOUS_KEYED_FACT_LEG_K`, and corpus size; not separately
  capped.
- **Vocab TTL staleness window.** `entity_key_vocabulary` is cached 300s per `Heart` instance — a newly
  written entity key is invisible to the vocab-matching NER-lite leg for up to 5 minutes. Quoted-span and
  capitalized-span matching are unaffected (they don't consult the vocab).
- **`_SCALAR_STOP` is hardcoded**, not env-configurable — `NOUS_ENTITY_KEY_MIN_CHARS` is the only stop-policy
  knob exposed, a partial meet of the requirements text's "configurable stop-policy" ask.
- **Eval harness vocab-cache no-op.** The retrieval eval harness constructs a fresh `Heart` per config, so the
  300s vocab cache never actually caches anything there — harmless (every call just re-queries), but worth
  knowing when reading eval timing numbers.
