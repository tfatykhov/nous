# 017 — The Associative Memory Faculty: Goal, Current-State Review, Action Plan

**Status:** Guiding thesis + roadmap (2026-05-31). Everything downstream — including whether
graph/co-mention work matters — gets judged against this, not against retrieval@k.

---

## PART 1 — THE GOAL

### The thesis
An LLM call is a **stateless processor**: each call is a fresh mind with amnesia that knows
only what's in its context window. So the *continuity of mind* — identity, goals, everything
learned, the whole web of what-relates-to-what — does not live in the model. **It lives in
memory. The memory + how it recalls *is* the mind.** The model is the moment of thinking; the
associative memory is what turns a sequence of stateless thoughts into a continuous, knowing
agent.

The load-bearing faculty is **associative recall + consolidation, as one loop**: assemble into
the tiny context window the knowledge a continuous mind *would have had active* for this moment
and this goal — and *grow that web from experience* so it gets richer over time.

### What the faculty must do (the loop)
| Stage | Brain analogue | What it must do |
|-------|----------------|-----------------|
| **Cue** | current focus/goal | start from the goal-state, not just the query string |
| **Activate** | light up associated knowledge | fire associations across *many* relations — similarity, **contiguity, causality, purpose, analogy** — not just "alike" |
| **Gate** | attention / relevance | the current goal changes *which* associations fire, not just filters scores |
| **Spread** | multi-hop association | activation flows through the web (A→B→C), bounded, not 1-hop |
| **Budget** | working memory / conscious focus | decide what occupies the scarce context window |
| **Think** | cortex | the (stateless) LLM call |
| **Consolidate** | hippocampal replay | **accrete new associations from lived experience** (co-activation / replay), not re-extract from text |
| **Strengthen / decay** | synaptic plasticity | links used often strengthen; unused ones fade |

### Success criteria (how we'll know it's working)
- The agent surfaces knowledge connected by **experience / causality / goal**, not just
  similarity, when the goal demands it (the *"who can fix program ABC?" → "T8me does Java"*
  class — a concept + role bridge neither cosine nor shared-name catches).
- Associations the agent **formed or used before** come back stronger; dead ones fade.
- Recall is **goal-conditioned**: the same fact base surfaces different context for different
  goals.
- Measured by a **discriminating eval** (Part 3, Phase 0), not retrieval@k.

