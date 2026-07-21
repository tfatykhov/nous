# F086 — ICL Exemplar Mode (Exemplar-Gathering Retrieval)

**Status:** Shipped, land-dark (both flags default OFF — write `NOUS_EXEMPLAR_EXTRACTION_ENABLED`, read
`NOUS_EXEMPLAR_MODE_ENABLED`)
**Migration:** `066_f086_exemplar_indexes.sql` (index-only, no new columns/tables)
**Branch:** `feat/icl-exemplar-mode`

---

## Why

The MAB evaluation program's sole decidable loss versus the 2026 field is in-context-learning (ICL)
classification: **live 0.555 vs the leader's 0.840** (200 questions, 5 sources — banking77, clinic150, nlu,
trec_coarse, trec_fine; corrected from an earlier double-counted 0.571). Live transcripts show the failure
shape directly: the agent finds *a* similar stored example over chunk retrieval and copies its label
("Found the direct match → label: 67", gold 28).

The MAB team's own zero-LLM simulations on the persisted eval agents (`probe_icl_exemplar_knn.py`,
`probe_icl_exemplar_emb.py`) established that storage is not the bottleneck and that a deterministic rule
already closes almost the entire gap:

- Lexical (Jaccard) 1-NN over all stored exemplars: **0.67** (beats live, p=0.018).
- **Embedding kNN (text-embedding-3-large @1536, exemplar granularity): 1-NN 0.76, majority-vote@5 = 0.82,
  strict-plurality@25 = 0.81, gold-in-top25 = 0.99.** Paired vs live: +70/−17, p=8×10⁻⁹.

The live system loses 26pp not to storage or model capability but to **retrieval granularity**: a chunk
buries ~40 exemplars, so chunk-level search returns *a* similar region, not the *k nearest labeled
examples*. F086 closes that granularity gap: store each `utterance\nlabel: N` pair as its own embedded
`heart.facts` row (write path), and add a land-dark retrieval leg that fetches the K nearest labeled
examples by cosine and injects them as evidence, not instruction (read path).

---

## Two Falsified Spec Assumptions (and their resolutions)

The original requirements sketch made two assumptions that validation during implementation disproved.
Both are corrected in the shipped code, not worked around.

1. **"A cheap regex/heuristic classifier at ingest, analogous to the R1 enumerative-density heuristic."**
   F084's `is_enumerable` (the R1 heuristic) does **not** fire on `utterance\nlabel: N` streams — its
   regexes are shaped for enumerable prose/tables, and label lines fail both of them. Exemplar detection
   needed its **own** predicate (`nous/heart/exemplars.py::is_exemplar_stream` /
   `exemplar_density`), checked *before* R1 in the `FactExtractor.extract_and_store` routing seam so the
   two modal paths never compete for the same transcript.
2. **Backfill can read `episodes.transcript`.** Transcript capture at capture time is capped at
   `NOUS_TRANSCRIPT_MAX_CHARS` (8000 chars) — but the exemplar streams these episodes carry run to
   ~400k characters. `episodes.transcript` truncates them into uselessness for backfill purposes. The
   validated finding: `heart.episode_chunks` (F067) persists the streams essentially losslessly
   (0.2–2.0% loss, chunk-boundary splits only). `scripts/backfill_exemplar_facts.py` therefore reads
   `heart.episode_chunks`, not `episodes.transcript` — see the Backfill Runbook below for how chunk
   boundaries are handled without losing cross-chunk ordinal continuity.

---

## Fact Shape (Gate-1 Sim Parity — binding)

`content = f"{utterance}\nlabel: {label}"` — the full pair text, **exactly** what the MAB embedding-kNN
sim (maj@5 0.82, text-embedding-3-large@1536) embedded. `subject_key=None` on every exemplar fact (keeps
F084/F085's same-slot conflict routing and entity-key emission fully short-circuited — both require both
keys / fire on entity presence, and an exemplar fact has neither), `attribute_key="label"` (a marker only,
not a real conflict-routing key), `source="exemplar_extractor"`, `source_ordinal` = the pair's position
within its episode, `entity_extraction_complete=True` (so F085's backfill never Haiku-sweeps exemplar
facts looking for entities that were never meant to exist there).

**Any deviation from this exact content shape invalidates the pre-green Gate 1** (the embedding-kNN sim
was computed against this exact string, not a summary or a reformatted version of it).

