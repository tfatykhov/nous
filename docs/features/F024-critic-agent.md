# F024 — Critic Agent: Speculative Parallel Cognitive Execution

> **Status:** Draft v2
> **Priority:** P1
> **Depends on:** F003 (Cognitive Layer), F009 (Async Subtasks), F015 (Subtask Hardening — specifically: error recovery [§3.1] and per-frame tool limits [§4.2])
> **Supersedes:** F013 (Frame Splitting — conceptually absorbed; no F013 code exists to remove)
> **Theoretical basis:** Minsky, *The Emotion Machine* Ch.7 (Critic-Selector Model), Ch.5 (6 Levels of Mental Activity); *Society of Mind* Ch.6 (B-Brains), Ch.18 (Parallel Bundles), Ch.24 (Frames)
> **Reviewers:** Emerson (A2A), Tim
> **Changelog:**
> - v2: Address Emerson review — transaction read-through, Critic content generation in merge, bash blocking, F015 specificity, recall_deep side effects, cost model revision, Critic model selection, F020/F022 interaction, skills auto-activation connection

---

## Problem Statement

Nous operates as a **single-threaded cognitive system** — one frame per turn, chosen at the start, locked for the duration. This creates three compounding problems:

**1. Frame selection is a one-shot bet.**
The orchestrator picks a single cognitive frame before processing begins. If the task requires research AND decision-making AND structured output, one frame must handle all three. The frame that's best for research may be worst for structured output. There is no recovery from a bad frame choice.

**2. No metacognitive monitoring.**
Once Nous is executing in a frame, nothing watches for dysfunction patterns — repeated failed searches, circular reasoning, wrong-frame symptoms, user frustration signals. The agent lacks what Minsky calls a "B-Brain": a system that observes the A-Brain's behavior patterns without needing to understand its domain.

**3. Complex tasks are artificially serialized.**
A task like "research how other agents handle memory, then build a comparison" runs serially: research first, then structure. These could run in parallel with different cognitive configurations, producing better results in less wall-clock time.

**What F013 got right, and what it missed:**
F013 (Frame Splitting) correctly identified parallel frame execution as valuable and designed the Split → Execute → Synthesize pattern. But F013 made the **agent itself** responsible for deciding when and how to split — adding cognitive load to the very system that's already overloaded. The missing piece is a **separate, lightweight intelligence** that handles decomposition, monitoring, and synthesis — the Critic Agent. Note: F013 was spec-only — no code was implemented, so this supersession is conceptual.

---

## Solution: The Critic Agent

A lightweight secondary agent (B-Brain) that sits between the user and the primary Nous agent(s). It performs three functions:

1. **Pre-turn classification** — Analyze the user message + conversation state → decide how to route
2. **Speculative parallel execution** — For complex tasks, spawn multiple Nous instances in different frames simultaneously
3. **Post-execution evaluation** — Evaluate outputs, pick the best, merge complementary results, or flag conflicts

### Content Generation Roles (v2 clarification)

The Critic Agent operates in two distinct roles with different content rules:

- **Orchestrator role** (Phases 0-1): The Critic classifies, routes, and selects. It **never generates user-facing content.** It is a cognitive traffic controller.
- **Synthesizer role** (Phase 2+): During merge operations, the Critic synthesizes a response from multiple instance outputs. In this role, it **does generate user-facing content** — but only by combining/editing existing instance outputs, never from scratch. This is a distinct operational mode with its own prompt and evaluation criteria.

This separation addresses the contradiction identified in v1 review: the "never generates content" constraint applies to the Orchestrator role, not the Synthesizer role.

### Minsky Alignment

**Emotion Machine Ch.7 — Critic-Selector Model:**
> "Each Critic is specialized to recognize a certain type of problematic mental condition. When a Critic detects such a condition, it activates a Selector that switches on a different Way to Think."

The Critic Agent implements this directly. Instead of a single Critic, we have a Critic that can recognize problem-types (frame classification) and activate multiple Ways to Think simultaneously (parallel frame execution).

**Society of Mind Ch.6 — B-Brains:**
> "You could imagine a B-brain connected to watch and supervise the activity of the A-brain. The B-brain could learn to recognize — and then try to correct — the mistakes the A-brain makes."

The Critic watches Nous execute and intervenes when it detects dysfunction — exactly the B-Brain pattern.

---

## Architecture

