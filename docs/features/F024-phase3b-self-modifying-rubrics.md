# F024 Phase 3b — Self-Modifying Evaluation Rubrics

> **Status:** Draft v1
> **Parent:** F024 — Critic Agent (docs/features/F024-critic-agent.md, v3)
> **Relationship to Phase 3a:** Phase 3a covers adaptive routing (frame combination learning). Phase 3b covers adaptive evaluation (rubric evolution). Both are "Critic learns from experience" but in orthogonal dimensions.
> **Depends on:** F024 Phase 0 (shipped, PR #192), self-improvement-loop skill (ID: 3497a6cc)
> **Does NOT depend on:** F024 Phases 1-2 (parallelism). This operates on the existing self-improvement loop.
> **Inspired by:** Zhang et al. 2026, "Hyperagents" (arXiv:2603.19461) — DGM-H metacognitive self-modification pattern. Specifically: emergent rubric/checklist evolution and cross-domain transfer of meta-skills.
> **Respects F024 Hard Constraint #1:** No meta-Critic. The rubric evolves via data analysis, not via a second Critic watching the first.

---

## Problem Statement

The self-improvement loop evaluates Nous across 4 fixed dimensions:

| Dimension | Current Weight |
|-----------|---------------|
| Recall | 25% |
| Tool Selection | 25% |
| Confidence Calibration | 25% |
| Proactivity | 25% |

**Three problems with fixed rubrics:**

**1. Plateau effect.**
Scores have plateaued at 6.5-6.75 across 4 consecutive evaluations (March 20-21, 2026). The same dimensions produce the same scores because the evaluation can't see what it's not measuring.

**2. Lumped dimensions hide distinct failure modes.**
"Tool Selection" currently bundles tool choice accuracy and tool call efficiency (e.g., sequential calls where parallel was possible). These are different skills — you can pick the right tool but call it 5 times when once would suffice. The March 21 regression (Tool Selection 6→5) was caused by "excessive sequential calls" but scored identically to "wrong tool chosen."

**3. No feedback loop from outcomes to evaluation.**
The self-improvement loop scores itself, but those scores never connect to actual outcomes (did Tim correct something? Did the task succeed? Was the response useful?). Without this ground truth, the rubric can't evolve toward what actually matters.

**What the Hyperagents paper demonstrates:**
Zhang et al. (2026) show that agents with editable evaluation criteria (DGM-H) achieve compounding improvement (imp@50 = 0.63) while agents with fixed evaluation criteria plateau at ~0 improvement beyond initial gains. The key mechanism: metacognitive self-modification of the improvement process itself, not just the task execution. (Note: this is their overall finding; the specific causal link between rubric flexibility and the imp@50 metric is our inference, not their direct claim.)

---

## Design

### Core Principle

The self-improvement loop's evaluation dimensions are **data, not code.** They can be versioned, weighted, split, merged, and extended — all tracked in memory with full audit trail.

### Rubric Schema

```python
RubricVersion = {
    "version": "1.0.0",           # semver
    "created_at": "ISO timestamp",
    "parent_version": null,        # or previous version string
    "change_reason": "Initial fixed rubric",
    "dimensions": [
        {
            "name": "Recall",
            "weight": 0.25,
            "description": "Accuracy and completeness of memory retrieval",
            "scoring_criteria": "...",  # what 1-10 means for this dimension
            "min_weight": 0.10,         # floor
            "max_weight": 0.40          # ceiling
        },
        # ... more dimensions
    ],
    "outcome_correlations": {},     # populated after Phase 1
    "status": "active"             # active | superseded | rollback
}
```

### Three Modification Levels

**Level 1 — Weight Adjustment** (autonomous, logged)
- Shift weights between existing dimensions based on correlation with outcome signals
- Constraints: no dimension below 10% or above 40%, weights must sum to 1.0
- Trigger: Phase 1 correlation analysis shows significant divergence between dimension weights and outcome predictiveness
- Example: If Confidence Calibration correlates 0.7 with user satisfaction but Proactivity only 0.2, shift weight toward Calibration

**Level 2 — Dimension Split/Merge** (autonomous, logged, reversible)
- Split a dimension when sub-components show divergent outcome correlations
- Merge dimensions when they're redundant (correlation > 0.85 with each other)
- Constraints: total dimensions must remain between 3 and 7
- Trigger: statistical evidence from 30+ episodes showing sub-components behave independently
- Example: "Tool Selection" → "Tool Choice Accuracy" + "Tool Call Efficiency" (evidence: March 21 eval showed these diverging)

**Level 3 — New Dimension Discovery** (requires Tim's approval)
- Propose entirely new dimensions based on gap analysis
- Provide evidence: episodes where all existing dimensions scored high but outcome was poor
- Tim reviews evidence + proposed dimension before activation
- Example: "Memory Hygiene" — tracking whether Nous creates useful, non-redundant memory entries

### Outcome Signals (Ground Truth)

These are what rubric evolution optimizes toward:

| Signal | Source | Weight | Collection Method |
|--------|--------|--------|-------------------|
| User correction | Tim corrects Nous response | High | Detect "no, actually..." or explicit correction patterns |
| Task completion | Task finished without rework | Medium | Episode outcome tagging |
| Explicit feedback | Tim says "good job" / "that was wrong" | High | Sentiment + intent detection |
| Response reuse | Tim uses Nous output directly | Medium | Proxy: no follow-up correction |
| Self-correction needed | Nous catches own error mid-turn | Low | Track self-correction events |

**Anti-Goodhart guardrail:** If all dimension scores reach 8+ but outcome signals don't improve proportionally, flag the rubric as potentially gamed. Pause autonomous modifications and surface for Tim's review.

---

## Rollout Phases

### Phase 0: Outcome Signal Collection
**Effort:** ~3-4 hours implementation + 2 weeks passive collection
**Goal:** Build the ground truth dataset before changing anything
**Minimum data:** 50 episodes with at least one outcome signal each

What to build:
- Outcome signal detector — classify each episode's outcome (corrected, completed, praised, reworked)
- Store as structured facts: `{episode_id, outcome_signals: [...], self_improvement_scores: {...}}`
- Dashboard view (F021 integration) showing outcome distribution

What NOT to change:
- Rubric stays fixed at v1.0.0 (current 4 dimensions, equal weights)
- Self-improvement loop runs unchanged
- No automated modifications

**Success criteria:**
- 50+ episodes tagged with outcome signals
- Outcome signal distribution looks reasonable (not all positive or all negative)
- Can query: "show me episodes where scores were high but outcomes were poor"

### Phase 1: Correlation Analysis + Weight Adjustment
**Effort:** ~4-5 hours
**Depends on:** Phase 0 data (50+ episodes)

What to build:
- Correlation engine: for each dimension, compute Pearson/Spearman correlation with each outcome signal
- Weight adjustment algorithm: shift weights toward dimensions with stronger outcome correlation
- Version management: create v1.1.0 with new weights, log change reason with evidence

Constraints:
- Max 1 version change per week (no thrashing)
- Weight changes capped at ±0.05 per adjustment cycle
- All versions immutable — rollback = activate previous version, don't edit

**Success criteria:**
- Correlation analysis runs successfully on 50+ episodes
- Weight adjustment applied *only if* correlations meet significance threshold (p < 0.05 and |r| > 0.2); no-op is a valid outcome when evidence is insufficient
- If weights are adjusted: dimension-outcome correlations measured against holdout episodes
- No dimension weight hits floor (0.10) or ceiling (0.40) — if it does, it's a signal to split/merge

### Phase 2: Dimension Splits and Merges
**Effort:** ~5-6 hours
**Depends on:** Phase 1 stable, evidence of lumped dimensions

What to build:
- Sub-dimension analysis: within each dimension, track sub-scores or failure-mode categories
- Split detector: if sub-components correlate differently with outcomes (Δcorrelation > 0.3), propose split
- Merge detector: if two dimensions correlate > 0.85 with each other, propose merge
- Automatic split/merge execution with new version, documented evidence

Example split (pre-identified candidate):
- "Tool Selection" has two known failure modes:
  - Wrong tool chosen (e.g., web_search when recall_deep would work)
  - Right tool, inefficient usage (e.g., 6 sequential calls for something achievable in 1 batch)
- If Phase 1 data shows these predict different outcomes, split into "Tool Choice" + "Tool Efficiency"

**Success criteria:**
- Split and merge detectors run successfully on accumulated data
- Structural changes applied *only when* evidence thresholds are crossed (Δcorrelation > 0.3 for splits, r > 0.85 for merges); no structural change is a valid outcome when dimensions are stable
- If split/merge occurs: post-change rubric shows improved outcome prediction vs previous version
- Dimension count stays in 3-7 range

### Phase 3: Full Evolution (New Dimensions)
**Effort:** ~4-5 hours
**Depends on:** Phase 2 stable, gap analysis evidence

What to build:
- Gap detector: find episodes where all dimensions scored ≥7 but outcome was poor
- Root cause analyzer: categorize what went wrong in gap episodes
- New dimension proposer: generate candidate dimensions with evidence package
- Tim approval workflow: surface proposals with evidence, wait for approval before activating

Candidate dimensions (hypothesized, require evidence):
- "Memory Hygiene" — quality/relevance of facts created during episode
- "Frame Compliance" — how well behavior matched the selected frame's intent
- "Context Efficiency" — token/call economy (are we using 10 tools where 3 would suffice?)

**Success criteria:**
- At least one new dimension proposed with real evidence
- Tim approves or rejects with reasoning that improves future proposals
- Rubric version reflects the full evolution from v1.0.0

---

## Safety & Guardrails

1. **Dimension count bounds:** Minimum 3, maximum 7. Below 3 collapses evaluation; above 7 dilutes signal.
2. **Max 1 rubric version per week.** Prevents oscillation and ensures each version gets enough data.
3. **All versions immutable.** Rollback = reactivate old version, never edit in place.
4. **Anti-Goodhart guardrail.** Scores ≥8 across all dimensions + outcomes not improving = flag for review.
5. **No recursive self-evaluation.** The rubric evolves via correlation analysis on historical data, NOT by a meta-critic watching the critic (respects F024 Hard Constraint #1).
6. **Tim approval gate on Level 3.** New dimensions require human judgment — the system proposes, Tim decides.
7. **Weight change caps.** ±0.05 per cycle prevents dramatic swings.
8. **Rollback trigger.** If post-change outcome signals degrade by >15% over 2 weeks, auto-rollback to previous version and flag.

---

## Interaction with Existing Systems

**Self-improvement loop (skill 3497a6cc):**
- Currently hard-codes 4 dimensions. Phase 1+ replaces hard-coded dimensions with rubric lookup from memory.
- The skill itself becomes a consumer of the rubric, not the owner of evaluation criteria.

**F024 Phase 3a (Routing History):**
- Phase 3a learns which frame combinations work → routing optimization
- Phase 3b learns which evaluation criteria matter → evaluation optimization
- These are complementary and can share the outcome signal infrastructure from Phase 0.

**F024 Phase 0 (Critic as Frame Selector):**
- Diagnostic critics (6 currently) could eventually be rubric-governed too
- But NOT in scope for Phase 3b v1 — focus on self-improvement loop first

**F021 Memory Dashboard:**
- Phase 0 adds a new dashboard view: Rubric Evolution History
- Shows: version timeline, weight changes, dimension splits/merges, outcome correlations

---

## Open Questions

1. **Outcome signal reliability.** How accurately can we detect "user corrected the response" vs "user added new information"? False positives here corrupt the ground truth.
2. **Small sample sizes.** 50 episodes may not be enough for reliable correlation. Should we wait for 100+?
3. **Interaction with conversation compaction.** If episodes get compacted, do we lose outcome signals? May need to extract signals before compaction.
4. **Cross-frame rubric variants.** Should different frames have different rubrics? Research frame might weight Recall higher; conversation frame might weight Proactivity higher. Adds complexity but may improve signal.
5. **Tim's feedback as training signal.** Should we add an explicit "rate this response" mechanism, or stick with implicit signals only?

---

## Score History (Reference Data)

| Date | Overall | Recall | Tool Sel | Conf Cal | Proactivity | Notes |
|------|---------|--------|----------|----------|-------------|-------|
| Mar 10 (a) | 4.5 | 4 | 5 | 4 | 5 | Baseline — freewheeling, no skill fetch |
| Mar 10 (b) | 5.8 | 5 | 7 | 5 | 6 | Post-corrective actions |
| Mar 11 | 5.5 | — | — | — | — | Freewheel detection failure |
| Mar 20 (a) | 6.75 | 7 | 6 | 7 | 7 | First plateau signal |
| Mar 20 (b) | 6.75 | 7 | 6 | 7 | 7 | Confirmed plateau |
| Mar 21 | 6.5 | 7 | 5 | 7 | 7 | Tool Selection regressed — sequential calls |

**Pattern:** Recall, Confidence Cal, Proactivity converged at 7. Tool Selection oscillates 5-7. Overall stuck at 6.5-6.75 for 3 consecutive runs. This is the plateau Phase 3b aims to break.

---

## References

- Zhang, J. et al. (2026). "Hyperagents: From System 1 Agents to System 2 Hyperagents for Complex Task-Solving with LLMs." arXiv:2603.19461. *Specific relevance: emergent meta-skill evolution (§4.2), cross-domain transfer via imp@k metric (§5.3), metacognitive self-modification of evaluation criteria.*
- Minsky, M. (2006). *The Emotion Machine.* Ch. 7 (Critic-Selector Model). *Foundation for F024 architecture.*
- F024 Critic Agent spec v3 — `docs/features/F024-critic-agent.md`. *Parent spec; Phase 3a (routing history) and Phase 3b (rubric evolution) both extend the adaptive learning goal.*
- Self-improvement-loop skill (ID: 3497a6cc). *Current fixed-rubric implementation that Phase 3b evolves.*
