# 018 — Capability Baseline Instrument (pre-registration)

**Status:** pre-registration (write-before-run). 2026-05-31. FORGE pending.
**Goal:** one comparable baseline of what current Nous memory CAN and CANNOT do across
real-world association/memory scenarios — strengths AND limits on one ruler — with
**mechanism attribution** per cell (lexical / embedding / agent-loop / graph-edge /
plasticity). The limit cells then become the precise improvement targets. Judges the
associative-memory-faculty thesis (017): so far everything that works = similarity +
LLM reasoning; the graph/edges/plasticity carry ~nothing. This instrument tries to
falsify-or-confirm that across 18 scenarios.

Supersedes the per-class harnesses (fc_phase0.py, abstract.py, continual.py) by RE-RUNNING
all of them on ONE shared corpus (apples-to-apples; also builds the cell-3 distractor
environment for free). Shared-corpus, not per-question isolation — per 017's finding that
isolation inflated chunk wins +17pp.

---

## Method (locked before any run)

1. **One corpus, private invented entities throughout** (kills parametric leak — the agent
   can only answer by retrieving). ~50 facts across a single fictional persona's history.
2. **Ingest ONCE, full-cycle:** live `/chat` fact formation → extraction → SLEEP
   (cosine backfill + co-mention + episode edges + supersession). Direct-insert only where
   extraction coverage is a known dropper (logged as a separate observation, not hidden).
3. **Two lenses, assigned per cell (not blanket):**
   - **BARE** = `run_recall_pipeline` top-k rank of the answer fact. The test itself for
     retrieval-mechanics cells (1–3) and the similarity-reachability probe everywhere.
   - **AGENTIC** = live `/chat` retrieve-then-answer loop. The test for bridging/learning
     cells (6–11, 14–18) where the loop is the mechanism.
4. **Instrumentation (Block 1 — PREREQUISITE, built before cells run):** per item log
   `{bare_topk_ids, bare_answer_rank, recalled_context_ids, agent_tool_calls, solved,
   false_bridge}`. `recalled_context_ids` = `TurnContext.recalled_*_ids` actually injected
   into the system prompt (NOT `tool_calls`, which is blind to pre_turn context injection —
   the tc=0/acc=1.00 trap from the Glorptax run). Mechanism attribution is computed from
   these, not guessed.
5. **Validity gate on the FINAL combined corpus:** for any cell asserting "no similarity
   handle," verify the answer fact is NOT in bare top-k for the query (else the item tests
   nothing). 50 invented entities share one embedding space → some cross-cell pairs will be
   accidentally cosine-near; the gate catches them per item.
6. **Precision, not just hit-rate (Sharpen), cells 6–11:** record `false_bridge` = agent
   named a WRONG entity/fact. A confabulated association is itself a real-world failure;
   hit-rate alone overstates the faculty.
7. **Smoke 11/18 on 2 items each BEFORE the 50-item full-cycle run** (threshold-yield
   discipline) — an accidental lexical/embedding handle must surface on 2 items, not after
   the battery.

### Mechanism attribution rule (per item)
- answer in BARE top-k → **similarity** carried it (lexical if shared tokens, else embedding).
- not in bare top-k, agent solved with tool_calls>0 → **agent-loop** (re-query/decomposition).
- not in bare top-k, solved with tool_calls=0 but answer-fact ∈ recalled_context_ids →
  **pre_turn context-injection** (still similarity at root, but not agentic).
- not surfaced by any of the above, solved only after positive-control edge/weight injection →
  **graph-edge / plasticity** (the faculty cells).
- not solved even after injection → **traversal/retrieval gap** (redirects the lever).

---

## The 18 cells (pre-registered predictions + validity gate + lens)

### A. Retrieval mechanics — does the right thing surface?
| # | Scenario | Lens | Pre-registered prediction | Validity gate |
|---|---|---|---|---|
| 1 | Surface/lexical recall | BARE | PASS rank 1–3 | answer fact exists |
| 2 | Semantic/paraphrase recall (0 shared tokens) | BARE | PASS top-k (embedding) | query↔answer 0 shared content words |
| 3 | **Needle under ~50-fact distractor corpus** | BARE | PASS but rank degrades vs #1 | ≥40 competing facts present |
| 4 | Abstention on no-record | AGENTIC | ABSTAIN (no hallucinated entity) | no such fact in corpus |
| 5 | **Cross-type surfacing** (fact→related decision/procedure) | BARE | PARTIAL — cross-type edge exists but rarely top-k | decision/procedure linked at ingest |

