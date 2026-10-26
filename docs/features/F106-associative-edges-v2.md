# F106: Associative Edges v2

**Status:** in progress. Task 1 (a query-relevance gate for Path A neighbours reached through an
associative edge, and an associative-edge multiplier in the neighbour fetch order), Task 2 (a v2 of
the co-mention edge builder) and Task 3 (a v2 of the co-occurrence edge builder) are built, with
every setting off or neutral by default, so recall and the graph are unchanged until they are
enabled. Task 3 also fixes two counting bugs of the co-occurrence builder that apply with v2 off.
Task 3b (a temporal-query exemption) was measured and not built.

**Builds on:** [F076 Co-mention Entity Linking](F076-comention-entity-linking.md) (the `co_mention`
edges), the experiential co-occurrence linker (`co_occurred` edges, `NOUS_COOCCURRENCE_LINKING_ENABLED`),
Path A graph-neighbour recall (Stage 2b of `run_recall_pipeline`, `NOUS_HEART_GRAPH_ALL_TYPES_ENABLED`),
[F105 Spreading Activation v2](F105-spreading-activation-v2.md) (`query_cosines` and `gate_factor`) and
F091 retrieval telemetry.

## Goal

Make associative edges help recall instead of adding noise. An edge is associative when its relation
is `co_occurred` or its extraction method is `co_mention` (`graph_constants.is_associative`, the
Python mirror of `associative_edge_sql`).

## Problem

Measured on Path A neighbours that reached the prompt in a 30-day prod recall log:

- **Delivered relevance.** A blinded judge rated each rendered Path A neighbour against its query.
  Rows reached through a `co_mention` edge were unrelated to the query 57% of the time, and rows
  reached through a `co_occurred` edge 53%. The controls: 3% for vector hits and 10% for neighbours
  reached through cosine edges.
- **Rank.** Associative neighbours rank higher (median final rank 14) than cosine-edge neighbours
  (56). Under the seed-score composition they inherit seed × 0.9, while cosine edges average about
  0.52 and take the inferred-edge penalty.
- **Crowding.** `Brain._neighbors` orders a seed's edges by weight, descending, and Path A fetches 3
  neighbours per neighbour type per seed, so 0.9 associative edges take the slots before any cosine
  edge is read.
- **Pairwise precision.** `co_mention` pairs whose shared entity is rare are about as precise,
  pairwise, as cosine edges (0.77 vs 0.80 judged related). The problem is how the edges are
  consumed, not only how they are created.

## Task 1: Path A associative query gate and fetch multiplier

### The gate

Behind `NOUS_GRAPH_ASSOCIATIVE_QUERY_GATE_ENABLED` (default off). In Stage 2b, after each
`brain.neighbors` call, a batch holding at least one associative neighbour is gated:

- The query is embedded once per recall (`brain.embeddings`), on the first such batch, and reused. A
  failed embed is not retried.
- `cos(node, query)` is read with F105's `query_cosines`, in a short session of its own, inside a
  savepoint.
- Each associative neighbour then gets a `query_gate_factor`:
  - **`raw` mode (the default).** Only the associative neighbours' cosines are read. A neighbour with
    `cos(node, query)` below `NOUS_GRAPH_ASSOCIATIVE_QUERY_GATE_MIN_COSINE` (0.31) is dropped, with
    factor 0.0. Any other keeps factor 1.0, and its score is unchanged.
  - **`seed_relative` mode.** The seed's cosine is read too, and the factor is F105's
    `gate_factor(cos_node, cos_seed, "seed_relative", 0, 1, seed_min)` =
    `min(1, max(cos_node, 0) / max(cos_seed, 0.19))`. The neighbour's score is multiplied by it, and
    the neighbour is dropped when the factor is below `NOUS_GRAPH_ASSOCIATIVE_QUERY_GATE_DROP_BELOW`
    (default 0, scale only).