### Current Flow (Single-Threaded)

```
User Message → Frame Heuristic → Single Nous Instance → Response
```

### Proposed Flow (Critic-Orchestrated)

```
User Message
    │
    ▼
┌──────────────────────────────────────┐
│  CRITIC AGENT (lightweight LLM)      │
│                                      │
│  Inputs:                             │
│  • User message                      │
│  • Conversation history (last N)     │
│  • Available frames + descriptions   │
│  • Available skills (for activation) │
│  • Recent tool call patterns         │
│  • Current working memory summary    │
│                                      │
│  Outputs:                            │
│  • Routing decision (see below)      │
│  • Frame assignments                 │
│  • Skill activations (F011/F012)     │
│  • Complexity classification         │
│  • Diagnostic observations           │
└──────────┬───────────────────────────┘
           │
     ┌─────┴──────┐
     ▼            ▼
  SIMPLE       COMPLEX
  (single)     (parallel)
     │            │
     ▼            ├──────────┬──────────┐
  ┌──────┐    ┌──────┐  ┌──────┐  ┌──────┐
  │ Nous │    │ Nous │  │ Nous │  │ Nous │
  │ (1   │    │ (A)  │  │ (B)  │  │ (C)  │
  │frame)│    │task  │  │rsrch │  │QA    │
  └──┬───┘    └──┬───┘  └──┬───┘  └──┬───┘
     │           │         │         │
     │           └─────────┴─────────┘
     │                     │
     │                     ▼
     │           ┌──────────────────┐
     │           │  CRITIC AGENT    │
     │           │  (evaluation)    │
     │           │                  │
     │           │  • Pick best     │
     │           │  • Merge parts   │
     │           │  • Cherry-pick   │
     │           │    side effects  │
     │           └────────┬─────────┘
     │                    │
     ▼                    ▼
  Response             Response
```

### Routing Decisions

The Critic classifies each turn into one of four routing modes:

**Passthrough** — Greetings, simple questions, status checks. Skip Critic entirely (heuristic gate). Cost: 0 extra.

**Single-Advised** — Clear single-frame task. Critic picks optimal frame + relevant skills, single instance. Cost: +1 Critic LLM call.

**Parallel-Select** — Ambiguous or multi-faceted task. Spawn 2-3 instances, pick best. Cost: +1 Critic call + N×Sonnet.

**Parallel-Merge** — Task with complementary aspects. Spawn 2-3 instances, Critic synthesizes outputs. Cost: +2 Critic calls (classification + synthesis) + N×Sonnet.

### Complexity Classifier (Passthrough Gate)

Before the Critic LLM call, a fast heuristic decides if the Critic is needed at all:

```python
def needs_critic(message: str, conversation_state: ConversationState) -> bool:
    """Fast heuristic — no LLM call. Errs toward invoking Critic."""
    
    # Skip for very short messages (greetings, acknowledgments)
    if len(message.split()) < 5 and not message.endswith('?'):
        return False
    
    # Skip if user explicitly named a task type
    if conversation_state.explicit_frame_request:
        return False
    
    # Always invoke for multi-sentence messages
    if message.count('.') + message.count('?') + message.count('!') > 2:
        return True
    
    # Invoke if recent conversation shows stuck patterns
    if conversation_state.repeated_tool_calls > 2:
        return True
    
    # Invoke if message contains multiple action verbs
    action_signals = ['research', 'build', 'compare', 'analyze', 'decide', 
                      'write', 'find', 'create', 'review', 'check']
    if sum(1 for s in action_signals if s in message.lower()) >= 2:
        return True
    
    # Default: invoke Critic (safe side)
    return True
```

Estimated passthrough rate: ~30-40% of turns skip the Critic entirely.

---

## Critic as Skill Selector (F011/F012 Connection)

The Critic Agent is the natural integration point for skills auto-activation (F011 discovery, F012 activation). During pre-turn classification, the Critic sees:

- Available skills catalog (name, description, trigger patterns)
- User message and conversation context
- Selected frame(s)

The Critic can then include in its routing decision:
```json
{
  "frames": ["research", "task"],
  "skills": ["web-research-protocol", "spec-writing"],
  "rationale": "User wants research + structured output, activate both skills"
}
```

This means **skills activate based on Critic judgment, not pattern matching alone.** The Critic understands task intent, which is strictly better than regex triggers. This connection is designed in Phase 0 but becomes more powerful in parallel mode — different instances can have different skills loaded.

