# F029 — Trajectory Learning: Strategy, Recovery & Optimization Tips

> **Status:** Draft v1
> **Priority:** P1
> **Depends on:** F002 (Heart Module), F009 (Async Subtasks), F011 (Skill Discovery), F012 (K-Line Learning)
> **Research:** Trajectory-Informed Memory (arXiv:2603.10600), EvoSkill (arXiv:2603.02766), CraniMem (arXiv:2603.15642)
> **Fills:** Gap in self-improvement loop — currently learns skills only from successes, not from failures or inefficiencies
> **Enhances:** EvoSkill skill discovery loop with evidence-based tip extraction

---

## Problem Statement

Nous learns procedures from two sources:
1. **Explicit:** User teaches skills via `learn_skill` (SKILL.md files)
2. **Automatic:** F012 K-Line Learning creates procedures from decision clusters, episode patterns, and error recovery

Both approaches share a blind spot: **they only learn from successes, and they don't classify _what kind_ of learning each procedure represents.**

**What's missing:**
- When a task fails, the failure trace is logged as an episode but no structured learning is extracted
- When a task succeeds but inefficiently (5 recall_deep calls when 1 would suffice, circular tool use), no optimization tip is generated
- When a task succeeds cleanly, the strategy pattern isn't captured in a reusable, trigger-indexed form
- Procedures don't carry provenance — which execution trace produced this knowledge?
- No negative examples — procedures say what TO do but not what NOT to do

**What Trajectory-Informed Memory proved:** Extracting typed tips (strategy/recovery/optimization) from execution traces with causal attribution produces a **+28.5pp improvement on complex tasks** (149% relative). The gain comes specifically from recovery tips on failure traces — the data source Nous currently ignores entirely.

**The EvoSkill gap:** EvoSkill discovers skills by analyzing failures and proposing new skills. But it proposes speculatively — "this skill might help" — rather than extracting what actually happened in the trace. TIM's causal attribution step produces evidence-grounded tips, not speculation.

---

## Solution: Post-Execution Tip Extraction Pipeline

### Tip Taxonomy

Every extracted tip is classified into exactly one of three types:

**Strategy Tips** — from clean successes
- Encode what worked: the approach, prerequisites checked, tools used in sequence
- Include ordered implementation steps
- Trigger: condition under which this strategy applies

**Recovery Tips** — from failure-then-recovery or outright failures
- Encode BOTH the failure pattern AND the correction (or what correction should have been)
- Include a `negative_example` showing the anti-pattern
- Trigger: condition that matches the failure context

**Optimization Tips** — from inefficient successes
- Encode the efficiency improvement discovered
- Include the inefficient approach as `negative_example`
- Trigger: condition where the suboptimal pattern might recur

### Tip Schema (extends SKILL.md)

```yaml
---
name: "verify-prerequisites-before-checkout"
type: tip                              # NEW: distinguishes from full skills
tip_category: strategy                 # strategy | recovery | optimization
trigger: "When task involves multi-step operations with prerequisites"
priority: high                         # critical | high | medium | low
source_episode_id: "abc-123"           # Provenance link
source_outcome: "clean_success"        # clean_success | recovery | inefficient | failure
app_context: null                      # Optional domain constraint
---

## Guidance

When performing multi-step operations, systematically verify all prerequisites
before initiating the main sequence.

## Steps

1. Identify all prerequisites for the target operation
2. Verify each prerequisite independently (don't assume)
3. If any prerequisite fails, resolve it before proceeding
4. Only initiate the main operation after all checks pass

## Anti-Pattern

Do NOT attempt the main operation first and handle prerequisites reactively.
This leads to partial state that's harder to recover from.
```

### Extraction Pipeline: 3 Stages

#### Stage 1: Trajectory Classification

On episode close, classify the execution trace:

```python
class TrajectoryClassifier:
    """Classify episode outcome for tip extraction routing."""
    
    OUTCOME_TYPES = {
        "clean_success":    "Task completed correctly with no errors or retries",
        "recovery":         "Task failed initially but agent self-corrected",
        "inefficient":      "Task completed but with unnecessary steps or retries",
        "complete_failure": "Task did not achieve its goal",
        "no_extraction":    "Simple Q&A, no actionable trajectory",
    }
    
    async def classify(self, episode: Episode) -> str:
        """LLM micro-call to classify trajectory outcome."""
        # Inputs: episode summary, decision outcomes, tool call sequence
        # Output: one of OUTCOME_TYPES
        ...
```