- A neighbour without an embedding is neutral in both modes (kept, factor 1.0): absence is not a
  relevance value. Non-associative neighbours are never gated (`query_gate_factor` stays None).

The factor is set before Stage 2b's best-path comparison, so a node reached through an associative
path from one seed and a cosine path from another keeps the path with the better *gated* score, and
the replacement copies the factor along with the rest of the path. A dropped neighbour is still
recorded as an F091 expansion, with `query_gate_factor`, `query_cosine` and `query_gate_dropped`, so
the prod log can measure the gate; it is then neither appended nor allowed to replace a stored path.
The trace agrees with that choice: a gated expansion's `path_strength` is
`seed_score × edge_weight × query_gate_factor`, a dropped one's is 0, and a dropped path never wins
`won_best_path` (a neighbour reached only by dropped paths has no winner). An ungated expansion's
`path_strength` is unchanged. `path_strength` leaves out the associative multiplier and the legacy
scorer's provenance penalty, so the trace's winner matches Stage 2b's choice exactly only at
`NOUS_GRAPH_ASSOCIATIVE_EDGE_MULTIPLIER = 1.0` with the seed-score scorer on.
`_score_memory_neighbor` multiplies the score by the factor in both of its branches (seed-score and
legacy).

**Failure.** No embedder or an empty query vector leaves the neighbours ungated. Any exception (the
embed, the read, an unknown mode) leaves that batch ungated, counts
`n_stage_errors["path_a_associative_gate"]` and logs at debug. Path A always goes on.

**Cost.** Nothing runs for a batch without an associative neighbour: about 3% of recalls fetch one.

**Why `raw` is the default.** An offline test scored both modes on 557 delivered associative Path A
rows from a 30-day prod recall log, judged by a blinded LLM judge. The cutoff was tuned on half of
the retrievals and tested on the other half. As a separator of useful from unrelated rows, the
neighbour's raw query cosine had an AUC of 0.87, against 0.78 for the seed-relative factor. On the
held-out half, a cutoff of 0.31 removed 75% of the rows judged unrelated and 18% of the rows judged
useful. The cutoff is specific to the embedding model (`text-embedding-3-large` at 1536 dimensions).

**Scope decisions.**

- The gate covers `co_occurred` edges as well as `co_mention`: both measured about as unrelated.
- There is no exemption for temporal queries. The available signal, `has_temporal_signal`, matches
  very common words, so an exemption keyed on it would also exempt many queries that are not
  temporal. A precise exemption was measured with Task 3 and not built (see Task 3b).

### The fetch multiplier

`NOUS_GRAPH_ASSOCIATIVE_EDGE_MULTIPLIER` (default 1.0, bounded `(0, 1]`) multiplies an associative
edge's weight wherever `Brain._neighbors` ranks by weight: in the per-neighbour window that picks
which edge represents a neighbour, and in the ordering that picks which neighbours fill the LIMIT.
At 0.5 a 0.9 `co_mention` edge ranks as 0.45, below a 0.6 cosine edge. The returned `edge_weight`
stays the raw stored weight. The multiplier applies inside the `COALESCE`, so a NULL weight still
sorts last whatever its edge. At 1.0 no term is added and the compiled SQL is unchanged.

