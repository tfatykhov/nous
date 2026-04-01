# F033: Subtask Completion Validation — Prevent False "Completed" Status

**Created:** 2026-04-01
**Priority:** Medium-High
**Category:** Architecture / Reliability
**Triggered by:** NYC Weekend Weather Report Day 2 — task marked "completed" without sending email

---

## Problem

Subtasks can be marked "completed" even when they fail to perform their core action. Two failure modes observed:

### Mode 1: Timeout (Task 4991de5e)
- Subtask spent 15 minutes researching weather data
- Never reached the email send step
- Correctly marked as **failed** (timeout)

### Mode 2: Silent Truncation (Task 562e3bea)
- Subtask gathered all weather data successfully
- Hit **context window limit** mid-execution
- Output truncated at: *"I have everything I need. Let me compose and send the email:"*
- System marked it **completed** because no error was thrown
- **Email was never sent** — user had to manually re-request

Mode 2 is the more dangerous failure: it silently drops the user's request with a false success signal.

---

## Root Cause

1. **No output validation** — The system does not verify that the subtask's result contains evidence of successful action completion (e.g., email confirmation, tool call result).

2. **Context budget mismanagement** — Research-heavy subtasks (multiple web fetches with verbose HTML) exhaust their context window on the research phase, leaving no room for the action phase.

3. **No "must-complete" action markers** — Subtasks have no way to declare "this task is not done until X happens" (e.g., email send confirmation).

---

## Proposed Fix

### 1. Result Validation (Short-term)
- After subtask completion, scan the result for truncation signals:
  - Ends with incomplete sentences ("Let me...", "Now I'll...", "I have everything I need...")
  - Contains no evidence of the requested action (no email send confirmation, no file write, etc.)
- If detected, mark task as **failed/incomplete** instead of completed
- Notify the parent context

### 2. Context Budget Reservation (Medium-term)
- For subtasks that include both research + action steps:
  - Reserve a minimum context budget (e.g., 20%) for the action phase
  - When research consumption approaches the budget threshold, stop fetching and proceed to action
  - Prefer compact data extraction over raw page fetches

### 3. Critical Action Declaration (Long-term)
- Allow subtask prompts to declare critical actions: `[MUST_COMPLETE: send_email]`
- If a subtask ends without invoking the declared critical action, auto-mark as failed
- Retry logic or parent notification on critical action miss

---

## Acceptance Criteria

- [ ] A subtask that ends mid-sentence is never marked "completed"
- [ ] A subtask with a send_email objective that doesn't call the email tool is marked "incomplete"
- [ ] Research-heavy subtasks budget context to ensure action steps have room to execute
- [ ] Parent task / user is notified when a subtask silently fails

---

## Related

- Daily Ski Weather Report timeout failure (Task 307c1b09, ~March 9)
- NYC Weather Day 2 original timeout (Task 4991de5e, April 1)
- NYC Weather Day 2 rerun silent failure (Task 562e3bea, April 1)
