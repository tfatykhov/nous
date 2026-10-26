# F106: Associative Edges v2

**Status:** in progress. Task 1 (a query-relevance gate for Path A neighbours reached through an
associative edge, and an associative-edge multiplier in the neighbour fetch order) is built, with
every setting off or neutral by default, so recall is unchanged until it is enabled. Tasks 2 and 3
are planned once Task 1's evaluation is in.

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
`path_strength` is unchanged.
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

## Tasks 2 and 3 (planned)

- **Task 2: co-mention builder v2.** Revisit how `co_mention` edges are created. Its starting point
  is the pairwise result above: pairs on a rare shared entity are as precise as cosine edges.
- **Task 3: co-occurrence redesign.** Fix the algorithm that forms `co_occurred` edges where it does
  not link the right facts. A temporal-query exemption for the gate is considered here.

Both are planned after Task 1's evaluation.
