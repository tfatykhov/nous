# F014 — Frame Reasoning Scaffolds

**Status:** Draft (v3 — revised after architecture review)  
**Author:** Nous (with Tim)  
**Created:** 2026-03-05  
**Revised:** 2026-03-05  
**Depends on:** F003 (Frames), F002 (Brain/Decisions), F001 (Heart/Memory)  
**Inspired by:** Paper #11 (Semi-Formal Reasoning Templates, arXiv 2603.01896v1)

---

## Problem

Nous has 7 cognitive frames (task, question, decision, creative, conversation, debug, initiation) that control **tool availability** and **context budgets**, but they provide only generic tool-use nudges. There is no structured reasoning guidance — the agent decides *how* to think about each task ad hoc.

Research (Paper #11, Kostka 2026) shows that semi-formal reasoning templates improve LLM accuracy by up to 30% on code analysis tasks. The key insight: **structured scaffolds constrain the reasoning path without constraining the conclusion**, reducing hallucination, improving consistency, and producing auditable deliberation traces.

Currently:
- Frame instructions in `runner.py::_get_frame_instructions()` are tool-use nudges, not reasoning scaffolds
- `deliberation.py` has a basic `_should_deliberate()` gate and `_validate_decision_quality()` but no structured deliberation process
- `record_decision` captures *what* was decided but not the structured *reasoning process*

## Solution

Add **Reasoning Scaffold Templates** — structured step-by-step thinking patterns that **replace** existing frame instructions (not layer on top). Pilot with Decision frame first, measure, then expand.

### Design Principles (from Paper #11)

1. **Guide process, not conclusions** — scaffolds tell you *what steps to take*, never *what to decide*
2. **Frame-native** — each scaffold matches its frame's cognitive purpose
3. **Token-efficient** — 150-300 tokens per scaffold with turn-aware compression
4. **Auditable** — scaffold steps create traceable deliberation artifacts
5. **Additive, not duplicative** — scaffolds REPLACE existing tool nudges, merging tool guidance into scaffold steps

---

## Phase 1 — Decision Frame Pilot (~2h)

### Rollout Strategy

**Decision frame only** for 2 weeks. Measure before expanding.

Why pilot Decision first:
- Highest-stakes frame — bad decisions are expensive
- Most measurable — `decisions` table tracks confidence, reasons, categories, stakes
- Already has deliberation infrastructure (`_should_deliberate()`, `_validate_decision_quality()`)
- Richest existing instructions — most to merge/replace

### Success Criteria (2-week measurement window)

- Average reason count per decision increases (baseline: current avg)
- Confidence calibration improves (fewer 0.95+ decisions that get marked as failures)
- No increase in scaffold-related token costs > 15% per decision turn
- Qualitative: decision descriptions become more structured without feeling formulaic

### File Structure

Create `nous/nous/cognitive/scaffolds.py` — standalone module, imported by `runner.py`.

```python
# nous/nous/cognitive/scaffolds.py
"""Reasoning scaffold templates for cognitive frames.

Each scaffold provides structured step-by-step reasoning guidance.
Scaffolds REPLACE frame tool-nudge instructions (not layered on top).
Phase 1: Decision frame only. Expand after measurement.
"""

# Full scaffold — injected on turn 1 of a frame
DECISION_SCAFFOLD = """## Reasoning Scaffold — Decision Frame

Follow these steps. You may skip steps that don't apply, but don't skip RECALL or CALIBRATE.

1. **CONTEXT** — State what needs to be decided and why now.
2. **RECALL** — Search memory for similar past decisions. Note what worked and what didn't.
   → Use `recall_deep` to find relevant prior decisions.
3. **CONSTRAINTS** — List hard constraints (time, resources, compatibility, censors).
4. **OPTIONS** — Enumerate viable alternatives. If only one reasonable option exists, state why others were rejected.
5. **TRADEOFFS** — For each option: pros, cons, risks, reversibility.
   → Use `web_search` and `web_fetch` to research options if needed.
6. **EVIDENCE** — What data supports each option? Flag gaps where you're guessing.
7. **CALIBRATE** — Set confidence honestly. What would change your mind? What's your uncertainty?
8. **DECIDE** — Record with `record_decision`. Include category, stakes, confidence, and structured reasons.

Do NOT record status reports, routine completions, or greetings as decisions."""

# Compressed reminder — injected on turn 2+ within same frame
DECISION_SCAFFOLD_SHORT = (
    "Continue following the Decision scaffold: "
    "CONTEXT → RECALL → CONSTRAINTS → OPTIONS → TRADEOFFS → EVIDENCE → CALIBRATE → DECIDE. "
    "Use `record_decision` for real decisions only."
)


def get_scaffold(frame_id: str, turn_in_frame: int, message_length: int) -> str | None:
    """Return the appropriate scaffold for a frame and turn.

    Args:
        frame_id: The active cognitive frame identifier.
        turn_in_frame: Which turn within this frame (1-indexed).
        message_length: Character length of the user's message.

    Returns:
        Scaffold string, or None if no scaffold applies.
    """
    # Phase 1: Only Decision frame gets a scaffold
    if frame_id != "decision":
        return None

    # Turn-aware compression
    if turn_in_frame <= 1:
        return DECISION_SCAFFOLD
    else:
        return DECISION_SCAFFOLD_SHORT
```

### Integration Point — `runner.py::_get_frame_instructions()`

```python
# In _get_frame_instructions():
from nous.cognitive.scaffolds import get_scaffold

def _get_frame_instructions(self, turn_context: TurnContext) -> str:
    frame_id = turn_context.frame.frame_id

    # Check for reasoning scaffold (replaces tool nudges for scaffolded frames)
    scaffold = get_scaffold(
        frame_id=frame_id,
        turn_in_frame=turn_context.turn_in_frame,  # requires tracking
        message_length=len(turn_context.user_message or ""),
    )
    if scaffold:
        return scaffold

    # Fallback to existing tool nudges for non-scaffolded frames
    if frame_id == "task":
        return "## Tool Instructions\n\n..."  # existing
    # ... etc
```

Key: scaffolds **replace** the existing `_get_frame_instructions()` return for their frame. No layering. The tool guidance ("Use `recall_deep`", "Use `record_decision`") is merged INTO the scaffold steps.

### Turn Tracking

Add `turn_in_frame: int` to TurnContext (or derive from conversation history). Resets when frame changes.

Token savings from compression:
- Turn 1: ~280 tokens (full scaffold)
- Turn 2+: ~45 tokens (compressed)
- 5-turn decision session: ~460 tokens total vs ~1,400 if full scaffold every turn (67% savings)

---

## Phase 1.5 — Expand to Other Frames (after 2-week pilot succeeds)

Only proceed if Decision frame pilot meets success criteria.

### Debug Frame Scaffold

```python
DEBUG_SCAFFOLD = """## Reasoning Scaffold — Debug Frame

1. **SYMPTOM** — What's the observable problem? Error messages, unexpected behavior, log output.
2. **REPRODUCE** — Can you trigger it reliably? What are the exact steps?
3. **ISOLATE** — Narrow the search space. Which component, file, function?
   → Use `bash` and `read_file` for investigation.
4. **HYPOTHESIZE** — What could cause this? List 2-3 candidates ranked by likelihood.
   → Use `recall_deep` to check for similar past bugs.
5. **TEST** — Design a test for each hypothesis. Run it.
   → Use `web_search` and `web_fetch` to look up error messages or docs.
6. **VERIFY** — Confirm the root cause. Don't fix symptoms.
7. **FIX** — Implement the fix. Explain why it addresses the root cause.
8. **RECORD** — Store root cause with `learn_fact`. Record meaningful debugging decisions with `record_decision` (root cause identified, fix approach chosen). Do NOT record routine debug steps.

Do NOT record routine status observations as decisions."""

DEBUG_SCAFFOLD_SHORT = (
    "Continue following the Debug scaffold: "
    "SYMPTOM → REPRODUCE → ISOLATE → HYPOTHESIZE → TEST → VERIFY → FIX → RECORD. "
    "Store root causes with `learn_fact`."
)
```

### Task Frame Scaffold

```python
TASK_SCAFFOLD = """## Reasoning Scaffold — Task Frame

1. **UNDERSTAND** — What's being asked? Restate the goal in your own terms.
2. **PLAN** — Break into steps. Identify dependencies and order.
   → Use `recall_deep` for relevant past work.
3. **EXECUTE** — Work through each step. Use all available tools.
4. **VERIFY** — Check your work. Does the output match the goal?
5. **REPORT** — Summarize what was done and any follow-ups needed.
   → Store important outcomes with `learn_fact`."""

TASK_SCAFFOLD_SHORT = (
    "Continue following the Task scaffold: "
    "UNDERSTAND → PLAN → EXECUTE → VERIFY → REPORT."
)
```

### Question Frame Scaffold

```python
QUESTION_SCAFFOLD = """## Reasoning Scaffold — Question Frame

1. **RECALL** — Search memory first. What do you already know?
   → Use `recall_deep` to search for relevant knowledge.
2. **ASSESS** — Is memory sufficient, or do you need external info?
   → Use `web_search` and `web_fetch` for current events or topics not in memory.
3. **SYNTHESIZE** — Combine sources. Flag confidence level and gaps.
4. **ANSWER** — Respond clearly. Cite sources when possible."""

QUESTION_SCAFFOLD_SHORT = (
    "Continue following the Question scaffold: "
    "RECALL → ASSESS → SYNTHESIZE → ANSWER."
)
```

### Complexity Gate (Phase 1.5)

Add complexity sensing for lighter frames:

```python
# Frames that ALWAYS get scaffolds regardless of message length
_ALWAYS_SCAFFOLD = {"decision", "debug", "task"}

# Frames that skip scaffolds for very short messages
_COMPLEXITY_GATED = {"question"}

# Frames that NEVER get scaffolds
_NEVER_SCAFFOLD = {"conversation", "creative", "initiation"}

def get_scaffold(frame_id: str, turn_in_frame: int, message_length: int) -> str | None:
    if frame_id in _NEVER_SCAFFOLD:
        return None

    if frame_id in _COMPLEXITY_GATED and message_length < 30:
        return None

    if frame_id in _ALWAYS_SCAFFOLD or frame_id in _COMPLEXITY_GATED:
        # return appropriate scaffold based on frame_id and turn
        ...
```

### Frames NOT scaffolded (rationale)

- **Conversation** — should feel natural, not transactional. Existing minimal nudge is sufficient.
- **Creative** — scaffolds constrain creative thinking. Keep it open.
- **Initiation** — has its own guided flow (store_identity, complete_initiation).

---

## Research Subtask Scaffold (separate from frame system)

"Research" is NOT a frame in FRAME_TOOLS. Research runs as subtasks via `spawn_task(frame_type="research")`.

### Injection Point — `build_subtask_prefix()` in `nous/nous/api/tools.py`

```python
# In build_subtask_prefix():
RESEARCH_SUBTASK_SCAFFOLD = (
    "You are executing a research subtask. Follow this process:\n"
    "1. SCOPE — Define what you're researching and success criteria.\n"
    "2. SEARCH — Use web_search with multiple query angles.\n"
    "3. GATHER — Use web_fetch to read promising sources. Note conflicts.\n"
    "4. SYNTHESIZE — Combine findings. Separate facts from inference.\n"
    "5. DELIVER — Structured summary with key findings, sources, and confidence.\n"
    "Deliver a clear, complete result. Do not ask questions."
)

def build_subtask_prefix(task: str, frame_type: str | None = None) -> str:
    if frame_type == "research":
        return f"{RESEARCH_SUBTASK_SCAFFOLD}\n\nTask: {task}"

    # existing logic for other frame types
    base = "You are executing a background subtask.\n..."
    ...
```

---

## Phase 2 — DB-Driven Templates (future, ~4h)

**Phase 2 scope is narrower than v2 spec.** No scaffold compliance checking — that creates perverse incentives (performing steps for appearance rather than genuine reasoning). The existing `_validate_decision_quality()` in `deliberation.py` handles structural quality.

Phase 2 is about **extensibility without code changes**:

### Schema

```sql
CREATE TABLE reasoning_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    frame_id VARCHAR(30) NOT NULL,              -- 'decision', 'debug', etc.
    domain VARCHAR(50),                          -- 'architecture', 'security', 'performance', 'integration', 'process', 'memory', 'debugging'
    name VARCHAR(100) NOT NULL,
    steps JSONB NOT NULL,                        -- [{"name": "CONTEXT", "instruction": "...", "tool_hint": "recall_deep"}]
    compressed TEXT NOT NULL,                    -- 1-line reminder version
    is_default BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Concrete domain values (aligned with record_decision categories):
-- architecture, security, performance, integration, process, memory, debugging
-- NULL domain = default template for that frame
```

### Lookup logic

```python
def get_scaffold(frame_id, turn_in_frame, message_length, domain=None):
    # 1. Check DB for domain-specific template
    # 2. Fall back to DB default template (domain=NULL)
    # 3. Fall back to hardcoded static scaffolds
    ...
```

### CSTP Extension (future)

When Cognition Engine protocol ships:
- Templates served to connected agents via CSTP
- Agent declares frame/domain → receives appropriate scaffold
- Reasoning traces stored with decision artifacts
- Domain template library as productizable feature

---

## Affected Files

| File | Change |
|------|--------|
| `nous/nous/cognitive/scaffolds.py` | **NEW** — scaffold templates + `get_scaffold()` function |
| `nous/nous/api/runner.py` | Modify `_get_frame_instructions()` to call `get_scaffold()` first |
| `nous/nous/api/tools.py` | Modify `build_subtask_prefix()` for research scaffold |
| `nous/nous/cognitive/context.py` | Add `turn_in_frame` tracking to TurnContext |
| `nous/nous/heart/heart.py` | Phase 2 only — template storage/retrieval |
| `nous/nous/cognitive/schemas.py` | Phase 2 only — template Pydantic models |

---

## What This Spec Does NOT Do

- **No scaffold compliance checking** — verifying "did you follow step 3?" creates perverse incentives. Removed per review.
- **No conversation/creative/initiation scaffolds** — these frames don't benefit from structured reasoning.
- **No automatic expansion** — each frame gets scaffolds only after the previous frame's pilot shows measurable improvement.
- **No modifications to `record_decision` schema** — scaffolds improve the reasoning *input*, the decision schema captures the *output* unchanged.

---

## Token Budget

### Phase 1 (Decision only)
- Turn 1: ~280 tokens (full scaffold)
- Turn 2+: ~45 tokens (compressed)
- 5-turn decision session: ~460 tokens total
- vs no compression: ~1,400 tokens (67% savings)
- vs current tool nudges: ~150 tokens/turn → scaffolds add ~130 tokens on turn 1, save on subsequent turns

### Phase 1.5 (All scaffolded frames)
- Each scaffold: 150-280 tokens (turn 1), 30-50 tokens (turn 2+)
- Worst case (decision): +130 tokens on turn 1 vs current nudges
- Best case (question): ~120 tokens, similar to current nudges

---

## Revision History

**v3 (2026-03-05) — Post-architecture-review:**
- **Rollout changed:** Decision frame pilot first (2 weeks), expand only after measurement
- **Research scaffold:** Moved to `build_subtask_prefix()` in tools.py (not frame system)
- **Tool nudge merge:** Scaffolds REPLACE `_get_frame_instructions()` output, not layer on top
- **File path fixed:** `nous/nous/heart/heart.py` (was `nous/memory/heart.py`)
- **scaffolds.py:** Extracted to `nous/nous/cognitive/scaffolds.py` from day 1
- **Complexity gate:** Added intra-frame sensing (question frame skips for < 30 char messages)
- **Phase 2 compliance checking removed:** Creates perverse incentives, existing `_validate_decision_quality()` is sufficient
- **Success criteria added:** Measurable metrics for pilot evaluation

**v2 (2026-03-05) — Post-review updates:**
- Dropped conversation and initiation scaffolds
- Renamed Research Frame → Research Subtask Scaffold
- Softened OPTIONS step from "at least 2 alternatives" to "enumerate if viable"
- Added turn-aware scaffold compression
- Merged Phase 2 into existing Brain validation flow
- Fixed complexity gate, Phase 3 schema, added run_python interaction section

**v1 (2026-03-05) — Initial draft**

---

## Open Questions

1. Should scaffolds be visible in the agent's response, or purely internal (thinking block only)?
2. Template inheritance — should subtasks inherit the parent's scaffold or get their own based on frame_type?
3. How should scaffold compression handle frame switches mid-conversation? (Reset turn count per frame switch — proposed default)
4. What's the baseline decision quality metric? Need to measure current avg reason count, confidence distribution, and failure rate before pilot starts.