The same multiplier scales an associative neighbour's score in `_score_memory_neighbor`. That scorer
is shared by Path A, Stage 2's decision neighbours and Stage 4's one-hop rows, and `_neighbors` serves
every `brain.neighbors` caller, so the multiplier reaches all of them.

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `NOUS_GRAPH_ASSOCIATIVE_QUERY_GATE_ENABLED` | `false` | Gate Path A neighbours reached through an associative edge by query relevance |
| `NOUS_GRAPH_ASSOCIATIVE_QUERY_GATE_MODE` | `raw` | `raw` or `seed_relative` |
| `NOUS_GRAPH_ASSOCIATIVE_QUERY_GATE_MIN_COSINE` | `0.31` | `raw` mode: drop below this cosine |
| `NOUS_GRAPH_ASSOCIATIVE_QUERY_GATE_SEED_MIN` | `0.19` | `seed_relative` mode: smallest denominator (F105's value) |
| `NOUS_GRAPH_ASSOCIATIVE_QUERY_GATE_DROP_BELOW` | `0.0` | `seed_relative` mode: drop below this factor; 0 scales only |
| `NOUS_GRAPH_ASSOCIATIVE_EDGE_MULTIPLIER` | `1.0` | Associative edge weight multiplier in the fetch order and the neighbour score |

With every setting at its default, Path A's candidates, scores and order, the F091 expansion rows,
and the `_neighbors` SQL are identical to before (`tests/test_f106_path_a_associative_gate.py`).

### Limitations

- The prod recall log holds only the winning path of each delivered neighbour. The evidence and the
  offline test therefore cannot say how often an associative path was the runner-up to a cosine path,
  or what a cosine edge pushed out of a fetch window would have delivered. The multiplier's effect is
  not measured offline.
- The `raw` cutoff depends on the embedding model and must be re-tuned for another one.

## Task 2: co-mention builder v2

Behind `NOUS_COMENTION_V2_ENABLED` (default off). Off, the F076 builder is unchanged: the same facts
are scanned, the same edges inserted at the same weight, and `dry_run` returns the same count.

### Why

The starting point is the pairwise result above: `co_mention` pairs on a rare shared entity are as
precise as cosine edges. The v1 builder does not use that signal well:

- **The window.** Each cycle scans only the newest `NOUS_COMENTION_MAX_FACTS_PER_CYCLE` (5000) facts
  and counts an entity's frequency inside that window. An entity named by many older facts looks rare
  there, so the hub gate (`NOUS_COMENTION_MAX_DEGREE`, 10) lets it link. A new fact never links to an
  old one outside the window.
- **The cap.** The per-fact cap (`NOUS_COMENTION_MAX_EDGES_PER_NODE`, 20) counts only the current
  cycle's new edges, so a fact can gain up to 20 more in every cycle.
- **The weight.** Every edge gets `NOUS_COMENTION_WEIGHT` (0.9), whether its entity is named by 2
  facts or by 10.
- **Noise phrases.** Timestamps (`Mon Sep`, `AM EDT`), imperative boilerplate (`DO NOT`, `HARD RULE`)
  and labels (`Option B`, `Codex P1`) extract as entities and link unrelated facts.
- **No repair.** An edge whose entity later becomes a hub stays forever.

### Behaviour

`GraphDensifier.plan_comention_changes()` computes a `ComentionPlan(inserts, deletes, reweights)`
without writing; `build_comention_edges()` applies one, deletes, re-weights and inserts in a single
transaction, and with `dry_run=True` returns `len(plan.inserts)`. Under v1 the plan is today's insert
list with no deletes or reweights. Under v2:

1. **Index.** Entities are extracted (with the stoplist) from the agent's newest
   `NOUS_COMENTION_INDEX_MAX_FACTS` (50,000) active facts. An entity's frequency `df` is counted over
   the whole index. The per-cycle window is not used. The index scan and the edge scan (step 5) yield
   to the event loop every 1000 facts or edges; an interrupt seen there leaves the plan empty, so the
   cycle writes nothing.
2. **Stoplist** (`extract_entities(..., stoplist=True)`). A phrase is dropped when it ends in a
   weekday followed by a month, abbreviated or full (`mon sep`, `tuesday october`,
   `build bot mon sep`, but not `rising sun may festival`), or in AM/PM followed by a time zone
   (`edt est et utc gmt pst pdt cet cest`); when it is exactly `do not`, `must not` or `hard rule`;
   when it starts with `standing rule`; or when it is `option <one letter or digit>` or
   `codex p<digit>`. With `stoplist=False` (the default, and v1) extraction is unchanged.
3. **Qualifying entity.** `2 <= df <= NOUS_COMENTION_V2_MAX_DEGREE` (20; v1 keeps
   `NOUS_COMENTION_MAX_DEGREE`, 10), and a rarity weight above 0.
4. **Rarity weight.** `w(df) = NOUS_COMENTION_WEIGHT x ln(N/df) / ln(N/2)`, where N is the number of
   indexed facts: `df = 2` gives the full weight and rarer is heavier. `df = N` gives 0, so an entity
   named by every indexed fact does not qualify. `NOUS_COMENTION_WEIGHT` when `N <= 2`. An edge's
   weight comes from its rarest qualifying shared entity.
5. **Maintenance**, before linking. It covers the agent's `co_mention` edges with both endpoints in
   the index. An edge with no qualifying shared entity (its entity became a hub, is stoplisted, weighs
   0, or is no longer shared) is deleted. An edge whose weight is NULL or differs from its target `w`
   by more than 1e-6 is set to `w`, which also restores a weight decayed to near zero. Deletes go
   first, then re-weights, in edge order, and the pass stops after
   `NOUS_COMENTION_PRUNE_MAX_PER_CYCLE` (2000) changes in all. An edge with an endpoint outside the
   index is left alone, and so is every edge that is not `co_mention` (a `contradicts` or cosine
   edge is never deleted or re-weighted, and still blocks a `co_mention` link on its pair).
6. **Linking.** Rarer entities first, as in v1, with the same pre-existing-edge guard (any fact-fact
   edge, any relation, either direction, skips the pair). The per-fact cap now holds over the fact's
   lifetime: a fact's count starts at its current `co_mention` degree (every partner, in the index or
   not) after this plan's deletes. A new link also needs the pair's embedding cosine to be at least
   `NOUS_COMENTION_V2_MIN_COSINE` (0.35). The cosines of all candidate pairs come from one query, and
   a pair where either fact has no embedding passes. The floor is checked before the pair takes a cap
   slot, so a rejected pair does not use one. It applies to new links only: it never deletes an
   existing edge or keeps one from its re-weight. New edges get weight `w`. A pair is only ever linked
   at its rarest shared entity (a skip there, for the cap, an existing edge or the cosine floor, holds
   for every later entity), so a new edge already has the weight maintenance would give it.

