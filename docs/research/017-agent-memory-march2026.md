# 017: Agent Memory Research — March 2026 Field Update

**Papers reviewed:** 11 new papers (March 2026)
**Date reviewed:** 2026-03-28
**Relevance:** Critical — massive acceleration in the field; ICLR 2026 MemAgents Workshop signals institutionalization

---

## Paper Index

| ID | Title | Source | Date | Deep Dive? |
|----|-------|--------|------|------------|
| P10 | SleepGate: Learning to Forget | arXiv:2603.14517 | Mar 2026 | ✅ Yes |
| P11 | CraniMem: Cranial Inspired Gated and Bounded Memory | arXiv:2603.15642 | Mar 2026 | ✅ Yes |
| P12 | Pichay: The Missing Memory Hierarchy (Demand Paging) | arXiv:2603.09023 | Mar 2026 | ✅ Yes |
| P13 | Trajectory-Informed Memory Generation | arXiv:2603.10600 | Mar 2026 | ✅ Yes |
| P14 | AdaMem: Adaptive Memory for LLM Agents | arXiv:2603.16496 | Mar 2026 | No |
| P15 | SuperLocalMemory V3 (Fisher Information Geometry) | arXiv:2603.14588 | Mar 2026 | No |
| P16 | Multi-Agent Memory as Computer Architecture | arXiv:2603.10062 | Mar 2026 | No |
| P17 | CoMAM: RL-Optimized Memory Sub-Agents | arXiv:2603.12631 | Mar 2026 | No |
| P18 | Modular Memory for Continual Learning | arXiv:2603.01761 | Mar 2026 | No |
| P19 | Memory for Autonomous LLM Agents (Survey) | arXiv:2603.07670 | Mar 2026 | No |
| P20 | Adaptive Memory Admission Control (Zhang et al.) | arXiv:2603.04549 | Mar 2026 | No (used in F023) |

---

## Key Convergent Findings Across Papers

### 1. Gated Admission Is Table Stakes
Every paper that builds a memory system includes an admission gate. Storing everything is universally identified as a failure mode. SleepGate (conflict detection), CraniMem (goal-conditioned cosine), A-MAC/Zhang (5-dimension scoring), TIM (consolidation threshold) — different mechanisms, same principle.

### 2. Typed Multi-Store Is the Consensus Architecture
No competitive system uses a single memory store. The minimum viable architecture includes episodic (short-range) + semantic/KG (long-range) with different lifecycles. CraniMem, AdaMem, and the Memory Survey all converge on this independently.

### 3. Forgetting Is a Feature, Not a Bug
SleepGate is the strongest statement: without principled forgetting, retrieval accuracy degrades to ~8% under proactive interference. With it: 99.5%. CraniMem adds bounded memory with scheduled pruning. The Survey identifies "learned forgetting" as the top open challenge.

### 4. The Context Window Is L1, Not RAM
Pichay's reframing is paradigm-shifting. The field has been making L1 bigger (1M→10M token windows) instead of building a memory hierarchy. 21.8% of production tokens are structural waste. The inverted cost model (keeping > faulting after 1 turn) changes optimal policy fundamentally.

### 5. Execution Traces Are Underexploited Learning Data
TIM shows +28.5pp on complex tasks by extracting typed tips from execution traces. EvoSkill discovers skills by analyzing failures. Both point to the same gap: most agent systems learn only from explicit teaching, not from implicit observation of their own behavior.

---

## Deep Dive Summaries

### P10 — SleepGate (arXiv:2603.14517)
Sleep-inspired memory consolidation with three mechanisms:
- **Conflict-aware temporal tagger:** Semantic signatures (d_s=64) + LSH for O(1) conflict detection. Marks entries as superseded when a newer entry has cosine similarity > δ.
- **Forgetting gate:** 2-layer MLP (74K params) scores entries on 7 features: key, value, age, semantic signature, superseded flag, cumulative attention, global context. Three-way action: keep/compress/evict.
- **Consolidation module:** Retention-weighted key averaging + recency-biased cross-attention for value compression. Cluster-based merging.

