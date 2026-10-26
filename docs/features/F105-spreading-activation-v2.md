# F105: Spreading Activation v2

**Status:** in progress. Each change below ships behind its own setting whose default reproduces
today's behaviour exactly, and each is enabled only after its gates pass. Landed so far: the offline
replay used as the gate (`scripts/diag/sa_replay.py`, Task 1), which changes no product behaviour;
fan normalisation (Task 3), off by default (`NOUS_SPREADING_FAN_NORMALISATION_ENABLED`);
associative edges at a reduced weight (Task 4), off by default
(`NOUS_SPREADING_ASSOCIATIVE_EDGES_ENABLED`); a query-conditioned gate after the CTE (Task 5),
off by default (`NOUS_SPREADING_QUERY_GATE_ENABLED`); a budget on SA rows in the merged recall
list (Task 6), off by default (`NOUS_SPREADING_BUDGET_ENABLED`); and two more ways to combine the
activation several seeds give one node (Task 7), off by default (`NOUS_SPREADING_AGGREGATION=max`).
The Task 3 to 7 offline gates passed; no MAB arm has run yet.

**Builds on:** [F022 Graph-Augmented Recall](F022-graph-augmented-recall.md) (the density-gated
recursive CTE in `nous/brain/spreading_activation.py`) and F091 retrieval telemetry
(`nous_system.retrieval_log`; #807 records each SA row's winning seed).

## Goal

Make SA deliver relevant neighbours at a bounded context cost, through six independently flagged
changes, each with an offline gate (the replay) and a MemoryAgentBench (MAB) arm.

## Measured facts the plan is built on

From MAB replays under the prod config with SA forced on (2026-10-25/26):

- SA rows fill 16-24% of the rendered prompt on LongMemEval (LME), multi-hop QA (qa2) and detective,
  but delivered gold that no other leg found on only 4 of 60 LME and 4 of 100 qa2 questions.
- Low-ranked SA rows still reach the prompt: `recall_deep` is exempt from SmartCompress and delivers
  a median of about 76 items whatever its `limit`. SA rows are rendered, just low-ranked and
  unbounded, so a ranking change reorders them; it does not decide which rows get in.
- Conflict resolution (CR): SA returns 0 rendered rows. The plan put this down to a graph made mostly
  of `co_occurred` edges, which the CTE does not traverse. Task 4 measured otherwise on the A2 clone:
  the CR graphs are mostly chunk-to-chunk `related_to` edges, plus a small `co_occurred` clique
  among each agent's few facts, where the fact seeds start. `compute_graph_density` gives 2.2 to 2.7
  on the CR agents whether or not associative edges count, so under `auto` they stay under the 3.0
  density gate and SA does not run there, for a reason other than associative edges (Task 4 below).
- Prod resolves SA on through `auto` (density 3.86 > gate 3.0); `spreading_score_depth1_parity` is
  false.

## Planned changes

| Task | Change | Where |
|---|---|---|
| 0 | Test-retest rerun of the A2 arm, giving the noise floor every MAB arm is read against | no code |
| 2 | Depth-1 score parity: evaluate the existing `NOUS_SPREADING_SCORE_DEPTH1_PARITY` flag | config only |
| 3 | Fan (degree) normalisation inside the recursive CTE, so hubs stop dominating hop 2 | `spreading_activation.py` |
| 4 | Associative edges (`co_occurred`, `co_mention`) traversed at a reduced weight | `graph_constants.py`, `spreading_activation.py` |
| 5 | Query-conditioned gate after the CTE: scale a row by its relevance to the query, relative to its seed's | `spreading_activation.py`, `retrieval_pipeline.py` |
| 6 | A budget for SA rows in what reaches the prompt | `retrieval_pipeline.py` |
| 7 | Aggregation experiment (`max` today; capped sum and noisy-OR as arms) | `spreading_activation.py` |
| 8 | Re-score existing candidates with their SA activation (never demotes) | `spreading_activation.py`, `retrieval_pipeline.py` |

Out of scope: personalised PageRank, lateral inhibition, depth above 3, traversing `supersedes`,
`contradicts` or `happened_before`, an LLM in the traversal, edge learning before these changes, and
changing the floor or caps without a gate.

## Evaluation

Two gates per change. The **merge gate** is green tests (including a flags-off identity test) and
the offline gate; the code merges with its flag off. The **enable gate** is the MAB arm (LME 60 +
qa2 100), paired against A2 (SA on, prod config) and A2off (SA off), read against the Task 0 noise
floor. With 160 questions and at most 8 where SA is the only gold source, the arms catch regressions
and context savings; they cannot prove small accuracy gains.

## Offline replay (Task 1)

`scripts/diag/sa_replay.py` replays the seeds recorded in `retrieval_log.expansions` (post-#807
rows, which carry real `seed_type` and `seed_score`) through `spreading_activation_search` on a clone
database, once per arm. Every arm is built on the A2 arm's resolved config, read from its env file
by named keys only, with the developer's own `.env` and `NOUS_*` variables kept out. Per arm it
reports rows above the floor, Jaccard@20 against the baseline arm, hub share (rows whose winning
path leaves a node of degree >= 50) and latency p50/p95. Connections are read-only. The script
refuses, unless `--allow-prod-readonly` is passed:
- a DSN on the prod host, which `PROD_HOST` or `--prod-host` names;
- when neither is set, any host that is not loopback.

Limitations, written into every report:
- `expansions` only holds winning seeds, so replayed seed sets are a subset of the live ones.
- The live call also excluded the retrieval's other candidates; the replay excludes only the seeds,
  so a replayed window can hold rows the live window did not.
- It is a screen, not a measurement of answer quality.

Two options added in Task 4:
- `--seeds-from candidates` replays every retrieval whose `spreading_activation` leg was attempted,
  including those where spreading found nothing and so recorded no seed. It rebuilds the live seeds
  (the `brain` decisions and the top three `heart_primary` facts) and the live exclusion set (the
  candidates the call already had before Stage 4, plus the Path A neighbours) from
  `retrieval_log.candidates` and `expansions`, which removes both limitations above. On the A2
  clone's 462 retrievals with recorded SA rows it reproduced every live SA row and activation.
- `--edge-kinds` reports which kind of edge carried each kept row's last hop: `co_occurred`,
  `co_mention` or `other`. For each row it re-runs that one hop from the node the winning path left.

Hub share judges each row by the node its winning path's last hop left: its seed for a depth-1 row,
the via node for a depth-2 row (`spreading_activation_search`'s `parent_out`, added in Task 3).
Before Task 3 it used the winning seed's degree, which is exact for depth-1 rows only.

**Baseline (A2 clone, 2026-10-26):** 462 retrievals from 17 agents; all 831 replayed seeds are
facts, so the baseline exercises fact-seeded spreading only. 4,524 rows above the floor (9.8 per
retrieval); latency p50 2.2 ms, p95 5.6 ms. Every one of the 3,351 live SA rows came back with the
same activation and depth. Hub share by seed degree is 0.0, because no seed has degree 50 or more.
Three agents do have nodes of degree 718 to 878, but on this clone a hub can only be left at hop 2,
as the via node, which the seed-degree share cannot see. A hub-share gate therefore needs Task 3's
via-node output. By via node, the same baseline's hub share is 0.048 (Task 3 replay).

## Fan normalisation (Task 3)

Settings: `NOUS_SPREADING_FAN_NORMALISATION_ENABLED` (default `false`),
`NOUS_SPREADING_FAN_FREE_DEGREE` (D0, default 10) and `NOUS_SPREADING_FAN_EXPONENT` (gamma, default
0.5); see [environment variables](../reference/environment-variables.md).

With the flag on, each recursive step multiplies the activation leaving a node by its fan factor:
`1` when the node's degree `d <= D0`, else `(D0 / d) ^ gamma`. `d` is the node's eligible-edge degree:
edges in either direction, this agent's only, under the same eligibility predicate as the traversal
(one `edge_filter` predicate builds both, so Task 4 changes them together). The factor is `<= 1`, so
the MAX invariant `activation <= seed_score` holds. The degree is a `CROSS JOIN LATERAL` count placed
before the edge join in the recursive term. It is exact, needs no schema change and uses the source
and target edge indexes. It is meant to run once per frontier row, not once per joined edge, and
`EXPLAIN` on realistic graphs shows that plan (below). The planner may still choose to evaluate it
once per joined edge, which changes the cost only, never the value. A node reached by `k` paths pays
`k` counts. With the flag off, the statement is byte-identical to before; one snapshot,
`tests/fixtures/spreading_sql_default.sql`, checks that for every F105 flag.

Why `(D0 / d) ^ gamma` and not SYNAPSE's `1 / d`: prod degree is p50 5, p90 15, p99 47, max 567, so a
plain `1 / d` would shrink ordinary nodes 5x. Worked values (seed 0.8, w 1, decay 0.5, so a hub's
undamped hop-2 outflow is 0.2; floor 0.1):

| hub degree | gamma 0.5: factor / hop-2 | gamma 1: factor / hop-2 | pure (D0 1, gamma 1): factor / hop-2 |
|---|---|---|---|
| 41 | 0.494 / 0.0988 | 0.244 / 0.0488 | 0.0244 / 0.0049 |
| 567 | 0.133 / 0.0266 | 0.0176 / 0.0035 | 0.0018 / 0.0004 |

At gamma 0.5, hubs up to about degree 39 keep their hop-2 outflow above the floor at this seed score.
Degree 41 lands just under it, and degree 567 is effectively silenced. At gamma 1, every hub of
degree 20 or more is silenced (degree 20 lands exactly on the exclusive floor). Hop-1 outflow from a hub seed is damped by the same factor.

**Parity caveat.** With fan on, a depth-1 row under `NOUS_SPREADING_SCORE_DEPTH1_PARITY` scores
`seed x w x factor(seed)`, while the 1-hop leg scores the same triple `seed x w`. For a seed of
degree above D0 the two differ, so that flag's "exact parity" statement stops holding for hub seeds.

**Replay output.** `spreading_activation_search` takes an optional `parent_out`: the node each
row's winning path left (a seed maps to itself). The CTE carries the extra column only when it is
given, and the winning path is chosen as before. Only the replay uses it.

**Offline gate (A2 clone, 2026-10-26).** 462 replayed retrievals from 17 agents, all fact-seeded,
under the A2 base config; two runs agreed. Kept rows are the top 20 above the floor. The baseline arm
reproduced Task 1's baseline on every retrieval.

| arm | hub share (via node) | Jaccard@20 vs baseline | rows above floor | added latency p50 / p95 |
|---|---|---|---|---|
| baseline | 0.048 | 1.00 | 4,524 | - |
| fan (D0 10, gamma 0.5) | 0.0 | 0.90 | 4,281 | 0.7 / 3.4 ms |
| fan_g1 (gamma 1) | 0.0 | 0.90 | 4,152 | 0.7 / 3.5 ms |
| fan_pure (D0 1, gamma 1) | 0.0 | 0.26 | 784 | 0.7 / 3.5 ms |

The default arm passes: hub share falls by 100% (gate: at least 50%), Jaccard@20 is 0.90 (gate: at
least 0.5) and added p95 latency is 3.4 ms (gate: at most 50 ms). `fan_pure` fails the Jaccard gate:
it silences most hop-2 rows, and 125 of 462 retrievals keep no row above the floor. On this clone
the baseline's hub rows are few (4.8% of kept rows), so a full drop is easy to reach. Every replayed
seed has degree 14 or less, so the hub-seed case below is outside the replayed distribution.

`EXPLAIN (ANALYZE)`: the lateral's count runs once per frontier row on realistic graphs, the
`depth < max_depth` filter sits on the work-table scan so last-hop rows pay no count, and both
counts use the source and target edge indexes. The slowest replayed seed set took 0.19 ms warm
without fan and 0.24 ms with it. The worst case measured is a degree-878 hub as the seed on a dense
chunk graph of a benchmark clone (228k CTE rows). It took about 275 ms warm without fan and about
350 ms with it, so +70 ms in all, from one count per hub neighbour. If prod's hub seeds show that
cost, the planned contingency is a TTL-cached list of nodes above D0 passed as a parameter; the factor
function stays the same. On a very small graph (the 41-edge test fixture) the planner joins the edges
first and runs the count once per joined edge; the results are the same and the cost there is
negligible.

**MAB arm F** (`NOUS_SPREADING_FAN_NORMALISATION_ENABLED=true`, defaults): not run yet.
Pre-registered sign: the SA share of rendered characters falls (fewer rows clear the 0.1 floor), and
accuracy stays within 1 question.

## Associative edges at a reduced weight (Task 4)

Settings: `NOUS_SPREADING_ASSOCIATIVE_EDGES_ENABLED` (default `false`) and
`NOUS_SPREADING_ASSOCIATIVE_WEIGHT_MULT` (default 0.5, bounded `(0, 1]`); see
[environment variables](../reference/environment-variables.md).

Associative edges are those with relation `co_occurred`, or extraction method `co_mention`
(relation `related_to`). The traversal used to refuse them through the same predicate as the density
gate. Task 4 gives the traversal its own predicate, `graph_constants.spreading_exclusion_sql`. With
the flag off it returns exactly `autobehavior_exclusion_sql`, so the statement is byte-identical to
before. With the flag on it excludes only `supersedes`, `contradicts` and `happened_before`
(`SPREADING_ALWAYS_EXCLUDED_RELATIONS`, report §7), and each hop over an associative edge is
multiplied by the setting (`CASE WHEN <associative_edge_sql> THEN :assoc_mult ELSE 1.0 END`). The
multiplier is `<= 1`, so the MAX invariant `activation <= seed_score` holds. At the defaults, seed 0.8
over a 0.9 `co_mention` edge gives `0.8 x 0.9 x 0.5 (decay) x 0.5 = 0.18`, and over a 0.8
`co_occurred` edge it gives 0.16. A test pins the partition: every relation in
`AUTOBEHAVIOR_EXCLUDED_RELATIONS` is either always excluded or associative, so a relation added to
the auto-behavior set alone fails a test rather than silently becoming traversable or not.

The fan degree count (Task 3) uses the same predicate as the traversal, so with both flags on, a
node's associative edges count toward its degree.

`compute_graph_density`, orphan detection and cluster discovery keep `autobehavior_exclusion_sql`
and read no settings. **Consequence:** under `auto` (prod's mode), `compute_graph_density` does not
count `co_occurred` or `co_mention` edges. A graph made mostly of them therefore stays under the 3.0
gate, spreading does not run, and this flag is inert there unless `NOUS_SPREADING_ACTIVATION_ENABLED`
is `true`. Making the flag matter on such graphs needs a separate decision about the density gate.
That is out of scope here, because the gate is deliberately builder-flag-proof.

**Offline gate (A2 clone, 2026-10-26).** Kept rows are the top 20 above the floor, as in Task 3.

(a) Conflict resolution (CR), replayed live-shaped (`--seeds-from candidates --edge-kinds`): 1,186
retrievals that attempted spreading, from 7 CR agents (the eighth holds no facts, so spreading never
ran there).

| arm | retrievals with rows above floor | rows above floor | carried by `co_occurred` / `co_mention` / other | Jaccard@20 vs baseline | added latency p95 |
|---|---|---|---|---|---|
| baseline | 18 | 18 | 0 / 0 / 18 | 1.00 | - |
| associative (mult 0.5) | 188 | 203 | 203 / 0 / 0 | 0.85 | 1.1 ms |
| associative (mult 1.0) | 188 | 203 | 203 / 0 / 0 | 0.85 | 1.1 ms |

- The baseline's 18 rows are the 18 SA rows the CR run recorded. All come from one agent's four
  inferred fact-to-fact edges, so "SA returns 0 rows on CR" holds for 1,168 of the 1,186 retrievals.
- Associative-carried rows go from 0 to 203, in 188 retrievals. All are depth 1, with activation
  0.197 to 0.225 (seed score x 0.9 x 0.5 x 0.5). They come from 17 distinct edges on two agents.
- On this clone the CR graphs are mostly chunk-to-chunk `related_to` edges. The fact seeds start
  from a different subgraph: each CR agent's facts (4 to 9) come from one source episode, and their
  only edges are a complete `co_occurred` clique. The clique is complete, so every reachable fact is
  one hop away, and the multiplier changes no row set.
- The live exclusion matters. Excluding only the seeds, 930 retrievals would gain 2,525 rows; 2,322
  of those neighbours were already candidates of the same retrieval. In 742 retrievals all of them
  were, including every retrieval of the two other agents that have a clique. The live-shaped number
  is the gate.

(b) LME and qa2: Jaccard@20 against baseline is 1.00 in both seed modes (102 and 119 retrievals from
recorded seeds, 108 and 120 from candidates). This is vacuous: neither agent has an associative
edge, so the flag cannot change anything there. The other agents of the Task 3 window give a
non-vacuous reading; six of them have 1 to 6 associative edges. There Jaccard@20 is 0.985
live-shaped (15 agents, 299 retrievals: 15 associative-carried rows in 13 retrievals) and 0.90 with
the seeds-only exclusion (14 agents, 223 retrievals). Over all 17 agents with recorded seeds, with
the seeds-only exclusion, it is 0.93. Added latency p95 is 1.0 to 1.2 ms in every run.

The gate passes: CR has associative-carried rows above the floor on 188 retrievals (gate: more than
0), and LME/qa2 Jaccard@20 is 1.00 (gate: at least 0.6). Hub share is computed from the degrees of
the edges the default traversal crosses, so under the associative arm it leaves out associative
edges; it is not a gate metric for this task.

**Provenance (report open question 7, this clone only).** 20 associative-carried CR rows were
sampled (seed 4) and their edges' metadata read. All 20 are `co_occurred` edges with method
`co_occurrence`, weight 0.9 and fact-to-fact, on two agents (12 and 8 rows). Each agent's
`co_occurred` edges share one `created_at`, 1.4 s and 3.7 s after that agent's first other edge,
inside the same July ingest. Across the CR agents, those with 4 to 6 facts carry the full clique (6,
10 or 15 edges) and those with 8 or 9 facts carry none. That split is the builder's noise gate
(`NOUS_COOCCURRENCE_MAX_EPISODE_FACTS`, default 6), not a flag state. So nothing on this clone
suggests these edges were built under a different builder flag state. The question stays open for
prod's 1,263 `co_occurred` and 3,294 `co_mention` edges, which this audit does not cover.

**Density gate on CR.** `compute_graph_density` gives 2.22 to 2.67 on the CR agents. Counting their
associative edges would give 2.22 to 2.67 as well, because they are at most 15 of 136 to 2,364
edges. So on these graphs `auto` keeps spreading off whatever this flag says, for a reason other
than associative edges. The CR arm below forces spreading on, as the measured CR fact was taken.

**MAB arms** (not run yet): A, LME + qa2 with the flag on, read against the default rule; B, a CR arm
with `NOUS_SPREADING_ACTIVATION_ENABLED=true` and the flag on, the only task that gets one.
Pre-registered sign: CR SA rows go from 0 to more than 0 and CR accuracy does not fall; LME + qa2
within the default rule.

## Query-conditioned gate (Task 5)

Settings: `NOUS_SPREADING_QUERY_GATE_ENABLED` (default `false`), `NOUS_SPREADING_QUERY_GATE_MODE`
(`seed_relative`, `quantile` or `raw`; default `seed_relative`), `NOUS_SPREADING_QUERY_GATE_SEED_MIN`,
and the `quantile` anchors `NOUS_SPREADING_QUERY_GATE_Q_LO` / `_Q_HI` (defaults 0 and 1, which make
`quantile` the raw clamp); see [environment variables](../reference/environment-variables.md).

The CTE scores a row by graph structure only, so a neighbour reached through a strong edge keeps a
high activation however unrelated it is to the query. The gate runs in Stage 4 of
`retrieval_pipeline.py`, after `spreading_activation_search` and before the activation floor and the
result cap. With the flag on, Stage 4 embeds the query once (`brain.embeddings`, the provider Heart
shares, so in practice an LRU hit after the chunk or exemplar leg), reads
`cos = 1 - (embedding <=> query)` for each row and for its winning seed
(`spreading_activation.query_cosines`: one agent-scoped query per node type over `brain.decisions`,
`heart.facts`, `heart.episodes`, `heart.procedures` and `heart.episode_chunks`), and multiplies each
row's activation by `gate_factor`:

- `seed_relative` (primary): `min(1, max(cos_node, 0) / max(cos_seed, s_min))`. A node as relevant
  to the query as the seed that activated it keeps its full activation. A seed whose cosine is below
  `s_min`, zero or negative included, is divided by `s_min`, so its rows are still gated.
- `quantile` (MAB arm G-B): `clamp((cos_node - q_lo) / (q_hi - q_lo), 0, 1)`.
- `raw`: `max(cos_node, 0)`, the paper's form, kept to show why it is not the default.

The factor is in [0, 1], so the gate never raises a score. The floor and the cap see the gated
value. The rows are re-sorted by it (stably, so ties keep the CTE's order) before the cap takes its
20, so a row the gate demotes gives up its cap slot. The F091 expansion row records the raw activation
as `path_strength` and the gated one as `edge_weight`. No LLM is involved; the query embedding is one
call, and the cosines one query per node type present.

**Missing embeddings.** All five node tables have a nullable `embedding`: decisions recorded without
an embedder, chunks mid-backfill and procedures can be NULL. A NULL node or seed embedding gets factor
1.0, because absence is not a relevance value; the count goes to a `logger.debug` line. A missing
embedder or an empty query vector skips the gate for the whole call. Any failure leaves the rows
ungated and counts `n_stage_errors["spreading_query_gate"]`: the gate fails open and never turns
spreading into the 1-hop fallback. Neither does a gate that prunes every row: spreading stays in use
and returns no row, and the 1-hop fallback does not run. That fallback exists for rows that clear
the floor but fail content resolution, which still fall back as before. After a full prune it would
re-add the depth-1 part of the same rows, ungated and under the `brain_graph` /
`heart_graph_memory` labels, and so undo the gate. A gate that skipped or failed pruned nothing, so
when the raw rows were all below the floor anyway, the fallback runs as it does with the flag off. In
the offline gate below, a full prune is 21 of 462 retrievals under `seed_relative`.

**Over-fetch.** The CTE's 40-row window (`_SPREADING_OVERFETCH_LIMIT`) is cut by raw activation
before the gate runs. The gate can only lower or reorder rows inside that window; it cannot bring in
a row the window left out. That is left as is.

**Offline gate (A2 clone, 2026-10-26).** `sa_replay.py --embed-queries`, baseline arm under the A2
base config. Queries are embedded with the base config's model, `text-embedding-3-large` at 1536
dimensions. Re-embedding 3 stored chunks and 3 stored facts with it gave a cosine of 1.0 against
their stored vectors, so queries and nodes share one space. The primary window is Tasks 1 and 3's: 462
retrievals from recorded seeds, 17 agents. It has 4,524 rows above the floor (3,982 chunks, 510
facts, 32 procedures), counted over the CTE window before the result cap, as in Tasks 1 to 4. Every
row and every one of the 831 seeds has an embedding, so the neutral case never fires here.

| decile | 0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `cos(node, q)`, 4,524 rows | -0.050 | 0.061 | 0.093 | 0.122 | 0.156 | 0.196 | 0.242 | 0.283 | 0.325 | 0.372 | 0.579 |
| `cos(seed, q)`, 831 seeds | -0.002 | 0.089 | 0.113 | 0.134 | 0.156 | 0.186 | 0.217 | 0.259 | 0.296 | 0.356 | 0.608 |
| `seed_relative` factor | 0.0 | 0.314 | 0.461 | 0.576 | 0.680 | 0.765 | 0.844 | 0.926 | 1.0 | 1.0 | 1.0 |

The parameters come from these histograms:
- `s_min` is the median seed cosine, 0.1859, so 0.19. It is now the setting's default.
- Arm B's anchors are `q_lo` = the 10th percentile of the node cosines, 0.0605, so 0.06, and `q_hi` =
  the median seed cosine, 0.19.

These values hold for this embedding model only; another model needs its own measurement. Under
`seed_relative`, 22.6% of rows keep factor 1 (at least as relevant as their seed) and 1.1% get 0.

The pruned share is also given per tercile of each row's winning-seed cosine. The terciles are cut at
0.155 and 0.285, giving 1,508, 1,510 and 1,506 rows.

| mode | rows above floor, before / after | pruned | pruned per seed-cosine tercile, low / middle / high | retrievals keeping a row | mean `cos(node, q)`, kept / pruned |
|---|---|---|---|---|---|
| `seed_relative` (s_min 0.19) | 4,524 / 3,236 | 28.5% | 50.7% / 17.0% / 17.7% | 441 of 462 | 0.249 / 0.105 |
| `quantile` (0.06, 0.19) | 4,524 / 3,140 | 30.6% | 68.8% / 20.8% / 2.2% | 424 | 0.267 / 0.075 |
| `raw` | 4,524 / 311 | 93.1% | 100% / 96.4% / 82.9% | 135 | 0.393 / 0.194 |

Against the pre-registered windows:
- `seed_relative` passes. It prunes 28.5% (window 10-40%). Every tercile is inside 5-70% and none is
  above 60%. Kept rows have the higher mean cosine.
- `quantile` passes. It prunes 30.6% (window 20-60%), and kept rows have the higher mean cosine. Its
  tercile split would fail the rule the plan sets for `seed_relative`: it gates on the node's
  absolute cosine, so it prunes almost only the rows of weak seeds.
- `raw` prunes 93.1%, above 60% as expected, which is why it is not the default.

**Live-shaped check.** The same 17 agents were also replayed with `--seeds-from candidates`, which
Task 4 showed reproduces the live SA rows. That gives 786 retrievals that attempted spreading, 462 of
them with rows above the floor: 3,570 rows (3,327 chunks) and 2,356 seeds (23 of them decisions).
Under the primary's parameters:
- `seed_relative` prunes 34.2% (terciles 57.1% / 22.9% / 22.2%), with kept rows at mean cosine 0.252
  and pruned rows at 0.107;
- `quantile` prunes 34.3% (73.0% / 26.6% / 2.8%);
- `raw` prunes 95.9%.

This replay's own median seed cosine is lower, 0.146. It counts every live seed, including those
of retrievals where spreading found nothing, while the expansions record only winning seeds. With `s_min` 0.15, `seed_relative` prunes 28.9% (43.1% /
21.0% / 22.2%). Both readings pass the `seed_relative` windows. At the primary's parameters, though,
the low tercile (57.1%) is close to the 60% mark at which the plan says a gate must be re-examined.

**MAB arms** (not run yet): G, `NOUS_SPREADING_QUERY_GATE_ENABLED=true` (`seed_relative`, `s_min`
0.19 by default); G-B, also `NOUS_SPREADING_QUERY_GATE_MODE=quantile`,
`NOUS_SPREADING_QUERY_GATE_Q_LO=0.06` and `NOUS_SPREADING_QUERY_GATE_Q_HI=0.19`. Arm G-B's anchors
fail the 70% tercile line under the live-shaped replay: its low tercile is pruned 73.0% there. That
line is set for `seed_relative`; G-B is run as the comparison arm. Pre-registered sign: SA rendered
characters fall, and of the Task 0 SA-only-gold union (4 questions), the arm passes at 3 of 4 or
more.

## Budget for SA rows (Task 6)

Settings: `NOUS_SPREADING_BUDGET_ENABLED` (default `false`), `NOUS_SPREADING_BUDGET_MAX_ROWS`
(default 5, bounded `[0, 20]`) and `NOUS_SPREADING_BUDGET_CUTLINE_RANK` (default 0, no cutline); see
[environment variables](../reference/environment-variables.md).

**Why on the merged list.** Stage 4 caps spreading at 20 rows (`_SPREADING_RESULT_CAP`) before the
merge, and `recall_deep` delivers the whole merged list: it has no `[:limit]` slice and is exempt
from SmartCompress. A bound on how many SA rows reach the prompt therefore has to act on the merged
list. The script path's `_apply_script_limit` slices the list again afterwards; the budget only frees
slots there.

**What it does.** `_apply_spreading_budget` in `retrieval_pipeline.py` runs in
`run_recall_pipeline` after the `rerank_by_score` sort and the F071 filter, and before the F044
recall-reactivation read. SA rows are those with `source == "spreading_activation"`.
1. With a cutline rank k > 0, the cutline is the k-th best non-SA score, and there is none when the
   list has fewer than k non-SA rows. An SA row strictly below it is dropped; one equal to it is
   kept.
2. Of the remaining SA rows, the `max_rows` best by score are kept. The sort is stable, so on a tie
   the earlier row in the list is kept.

Non-SA rows and the order of the survivors are untouched. The placement matters on both sides:
after F071, so a row F071 removes takes no slot; before the reactivation read, so a cut row, which
never reaches the model, is not reinforced as co-retrieved. The budget selects by score, so the sort
before it does not change which rows survive. The pre-turn context path does not run Stage 4, so it
has no SA rows (none in the A2 clone's 756 context retrievals).

**Telemetry.** Each cut row gets the disposition `budget_truncated` at stage `spreading_budget`. The
`spreading_activation` leg keeps `n_returned` as Stage 4 produced it and reports the cut in
`n_dropped`. The count goes on the leg's existing summary call, which runs only when spreading was
attempted, so the budget cannot create a leg on a retrieval where spreading did not run. The F091
expansion rows are unchanged.

**Offline gate (A2 and A2r clones, 2026-10-26).** For the Task 0 SA-only-gold union (qa2 no13,
no38 and no59; LME 9d25d4e0), the benchmark operator exported each retrieval whose SA expansions
carried the gold row, with the gold row's rank by `edge_weight` among that retrieval's SA
expansions. `edge_weight` is the activation the pipeline used. With depth-1 parity off, an SA row
scores activation x 0.7 in both arms, so the rank by `edge_weight` is the rank the budget uses. The
ranks were re-checked against the SA rows each retrieval delivered, which are the rows the budget
sees: every gold row was delivered, and the ranks agree.

| question | A2: gold rank / SA rows | A2r: gold rank / SA rows |
|---|---|---|
| qa2 no13 | 5 / 5 | 5 / 5 |
| qa2 no38 | 4 / 4 | 4 / 4 |
| qa2 no59 | 4 / 4 | 4 / 4 |
| LME 9d25d4e0 | gold not delivered by SA | 4 / 8 (tied with one other row) |

Under the union rule each question is read from an arm where SA delivered its gold: the qa2 three
from A2 (A2r gives the same ranks), LME 9d25d4e0 from A2r, since in A2 its gold did not come through
SA. Per arm:

| arm | qa2 no13 | qa2 no38 | qa2 no59 | LME 9d25d4e0 | verdict |
|---|---|---|---|---|---|
| B5 (`max_rows` 5, no cutline) | kept (5 <= 5) | kept (4) | kept (4) | kept (4) | 4 of 4, pass |
| B5C (`max_rows` 5, cutline rank 20) | cut: 0.091 < 0.729 | cut: 0.095 < 0.676 | cut: 0.085 < 0.729 | cut: 0.105 < 0.813 | 0 of 4, fail |

The B5C cells give the gold row's score against that retrieval's 20th-best non-SA score. The gate
asks for at least 3 of 4. B5 passes with every gold inside the cap, and B5C cuts all four, as its
pre-registered prediction says. The margin is thin:
- At `max_rows` 4, no13 is cut, since its gold is the 5th of 5 SA rows in both arms. That leaves 3 of
  4 under the union, exactly the threshold, and 2 of 4 in A2 alone.
- LME's gold survives a cap of 4 only on its tie. It shares rank 4 with another row and comes first
  in the list, so the stable sort keeps it.
- At `max_rows` 3, all four are cut.
- The three qa2 questions are carried by the same gold chunk of one agent, so the gate rests on two
  distinct rows.

B5C's cut is not close: the gold rows score 7 to 9 times below the cutline.

**Context saving (estimate).** For each retrieval with SA rows, the rows kept are the top
`max_rows` of its delivered SA rows by `edge_weight`. Characters are the content length of the
delivered rows: fact and chunk content, episode summary, procedure and decision description. A2 has
462 such retrievals, A2r 236.

| `max_rows` | A2: SA rows per retrieval, median | A2: SA characters | A2: SA share of delivered characters | A2r: SA characters | A2r: SA share |
|---|---|---|---|---|---|
| off | 6 (mean 6.7) | - | 16.5% | - | 18.3% |
| 8 | 6 | -23% | 13.3% | -16% | 15.9% |
| 5 | 5 (mean 4.0) | -42% | 10.3% | -37% | 12.4% |
| 4 | 4 | -52% | 8.7% | -47% | 10.6% |
| 3 | 3 | -63% | 6.9% | -59% | 8.4% |

Counted per question instead (unique rows across a session's retrievals, the convention of the arm
comparisons), `max_rows` 5 gives -40% on A2 and -35% on A2r, and `max_rows` 4 gives -49% and -46%.
At `max_rows` 5, all delivered characters fall by 7.0% on A2 and 6.7% on A2r. Against arm B5's
pre-registered sign (SA rendered characters down at least 50% against A2), this estimate predicts a
miss at `max_rows` 5. Getting to about 50% takes `max_rows` 4 (-52% per retrieval, -49% per
question), where the gold gate above passes only at its threshold, 3 of 4 under the union.

## Aggregation across seeds (Task 7)

Setting: `NOUS_SPREADING_AGGREGATION` (`max`, `sat` or `noisy_or`; default `max`); see
[environment variables](../reference/environment-variables.md).

The CTE runs one set of paths per seed. A node reached from several seeds keeps only the strongest
seed's best path under `max`, so two seeds pointing at one node give it no more activation than one
does. Task 7 adds two ways to combine them:
- `sat` (saturating sum): `min(1, sum of a_s)`.
- `noisy_or` **[NOVEL]**: `1 - product of (1 - a_s)`. No paper fetched for F105 uses it (report
  §7.3), so it is a hypothesis to test here and in the MAB arms, not a replication.

`a_s` is seed `s`'s best path to the node. A `per_seed` CTE keeps one row per (node, seed):
`ROW_NUMBER() OVER (PARTITION BY id, node_type, seed_id ORDER BY activation DESC, depth ASC)`, rn 1.
Parallel edges between two nodes and cycles through a seed's own paths therefore never count twice.
There is no uncapped sum (report §7). Fan normalisation (Task 3) and the associative multiplier
(Task 4) scale each path before this reduction, so the modes combine already-damped values.

The `ranked` CTE then computes the combination as a window over the node. It keeps the row of the
largest contributor, ordered as under `max` (activation desc, depth asc, seed id asc), so the returned
depth, `seed_out` and `parent_out` all describe that one best path; `parent_id` is never aggregated.
The where-clause conditions (exclusions, refused decisions, strategy cards, job lessons), the `LIMIT`
and `ORDER BY total_activation DESC` are as before. Under `max` the statement is byte-identical to
before (the snapshot test).

**Bounds.** Both modes stay `<= 1`. noisy-OR caps each `a_s` at 0.999999 (and floors `1 - a_s` at
1e-6), so `LN` is always defined. A lone path at 1.0 therefore returns 0.999999, and five paths at 1.0
return 1.0 in a double. The invariant `activation <= winning seed's score`, which Tasks 3 and 4 keep,
now holds per path but not per row: two seeds at 0.3 can give a node 0.6.

**Parity caveat.** With `NOUS_SPREADING_SCORE_DEPTH1_PARITY` on, a row scores `activation / decay`.
Under `sat` or `noisy_or` a multi-seed row can then score above 1, up to `1 / decay`. Parity is off in
prod and in A2.

**Offline gate (A2 clone, 2026-10-26).** The definitions, the signs forced by construction and rough
sizes were pre-registered in the task report before the replay. The plain replay
(`sa_replay.py --agent all`, the window of Tasks 3 to 5) gives rows above the floor, Jaccard@20 and
latency, for both seed sources. A separate analysis script (not committed) also replays each
retrieval uncapped under each mode and from each seed alone. That counts each node's seeds and
compares the SQL's values with a Python combination of the per-seed best paths. Over 149,967 and
142,257 checked rows there was no difference above 1e-9, and the reported seed and depth were always
the largest contributor's.

Definitions:
- multi-seed row: reached with positive activation from 2 or more seeds;
- floor-crosser: its `max` value is at or below the 0.1 floor and its value under the mode above it;
- rank movement: the row's rank under `max` minus its rank under the mode, so positive is up. Ranks
  are among the retrieval's rows above the floor, uncapped, by activation (ties by id);
- kept: the first 20 rows above the floor of the 40-row window, which is what Stage 4 appends.

| | recorded seeds (`expansions`) | live-shaped (`candidates`) |
|---|---|---|
| retrievals (with 2 or more seeds) | 462 (291) | 1,574 (1,533), 462 with rows above the floor |
| rows above the floor under `max` | 4,524 | 3,570 |
| of which multi-seed | 676 (14.9%), in 139 retrievals | 522 (14.6%), in 154 retrievals |
| nodes reached from 2 or more seeds, any activation | 864 of 49,989 | 718 of 47,419 |

| | `sat`, expansions | `noisy_or`, expansions | `sat`, candidates | `noisy_or`, candidates |
|---|---|---|---|---|
| rows above the floor | 4,712 | 4,712 | 3,766 | 3,766 |
| floor-crossers (share of rows above the floor) | 188 (4.0%) | 188 (4.0%) | 196 (5.2%) | 196 (5.2%) |
| floor-crossers among kept rows | 124 (2.9%) | 123 (2.9%) | 165 (4.8%) | 162 (4.7%) |
| multi-seed rows: rank up / same / down | 424 / 87 / 165 | 405 / 104 / 167 | 286 / 179 / 57 | 271 / 195 / 56 |
| multi-seed rows: rank movement, mean / median | +2.5 / +2 | +2.2 / +1 | +2.3 / +1 | +2.0 / +1 |
| rows entering the kept 20 (all multi-seed) | 171, in 80 retrievals | 170, in 80 | 196, in 79 | 193, in 78 |
| Jaccard@20 against `max` | 0.971 | 0.971 | 0.989 | 0.989 |
| added latency p95 | 1.1 ms | 1.3 ms | 0.8 ms | 0.8 ms |

What the numbers show:
- Every node reached from 2 or more seeds clears the floor under either mode; the weakest two-seed
  sum is 0.109. The replayed seeds score 0.79 to 1.0, and the nodes two seeds share sit on strong
  edges. So every floor-crosser is a multi-seed row, and `sat` and `noisy_or` admit the same rows.
  They differ only in order: their kept-row counts differ by one to three per seed source.
- A single-seed row never moves up, as the construction forces. Under `sat`, 28% (recorded seeds)
  and 30% (live-shaped) of them move down; the mean over all single-seed rows is -0.8 places.
- On recorded seeds, LME holds 367 of the 676 multi-seed rows and 84 of the 188 floor-crossers. qa2
  has 34 multi-seed rows and no floor-crosser, so on qa2 these modes can only reorder a few rows.
  Ten of the 17 agents have a multi-seed row, eight a floor-crosser.

Against the pre-registration:
- The multi-seed share is inside its band on recorded seeds (10-35%). The live-shaped share is no
  higher (14.6%), which is under its predicted band (20-50%).
- Rank movement is inside its bands.
- The floor-crosser share sits at the lower edge (4.0%) and is higher live-shaped (5.2%), as
  predicted.
- Jaccard@20 is above the predicted bands (0.97 and 0.99, against at most 0.97): the modes change
  the kept rows less than expected.

The gate passes as a screen. The implementation check is exact, `max` reproduces the baseline
(Jaccard@20 1.00, 4,524 rows), and multi-seed rows are 14.6% to 14.9% of the rows above the floor. Had
that share been under 1%, the MAB arms would have measured next to nothing.

**MAB arms** (not run yet): `sat` and `noisy_or`, each the combined arm plus this one flag, paired
against the combined arm (not A2), so the added flag is the only variable.