---

## Write Path

`FactExtractor.extract_and_store` checks `is_exemplar_stream(transcript, exemplar_density_threshold)`
**before** the R1 enumerative branch. If it fires, `nous/handlers/exemplar_ingest.py::ingest_exemplars`
parses the transcript with `nous/heart/exemplars.py::parse_exemplars` (pure, zero-LLM), caps at
`exemplar_max_per_episode` with a loud WARNING on truncation, batch-embeds every pair's content, and
stores each pair via `Heart.learn(fact_input, precomputed_embedding=vec)`. This is **modal, not
additive**: when the exemplar leg stores anything, the legacy summary-derived candidate facts for that
transcript are *not* also stored — the transcript routes to exactly one path.

Five guards keep exemplar facts from tripping machinery meant for narrative facts (four at write time, one
at sleep — the background-machinery family is supersession, contradiction scan, band-classify,
actionability, and now the sleep sweep):

- **Source-aware min-content floor** (`nous/heart/facts.py`, both enforcement sites): exemplar facts use
  `exemplar_min_content_chars` (default 5) instead of the global 30-char floor — a labeled pair like
  `"yes\nlabel: 1"` is well under 30 chars but a completely valid exemplar.
- **Admission bypass** (`nous/heart/admission.py::bypass_sources`): exemplar facts skip utility/novelty
  scoring for the same reason enumerative facts do — near-identical banking-style utterances with
  different labels are the *point*, not low-novelty noise an admission scorer should reject.
- **Label-aware dedup guard** (`nous/heart/facts.py`): the near-duplicate check that would otherwise
  confirm a near-identical utterance as a duplicate is bypassed whenever the candidate's `parse_label(...)`
  differs from the incoming fact's label — **different-label near-duplicates must never dedup-drop**,
  since two banking utterances that are nearly identical text but carry different labels are exactly the
  discriminative signal exemplar retrieval needs. The guard is **two-sided**: it fires when *either* the
  incoming input *or* its nearest stored dupe is an exemplar. Without the dupe-side term, a genuine
  conversational fact whose nearest neighbor happens to be an exemplar row (labels differ —
  `parse_label` returns `None` for label-less content) would dedup-confirm *into* that exemplar and be
  lost.
- **Conflict/contradiction/actionability exemptions** (`nous/heart/facts.py`, one local
  `is_exemplar = input.source == "exemplar_extractor"` in `_learn`): exemplar facts bypass the legacy
  subject-supersession pass, the post-insert `_find_contradiction` scan + domain-compaction check, and the
  F047 actionability classifier (hard-set `actionable=False`). Exemplars carry an intentionally identical
  `subject` and `subject_key=None`, so without these gates the legacy subject-supersession would deactivate
  different-label pairs (destroying exactly the discriminative signal), and the tier-3 Haiku actionability
  classifier would fire **per exemplar fact** (up to `exemplar_max_per_episode`) — the latter is what keeps
  the write path genuinely **zero-LLM**, not just zero-LLM at parse time. A same-label near-duplicate that
  survives the label-guard confirms directly (no F027 band classifier — another per-fact LLM call avoided).