**Classification signals (no LLM needed for many cases):**
- Episode has decisions with `outcome = "failure"` → recovery or complete_failure
- Episode has >3 recall_deep calls with similar queries → inefficient
- Episode has tool errors followed by retries → recovery
- Episode completed with all decisions `outcome = "success"` → clean_success or inefficient
- Episode is conversational with no tool use → no_extraction

LLM micro-call only for ambiguous cases.

#### Stage 2: Causal Attribution

For recovery and failure traces, identify the **root cause** — not just the proximate error:

```python
class CausalAttributor:
    """Trace backwards through reasoning to find root cause."""
    
    async def attribute(self, episode: Episode, outcome: str) -> Attribution:
        """
        For failures: immediate cause → proximate cause → root cause
        For recoveries: what failed → how detected → what fixed it → why fix worked
        For inefficiencies: what was suboptimal → what's better → why
        """
        prompt = f"""
        Analyze this execution trace:
        {episode.detail[:3000]}
        
        Outcome: {outcome}
        
        Identify:
        1. The critical decision point (where did things go right/wrong?)
        2. Root cause (not the error message — the underlying decision)
        3. Prevention/improvement steps (concrete, actionable, tool-specific)
        4. Trigger condition (when should an agent apply this learning?)
        """
        ...
```

**Key insight from TIM:** The failure at step 15 is often caused by an assumption at step 3. Attribution must trace backwards through the reasoning chain, not just report the error.

#### Stage 3: Tip Generation

Generate typed tips from the attribution:

```python
class TipGenerator:
    """Generate structured tips from attributed trajectories."""
    
    async def generate(self, episode: Episode, outcome: str, 
                       attribution: Attribution) -> list[Tip]:
        tips = []
        
        if outcome == "clean_success":
            tips.append(self._strategy_tip(episode, attribution))
        elif outcome == "recovery":
            tips.extend([
                self._recovery_tip(episode, attribution),
                self._strategy_tip(episode, attribution),  # Also capture what worked
            ])
        elif outcome == "inefficient":
            tips.append(self._optimization_tip(episode, attribution))
        elif outcome == "complete_failure":
            tips.append(self._recovery_tip(episode, attribution))
        
        # Generate both domain-specific AND generic versions
        generic_tips = [self._generalize(tip) for tip in tips]
        return tips + generic_tips
```

**Dual generalization (from TIM):** Every tip gets two versions:
- Domain-specific: "When reading a Python file with read_file, check if the path exists first"
- Generic: "When accessing resources by path, verify the resource exists before processing"

### Storage & Consolidation

**Storage:** Tips stored as procedures in Heart (existing infrastructure) with extended metadata:
- `tip_category` in metadata JSON
- `source_episode_id` linked via episode_procedures junction table
- `negative_example` in procedure body
- `effectiveness` tracked via existing procedure activation tracking

**Consolidation (prevents memory bloat):**

Run after every 10 tips generated:

1. **Dedup:** Find tips with cosine similarity >0.85 on trigger + content → merge, keep highest-source-quality version
2. **Conflict resolution:** Tips with contradictory guidance → resolve using outcome metadata (tips from successful trajectories beat tips from failures)
3. **Synthesis:** Complementary tips (same trigger, different steps) → merge into comprehensive ordered procedure

```python
class TipConsolidator:
    """Prevent tip proliferation via dedup, conflict resolution, synthesis."""
    
    async def consolidate(self, tips: list[Procedure]) -> list[Procedure]:
        clusters = self._cluster_by_trigger(tips, threshold=0.85)
        consolidated = []
        for cluster in clusters:
            if len(cluster) == 1:
                consolidated.append(cluster[0])
            elif self._has_conflicts(cluster):
                consolidated.append(self._resolve_by_outcome(cluster))
            else:
                consolidated.append(self._synthesize(cluster))
        return consolidated
```

### Retrieval Integration

Tips surface through existing `recall_deep` via procedure matching. The `trigger` field is the retrieval unit — it's embedded and matched against the current task description.

