# F024 — Critic Agent: Speculative Parallel Cognitive Execution

> **Status:** Draft v1
> **Priority:** P1
> **Depends on:** F003 (Cognitive Layer), F009 (Async Subtasks), F015 (Subtask Hardening)
> **Supersedes:** F013 (Frame Splitting — absorbed and extended)
> **Theoretical basis:** Minsky, *The Emotion Machine* Ch.7 (Critic-Selector Model), Ch.5 (6 Levels of Mental Activity); *Society of Mind* Ch.6 (B-Brains), Ch.18 (Parallel Bundles), Ch.24 (Frames)

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
F013 (Frame Splitting) correctly identified parallel frame execution as valuable and designed the Split → Execute → Synthesize pattern. But F013 made the **agent itself** responsible for deciding when and how to split — adding cognitive load to the very system that's already overloaded. The missing piece is a **separate, lightweight intelligence** that handles decomposition, monitoring, and synthesis — the Critic Agent.

---

## Solution: The Critic Agent

A lightweight secondary agent (B-Brain) that sits between the user and the primary Nous agent(s). It performs three functions:

1. **Pre-turn classification** — Analyze the user message + conversation state → decide how to route
2. **Speculative parallel execution** — For complex tasks, spawn multiple Nous instances in different frames simultaneously
3. **Post-execution evaluation** — Evaluate outputs, pick the best, merge complementary results, or flag conflicts

The Critic never generates user-facing content directly. It is a **cognitive traffic controller** that makes Nous's existing capabilities more effective through better orchestration.

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
│  • Recent tool call patterns         │
│  • Current working memory summary    │
│                                      │
│  Outputs:                            │
│  • Routing decision (see below)      │
│  • Frame assignments                 │
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

| Mode | When | Action | Cost |
|---|---|---|---|
| **Passthrough** | Greetings, simple questions, status checks | Skip Critic entirely (heuristic gate) | 0 extra |
| **Single-Advised** | Clear single-frame task | Critic picks optimal frame, single instance | +1 Haiku call |
| **Parallel-Select** | Ambiguous or multi-faceted task | Spawn 2-3 instances, pick best | +1 Haiku + N×Sonnet |
| **Parallel-Merge** | Task with complementary aspects | Spawn 2-3 instances, merge outputs | +2 Haiku + N×Sonnet |

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

## Transactional Cognition (The Hard Problem)

### Why This Matters

When 3 parallel Nous instances execute simultaneously, each may:
- Learn facts (`learn_fact`)
- Record decisions (`record_decision`)
- Create censors (`create_censor`)
- Write files (`write_file`)
- Execute shell commands (`bash`)

If Instance B gets discarded because Instance A's response was better, Instance B's side effects must not persist. This requires **cognitive transactions** — buffered side effects that commit only on Critic approval.

### Tool Classification

| Category | Tools | Parallel Behavior |
|---|---|---|
| **Pure Read** | `recall_deep`, `recall_recent`, `web_search`, `web_fetch`, `read_file`, `get_procedure` | Execute normally. No isolation needed. |
| **Memory Write** | `learn_fact`, `record_decision`, `create_censor`, `store_identity` | **Journaled.** Writes go to per-instance buffer. Committed only if instance is selected/merged. |
| **World Write** | `write_file`, `bash` (write ops) | **Restricted.** In parallel mode, write ops are either blocked or journaled to temp paths. |
| **Spawn** | `spawn_task`, `schedule_task` | **Blocked.** Parallel instances cannot spawn sub-tasks (no recursive explosion). |

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
        ...
    
    def rollback(self) -> None:
        """Discard all buffered side effects. Clean up temp files."""
        ...
    
    def cherry_pick(self, fact_ids: list[str] = None, 
                    decision_ids: list[str] = None) -> CommitResult:
        """Commit only selected side effects (for merge scenarios)."""
        ...