---

## Transactional Cognition (The Hard Problem)

### Why This Matters

When 3 parallel Nous instances execute simultaneously, each may:
- Learn facts (`learn_fact`)
- Record decisions (`record_decision`)
- Create censors (`create_censor`)
- Write files (`write_file`)
- Execute shell commands (`bash`)

If Instance B gets discarded because Instance A's response was better, Instance B's side effects must not persist. This requires **cognitive transactions** — buffered side effects that commit only on Critic approval.

### Tool Classification (v2 revised)

**Pure Read (no isolation needed):**
- `web_search`, `web_fetch`, `read_file`, `get_procedure`, `list_tasks`
- Execute normally against real state

**Read with Side Effects (isolation required) — v2 addition:**
- `recall_deep`, `recall_recent` — these update `access_count` and `updated_at` timestamps
- In parallel mode: execute the read against real state but **suppress the write-back** (skip access count updates)
- Access count accuracy is low-stakes; avoiding race conditions is high-stakes
- Alternative considered: per-instance access count journals → too complex for the value

**Memory Write (journaled):**
- `learn_fact`, `record_decision`, `create_censor`, `store_identity`
- Writes go to per-instance buffer. Committed only if instance is selected/merged.

**World Write — v2 revised:**
- `bash`: **Blocked entirely in parallel mode** for Phase 1. Static analysis of write vs read commands is unreliable (pipes, subshells, aliases defeat it). Revisit in Phase 2 with sandboxed execution if needed.
- `write_file`: Journaled to temp paths. Instance can read back its own writes via read-through (see below).

**Spawn:**
- `spawn_task`, `schedule_task`: **Blocked.** Parallel instances cannot spawn sub-tasks (no recursive explosion).

### Transaction Read-Through (v2 addition — P1 fix)

**Problem:** If Instance A calls `learn_fact("X is true")` then later `recall_deep("X")`, it won't find its own fact — because the fact is in the journal buffer, not Heart. This breaks chains of reasoning within a single instance.

**Solution:** The `TransactionInterceptor` implements a **read-through layer** that checks the transaction's journal buffer before (or in addition to) querying real storage:

```python
class TransactionInterceptor:
    """Wraps tool execution to buffer writes and provide read-through."""
    
    def __init__(self, transaction: CognitiveTransaction):
        self.txn = transaction
    
    async def intercept_recall_deep(self, query: str, **kwargs) -> dict:
        """Query real storage + overlay with transaction's pending facts."""
        # 1. Get real results (suppress access_count update in parallel mode)
        real_results = await real_recall_deep(query, skip_access_update=True, **kwargs)
        
        # 2. Search transaction's pending facts for matches
        local_results = self._search_pending_facts(query)
        
        # 3. Merge: local results first (they're "newer"), then real results
        # Deduplicate by semantic similarity to avoid showing the same fact twice
        merged = self._merge_results(local_results, real_results)
        return merged
    
    async def intercept_read_file(self, path: str) -> dict:
        """Check if file was written to temp by this transaction first."""
        # Check if this instance wrote to this path
        for pending_write in self.txn.pending_files:
            if pending_write.intended_path == path:
                return await real_read_file(pending_write.temp_path)
        # Otherwise read from real filesystem
        return await real_read_file(path)
    
    def _search_pending_facts(self, query: str) -> list:
        """Simple keyword/embedding search over pending facts in journal."""
        # Use same embedding model as recall_deep for consistency
        matches = []
        for fact in self.txn.pending_facts:
            score = compute_similarity(query, fact.content)
            if score > SIMILARITY_THRESHOLD:
                matches.append(fact.as_recall_result(score))
        return matches
    
    def _merge_results(self, local: list, real: list) -> list:
        """Merge local (journal) and real (Heart) results, deduplicating."""
        seen_content_hashes = set()
        merged = []
        for result in local + real:
            content_hash = hash(result.content[:100])
            if content_hash not in seen_content_hashes:
                merged.append(result)
                seen_content_hashes.add(content_hash)
        return merged[:kwargs.get('limit', 10)]
```

**Read-through applies to:**
- `recall_deep` / `recall_recent` — overlay pending facts
- `read_file` — check pending file writes first
- `get_procedure` — check pending procedure modifications (future)