**Injection format** (follows TIM's validated approach):

```
[TIP — Recovery — HIGH PRIORITY]
When file operations fail with "file not found", verify the path using
bash("ls -la /path/to/parent/") before retrying. Do NOT assume the file
exists just because it was there in a previous session.
Source: Episode "Debug deploy script" (2026-03-25), outcome: recovery
```

---

## Implementation Plan

### Phase 1: Trajectory Classifier (~3h)
- Add TrajectoryClassifier with heuristic shortcuts + LLM fallback
- Wire into episode close handler (after summary generation)
- Classify outcome type, store in episode metadata
- **Shadow mode:** Log classifications without generating tips
- Measure: outcome type distribution across real sessions

### Phase 2: Causal Attribution (~4h)
- Implement CausalAttributor for failure/recovery/inefficiency traces
- Extract: critical decision point, root cause, prevention steps, trigger condition
- Test on 10 recent episodes with known outcomes
- Validate attribution quality via manual review

### Phase 3: Tip Generation & Storage (~4h)
- Implement TipGenerator for all three tip types
- Add tip_category, source_episode_id, negative_example to procedure metadata
- Generate dual versions (domain-specific + generic)
- Wire into episode close pipeline: classify → attribute → generate → store
- Add to SKILL.md schema: `type: tip`, `tip_category`, `anti_pattern` section

### Phase 4: Consolidation (~3h)
- Implement TipConsolidator: dedup, conflict resolution, synthesis
- Trigger after every 10 new tips
- Add consolidation metrics (dedup count, merge count, conflict resolution count)
- Effectiveness tracking: does task success rate improve when tips are retrieved?

### Phase 5: Evaluation (~2h)
- Measure tip retrieval precision: when tips surface, are they relevant?
- Measure outcome improvement: do tasks that retrieve tips succeed more often?
- Compare: tasks with recovery tips available vs. same error without tip
- Track tip effectiveness scores over time → deprecate low-effectiveness tips

---

## Success Metrics

- **≥1 tip extracted per 3 task episodes** — not every conversation is extractable, but task episodes should yield learnings
- **Recovery tip retrieval → 50%+ reduction in repeated failures** — same error type should trigger tip next time
- **Tip consolidation ratio ≥ 3:1** — 3 raw tips consolidate to 1 refined tip (prevents bloat)
- **Tip effectiveness ≥ 70%** — measured by: task succeeded when tip was activated

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Over-extraction (too many tips from trivial episodes) | Classifier filters: no_extraction for simple Q&A, min complexity threshold |
| Low-quality attribution (LLM hallucinates root cause) | Attribution validated against actual tool call sequence; reject if attribution references steps not in trace |
| Tip bloat (unbounded growth) | Consolidation runs every 10 tips; effectiveness-based deprecation after 5 activations below 40% |
| Stale tips (tools or APIs change) | Source episode linked; tips older than 90 days with no successful activation auto-deprecated |
| Generic tips too vague to help | Dual generation: domain-specific tip is primary; generic is fallback. Track which version activates more. |

---

## Connection to Existing Work

- **F011 (Skill Discovery):** Tips are lightweight procedures. learn_skill creates full skills from explicit teaching; F029 creates tips from implicit observation. Same storage, different source.
- **F012 (K-Line Learning):** K-Line creates procedures from decision clusters and error recovery patterns. F029 adds causal attribution and tip typing. K-Line is pattern-matching; F029 is causal analysis. Complementary.
- **EvoSkill loop:** EvoSkill proposes skills speculatively from failure analysis. F029 grounds proposals in actual execution traces. EvoSkill can consume F029 tips as inputs — "these are the real patterns, now propose skills that address them."
- **F024 (Critic Agent):** Critic evaluates execution quality. F029 extracts learning from that evaluation. Critic produces the signals; F029 converts signals to procedures.
- **F027 (Supersession):** Tips that contradict each other are resolved via the same supersession mechanism. A newer recovery tip for the same error pattern supersedes an older one.

---

## Research Grounding

**Trajectory-Informed Memory (arXiv:2603.10600):**
- +28.5pp on complex tasks (149% relative improvement) via typed tip extraction
- Subtask-level extraction > task-level (73.8% vs 72.0% TGC)
- Cosine retrieval ≈ LLM-guided selection (73.8% vs 73.2%) — embedding similarity is sufficient
- Three-step consolidation (dedup → conflict resolution → synthesis) essential for memory quality
- Negative examples are first-class memory types — showing anti-patterns prevents the most common failures
- Provenance (source_trajectory_id) enables quality validation and trust

**EvoSkill (arXiv:2603.02766):**
- Skill-level optimization yields transferable, interpretable, composable capabilities
- Skill-merge (combine unique skills from independent runs) outperforms skill-selection
- Automated discovery loop: Executor → Proposer → Skill-Builder

**CraniMem (arXiv:2603.15642):**
- ReplayScore = BaseUtility × (1 + α × FreqBonus) — frequency-weighted consolidation
- Scheduled consolidation prevents unbounded memory growth