```

### Transaction Interceptor

Tool calls within parallel instances are intercepted by a `TransactionInterceptor`:

```python
class TransactionInterceptor:
    """Wraps tool execution to buffer writes during parallel cognition."""
    
    def __init__(self, transaction: CognitiveTransaction):
        self.txn = transaction
    
    async def intercept_learn_fact(self, **kwargs) -> dict:
        """Buffer fact instead of writing to Heart."""
        pending = PendingFact(id=uuid4(), kwargs=kwargs)
        self.txn.pending_facts.append(pending)
        # Return fake success so the instance doesn't know it's buffered
        return {"status": "stored", "fact_id": str(pending.id)}
    
    async def intercept_write_file(self, path: str, content: str) -> dict:
        """Write to temp location, record mapping."""
        temp_path = f"/tmp/nous_txn/{self.txn.instance_id}/{path}"
        # Actually write to temp so instance can read it back
        await real_write_file(temp_path, content)
        self.txn.pending_files.append(PendingFileWrite(
            intended_path=path, temp_path=temp_path, content=content
        ))
        return {"status": "written", "path": path}  # Lie about path
    
    async def intercept_bash(self, command: str) -> dict:
        """Allow read-only commands. Block or sandbox writes."""
        if is_write_command(command):
            return {"status": "blocked", 
                    "reason": "Write commands blocked in parallel mode"}
        return await real_bash(command)
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
4. rationale: brief explanation of why this decomposition
5. per_frame_instructions: specific focus for each frame instance

Respond in JSON.
```

### Post-Execution Evaluation Prompt

```
You are evaluating parallel outputs from Nous cognitive instances.

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
   - "select_a" | "select_b" | "select_c"
   - "merge" (specify which parts from which instance)
   - "cherry_pick" (specify response source + side effects from others)