**Read-through does NOT apply to:**
- `web_search` / `web_fetch` — external, no local state
- `list_tasks` — real-time system state, not user data

### Transaction Journal

Each parallel instance gets an isolated `CognitiveTransaction`:

```python
@dataclass
class CognitiveTransaction:
    instance_id: str
    frame_type: str
    status: str  # "running" | "completed" | "failed" | "committed" | "rolled_back"
    
    # Buffered side effects
    pending_facts: list[PendingFact]        # learn_fact calls
    pending_decisions: list[PendingDecision]  # record_decision calls
    pending_censors: list[PendingCensor]      # create_censor calls
    pending_files: list[PendingFileWrite]     # write_file calls
    
    # Output
    response_text: str
    tool_calls: list[ToolCallRecord]
    duration_ms: int
    
    def commit(self) -> CommitResult:
        """Apply all buffered side effects to real storage."""
        results = CommitResult()
        for fact in self.pending_facts:
            result = await real_learn_fact(**fact.kwargs)
            results.facts_committed.append(result)
        for decision in self.pending_decisions:
            result = await real_record_decision(**decision.kwargs)
            results.decisions_committed.append(result)
        for censor in self.pending_censors:
            result = await real_create_censor(**censor.kwargs)
            results.censors_committed.append(result)
        for file_write in self.pending_files:
            await real_write_file(file_write.intended_path, file_write.content)
            results.files_committed.append(file_write.intended_path)
        self.status = "committed"
        return results
    
    def rollback(self) -> None:
        """Discard all buffered side effects. Clean up temp files."""
        for file_write in self.pending_files:
            if os.path.exists(file_write.temp_path):
                os.remove(file_write.temp_path)
        self.pending_facts.clear()
        self.pending_decisions.clear()
        self.pending_censors.clear()
        self.pending_files.clear()
        self.status = "rolled_back"
    
    def cherry_pick(self, fact_ids: list[str] = None, 
                    decision_ids: list[str] = None,
                    file_paths: list[str] = None) -> CommitResult:
        """Commit only selected side effects (for merge scenarios)."""
        results = CommitResult()
        if fact_ids:
            for fact in self.pending_facts:
                if str(fact.id) in fact_ids:
                    result = await real_learn_fact(**fact.kwargs)
                    results.facts_committed.append(result)
        if decision_ids:
            for decision in self.pending_decisions:
                if str(decision.id) in decision_ids:
                    result = await real_record_decision(**decision.kwargs)
                    results.decisions_committed.append(result)
        if file_paths:
            for file_write in self.pending_files:
                if file_write.intended_path in file_paths:
                    await real_write_file(file_write.intended_path, file_write.content)
                    results.files_committed.append(file_write.intended_path)
        return results
```

---

## Critic Agent Prompt Design

### Pre-Turn Classification Prompt

```
You are the Critic Agent for Nous, a cognitive AI system. Your role is to
analyze the user's message and decide how Nous should process it.

AVAILABLE FRAMES:
- task: Focused execution, building, creating
- research: Information gathering, web search, paper analysis  
- conversation: Casual chat, relationship building
- decision: Evaluating alternatives, making choices
- debug: Troubleshooting, error analysis
- question: Answering factual questions
- creative: Writing, brainstorming, ideation

AVAILABLE SKILLS:
{skill_catalog_with_descriptions}

CONVERSATION STATE:
{recent_messages}
{working_memory_summary}
{recent_tool_patterns}

USER MESSAGE:
{user_message}

DECIDE:
1. complexity: "simple" | "moderate" | "complex"
2. routing: "single" | "parallel-select" | "parallel-merge"
3. frames: list of frame assignments (1 for single, 2-3 for parallel)
4. skills: list of skills to activate per frame instance
5. rationale: brief explanation of why this decomposition
6. per_frame_instructions: specific focus for each frame instance

Respond in JSON.
```

### Post-Execution Evaluation Prompt (Orchestrator Role)

```
You are evaluating parallel outputs from Nous cognitive instances.
Your role is to SELECT the best output or recommend a merge. 
You do NOT rewrite or generate content yourself.

ORIGINAL USER REQUEST:
{user_message}

INSTANCE OUTPUTS:
{instance_a_output}
---
{instance_b_output}
---
{instance_c_output}  (if applicable)

INSTANCE SIDE EFFECTS:
{instance_a_journal_summary}
{instance_b_journal_summary}

EVALUATE:
1. Which instance(s) best address the user's request?
2. Are there complementary strengths to merge?
3. Are any side effects (facts, decisions) worth keeping from non-selected instances?
4. What is your recommended action?

Respond in JSON with:
{
  "action": "select_a" | "select_b" | "select_c" | "merge",
  "response_source": "a" | "b" | "c",
  "commit_facts_from": ["a", "b"],
  "commit_decisions_from": ["a"],
  "rationale": "..."
}
```

