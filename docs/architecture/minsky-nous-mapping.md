# Minsky → Nous Architecture Mapping

**Source:** Marvin Minsky, *The Society of Mind* (1986)
**Compiled:** March 19, 2026
**Chapters Analyzed:** All 30 (1–30)

---

## Executive Summary

All 30 chapters of *The Society of Mind* were analyzed against Nous's current codebase. The central finding: **Nous has built the right data structures but lacks the plasticity layer.** Minsky's mind constantly reshapes itself — edges strengthen, censors auto-create, memories reconstruct differently each time. Nous stores and retrieves but doesn't reshape.

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
| **Substitution Principle** | Replace any complex agent with a subsociety of simpler agents. Never hide complexity in one box | Subtask spawning | Weak — flat subtask list, not hierarchical agent decomposition |
| **Common Sense** | "An immense society of hard-earned practical ideas." Not simple at all | Facts + censors + procedures | Good — facts are learned heuristics, censors are rule exceptions |
| **Amnesia of Infancy** | We forget how hard basic skills were to learn, making them seem innate | Episode consolidation / sleep | Partial — we consolidate but don't track what was hard to learn |

**Key Takeaway:** The Substitution Principle means Nous should never let complexity hide inside a single tool call or handler. If something is complex, decompose it.

---

### Chapter 2: Wholes and Parts

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Three-Level Problem** | Understanding requires: how each part works, how parts interact, how interactions produce global behavior | Facts (L1), graph edges (L2), spreading activation (L3) | L1 good, L2 present, L3 barely exists |
| **Gestalt Trap** | "The whole is more than the sum of parts" is a pseudo-explanation. Specify the interactions | Facts can be isolated without knowing connections | Risk — storing facts without connections reproduces the trap |
| **Containment = Arrangement** | No single board contains a mouse. Six boards arranged correctly do. Properties emerge from arrangement | Procedures encode sequences | Partial — procedures capture sequences but not constraint satisfaction |
| **Easy Things Are Hard** | Simplest perceptual tasks require enormous hidden machinery | No introspective cost model | Missing — Nous doesn't know which tasks were harder than expected |

**Key Takeaway:** Level 3 (emergent behavior from local interactions) is the core missing layer. Without it, the memory system is a filing cabinet, not a society.

---

### Chapter 3: Conflict and Compromise

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Noncompromise (3.2)** | Conflicts migrate upward. When subordinates stall, rivals seize control | Task cancellation | No conflict-migration detection |
| **Heterarchies (3.4)** | Not everything is hierarchical. Loops require memory | EventBus + graph edges | ✅ Working well |
| **Pain/Pleasure Simplify (3.6)** | Both narrow focus. Too much simplification degrades the self | Block-level censors = pain | Risk: too many block censors over-simplify |

**Key Takeaway:** Stalled subtasks should trigger alternative strategies, not just timeout.

---

### Chapter 4: The Self

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **One Self or Many** | Self is a society of sub-agents with different concerns | Single agent_identity | Frames could be "partial selves" with own priorities |
| **Self-Control via Censors** | You don't resist bad actions by willpower — you prevent them from being considered | Absolute censors | ✅ Good alignment |
| **Identity as Narrative** | Personal identity is a story, not a fact | agent_identity document | ✅ Exactly this — a story Nous tells itself |

**Key Takeaway:** Each frame could be a "partial self" with its own defaults, priorities, and active skills.

---

### Chapter 5: Individuality

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Circular Causality (5.1)** | Goals form feedback loops, not linear chains. Neither cause is "first" | No goal-loop tracking | Missing — no way to represent mutually reinforcing goals |
| **Loop-Breakers (5.2)** | Dogma and external anchors break infinite regress. "Just because!" is legitimate self-regulation | Absolute censors + values | ✅ Censors are exactly Minsky's loop-breakers |
| **Homunculus Fallacy (5.3)** | A central unified Self explains nothing. Must be distributed agencies | Heart + Brain + Frame + Skills distributed | ⚠️ Architecturally correct but LLM layer risks being the homunculus |

