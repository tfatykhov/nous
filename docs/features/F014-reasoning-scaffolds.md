# F014 — Frame Reasoning Scaffolds

**Status:** Draft (v2 — revised after review)  
**Author:** Nous (with Tim)  
**Created:** 2026-03-05  
**Revised:** 2026-03-05  
**Depends on:** F003 (Frames), F002 (Brain/Decisions), F001 (Heart/Memory)  
**Inspired by:** Paper #11 (Semi-Formal Reasoning Templates, arXiv 2603.01896v1)

---

## Problem

Nous has 7 cognitive frames (task, question, decision, creative, conversation, debug, initiation) that control **tool availability** and **context budgets**, but they provide only generic prose instructions. There is no structured reasoning guidance — the agent decides *how* to think about each task ad hoc.

Research (Paper #11, Kostka 2026) shows that semi-formal reasoning templates improve LLM accuracy by up to 30% on code analysis tasks. The key insight: **structured scaffolds constrain the reasoning path without constraining the conclusion**, reducing hallucination, improving consistency, and producing auditable deliberation traces.

Currently:
- Frame instructions in `runner.py::_get_frame_instructions()` are tool-use nudges, not reasoning scaffolds
- `deliberation.py` has a basic `_should_deliberate()` gate but no structured deliberation process
- Decision `record_decision` captures *what* was decided but not the structured *reasoning process*
- No quality gates on memory operations (facts get stored without consistency checks)

## Solution

Add **Reasoning Scaffold Templates** to each frame — structured step-by-step thinking patterns that are injected into the system prompt alongside existing tool instructions. Templates are semi-formal: they define required reasoning phases without constraining the content of each phase.

## Design Principles

1. **Scaffolds guide, not constrain** — Templates define *what steps to take*, not *what to conclude*
2. **Frame-native** — Each frame gets scaffolds matched to its cognitive purpose
3. **Auditable** — Deliberation traces stored with decisions, enabling calibration analysis
4. **Adaptive** — Templates compress after first turn and skip for trivial interactions in appropriate frames
5. **Extensible** — New templates can be added without code changes (DB-driven in Phase 3)

---

## Architecture

### Phase 1: Static Scaffolds (Prompt Injection)

Extend `_get_frame_instructions()` in `runner.py` to include reasoning scaffolds per frame.

#### Scaffold Compression (Turn-Aware Injection)

To manage token costs over multi-turn conversations:
- **Turn 1 in frame:** Inject full scaffold (150-300 tokens)
- **Turn 2+ in frame:** Inject 1-line compressed reminder (e.g., "Follow the decision scaffold: CONTEXT → RECALL → OPTIONS → TRADEOFFS → EVIDENCE → CONFIDENCE → DECIDE")

This requires tracking turn count per frame in `TurnContext`. The frame's turn counter resets when the frame changes.

#### Frame Scaffolds

**Decision Frame:**
```
REASONING SCAFFOLD — Decision Analysis:
1. CONTEXT: State the decision and constraints
2. RECALL: Search for similar past decisions (recall_deep)
3. OPTIONS: Enumerate alternatives if viable ones exist. If there is only one reasonable option, explicitly state why alternatives were considered and rejected.
4. TRADEOFFS: For each option, list pros/cons/risks
5. EVIDENCE: Rate evidence strength (strong/moderate/weak/none) for each option
6. CONFIDENCE: Score 0.0-1.0 — evidence FOR minus evidence AGAINST, not optimism
7. DECIDE: Choose and record with record_decision
```

**Debug Frame:**
```
REASONING SCAFFOLD — Diagnostic Analysis:
1. SYMPTOM: Describe the observed behavior precisely
2. REPRODUCE: Can the issue be reproduced? Under what conditions?
3. ISOLATE: Narrow scope — what works, what doesn't?
4. HYPOTHESIZE: Generate at least 2 candidate root causes
5. TEST: Design a test to distinguish between hypotheses
6. VERIFY: Confirm the root cause with evidence
7. FIX: Implement and verify the fix
8. RECORD: Store root cause as fact, record decision if approach was chosen
```

**Task Frame:**
```
REASONING SCAFFOLD — Task Execution:
1. UNDERSTAND: Restate the task and success criteria
2. RECALL: Check memory for relevant context, past approaches, procedures
3. PLAN: Break into steps if complex (>1 tool call likely)
4. EXECUTE: Work through steps, checking results
5. VERIFY: Does the output meet the success criteria?
6. LEARN: Store any new facts discovered; record decisions if alternatives were chosen
```

**Question Frame:**
```
REASONING SCAFFOLD — Knowledge Retrieval:
1. CLASSIFY: What kind of question? (factual, opinion, procedural, temporal)
2. RECALL: Search memory first (recall_deep, recall_recent)
3. VERIFY: Is the recalled information current and reliable?
4. SUPPLEMENT: If memory is insufficient, search web
5. ANSWER: Respond with appropriate confidence qualifiers
```

**Creative Frame:**
```
REASONING SCAFFOLD — Creative Process:
1. UNDERSTAND: What is being created and for whom?
2. INSPIRE: Search for reference material and patterns
3. GENERATE: Produce initial draft/concept
4. EVALUATE: Does it meet the brief? What's missing?
5. REFINE: Iterate on weak areas
6. DELIVER: Present with rationale for key choices
```

**Conversation Frame:**
No scaffold. Conversations should feel natural, not formulaic. The existing frame instructions are sufficient. Fact storage should happen organically when the user explicitly shares information — not as a prompted checklist.

**Initiation Frame:**
No scaffold. Onboarding is a guided flow with its own structure.

#### Research Subtask Scaffold

Research is not a main-conversation frame — it's a subtask mode. This scaffold is injected via `build_subtask_prefix()` when `frame_type="research"` is passed to `spawn_task`.

```
REASONING SCAFFOLD — Research Synthesis:
1. SCOPE: Define the research question and boundaries
2. GATHER: Search multiple sources (web_search, recall_deep)
3. EVALUATE: Assess source quality and recency
4. SYNTHESIZE: Identify patterns, contradictions, gaps across sources
5. CONCLUDE: State findings with confidence levels
6. STORE: Record key facts and cite sources
```

### Phase 2: Enhanced Brain Quality Checks

Instead of adding parallel prompt-based quality gates, **integrate quality checks into existing Brain validation flow**. This avoids conflicting enforcement layers.

#### Enhanced `_should_deliberate()` in `deliberation.py`
- Before recording a decision, check if the scaffold's reasoning steps were followed
- Flag decisions with confidence > 0.9 for extra scrutiny (suspiciously high)
- Verify that alternatives were considered (if Decision scaffold was active)

#### Enhanced `learn_fact` validation in Heart
- Extend existing fact storage logic with lightweight checks:
  - **ATOMIC:** Does the fact contain a single piece of information?
  - **NOVEL:** Quick `recall_deep` check for potential duplicates before committing
  - **ATTRIBUTED:** Is a source specified?
- These are **programmatic checks in the storage layer**, not prompt injection
- Non-blocking in v1: log warnings for quality issues but still store the fact

### Phase 3: DB-Driven Templates (Future)

Move templates from hardcoded strings to a database table, enabling:
- Runtime modification without code deploys
- Per-domain template variants (e.g., "security decision" vs "architecture decision")  
- Template versioning and A/B testing
- User-customizable scaffolds

#### Schema
```sql
CREATE TABLE reasoning_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_name VARCHAR(100) NOT NULL,  -- agent identifier (no agents table yet)
    frame_type VARCHAR(30) NOT NULL,
    domain VARCHAR(100),  -- NULL = default for frame
    template_name VARCHAR(200) NOT NULL,
    steps JSONB NOT NULL,  -- ordered array of {name, instruction, required}
    version INT NOT NULL DEFAULT 1,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_reasoning_templates_lookup
    ON reasoning_templates(agent_name, frame_type, domain)
    WHERE active = true;
```

**Domain values** (initial set, aligned with `record_decision` categories):
- `architecture`
- `security`
- `performance`
- `integration`
- `process`
- `memory`
- `debugging`

#### Step Schema (JSONB)
```json
[
    {
        "name": "CONTEXT",
        "instruction": "State the decision and constraints",
        "required": true
    },
    {
        "name": "RECALL",
        "instruction": "Search for similar past decisions",
        "required": true
    },
    {
        "name": "OPTIONS",
        "instruction": "Enumerate alternatives if viable ones exist",
        "required": true
    }
]
```

---

## Implementation Plan

### Phase 1 — Static Scaffolds (~4 hours)

**1.1 — Scaffold Text Module**
- Create shared scaffold definitions (used by both `_get_frame_instructions()` and `build_subtask_prefix()`)
- Each scaffold is a simple string constant

**1.2 — Extend `_get_frame_instructions()` in `runner.py`**
- Append reasoning scaffold text to each frame's instruction block (except conversation + initiation)
- Keep existing tool instructions; append scaffold after them
- Gate behind a config flag `NOUS_REASONING_SCAFFOLDS=true` (default true)

**1.3 — Turn-Aware Scaffold Compression**
- Add `frame_turn_count` to `TurnContext`
- Increment when frame stays the same across turns; reset on frame change
- Turn 1: full scaffold injection
- Turn 2+: compressed 1-line reminder with step names only

**1.4 — Extend `build_subtask_prefix()` in `tools.py`**
- Inject Research scaffold when `frame_type="research"`
- Inject frame-appropriate scaffold for other subtask frame types

**1.5 — Complexity Gate**
- Skip scaffold injection when ALL of:
  - Frame is `conversation` or `initiation`
  - User message < 20 chars
- All other frames (task, debug, decision, question, creative) **always** get scaffolds regardless of message length
- Rationale: if the frame classifier assigned a structured frame, the task needs structured reasoning even if the prompt is short

### Phase 2 — Enhanced Brain Quality Checks (~2 hours)

**2.1 — Extend `_should_deliberate()` in `deliberation.py`**
- Use scaffold-informed criteria for deliberation triggers
- Add confidence calibration warning for confidence > 0.9

**2.2 — Extend fact storage validation in Heart**
- Programmatic checks (atomic, novel, attributed) in the storage layer
- Log warnings for quality issues in v1, don't block storage

### Phase 3 — DB-Driven Templates (~4 hours, future)

**3.1 — Create `reasoning_templates` table**
**3.2 — Seed with Phase 1 scaffolds**
**3.3 — Modify `_get_frame_instructions()` to query DB (with cache)**
**3.4 — Add domain-specific template selection based on intent signals**

---

## Affected Files

- `nous/api/runner.py` — Extend `_get_frame_instructions()` with scaffolds + turn-aware compression
- `nous/api/tools.py` — Extend `build_subtask_prefix()` with scaffolds
- `nous/cognitive/schemas.py` — Add `frame_turn_count` to `TurnContext`
- `nous/cognitive/deliberation.py` — Enhanced deliberation criteria (Phase 2)
- `nous/memory/heart.py` — Fact quality checks in storage layer (Phase 2)
- `sql/migrations/` — New migration for `reasoning_templates` table (Phase 3)

---

## Token Budget Impact

Each scaffold adds ~150-300 tokens to the system prompt on **first turn only**. Subsequent turns get a compressed 1-line reminder (~30-50 tokens).

**First-turn cost per frame:**
- Decision: +250 tokens
- Debug: +220 tokens
- Task: +180 tokens
- Question: +150 tokens
- Creative: +160 tokens
- Conversation: +0 tokens (no scaffold)
- Initiation: +0 tokens (no scaffold)
- Research (subtask): +170 tokens

**Multi-turn example (5-turn debug session):**
- Without compression: 5 × 220 = 1,100 tokens
- With compression: 220 + (4 × 40) = 380 tokens (65% reduction)

**Mitigation:**
- Turn-aware compression reduces repetition cost dramatically
- Complexity gate skips scaffolds for trivial conversations
- Scaffolds are concise — step names + one-line instructions, not paragraphs

---

## Interaction with `run_python`

Scaffolds guide **high-level reasoning and tool selection**, including what recall queries to batch in `run_python`. They do NOT inject into the Python execution environment itself.

Example: A Decision scaffold step "RECALL: Search for similar past decisions" may cause the agent to write a `run_python` script that calls `recall_deep()` with relevant queries. The scaffold influenced *what* to search for, but the Python code itself runs in its normal sandboxed context.

---

## Success Metrics

1. **Decision quality** — Decisions with documented alternatives increase from ~30% to ~70%+
2. **Confidence calibration** — Mean absolute calibration error decreases (measured via outcome tracking)
3. **Fact deduplication** — Duplicate fact rate drops by >50%
4. **Debug resolution** — Root cause identification rate in debug frames improves
5. **Deliberation traces** — >90% of decisions in decision frames follow the scaffold steps
6. **Token efficiency** — Multi-turn scaffold cost stays under 15% of total system prompt tokens

---

## Research Connections

- **Paper #11** (Semi-Formal Reasoning, arXiv 2603.01896) — Direct inspiration. Structured templates with natural language slots
- **Paper #4** (SCL) — 5 cognitive phases map to scaffold steps (Cognition phase = our scaffold execution)
- **Paper #5** (ACC) — Better structured reasoning produces better state for compression
- **Paper #10** (ToM/IB) — Caution: scaffolds should be adaptive, not forced on simple tasks. Model capability is dominant factor — scaffolds amplify strengths but can confuse weak models
- **Paper #3** (Episodic Memory) — Scaffold traces become part of episodic memory, improving future recall

---

## CSTP / Cognition Engine Implications

This feature has a natural extension path into the Cognition Engine / CSTP protocol:

1. **Template serving** — CSTP could serve reasoning templates to connected agents based on decision category
2. **Cross-agent calibration** — Scaffold traces enable comparing reasoning quality across different LLM backends
3. **Template marketplace** — Domain-specific template packs (DevOps, Security, Architecture) as a product feature
4. **Reasoning audit trail** — Every decision made through CSTP has a structured deliberation trace, not just a result

---

## Revision History

**v2 (2026-03-05) — Post-review updates:**
- Dropped conversation scaffold entirely — conversations should feel natural
- Dropped initiation scaffold — has its own guided flow
- Renamed "Research Frame" to "Research Subtask Scaffold" — injected via `build_subtask_prefix()`, not frame system
- Softened OPTIONS step from "at least 2 alternatives" to "enumerate if viable, explain why if not"
- Added turn-aware scaffold compression (full on turn 1, 1-line reminder on turn 2+)
- Merged Phase 2 into existing Brain validation flow — no parallel prompt-based quality gates
- Fixed complexity gate: skip only in conversation + initiation frames, all structured frames always get scaffolds
- Fixed Phase 3 schema: removed `agents(id)` FK reference (table doesn't exist), defined concrete domain values
- Added `run_python` interaction section
- Added token efficiency metric
- Updated affected files list (added `heart.py`, `schemas.py`)

---

## Open Questions

1. Should scaffolds be visible in the agent's response, or purely internal (thinking block only)?
2. Template inheritance — should subtasks inherit the parent's scaffold or get their own based on frame_type?
3. Should Phase 2 fact quality checks become blocking (reject bad facts) after a validation period, or remain advisory permanently?
4. How should scaffold compression handle frame switches mid-conversation? (e.g., conversation → decision → conversation — does decision get full scaffold again?)