### Post-Execution Synthesis Prompt (Synthesizer Role — Phase 2+)

```
You are synthesizing a response from multiple Nous cognitive instances.
You may ONLY use content from the instance outputs below — do not add
new claims, facts, or information that doesn't appear in at least one output.

ORIGINAL USER REQUEST:
{user_message}

INSTANCE A ({instance_a_frame}):
{instance_a_output}

INSTANCE B ({instance_b_frame}):
{instance_b_output}

MERGE INSTRUCTIONS:
Combine the strongest elements from each instance into a single coherent
response. Preserve the voice and style of the primary instance. Resolve
any contradictions by preferring the instance with stronger evidence.

Produce the merged response directly (this will be shown to the user).
```

---

## Diagnostic Critics (Post-Turn Monitoring)

Beyond frame selection, the Critic monitors for **dysfunction patterns** during multi-turn conversations. These are Minsky's "Critics that recognize types of stuck":

### Built-in Diagnostic Patterns

**Repetition** — 3+ similar `recall_deep` queries in a conversation → Inject: "You've searched for similar things multiple times. Reformulate the problem or try a different approach."

**Frame mismatch** — Task-frame behaviors in conversation context (or vice versa) → Suggest frame switch.

**Stuck loop** — Same tool called 3+ times with similar args → Inject: "Consider a completely different strategy."

**Scope creep** — Response length growing, tangential topics appearing → Inject: "Focus. What was the user's core ask?"

**Confidence drift** — Multiple low-confidence decisions in sequence (requires Brain access to check recent decision confidence scores) → Inject: "Pause. What are you uncertain about? Ask the user."

**User frustration** — Short responses after long agent outputs, repeated questions, "no I meant..." → Inject: "The user may be frustrated. Acknowledge, clarify, re-align."

These diagnostics are injected into the context as system-level nudges before the next turn, not as user-visible messages.

### Diagnostic Implementation

```python
@dataclass
class DiagnosticCritic:
    name: str
    detect: Callable[[ConversationState], bool]
    intervention: str  # Text injected into agent context
    cooldown_turns: int = 3  # Don't fire again for N turns after triggering

class CriticDiagnostics:
    critics: list[DiagnosticCritic]
    
    def evaluate(self, state: ConversationState) -> list[str]:
        """Return list of intervention messages to inject."""
        interventions = []
        for critic in self.critics:
            if critic.detect(state) and not critic.on_cooldown():
                interventions.append(f"[Critic/{critic.name}]: {critic.intervention}")
                critic.mark_fired()
        return interventions
```

---

## Interaction with F020/F022 (v2 addition)

### F020 (ReversibleCache)
Parallel instances share the same `ReversibleCache` for web fetches. This is safe because:
- Web content is external and immutable (same URL returns same content)
- Cache is read-only from the instance's perspective
- No isolation needed — cache hits save redundant fetches across instances

### F022 (Graph-Augmented Recall)
When F022 is active, `recall_deep` uses graph traversal for richer results. In parallel mode:
- Graph reads are safe (no write side effects beyond access counts, already suppressed)
- Each instance benefits from the full graph — no degradation
- If F022 adds write operations (e.g., strengthening edges on access), those must be suppressed in parallel mode, same as access count updates

---

## Phased Implementation

### Phase 0: Critic as Smart Frame Selector (no parallelism)
**Goal:** Validate that a Critic Agent picks better frames than the current heuristic.
**Effort:** ~6-8 hours
**Risk:** Low — additive, no changes to existing execution path
**F015 dependency:** None (no parallel execution)

**What changes:**
- Add `CriticAgent` class with pre-turn classification
- Complexity gate (heuristic passthrough for simple messages)
- Critic makes a single LLM call → returns recommended frame + skills to activate
- **Shadow mode first**: Log Critic's recommendation alongside current heuristic choice
- After validation: Critic recommendation replaces heuristic
- Add diagnostic critics (post-turn monitoring) — injected as context nudges
- Skills catalog passed to Critic for auto-activation recommendations (F011/F012 connection)