**Key Takeaway:** The deepest architectural gap — Heart/Brain/Skills should be able to initiate actions, not just respond to LLM queries. Otherwise we have a distributed toolset controlled by a central homunculus.

---

### Chapter 6: Insight and Introspection

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **B-Brains (6.4)** | Meta-cognitive monitor watches for dysfunction and intervenes | Frames (passive) | Frames don't actively monitor mid-conversation |
| **Frozen Reflection (6.5)** | Can't observe a process without changing it. Accept incompleteness | Episode summaries | ✅ Good alignment |
| **Meaning is Relational (6.9)** | Indiscriminate connections destroy meaning | Graph topology | Validates F023 — admission control on facts AND edges |
| **Self-Knowledge is Dangerous (6.13)** | Core ideals must change slowly. Some constraints tamper-proof | Absolute censors + identity versioning | ✅ Good alignment |

**Key Takeaway:** Active B-brain monitoring is the biggest gap. Nous needs to detect and intervene on its own cognitive failures mid-conversation.

---

### Chapter 7: Problems and Goals

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Uncommon Sense (7.2)** | Common sense requires vast knowledge networks | Retrieval pipeline | Validates that retrieval is harder than storage |
| **Difference-Engines (7.8)** | Detect difference between current and desired state, find agent that reduces it | Task system | Tasks are "do X" not "reduce this gap" |
| **Papert's Principle (7.10)** | Most crucial growth = new administrative ways to use existing skills | — | **THE design principle for Nous** |

**Key Takeaway:** Before building any new capability, ask: "Can we get more from what we already have by administering it better?"

---

### Chapter 8: A Theory of Memory

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **K-lines (8.1)** | Memory stored near agents that created it. Reactivate K-line → restore mental state | graph_edges + spreading activation | ✅ Core concept implemented |
| **Level-bands / Fringes (8.4-8.7)** | Strong connections at middle detail, weak "fringe" connections at extremes. Fringes = default assumptions | All edges equal weight | **Missing** — need `strength` field on graph_edges |
| **Societies of Memories (8.8)** | Connect K-lines to older K-lines, not all active agents | FactGraphLinker | Correct pattern but too aggressive — needs edge admission control |
| **Layers (8.11)** | B-brain controls zoom level. Each layer exploits the previous | Frames + sleep | Sleep creates abstractions — correct. No dynamic zoom |

**Key Takeaway:** Edge strength/fringes is the most actionable gap. Adding `strength` float to `graph_edges` enables decay, reinforcement, and default connections.

---

### Chapter 9: Summaries

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Summarizing is Hard** | Requires knowing what to OMIT — harder than inclusion | Episode summaries | Summaries are flat text, should be hierarchical (outline trees) |
| **Bridge-Definitions** | Connect new concepts to existing ones. Note HOW related, not just THAT similar | Cosine similarity | We know things are similar but not WHY — structural gap |

**Key Takeaway:** Episode summaries should be structured (key decision → supporting facts → context), not flat prose.

---

### Chapter 10: Papert's Principle Extended

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Meta-skills Grounded in Domain** | Can't learn to think about thinking without first thinking about something specific | Frames grounded in task types | ✅ Correct pattern |
| **Debugging as Meta-skill** | A system that debugs its own reasoning > one with better initial reasoning | No self-debugging | Missing — can't detect or fix its own reasoning errors |
| **Administrative Intelligence** | "Society of Mind is about the administrative structure of intelligence" | Frame/routing/admission pipeline | Validates entire Nous direction |

**Key Takeaway:** Self-debugging capability is more valuable than better initial performance.

---

### Chapter 11: The Shape of Space

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Relational Meaning (11.1/11.5)** | Nothing has intrinsic meaning. Quality = relationship. Isolated signals meaningless | Facts stored atomically | **Critical gap** — facts stored as strings, not as relational graphs |
| **Society of Nearnesses (11.2-11.3)** | Space built from co-activation statistics. Similarity = co-activation frequency | recall_deep similarity scoring | Functional but opaque — nearness not explicitly modeled |
| **Manifold Learning (11.4)** | Local nearness → global geometry. Hierarchical correlation layers | Flat memory search | Missing — no tiered spatial structure |
| **Co-activation (11.3)** | Nerves that fire together wire together | Skill auto-activation (rules-based) | Gap — should be statistical co-activation, not pattern matching |
| **Egocentric → Allocentric (11.6)** | Progress from body-relative to world-relative understanding | Frames are conversation-centric | No third-person self-model for A2A reasoning |
| **Anti-Dumbbell (11.9)** | Always seek a third alternative before accepting binary framings | Confidence scoring, multi-reason deliberation | ✅ Partially implemented |
| **State-Diffing (11.8)** | Mirror agencies: freeze state, work, compare before/after | Decisions recorded | No explicit before/after diff on episodes |

