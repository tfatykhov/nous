# Edge-precision audit — live prod (`nous-default`), 2026-06-13

**Source data:** `reports/edge-audit-20260613-232013.md` (raw) +
`reports/edge-audit-20260613-232013.json`. Harness: `nous_eval.run_edge_audit`
(Sonnet YES/WEAK/NO judge, 30 edges/relation random sample, content hydrated
from the densifier's `_ENTITY_CONFIG` via #354). Run **read-only against live
prod** (`192.168.1.141:5432/nous`, `NOUS_EVAL_AGENT_ID=nous-default`), so it
includes the current graph: the just-backfilled `supersedes` edges,
`co_occurred` (256 live), and F067/F070 chunk edges the April audit never saw.

Precision = YES / (YES + WEAK + NO). Gate floor 0.75; N-floor 15 for a powered
verdict.

## Result

| relation | n | YES | WEAK | NO | precision | gate | what it is |
|----------|---|-----|------|----|-----------|------|------------|
| summarized_by | 30 | 29 | 1 | 0 | **0.97** | PASS | chunk→fact (F070) |
| related_to | 30 | 27 | 3 | 0 | **0.90** | PASS | same-type associative (F040) |
| supersedes | 30 | 27 | 0 | 3 | **0.90** | PASS | lineage (F027/#518, backfilled) |
| informed_by | 29 | 23 | 3 | 3 | **0.79** | PASS | decision←procedure |
| evidence_for | 30 | 21 | 8 | 1 | **0.70** | FAIL | fact→decision (cross-type) |
| co_occurred | 30 | 20 | 5 | 5 | **0.67** | FAIL | Gap-1 co-occurrence |
| extracted_from | 30 | 13 | 10 | 7 | **0.43** | FAIL | fact←episode provenance |
| happened_before | 30 | 8 | 4 | 18 | **0.27** | FAIL | **F075 temporal** |
| caused_by | 2 | 0 | 2 | 0 | 0.00 | underpowered | — |
| discussed_in | 2 | 1 | 1 | 0 | 0.50 | underpowered | — |
| part_of | 1 | 1 | 0 | 0 | 1.00 | underpowered | — |

## vs the 2026-04-30 baseline (`reports/edge-audit-20260430-032612.md`)

- `related_to` **0.70 → 0.90** (+0.20). The April FAIL was the empty-decision-
  content artifact; #354 (judge reads `description` not NULL `context`) plus the
  F040/F054 backfill maturing lifted it cleanly. **This invalidates the
  2026-06-13 analysis's "graph backbone is low-precision" premise** — the
  backbone was never measured against corrected content.
- `informed_by` **0.70 → 0.79** (+0.09): now PASS, same cause.
- `evidence_for` **0.75 → 0.70** (−0.05): the empty-content NOs are gone, but
  wrong-direction / tangential WEAKs replaced them (8 WEAK / 30). Cross-type
  fact→decision linking is genuinely the soft cross-type spot, not an artifact.
- `supersedes` now powered (n=30) at **0.90** — validates the #518 + backfill
  lineage edges as real, not mechanical noise.

## Verdict: does graph retrieval earn its token cost?

**The associative/structural backbone earns it; the cross-type/provenance/
temporal edges do not yet.** Split the graph into two populations:

1. **High-precision, retrieval-load-bearing (0.79–0.97):** `related_to`,
   `summarized_by`, `supersedes`, `informed_by`. These are most of the edges a
   recall actually traverses. The "edge precision is the graph's problem"
   hypothesis from the code-only analysis is **falsified for this population.**
   The real ceiling on these is *ranking* (graph 0.70 flat-score rarely clears
   the RRF-normalized vector head into top-K), not edge quality — consistent
   with every prior "graph rarely reaches top-K" finding. Re-ranking them up
   regresses (3e spike), so the lever here is the score-space work, not more
   edges.

2. **Low-precision (0.27–0.70):** `happened_before`, `extracted_from`,
   `co_occurred`, `evidence_for`. These are where "improve edge precision"
   actually applies.

### The load-bearing finding: `happened_before` = 0.27 (F075 temporal)

The F075 temporal edges are **mostly noise** (18/30 NO). This is the same edge
type behind the deferred "flip temporal flags + re-measure on BEAM" bet. Two
direct consequences:

- **Temporal edges are not retrieval-ready.** Flipping
  `NOUS_TEMPORAL_EXTRACTION_ENABLED` to chase `event_ordering` would inject a
  population that is wrong 60% of the time. Fix temporal *extraction precision*
  before any retrieval consumer reads these edges.
- **`happened_before` is in `AUTOBEHAVIOR_EXCLUDED_RELATIONS`** (density /
  spreading / orphans / clusters) so it's inert there today — **but the
  adjacency boost (`retrieval_pipeline.py`, excludes only `contradicts`) does
  NOT exclude it.** So flipping `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED` (the other
  temporal-bet flag) would let 0.27-precision edges boost candidate ranking.
  **This re-justifies the deferred PR-3c exclusion (add `supersedes`,
  `co_occurred`, `happened_before` to the adjacency-boost exclusion) as a
  prerequisite to flipping adjacency boost — it is no longer "low signal."**

### Secondary

- `extracted_from` (0.43): fact←episode provenance is noisy. Low retrieval
  impact (excluded as connectivity in most consumers), but it inflates the
  apparent graph and is the worst gate-eligible relation after temporal.
- `co_occurred` (0.67): the 256 Gap-1 edges are middling — borderline whether
  they earn their adjacency-boost weight (another reason 3c should exclude them
  pending a precision bump).
- `evidence_for` (0.70): the standing cross-type weak spot; direction errors
  (decision→fact mislabeled) and same-project-but-different-instance WEAKs
  dominate. Candidate for a directionality/content-length guard, not a
  threshold change.

## Recommended next moves (ranked)

1. **Gate the temporal bet on extraction precision, not retrieval.** Before any
   `NOUS_TEMPORAL_EXTRACTION_ENABLED` / adjacency-boost flip, drive
   `happened_before` precision up at the *extractor*. Re-audit this relation as
   the acceptance metric.
2. **Ship PR-3c (adjacency-boost exclusion) as a correctness fix, not an
   experiment** — this audit shows the excluded relations (happened_before 0.27,
   co_occurred 0.67, supersedes=lineage) are exactly the ones that should not
   boost ranking. Direction-safe (boost only ever helped good edges).
3. **Leave the backbone alone.** related_to/summarized_by/supersedes/informed_by
   are healthy; the graph's retrieval ceiling there is the score-space/ranking
   problem (already characterized in the 3e spike), not edge quality.
4. `evidence_for` directionality guard — smaller, separate.

**Cost:** ~13 Sonnet calls, ~$0.15, ~12 min. Read-only; no prod writes.
