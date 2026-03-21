# 026 — Execution Integrity Implementation Plan

> **Spec:** [F026-execution-integrity.md](../features/F026-execution-integrity.md)
> **Status:** Approved (post-review v2)
> **Decision ID:** 34a8bc84
> **Review:** 3-agent team (arch, integ, devil) — all findings addressed below

---

## Overview

Three new files, three modified files. All three layers (Execution Ledger, Claim Verification, Action Gating) implemented in a single pass. No database changes required — the ledger is session-scoped, in-memory only.

**Constraint:** No DB-dependent tests. All tests are pure unit tests.

### Review Findings Addressed

| Finding | Resolution |
|---------|------------|
| Tool classification phantom entries (`send_email`, `notify_telegram`, `list_files`, `talk_to_emerson`) | Removed; constants match actual registered tools |
| Missing tools (`cache_retrieve`, `spawn_task`, `schedule_task`, `cancel_task`, `run_python`, `complete_initiation`, `get_procedure`) | Added to appropriate sets |
| `_build_system_prompt` needs ledger passed directly | Changed to pass `ledger` kwarg (not session_id) |
| `user_message` not in scope in `_tool_loop` | Add `user_message` param to `_tool_loop` signature |
| LRU eviction doesn't clean `_ledgers` | Add cleanup in eviction loop |
| Streaming re-run impossible | Streaming uses warn+inject-correction; non-streaming can re-run. Documented as known gap. |
| `_args_similar` compares summarized vs raw | Summarize new args before comparison |
| No timeout on gate model call | 5s timeout, fail-open on timeout |
| `_pending_corrections` unspecified | Fully specified: inject in `_build_system_prompt`, clear after injection |
| `execution_ledger_max_tokens` never enforced | `system_prompt_section()` enforces token budget with truncation |

### Known Limitations (accepted)
- Ledger lost on container restart / LRU eviction — spec says session-scoped, accepted
- Bash pipe/semicolon classification is approximate — conservative default covers it
- Ghost planning may false-positive on technical explanations — 2-signal threshold mitigates
- Turn number cosmetically wrong after compaction — non-functional

---

## Files

### New Files
| File | Purpose | LOC (est.) |
|------|---------|------------|
| `nous/cognitive/execution_ledger.py` | ExecutionLedger, ExecutedAction, tool classification | ~220 |
| `nous/cognitive/claim_verifier.py` | ClaimVerifier, IntentTracker, VerificationResult | ~160 |
| `nous/cognitive/action_gate.py` | ActionGate, GateResult, tiered gating | ~200 |
| `tests/test_execution_integrity.py` | Unit tests for all three layers | ~450 |

### Modified Files
| File | Changes |
|------|---------|
| `nous/config.py` | Add 7 F026 settings fields |
| `nous/api/runner.py` | Hook ledger recording, gate checks, claim verification, system prompt injection |

---

## Phase A: Configuration (`nous/config.py`)

Add after the F023 A-MAC settings block (line ~267):

```python
# F026: Execution Integrity
execution_ledger_enabled: bool = True
execution_ledger_max_tokens: int = 500

claim_verification_enabled: bool = True
claim_verification_mode: str = "enforce"  # "shadow" | "warn" | "enforce"

action_gating_enabled: bool = True
action_gating_mode: str = "enforce"  # "shadow" | "warn" | "enforce"
action_gating_model: str = "claude-haiku-4-5-20251001"
```

---

## Phase B: Execution Ledger (`nous/cognitive/execution_ledger.py`)

### Data Model

```python
@dataclass
class ExecutedAction:
    turn: int
    tool_name: str
    key_args: dict[str, str]
    status: str  # "success" | "error" | "timeout" | "blocked"
    timestamp: datetime
    result_summary: str  # First 100 chars
    side_effect_type: str  # "none" | "write" | "external" | "irreversible"
```

### Tool Classification Constants (REVIEWED — matches actual registered tools)