**Key Takeaway:** Move from a catalog of facts toward a society of related signals where retrieval is topology-aware and meaning emerges from connection density.

---

### Chapter 12: Learning Meaning

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Four Types of Learning** | Uniframing, accumulating, reformulating, trans-framing | Mostly accumulating | Missing reformulation and trans-framing |
| **Don't Notice Too Much** | Over-attending to detail prevents generalization | F023 admission control | ✅ Directly validates F023 |
| **~7 Item Limit** | Before forced generalization. Working memory can't hold more | No item limit on working memory | Should trigger generalization when threshold exceeded |
| **Exception Principle** | Censors should support "unless" clauses | Binary censors only | Censors can't express contextual exceptions |

**Key Takeaway:** F023 is Minsky's "don't notice too much" in engineering terms. Also: censors need exception/context support.

---

### Chapter 13: Seeing and Believing

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Expectations Shape Perception** | We see what we expect to see. Expectations are active predictions | No expectation-setting before recall | Missing — recall returns whatever matches, no prediction step |
| **Prediction-Driven Processing** | Perceiving = matching input against expectations, noting differences | Passive retrieval | Could pre-activate likely facts before user query completes |

**Key Takeaway:** Recall should be prediction-driven — pre-activate expected context based on conversation trajectory.

---

### Chapter 14: Reformulation

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Right Representation** | Hard problems become easy with the right description | Single query → single recall | Nous doesn't reformulate queries before recall |
| **Multiple Descriptions** | Same problem should be re-described in multiple ways to find the best approach | One-shot retrieval | Could run the same query through multiple reformulations |

**Key Takeaway:** Query reformulation before recall could dramatically improve retrieval quality at low cost.

---

### Chapter 15: Consciousness and Memory

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Reconstructive Recall (15.3)** | Remembering = reconstruction from fragments, not retrieval of stored text | Verbatim fact retrieval | **Major gap** — should assemble from fragments |
| **Memories of Memories (15.4)** | Each recall creates new memory. You remember your last recall, not the original | F023 adds recall_count/last_recalled_at | Partial — track count but not context of recall |
| **Memory Rearrangement (15.7)** | Memories merge, compress, relink over time | Sleep Phase 4 only adds | Missing — need merge/compress/relink in sleep |
| **Interruption Recovery (15.8)** | Need mechanism to restore previous state after interruption | Interruptible sleep + working memory | ✅ Well implemented |
| **Immanence Illusion (15.9)** | Feeling of total access = having the RIGHT subset pre-loaded | Working memory + frame activation | Validates intent-aware routing |

**Key Takeaway:** Reconstructive recall is the most fundamental long-term gap. Near-term: track recall context. Long-term: assemble from fragments.

---

### Chapter 16: Emotion

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Emotions ARE Cognition** | Not separate — rapid whole-system state changes that reprioritize everything | No emotional modeling | Missing — no urgency cascades |
| **Urgency Cascades** | Critical events should shift whole-system priorities, not just log a warning | Censors log warnings | Missing — a fired censor should cascade priority changes |
| **Emotional States = Micronemes** | Persistent background signals biasing all processing | Nothing | Confirms micronemes are the key missing dimension |

**Key Takeaway:** Urgency cascades — when a censor fires or high-stakes decision detected, the whole system should shift priorities.

---

### Chapter 17: Development

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Growth Through Stages** | Each stage builds on previous. Can't skip stages | No developmental model | Missing — no prerequisite tracking for skills |
| **Prerequisite Skills** | Advanced capabilities require foundational ones first | Skills are independent | Should have skill dependency graph |