Respond in JSON with:
{
  "action": "select_a" | "merge" | "cherry_pick",
  "response_source": "a" | "b" | "merged",
  "merge_instructions": "...",  // if merge
  "commit_facts_from": ["a", "b"],  // which journals to commit
  "commit_decisions_from": ["a"],
  "rationale": "..."
}
```

---

## Diagnostic Critics (Post-Turn Monitoring)

Beyond frame selection, the Critic monitors for **dysfunction patterns** during multi-turn conversations. These are Minsky's "Critics that recognize types of stuck":

### Built-in Diagnostic Patterns

| Pattern | Detection | Intervention |
|---|---|---|
| **Repetition** | 3+ similar `recall_deep` queries in a conversation | Inject: "You've searched for similar things multiple times. Reformulate the problem or try a different approach." |
| **Frame mismatch** | Task-frame behaviors in conversation context (or vice versa) | Suggest frame switch |
| **Stuck loop** | Same tool called 3+ times with similar args | Inject: "Consider a completely different strategy." |
| **Scope creep** | Response length growing, tangential topics appearing | Inject: "Focus. What was the user's core ask?" |
| **Confidence drift** | Multiple low-confidence decisions in sequence | Inject: "Pause. What are you uncertain about? Ask the user." |
| **User frustration** | Short responses after long agent outputs, repeated questions, "no I meant..." | Inject: "The user may be frustrated. Acknowledge, clarify, re-align." |

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

## Phased Implementation

### Phase 0: Critic as Smart Frame Selector (no parallelism)
**Goal:** Validate that a Critic Agent picks better frames than the current heuristic.
**Effort:** ~6-8 hours
**Risk:** Low — additive, no changes to existing execution path

**What changes:**
- Add `CriticAgent` class with pre-turn classification
- Complexity gate (heuristic passthrough for simple messages)
- Critic makes a single Haiku LLM call → returns recommended frame
- **Shadow mode first**: Log Critic's recommendation alongside current heuristic choice
- After validation: Critic recommendation replaces heuristic
- Add diagnostic critics (post-turn monitoring) — injected as context nudges

**What doesn't change:**
- Single Nous instance per turn
- All existing tool behavior
- No transaction infrastructure needed

**Success criteria:**
- Critic disagrees with heuristic on >15% of turns (it's finding improvements)
- On disagreements, Critic's choice is judged better by human review >60% of the time
- Latency overhead < 500ms per turn (Haiku call)
- Diagnostic critics fire appropriately — true positives > 70%

**Measurement:**
- Log both Critic and heuristic frame choices for every turn
- Weekly review of disagreement cases
- Track diagnostic firing rate and appropriateness

### Phase 1: Parallel Spawn + Pick Winner
**Goal:** Enable speculative parallel execution with winner selection.
**Effort:** ~15-20 hours
**Depends on:** Phase 0 validated, F015 subtask hardening complete
**Risk:** Medium — requires transaction infrastructure

**What changes:**
- `CognitiveTransaction` class — journaled side effects
- `TransactionInterceptor` — wraps tool execution in parallel mode
- Critic can route to "parallel-select" — spawn 2-3 instances
- Each instance runs in isolated transaction
- Critic evaluates outputs, selects winner
- Winner's journal committed, losers rolled back
- Spawn/schedule tools blocked in parallel instances

**What doesn't change:**
- No merge capability yet (pick one winner only)
- No cherry-picking side effects across instances
- Critic model stays Haiku-class

**Success criteria:**
- Transaction isolation verified — no leaked side effects from discarded instances
- Parallel route produces better responses than single-frame on >50% of complex tasks (human judged)
- Total latency for parallel turns < 2× single turn (parallelism saves time vs serial)
- Cost per parallel turn < 3× single turn

**Key technical risks:**
- Transaction interceptor must be invisible to instances (they shouldn't know they're buffered)
- Concurrent recall_deep calls may hit rate limits or DB contention
- Subtask worker pool sizing — do we have enough workers for 3 parallel + background tasks?

### Phase 2: Merge + Cherry-Pick
**Goal:** Enable the Critic to synthesize complementary outputs and cherry-pick side effects.
**Effort:** ~10-15 hours
**Depends on:** Phase 1 stable in production
**Risk:** Medium-High — merge quality is hard to evaluate

**What changes:**
- Critic can route to "parallel-merge"
- Post-execution Critic prompt includes merge instructions
- Critic (upgraded to Sonnet-class for merge) synthesizes response from multiple instances
- `cherry_pick()` on transactions — commit selected facts/decisions from any instance
- Merge quality tracking — was the merged response better than best individual?

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
6. **Critic never generates user-facing content.** It orchestrates, it doesn't speak.
7. **Parallel instances are mutually isolated.** No cross-instance communication during execution.
8. **All phases must be independently useful.** Phase 0 without Phase 1 is still valuable. Phase 1 without Phase 2 is still valuable. No phase depends on a future phase for its value.

---

## Cost Model

### Per-Turn Cost Estimates

| Routing Mode | Critic Cost | Instance Cost | Total | vs. Current |
|---|---|---|---|---|
| **Passthrough** | $0 | 1× Sonnet | ~$0.02 | Same |
| **Single-Advised** | 1× Haiku (~$0.001) | 1× Sonnet | ~$0.021 | +5% |
| **Parallel-Select** | 2× Haiku (~$0.002) | 2-3× Sonnet | ~$0.05-0.07 | +150-250% |
| **Parallel-Merge** | 1× Haiku + 1× Sonnet (~$0.025) | 2-3× Sonnet | ~$0.07-0.10 | +250-400% |

### Blended Cost Estimate

Assuming traffic distribution: 35% passthrough, 40% single-advised, 20% parallel-select, 5% parallel-merge:

- Current blended cost: ~$0.02/turn
- Projected blended cost: ~$0.028/turn
- **Increase: ~40%**

This is acceptable if quality improves meaningfully on complex tasks.

### Cost Controls

- `NOUS_CRITIC_ENABLED` — kill switch (env var)
- `NOUS_CRITIC_MODE` — "shadow" | "advised" | "parallel" (progressive enablement)
- `NOUS_CRITIC_MAX_PARALLEL` — cap parallel instances (default 3)
- `NOUS_CRITIC_MODEL` — model for Critic calls (default haiku)
- `NOUS_CRITIC_MERGE_MODEL` — model for merge operations (default sonnet)
- Per-conversation cost tracking — alert if conversation exceeds threshold

---

## Relationship to Other Features

| Feature | Relationship |
|---|---|
| **F003** (Cognitive Layer) | Critic uses frame definitions and frame-specific instructions |
| **F009** (Async Subtasks) | Parallel instances use subtask worker pool |
| **F013** (Frame Splitting) | **Superseded.** F024 absorbs F013's parallel execution with Critic orchestration layer |
| **F014** (Reasoning Scaffolds) | Each parallel instance gets frame-appropriate scaffolds |
| **F015** (Subtask Hardening) | **Prerequisite.** Parallel reliability depends on stable subtask infra |
| **F022** (Graph-Augmented Recall) | Parallel instances benefit from richer recall |
| **F023** (Admission Control) | Transaction journals must respect admission control on commit |

---

## Open Questions

1. **Critic model selection.** Haiku is cheap but may not be smart enough for good frame classification. Should we benchmark Haiku vs Sonnet as Critic? Cost difference is ~10×.
2. **Conversation-level vs turn-level.** Should the Critic maintain state across a conversation (learning mid-conversation that parallel isn't needed) or reset each turn?
3. **User visibility.** Should the user see when parallel execution is happening? ("🧠 Thinking in 3 frames...") or is it invisible?
4. **Partial results.** If one parallel instance finishes fast and another is slow, should Critic be able to go with the fast one + timeout penalty?
5. **Telegram latency.** Tim expects quick responses on Telegram. Should parallel mode be disabled for Telegram and enabled only for CLI/longer-form interactions?
6. **Diagnostic critic tuning.** How do we evaluate if diagnostic interventions actually help? Need a feedback signal.
7. **F015 readiness.** Current subtask infra has known bugs (Haiku failures, side effect leakage). How much of F015 must be complete before Phase 1 is safe?

---

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Transaction leaks — side effects escape from discarded instances | High | Comprehensive integration tests, shadow mode first |
| Critic adds latency to every turn | Medium | Passthrough gate, fast Haiku model, async Critic call |
| Merge produces Frankenstein responses | Medium | Phase 2 is separate — validate select-mode first |
| Cost spiral on complex conversations | Medium | Per-conversation cost tracking + alerts |
| Over-parallelization — Critic spawns parallel when single would suffice | Medium | Adaptive learning in Phase 3, conservative defaults |
| Subtask infra instability | High | F015 is prerequisite, not nice-to-have |
| Context window pressure — Critic evaluation prompt + multiple outputs | Medium | Summarize instance outputs before Critic evaluation |

---

## Implementation Notes

### Files to Create/Modify (Phase 0)

- `nous/cognitive/critic.py` — New. CriticAgent class, complexity gate, diagnostic critics
- `nous/cognitive/layer.py` — Modify. Insert Critic pre-turn hook, inject diagnostic nudges
- `nous/config.py` — Modify. Add Critic configuration (env vars, defaults)

### Files to Create/Modify (Phase 1)

- `nous/cognitive/transaction.py` — New. CognitiveTransaction, TransactionInterceptor
- `nous/cognitive/critic.py` — Modify. Add parallel routing, post-execution evaluation
- `nous/tools/interceptor.py` — New. Tool-level write interception for parallel mode
- `nous/subtasks/worker.py` — Modify. Support transactional execution mode

### Files to Create/Modify (Phase 2)

- `nous/cognitive/critic.py` — Modify. Add merge logic, cherry-pick evaluation
- `nous/cognitive/transaction.py` — Modify. Add cherry_pick() method

### Files to Create/Modify (Phase 3)

- `nous/cognitive/routing_history.py` — New. Routing decision log + pattern learning
- `nous/cognitive/critic.py` — Modify. Inject routing history into Critic prompt