**Reporting.** `run_backfill_cycle` returns `_co_mention_deleted` and `_co_mention_reweighted` with
v2 on; the sleep handler pops them (so they never inflate `orphan_edges_created`) into sleep stats as
`comention_edges_deleted` and `comention_edges_reweighted`. With v2 off neither key appears. When a
cycle deletes or re-weights, the sleep handler also records a `graph_densify` / `edge_prune` row in
the consolidation audit (F035.6) with both counts and the rationale `F106 co_mention maintenance`, as
the F053 dead-edge prune records its `edge_prune` row. The build also logs
`F106: co_mention maintenance ...`.

**Ownership of `co_mention` edges.** They are derived data: the builder creates them from fact text
and can rebuild them, so v2 deletes the ones it no longer supports (as the F053 dead-edge prune
deletes edges). It also owns their weights: if the tinyHippo downscale
(`NOUS_TINYHIPPO_DOWNSCALE_ENABLED`, off) is turned on again, its downscale of `co_mention` edges is
undone by the next cycle's maintenance, up to the per-cycle bound.

**The backfill script.** `scripts/backfill_comention_edges.py` plans once per agent and prints the
insert, delete and re-weight counts. With `--commit`, when any of them is non-zero, it applies that
same plan (`build_comention_edges(plan=plan)`); it never plans again. It lifts
`NOUS_COMENTION_INDEX_MAX_FACTS` to the corpus size as well as the per-cycle cap, so under v2 the plan
covers the whole corpus; maintenance stays within the per-cycle bound.

### The first offline test