**Key Takeaway:** Skills should have prerequisites. Can't run advanced multi-agent coordination without basic task decomposition.

---

### Chapter 18: Reasoning

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **All Reasoning = Chaining** | Connecting one mental state to the next through shared K-lines | Spreading activation CTE | ✅ Good alignment |
| **Magnitude from Multitude** | Multiple weak signals voting > single strong signal | Fixed formula | Should let multiple retrieval paths converge |
| **Robustness via Redundancy** | Redundant retrieval paths are a feature, not waste | Vector + graph + temporal | ✅ Multiple paths exist — need to make them vote |

**Key Takeaway:** Having vector search + graph traversal + temporal recall is correct. The missing piece is letting them converge/vote rather than running independently.

---

### Chapter 19: Words and Ideas

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Polynemes (19.5)** | Single signal activates different things in different memory systems. Meaning = distributed activation pattern | Heterogeneous graph traversal | Crude polyneme — but all memory types weighted equally |
| **Weighing Evidence (19.7)** | Ambiguity resolved by accumulating weak evidence from many sources | Fixed formula | Should be dynamic — multiple paths voting |
| **Language ≠ Thought (19.1)** | Words trigger distributed multi-system activation | Single-embedding approach | Validates MAGMA multi-view direction |

**Key Takeaway:** True meaning requires multi-view activation (semantic + temporal + causal + entity). Validates MAGMA.

---

### Chapter 20: Context and Ambiguity

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Context as Disambiguator (20.3)** | Same input means different things in different contexts | Working memory | Recall quality depends on working memory contents |
| **Micronemes (20.5)** | Tiny pervasive signals: mood, situation, time of day. Bias everything without being noticed | **Nothing** | **Completely absent** — most novel missing concept |
| **Distributed Memory (20.9)** | Memory distributed across agents. Recall assembles fragments | Graph distributes across types | Correct distribution, missing assembly step |

**Key Takeaway:** Micronemes = ambient context biasing everything. Most impactful new concept for Nous.

---

### Chapter 21: Trans-Frames

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Cross-Domain Analogies** | Understanding one domain through another. Structural mapping between frames | No analogy detection | Missing — can't detect when two different-domain facts share structural patterns |

**Key Takeaway:** Cross-domain analogy detection could improve both recall (find structurally similar situations) and learning (transfer lessons across domains).

---

### Chapter 22: Expression

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Listener Model** | Communication requires modeling what the listener already knows | Recall of prior conversations | Partial — we recall context but don't systematically model Tim's current knowledge state |
| **Paraphrase as Self-Test** | Restating in different terms reveals understanding vs memorization | No self-testing | Could validate fact understanding by attempting rephrase |

**Key Takeaway:** Shape responses by what Tim already knows from prior conversations. Don't re-explain established context.

---

### Chapter 23: Comparisons

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Understanding Through Contrast** | We understand by comparing to what we know. Differences are more informative than similarities | No systematic comparison | Missing — no mechanism to compare new facts/decisions against existing ones |

**Key Takeaway:** When storing new facts, explicitly contrast them with existing related facts. What's different is more informative than what's similar.

---

### Chapter 24: Frames (Extended)

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Default Assumptions** | Frames should pre-load expected context. "Debug" should assume recent errors | Frames set mode only | Missing — frames don't pre-load contextual defaults |
| **Frame Recognizers** | Detect mid-conversation when a different frame is needed | Static frame selection at session start | Missing — no mid-conversation frame switching |
| **Frame Competition** | Multiple frames bid for activation. Strongest wins | Single frame active | Missing — no multi-frame bidding |

**Key Takeaway:** Frames need three upgrades: default assumptions, mid-conversation recognition, and competition.

---

### Chapter 25: Frame Arrays

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Coordinated Frames** | Complex situations require multiple coordinated frames, not just one | Single frame active | Missing — no frame composition or coordination |

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
| **Censors from Failure (27.1)** | When thought → bad outcome, create censor to suppress that thought BEFORE it forms | Manual censor creation only | **Missing** — no auto-learning from bad outcomes |
| **Suppressors vs Censors (27.2)** | Suppressors block after formation. Censors prevent formation. Censors more efficient | All censors are suppressors (regex on output) | No early interception at reasoning level |
| **Humor = Disabled Censor (27.8)** | Jokes let forbidden connections through momentarily | — | Suggests censors should be contextually soft, not just hard blocks |