### The far-end substrate (north star, not the starting point)
A CA3 autoassociative spiking network (cf. tinyHippo) is, by construction, a **unified
consolidation + recall** engine: replay + STDP write associations during sleep ("fire together,
wire together"), and pattern-completion dynamics reconstruct the associated constellation from a
cue at recall. That is *exactly* the faculty. It is the **terminal rung of a substrate ladder**
(Part 3), earned by validation — not assumed.

---

## PART 2 — FULL REVIEW OF THE CURRENT SYSTEM

Audited 2026-05-31 across store / consolidation / retrieval (three independent code passes).
**Convergent verdict: every stage of the loop is a *similarity pipe*. Nous is a sophisticated
similarity-retrieval + LLM-housekeeping system — not yet an associative-memory faculty.**

### 2.1 STORE (write-time formation)
- Edge formation at write time (fact→decision, fact→fact, procedure→fact) is **almost
  entirely cosine similarity** of re-embedded templates (`graph_linker.py`; thresholds
  `cross_type_threshold=0.80`, `cross_type_same_threshold=0.90`).
- Non-cosine signals are thin: **deterministic structural** links (fact→episode `extracted_from`,
  episode→decision `discussed_in`, weight 1.0) and **subject+cosine** supersession/contradiction
  (`refines`/`supersedes`/`contradicts`).
- **Absent at write time:** temporal-contiguity linking, **co-activation** ("facts learned/used
  in the same episode should associate"), shared-subject general linking, provenance chains.
  Working memory is purely structural (no association at all).

### 2.2 CONSOLIDATION (sleep)
- Sleep mostly **RE-DERIVES similarity** + does LLM housekeeping: F040 orphan backfill (cosine),
  F043 CE rerank, F027 cluster-merge, F031 contradiction resolution, F012 procedure learning
  (cosine-cluster → LLM extract), F060 abandoned-episode recovery, F035 stale-scan.
- The **only Hebbian-flavoured signals** are F075 `happened_before` (temporal contiguity within
  an episode) and F076 co-mention (shared entity) — both *experiential, not similarity* — but
  they're a small fraction of edge volume.
- **Absent — the core of biological consolidation:**
  - **No replay** — no re-traversal/resampling of past episode sequences.
  - **No edge-weight plasticity** — weights are set at creation (cosine or 1.0) and **never
    strengthen with use or decay with disuse**. `track_access()` updates `recall_count` /
    `last_recalled_at` but feeds only the stale-scan, **never edge weights**.
  - **No co-activation loop** — nothing strengthens a link because two items fired together.
  - Net: sleep is **re-verification + housekeeping**, not a replay-based associative amplifier.

### 2.3 RETRIEVAL (recall + context assembly)
- Core recall (Stages 1–4) is **top-k cosine + RRF keyword**, rescored by **static** edge weight
  × decay. "Graph" stages 2/2b are **1-hop neighbourhood sampling**.
- **Spreading activation exists but is the wrong shape:** decision-seeded only, **density-gated
  not goal-gated**, uniform per-hop decay, returns a flat activation sum. It is "edge-weight ×
  decay," not context-driven activation.
- **Gating is TYPE-level, not ITEM-level:** the frame picks *which memory types* are retrieved;
  within a type, ranking is frame-indifferent cosine + a post-hoc scalar boost. The current
  **goal never changes *which* associations fire.**
- **No pattern-completion / attractor recall** — there is no "cue a fragment → complete the set";
  everything starts from the query string.
- Multi-hop on the **fact** graph is effectively 1-hop (Path A); the recursive multi-hop
  (spreading) runs only on the decision graph.

### 2.4 Faculty-stage → current-state map
| Faculty stage | Current mechanism | Signal | Verdict |
|---|---|---|---|
| Cue | query text + frame | text | goal-state under-used |
| Activate | cosine recall + co-mention | **similarity** (+ thin entity) | one relation only |
| Gate | frame selection, relevance floor | type + score | **type-gated, not goal-gated** |
| Spread | Path A (1-hop) / spreading (decision-only) | static edge-weight×decay | **not goal-driven, 1-hop on facts** |
| Budget | ContextEngine token budgets | budgets | **genuinely strong** |
| Consolidate | sleep: cosine re-derivation + LLM | similarity | **no replay / no co-activation** |
| Strengthen/decay | stale-scan (prune only) | recall recency | **no edge plasticity** |

The two strong parts (token-budgeted working-memory assembly; LLM housekeeping) are real and
worth keeping. The *distinctive* properties of an associative mind — experiential association,
replay, plasticity, goal-gated activation, pattern-completion — are **absent**.

---

## PART 3 — ACTION PLAN

**Strategy:** build the faculty **stage by stage, cheapest substrate first**, each rung
**validated by a discriminating eval before the next**. Principle before substrate — do not
build the SNN until cheap versions prove the principle. The plan is *additive*: keep the strong
similarity-retrieval + budgeting; add the missing faculty on top.

### Phase 0 — Define & measure (gate for everything)
Build the **discriminating eval** that measures the *faculty*, not retrieval@k. A small,
private-entity set (no parametric leak) of items where the answer needs an association the
current pipe cannot make:
- **concept-bridge + role chain** (ABC/Java/T8me),
- **experiential co-occurrence** (two things that mattered together in one episode, dissimilar
  in text),
- **goal-gated** (same fact base, two goals, different correct context),
- **used-before** (a link the agent formed earlier should come back stronger).
Baseline today's system on it. *This is the number every later phase is judged against.* Reuse
the `scripts/diag/hippo/` + whole-system eval-instance harness.

### Phase 1 — Experiential consolidation + edge plasticity (rung 1: software-Hebbian; highest leverage, cheapest)
The biggest gap with the smallest build. No SNN.
1. **Co-activation edges** — items co-active in a turn/episode (recalled together, or learned
   together) strengthen a link. "Fire together, wire together," in software.
2. **Edge-weight plasticity** — feed the *already-tracked* `track_access` usage into edge
   weights: links on co-retrieved/co-used paths **strengthen**; unused links **decay**. (Today
   weights are frozen at creation — this is the single highest-leverage change.)
3. **Replay-as-resampling** — during sleep, resample recent episode sequences and reinforce the
   pairs that co-occurred, biased toward outcome-positive ones (credit assignment).
- **Validated by:** Phase-0 "experiential" + "used-before" cases move.

### Phase 2 — Goal-gated activation + multi-hop on the fact graph
1. **Goal-conditioned activation** — the current goal/frame weights *which* associations fire
   (edge-type and topic priors per goal), not just filters scores. Item-level gating.
2. **Multi-hop fact spreading** — extend spreading-activation seeding to fact/chunk seeds (not
   decision-only), bounded depth, with a bleed guard (frequency/IDF down-weighting so concept
   hubs like "Java" bridge without flooding).
- **Validated by:** Phase-0 "concept-bridge" + "goal-gated" cases move.

### Phase 3 — Pattern-completion recall (rung 2: dense associative memory)
A **modern Hopfield / dense associative memory** as the recall-by-completion layer — the
mathematically clean, trainable version of CA3 autoassociation (provably related to attention).
Cue a fragment of context → complete the associated constellation. Captures the *recall* half of
the hippocampal faculty without spikes.
- **Validated by:** recall quality on partial/indirect cues vs. Phase 2.

### Phase 4 — Replay-based SNN consolidation (rung 3: tinyHippo; the north star, earned)
Only if Phases 1–3 prove the principle *and* hit the ceiling of software methods. A spiking
CA3↔CA1 model doing bidirectional replay + STDP, serving **both** consolidation and
pattern-completion recall.
- **The research problem** is the **SNN↔symbolic bridge**: encode embeddings/facts → spike
  patterns, decode learned synapses → usable associations. Prototype encode/decode on a toy set
  first; do not start here.

### Sequencing principle & what we stop doing
- Each phase is **gated by the Phase-0 eval**; cheap-first; the SNN is *earned*, not assumed.
- **De-emphasize piling on more cosine-edge variants** (the co-mention family). They improve one
  cue on one relation — the similarity pipe we've already over-invested in. Co-mention ships
  (merged, ranking-safe); further graph-edge tuning is *not* the lever. The lever is plasticity,
  experiential consolidation, goal-gating, and pattern-completion — i.e. making the *loop* real.

## PART 5 — PHASE 0 RESULTS (2026-05-31, full-cycle, all four classes, both lenses)

Instrument: `scripts/diag/faculty/` (private invented entities, controls, pre-registered
predictions, per-item validity gate, checkable-token grading). Corpus ingested
FULL-CYCLE (live instance `/chat` → fact extraction → sleep: 27 facts, 35 edges) — concept-
bridge hop1/bridge in separate sessions (concept bridge only), experiential pair in one
session (shared-episode bridge). A prerequisite bug was found + fixed (see below).

| Class | Bare (full-cycle) | Agentic |
|-------|-------------------|---------|
| control + (dentist) | rank 1 — PASS | PASS |
| control − (accountant) | — | PASS (clean abstain) |
| **concept_bridge** ×3 | **0/3** — answers rank 12/17/18, disjoint, NOT recovered **even with sleep** | **2/3** (cb_brae abstained) |
| **experiential** | FAIL — answer rank 30, disjoint | **PASS** — agent found the shared *conversation* (5 tool calls) |
| **goal_gated** | n/a | **PASS** both goals (LLM filters retrieved facts by goal) |
| **used_before** | null by construction | null by construction |

**Findings:**
1. **The bare association graph fails the whole faculty — even fully sleep-consolidated.**
   Concept-bridge stays rank 12-18 (outside top-k) with cosine backfill + co-mention +
   episode edges all built: sleep does not bridge embedding-dissimilar / single-token-concept
   facts. The earlier direct-load 0/3 is confirmed full-cycle.
2. **The agent compensates across three classes** (concept-bridge, experiential, goal-gated)
   via its retrieve-and-reason loop — the associative work happens in the agent, not the graph.
   → **The lever is the agent loop's reliability, NOT denser graphs. Phase 2 (multi-hop graph)
   is DEMOTED.** This re-sequences the roadmap (Part 3 held Phase order as a hypothesis Phase 0
   could revise; it did).
3. **Caveat — the loop is not reliable:** cb_brae abstained (couldn't surface the Brae facts),
   so concept-bridge is 2/3 agentically. That unreliability is the actual lever. goal-gating is
   solved at the reasoning layer (LLM filters), not retrieval.
4. **Prerequisite bug fixed (PR #474):** the agentic lens initially returned empty answers
   because the opus-4.8 non-streaming tool loop dropped tool calls when stop_reason='end_turn'
   with tool_use blocks present (runner.py:1595/1205) — a real prod bug the eval surfaced. See
   [[reference_opus48_toolloop_stopreason_bug]]. Fixed + confirmed (turns 0→1, toolcalls 0→3).

**Roadmap implication:** the original Phase 1→4 (plasticity → goal-gating → completion → SNN)
assumed the gap was in the memory substrate. Phase 0 shows the substrate's *graph* doesn't carry
the associative load and the *agent loop* already does — so the revised priority is **(a) make the
agent's retrieve-and-reason loop reliable** (cb_brae-style misses), and **(b)** the substrate work
(plasticity/experiential consolidation) becomes a recall *assist* to the loop, not the primary
mechanism. tinyHippo/SNN stays the far horizon, now clearly gated behind "is the substrate even
the bottleneck" — Phase 0 says: not for these cases.

## PART 6 — ABSTRACT-ASSOCIATION CLASS (the frontier; final Phase 0 experiment, 2026-05-31)

Motivated by the observation that Parts 4-5 cases were all SURFACE-anchored (shared name /
concept-word / episode) and the agent "solved" them by lexical query reformulation — never
testing ABSTRACT association. Item shape: query describes a problem by its abstract STRUCTURE
(domain A); answer fact = an invented person who solved a surface-different instance of the
SAME structure (domain B), ~zero shared content words. Three-way validity gate: (a) co-mention
0 by construction; (b) **embedding cosine** — does the representation span the structure?; (c)
agent reformulation. Full-cycle consolidation (sleep). [NOTE: /chat extraction dropped 3/6
abstract facts — an extraction-COVERAGE gap; test material guaranteed via direct-insert + sleep.]

**RESULT (the surprise — the embedding spans abstract structure):**
- Cosine-reachable (top-10): **5/6** — deadlock r3, cascade r1, runaway r4, starvation r7,
  bottleneck r9; only thrashing missed (r16). Twin control r2 (instrument sound).
- co-mention bridges: 0. agent-reformulation-only: 0.
- Agent solved 4/6 — and **the agent sometimes HURT**: on bottleneck/thrashing it over-reasoned
  to structurally-adjacent-but-WRONG neighbors (picked the crowd-jam / runaway person).
- Failure split: REPRESENTATION gap 1/6 (thrashing); solved-via-cosine 5/6; agent-only 0.

**SYNTHESIS — where the associative load lives (across Parts 5-6):**
| Association type | Carried by |
|---|---|
| concrete / lexical (name, concept-word, episode) | the agent's retrieve-and-reason loop |
| abstract / structural (deadlock, cascade, …) | the **embedding representation** (5/6) |
| the graph (cosine edges, co-mention, multi-hop) | **~nothing, in either regime** |

**ROADMAP CONCLUSION (the abstract test re-elevated the substrate, as Part 3/advisor flagged
it could):** the lever is **representation quality + agent retrieval/precision**, NOT the
association graph. This DEMOTES the edge-building direction **including tinyHippo/SNN-replay** —
edges are not where association happens; the embedding and the agent are. Phase 1+ should target
(1) representation/embedding quality (the abstract frontier — strong, with gaps like thrashing),
and (2) the agent's PRECISION among structurally-similar candidates — not denser graphs.

**CAVEATS (load-bearing):** N=6 hand-authored; structural descriptions were evocative, so cosine
reachability is partly authoring-driven (a domain-buried harder version would stress it). Agent
misses partly reflect corpus density (6 similar-structure facts compete). Strong-DIRECTIONAL,
not bulletproof — but it answers the question: abstract association is real and the embedding
already does most of it.

## Honest framing
"Replicate the human brain" is a north star, not a literal target. The meaningful, measurable
goal is an associative-memory faculty **much richer than top-k similarity** — experiential,
plastic, goal-gated, completion-capable. That is buildable, in rungs, each one provable.

---

## PART 4 — KEEP / MODIFY / REMOVE / ADD, BY AREA

The rebuild is **overwhelmingly additive.** The strong parts stay; we MODIFY a few components to
become plastic / goal-gated / multi-hop; we ADD the missing faculty; we REMOVE almost nothing
(low blast radius). `(P#)` = roadmap phase that does the work.

### 4.1 STORE (write-time formation)
| Component | Verdict | Rationale |
|---|---|---|
| `Heart.learn` spine, embeddings, dedup | **KEEP** | the storage backbone; similarity stays one legitimate cue |
| Admission control (F023), actionability (F047) | **KEEP** | quality gates, orthogonal to the faculty |
| Supersession / contradiction (subject+cosine, F027) | **KEEP** | relational (non-pure-similarity) edges — valuable |
| Deterministic structural links (`extracted_from`, `discussed_in`, w=1.0) | **KEEP** | these *are* experiential/provenance edges — the good kind; we want *more* of this shape |
| Chunk creation (F067/F069), working memory | **KEEP** | verbatim-recall substrate; structural scratch |
| Cross-type cosine linker (`graph_linker.py`) — edge **weight = frozen cosine** | **MODIFY (P1)** | the weight must become a *mutable association strength* (cosine = the *initial* value), not a permanent snapshot. Also fix the silent re-embed fallback. |
| Cross-type linker re-embeds async + can silently fail | **MODIFY (P1)** | fall back to native embedding + subject on failure; don't drop the edge silently |
| — | **ADD (P1)** | **Co-activation edges:** facts/items learned in the same episode (and items co-active in a turn) get a weak, *plastic* association edge — "they happened together," regardless of similarity. This is the experiential signal absent at write time. |
| — | **ADD (P1)** | **Association-strength as a first-class mutable field** on edges (the substrate plasticity reads/writes). |
| **REMOVE** | *(nothing)* | no write-time component is harmful; the only "removal" is conceptual — stop treating the cosine weight as the permanent strength. |

### 4.2 CONSOLIDATION (sleep)
| Component | Verdict | Rationale |
|---|---|---|
| LLM housekeeping — reflect, contradiction-resolve (F031), cluster-merge (F027), abandoned-recovery (F060), procedure-learning (F012), rubric-evolution (F024) | **KEEP** | genuinely valuable; orthogonal to plasticity |
| Relink open episodes (F057, structural) | **KEEP** | experiential/provenance backfill — the good kind |
| `happened_before` (F075), co-mention (F076) | **KEEP** | the *only* current experiential edges — but **do not extend the co-mention/cosine-edge family** (not the lever) |
| Dead-edge / hub-snapshot pruning (F053/F065) | **KEEP** | housekeeping |
| F040 cosine orphan backfill, F043 CE | **KEEP (reframed)** | fine as a *completeness/re-verification* pass — but it is **not** the consolidation faculty; do not treat it as such. CE stays flag-gated (prod-slow). |
| Stale-scan (F035) — deactivates unused *facts* only | **MODIFY (P1)** | extend to **edge decay**: links unused over time *weaken* (the negative-Hebbian half). Today only facts get pruned; edges never decay. |
| — | **ADD (P1)** | **Replay-as-resampling:** resample recent episode sequences during sleep and reinforce co-activated pairs, biased toward outcome-positive ones (credit assignment). The missing replay. |
| — | **ADD (P1)** | **Edge-weight plasticity (strengthen half):** wire the *already-tracked* `track_access` co-retrieval/co-use stats into edge weights — used associations strengthen. (Decay half = the stale-scan modify.) |
| **REMOVE** | *(nothing wholesale)* | the conceptual removal: stop relying on cosine re-derivation as the *association-building* mechanism — it stays as completeness, not as the faculty. |

### 4.3 RETRIEVAL (recall + context assembly)
| Component | Verdict | Rationale |
|---|---|---|
| Vector recall + RRF hybrid; CE rerank (F042), MMR (F030), contradiction attach, recency resolver, context-dedup (F071) | **KEEP** | similarity is the legitimate *entry* cue; the rerankers are reasonable refinements |
| ContextEngine — token budgets, tier structure, assembly order | **KEEP** | the **strongest** part — this *is* the working-memory/budget stage; keep it |
| Tier-1 category facts, working-memory injection | **KEEP** | always-on identity/profile context |
| Usage boost (D3), residual activation (F055) | **KEEP + extend** | already use-/recency-aware — the seed of plasticity-at-read; extend toward genuine activation state |
| Staleness penalty, diversity, relevance floor | **KEEP** | sane filters |
| Edge weight in scoring = **static** | **MODIFY (P1, falls out)** | once weights are plastic (P1), retrieval ranking automatically reflects association *strength* — strong/used associations rank up. No retrieval code change beyond reading the now-mutable weight. |
| Frame/goal gating = **TYPE-level + post-hoc scalar boost** | **MODIFY (P2)** | the current goal must change **which** associations fire (goal-conditioned edge-type/topic priors), not just which memory *types* retrieve. Item-level, not type-level. |
| Spreading activation = **decision-seeded, density-gated, flat** | **MODIFY (P2)** | seed from fact/chunk too; **goal-gate** (not density-gate); bounded **multi-hop on the fact graph** with a **hub-bleed/IDF guard** (so concept hubs like "Java" *bridge* without flooding — do **not** hard-drop hubs the way the current degree-cap does) |
| Path A (Stage 2b) — 1-hop, flag-off | **MODIFY (P2)** | turn on (it's the association consumer); extend toward multi-hop; fed by plastic weights |
| — | **ADD (P3)** | **Pattern-completion recall:** a modern-Hopfield / dense-associative-memory stage that, given the assembled context as a partial cue, *completes* the associated constellation. The recall half of the hippocampal faculty. |
| Hub **degree-cap** (drops high-degree concept edges) | **REMOVE/REPLACE (P2)** | the one genuine *remove*: dropping hubs kills concept bridges (the "Java" case). Replace with **frequency/IDF down-weighting** — keep the bridge, weaken it. |

### 4.4 Net
- **KEEP:** the entire similarity-retrieval + budgeting + LLM-housekeeping stack (most of the system).
- **MODIFY:** make edge weights *plastic* (P1); extend stale-scan to *edge decay* (P1); goal-gate + multi-hop + fact-seed *spreading/Path A* (P2); reframe F040 as completeness, not faculty.
- **ADD:** co-activation edges + plastic-strength field (P1); replay-as-resampling (P1); pattern-completion recall (P3); goal-conditioned activation (P2); SNN replay (P4, earned).
- **REMOVE:** essentially nothing — except the **hub degree-cap** (replace with IDF down-weighting, P2). Low blast radius is deliberate.
