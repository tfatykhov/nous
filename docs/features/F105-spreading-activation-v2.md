# F105: Spreading Activation v2

**Status:** plan. No change to spreading activation (SA) has landed. Each change below ships behind
its own setting whose default reproduces today's behaviour exactly, and each is enabled only after
its gates pass. The one piece of code so far is the offline replay used as the gate
(`scripts/diag/sa_replay.py`, Task 1), which changes no product behaviour.

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
- Until Task 3 adds the via node of each path, hub share judges every row by its winning seed's
  degree. That is exact for depth-1 rows only.

**Baseline (A2 clone, 2026-10-26):** 462 retrievals from 17 agents; all 831 replayed seeds are
facts, so the baseline exercises fact-seeded spreading only. 4,524 rows above the floor (9.8 per
retrieval); latency p50 2.2 ms, p95 5.6 ms. Every one of the 3,351 live SA rows came back with the
same activation and depth. Hub share by seed degree is 0.0, because no seed has degree 50 or more.
Three agents do have nodes of degree 718 to 878, but on this clone a hub can only be left at hop 2,
as the via node, which the seed-degree share cannot see. A hub-share gate therefore needs Task 3's
via-node output.
