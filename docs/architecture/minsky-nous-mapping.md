# Minsky → Nous Architecture Mapping

**Source:** Marvin Minsky, *The Society of Mind* (1986)
**Compiled:** March 19, 2026
**Last verified:** 2026-06-01 against commit 169f85c (main; PR #475 associative-memory faculty merged).
**Chapters Analyzed:** All 30 (1–30)

### Changelog since 2026-03-29

Material mapping changes shipped since the original analysis, with the Minsky concepts they touch:

- **F031 — Censor middleware (action payloads + conditional unblock):** `unblock_pattern` field + `trigger_action`/`action_instruction` give censors "unless"-clause semantics and read-only action execution on fire. Addresses Ch. 12 Exception Principle and Ch. 27 contextual censor exceptions.
- **F040 — Graph densification + admission control:** orphan backfill with per-relation cosine thresholds (fact-fact, fact-decision, …). Addresses Ch. 8 "Societies of Memories too aggressive" via conservative edge admission. Pairs with Ch. 6 "meaning is relational."
- **F042 / F045 — Cross-encoder reranking:** improves retrieval precision; does not close a Minsky mapping gap directly.
- **F050 — Query expansion (multi-query):** Haiku generates semantic variants at recall time. Addresses Ch. 14 Reformulation / Multiple Descriptions.
- **F067 — Episode chunks (verbatim transcript recall):** `heart.episode_chunks` preserves verbatim tokens fact extraction discards. Addresses Ch. 15 "memory as fragments" (preservation, not yet reconstruction).
- **F075 — Temporal extraction (`happened_before` edges + `event_date`):** date-anchored event extraction and temporal ordering within episodes. Touches Ch. 8 level-bands and Ch. 18/19 evidence chaining.
- **F076 — Co-mention edges:** facts naming the same entity are explicitly linked; excluded from the spreading-activation density gate to prevent over-linking. Addresses Ch. 11 co-activation.
- **PR #475 — Associative-memory faculty (merged to main):** co-occurrence edge **formation** (`build_cooccurrence_edges`: facts sharing `source_episode_id` → `co_occurred` edges), prominence interleave, and the **recency resolver** (`ContextEngine._resolve_recency`: same-subject facts with conflicting `event_date` are tagged current vs superseded and the older down-ranked ×0.3). Addresses Ch. 11 co-activation, Ch. 15 memory rearrangement, Ch. 20 context-dependent contradictions.

---

## Executive Summary

All 30 chapters of *The Society of Mind* were analyzed against Nous's current codebase. The original central finding was: **Nous has built the right data structures but lacks the plasticity layer** — it stores and retrieves but does not reshape.

As of 2026-06-01 that thesis is **PARTIALLY addressed, with one critical gap remaining**:

> **Status vocabulary** (used throughout — *code existing* is not the same as *behavior being active*): `(stub)` = the function logs and returns without doing the work; `(shipped, default OFF)` = code is complete but its gating flag defaults to `False`, so it is opt-in and inactive in a standard deployment; `(reserved, no consumer)` = a DB column/field exists but no code reads it; `(shadow mode — non-enforcing)` = it runs and scores but does not act on the result. Several mechanisms below are shipped-but-default-OFF (co-occurrence formation, query expansion, epistemic gate, episode chunks) or non-enforcing (admission control) — they appear in code but do not change behavior unless explicitly enabled.

- **Plasticity now partially exists.** Edges are now *formed from experience* (PR #475 co-occurrence edges from shared `source_episode_id` — `(shipped, default OFF)`; F076 co-mention edges; F075 temporal `happened_before` edges) rather than only re-derived from cosine similarity. And the fact set is now *reshaped at recall time* (PR #475 recency resolver demotes superseded same-subject facts ×0.3 and tags current vs superseded). Query intent can be reshaped at recall via F050 expansion `(shipped, default OFF)`.
- **But edge-weight plasticity is still NOT shipped.** Edge weights are **frozen at creation** — there is no strengthen-by-use or decay-by-disuse mechanism. The codebase contains zero `UPDATE` statements on `graph_edges.weight`. F023 tracks `recall_count`/`last_recalled_at` on facts but never feeds those signals back into edge strength. This is the difference between "edges are *formed* from experience" (now true) and "edges are *shaped* by experience" (still missing) — i.e. Minsky's "cells that fire together, wire together" / Ch. 8 fringes remain unimplemented at the weight level.

Net: Nous is roughly one-quarter plastic (edge formation from experience + turn-level supersession reshaping + spreading-activation assembly) and three-quarters still static (edge-weight mutation, urgency cascades, micronemes, algebraic fragment synthesis).

Five meta-themes emerged:

1. **Administration > Acquisition** (Papert's Principle, Ch. 7/10) — Better ways to use existing capabilities beats building new ones
2. **Micronemes** (Ch. 16/20) — Ambient context signals that bias everything, completely absent from Nous
3. **Reconstruction > Retrieval** (Ch. 15/20) — Recall should assemble from fragments, not return verbatim stored text
4. **Relational Meaning** (Ch. 6/11/19) — Nothing has intrinsic meaning; everything means through relationships. Facts stored atomically lose meaning.
5. **Distributed Initiative** (Ch. 1/5) — Heart/Brain/Skills should initiate actions, not just respond to LLM queries. Otherwise the LLM layer becomes the homunculus.

---

## Chapter-by-Chapter Mapping

### Chapter 1: Building Blocks

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Agent/Agency Duality** | Same system = mindless agent (micro) or intelligent agency (macro). Both views needed | Heart + Brain + Frame + Skills are separate agencies | ✅ Structurally correct — distributed by design |
| **Substitution Principle** | Replace any complex agent with a subsociety of simpler agents. Never hide complexity in one box | Subtask spawning (`SubtaskManager`, `nous/heart/subtasks.py:14-100`); pending limit `_MAX_PENDING=5` | Weak — subtask decomposition is flat (1 level + external dispatch). Subtasks don't spawn sub-subtasks, lack skill-level capabilities, and never form a recursive society. |
| **Common Sense** | "An immense society of hard-earned practical ideas." Not simple at all | Facts + censors + procedures | Good — facts are learned heuristics, censors are rule exceptions |
| **Amnesia of Infancy** | We forget how hard basic skills were to learn, making them seem innate | Episode consolidation / sleep | Partial — we consolidate but don't track what was hard to learn |

**Key Takeaway:** The Substitution Principle means Nous should never let complexity hide inside a single tool call or handler. If something is complex, decompose it.

---

### Chapter 2: Wholes and Parts

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Three-Level Problem** | Understanding requires: how each part works, how parts interact, how interactions produce global behavior | Facts (L1), graph edges (L2), spreading activation (L3) | L1 good, L2 present, L3 barely exists |
| **Gestalt Trap** | "The whole is more than the sum of parts" is a pseudo-explanation. Specify the interactions | Split by path: the **`recall_deep`** tool's `run_recall_pipeline` chains vector seeds (L1) → 1-hop graph neighbors (L2) → conditional spreading activation (L3) and is default-on (`graph_recall_enabled=True`). But the **pre-turn context path** (`ContextEngine.build`) uses plain `heart.search_facts` with **no graph expansion** — that path is still a bare-fact endpoint. | Partially addressed — `recall_deep` prevents bare-fact isolation by construction; pre-turn context assembly does not (no L2/L3 there). |
| **Containment = Arrangement** | No single board contains a mouse. Six boards arranged correctly do. Properties emerge from arrangement | Procedures capture step sequences (`nous/heart/procedures.py:54-146`); frames set mode (default_category/stakes, questions_to_ask). Arrangement DOES drive cognition (frame selection tunes stakes/category). | Still missing constraint satisfaction — the system cannot represent "a valid arrangement must satisfy X, Y, Z"; only sequential execution. Long-term architecture gap. |
| **Easy Things Are Hard** | Simplest perceptual tasks require enormous hidden machinery | No introspective cost model | Missing — Nous doesn't know which tasks were harder than expected |

**Key Takeaway:** Level 3 (emergent behavior from local interactions) is the core missing layer. Without it, the memory system is a filing cabinet, not a society.

---

### Chapter 3: Conflict and Compromise

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Noncompromise (3.2)** | Conflicts migrate upward. When subordinates stall, rivals seize control | Task cancellation exists (`subtasks.py` `cancel()`/`mark_failed()`); stalled subtasks hit `timeout_seconds` and are marked failed. EventBus logs events but does not cascade priority shifts. | Still missing — no stall-detection or alternative-strategy mechanism. On timeout the system marks failed and moves on; it does not detect a stalled goal and try a different approach. Architectural. |
| **Heterarchies (3.4)** | Not everything is hierarchical. Loops require memory | EventBus + graph edges | ✅ Working well |
| **Pain/Pleasure Simplify (3.6)** | Both narrow focus. Too much simplification degrades the self | Block-level censors = pain | Risk: too many block censors over-simplify |

**Key Takeaway:** Stalled subtasks should trigger alternative strategies, not just timeout.

---

### Chapter 4: The Self

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **One Self or Many** | Self is a society of sub-agents with different concerns | Single `agent_identity` per agent (one versioned story, `models.py:53-66`). `suppressed_frames` + `agencies_to_activate` exist as Frame DB columns but are `(reserved, no consumer)` — no mode-switching logic reads them anywhere. Frames are NOT modeled as partial selves with distinct values. | Still missing — frames are mode-selectors, not sub-agents. The "each frame = a partial self with own defaults/priorities" idea is unimplemented; the system does not populate or enforce per-frame identity. |
| **Self-Control via Censors** | You don't resist bad actions by willpower — you prevent them from being considered | Absolute censors | ✅ Good alignment |
| **Identity as Narrative** | Personal identity is a story, not a fact | agent_identity document | ✅ Exactly this — a story Nous tells itself |

**Key Takeaway:** Each frame could be a "partial self" with its own defaults, priorities, and active skills.

---

### Chapter 5: Individuality

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Circular Causality (5.1)** | Goals form feedback loops, not linear chains. Neither cause is "first" | No goal-loop tracking. Subtasks are flat priority lists; the DAG module (`nous/dag/`) is workflow scheduling, not goal-loop representation. | Still missing — goal loops are not representable; the task model assumes acyclic execution. No way to represent "goal A and goal B mutually reinforce." |
| **Loop-Breakers (5.2)** | Dogma and external anchors break infinite regress. "Just because!" is legitimate self-regulation | Absolute censors + values | ✅ Censors are exactly Minsky's loop-breakers |
| **Homunculus Fallacy (5.3)** | A central unified Self explains nothing. Must be distributed agencies | Heart + Brain + Frame + Skills are modular but all respond to LLM tool invocations (`api/tools.py` routes calls). Skills don't auto-fire on pattern match; Heart doesn't autonomously consolidate at end-of-turn; Brain doesn't auto-record decisions mid-reasoning. Sleep runs during gaps but requires session state. | Still LLM-centric (stale). To resolve Minsky's complaint, Heart should autonomously consolidate at conversation end, Brain should auto-record decisions without prompting, and Skills should self-activate on context. Today all agency flows through the LLM layer. |

**Key Takeaway:** The deepest architectural gap — Heart/Brain/Skills should be able to initiate actions, not just respond to LLM queries. Otherwise we have a distributed toolset controlled by a central homunculus.

---

### Chapter 6: Insight and Introspection

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **B-Brains (6.4)** | Meta-cognitive monitor watches for dysfunction and intervenes | `FrameEngine.select` (`nous/cognitive/frames.py:32-127`) picks a frame at session start via static pattern matching, once per session. | Still stale — frames do not monitor mid-conversation. No mechanism detects reasoning errors, stalled subtasks, or cognitive dysfunction in-progress. Monitoring is passive selection, not active intervention. |
| **Frozen Reflection (6.5)** | Can't observe a process without changing it. Accept incompleteness | Episode summaries | ✅ Good alignment |
| **Meaning is Relational (6.9)** | Indiscriminate connections destroy meaning | Graph topology is now actively admission-controlled: F040 per-relation cosine thresholds (`graph_densifier.py:50-91`), F031 censor action payloads with allowed-tool whitelist (`censor_actions.py:22-29`), and F076 co-mention edges EXCLUDED from the spreading-activation density gate (`spreading_activation.py:29-41`) to prevent indiscriminate linking. | ✅ Now addressed — edge creation is gated by per-relation thresholds + density checks; co_mention edges are kept out of the density calculation to avoid over-linking. |
| **Self-Knowledge is Dangerous (6.13)** | Core ideals must change slowly. Some constraints tamper-proof | Absolute censors + identity versioning | ✅ Good alignment |

**Key Takeaway:** Active B-brain monitoring is the biggest gap. Nous needs to detect and intervene on its own cognitive failures mid-conversation.

---

### Chapter 7: Problems and Goals

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Uncommon Sense (7.2)** | Common sense requires vast knowledge networks | Retrieval pipeline | Validates that retrieval is harder than storage |
| **Difference-Engines (7.8)** | Detect difference between current and desired state, find agent that reduces it | Task system (`nous/heart/subtasks.py`, `task_scheduler.py`); subtasks carry `goal_summary` + instructions. | Still stale — tasks are "do X," not "reduce the gap between current and desired state." No explicit state-diffing or gap-reduction framing; the delta between world-state and goal-state is not a driving signal. |
| **Papert's Principle (7.10)** | Most crucial growth = new administrative ways to use existing skills | — | **THE design principle for Nous** |

**Key Takeaway:** Before building any new capability, ask: "Can we get more from what we already have by administering it better?"

---

### Chapter 8: A Theory of Memory

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **K-lines (8.1)** | Memory stored near agents that created it. Reactivate K-line → restore mental state | graph_edges + spreading activation | ✅ Core concept implemented |
| **Level-bands / Fringes (8.4-8.7)** | Strong connections at middle detail, weak "fringe" connections at extremes. Fringes = default assumptions | `GraphEdge.weight` exists (`models.py:274`) and is read during spreading activation (`spreading_activation.py:107`) and scoring (`retrieval_pipeline.py:974-975`), but weights are set at edge-creation time and **never modified** — zero `UPDATE` statements on `graph_edges.weight`. | **Still missing** — fringe behavior is unimplemented. No strengthen-by-use or decay-by-disuse; weights are immutable frozen values, not plastic signals shaped by experience. This is the single highest-leverage remaining gap. |
| **Societies of Memories (8.8)** | Connect K-lines to older K-lines, not all active agents | `FactGraphLinker` (`fact_graph_linker.py:42-79`, gated by `cross_type_linking_enabled`) links new facts to related decisions. F040 orphan backfill adds per-relation cosine thresholds (`graph_densifier.py:50-91`); F076 co-mention edges link same-entity facts; spreading activation is density-gated (`should_use_spreading_activation`, `spreading_activation.py:53-64`). | Changed — the "too aggressive" concern is now addressed via per-relation admission control (fact-fact 0.75 strict / 0.65 CE-mode) and a density gate before multi-hop expansion. |
| **Layers (8.11)** | B-brain controls zoom level. Each layer exploits the previous | Frames + sleep create abstraction layers; `sleep_handler.py` Phase 4 consolidates facts and creates procedures (offline). | Still stale — no dynamic zoom. The B-brain doesn't choose an abstraction layer at runtime; frames are fixed at session start and sleep is offline. No in-conversation zoom-in/out. |

**Key Takeaway:** Edge strength/fringes is the most actionable gap. Adding `strength` float to `graph_edges` enables decay, reinforcement, and default connections.

---

### Chapter 9: Summaries

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Summarizing is Hard** | Requires knowing what to OMIT — harder than inclusion | Episode summaries are still flat prose (`Episode.summary` TEXT field, `nous/heart/episodes.py`). | Unchanged — summaries are flat strings, not outline trees (key-decision → supporting facts → context). |
| **Bridge-Definitions** | Connect new concepts to existing ones. Note HOW related, not just THAT similar | Cosine similarity is used throughout (`graph_densifier.py`, `heart/search.py` `hybrid_search`). | Unchanged — cosine gives similarity magnitude but no WHY. Still need relation-specific structural explanations, not just similarity scores. |

**Key Takeaway:** Episode summaries should be structured (key decision → supporting facts → context), not flat prose.

---

### Chapter 10: Papert's Principle Extended

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Meta-skills Grounded in Domain** | Can't learn to think about thinking without first thinking about something specific | Frames grounded in task types | ✅ Correct pattern |
| **Debugging as Meta-skill** | A system that debugs its own reasoning > one with better initial reasoning | Nous can detect task failures (`nous/handlers/outcome_detector.py`) but does not diagnose or fix its own reasoning errors. | Still missing — no self-debugging. A system that detects "I made a mistake" and auto-corrects reasoning would beat better initial performance. Currently unaddressed. |
| **Administrative Intelligence** | "Society of Mind is about the administrative structure of intelligence" | Frame/routing/admission pipeline | Validates entire Nous direction |

**Key Takeaway:** Self-debugging capability is more valuable than better initial performance.

---

### Chapter 11: The Shape of Space

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Relational Meaning (11.1/11.5)** | Nothing has intrinsic meaning. Quality = relationship. Isolated signals meaningless | Facts stored as atomic text (`heart.facts.content`, `models.py:475-523`). Facts ARE connected via `graph_edges`, but the fact itself is atomic text, not decomposed into relational components. | Still stale — facts fundamentally atomic; graph connections are a separate metadata layer, not integrated relational structure. Storage unchanged. |
| **Society of Nearnesses (11.2-11.3)** | Space built from co-activation statistics. Similarity = co-activation frequency | recall_deep uses cosine for semantic nearness; PR #475 `build_cooccurrence_edges` + F076 co-mention build statistical co-activation at sleep time (shared `source_episode_id` ⇒ `co_occurred`). | Changed — co-mention edges build at sleep; co-occurrence edges `(shipped, default OFF)` (`cooccurrence_linking_enabled=False`). Built POST-HOC at sleep, not dynamically during the turn. Weights are static at creation (no strengthen-by-use / decay-by-disuse). |
| **Manifold Learning (11.4)** | Local nearness → global geometry. Hierarchical correlation layers | recall_deep uses flat vector search + spreading activation (1-hop / multi-hop CTE) with a single exponential decay; no tiered geometry. | Still stale — no hierarchical/manifold structure; all nodes treated as a single layer. Long-term gap unchanged. |
| **Co-activation (11.3)** | Nerves that fire together wire together | PR #475 + F022 Phase 2: facts sharing `source_episode_id` now FORM `co_occurred` edges during sleep (`build_cooccurrence_edges`, `extraction_method='co_occurrence'`); F076 forms co-mention edges for same-entity facts. Deterministic, based on episode co-presence. | Formation layer `(shipped, default OFF)` — `cooccurrence_linking_enabled=False`, so the sleep cycle does not form these edges unless enabled (backfilled once on prod, not live by default). Still missing: edge-weight STRENGTHENING-BY-USE / DECAY-BY-DISUSE (co-occurrence weight is constant at creation). |
| **Egocentric → Allocentric (11.6)** | Progress from body-relative to world-relative understanding | Frames are conversation-/task-relative (`models.py:68-89`). No third-person self-model or world-model for cross-agent reasoning. | Still stale — no allocentric representation. Long-term gap unchanged. |
| **Anti-Dumbbell (11.9)** | Always seek a third alternative before accepting binary framings | Confidence scoring + multi-reason deliberation on decisions (active); epistemic routing gate (§2, `context.py:236-247`) `(shipped, default OFF)` — `epistemic_gate_enabled=False`. | ✅ Implemented functionally via confidence + multi-reason deliberation (the epistemic gate is opt-in); not an explicit third-option requirement gate. Low-priority gap. |
| **State-Diffing (11.8)** | Mirror agencies: freeze state, work, compare before/after | Decisions recorded with no explicit before/after diff on episodes; `outcome` tracks result, not delta (`models.py:363-402`). | Still stale — no before/after diffing on episodes or state snapshots. Long-term gap unchanged. |

**Key Takeaway:** Move from a catalog of facts toward a society of related signals where retrieval is topology-aware and meaning emerges from connection density.

---

### Chapter 12: Learning Meaning

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Four Types of Learning** | Uniframing, accumulating, reformulating, trans-framing | Mostly accumulating (store facts, learn procedures). Sleep phases review/prune/compress. F050 query expansion adds reformulation at RECALL time, not learning time. | Still stale — learning-time reformulation and trans-framing remain missing. |
| **Don't Notice Too Much** | Over-attending to detail prevents generalization | F023 admission control scores what enters memory (`nous/heart/admission.py`); runs in `(shadow mode — non-enforcing)` by default (`admission_shadow_mode=True` — it scores and logs but does NOT reject). | ✅ Validates the F023 concept, but shadow-mode default means it filters nothing yet (enforcement requires `admission_shadow_mode=False`). |
| **~7 Item Limit** | Before forced generalization. Working memory can't hold more | Working memory enforces `max_items` (default 20, `models.py:727`). | Limit enforced (at 20, not Minsky's ~7); does not yet trigger forced generalization at threshold, but bounded. Good enough. |
| **Exception Principle** | Censors should support "unless" clauses | F031: `Censor.unblock_pattern` field + `action_instruction`; `CensorActionExecutor` validates and runs read-only action payloads (recall, search_facts, …) to conditionally unblock (`censor_actions.py`, `models.py:706`). | Now addressed — pattern-based unblock + action payloads are shipped. Not yet full probabilistic "unless" semantics, but contextual exceptions work. |

**Key Takeaway:** F023 is Minsky's "don't notice too much" in engineering terms. Also: censors need exception/context support.

---

### Chapter 13: Seeing and Believing

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Expectations Shape Perception** | We see what we expect to see. Expectations are active predictions | Passive retrieval; no expectation-setting before recall (`nous/api/retrieval_pipeline.py` has no prediction stage). | Still stale — recall returns whatever matches; no active prediction or pre-activation before the query. Long-term gap unchanged. |
| **Prediction-Driven Processing** | Perceiving = matching input against expectations, noting differences | Passive retrieval. | Still stale — could pre-activate likely facts before the user query completes; not implemented. |

**Key Takeaway:** Recall should be prediction-driven — pre-activate expected context based on conversation trajectory.

---

### Chapter 14: Reformulation

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Right Representation** | Hard problems become easy with the right description | F050: `nous/heart/query_expansion.py` `QueryExpander` runs Haiku to generate semantic variants at recall time, expanding a single query into `[query, variant1, variant2]`. | `(shipped, default OFF)` — `query_expansion_enabled=False`; the reformulation code is complete but does not run unless explicitly enabled. |
| **Multiple Descriptions** | Same problem should be re-described in multiple ways to find the best approach | F050 multi-query expansion (`query_expansion.py:66-93`): variants preserve intent while changing phrasing (synonyms, jargon vs plain, noun vs verb); all variants run through the same recall pipeline. | `(shipped, default OFF)` — `query_expansion_enabled=False`; reformulation runs at recall time only when enabled. |

**Key Takeaway:** Query reformulation before recall could dramatically improve retrieval quality at low cost.

---

### Chapter 15: Consciousness and Memory

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Reconstructive Recall (15.3)** | Remembering = reconstruction from fragments, not retrieval of stored text | F067: `heart.episode_chunks` stores verbatim transcript chunks (`models.py:415-452`), preserving tokens fact extraction discards; F022 Phase 3 assembles contradiction context; sleep Phases 4-5 compress/generalize but still return stored text. | Changed but MAJOR GAP REMAINS — still retrieves verbatim text. F067 is verbatim *preservation*, NOT on-the-fly assembly from fragments. Still fundamentally stored-text retrieval. |
| **Memories of Memories (15.4)** | Each recall creates new memory. You remember your last recall, not the original | F023 adds recall_count/last_recalled_at | Partial — track count but not context of recall |
| **Memory Rearrangement (15.7)** | Memories merge, compress, relink over time | Episode **compression is a `(stub)`** (`_phase_compress` logs and returns True, no work); F031 contradiction **consolidation is real** (SUPERSEDE/MERGE/KEEP_BOTH with a confidence safety-floor, `sleep_handler.py`); the reflect/generalize phases run; recency resolver tags current vs superseded; co-occurrence edges form new links at sleep `(default OFF)`; `graph_densifier` orphan backfill relinks. | Changed — contradiction consolidation (merge/supersede) shipped and active; episode compression is a stub; relink partially addressed via co-occurrence formation (default OFF) + orphan backfill. Still missing: DYNAMIC re-weighting during conversation (weights static at creation). |
| **Interruption Recovery (15.8)** | Need mechanism to restore previous state after interruption | Interruptible sleep + working memory | ✅ Well implemented |
| **Immanence Illusion (15.9)** | Feeling of total access = having the RIGHT subset pre-loaded | Working memory + frame activation | Validates intent-aware routing |

**Key Takeaway:** Reconstructive recall is the most fundamental long-term gap. Near-term: track recall context. Long-term: assemble from fragments.

---

### Chapter 16: Emotion

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Emotions ARE Cognition** | Not separate — rapid whole-system state changes that reprioritize everything | Censors track `activation_count`, `escalation_threshold` (warn→block→absolute), `false_positive_count` (`censors.py:175-218`); F031 adds `trigger_action`/`action_instruction` for richer per-censor responses. | Still stale — no whole-system priority shift on censor fire. F031 enables richer per-censor actions but NOT system-wide priority cascades. |
| **Urgency Cascades** | Critical events should shift whole-system priorities, not just log a warning | Censors log warnings/blocks and may execute read-only `trigger_action` payloads, but do not shift system-wide priorities. | Still missing — a fired censor should cascade priority changes across all downstream tasks; the urgency cascade is unimplemented. |
| **Emotional States = Micronemes** | Persistent background signals biasing all processing | Nothing — no ambient context bias layer. Working memory holds `current_task` but there is no microneme signal biasing all retrieval/decisions. | Completely absent — time-of-day, conversation-turn-count, mood/urgency flags, and other persistent ambient signals are not modeled. THE most impactful missing Minsky concept. |

**Key Takeaway:** Urgency cascades — when a censor fires or high-stakes decision detected, the whole system should shift priorities.

---

### Chapter 17: Development

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Growth Through Stages** | Each stage builds on previous. Can't skip stages | Skills are independent (`nous/skills/`); activation is match-rule-based, not prerequisite-gated (`bootstrap.py` static list, `subtask_executor.py` no prerequisite gates). | Still missing — no developmental/prerequisite model. |
| **Prerequisite Skills** | Advanced capabilities require foundational ones first | No skill dependency graph or prerequisite validation. | Still missing — advanced multi-agent coordination cannot check that basic task decomposition is learned first. |

**Key Takeaway:** Skills should have prerequisites. Can't run advanced multi-agent coordination without basic task decomposition.

---

### Chapter 18: Reasoning

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **All Reasoning = Chaining** | Connecting one mental state to the next through shared K-lines | Spreading activation CTE | ✅ Good alignment |
| **Magnitude from Multitude** | Multiple weak signals voting > single strong signal | Fixed spreading-activation decay formula (`settings.spreading_activation_decay`); no dynamic voting of independent retrieval paths yet. | Still — multiple weak signals voting is correct conceptually but not implemented; the formula is fixed. |
| **Robustness via Redundancy** | Redundant retrieval paths are a feature, not waste | Vector search + graph traversal (density-gated spreading activation) + temporal recall exist as separate paths; F040 densifies, F075 adds `happened_before` edges, F022 spreads via cosine-weighted edges. | Changed — paths still run INDEPENDENTLY, not converging/voting. The "let them vote" piece remains missing. |

**Key Takeaway:** Having vector search + graph traversal + temporal recall is correct. The missing piece is letting them converge/vote rather than running independently.

---

### Chapter 19: Words and Ideas

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Polynemes (19.5)** | Single signal activates different things in different memory systems. Meaning = distributed activation pattern | Heterogeneous graph: facts/decisions/episodes/procedures/chunks are separate node types; edges span types (fact→decision, fact→episode); spreading activation crosses types via multi-hop; F076 co-mention + F075 temporal edges link facts. | Changed — graph is now genuinely heterogeneous, but weights are uniform across types unless explicitly set at edge creation. No multi-view activation (semantic + temporal + causal + entity) like MAGMA validates. |
| **Weighing Evidence (19.7)** | Ambiguity resolved by accumulating weak evidence from many sources | Fixed spreading-activation formula (`COALESCE(e.weight, 1.0) * :decay`, `spreading_activation.py:107`); evidence is OR'd/summed across paths in the CTE, not voted. | Still stale — multiple paths are NOT voting; dynamic evidence accumulation from different source types (temporal + causal + semantic convergence) is not modeled. |
| **Language ≠ Thought (19.1)** | Words trigger distributed multi-system activation | Single-embedding approach | Validates MAGMA multi-view direction |

**Key Takeaway:** True meaning requires multi-view activation (semantic + temporal + causal + entity). Validates MAGMA.

---

### Chapter 20: Context and Ambiguity

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Context as Disambiguator (20.3)** | Same input means different things in different contexts | Working memory | Recall quality depends on working memory contents |
| **Micronemes (20.5)** | Tiny pervasive signals: mood, situation, time of day. Bias everything without being noticed | **Nothing** — no microneme layer. | **Completely absent** — still the most novel missing concept from the entire mapping. |
| **Distributed Memory (20.9)** | Memory distributed across agents. Recall assembles fragments | Memory is distributed across fact/decision/episode/procedure/chunk types connected by graph edges; F067 `episode_chunks` `(shipped, default OFF)` — `episode_chunks_enabled=False`; spreading activation activates fragments across the graph but is **density-gated** (`spreading_activation_enabled='auto'`, fires only when graph density ≥ 3.0 — often inactive on sparse graphs). | Partially addressed — cross-type distribution exists; spreading-activation fragment activation is conditional (density-gated, frequently dormant). Recall still returns stored entities; algebraic fragment ASSEMBLY (combine partial facts into one narrative) is still missing. |

**Key Takeaway:** Micronemes = ambient context biasing everything. Most impactful new concept for Nous.

---

### Chapter 21: Trans-Frames

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Cross-Domain Analogies** | Understanding one domain through another. Structural mapping between frames | No analogy detection. Frame selection is pure pattern matching (`frames.py:40-125`); graph edges store relation types but no cross-domain structural comparison. | Still missing — cannot detect when two different-domain facts share structural patterns. Would need domain classification + structural-signature extraction + cross-domain matching. |

**Key Takeaway:** Cross-domain analogy detection could improve both recall (find structurally similar situations) and learning (transfer lessons across domains).

---

### Chapter 22: Expression

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Listener Model** | Communication requires modeling what the listener already knows | Episode recall (`heart.list_episodes()`) + `ContextEngine.build()` assemble prior-conversation context (`context.py:102-150`); conversations are retrieved and deduped (`nous/cognitive/dedup.py`). | Now partially addressed — recall happens, but there's no explicit knowledge-state PREDICTION model; context assembly uses heuristics (frame selection, dedup) rather than predicting what the user can infer vs needs re-explaining. |
| **Paraphrase as Self-Test** | Restating in different terms reveals understanding vs memorization | No self-testing | Could validate fact understanding by attempting rephrase |

**Key Takeaway:** Shape responses by what Tim already knows from prior conversations. Don't re-explain established context.

---

### Chapter 23: Comparisons

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Understanding Through Contrast** | We understand by comparing to what we know. Differences are more informative than similarities | Contradiction detection exists (`contradicts` edges) but only at RETRIEVAL time (`retrieval_pipeline.py:149`); no contrast/comparison at STORAGE time. | Still missing — on intake Nous does not find related facts, extract key DIFFERENCES, and store a difference signature. Contradictions are found late (recall), not early (intake). No contrastive feature extraction. |

**Key Takeaway:** When storing new facts, explicitly contrast them with existing related facts. What's different is more informative than what's similar.

---

### Chapter 24: Frames (Extended)

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Default Assumptions** | Frames should pre-load expected context. "Debug" should assume recent errors | `_frame_to_selection` (`frames.py:186-195`) copies only `default_category`, `default_stakes`, `questions_to_ask` into `FrameSelection`. `agencies_to_activate` (`models.py:80`) is `(reserved, no consumer)` — `FrameSelection` has no such field and nothing reads it. `default_category`/`default_stakes` feed decision-recording; `questions_to_ask` is injected into context. | Now addressed (partial) — `questions_to_ask` is surfaced; `agencies_to_activate` pre-load is unimplemented, so "default context pre-load" remains shallow. |
| **Frame Recognizers** | Detect mid-conversation when a different frame is needed | Frame selection runs once at session start (`frames.py:40-62`). | Still missing — no dynamic mid-conversation frame recognition/switching. |
| **Frame Competition** | Multiple frames bid for activation. Strongest wins | `FrameSelection` returns one `frame_id`; single frame per session. | Still missing — no multi-frame bidding. |

**Key Takeaway:** Frames need three upgrades: default assumptions, mid-conversation recognition, and competition.

---

### Chapter 25: Frame Arrays

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Coordinated Frames** | Complex situations require multiple coordinated frames, not just one | Single-frame-at-a-time architecture throughout; `FrameSelection` returns one `frame_id` (`frames.py:185-195`), immutable per session (`context.py` frame is singular). | Still missing — no frame composition, multi-frame context merging, or inter-frame priority/conflict resolution. Not architected. |

**Key Takeaway:** Complex tasks (like "research + write + fact-check an article") require multiple frames active simultaneously.

---

### Chapter 26: Language and Thought

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Language Channels Thought** | Language doesn't cause thought — it directs it. Words are frame-activators | Embeddings capture semantic position | ✅ Embeddings function as word → activation mapping |
| **Inner Speech** | Using language to control yourself | System prompts + frame instructions | ✅ System prompts ARE inner speech — Nous talks to itself about how to behave |

**Key Takeaway:** Our system prompts are Minsky's "inner speech." This is correct and should be recognized as such.

---

### Chapter 27: Censors and Jokes

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Censors from Failure (27.1)** | When thought → bad outcome, create censor to suppress that thought BEFORE it forms | Post-turn `SelfMonitor` (`monitor.py:148-183`) **does auto-create censors** from high-surprise tool errors (dedup + per-session cap, `action="warn"`). But Sleep Phase 1 (`_phase_review_decisions`, `sleep_handler.py:546-558`) reviews pending decisions WITHOUT generating censors. | Partially wired — tool-error auto-censors exist (post-turn); the gap is **sleep-time auto-censor from bad *decision* outcomes** (the review phase doesn't generate them). (F012 procedure learning also captures error→recovery pairs; see Ch. 30.) |
| **Suppressors vs Censors (27.2)** | Suppressors block after formation. Censors prevent formation. Censors more efficient | All censors operate as post-hoc output matchers (regex + embedding-semantic match); F031 `trigger_action` runs read-only tools when a censor fires, but the censor still fires AFTER output generation (`censors.py:127-144`, `censor_actions.py:22-29`). | Still stale — censors block/warn after reasoning, not before thought forms. No precursor-pattern detection during reasoning formation. |
| **Humor = Disabled Censor (27.8)** | Jokes let forbidden connections through momentarily | F031 `unblock_pattern` + `trigger_action` provide "unless"-clause semantics; escalation thresholds permit soft blocks (warn→block→absolute, `censors.py:180-193`) rather than only hard blocks. | Now addressed — censors are contextually soft, not just hard blocks; conditional unblock distinguishes (e.g.) dangerous real-world actions from safe educational discussion. |

**Key Takeaway:** Auto-learned censors from bad decision outcomes is the most impactful single feature. Sleep already reviews outcomes — just needs to generate censors.

---

### Chapter 28: Mind and World

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Mental Models of World** | Understanding = building internal models of how the world works | No simulation, world modeling, or predictive capability. The system is reactive — it retrieves and reasons over stored facts. | Still missing — no simulation substrate; cannot ask "what if I change X?" and run an internal model forward. Years away (paradigm shift). |
| **Prediction from Models** | Models allow "what if" reasoning | No forecasting; responds to explicit turn input. Sleep does reflection/learning but no forward prediction. | Still missing — same substrate gap as Mental Models. |

**Key Takeaway:** World-modeling and simulation is the long-term frontier. Years away but the ultimate direction.

---

### Chapter 29: Paranomes and Pronomes

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Cross-Domain Connectors** | Paranomes connect similar structures across different domains | Graph edges exist within type pairs (fact→fact, fact→decision, …); F021/F022 semantic edges, F076 co-mention, F075 temporal — none cross domain boundaries to map structural patterns (`graph_densifier.py:32-47` `_RELATION_MAP` is type-specific). | Still missing — no paranome / cross-domain structural connector; analogy detection / structural mapping across domains is absent (Low priority). |
| **Temporary Bindings (Pronomes)** | Short-lived variable bindings for multi-step reasoning. "It" = currently bound referent | Working memory holds context as flat text (`current_task`, `current_frame`, recent facts, `context.py:130-200`); F072 added chunk references but still flat key-value, not structured pronome slots. | Changed but still partial — working memory lacks structured temporary bindings. The system cannot explicitly track "X = current topic" vs "Y = the person just discussed"; bindings are implicit in text, not explicit data structures. |

**Key Takeaway:** Pronomes = temporary variable bindings for multi-step reasoning. Working memory needs structured slots, not just flat text.

---

### Chapter 30: Mental Models

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Knowing vs Believing (30.2)** | Different mechanisms. Contradictions OK in different contexts | The system detects `contradicts` edges (F022 Phase 3, `retrieval_pipeline.py:657-695`) and flags conflicts; directed supersession edges exist but are not context-aware. Facts are globally true/false. | Changed but still limited — contradictions are binary; the system cannot represent "Dr. Smith (true now)" vs "Dr. Jones (was true)" as simultaneous context-indexed truths. Requires a paradigm shift to context-indexed fact storage. |
| **Mental Models (30.5)** | Understanding = building and running internal models, not looking up answers | Nothing — retrieve and present | Paradigm shift — years away |
| **Mental Bugs (30.8)** | Systematic errors persist because you never test that part of your model | Auto-censors from *decision* outcomes are still not wired (tool-error auto-censors DO exist post-turn, `monitor.py:148-183`), and F012 procedure learning auto-creates procedures from observed error→recovery pairs, capturing failed paths so they aren't repeated. | Now partially addressed — F012 error→recovery capture + post-turn tool-error censors partially implement this; full coverage still needs sleep-time auto-censor generation from bad decision outcomes (Ch. 27). |

**Key Takeaway:** Context-dependent contradictions should be supported, not flagged as errors.

---

## Consolidated Gap Analysis

### ✅ Well-Aligned (6 concepts)

| Concept | Source | Nous Implementation |
|---|---|---|
| K-lines | Ch. 8 | graph_edges + spreading activation |
| Frozen Reflection | Ch. 6 | Episode summaries |
| Heterarchies | Ch. 3 | EventBus + graph edges |
| Self-protection | Ch. 6 | Absolute censors + identity versioning |
| Interruption Recovery | Ch. 15 | Interruptible sleep handler |
| Inner Speech | Ch. 26 | System prompts + frame instructions |
| Loop-Breakers | Ch. 5 | Absolute censors + values system |
| Identity as Narrative | Ch. 4 | agent_identity document |
| Distributed Architecture | Ch. 1/5 | Heart + Brain + Frame + Skills |

### 🟢 Now Addressed since 2026-03-29 (newly closed or substantially closed)

| Concept | Source | What Closed It |
|---|---|---|
| Gestalt Trap (partial — `recall_deep` only) | Ch. 2 | `recall_deep` chains L1→L2→L3 (default on); pre-turn context uses bare `search_facts` (no graph) |
| Meaning is Relational (edge admission) | Ch. 6 | F040 per-relation thresholds + F076 co_mention excluded from density gate |
| Societies of Memories (too aggressive linking) | Ch. 8 | F040 admission control + density-gated spreading activation |
| Co-activation / edge FORMATION from experience `(shipped, default OFF)` | Ch. 11 | PR #475 `build_cooccurrence_edges` (`cooccurrence_linking_enabled=False`) + F076 co-mention; formation only |
| Exception Principle / Contextual Censor Exceptions | Ch. 12/27 | F031 `unblock_pattern` + `trigger_action` payloads |
| Query Reformulation / Multiple Descriptions `(shipped, default OFF)` | Ch. 14 | F050 multi-query expansion (`query_expansion_enabled=False`) |
| Memory Rearrangement (merge/relink) | Ch. 15 | F031 contradiction consolidation + recency resolver are real; co-occurrence relink `(default OFF)`; episode compress is a `(stub)` |
| Distributed Memory (fragment activation, partial) | Ch. 20 | Cross-type distribution real; spreading activation is density-gated (often dormant) + F067 chunks `(shipped, default OFF)` |
| Listener Modeling (partial) | Ch. 22 | Episode recall + context assembly + dedup |
| Frame Default Assumptions (partial) | Ch. 24 | Frame `default_*` / `questions_to_ask` surfaced in `FrameSelection` |
| Mental Bugs (partial) | Ch. 30 | F012 error→recovery procedure learning |

### 🟡 Partially Implemented

| Concept | Source | Current State | Enhancement Needed |
|---|---|---|---|
| B-Brains | Ch. 6 | Frames selected once at session start (passive) | Active mid-conversation monitoring |
| Memories of Memories | Ch. 15 | `recall_count`/`last_recalled_at` (F023) tracked | Track/store recall CONTEXT, not just count |
| Sleep Consolidation | Ch. 15 | review/reflect/generalize + F031 contradiction consolidation are real; **prune & compress are `(stub)`s** (log + return True) | Real prune/compress; auto-censor from bad decision outcomes |
| Evidence Accumulation | Ch. 18/19 | Vector + graph + temporal paths run independently | Multiple paths converging/voting |
| Anti-Dumbbell | Ch. 11 | Confidence scoring + epistemic routing (§2) | Explicit third-option requirement for high-stakes |
| Reconstructive Recall | Ch. 15 | F067 verbatim chunk preservation | On-the-fly assembly from fragments |
| Context-Dependent Contradictions | Ch. 30 | Directed supersession + recency resolver | Context-indexed (not directed) truth values |

### 🔴 Still Missing

| Concept | Source | Priority | Description |
|---|---|---|---|
| **Edge-Weight Plasticity (strengthen/decay)** | Ch. 8 | **High** | Weights FROZEN at creation; no strengthen-by-use / decay-by-disuse. Zero `UPDATE`s on `graph_edges.weight`. The single highest-leverage remaining gap. |
| **Micronemes** | Ch. 16/20 | High | Ambient context signals biasing everything |
| **Auto-Learned Censors** | Ch. 27 | High | Sleep → bad *decision* outcomes → censors (tool-error censors exist post-turn in `monitor.py`; sleep-time decision-outcome generation not wired) |
| **Relational Fact Storage** | Ch. 11 | High | Facts as relational graphs, not atomic strings |
| **Mid-Conversation Frame Recognition + Competition** | Ch. 24/25 | Medium | Dynamic frame switching, multi-frame bidding/arrays |
| **Urgency Cascades** | Ch. 16 | Medium | Whole-system priority shifts on critical events |
| **Self-Debugging** | Ch. 10 | Medium | Detect and fix own reasoning errors |
| **Early Interception Censors** | Ch. 27 | Medium | Precursor pattern detection before output forms |
| **Stall-Detection / Alternative Strategies** | Ch. 3 | Medium | Detect stalled goals, try a different approach |
| **Evidence Voting** | Ch. 18/19 | Medium | Retrieval paths converge/vote instead of running independently |
| **Pronome Bindings** | Ch. 29 | Low | Structured temporary variable slots |
| **Cross-Domain Analogies / Paranomes** | Ch. 21/29 | Low | Structural mapping between domains |
| **Mental Simulation** | Ch. 28/30 | Future | Internal model building and testing |

---

## Design Principles (12, derived from all 30 chapters)

1. **Papert's Principle** (Ch. 7/10) — Before building new capabilities, improve administration of existing ones.
2. **Meaning Through Connection** (Ch. 6/11) — A fact with few connections has little meaning. Indiscriminate connections = confused meaning. Aim for selective, relevant connections.
3. **Plasticity Over Storage** (Ch. 8/15/27) — A system that reshapes itself from experience is fundamentally more intelligent than one that accumulates.
4. **Protect the Core** (Ch. 6/5) — Core identity and values must change slowly and be partially tamper-proof. Self-knowledge is dangerous.
5. **Accept Incompleteness** (Ch. 6/15) — Summaries, self-models, and introspection are inherently incomplete. Design for this.
6. **Context is Primary** (Ch. 20) — Disambiguation is the core cognitive challenge. Same input, different context = different meaning.
7. **Fringes Matter** (Ch. 8) — Weak, easily-displaced connections (defaults, assumptions) are as important as strong ones.
8. **Learn from Failure** (Ch. 27) — Every bad outcome should leave a trace (censor) that prevents the same failure path.
9. **Don't Notice Too Much** (Ch. 12) — Over-attending to detail prevents generalization. Admission control is essential.
10. **Anti-Dumbbell** (Ch. 11) — Always seek a third alternative before accepting binary framings.
11. **Substitution Principle** (Ch. 1) — Never let complexity hide inside a single agent. Decompose into sub-societies.
12. **Distributed Initiative** (Ch. 1/5) — Components should initiate, not just respond. A distributed toolset controlled by a central orchestrator is still a homunculus.

---

## Recommended Feature Sequence

Based on Papert's Principle — prioritize better administration of existing capabilities:

### Phase 1: Better Administration (Next 2-4 weeks)
1. **F023 — Admission Control** — Gate what goes in. Already spec'd, PR open.
2. **Frame-gating for skills** — Stop skills firing in wrong contexts.
3. **Edge strength on graph_edges** — Add `strength` float, decay formula, use-based reinforcement.

### Phase 2: Plasticity (4-8 weeks)
4. **Auto-learned censors** — Sleep Phase 2: detect bad outcomes → create censors.
5. **Sleep prune/compress** — Implement Phases 2-3. Merge, retire, relink.
6. **Microneme prototype** — Time-of-day + conversation-turn-count as ambient bias signals.

### Phase 3: Intelligence (8-12 weeks)
7. **Intent-aware retrieval router** — Multiple retrieval paths voting.
8. **Active B-brain** — Mid-conversation monitoring for cognitive dysfunction.
9. **Query reformulation** — Re-describe queries before recall.
10. **Frame defaults + recognition** — Pre-load context, mid-conversation switching.

### Phase 4: Paradigm Shifts (Future)
11. **Reconstructive recall** — Assemble from fragments.
12. **Pronome bindings** — Structured temporary variable slots.
13. **Mental simulation** — Internal model building.
14. **Cross-domain analogies** — Structural mapping between domains.

---

## References

- Minsky, M. (1986). *The Society of Mind*. Simon & Schuster.
  - Full text: http://aurellem.org/society-of-mind/index.html
  - All 30 chapters analyzed
- Zhang, L. et al. (2026). "Adaptive Memory Admission Control for LLM Agents." arXiv:2603.04549.
- Nous codebase: https://github.com/tfatykhov/nous
