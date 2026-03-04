# F014 — Frame Reasoning Scaffolds

**Status:** Draft  
**Author:** Nous (with Tim)  
**Created:** 2026-03-05  
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
4. **Adaptive** — Templates can be skipped for trivial tasks (under a complexity threshold)
5. **Extensible** — New templates can be added without code changes (DB-driven in Phase 3)

---

## Architecture

### Phase 1: Static Scaffolds (Prompt Injection)

Extend `_get_frame_instructions()` in `runner.py` to include reasoning scaffolds per frame.

#### Frame Scaffolds

**Decision Frame:**
```
REASONING SCAFFOLD — Decision Analysis:
1. CONTEXT: State the decision and constraints
2. RECALL: Search for similar past decisions (recall_deep)
3. OPTIONS: Enumerate at least 2 alternatives
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

**Research Frame (subtask):**
```
REASONING SCAFFOLD — Research Synthesis:
1. SCOPE: Define the research question and boundaries
2. GATHER: Search multiple sources (web_search, recall_deep)
3. EVALUATE: Assess source quality and recency
4. SYNTHESIZE: Identify patterns, contradictions, gaps across sources
5. CONCLUDE: State findings with confidence levels
6. STORE: Record key facts and cite sources
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
Minimal scaffold — conversations should feel natural, not formulaic.
```
REASONING SCAFFOLD — Conversation:
- If the user shares information, consider storing as fact
- If a decision point arises, switch to decision reasoning
- Proactively recall relevant context before responding
```

### Phase 2: Memory Quality Gates

Add lightweight pre-commit checks before memory operations.

#### Fact Quality Gate (before `learn_fact`)
```
Before storing a fact, verify:
- ATOMIC: Does it contain exactly one piece of information?
- NOVEL: Does it duplicate an existing fact? (recall_deep check)
- CONFIDENT: Is the confidence level justified by evidence?
- ATTRIBUTED: Is the source clear?
```

#### Decision Quality Gate (before `record_decision`)
```
Before recording a decision, verify:
- ALTERNATIVES: Were at least 2 options considered?
- EVIDENCE: Is there supporting evidence for the chosen option?
- CALIBRATED: Does the confidence reflect uncertainty, not optimism?
- DESCRIBED: Is the description a clean summary, not a stream-of-consciousness?
```

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
    agent_id UUID NOT NULL REFERENCES agents(id),
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
    ON reasoning_templates(agent_id, frame_type, domain)
    WHERE active = true;
```

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
        "instruction": "Enumerate at least 2 alternatives",
        "required": true
    }
]
```

---

## Implementation Plan

### Phase 1 — Static Scaffolds (~3 hours)

**1.1 — Extend `_get_frame_instructions()` in `runner.py`**
- Add reasoning scaffold text to each frame's instruction block
- Keep existing tool instructions; append scaffold after them
- Gate behind a config flag `NOUS_REASONING_SCAFFOLDS=true` (default true)

**1.2 — Extend `build_subtask_prefix()` in `tools.py`**
- Subtasks should also receive scaffolds matching their `frame_type`
- Extract scaffold text into a shared function used by both `_get_frame_instructions()` and `build_subtask_prefix()`

**1.3 — Complexity Gate**
- Add a simple heuristic: if user message < 20 chars and frame is `conversation`, skip scaffold
- Prevents over-engineering simple greetings/acknowledgments

### Phase 2 — Memory Quality Gates (~2 hours)

**2.1 — Fact Quality Gate**
- Add scaffold text to frames that encourage pre-commit checks before `learn_fact`
- Not enforced programmatically in v1 — purely prompt-based guidance

**2.2 — Decision Quality Gate**
- Extend decision frame scaffold with explicit quality checklist
- Log warning in `_check_safety_net()` if decision confidence > 0.9 (suspiciously high)

### Phase 3 — DB-Driven Templates (~4 hours, future)

**3.1 — Create `reasoning_templates` table**
**3.2 — Seed with Phase 1 scaffolds**
**3.3 — Modify `_get_frame_instructions()` to query DB**
**3.4 — Add domain-specific template selection based on intent signals**

---

## Affected Files

- `nous/api/runner.py` — Extend `_get_frame_instructions()` with scaffolds
- `nous/api/tools.py` — Extend `build_subtask_prefix()` with scaffolds
- `nous/cognitive/schemas.py` — Add `scaffold_enabled` to `FrameSelection` (Phase 1)
- `nous/cognitive/deliberation.py` — Add confidence calibration warning (Phase 2)
- `sql/migrations/` — New migration for `reasoning_templates` table (Phase 3)

---

## Token Budget Impact

Each scaffold adds ~150-300 tokens to the system prompt. Current frame instructions are ~50-100 tokens each.

**Estimated increase per frame:**
- Decision: +250 tokens (most detailed scaffold)
- Debug: +220 tokens
- Task: +180 tokens
- Research: +170 tokens
- Question: +150 tokens
- Creative: +160 tokens
- Conversation: +80 tokens (minimal scaffold)

**Mitigation:**
- Complexity gate skips scaffolds for trivial interactions
- Scaffolds are concise — step names + one-line instructions, not paragraphs
- Net token cost is small vs. the quality improvement in reasoning

---

## Success Metrics

1. **Decision quality** — Decisions recorded with ≥2 alternatives increase from ~30% to ~80%
2. **Confidence calibration** — Mean absolute calibration error decreases (measured via outcome tracking)
3. **Fact deduplication** — Duplicate fact rate drops by >50%
4. **Debug resolution** — Root cause identification rate in debug frames improves
5. **Deliberation traces** — >90% of decisions in decision frames follow the scaffold steps

---

## Research Connections

- **Paper #11** (Semi-Formal Reasoning, arXiv 2603.01896) — Direct inspiration. Structured templates with natural language slots
- **Paper #4** (SCL) — 5 cognitive phases map to scaffold steps (Cognition phase = our scaffold execution)
- **Paper #5** (ACC) — Better structured reasoning produces better state for compression
- **Paper #10** (ToM/IB) — Caution: scaffolds should be adaptive, not forced on simple tasks (weaker reasoning on trivial tasks wastes tokens)
- **Paper #3** (Episodic Memory) — Scaffold traces become part of episodic memory, improving future recall

---

## CSTP / Cognition Engine Implications

This feature has a natural extension path into the Cognition Engine / CSTP protocol:

1. **Template serving** — CSTP could serve reasoning templates to connected agents based on decision category
2. **Cross-agent calibration** — Scaffold traces enable comparing reasoning quality across different LLM backends
3. **Template marketplace** — Domain-specific template packs (DevOps, Security, Architecture) as a product feature
4. **Reasoning audit trail** — Every decision made through CSTP has a structured deliberation trace, not just a result

---

## Open Questions

1. Should scaffolds be visible in the agent's response, or purely internal (thinking block only)?
2. How aggressive should the complexity gate be? Risk of skipping scaffolds when they'd help vs. cluttering simple interactions
3. Should Phase 2 quality gates be enforced programmatically (reject bad facts) or purely advisory (prompt guidance)?
4. Template inheritance — should subtasks inherit the parent's scaffold or get their own based on frame_type?