```python
# No side effects — pure reads
READ_TOOLS = {
    "recall_deep", "recall_recent", "read_file", "get_procedure",
    "web_search", "web_fetch", "list_tasks", "cache_retrieve",
}

# Local writes — reversible
WRITE_TOOLS = {
    "write_file", "learn_fact", "record_decision", "create_censor",
    "store_identity", "learn_skill", "complete_initiation",
    "spawn_task", "schedule_task", "cancel_task",
}

# Bash requires dynamic classification (see _classify_bash)
# Note: _classify_bash inspects first command only; pipes/chains approximate

# External side effects — extend when email/notification tools are registered
EXTERNAL_TOOLS: set[str] = set()

# Irreversible — extend when irreversible tools are registered
IRREVERSIBLE_TOOLS: set[str] = set()
```

**Note on `run_python`:** Classified as `"write"` because it can call `learn_fact()` and modify state. Added to WRITE_TOOLS.

### ExecutionLedger Class

- `record(tool_name, tool_input, result, status)` — called by runner after each tool dispatch
- `system_prompt_section(max_tokens: int = 500) -> str` — compact summary, enforces token budget
- `set_turn(turn: int)` — called at start of each turn
- `_classify_side_effect(tool_name, tool_input)` — static classification + dynamic bash analysis
- `_summarize_args(tool_name, args)` — extract key identifying args (truncated to 80 chars)
- `_classify_bash(command)` — read/write/external classification (first command token)
- `has_blocked_actions_this_turn` property
- `one_line_summary() -> str` — for compaction hints

### Token Budget Enforcement

