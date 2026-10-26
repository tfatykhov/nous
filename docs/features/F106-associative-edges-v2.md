# F106: Associative Edges v2

**Status:** in progress. Task 1 (a query-relevance gate for Path A neighbours reached through an
associative edge, and an associative-edge multiplier in the neighbour fetch order) and Task 2 (a v2
of the co-mention edge builder) are built, with every setting off or neutral by default, so recall
and the graph are unchanged until they are enabled. Task 3 is planned.

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
  temporal. A temporal exemption is deferred to Task 3, the co-occurrence redesign.

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
   the whole index. The per-cycle window is not used.
2. **Stoplist** (`extract_entities(..., stoplist=True)`). A phrase is dropped when it contains a
   weekday followed by a month, abbreviated or full (`mon sep`, `tuesday october`), or AM/PM followed
   by a time zone (`edt est et utc gmt pst pdt cet cest`), anywhere in the phrase; when it is exactly
   `do not`, `must not` or `hard rule`; when it starts with `standing rule`; or when it is
   `option <one letter or digit>` or `codex p<digit>`. With `stoplist=False` (the default, and v1)
   extraction is unchanged.
3. **Qualifying entity.** `2 <= df <= NOUS_COMENTION_MAX_DEGREE`.
4. **Rarity weight.** `w(df) = NOUS_COMENTION_WEIGHT x ln(N/df) / ln(N/2)`, where N is the number of
   indexed facts: `df = 2` gives the full weight, rarer is heavier, and `df = N` gives 0.
   `NOUS_COMENTION_WEIGHT` when `N <= 2`. An edge's weight comes from its rarest qualifying shared
   entity.
5. **Maintenance**, before linking. It covers the agent's `co_mention` edges with both endpoints in
   the index. An edge with no qualifying shared entity (its entity became a hub, is stoplisted, or is
   no longer shared) is deleted. An edge whose weight is NULL or differs from its target `w` by more
   than 1e-6 is set to `w`, which also restores a weight decayed to near zero. Deletes go first, then
   re-weights, in edge order, and the pass stops after `NOUS_COMENTION_PRUNE_MAX_PER_CYCLE` (2000)
   changes in all. An edge with an endpoint outside the index is left alone.
6. **Linking.** Rarer entities first, as in v1, with the same pre-existing-edge guard (any fact-fact
   edge, any relation, either direction, skips the pair). The per-fact cap now holds over the fact's
   lifetime: a fact's count starts at its current `co_mention` degree (every partner, in the index or
   not) after this plan's deletes. New edges get weight `w`. A pair is only ever linked at its rarest
   shared entity, so a new edge already has the weight maintenance would give it.

**Reporting.** `run_backfill_cycle` returns `_co_mention_deleted` and `_co_mention_reweighted` with
v2 on; the sleep handler pops them (so they never inflate `orphan_edges_created`) into sleep stats as
`comention_edges_deleted` and `comention_edges_reweighted`. With v2 off neither key appears. The
build also logs `F106: co_mention maintenance ...` when it deletes or re-weights.

**Ownership of `co_mention` edges.** They are derived data: the builder creates them from fact text
and can rebuild them, so v2 deletes the ones it no longer supports (as the F053 dead-edge prune
deletes edges). It also owns their weights: if the tinyHippo downscale
(`NOUS_TINYHIPPO_DOWNSCALE_ENABLED`, off) is turned on again, its downscale of `co_mention` edges is
undone by the next cycle's maintenance, up to the per-cycle bound.

**The backfill script.** `scripts/backfill_comention_edges.py` lifts `NOUS_COMENTION_INDEX_MAX_FACTS`
to the corpus size as well as the per-cycle cap, so under v2 it still covers the whole corpus. Its
`--commit` run then applies maintenance too, within the per-cycle bound.

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `NOUS_COMENTION_V2_ENABLED` | `false` | Run the v2 builder |
| `NOUS_COMENTION_INDEX_MAX_FACTS` | `50000` | Index size: the newest N active facts |
| `NOUS_COMENTION_PRUNE_MAX_PER_CYCLE` | `2000` | Most maintenance deletes plus re-weights per cycle; 0 turns maintenance off |

v2 also reads the F076 settings `NOUS_COMENTION_MAX_DEGREE`, `NOUS_COMENTION_MAX_EDGES_PER_NODE`,
`NOUS_COMENTION_WEIGHT` and `NOUS_COMENTION_MIN_ENTITY_CHARS`, but not
`NOUS_COMENTION_MAX_FACTS_PER_CYCLE`. Tests: `tests/test_f106_comention_v2.py`; the v1 builder stays
pinned by `tests/test_graph_densifier.py` and `tests/test_comention_linking.py`.

### Limitations

- **Convergence.** v1 wrote every edge at 0.9, so the first v2 cycles mostly re-weight. With the
  default bound a graph with more than 2000 edges to change takes several cycles; until then some
  edges keep their v1 weight or their hub link.
- **Cost.** Each cycle reads every `co_mention` edge of the agent and extracts entities from up to
  50,000 facts in Python.
- **Small corpora.** With N facts indexed, an entity named by all N gets weight 0 (it still links,
  if `N <= NOUS_COMENTION_MAX_DEGREE`).
- **The stoplist is lexical.** It catches the listed shapes only; other boilerplate still extracts.
- **The index boundary.** An edge to a fact that has fallen out of the index is neither deleted nor
  re-weighted.
- **Not evaluated yet.** An offline comparison of v1 and v2 plans on a prod copy is pending; the flag
  stays off until then.

## Task 3 (planned)

- **Task 3: co-occurrence redesign.** Fix the algorithm that forms `co_occurred` edges where it does
  not link the right facts. A temporal-query exemption for the gate is considered here.