### B. Association / bridging — the core faculty
| # | Scenario | Lens | Pre-registered prediction | Validity gate |
|---|---|---|---|---|
| 6 | Shared-entity co-mention bridge (F076) | BARE+AGENTIC | PARTIAL (co-mention fires, ranking-capped) | two facts share one ≥2-token proper noun |
| 7 | **Role/skill match** ("who can fix prog ABC?"→knows-Java) | AGENTIC | PASS via agent re-query (lexical handle on the skill token) | project-needs-X and person-knows-X facts disjoint |
| 8 | Multi-hop decomposition (bridge token in hop-1) | AGENTIC | PASS but UNRELIABLE (cb_brae-class misses) | bridge concept present in hop-1 fact |
| 9 | Experiential co-occurrence (shared episode, disjoint surface) | AGENTIC | PARTIAL — agent finds shared session, not graph | answer-fact ∉ bare top-k for query |
| 10 | Abstract structural analogy (cross-domain) | BARE+AGENTIC | PARTIAL (embedding spans ~5/6, authoring-bias) | ~0 shared content words |
| 11 | **No-handle association** (no lexical, no embedding, only co-occurrence) | BARE+AGENTIC **+ POS-CTRL** | **FAIL** bare + agentic | answer ∉ bare top-k AND no shared tokens |

### C. Temporal / dynamic — memory changes over time
| # | Scenario | Lens | Pre-registered prediction | Validity gate |
|---|---|---|---|---|
| 12 | **Contradiction / supersession** (return current not stale) | BARE+AGENTIC | PARTIAL (F027 subject-based; needs recency resolver flag) | two facts, same subject, conflicting value |
| 13 | **Temporal recency** ("most recent decision about X") | AGENTIC | PARTIAL/FAIL (event_date populated only if temporal-extraction on) | ≥2 dated facts same subject |
| 14 | **Multi-session aggregation** (combine facts across sessions) | AGENTIC | PARTIAL (retrieval breadth limited) | answer needs ≥2 facts from ≥2 sessions |

### D. Learning / adaptation — test-time learning
| # | Scenario | Lens | Pre-registered prediction | Validity gate |
|---|---|---|---|---|
| 15 | Declarative rule learning + composition (Glorptax) | AGENTIC | PASS (banked 10/10) | invented rules, cold baseline ≈0 |
| 16 | **Correction/update learning** (user corrects → applies later) | AGENTIC | PARTIAL (F039 correction path) | correction in a later session than original |
| 17 | Goal-gated selection (same facts, goal selects) | AGENTIC | PASS (LLM filter) | two goals select disjoint facts |
| 18 | **Plasticity / strengthen-by-use** (improves w/ repeated co-activation) | AGENTIC **+ POS-CTRL** | **NULL** (frozen weights) — flat across repetitions | a co-activation pair re-queried N times |

### Load-bearing rows — the positive controls (Block 2)
The conclusion hinges on cells **11** and **18** (one architectural gap — no edge *formation*
from co-occurrence (11), no *strengthening* with use (18) — measured twice). A bare FAIL is
uninterpretable without a control. So:

- **11 pos-ctrl:** after ingest, probe (expect FAIL) → manually `INSERT` one
  `brain.graph_edges` co-activation edge between the two facts → re-probe the SAME item.
  - injected → **solved**: test valid; limit is specifically edge-FORMATION; the fix
    (consolidation that creates the edge) is de-risked — adding the edge demonstrably works.
  - injected → **still fails**: gap is in traversal/retrieval, not store → lever moves off
    edge-building entirely.
- **18 pos-ctrl:** probe the pair (expect flat across N repetitions) → manually bump the
  edge weight (simulate strengthen-by-use) → re-probe.
  - bumped → rank improves: validates wiring `track_access`→weights as the lever.
  - bumped → no change: weighting isn't consumed in scoring → redirects to the scorer.

---

## Output
One baseline table: per cell → {verdict PASS/PARTIAL/FAIL/NULL, mechanism that carried it,
bare_rank, agentic solved, false_bridge rate, pos-ctrl result for 11/18}. The
FAIL/PARTIAL/NULL cells with mechanism = "absent (graph/plasticity)" are the ranked
improvement backlog; each future fix is gated on moving its specific cell, scored identically.