`system_prompt_section()` enforces `max_tokens` by:
1. Building recent actions (last 5 turns) individually
2. Building grouped summary for older actions
3. Estimating tokens (chars // 4)
4. If over budget: reduce recent window from 5 to 3, then to 1
5. If still over: truncate grouped summary

---

## Phase C: System Prompt Injection + Runner Integration

### Runner.__init__ additions

```python
self._ledgers: dict[str, ExecutionLedger] = {}
self._pending_corrections: dict[str, list[str]] = {}
self._claim_verifier = ClaimVerifier() if settings.claim_verification_enabled else None
self._intent_tracker = IntentTracker() if settings.claim_verification_enabled else None
self._action_gate = ActionGate(settings) if settings.action_gating_enabled else None
```

### `_get_or_create_ledger` helper

```python
def _get_or_create_ledger(self, session_id: str) -> ExecutionLedger:
    if session_id not in self._ledgers:
        self._ledgers[session_id] = ExecutionLedger(session_id=session_id)
    return self._ledgers[session_id]
```

### `_build_system_prompt` — pass ledger directly (REVIEW FIX)

Signature change:
```python
def _build_system_prompt(
    self,
    turn_context: TurnContext,
    platform: str | None = None,
    *,
    ledger: ExecutionLedger | None = None,
) -> str:
```

Inject ledger section + pending corrections:
```python
if ledger and self._settings.execution_ledger_enabled:
    ledger_section = ledger.system_prompt_section(self._settings.execution_ledger_max_tokens)
    if ledger_section:
        parts.append(ledger_section)

# Inject pending corrections from prior turn
if session_id_for_corrections and self._pending_corrections.get(session_id_for_corrections):
    corrections = self._pending_corrections.pop(session_id_for_corrections)
    parts.append("[Previous Turn Corrections]\n" + "\n".join(corrections))
```

**Wait** — `_build_system_prompt` doesn't have session_id. For corrections, pass them as a parameter too:
```python
def _build_system_prompt(
    self,
    turn_context: TurnContext,
    platform: str | None = None,
    *,
    ledger: ExecutionLedger | None = None,
    corrections: list[str] | None = None,
) -> str:
```

Callers consume and pass:
```python
corrections = self._pending_corrections.pop(session_id, None)
system_prompt = self._build_system_prompt(
    turn_context, platform=platform,
    ledger=ledger, corrections=corrections,
)
```

### `_tool_loop` — add `user_message` param (REVIEW FIX)

```python
async def _tool_loop(
    self,
    system_prompt: str,
    conversation: Conversation,
    frame_id: str,
    session_id: str | None = None,
    is_subtask: bool = False,
    max_tool_calls: int | None = None,
    model_override: str | None = None,
    user_message: str = "",           # NEW — for ActionGate
    ledger: ExecutionLedger | None = None,  # NEW — for recording
) -> tuple[str, list[ToolResult], dict[str, int], list[str]]:
```

### Pre-dispatch gate check (in `_tool_loop`)

Before `dispatcher.dispatch()`:
```python
# F026: Action gating
if self._action_gate and ledger:
    gate_result = await self._action_gate.check(
        tool_name, tool_input, ledger, user_message=user_message
    )
    if not gate_result.approved:
        if self._settings.action_gating_mode == "enforce":
            result_text = f"[BLOCKED by ActionGate] {gate_result.reason}"
            if gate_result.suggestion:
                result_text += f"\n{gate_result.suggestion}"
            is_error = True
            ledger.record(tool_name, tool_input, result_text, "blocked")
            # Skip dispatch — use blocked result
            # ... continue to tool_results_for_message append
        elif self._settings.action_gating_mode == "warn":
            logger.warning("ActionGate would block %s: %s", tool_name, gate_result.reason)
        # shadow: debug log only
```

### Post-dispatch recording (in `_tool_loop`)

After `dispatcher.dispatch()` returns:
```python
# F026: Record in execution ledger
if ledger:
    ledger.record(tool_name, tool_input, result_text, "error" if is_error else "success")
```

### Post-response verification (in `run_turn`)

After `_tool_loop` returns and before post_turn:
```python
# F026: Claim verification + ghost planning
if self._claim_verifier and ledger:
    turn_tool_names = [tr.tool_name for tr in tool_results]
    verification = self._claim_verifier.verify(response_text, turn_tool_names, ledger)
    if not verification.verified:
        if self._settings.claim_verification_mode == "enforce":
            logger.warning("Claim verification failed: %s", verification.correction)
            self._pending_corrections.setdefault(session_id, []).append(verification.correction)
        elif self._settings.claim_verification_mode == "warn":
            logger.warning("Claim verification: %s", verification.correction)
        # shadow: debug log

if self._intent_tracker and ledger and not tool_results:
    if self._intent_tracker.check_ghost_planning(response_text, [], ledger):
        logger.info("Ghost planning detected in session %s", session_id)
        nudge = self._intent_tracker.build_nudge()
        self._pending_corrections.setdefault(session_id, []).append(nudge)
```

### Same hooks in `stream_chat`

Mirror the above patterns in `stream_chat`:
- Pre-dispatch gate check before `_dispatch_with_keepalive`
- Post-dispatch recording after result tuple received
- Post-response verification after response assembled
- **Streaming limitation:** enforce mode uses warn+inject-correction (cannot re-run)

### LRU eviction cleanup (REVIEW FIX)

In `_get_or_create_conversation`, add to eviction loop:
```python
while len(self._conversations) >= MAX_CONVERSATIONS:
    evicted_id, _ = self._conversations.popitem(last=False)
    self._compaction_locks.pop(evicted_id, None)
    self._ledgers.pop(evicted_id, None)           # F026
    self._pending_corrections.pop(evicted_id, None)  # F026
```

### `end_conversation` cleanup

```python
self._ledgers.pop(session_id, None)
self._pending_corrections.pop(session_id, None)
```

---

## Phase D: Claim Verification (`nous/cognitive/claim_verifier.py`)

### ClaimVerifier

```python
ACTION_CLAIM_PATTERNS = [
    (r"(?:I |I've |I just )(?:saved|wrote|created|generated) .+(?:file|document|report)", "write_file"),
    (r"(?:I |I've |I just )(?:sent|emailed|forwarded) .+(?:email|message|report)", "send_email"),
    (r"(?:I |I've |I just )(?:pushed|committed|deployed)", "bash"),
    (r"(?:saved|written) to[:\s]+[/\w.-]+", "write_file"),
    (r"email sent to", "send_email"),
]
```

### IntentTracker

```python
WORK_PRODUCT_SIGNALS = [
    r"```[\w]*\n.{200,}```",
    r"(?:here'?s|below is) (?:the|a|my) (?:draft|plan|outline|report|email|message)",
    r"(?:I'?ll|let me|going to) (?:write|create|save|send|push)",
    r"[Ss]aved? to[:\s]+[/\w.-]+",
]
```

### VerificationResult and ClaimViolation dataclasses

```python
@dataclass
class ClaimViolation:
    claimed_text: str
    expected_tool: str
    found_in_turn: bool
    found_in_ledger: bool

@dataclass
class VerificationResult:
    verified: bool
    violations: list[ClaimViolation] = field(default_factory=list)
    correction: str | None = None
```

---

## Phase E: Action Gating (`nous/cognitive/action_gate.py`)

### ActionGate

- Receives `call_gate_model` callable (async function) from runner
- Tier 1: Read-only → pass through
- Tier 2: Local write → `_consistency_check` (duplicate detection)
- Tier 3: External/irreversible → `_full_gate` (LLM, 5s timeout, fail-open)

### `_consistency_check` — REVIEW FIX: summarize before comparison

```python
def _consistency_check(self, tool_name, tool_input, ledger):
    # Summarize new args the same way as recorded args
    new_key_args = ledger._summarize_args(tool_name, tool_input)
    recent = [a for a in ledger.actions[-20:] if a.tool_name == tool_name and a.status == "success"]
    for prior in recent:
        if self._args_similar(prior.key_args, new_key_args):
            return GateResult(approved=False, reason=f"Duplicate: {tool_name} already succeeded on turn {prior.turn}", ...)
    return GateResult(approved=True, reason="consistency-pass")
```

### `_full_gate` — timeout and fail-open

```python
async def _full_gate(self, tool_name, tool_input, ledger, user_message):
    if not self._call_gate_model:
        return GateResult(approved=True, reason="no-gate-model")
    try:
        response = await asyncio.wait_for(
            self._call_gate_model(prompt), timeout=5.0
        )
        return GateResult.from_json(response)
    except (TimeoutError, Exception) as e:
        logger.warning("Gate model call failed, failing open: %s", e)
        return GateResult(approved=True, reason=f"gate-error-fail-open: {e}")
```

### GateResult

```python
@dataclass
class GateResult:
    approved: bool
    reason: str
    suggestion: str | None = None

    @classmethod
    def from_json(cls, text: str) -> "GateResult":
        # Parse {"approved": bool, "reason": str} from LLM response
```

---

## Phase F: Tests (`tests/test_execution_integrity.py`)

All pure unit tests, no database required. ~40 tests total.

### ExecutionLedger Tests (~15)
- `test_record_adds_action`
- `test_system_prompt_section_empty`
- `test_system_prompt_section_single_action`
- `test_system_prompt_section_grouping`
- `test_system_prompt_section_token_budget`
- `test_classify_side_effect_read_tools`
- `test_classify_side_effect_write_tools`
- `test_classify_bash_read_commands`
- `test_classify_bash_write_commands`
- `test_classify_bash_git_read`
- `test_classify_bash_git_push`
- `test_summarize_args_write_file`
- `test_summarize_args_bash`
- `test_has_blocked_actions_this_turn`
- `test_one_line_summary`

### ClaimVerifier Tests (~10)
- `test_no_claims_verified`
- `test_claim_with_matching_tool_call`
- `test_claim_without_tool_call`
- `test_claim_matched_by_ledger`
- `test_multiple_violations`
- `test_build_correction_message`
- `test_email_claim_patterns`
- `test_file_claim_patterns`
- `test_git_claim_patterns`
- `test_no_false_positive_on_plans`

### IntentTracker Tests (~5)
- `test_no_ghost_planning_with_tools`
- `test_ghost_planning_detected`
- `test_single_signal_not_enough`
- `test_build_nudge_format`

### ActionGate Tests (~10)
- `test_read_only_passes`
- `test_write_consistency_pass`
- `test_write_duplicate_blocked`
- `test_args_similar_summarized_comparison`
- `test_args_similar_case_insensitive`
- `test_external_with_no_gate_model`
- `test_gate_result_from_json`
- `test_gate_result_from_invalid_json`
- `test_blocked_result_format`
- `test_full_gate_timeout_fails_open`

---

## Build Order

1. **Phase A** — Config settings
2. **Phase B** — ExecutionLedger class (standalone)
3. **Phase D** — ClaimVerifier + IntentTracker (standalone)
4. **Phase E** — ActionGate (standalone)
5. **Phase C** — Runner integration (ties everything together)
6. **Phase F** — Tests