**Key Takeaway:** Auto-learned censors from bad decision outcomes is the most impactful single feature. Sleep already reviews outcomes — just needs to generate censors.

---

### Chapter 28: Mind and World

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Mental Models of World** | Understanding = building internal models of how the world works | No world model | Missing — no simulation capability |
| **Prediction from Models** | Models allow "what if" reasoning | Reactive only | Nous responds to queries, doesn't predict or simulate |

**Key Takeaway:** World-modeling and simulation is the long-term frontier. Years away but the ultimate direction.

---

### Chapter 29: Paranomes and Pronomes

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Cross-Domain Connectors** | Paranomes connect similar structures across different domains | Graph edges within types only | Missing — no explicit cross-domain structural links |
| **Temporary Bindings (Pronomes)** | Short-lived variable bindings for multi-step reasoning. "It" = currently bound referent | Working memory holds current context | Partial — working memory is flat, not structured bindings |

**Key Takeaway:** Pronomes = temporary variable bindings for multi-step reasoning. Working memory needs structured slots, not just flat text.

---

### Chapter 30: Mental Models

| Minsky Concept | Description | Nous Implementation | Gap |
|---|---|---|---|
| **Knowing vs Believing (30.2)** | Different mechanisms. Contradictions OK in different contexts | Binary contradiction detection | Should support context-dependent contradictions |
| **Mental Models (30.5)** | Understanding = building and running internal models, not looking up answers | Nothing — retrieve and present | Paradigm shift — years away |
| **Mental Bugs (30.8)** | Systematic errors persist because you never test that part of your model | — | Auto-learned censors (Ch. 27) catch mental bugs |

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

### 🟡 Partially Implemented (7 concepts)

| Concept | Source | Current State | Enhancement Needed |
|---|---|---|---|
| B-Brains | Ch. 6 | Frames (passive) | Active mid-conversation monitoring |
| Societies of Memories | Ch. 8 | FactGraphLinker | Edge admission control |
| Memories of Memories | Ch. 15 | recall_count (F023) | Track recall context |
| Sleep Consolidation | Ch. 15 | Phases 1,4,5 active; 2,3 stubs | Merge/compress/relink |
| Evidence Accumulation | Ch. 18/19 | Fixed spreading activation | Multiple paths voting |
| Listener Modeling | Ch. 22 | Recall of prior conversations | Systematic knowledge-state tracking |
| Anti-Dumbbell | Ch. 11 | Confidence scoring | Explicit third-option requirement for high-stakes |

### 🔴 Missing (13 concepts)

| Concept | Source | Priority | Description |
|---|---|---|---|
| **Edge Strength/Fringes** | Ch. 8 | High | `strength` field, decay, reinforcement, defaults |
| **Micronemes** | Ch. 16/20 | High | Ambient context signals biasing everything |
| **Auto-Learned Censors** | Ch. 27 | High | Sleep → detect bad outcomes → create censors |
| **Relational Fact Storage** | Ch. 11 | High | Facts as relational graphs, not atomic strings |
| **Frame Defaults + Recognition** | Ch. 24 | Medium | Pre-load context, mid-conversation switching |
| **Query Reformulation** | Ch. 14 | Medium | Re-describe queries before recall |
| **Urgency Cascades** | Ch. 16 | Medium | Whole-system priority shifts on critical events |
| **Self-Debugging** | Ch. 10 | Medium | Detect and fix own reasoning errors |
| **Early Interception Censors** | Ch. 27 | Medium | Precursor pattern detection |
| **Reconstructive Recall** | Ch. 15/20 | Low (long-term) | Assemble from fragments |
| **Pronome Bindings** | Ch. 29 | Low | Structured temporary variable slots |
| **Cross-Domain Analogies** | Ch. 21 | Low | Structural mapping between domains |
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