An offline test on a prod copy compared v1 and v2 plans, with a blinded judge rating sampled new
links, deleted edges and kept edges. v2 as first built (hub cap 10 counted over the index, no cosine
floor) failed on its new links: 62% were judged related, below the 70% bar. The unrelated new links
mostly shared generic tech names, and good links that maintenance deleted shared entities named in
11-17 facts. Two v2-only settings followed: `NOUS_COMENTION_V2_MAX_DEGREE` (20), so such entities
still qualify, and `NOUS_COMENTION_V2_MIN_COSINE` (0.35) on new links. v2 with them has not been
re-tested yet.

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `NOUS_COMENTION_V2_ENABLED` | `false` | Run the v2 builder |
| `NOUS_COMENTION_INDEX_MAX_FACTS` | `50000` | Index size: the newest N active facts |
| `NOUS_COMENTION_PRUNE_MAX_PER_CYCLE` | `2000` | Most maintenance deletes plus re-weights per cycle; 0 turns maintenance off |
| `NOUS_COMENTION_V2_MAX_DEGREE` | `20` | v2 hub cap over the whole index (v1 keeps `NOUS_COMENTION_MAX_DEGREE`) |
| `NOUS_COMENTION_V2_MIN_COSINE` | `0.35` | Embedding cosine a new v2 link needs; a fact without an embedding passes |

v2 also reads the F076 settings `NOUS_COMENTION_MAX_EDGES_PER_NODE`, `NOUS_COMENTION_WEIGHT` and
`NOUS_COMENTION_MIN_ENTITY_CHARS`, but not `NOUS_COMENTION_MAX_DEGREE` or
`NOUS_COMENTION_MAX_FACTS_PER_CYCLE`. Tests: `tests/test_f106_comention_v2.py`; the v1 builder stays
pinned by `tests/test_graph_densifier.py` and `tests/test_comention_linking.py`.

### Limitations

- **Convergence.** v1 wrote every edge at 0.9, so the first v2 cycles mostly re-weight. With the
  default bound a graph with more than 2000 edges to change takes several cycles; until then some
  edges keep their v1 weight or their hub link.
- **Cost.** Each cycle reads every `co_mention` edge of the agent, extracts entities from up to
  50,000 facts in Python, and reads the cosine of every candidate pair in one query.
- **The stoplist is lexical.** It catches the listed shapes only; other boilerplate still extracts.
- **The cosine floor** depends on the embedding model (`text-embedding-3-large` at 1536 dimensions
  here), and a new link where either fact has no embedding is not gated.
- **The index boundary.** An edge to a fact that has fallen out of the index is neither deleted nor
  re-weighted.
- **Not re-evaluated.** v2 with the hub cap of 20 and the cosine floor has not been through the
  offline test again; the flag stays off until it passes.

## Task 3: co-occurrence builder v2

Behind `NOUS_COOCCURRENCE_V2_ENABLED` (default off). Off, the v1 builder links the same pairs at the
same weight as before. Two counting fixes apply either way (see Reporting).

### Why

The v1 builder (`NOUS_COOCCURRENCE_LINKING_ENABLED`, itself off by default) links every pair of
active facts from an episode with 2 to `NOUS_COOCCURRENCE_MAX_EPISODE_FACTS` (6) facts. A private
audit judged same-conversation fact pairs with an episodic rubric (related = the same event, task or
thread; 280 pairs across the rules). By the pair's embedding cosine:

| Pair cosine | Judged related | Judged unrelated |
|---|---|---|
| below 0.25 | 16% | 34% |
| 0.25 to 0.35 | 48% | 3% |
| 0.35 to 0.45 | 73% | 2% |
| 0.45 and above | 90% or more | 0% |

By rule:

- Same conversation and a shared extracted entity key: 78% related, 0% unrelated.
- Facts enumerated from the same transcript chunk, at most 3 positions apart: 68% related, 5%
  unrelated.
