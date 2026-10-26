# F105: Spreading Activation v2

**Status:** in progress. Each change below ships behind its own setting whose default reproduces
today's behaviour exactly, and each is enabled only after its gates pass. Landed so far: the offline
replay used as the gate (`scripts/diag/sa_replay.py`, Task 1), which changes no product behaviour,
and fan normalisation (Task 3), off by default (`NOUS_SPREADING_FAN_NORMALISATION_ENABLED`); its
offline gate passed and its MAB arm has not run yet.

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
- Conflict resolution (CR): SA returns 0 rendered rows, because that graph is mostly `co_occurred`
  edges, which the CTE does not traverse. Under `auto`, `compute_graph_density` excludes the same
  edges, so a CR-like graph stays under the 3.0 density gate and SA never runs there.
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