- **Sleep F031 sweep exclusion** (`nous/heart/facts.py::_find_contradiction_candidates`, codex r7 — the
  fifth, sleep-side member): the background contradiction sweep's same-subject candidate query would
  otherwise re-discover same-utterance/different-label exemplar pairs (they share `subject = utterance[:200]`
  and land in F031's 0.75–0.95 band), letting the sleep resolver supersede/merge label variants the
  write-time guards preserved. The query now excludes `source = 'exemplar_extractor'` on **both** sides
  (`IS DISTINCT FROM`, so NULL-source normal facts stay in scope) — an exemplar may neither be resolved nor
  serve as the resolving counterpart against a normal fact. This closes the previously-accepted residual
  from the sleep side.

**Codex r3 — an exemplar is never persisted without an embedding.** `_embed_and_store_pairs` batch-embeds
all pairs; on a batch miss it retries once per-pair (`embedder.embed(content)`), and if the vector is
*still* `None` (or there is no embedder) it **skips** the pair rather than hand `precomputed_embedding=None`
to `heart.learn`. A NULL-embedding exemplar would be invisible to both `fetch_exemplars_by_vector`
(`embedding IS NOT NULL`) *and* cosine dedup, so a backfill rerun would silently duplicate it. Skips are
counted (`skipped_no_embedding`), logged as a loud WARNING, and surfaced in the ingest INFO line and the
backfill summary — never silent.

---

## Read Path — Stage 1.7 Exemplar Leg

`run_recall_pipeline` gates the leg on `exemplar_mode_enabled` AND a cheap trigger heuristic AND a
TTL-cached exists-probe (`FactManager.has_exemplars()`), in that order, so the common "flag off" and
"empty store" cases never pay for anything past a boolean check.

### Trigger heuristic (and its trec rationale)

```python
_MEMORY_VERB = r"(?:say|said|tell|told|mention(?:ed)?|ask(?:ed)?|discuss(?:ed)?|talk(?:ed)?\s+about)"
_MEMORY_REFERENTIAL = re.compile(
    r"\b(?:"
    r"(?:did\s+(?:i|we|you)|what\s+did|what\s+have\s+(?:i|we))(?:\s+\w+){0,4}?\s+" + _MEMORY_VERB
    + r"|remind me|last time|earlier|previous(?:ly)?"
    r"|we (?:discussed|talked)|you (?:said|told|mentioned)"
    r")\b", re.IGNORECASE)

def _is_classification_shaped(query: str, max_words: int) -> bool:
    words = query.split()
    return 0 < len(words) <= max_words and not _MEMORY_REFERENTIAL.search(query)
```

**The trigger must NOT exclude questions generally** — trec-style classification queries (e.g. "what is
the capital of france") ARE questions, and trec_coarse's live accuracy is already 0.90 on parametric
knowledge alone, so a naive "no interrogatives" gate would silence the leg on exactly the source where it
has the least to prove and the most headroom elsewhere. Only *memory-referential* interrogatives — "what
did I say about...", "remind me...", "last time we discussed..." — are excluded, because those are asking
the agent to recall its own conversation history, not to classify an utterance against stored exemplars.
Long queries (`> exemplar_max_query_words`, default 64) are also excluded — classification-shaped
utterances are short by construction.

**Codex r2 — the ambiguous prefixes require a memory verb.** `did (i|we|you)`, `what did`, and
`what have (i|we)` are *not* memory-referential on their own: banking77 carries ordinary
classification-shaped questions like *"did I get charged twice"* / *"did I make a cash withdrawal"* that
would have been wrongly blocked (the spec-review M2 risk, materialized). Those three prefixes now only
match when followed within a few words by an actual stored-memory verb — say/tell/mention/ask/discuss/
talk about — so a bare past-tense banking question stays classification-shaped and still triggers the
leg, while *"what did I **say** about my card"* / *"did you **mention** the deadline"* remain excluded.
The standalone phrases (remind me, last time, earlier, previous(ly), we discussed/talked, you
said/told/mentioned) still match on their own.

### Similarity floor (Gate-2 mechanism)

`exemplar_min_similarity` (default 0.30) is the concrete mechanism behind acceptance Gate 2 (see below):
"non-exemplar retrieval unchanged when the mode triggers falsely." A false trigger (heuristic fires but the
query isn't actually classification-shaped) still runs `fetch_exemplars_by_vector`, but hits below the
floor are dropped before merge — bounding how often an irrelevant exemplar can displace a slot at all. The
second half of the non-displacement guarantee is the merge itself: hits are converted to `PipelineResult`s
scored in a band under `exemplar_leg_score` (default 0.55, per-rank decay 0.005) and inserted at their
**sorted position** (never tail-appended), so a genuine merge can only ever *add* a row, never reorder or
evict an existing higher-scoring result.

### Embedding-parity operational constraint

Query↔exemplar cosine similarity is only meaningful when the deployment's embedding configuration matches
the one the sim was computed against: **text-embedding-3-large @ 1536 dimensions**. This is a standing
operational constraint, not something the code enforces — the mechanism is config-driven (whatever
`EMBEDDING_MODEL`/`EMBEDDING_DIMENSIONS` the deployment runs), so a deployment running a different
embedding model or dimensionality is not measuring the same retrieval quality the sim (and therefore Gate
1) validated. State this in the PR body alongside this doc whenever the read flag is proposed for
flipping.

### Stage-1 routing — exemplars appear ONLY in the examples block (codex r1)

Stage 1 (ordinary fact recall) sees exemplar facts as plain facts — they carry no `retrieval_leg` tag —
so a query that surfaces an exemplar through normal vector/FTS recall would render it under **Heart
Memory** without the inform-not-force framing. To keep the two presentations coherent, **when the leg
actually fires** (all trigger gates passed, `has_exemplars()` true, query embedded), Stage 1.7:

1. Fetches the K nearest exemplars (`fetch_exemplars_by_vector`) and applies the similarity floor. Call
   the survivors the **post-floor hit set**. **Codex r3:** those survivors get `track_access` called on
   them (recall_count / last_recalled_at) — retrieval == access, so an actively-retrieved exemplar is not
   later reaped by `stale_scan`. Only survivors are tracked; below-floor hits are not. This mirrors the
   keyed_r2 survivors-only, sync-await precedent in assembly.
2. Batch-identifies which already-surfaced Stage-1 fact ids are exemplar-source
   (`FactManager.exemplar_ids_among`, one agent-scoped
   `SELECT id … WHERE source='exemplar_extractor' AND id = ANY(...)`).
3. **Removes from Heart Memory only** the exemplar-source Stage-1 rows whose id is in the post-floor hit
   set — i.e. only rows whose replacement in the examples block is guaranteed. Those hits come back
   through the leg with banded score + label/similarity metadata.

**Codex r2 — strip only after a successful fetch, and only what is replaced.** The strip happens *after*
step 1, never before, so a `fetch_exemplars_by_vector` that raises (caught, non-fatal) or an
all-below-floor result leaves `acc.heart_results` **untouched** — a non-fatal leg error can never delete
an earlier successful Stage-1 result. Two edge cases follow directly and are **by design**:

- A Stage-1 exemplar that is **not** in the fetched top-K (or falls below the floor) **stays in Heart
  Memory** as an ordinary fact — it is below the leg's relevance bar, so it is not force-injected into
  the examples block either.
- On fetch failure the leg records a stage error and every Stage-1 exemplar stays put (the leg-not-fired
  fallback).

Net effect: **mode on + trigger met + fetch succeeds ⇒ an exemplar fact that the leg surfaces appears
exactly once, only in the `=== Nearest stored examples ===` block, never doubled into Heart Memory.**
When the leg does **not** fire (read flag off, or mode-on but the trigger heuristic is unmet), Stage-1
results are touched by nothing — the flag-off byte-identity and the write-on/read-off land-dark contract
below hold exactly as documented. (Assembly-side dedup remains as belt-and-suspenders for the rare case
where a *non-exemplar* Stage-1 fact shares an id with a returned hit — `test_dedup_against_existing_results`.)

### Do-not-filter-leakage note (spec-review M4)

The MAB team's 0.82 embedding-kNN sim was measured **with** ~7–8/200 (~4pp) instances of question-text
leakage already present in the stored exemplar corpus. **Do not add a "cleanup" pass that filters this
leakage** — doing so would change the corpus the sim was computed against and break Gate-1 parity. This is
a one-line note precisely so nobody "fixes" it later under the impression it's a bug.

---

## Land-Dark Contract (arch-review M3)

**Byte-identical behavior holds only when BOTH flags are off.** `exemplar_extraction_enabled=false` +
`exemplar_mode_enabled=false` is the byte-identical configuration, pinned by:

- `test_extractor_flag_off_exemplar_leg_never_runs` (write path — legacy candidate extraction runs
  unchanged, `ingest_exemplars` is never even imported into the call).
- `test_flag_off_byte_identical` (read path — `has_exemplars` is never called, `PipelineStats.n_exemplar
  == 0`, no `retrieval_leg == "exemplar"` rows in results).
- `test_no_exemplars_no_block` (rendering — no `=== Nearest stored examples ===` section emitted).

**Write ON / read OFF is NOT byte-identical to baseline and must not be run in an A/B-compared corpus.**
With the write flag on and the read flag off, exemplar facts still get stored (`source='exemplar_extractor'`)
but no read-path logic filters by source — they surface through ordinary Stage-1 fact recall like any other
fact, since nothing in the read-OFF path excludes `source='exemplar_extractor'` rows (the Stage-1 routing
above only runs when the *read* flag is on **and** the trigger fires). An A/B comparison that flips only the
write flag is silently comparing two different fact-store contents, not isolating the read-path mechanism.

---

## Acceptance Gates (quoted verbatim from the MAB requirements doc)

Per the plan's Global Constraints, gates 1–4 below are quoted **verbatim** from
`nousiclexemplarmoderequirements.md` (2026-07-19), "Acceptance gates (cost order)". They run **externally**
in the MAB evaluation program's own harness — this repository's deliverable is the mechanism plus
unit/integration tests, not the eval run itself.

> 1. **Free, already green:** the embedding-kNN sim above IS gate 1 (maj@5 0.82 ≥ bar 0.75 on
>    the persisted eval agents). If the shipped index/normalization differs from the sim
>    (different embedding model/dims, different parsing), re-run the sim against the
>    implementation's own index before building the read path.
> 2. **Free:** displacement check — non-exemplar retrieval unchanged when the mode triggers
>    falsely (bounded block, band ordering).
> 3. **Paid, decisive:** TTL ICL n=200 replay on the persisted agents, mode on vs corrected
>    0.555. Prediction: 0.75–0.85 (sim 0.82 ± LLM-reader effects, which program precedent
>    says are net-positive). trec_fine will lag (0.62 sim ceiling; 50 fine labels).
> 4. nous-side regression on non-ICL workloads (mode dark → byte-identical; mode on →
>    trigger-precision check).

### Gate-1 re-sim clause — open external precondition for MAB (spec-review C1)

Gate 1's own text above contains a conditional clause that is an **open precondition**, not something this
repository can satisfy on MAB's behalf: *"if the shipped index/normalization differs from the sim …
re-run the sim against the implementation's own index before building the read path."* Three things could
differ between the sim and the shipped implementation, and MAB must check each before treating Gate 1 as
satisfied for the shipped code:

- **(a) Parser normalization** — the shipped parser strips `User:`/skips `Assistant:` transcript prefixes.
  This is provably inert on clean `utterance\nlabel: N` chunks (pinned by
  `test_parse_transcript_skips_assistant_and_strips_user_prefix` and the pure-stream tests in
  `TestExemplarParser`), but MAB should confirm the corpus their sim ran against doesn't carry some other
  prefix shape this parser doesn't normalize.
- **(b) Embedding model/dims** — see the Embedding-parity constraint above.
- **(c) The shipped, source-filtered index itself** — `fetch_exemplars_by_vector` queries a partial HNSW
  index filtered to `source = 'exemplar_extractor'` (migration 066). If the sim's own kNN implementation
  used a different similarity metric, `ef_search` setting, or candidate pool, re-run
  `probe_icl_exemplar_emb.py` against the shipped `source='exemplar_extractor'` rows directly and confirm
  maj@5 ≥ 0.75 before flipping `NOUS_EXEMPLAR_MODE_ENABLED`.

### Gate-4 composite note (spec-review M3)

Gate 4's "mode dark → byte-identical" clause is not one test but a **composite** of three, each covering a
different layer of the pipeline:

1. **Formatter snapshot** — `test_no_exemplars_no_block` (Task 5) + the pre-existing `recall_deep` text
   snapshot test, proving the rendered text is unchanged when no exemplar rows exist.
2. **Read-results identity test** — `test_flag_off_byte_identical` (Task 4, `TestExemplarLeg`), proving
   `run_recall_pipeline`'s structured results and stats are unchanged when the read flag is off (not just
   that the *text* looks the same).
3. **Write mock-not-called test** — `test_extractor_flag_off_exemplar_leg_never_runs` (Task 2), proving the
   write path never even imports/calls `ingest_exemplars` when the write flag is off.

All three must stay green for the "mode dark → byte-identical" half of Gate 4 to hold; the "mode on →
trigger-precision check" half is the pre-gate-3 blocklist scan described next.

### Pre-Gate-3 blocklist scan item for MAB (spec-review M2)

Before running Gate 3 (the paid, decisive n=200 replay), **MAB should scan all 5 sources' gold queries
against `_MEMORY_REFERENTIAL`** (the trigger heuristic's blocklist regex, quoted above) and check for false
positives — a gold query that happens to match the blocklist would silently skip the exemplar leg for that
question, understating gate-3's measured effect. The **banking77** highest-risk case — conversational
banking questions shaped like *"did I get charged twice for this transaction?"* — is now addressed
**in-pattern** (codex r2: the `did (i|we|you)` / `what did` / `what have (i|we)` prefixes require a
stored-memory verb, so a bare past-tense banking question no longer matches; pinned by
`TestClassificationShapedTrigger`). The gold-query scan nonetheless **stays as the verification step**:
if any gold query in any of the 5 sources still matches, the blocklist patterns need further narrowing
before Gate 3 is run, or the measured effect will be an underestimate.

---

## Backfill Runbook: `scripts/backfill_exemplar_facts.py`

```
python scripts/backfill_exemplar_facts.py --agent-id nous-default [--dry-run] \
  [--max-episodes N] [--since YYYY-MM-DD] [--density-threshold 0.8] \
  [--source-kinds dialogue] [--log-level INFO]

python scripts/backfill_exemplar_facts.py --agent-id nous-default \
  --phase rollback --manifest <path> [--dry-run]                      # preferred: exact
python scripts/backfill_exemplar_facts.py --agent-id nous-default \
  --phase rollback --watermark <iso-ts> [--dry-run] [--include-live-writes]   # fallback
```

**Reads `heart.episode_chunks`, not `episodes.transcript`** (see Falsified Assumption #2 above), grouped
by `episode_id` and ordered by `chunk_index`. Three pure, unit-tested functions do the assembly:

1. **`group_chunks_by_episode`** — groups a flat chunk-row result set into `{episode_id: [content, ...]}`,
   contents ordered by `chunk_index` regardless of the input row order.
2. **`episode_qualifies`** — runs `is_exemplar_stream` on the episode's **concatenated** chunk text, not
   per-chunk. A lone chunk can look diluted at a boundary split (e.g. half a pair on either side of the
   split) even when the whole episode is a genuine, high-density exemplar stream — qualification has to
   see the whole episode to judge it correctly.
3. **`build_episode_pairs`** — parses **each chunk independently** via `parse_exemplars` (chunk-boundary
   fragments — an utterance split mid-text — simply drop their label-less half; parse_exemplars already
   skips label-less utterances, and the MAB measurement itself found such fragments harmless to ranking),
   then **re-stamps every pair's ordinal with a running per-episode offset** so `source_ordinal` is one
   continuous sequence across the whole episode, never reset at a chunk boundary.

**Dialogue-only by default (`--source-kinds`, codex r5).** The chunk query reads **only**
`source_kind = 'dialogue'` chunks by default. `heart.episode_chunks` also holds F069 `ingest_document`
bodies and F024 attachment files as `document` (and reserves `code`) chunks (migration 052 CHECK set:
`dialogue`/`document`/`code`), and a document that happens to contain `label:` lines would otherwise
qualify an episode and pollute the ICL exemplar corpus with non-dialogue content. Old rows default
`dialogue`, so the default exactly matches the transcript-backfill contract. `--source-kinds`
(comma-separated, validated against the CHECK set) is the escape hatch: pass `--source-kinds
dialogue,document` **only when you deliberately mean** to backfill an attachment/document corpus (F024
attachment-ingested exemplar *files* land as `document` chunks) — the widening must be an explicit
operator choice, never an accident.

The live-mode store loop (`_store_episode_pairs`) is a thin wrapper around the **shared**
`nous/handlers/exemplar_ingest.py::_embed_and_store_pairs` helper — the one cap+embed+learn
implementation, also used by `ingest_exemplars` (the live write path). The two callers differ only in *how*
the pair list gets assembled: `ingest_exemplars` parses one transcript in a single `parse_exemplars` call
(ordinals always start at 0); the backfill assembles pairs across an episode's chunks with continuing
ordinals via `build_episode_pairs` *before* handing the already-ordinaled list to the shared helper. The
backfill deliberately does **not** call `ingest_exemplars` per chunk — that would reset each chunk's
ordinals back to 0 and violate ordinal continuation. The episode-level cap (`exemplar_max_per_episode`) is
applied inside `_embed_and_store_pairs` on the whole, already-concatenated pair list, with the same
loud-truncation-WARNING convention on both paths.

**Idempotency** relies on the same mechanism as the live write path: re-running re-embeds identical
content, and `Heart.learn`'s native-cosine dedup (0.95 threshold) confirms rather than duplicates — except
the label-aware guard never drops a different-label near-duplicate, which is exactly the correct re-run
behavior (a genuinely new label for previously-seen text must still be stored).

**SMOKE TEST FIRST:** `--dry-run`, then `--max-episodes 2`, before a full live run (threshold-yield
discipline — a density threshold that behaves as expected on a 2-episode sample can still surprise at
corpus scale).

### Rollback: manifest (preferred, exact) vs watermark (fallback) — codex r8

Each live backfill prints **two** rollback handles up-front, before any write: the DB-clock `SELECT now()`
watermark ("ROLLBACK KEY") **and** a "ROLLBACK MANIFEST" path. On completion it writes the manifest — a
JSONL file (one `{"fact_id": "<uuid>"}` per line) under `reports/`, holding the **exact** fact ids the run
created. `--dry-run` prints the manifest path it *would* write but writes nothing.

**`--phase rollback --manifest <path>` is the preferred, exact mode.** It soft-deactivates exactly the
listed ids (agent- and source-scoped, active rows only):

```sql
UPDATE heart.facts SET active = false
WHERE agent_id = :agent_id AND source = 'exemplar_extractor' AND active = true
  AND id = ANY(CAST(:ids AS uuid[]));
```

Because a concurrent **live** write's fact id is never in a backfill's manifest, this mode **cannot** touch
a live write — it closes the race the watermark mode cannot.

**`--phase rollback --watermark <iso-ts>` is the fallback** (use only when no manifest exists). It keys on
`created_at >= watermark`, so it **may catch concurrent live writes** and prints a loud WARNING saying so.
The race it cannot fully avoid: an in-flight live extraction reading **pre-watermark** chunks can commit its
facts **after** the watermark; the chunk-timestamp guard below only catches facts whose *source chunk
itself* is newer than the watermark (a JOIN against `heart.episode_chunks` on `source_episode_id`, both
sides' `created_at >= watermark`), so a nonzero count aborts (no writes) unless `--include-live-writes` is
passed. `--dry-run` reports both counts and never aborts. **The manifest closes the pre-watermark-chunk case
the chunk-timestamp guard leaves open** — prefer it whenever the printed manifest is available.

Both modes are soft-deactivation, never a hard delete; reactivation is the inverse.

---

## Flags (all 9, verified against `nous/config.py`)

| Env Var | Default | Description |
|---------|---------|-------------|
| `NOUS_EXEMPLAR_EXTRACTION_ENABLED` | `false` | Write-path master switch: parse-only exemplar extraction of `utterance\nlabel: N` streams into individually-embedded facts (`source='exemplar_extractor'`). Zero LLM. |
| `NOUS_EXEMPLAR_DENSITY_THRESHOLD` | `0.8` | `exemplar_density` score at/above which a transcript routes to exemplar extraction (checked before R1). |
| `NOUS_EXEMPLAR_MAX_PER_EPISODE` | `5000` | Cap on exemplar facts stored per episode; truncation logs WARNING (never silent). |
| `NOUS_EXEMPLAR_MIN_CONTENT_CHARS` | `5` | Source-aware min-content floor for exemplar facts (labels/utterances are short; the global 30-char floor would reject them). |
| `NOUS_EXEMPLAR_MODE_ENABLED` | `false` | Read-path master switch: exemplar retrieval leg in `run_recall_pipeline` (land-dark). |
| `NOUS_EXEMPLAR_TOP_K` | `25` | Max exemplars fetched/injected per query. |
| `NOUS_EXEMPLAR_LEG_SCORE` | `0.55` | Score-band ceiling for exemplar hits (below the RRF direct-hit head; per-rank decay 0.005). |
| `NOUS_EXEMPLAR_MIN_SIMILARITY` | `0.30` | Cosine floor — exemplars below this similarity are not merged (bounds false-trigger displacement, Gate 2). |
| `NOUS_EXEMPLAR_MAX_QUERY_WORDS` | `64` | Trigger gate: queries longer than this many words are not classification-shaped. |

---

## Migration 066 (index-only)

| Object | Type | Description |
|--------|------|-------------|
| `idx_facts_exemplar_embedding` | partial HNSW on `heart.facts(embedding)` `WHERE source = 'exemplar_extractor' AND active = true` | Keeps the exemplar cosine walk off the global embedding index; the `active = true` predicate (codex r7) drops rollback-deactivated exemplars out of the ANN candidate horizon. Matches `fetch_exemplars_by_vector`'s WHERE (source + active) so the planner uses the partial index. |
| `idx_facts_exemplar_agent` | partial btree on `heart.facts(agent_id)` `WHERE source = 'exemplar_extractor' AND active = true` | Supports `has_exemplars()`'s existence probe. |

**Codex r7 re-create note.** Migration 066 was amended in place (branch is unmerged) to add `active = true`
to the HNSW predicate, and it now runs `DROP INDEX IF EXISTS heart.idx_facts_exemplar_embedding;` before
the `CREATE`. The migrator tracks by filename, so a **pre-merge dev DB** that already recorded 066 will
**not** rerun it automatically — re-create the index manually by running 066's two `DROP`/`CREATE`
statements, or by deleting the `066` row from `nous_system.schema_migrations` and re-running the migrator
(safe: the file is drop-then-create idempotent). Fresh DBs get the corrected predicate directly.

No new columns or tables — F086 reuses the existing `heart.facts` schema in full (option B from the
requirements doc; see Documented Deviations below for why).

---

## Documented Deviations from the Requirements Text

1. **Detection is a NEW predicate, not "the R1 heuristic."** Validated: `is_enumerable` provably does not
   fire on label-streams. See Falsified Assumption #1 above.
2. **Backfill reads `heart.episode_chunks`, not `episodes.transcript`.** Validated: the 8000-char capture
   cap. See Falsified Assumption #2 above.
3. **`subject_key=NULL`, while the requirements doc's option-B sketch implied keyed rows** ("value=label" as
   an attribute on a keyed fact). The requirements text is silent on `subject_key` specifically; leaving it
   `NULL` is a deliberate collision-safety choice — F084/F085's same-slot conflict routing and entity-key
   emission both key off `subject_key`/entity presence, and near-identical exemplar utterances with
   different labels must never be treated as same-slot conflicts.
4. **`value=label` is encoded in the content text, not a separate column.** `heart.facts` has no `value`
   column and `FactInput` has no `value` field — the label lives in the pair text itself plus
   `attribute_key='label'` as a marker, recoverable via `parse_label`. This encoding is also *forced* by
   Gate-1 sim parity regardless: the sim embedded the full `utterance\nlabel: N` string, not a
   utterance-only field with the label elsewhere.
5. **A similarity floor (`NOUS_EXEMPLAR_MIN_SIMILARITY`) was added.** The requirements doc's Gate 2
   ("non-exemplar retrieval unchanged when the mode triggers falsely") needs a concrete mechanism; the
   floor plus score-banded stable insertion are that mechanism (see Similarity floor above).
6. **A write-side flag (`NOUS_EXEMPLAR_EXTRACTION_ENABLED`) was added.** The requirements doc names only
   the read flag (`NOUS_EXEMPLAR_MODE_ENABLED`); land-dark discipline needs both, since the write path
   must also be independently gateable — see the Land-Dark Contract above for why write-on/read-off is
   *not* an equivalent-to-baseline configuration.
7. **The backfill re-derives ordinals itself rather than calling `ingest_exemplars` per chunk.** Task 2's
   `ingest_exemplars` parses one transcript in a single call and always starts pair ordinals at 0; the
   backfill needs ordinals to continue across an episode's chunk boundaries, so
   `scripts/backfill_exemplar_facts.py::build_episode_pairs` re-parses each chunk independently and
   re-stamps ordinals with a running offset. Storage itself is **not** duplicated: both the live write path
   and the backfill call the shared `nous/handlers/exemplar_ingest.py::_embed_and_store_pairs`
   cap+embed+learn helper (refactored out in b5b60c4) — only the pair-assembly step differs between the two.

**Not a deviation** (spec-review M1): the memory-referential-only trigger blocklist is a faithful
operationalization of the requirements doc's own qualifier — "classification-shaped (short utterance, no
interrogative **about stored content**)" — not a narrowing of it. trec_coarse's own gold queries are
interrogative and must trigger (supported by its 0.90-live / 0.82-sim numbers); only interrogatives asking
about the agent's own conversation history are excluded.

---

## Non-Goals (v1, per the requirements doc)

- **Multi-round exemplar gathering.** A single embedding round already reaches 0.82 sim; iterate only if a
  future replay shows conversion loss.
- **LLM-based trigger classification.** The trigger heuristic is regex + word-count only, zero LLM calls.
- **Recsys-style reasoning.** Out of scope (the requirements doc calls this "data-blocked").
- **Label-space reasoning beyond injection.** The leg injects labeled examples as evidence; it does not
  reason about the label space itself (e.g. label hierarchies, confusable-label clustering).