**Critic model selection (v2 revision):**
Phase 0 starts with **Sonnet** as the Critic model, not Haiku. Rationale:
- A wrong frame selection wastes an entire Sonnet execution (~$0.02)
- A Sonnet Critic call costs ~$0.01 — cheaper than the waste from a bad Haiku classification
- If Sonnet proves over-powered for classification, we can downgrade to Haiku with empirical data
- Shadow mode lets us A/B test: run both Haiku and Sonnet classifications, compare accuracy

**What doesn't change:**
- Single Nous instance per turn
- All existing tool behavior
- No transaction infrastructure needed

**Success criteria:**
- Critic disagrees with heuristic on >15% of turns (it's finding improvements)
- On disagreements, Critic's choice is judged better by human review >60% of the time
- Latency overhead < 800ms per turn (Sonnet call, revised from 500ms Haiku estimate)
- Diagnostic critics fire appropriately — true positives > 70%

**Measurement:**
- Log both Critic and heuristic frame choices for every turn
- Weekly review of disagreement cases
- Track diagnostic firing rate and appropriateness
- If running dual-model: compare Haiku vs Sonnet classification accuracy

### Phase 1: Parallel Spawn + Pick Winner
**Goal:** Enable speculative parallel execution with winner selection.
**Effort:** ~15-20 hours
**Depends on:** Phase 0 validated, F015 §3.1 (error recovery) and §4.2 (per-frame tool limits) complete
**Risk:** Medium — requires transaction infrastructure

**What changes:**
- `CognitiveTransaction` class — journaled side effects with read-through
- `TransactionInterceptor` — wraps tool execution in parallel mode, including read-through layer for pending facts/files
- Critic can route to "parallel-select" — spawn 2-3 instances
- Each instance runs in isolated transaction
- `recall_deep`/`recall_recent` access count updates suppressed in parallel mode
- **`bash` blocked entirely** in parallel instances (v2: static write detection is unreliable)
- Critic evaluates outputs, selects winner
- Winner's journal committed, losers rolled back
- Spawn/schedule tools blocked in parallel instances

**What doesn't change:**
- No merge capability yet (pick one winner only)
- No cherry-picking side effects across instances
- No bash access in parallel (revisit Phase 2)

**Success criteria:**
- Transaction isolation verified — no leaked side effects from discarded instances
- Read-through works — instances can recall their own pending facts
- Parallel route produces better responses than single-frame on >50% of complex tasks (human judged)
- Total latency for parallel turns < 2× single turn (parallelism saves time vs serial)
- Cost per parallel turn < 3× single turn

**Key technical risks:**
- Transaction interceptor must be invisible to instances (they shouldn't know they're buffered)
- Concurrent recall_deep calls may hit rate limits or DB contention
- Subtask worker pool sizing — do we have enough workers for 3 parallel + background tasks?
- Read-through embedding search adds latency — keep pending fact count small

### Phase 2: Merge + Cherry-Pick
**Goal:** Enable the Critic to synthesize complementary outputs and cherry-pick side effects.
**Effort:** ~10-15 hours
**Depends on:** Phase 1 stable in production
**Risk:** Medium-High — merge quality is hard to evaluate

**What changes:**
- Critic operates in **Synthesizer role** for merge (separate prompt, generates user-facing content by combining instance outputs)
- Post-execution Critic prompt includes merge instructions
- Critic uses Sonnet-class for synthesis (required for quality)
- `cherry_pick()` on transactions — commit selected facts/decisions from any instance
- Merge quality tracking — was the merged response better than best individual?
- `bash` in parallel: evaluate sandboxed execution (container/namespace isolation) if needed
- `write_file` read-through: bidirectional (write to temp, read from temp)

**What doesn't change:**
- Phase 0 and Phase 1 still operate for simple and select-mode tasks
- No recursive spawning
- No adaptive learning yet

**Success criteria:**
- Merged responses rated better than best individual >40% of the time
- Cherry-picked side effects are appropriate (no garbage facts committed from discarded instances)
- Merge adds < 3 seconds latency over select mode

**Key technical risks:**
- Merge is essentially "write a response using the best parts of these N drafts" — this is a creative task that may not be reliable
- Cost: merge requires Sonnet-class Critic call → significantly more expensive
- Cherry-picking side effects requires Critic to understand what facts/decisions mean

### Phase 3: Adaptive Critic (Learning)
**Goal:** Critic learns which frame combinations work for which problem types.
**Effort:** ~10-12 hours
**Depends on:** Phase 2 stable, sufficient data from Phases 0-2
**Risk:** Medium — requires credit assignment (hard problem)

**What changes:**
- Critic maintains a **routing history** — problem type → frame combination → outcome
- After each parallel execution, record: which routing was used, which instance won/merged, user satisfaction signal
- Critic prompt includes relevant routing history: "For similar problems, research+task worked 70% of the time"
- Frame combination priors — empirical data replaces guessing
- Credit assignment: track which Way to Think (frame) led to the successful parts

**What doesn't change:**
- Core architecture from Phases 0-2 unchanged
- Critic remains a single agent (no meta-Critic)

**Success criteria:**
- Critic routing accuracy improves over time (measurable learning curve)
- Reduction in parallel spawns as Critic learns which single frame works for common patterns
- Cost decreases as Critic gets more confident (fewer unnecessary parallel runs)

---

## Hard Constraints (Non-Negotiable)

1. **No recursive Critics.** One Critic layer. If the Critic is bad, tune it — don't add a meta-Critic. (Minsky's C-Brain trap)
2. **Max 3 parallel instances.** No unbounded spawning. Configurable but hard-capped.
3. **Parallel instances cannot spawn subtasks.** No recursive explosion.
4. **Passthrough for simple messages.** The Critic must not add latency to "hey what's up."
5. **Transaction rollback must be clean.** No orphaned facts, no ghost decisions, no temp file leaks.
6. **Critic Orchestrator role never generates user-facing content.** Critic Synthesizer role (Phase 2+) may combine instance outputs only.
7. **Parallel instances are mutually isolated.** No cross-instance communication during execution.
8. **All phases must be independently useful.** Phase 0 without Phase 1 is still valuable. Phase 1 without Phase 2 is still valuable. No phase depends on a future phase for its value.
9. **`bash` blocked in parallel mode until sandboxed execution is available.** (v2 addition)