Results: 99.5% retrieval at n=5 vs <18% for all baselines. Sharp failure cliff at n≥15 (signature capacity limitation). H2O actively harmful under proactive interference — "keep heavy hitters" = "keep stale values."

Key insight for Nous: supersession flag is the missing primitive. Without knowing WHAT is stale, all forgetting policies are blind.

### P11 — CraniMem (arXiv:2603.15642)
Cranial-inspired bounded memory with:
- **Goal-conditioned gating:** cosine(input, current_goal) < threshold → discard before encoding
- **Utility tagging:** (Importance + Surprise + Emotion) / 3 via LLM tagger
- **Bounded FIFO episodic buffer + structured knowledge graph**
- **Scheduled consolidation:** ReplayScore = BaseUtility × (1 + α × FreqBonus)

Beats Mem0: +38% F1 clean, +58% F1 under noise, 3.3× less noise degradation. But 25× slower latency.

Key insight for Nous: goal-conditioned gating maps directly to cognitive frames as a 6th A-MAC dimension.

### P12 — Pichay (arXiv:2603.09023)
OS virtual memory concepts applied to LLM context windows:
- **Four-level hierarchy:** L1 (context), L2 (session cache with fault-pinning), L3 (compressed history), L4 (persistent stores)
- **FIFO eviction:** τ=4 turns, s_min=500 bytes. 0.0254% fault rate.
- **Retrieval handles:** Self-describing stubs that models recognize and cooperatively re-request
- **Cooperative protocol:** Models have incentive to help manage their own cache (cleaner context = better attention)

Production data: 857 sessions, 4.45B tokens. 21.8% structural waste. 93% peak context reduction (but that session thrashed at 97% fault rate).

Key insight for Nous: the inverted cost model — keeping is expensive (per-turn), faulting is cheap (one-time). Break-even is 1 turn.

### P13 — Trajectory-Informed Memory (arXiv:2603.10600)
Typed tip extraction from execution traces:
- **3 tip types:** Strategy (clean success), Recovery (failure→fix), Optimization (inefficient success)
- **3-stage extraction:** Trajectory analysis → Causal attribution → Tip generation
- **Dual generalization:** Every tip in domain-specific AND generic form
- **Subtask decomposition:** Extract per-phase, not per-task → cross-domain transfer

+28.5pp on complex AppWorld tasks (149% relative). Cosine retrieval (73.8%) ≈ LLM-guided (73.2%). Consolidation essential: dedup → conflict resolution → synthesis.

Key insight for Nous: mine failures and inefficiencies, not just successes. Recovery tips are the highest-value learning.

---

## Gaps Identified → Feature Specs Created

| Gap | Paper Source | Feature Spec |
|-----|-------------|--------------|
| No supersession detection / principled forgetting | SleepGate, Survey | **F027 — Supersession Detection** |
| No context eviction / working set management | Pichay, SleepGate | **F028 — Context Demand Paging** |
| No learning from failures / execution trace mining | TIM, EvoSkill | **F029 — Trajectory Learning** |
| No frame-alignment in admission control | CraniMem | F023 enhancement (add frame-relevance dimension) |
| No scheduled consolidation loop | SleepGate, CraniMem, Survey | F027 Phase 3 + F008 activation |
| No access frequency tracking | CraniMem | F027 (last_accessed tracking) |
| No stale retrieval rate metric | SleepGate | F027 success metrics |

---

## Relationship to Prior Research (Doc 016)

Doc 016 covered 9 papers (P1-P9, 2025-early 2026). This update adds 11 more (P10-P20, March 2026).

The field has moved from **"agents need memory"** (P1-P3, position papers) to **"here's how to build it"** (P10-P13, engineered systems with benchmarks). Key shifts:
- From retrieval-focused → admission+forgetting-focused
- From single benchmarks → production deployment data (Pichay)
- From conceptual → mathematical foundations (SuperLocalMemory V3)
- From isolated → workshop-level community (ICLR MemAgents)

The March 2026 explosion validates Nous's architectural direction while exposing specific implementation gaps (supersession, context paging, trajectory learning) that the three new feature specs address.
