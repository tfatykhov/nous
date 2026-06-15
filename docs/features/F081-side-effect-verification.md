# F081 — Side-Effect Verification

**Status:** Proposed  
**Filed:** 2026-06-15  
**Motivated by:** Recurring heartbeat false-positive from fact `972360c1`; gap identified in June 14 2026 triage (fact `9afd03fb`)  
**Depends on:** F061 (Subtask Hardening, deployed May 7 2026)

---

## Problem

F061 fully resolved *structural* false-completion modes:

- Empty result (zero-length output)  
- Context-truncation (output cut mid-sentence)  
- Tool-call limit reached without core action  
- Exception swallowed silently  

**The remaining gap:** F061's validator is structural-only. It does **not** verify that *side effects actually occurred* at runtime. A subtask can report a non-empty, well-formed result while having silently skipped its core side effect (e.g., email send, Telegram message, `dag_create`, file write).

### Concrete example

Subtask `e3071451` (longevity weekly) completed with a valid-looking result string but never called `dag_create`. F061 saw a non-empty result and accepted it. The failure was only noticed downstream.

### Existing mechanism

The `success_criteria` column exists in the subtask schema. It is declared at authoring time but **never evaluated** post-execution. The validator ignores it.

---

## Proposed Fix

### Phase 1 — Declarative side-effect assertions (simple, high-ROI)

Extend `success_criteria` to accept a structured assertion list alongside the existing freeform string:

```yaml
success_criteria:
  text: "Must send the weekly email report"
  assertions:
    - tool_called: send_email
    - tool_called: dag_create
      min_calls: 1
    - file_written: /tmp/weekly-report.html
```

At task completion, the F061 validator checks the subtask's recorded `tool_call_log` against these assertions. If any assertion fails → mark subtask `failed` (not `completed`).

**Implementation scope:**
- Extend `DAGNodeSpec` schema with `assertions: list[SideEffectAssertion]`
- Add assertion-checking pass to `SubtaskHardeningMiddleware` (post-result, pre-status-write)
- assertion types: `tool_called`, `file_written`, `db_row_written` (future), `http_call_made` (future)

### Phase 2 — Probe-based verification (for non-logged side effects)

For side effects that don't appear in the tool call log (external HTTP, email delivery receipts):

- Allow a `verification_probe` shell command on a subtask node
- Probe runs after subtask completes; exit 0 = verified, exit 1 = failed
- Mirrors the `completion_check` pattern from F038.1

```yaml
verification_probe: |
  python3 -c "import sys; import json; d=json.load(open('/tmp/email_receipt.json')); sys.exit(0 if d['delivered'] else 1)"
```

### Phase 3 — Retroactive alerting

For subtasks without explicit assertions, apply a heuristic: if the result text *describes* a side effect but zero matching tool calls appear in the log, emit a warning (not a failure — backwards compatible).

---

## Why not just add more censors?

Censors fire at the *agent* level (blocking bad outputs). Side-effect verification must fire at the *harness* level (inspecting the execution record after the subtask finishes). These are orthogonal layers.

---

## Success Criteria

1. A subtask declaring `assertions: [{tool_called: send_email}]` that completes without calling `send_email` is marked `failed`, not `completed`.
2. Existing subtasks without assertions are unaffected (no regression).
3. The recurring heartbeat false-positive from fact `972360c1` no longer surfaces because the structural gap is now documented and on a fix track (not "future someday").

---

## Suppression Note

The heartbeat fact `972360c1` ("False task completion — side-effect verification") has been flagged **4+ times** since June 2026. Each triage reaches the same conclusion:

- F061 handles the *structural* half → ✅ done  
- Side-effect verification → **this spec (F081)**

After F081 is implemented, fact `972360c1` should be superseded/archived.

---

## Open Questions

1. Should `tool_called` assertions be order-sensitive (sequence) or order-agnostic (set)?  
2. Should probe failures count toward the subtask retry budget (F061's 1x recoverable retry)?  
3. DAG authoring UX: inline assertions vs. a separate `verify:` block?