---

## Cost Model (v2 revised)

### Per-Turn Cost Estimates

**Passthrough** — Critic: $0 / Instance: 1× Sonnet / Total: ~$0.02 / vs Current: Same

**Single-Advised** — Critic: 1× Sonnet (~$0.01) / Instance: 1× Sonnet / Total: ~$0.03 / vs Current: +50%

**Parallel-Select** — Critic: 2× Sonnet (~$0.02) / Instance: 2-3× Sonnet / Total: ~$0.06-0.08 / vs Current: +200-300%

**Parallel-Merge** — Critic: 2× Sonnet (~$0.02) / Instance: 2-3× Sonnet / Total: ~$0.08-0.10 / vs Current: +300-400%

### Blended Cost Estimate (v2 revised)

**v1 assumption** (casual/mixed usage): 35% passthrough, 40% single, 20% parallel-select, 5% merge → ~40% increase.

**v2 realistic assumption** (Tim's usage is task-heavy): 20% passthrough, 35% single, 35% parallel-select, 10% merge → **~80% increase**.

This needs validation with real data. Phase 0 shadow mode will give us actual complexity distribution before we commit to parallel costs.

**Cost gate:** If blended cost exceeds 100% increase, tighten the complexity classifier to route more to single-advised.

### Cost Controls

- `NOUS_CRITIC_ENABLED` — kill switch (env var)
- `NOUS_CRITIC_MODE` — "shadow" | "advised" | "parallel" (progressive enablement)
- `NOUS_CRITIC_MAX_PARALLEL` — cap parallel instances (default 3)
- `NOUS_CRITIC_MODEL` — model for Critic calls (default sonnet, can downgrade to haiku)
- `NOUS_CRITIC_MERGE_MODEL` — model for merge/synthesis operations (default sonnet)
- Per-conversation cost tracking — alert if conversation exceeds threshold

---

## Relationship to Other Features

**F003** (Cognitive Layer) — Critic uses frame definitions and frame-specific instructions.

**F009** (Async Subtasks) — Parallel instances use subtask worker pool.

**F011/F012** (Skill Discovery/Activation) — Critic selects skills to activate per frame instance. This is the natural home for skills auto-activation — Critic understands task intent better than regex triggers.

**F013** (Frame Splitting) — **Superseded conceptually.** F024 absorbs F013's parallel execution design with Critic orchestration. No F013 code exists to remove.

**F014** (Reasoning Scaffolds) — Each parallel instance gets frame-appropriate scaffolds.

**F015** (Subtask Hardening) — **Prerequisite for Phase 1 only.** Specifically: §3.1 error recovery and §4.2 per-frame tool limits. Other F015 sections are nice-to-have, not blocking.

**F020** (ReversibleCache) — Shared across parallel instances (safe, read-only cache).

**F022** (Graph-Augmented Recall) — Safe for parallel reads. Write-back operations (edge strengthening) suppressed in parallel mode.

**F023** (Admission Control) — Transaction journals must respect admission control on commit. Facts from winning transaction go through A-MAC scoring before persisting.

---

## Open Questions

1. ~~**Critic model selection.**~~ (v2: resolved — start with Sonnet, downgrade if data supports it)
2. **Conversation-level vs turn-level.** Should the Critic maintain state across a conversation (learning mid-conversation that parallel isn't needed) or reset each turn?
3. **User visibility.** Should the user see when parallel execution is happening? ("🧠 Thinking in 3 frames...") or is it invisible?
4. **Partial results.** If one parallel instance finishes fast and another is slow, should Critic be able to go with the fast one + timeout penalty?
5. **Telegram latency.** Tim expects quick responses on Telegram. Should parallel mode be disabled for Telegram and enabled only for CLI/longer-form interactions?
6. **Diagnostic critic tuning.** How do we evaluate if diagnostic interventions actually help? Need a feedback signal.
7. ~~**F015 readiness.**~~ (v2: resolved — specified §3.1 and §4.2 as concrete prerequisites)
8. **Embedding model for read-through.** (v2 new) Should pending fact search in read-through use the same embedding model as recall_deep, or a lighter one for speed?

---

## Risks

**Transaction leaks** — Side effects escape from discarded instances. Severity: High. Mitigation: Comprehensive integration tests, shadow mode first.

**Critic adds latency** — Every turn gets slower. Severity: Medium. Mitigation: Passthrough gate, async Critic call, Telegram-specific tuning.

**Merge produces Frankenstein responses** — Severity: Medium. Mitigation: Phase 2 is separate — validate select-mode first. Synthesizer prompt constrained to existing content only.

**Cost spiral on complex conversations** — Severity: Medium. Mitigation: Per-conversation cost tracking + alerts + cost gate.

**Over-parallelization** — Critic spawns parallel when single would suffice. Severity: Medium. Mitigation: Adaptive learning in Phase 3, conservative defaults.

**Subtask infra instability** — Severity: High. Mitigation: F015 §3.1/§4.2 are prerequisites, not nice-to-have.

**Context window pressure** — Critic evaluation prompt + multiple outputs. Severity: Medium. Mitigation: Summarize instance outputs before Critic evaluation.

**Read-through latency** — Embedding search over pending facts adds per-query overhead. Severity: Low. Mitigation: Pending fact count is small (typically <10 per instance).

---

## Implementation Notes

### Files to Create/Modify (Phase 0)

- `nous/cognitive/critic.py` — New. CriticAgent class, complexity gate, diagnostic critics, skill selection
- `nous/cognitive/layer.py` — Modify. Insert Critic pre-turn hook, inject diagnostic nudges
- `nous/config.py` — Modify. Add Critic configuration (env vars, defaults)

### Files to Create/Modify (Phase 1)

- `nous/cognitive/transaction.py` — New. CognitiveTransaction, TransactionInterceptor, read-through layer
- `nous/cognitive/critic.py` — Modify. Add parallel routing, post-execution evaluation
- `nous/tools/interceptor.py` — New. Tool-level write interception + read-through for parallel mode
- `nous/subtasks/worker.py` — Modify. Support transactional execution mode

### Files to Create/Modify (Phase 2)

- `nous/cognitive/critic.py` — Modify. Add Synthesizer role, merge logic, cherry-pick evaluation
- `nous/cognitive/transaction.py` — Modify. Add cherry_pick() method, bidirectional file read-through

### Files to Create/Modify (Phase 3)

- `nous/cognitive/routing_history.py` — New. Routing decision log + pattern learning
- `nous/cognitive/critic.py` — Modify. Inject routing history into Critic prompt