- The time between two facts does not separate them: pairs at most 2 minutes apart and pairs 10 to
  60 minutes apart were both 47% related.
- The edges v1 had built: 57% related, 20% unrelated.

v1's 2-to-6-fact gate also covers only 10% of facts. So v2 keeps the episode as the unit, drops the
size gate and requires a thread signal.

### Behaviour

`GraphDensifier.plan_cooccurrence_changes()` computes a `CooccurrencePlan(inserts, deletes,
reweights)`, the same shape as Task 2's `ComentionPlan`, without writing. `build_cooccurrence_edges()`
applies one (deletes, restores, then inserts, in one transaction), and with `dry_run=True` returns
`len(plan.inserts)`. Under v1 the plan is the v1 insert list with no deletes or reweights. Under v2:

1. **Qualifying pair.** Two active facts of the agent with the same `source_episode_id`, and at
   least one thread signal:
   - an embedding cosine of at least `NOUS_COOCCURRENCE_MIN_COSINE` (0.30);
   - a shared `heart.fact_entity_keys` key, read under that table's invariant (both facts active,
     the key rows scoped to the agent);
   - both facts carry a `source_ordinal` (the enumerative extractor's chunk index × 1,000,000 +
     position), in the same chunk, at most `NOUS_COOCCURRENCE_ORDINAL_WINDOW` (3) positions apart.

   When both facts are embedded, their cosine must also be below `NOUS_GRAPH_THRESHOLD_FACT_FACT`
   (0.82): near-duplicates are the cosine densifier's job. A pair where either fact has no embedding
   passes that ceiling and qualifies only on a key or an ordinal. There is no episode-size gate. The
   check is one SQL predicate (pgvector cosine, ordinal arithmetic, then an `EXISTS` on the key
   table), shared by the candidate read and the maintenance read.
2. **Maintenance**, before linking. It covers every `co_occurrence` edge of the agent whose
   endpoints are both active, not only those in the episode window, so old v1 edges are reached. An
   edge whose pair no longer qualifies (no thread signal, a near-duplicate, or facts that are now in
   different episodes) is deleted. A qualifying edge whose weight is below `NOUS_COOCCURRENCE_WEIGHT`
   (or NULL) is restored to it, which undoes decay; a weight above it is kept. Deletes go first,
   then restores, each in edge order, and the pass stops after `NOUS_COOCCURRENCE_PRUNE_MAX_PER_CYCLE`
   (2000) changes in all. An edge with an inactive endpoint is left to the F053 dead-edge prune. The
   `co_occurred` edges that another producer writes with extraction method `deterministic` are never
   read or written.
3. **Linking.** The candidates are the qualifying pairs of the newest
   `NOUS_COOCCURRENCE_MAX_EPISODES_PER_CYCLE` (2000) episodes (episodes with at least 2 active facts,
   ordered by their newest fact), computed in one SQL query. As in v1, a pair already joined by any
   fact-fact edge (any relation, either direction) is skipped. Pairs link strongest cosine first;
   pairs without a cosine come last, and ties go by fact id. The per-fact cap
   `NOUS_COOCCURRENCE_MAX_EDGES_PER_FACT` (10) holds over the fact's lifetime: its count starts at its
   current `co_occurrence` edges (any partner) less this plan's deletes. New edges get
   `NOUS_COOCCURRENCE_WEIGHT` (0.9).
4. **Interrupt.** The edge scan and the candidate scan yield to the event loop every 1000 rows; an
   interrupt seen there leaves the plan empty, so the cycle writes nothing.

**Reporting.**

- **Honest insert count (v1 and v2).** The build returns the rows actually inserted: it writes one
  `INSERT ... SELECT unnest(...) ON CONFLICT DO NOTHING` per batch of 500 pairs and adds up their row
  counts. v1 counted every attempted insert, including the ones that hit a conflict. The interrupt
  is now checked between batches rather than between single inserts.