## Build order (locked)
1. Instrumentation (Block 1): surface `recalled_context_ids` per `/chat` turn + bare top-k log.
2. Corpus (`scripts/diag/faculty/baseline_corpus.py`) — 50 invented facts, one persona,
   every cell's item embedded; run combined-corpus validity gate.
3. Smoke cells 11 + 18 (2 items each) — confirm no accidental handle, confirm pos-ctrl wiring.
4. Full ingest (full-cycle + sleep) → run all 18 cells → baseline table.

---

## RESULTS (2026-05-31)

Harness: `baseline.py` (bare), `baseline_agentic.py` (agentic, instance :8079),
`baseline_smoke.py` (cells 11/18 controls), `baseline_flagarm.py` (cells 12/13 resolver).
Corpus = 77 facts, agent `nous-baseline-eval`. text-embedding-3-large.

### Baseline table
| Cell | Bare (cosine top-10) | Agentic (picks right) | Verdict | Carried by |
|------|----------------------|-----------------------|---------|------------|
| c1 surface / c2 semantic / c3 needle | ✓ r1 | — | PASS | similarity |
| c4 abstain | — | abstained | PASS | — |
| c5 cross-type | ✗ | (fixture bug) | INVALID | — (decision never inserted) |
| c6 co-mention / c7 role-skill / c8 multi-hop | ✓ (false bridges present) | ✓ | PASS | embedding + LLM |
| c9 experiential | ✓ r3 | ✓ | PASS\* | *text leak ("same afternoon")* |
| c10 abstract | ✓ r1 | ✓ | PASS | embedding |
| **c11 no-handle** | ✗ disjoint (6/6) | edge rescues 5/6 | FAIL→edge | graph (only when injected) |
| **c12 contradiction** | ✓ stale **out-ranks** current | CONFABULATED "Halloway" | FAIL | — |
| c13 recency / c16 correction | ✓ (stale present) | ✓ | PASS | *LLM reads date/"correction" in text* |
| c14 multi-session / c15 rules / c17 goal-gated×2 | ✓ | ✓ | PASS | embedding + LLM |

Agentic: **12/13 PASS**; `recalled_facts=2` and `tc=0` on most cells ⇒ the work is
pre_turn context-injection + LLM reasoning; the **graph/edges carried none of the passes**.

### Cell 11 (no-handle) — positive control
6/6 register-contrast pairs valid-disjoint. Edge weight IS consumed and strongly modulates
rank **for absent targets** via the `seed_score × edge_weight` path (n=4: w0.3→rank31 absent,
w0.9→rank5 top-10); inert for in-pool targets (adjacency boost is weight-blind; Stage 2b skips
direct hits). ⇒ plasticity (strengthen-by-use) would move retrieval for NOVEL associations, not
re-ranking. Missing pieces = edge FORMATION + weight STRENGTHENING (both write-path).

### Cells 12/13 flag arm — recency resolver
With `subject`+`event_date`+parallel phrasing set: resolver-ON flips c12 (stale rank1→10,
current rank2→1, tagged `current`); c13 demotes stale rank2→15 (`superseded`, score ×0.3).
**Resolver mechanism is sound** but: (1) runs in `run_recall_pipeline`, NOT the pre_turn
injection path the agent used (tc=0) — so live agent only benefits if it calls `recall_deep`;
(2) needs subject+event_date+difflib≥0.55 — natural correction phrasing falls below the floor.

### Conclusion — the two real gaps (ranked improvement backlog)
1. **FORMATION** (c11): links must EXIST; co-occurrence never creates an edge today. Once an
   edge exists it works (pos-ctrl confirmed). Fix = a co-occurrence/co-activation edge-formation
   pass. Gated on moving c11.
2. **RESOLUTION when the key fact doesn't survive retrieval** (c12): stale value out-ranks
   current; the resolver fixes it but isn't in the injection path and needs metadata+phrasing
   it usually lacks. Fix = (a) run resolver in pre_turn path, (b) populate subject+event_date +
   loosen trigger. Gated on moving c12.

Everything else (surfacing + selection) is already carried by similarity + LLM — NOT by the
graph. Confirms the 017 thesis across 18 real-world scenarios: the lever is FORMATION +
RESOLUTION (write-path / injection-path), not denser cosine edges or multi-hop graph traversal.