- **Sleep stats (v1 and v2).** `_phase_graph_densification` did not pop the `_co_occurrence` count,
  so with co-occurrence linking on it was added into `orphan_edges_created`. It is now reported on
  its own as `cooccurrence_edges` and listed in the `graph_densify` / `edge_add` audit counts. This
  changes v1's numbers: `orphan_edges_created` no longer includes co-occurrence edges.
- **Maintenance (v2 only).** `run_backfill_cycle` returns `_co_occurrence_deleted` and
  `_co_occurrence_reweighted`; the sleep handler pops them into `cooccurrence_edges_deleted` and
  `cooccurrence_edges_reweighted` and, when either is above 0, records a `graph_densify` /
  `edge_prune` row in the consolidation audit with the rationale `F106 co_occurrence maintenance`.
  The build also logs `F106: co_occurrence maintenance ...`.

**The backfill script.** `scripts/backfill_cooccurrence_edges.py` plans once per agent and prints the
insert, delete and restore counts. With `--commit`, when any of them is non-zero, it applies that same
plan; it never plans again. As before it lifts the episode window to the corpus; maintenance stays
within the per-cycle bound.

### Offline test

Set before the build: dry-run plans on a prod copy, with the episodic rubric judging 40 new links, 40
deletes and 40 kept edges. It passes when new links are at least 65% related and at most 5%
unrelated, and deletes at most 35% related; coverage (facts with at least one `co_occurred` edge)
is reported before and after. It has not been run yet, and the flag stays off until it passes.

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `NOUS_COOCCURRENCE_V2_ENABLED` | `false` | Run the v2 builder |
| `NOUS_COOCCURRENCE_MIN_COSINE` | `0.30` | Cosine signal: a same-episode pair at or above it qualifies |
| `NOUS_COOCCURRENCE_ORDINAL_WINDOW` | `3` | Ordinal signal: most positions apart within one transcript chunk |
| `NOUS_COOCCURRENCE_MAX_EDGES_PER_FACT` | `10` | Lifetime cap on a fact's `co_occurrence` edges |
| `NOUS_COOCCURRENCE_PRUNE_MAX_PER_CYCLE` | `2000` | Most maintenance deletes plus restores per cycle; 0 turns maintenance off |

v2 also reads `NOUS_COOCCURRENCE_WEIGHT`, `NOUS_COOCCURRENCE_MAX_EPISODES_PER_CYCLE` and
`NOUS_GRAPH_THRESHOLD_FACT_FACT`, but not `NOUS_COOCCURRENCE_MAX_EPISODE_FACTS`. Tests:
`tests/test_f106_cooccurrence_v2.py`, which also pins the v1 builder.

### Limitations

- **Cost.** The candidate query computes the cosine of every same-episode pair in the window, so its
  cost grows with the square of episode size (a 1000-fact episode is about 500,000 pairs). Each
  cycle also checks every `co_occurrence` edge of the agent. Neither is measured on a prod-sized
  corpus.
- **The key signal** needs entity-key rows, which only keyed facts have.
- **The cosine floor and ceiling** depend on the embedding model (`text-embedding-3-large` at 1536
  dimensions here).
- **Convergence.** v1 edges that do not qualify are deleted at most 2000 per cycle (with the
  restores), so a large v1 graph takes several cycles.
- **Not evaluated.** The offline test above has not been run.

## Task 3b: temporal-query exemption (measured, not built)

Task 1 gates `co_occurred` neighbours like `co_mention` ones. A query about an occasion or a time
("what else happened when ...", "what did we do that day") might want them ungated, but
`has_temporal_signal` matches common words and cannot pick those queries out. The plan was to build a
precise episodic-query detector, and exempt `co_occurred` neighbours from the gate on those queries,
only if such queries are at least 5% of recall queries. An LLM classifier on 300 sampled prod recall
queries found 0.7%, so neither was built, and Task 1's gate stays the only consumer rule.
